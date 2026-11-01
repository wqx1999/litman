"""A repo's bind refused after its commit: the clone stays for it to point at.

Rolling the clone back while recovery goes on to finish the bind left
``code-clones`` naming a folder with nothing in it. Before the commit, nothing
lands, so the rollback is still right.
"""

from __future__ import annotations

import errno
import io
import os
import types
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from rich.console import Console
from ruamel.yaml import YAML

from litman.cli import cli
from litman.commands import trash as trash_cmd
from litman.core import views
from litman.core.atomic import StagedWrite
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.exceptions import CodeError, RepoBindPendingError
from tests.commands.test_link import _make_paper
from tests.commands.test_restore import _git_local_upstream

PAPER = "2024_Foo_Bar"
REPO = "srcrepo"
FINISHED = (
    "recovered: the last command's change is saved, no need to run it again. "
    "Run `lit refresh-views` to update the links."
)
# What a bind promotes first, so past its decision point and with nothing landed.
PAST_COMMIT = "metadata.yaml"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    home = tmp_path / "lib"
    home.mkdir()
    vault = create_vault(home)
    _make_paper(vault, PAPER, status="inbox")
    views.write_index(vault, list_papers(vault))
    views.rebuild_views(vault, list_papers(vault))
    return vault


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / REPO
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    return src


def _refuse_once(monkeypatch: Any, name: str) -> list[str]:
    """Refuse the first time the bind's own staged write replaces ``name``."""
    real = os.replace
    refused: list[str] = []

    def replace(src: Any, dst: Any) -> None:
        if "code-bind-" in str(src) and Path(dst).name == name and not refused:
            refused.append(str(dst))
            raise PermissionError(errno.EACCES, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return refused


def _refuse_before_commit(monkeypatch: Any) -> None:
    """The manifest step comes before the decision point."""

    def refuse(_self: Any) -> None:
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(StagedWrite, "_write_manifest", refuse)


def _yaml(path: Path) -> dict[str, Any]:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def _code_clones(vault: Path) -> list[str]:
    return _yaml(vault / "papers" / PAPER / "metadata.yaml").get("code-clones") or []


def _assert_bound_and_whole(vault: Path, *expected_in_repo: str) -> None:
    root = vault / "codes" / REPO
    assert _code_clones(vault) == [REPO]
    assert _yaml(root / "repo-meta.yaml")["papers"] == [PAPER]
    assert (root / "notes.md").is_file()
    for name in expected_in_repo:
        assert (root / "repo" / name).is_file(), name


def _add(vault: Path, source: Path, *flags: str) -> Any:
    return CliRunner().invoke(
        cli, ["code", "add", str(source), "--paper", PAPER, *flags, "--library", str(vault)]
    )


def test_code_add_past_the_commit_keeps_the_clone_for_the_next_lit_to_bind(
    vault: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as held:
        refused = _refuse_once(held, PAST_COMMIT)
        result = _add(vault, source)

    assert refused, result.output
    assert isinstance(result.exception, PermissionError)
    assert (vault / "codes" / REPO / "repo" / "a.py").is_file()

    following = CliRunner().invoke(cli, ["list", "--library", str(vault)])

    assert following.exit_code == 0, following.output
    assert FINISHED in following.output.splitlines()
    _assert_bound_and_whole(vault, "a.py")


def test_code_add_move_past_the_commit_leaves_the_source_alone(
    vault: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as held:
        refused = _refuse_once(held, PAST_COMMIT)
        result = _add(vault, source, "--move")

    assert refused, result.output
    assert isinstance(result.exception, PermissionError)
    assert (source / "a.py").is_file()


def test_code_add_before_the_commit_rolls_back_and_can_be_run_again(
    vault: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as held:
        _refuse_before_commit(held)
        result = _add(vault, source, "--move")

    assert isinstance(result.exception, PermissionError)
    assert not (vault / "codes" / REPO).exists()
    assert (source / "a.py").is_file()
    assert _code_clones(vault) == []

    again = _add(vault, source)

    assert again.exit_code == 0, again.output
    assert "recovered:" not in again.output
    _assert_bound_and_whole(vault, "a.py")


def _reclone(vault: Path, upstream: Path) -> None:
    trash_cmd._reclone_missing_repo(vault, PAPER, REPO, str(upstream), link_errors=[])


def test_a_reclone_past_the_commit_keeps_the_checkout_for_the_next_lit_to_bind(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = _git_local_upstream(tmp_path, "upstream-x")

    with monkeypatch.context() as held:
        refused = _refuse_once(held, PAST_COMMIT)
        with pytest.raises(RepoBindPendingError):
            _reclone(vault, upstream)

    assert refused
    assert (vault / "codes" / REPO / "repo" / ".git").is_dir()

    following = CliRunner().invoke(cli, ["list", "--library", str(vault)])

    assert following.exit_code == 0, following.output
    assert FINISHED in following.output.splitlines()
    _assert_bound_and_whole(vault)
    assert (vault / "codes" / REPO / "repo" / ".git").is_dir()


def test_a_reclone_before_the_commit_rolls_back(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = _git_local_upstream(tmp_path, "upstream-x")

    with monkeypatch.context() as held:
        _refuse_before_commit(held)
        with pytest.raises(CodeError) as raised:
            _reclone(vault, upstream)

    assert not isinstance(raised.value, RepoBindPendingError)
    assert not (vault / "codes" / REPO).exists()
    assert _code_clones(vault) == []


def test_restore_says_the_reclone_is_there_and_does_not_call_it_failed(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = _git_local_upstream(tmp_path, "upstream-x")
    printed = io.StringIO()
    monkeypatch.setattr(trash_cmd, "console", Console(file=printed, width=300, color_system=None))
    result = types.SimpleNamespace(paper_id=PAPER, missing_repos={REPO: str(upstream)})

    with monkeypatch.context() as held:
        refused = _refuse_once(held, PAST_COMMIT)
        trash_cmd._handle_missing_repos(
            vault,
            result,  # type: ignore[arg-type]  # only these two fields are read
            skip_confirm=True,
            link_errors=[],
        )

    assert refused
    output = printed.getvalue()
    assert f"Re-cloned '{REPO}'" in output
    assert "the next `lit` finishes it." in output
    assert "failed" not in output
    assert "restore-all" not in output
