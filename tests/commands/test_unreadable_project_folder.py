"""A project folder litman cannot read: every command still finishes and says so.

A project's ``litman_reflib/`` or ``litman_code/`` can stop being readable
(permissions changed, a folder another user made, a read-only mount). Every
command that touches it used to finish its own write and then die in a
traceback, leaving the folder half updated; ``lit health-check`` died too, so
nothing could say what was wrong. ``deny`` (tests/_project_folder.py) makes the
folder unreadable on every platform; one test per area also runs the real thing
with ``chmod`` where that exists.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.checks import check_project_references
from litman.core.document import list_papers
from tests._project_folder import ACCESS_DENIED, Library, deny, make_library

HUBS = ["litman_reflib", "litman_code"]

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod does not take read access away from a folder on Windows",
)
not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads a folder whatever its permission bits",
)


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    return make_library(tmp_path)


def _run(lib: Library, argv: list[str]) -> tuple[int, str, BaseException | None]:
    """Exit code, output with one space between words (so a line Rich wrapped
    still matches), and the exception the command ended with."""
    result = CliRunner().invoke(cli, [*argv, "--library", str(lib.vault)])
    return result.exit_code, " ".join(result.output.split()), result.exception


def _project_issues(lib: Library) -> list[str]:
    return [
        i.message
        for i in check_project_references(lib.vault, list_papers(lib.vault))
    ]


# ---------------------------------------------------------------------------
# lit health-check: says which folder it could not read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hub", HUBS)
def test_health_check_names_a_project_folder_it_cannot_read(
    lib: Library, monkeypatch: pytest.MonkeyPatch, hub: str
) -> None:
    with monkeypatch.context() as patch:
        deny(patch, lib.hub(hub))
        issues = _project_issues(lib)
        code, output, exc = _run(lib, ["health-check"])

    # One finding for the project, not one per member it could not see.
    assert len(issues) == 1
    assert issues[0].startswith(f"could not read {lib.hub(hub)}")
    assert issues[0].endswith(f"({ACCESS_DENIED}) — project 'pp' not checked")
    assert code == 1
    assert isinstance(exc, SystemExit), exc
    assert "project 'pp' not checked" in output
    assert "fix that, then run `lit health-check --fix`" in output


@posix_only
@not_root
@pytest.mark.parametrize("hub", HUBS)
def test_health_check_names_a_folder_chmod_took_read_access_from(
    lib: Library, hub: str
) -> None:
    """The real failure, nothing injected."""
    lib.hub(hub).chmod(0)
    try:
        issues = _project_issues(lib)
        code, output, exc = _run(lib, ["health-check"])
    finally:
        lib.hub(hub).chmod(0o755)

    assert len(issues) == 1
    assert issues[0].endswith("(Permission denied) — project 'pp' not checked")
    assert code == 1
    assert isinstance(exc, SystemExit), exc
    assert "project 'pp' not checked" in output


# ---------------------------------------------------------------------------
# A write: it lands, the command finishes, one line says what to do
# ---------------------------------------------------------------------------

LINE = (
    f"warning: could not update a project folder ({ACCESS_DENIED}). "
    "Fix that, then run `lit health-check --fix`."
)


def _meta(lib: Library, paper_id: str) -> dict:
    from ruamel.yaml import YAML

    path = lib.vault / "papers" / paper_id / "metadata.yaml"
    return YAML(typ="safe").load(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _projects(lib: Library) -> dict:
    from litman.core.config import load_config

    return dict(load_config(lib.vault).projects)


# (argv, did the command's own write land, the hubs whose denial it meets)
REBUILDS = {
    "link --rebuild-all": (["link", "--rebuild-all"], None, HUBS),
    "refresh-views": (["refresh-views"], None, HUBS),
    "modify --add-tag projects": (
        ["modify", "p3", "--add-tag", "projects=pp"],
        lambda lib: "pp" in (_meta(lib, "p3").get("projects") or []),
        HUBS,
    ),
    # A title is in REFERENCES.md only; litman_code/ is not touched.
    "modify --set title": (
        ["modify", "p2", "--set", "title=New title"],
        lambda lib: _meta(lib, "p2").get("title") == "New title",
        ["litman_reflib"],
    ),
    "rename": (
        ["rename", "p2", "p2b"],
        lambda lib: bool(_meta(lib, "p2b")) and not _meta(lib, "p2"),
        HUBS,
    ),
    "project rename": (
        ["project", "rename", "pp", "pp2"],
        lambda lib: "pp2" in _projects(lib),
        HUBS,
    ),
}


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("name", list(REBUILDS))
def test_a_rebuild_finishes_and_says_a_project_folder_was_not_updated(
    lib: Library, monkeypatch: pytest.MonkeyPatch, name: str, hub: str
) -> None:
    argv, landed, meets = REBUILDS[name]
    with monkeypatch.context() as patch:
        deny(patch, lib.hub(hub))
        code, output, exc = _run(lib, argv)

    assert code == 0, f"{output} {exc!r}"
    assert output.count(LINE) == (1 if hub in meets else 0)
    if landed is not None:
        assert landed(lib)
    assert lib.project_dir.joinpath("my_own_file.txt").is_file()

    # The way out it names.
    CliRunner().invoke(cli, ["health-check", "--fix", "--library", str(lib.vault)])
    assert _project_issues(lib) == []


@pytest.mark.parametrize("hub", HUBS)
def test_health_check_fix_does_not_claim_a_folder_it_could_not_change(
    lib: Library, monkeypatch: pytest.MonkeyPatch, hub: str
) -> None:
    with monkeypatch.context() as patch:
        deny(patch, lib.hub(hub))
        code, output, exc = _run(lib, ["health-check", "--fix"])

    assert isinstance(exc, SystemExit), exc
    assert code == 1
    assert "project_references: cleaned" not in output
    assert output.count(LINE) == 1

    # Fixed, the same command finishes the job.
    code, output, exc = _run(lib, ["health-check", "--fix"])
    assert _project_issues(lib) == []


@posix_only
@not_root
@pytest.mark.parametrize("hub", HUBS)
def test_rename_finishes_when_chmod_took_read_access_from_a_hub(
    lib: Library, hub: str
) -> None:
    """The real failure, nothing injected."""
    lib.hub(hub).chmod(0)
    try:
        code, output, exc = _run(lib, ["rename", "p2", "p2b"])
    finally:
        lib.hub(hub).chmod(0o755)

    assert code == 0, f"{output} {exc!r}"
    assert output.count(
        "warning: could not update a project folder (Permission denied). "
        "Fix that, then run `lit health-check --fix`."
    ) == 1
    assert _meta(lib, "p2b")

    CliRunner().invoke(cli, ["health-check", "--fix", "--library", str(lib.vault)])
    assert _project_issues(lib) == []


# ---------------------------------------------------------------------------
# One paper's links: lit link / unlink / rm
# ---------------------------------------------------------------------------

SINGLE = {
    "link": (
        ["link", "p3", "--project", "pp"],
        lambda lib: "pp" in (_meta(lib, "p3").get("projects") or []),
        ["litman_reflib"],  # p3 has no repo: litman_code/ is not touched
    ),
    "unlink": (
        ["unlink", "p2", "--project", "pp"],
        lambda lib: "pp" not in (_meta(lib, "p2").get("projects") or []),
        ["litman_reflib"],
    ),
    "rm": (["rm", "p1", "-y"], lambda lib: not _meta(lib, "p1"), HUBS),
    "rm --purge": (
        ["rm", "p1", "--purge", "-y"], lambda lib: not _meta(lib, "p1"), HUBS
    ),
}


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("name", list(SINGLE))
def test_a_one_paper_write_finishes_and_says_a_project_folder_was_not_updated(
    lib: Library, monkeypatch: pytest.MonkeyPatch, name: str, hub: str
) -> None:
    argv, landed, meets = SINGLE[name]
    with monkeypatch.context() as patch:
        deny(patch, lib.hub(hub))
        code, output, exc = _run(lib, argv)

    assert code == 0, f"{output} {exc!r}"
    assert output.count(LINE) == (1 if hub in meets else 0)
    assert landed(lib)
    assert lib.project_dir.joinpath("my_own_file.txt").is_file()

    CliRunner().invoke(cli, ["health-check", "--fix", "--library", str(lib.vault)])
    assert _project_issues(lib) == []


def test_unlink_still_shows_the_relevance_it_drops(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dropped relevance- note is shown so it can be copied back from the
    terminal; a crash after the commit used to lose it for good."""
    _run(lib, ["link", "p2", "--project", "pp", "--relevance", "why it matters"])
    with monkeypatch.context() as patch:
        deny(patch, lib.reflib)
        code, output, exc = _run(lib, ["unlink", "p2", "--project", "pp"])

    assert code == 0, f"{output} {exc!r}"
    assert "why it matters" in output
    assert "Paper link: not removed" in output
    assert output.count(LINE) == 1


@posix_only
@not_root
@pytest.mark.parametrize("hub", HUBS)
def test_rm_finishes_when_chmod_took_read_access_from_a_hub(
    lib: Library, hub: str
) -> None:
    """The real failure, nothing injected."""
    lib.hub(hub).chmod(0)
    try:
        code, output, exc = _run(lib, ["rm", "p1", "-y"])
    finally:
        lib.hub(hub).chmod(0o755)

    assert code == 0, f"{output} {exc!r}"
    assert output.count(
        "warning: could not update a project folder (Permission denied). "
        "Fix that, then run `lit health-check --fix`."
    ) == 1
    assert not _meta(lib, "p1")
    # The rest of the delete ran: the trash entry is there.
    assert any((lib.vault / ".trash").glob("p1-*"))

    CliRunner().invoke(cli, ["health-check", "--fix", "--library", str(lib.vault)])
    assert _project_issues(lib) == []
