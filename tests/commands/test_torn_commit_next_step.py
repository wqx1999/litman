"""A staged write refused after its commit: what the next ``lit`` says and leaves.

Recovery finishes the write, but the steps after the commit never ran, and for
most commands a rerun would refuse because the old name is gone already.

Each write here is refused after its first file landed. One refused at its
first file changed nothing and is called off: the next ``lit`` says nothing.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core import atomic, views
from litman.core.checks import run_all_checks
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.trash import list_trash
from tests.commands.test_link import _make_paper

PAPER = "2024_Foo_Bar"
GONE = "2020_Gone_x"
CITING = "2023_Cites_Foo"
FINISHED = (
    "recovered: the last command's change is saved, no need to run it again. "
    "Run `lit refresh-views` to update the links."
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    vault = create_vault(tmp_path)
    _make_paper(vault, PAPER, status="inbox", related=[GONE])
    views.write_index(vault, list_papers(vault))
    views.rebuild_views(vault, list_papers(vault))
    return vault


def _lit(vault: Path, *args: str) -> Any:
    return CliRunner().invoke(cli, [*args, "--library", str(vault)])


def _refuse_once(monkeypatch: Any, name: str) -> list[str]:
    """Refuse the first time a staged write promotes ``name``."""
    real = os.replace
    refused: list[str] = []

    def replace(src: Any, dst: Any) -> None:
        if ".litman-staging" in str(src) and Path(dst).name == name and not refused:
            refused.append(str(dst))
            raise PermissionError(errno.EACCES, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return refused


def _view_drift(vault: Path) -> int:
    issues = run_all_checks(vault, list_papers(vault))
    return sum(1 for issue in issues if issue.category == "views_vs_metadata")


def _torn(vault: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    with monkeypatch.context() as held:
        refused = _refuse_once(held, "INDEX.json")
        result = _lit(vault, *args)
    assert refused, result.output
    assert isinstance(result.exception, PermissionError)


@pytest.mark.parametrize(
    "args",
    [("rename", PAPER, "2024_Foo_Baz"), ("modify", PAPER, "--set", "status=skim")],
    ids=["rename", "modify"],
)
def test_the_next_lit_says_it_is_done_and_refresh_views_finishes_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    _torn(vault, monkeypatch, *args)

    following = _lit(vault, "list")

    assert following.exit_code == 0, following.output
    assert FINISHED in following.output.splitlines()
    assert len(FINISHED) <= 120
    # What the sentence is for: the links really are behind until it is followed.
    assert _view_drift(vault) > 0

    repaired = _lit(vault, "refresh-views")

    assert repaired.exit_code == 0, repaired.output
    assert _view_drift(vault) == 0
    assert "recovered:" not in repaired.output


def test_the_next_lit_says_so_only_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _torn(vault, monkeypatch, "rename", PAPER, "2024_Foo_Baz")

    assert "recovered:" in _lit(vault, "list").output
    assert "recovered:" not in _lit(vault, "list").output


def test_rm_is_told_to_run_again_because_the_folder_has_not_moved(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A paper related to PAPER: rm drops PAPER from its record first, so that
    # file has landed when INDEX.json is refused.
    _make_paper(vault, PAPER, status="inbox", related=[GONE, CITING])
    _make_paper(vault, CITING, status="inbox", related=[PAPER])
    views.write_index(vault, list_papers(vault))

    _torn(vault, monkeypatch, "rm", PAPER, "--yes")

    following = _lit(vault, "list")

    assert (
        f"recovered: the last command (rm-{PAPER}) was saved but did not finish. Run it again."
        in following.output.splitlines()
    )
    assert FINISHED not in following.output.splitlines()
    assert (vault / "papers" / PAPER).is_dir()

    again = _lit(vault, "rm", PAPER, "--yes")

    assert again.exit_code == 0, again.output
    assert not (vault / "papers" / PAPER).exists()
    assert [entry.paper_id for entry in list_trash(vault)] == [PAPER]


@pytest.mark.parametrize(
    ("op_id", "rerun"),
    [
        ("rm-2024_A", True),
        ("code-unbind-a_repo", True),
        ("project-rm-a", False),
        ("taxonomy-rm-topics", False),
        ("rename-2024_A-to-2024_B", False),
        ("restore-2024_A", False),
    ],
)
def test_only_the_commands_with_real_work_left_are_told_to_run_again(
    op_id: str, rerun: bool
) -> None:
    assert ("Run it again" in atomic._rolled_forward_message(op_id)) is rerun


def test_a_write_that_never_committed_says_nothing(vault: Path) -> None:
    op = vault / ".litman-staging" / "modify-aborted"
    op.mkdir(parents=True)
    (op / "metadata.yaml").write_text("x: 1\n", encoding="utf-8")

    following = _lit(vault, "list")

    assert following.exit_code == 0, following.output
    assert "recovered:" not in following.output


def test_a_restore_past_its_commit_leaves_the_folder_for_recovery_to_finish(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed = _lit(vault, "rm", PAPER, "--yes")
    assert removed.exit_code == 0, removed.output
    entry = list_trash(vault)[0].entry_path
    sealed = (entry / "metadata.yaml").read_text(encoding="utf-8")
    assert GONE in sealed  # what the restore prunes, so a stale copy would show

    with monkeypatch.context() as held:
        # Refused last: A's pruned metadata.yaml has landed by then.
        refused = _refuse_once(held, "INDEX.json")
        result = _lit(vault, "trash", "restore", PAPER)

    # At the failure itself: not moved back into the trash.
    assert refused, result.output
    assert isinstance(result.exception, PermissionError)
    assert (vault / "papers" / PAPER / "metadata.yaml").is_file()
    assert not entry.exists()

    following = _lit(vault, "list")

    assert following.exit_code == 0, following.output
    assert FINISHED in following.output.splitlines()
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert PAPER in json.dumps(index)
    metadata = (vault / "papers" / PAPER / "metadata.yaml").read_text(encoding="utf-8")
    assert f"id: {PAPER}" in metadata
    assert GONE not in metadata
    assert list((vault / ".trash").iterdir()) == []
    assert list_trash(vault) == []


def test_a_restore_refused_at_its_first_file_goes_back_into_the_trash(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed = _lit(vault, "rm", PAPER, "--yes")
    assert removed.exit_code == 0, removed.output
    entry = list_trash(vault)[0].entry_path

    with monkeypatch.context() as held:
        refused = _refuse_once(held, "metadata.yaml")
        result = _lit(vault, "trash", "restore", PAPER)

    # Nothing landed, so nothing waits for recovery: the paper is in the trash.
    assert refused, result.output
    assert isinstance(result.exception, PermissionError)
    assert entry.is_dir()
    assert not (vault / "papers" / PAPER).exists()
    assert not any((vault / ".litman-staging").iterdir())

    following = _lit(vault, "list")

    assert following.exit_code == 0, following.output
    assert "recovered:" not in following.output
    assert [e.paper_id for e in list_trash(vault)] == [PAPER]
