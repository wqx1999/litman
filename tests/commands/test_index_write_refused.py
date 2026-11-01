"""INDEX.json cannot be written, after a command's change is already on disk.

Whatever holds it, the change has happened: the command finishes the rest, says
so, and exits as a success. ``os.replace`` is what refuses, so everything above it
runs for real. Only ``write_index``'s own tmp is refused: a command's staged write
goes first and can carry INDEX.json as well, which is the torn-commit path.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from rich.console import Console

from litman.cli import cli
from litman.commands import health as health_cmd
from litman.commands._hub_report import refreshed_line
from litman.core import correctors, views
from litman.core.checks import run_all_checks
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.portable_link import is_portable_link
from litman.exceptions import IndexWriteError
from tests.commands.test_link import _make_paper, _write_config_with_project

PAPER = "2024_Foo_Bar"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    vault = create_vault(tmp_path)
    _make_paper(vault, PAPER, status="inbox")
    views.write_index(vault, list_papers(vault))
    views.rebuild_views(vault, list_papers(vault))
    return vault


def _refuse_index(monkeypatch: Any) -> None:
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if Path(src).name == "INDEX.json.tmp":
            raise PermissionError(errno.EACCES, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)


def _status(vault: Path) -> str:
    text = (vault / "papers" / PAPER / "metadata.yaml").read_text(encoding="utf-8")
    return next(ln for ln in text.splitlines() if ln.startswith("status:"))


def test_a_write_finishes_and_says_so_when_index_is_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as held:
        _refuse_index(held)
        result = CliRunner().invoke(
            cli, ["modify", PAPER, "--set", "status=skim", "--library", str(vault)]
        )

    assert result.exit_code == 0, result.output
    assert result.exception is None
    # Said, on one line, once.
    lines = [ln for ln in result.output.splitlines() if "could not update" in ln]
    assert lines == [
        "warning: could not update INDEX.json (Access is denied). "
        "Your change is saved. Run `lit refresh-views` to update it."
    ]
    assert len(lines[0]) <= 125
    # The change, and what comes after INDEX: the views moved with it.
    assert _status(vault) == "status: skim"
    assert is_portable_link(vault / "views" / "by-status" / "skim" / PAPER)
    assert not is_portable_link(vault / "views" / "by-status" / "inbox" / PAPER)
    assert not (vault / "INDEX.json.tmp").exists()

    repaired = CliRunner().invoke(cli, ["refresh-views", "--library", str(vault)])
    assert repaired.exit_code == 0, repaired.output
    assert '"status": "skim"' in (vault / "INDEX.json").read_text(encoding="utf-8")


def test_a_write_with_no_staged_index_says_what_refresh_views_then_repairs(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reconcile_derived`` alone: INDEX really is behind, and the line is true."""
    before = (vault / "INDEX.json").read_text(encoding="utf-8")
    _make_paper(vault, "2024_New_x")

    with monkeypatch.context() as held:
        _refuse_index(held)
        derived = correctors.reconcile_derived(vault, project_refs=False)

    assert derived["index"] == 0
    assert (vault / "INDEX.json").read_text(encoding="utf-8") == before
    assert is_portable_link(vault / "views" / "by-status" / "inbox" / "2024_New_x")

    repaired = CliRunner().invoke(cli, ["refresh-views", "--library", str(vault)])
    assert repaired.exit_code == 0, repaired.output
    assert "2024_New_x" in (vault / "INDEX.json").read_text(encoding="utf-8")


def test_project_links_are_still_made_when_index_is_refused(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _write_config_with_project(vault, "proj", project)
    _make_paper(vault, "2024_Member_x", projects=["proj"])

    with monkeypatch.context() as held:
        _refuse_index(held)
        derived = correctors.reconcile_derived(vault, project_refs=True)

    assert derived["index"] == 0
    assert derived["project_refs"] == 1
    assert is_portable_link(project / "litman_reflib" / "2024_Member_x")
    assert "2024_Member_x" in (project / "litman_reflib" / "REFERENCES.md").read_text(
        encoding="utf-8"
    )


def test_a_failure_that_is_not_about_the_disk_still_propagates(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an ``OSError`` is the file being held; a bug must stay a crash."""

    def broken(*_a: Any, **_k: Any) -> None:
        raise ValueError("a bug")

    monkeypatch.setattr(views, "write_index", broken)
    with pytest.raises(ValueError, match="a bug"):
        correctors.reconcile_derived(vault, project_refs=False)


def test_refresh_views_gives_one_clear_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as held:
        _refuse_index(held)
        result = CliRunner().invoke(cli, ["refresh-views", "--library", str(vault)])

    assert result.exit_code != 0
    assert isinstance(result.exception, IndexWriteError)
    assert str(result.exception) == (
        "Could not update INDEX.json (Access is denied). Run `lit refresh-views` again."
    )
    assert "INDEX.json updated" not in result.output
    assert not (vault / "INDEX.json.tmp").exists()


def test_refresh_views_still_repairs_views_and_project_links_first(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (vault / "INDEX.json").read_text(encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    _write_config_with_project(vault, "proj", project)
    _make_paper(vault, "2024_Member_x", projects=["proj"])
    stale = vault / "views" / "by-status" / "inbox" / PAPER
    assert is_portable_link(stale)
    (vault / "views" / "by-status" / "skim").mkdir(parents=True, exist_ok=True)
    os.replace(stale, vault / "views" / "by-status" / "skim" / PAPER)

    with monkeypatch.context() as held:
        _refuse_index(held)
        result = CliRunner().invoke(cli, ["refresh-views", "--library", str(vault)])

    assert result.exit_code == 1
    assert isinstance(result.exception, IndexWriteError)
    # The views and the project side were done before it said so.
    assert is_portable_link(stale)
    assert not os.path.lexists(vault / "views" / "by-status" / "skim" / PAPER)
    assert is_portable_link(vault / "views" / "by-status" / "inbox" / "2024_Member_x")
    assert is_portable_link(project / "litman_reflib" / "2024_Member_x")
    assert "2024_Member_x" in (project / "litman_reflib" / "REFERENCES.md").read_text(
        encoding="utf-8"
    )
    assert (vault / "INDEX.json").read_text(encoding="utf-8") == before


_WRITES = {
    "modify": (["modify", PAPER, "--set", "status=skim"], None),
    "rm": (["rm", PAPER, "--yes"], None),
    "restore": (["trash", "restore", PAPER], ["rm", PAPER, "--yes"]),
    "rename": (["rename", PAPER, "2024_Foo_Baz"], None),
}


def _prepare(vault: Path, write: str) -> list[str]:
    args, first = _WRITES[write]
    if first is not None:
        prepared = CliRunner().invoke(cli, [*first, "--library", str(vault)])
        assert prepared.exit_code == 0, prepared.output
    return [*args, "--library", str(vault)]


@pytest.mark.parametrize("write", list(_WRITES))
def test_the_closing_line_claims_both_when_both_were_refreshed(
    vault: Path, write: str
) -> None:
    result = CliRunner().invoke(cli, _prepare(vault, write))

    assert result.exit_code == 0, result.output
    assert "INDEX.json + views/" in result.output
    assert " refreshed." in result.output


@pytest.mark.parametrize("write", list(_WRITES))
def test_the_closing_line_does_not_claim_an_index_it_could_not_write(
    vault: Path, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    args = _prepare(vault, write)
    with monkeypatch.context() as held:
        _refuse_index(held)
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert "could not update INDEX.json" in result.output
    assert "INDEX.json + " not in result.output
    assert "INDEX.json refreshed" not in result.output
    assert "views/ refreshed." in result.output


@pytest.mark.parametrize("write", list(_WRITES))
def test_the_closing_line_goes_when_nothing_was_refreshed(
    vault: Path, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    args = _prepare(vault, write)

    def left(*_a: Any, view_errors: list[Any], **_k: Any) -> dict[str, int]:
        view_errors.append(("by-status", OSError(errno.EACCES, "Access is denied")))
        return {}

    with monkeypatch.context() as held:
        _refuse_index(held)
        held.setattr(views, "update_views_for_paper", left)
        held.setattr(views, "rebuild_views", left)
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert "could not update INDEX.json" in result.output
    assert " refreshed." not in result.output


@pytest.mark.parametrize(
    ("derived", "also", "line"),
    [
        ({"index": 1, "view_errors": []}, (), "[dim]INDEX.json + views/ refreshed.[/]"),
        ({"index": 1, "view_errors": ["x"]}, (), "[dim]INDEX.json refreshed.[/]"),
        ({"index": 0, "view_errors": []}, (), "[dim]views/ refreshed.[/]"),
        ({"index": 0, "view_errors": ["x"]}, (), None),
        (
            {"index": 0, "view_errors": ["x"]},
            ("notes.md heading",),
            "[dim]notes.md heading refreshed.[/]",
        ),
    ],
    ids=["both", "views-left", "index-left", "both-left", "only-the-rest"],
)
def test_refreshed_line(derived: dict[str, Any], also: tuple[str, ...], line: str | None) -> None:
    assert refreshed_line(derived, *also) == line


def test_a_new_vault_gives_one_clear_error_and_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_index(monkeypatch)
    with pytest.raises(IndexWriteError, match=r"Could not write INDEX\.json in .*Try again"):
        create_vault(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_fix_does_not_claim_index_it_could_not_write(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_cmd, "console", Console(width=300))
    index = vault / "INDEX.json"
    stale = index.read_text(encoding="utf-8").replace(PAPER, "2020_Gone_x")
    index.write_text(stale, encoding="utf-8")
    assert "index_vs_disk" in {i.category for i in run_all_checks(vault, list_papers(vault))}

    with monkeypatch.context() as held:
        _refuse_index(held)
        result = CliRunner().invoke(
            cli, ["health-check", "--fix", "--library", str(vault)]
        )

    assert "could not update INDEX.json" in result.output
    assert "index_vs_disk: cleaned" not in result.output


@pytest.mark.parametrize("rewrite", ["write_index", "rewrite_index_dropping_ids"])
def test_a_failed_index_write_leaves_no_tmp_and_the_old_index(
    vault: Path, monkeypatch: pytest.MonkeyPatch, rewrite: str
) -> None:
    before = (vault / "INDEX.json").read_text(encoding="utf-8")
    _refuse_index(monkeypatch)

    with pytest.raises(PermissionError):
        if rewrite == "write_index":
            views.write_index(vault, [])
        else:
            views.rewrite_index_dropping_ids(vault, {PAPER})

    assert (vault / "INDEX.json").read_text(encoding="utf-8") == before
    assert not (vault / "INDEX.json.tmp").exists()
