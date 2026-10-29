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
