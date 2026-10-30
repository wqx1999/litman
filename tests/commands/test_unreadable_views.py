"""A views/ folder litman cannot read.

``chmod 000`` on views/, one of its views or a bucket, or a copy owned by
another account: `lit health-check` stopped with a Python error, and the web
UI's health panel answered with a server error. The check now names the folder
in one error and still checks the other views; ``--fix`` finishes. ``deny``
(tests/_project_folder.py) refuses the folder on every platform; the last test
runs the real thing with ``chmod``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from litman.core.portable_link import is_portable_link, remove_link_if_present
from tests._project_folder import (
    ACCESS_DENIED,
    Library,
    deny,
    make_library,
    not_root,
    posix_only,
    run_lit,
)
from tests.commands.test_readonly_views import ANY_VIEWS_LINE, view_issues, views_line

HINT = "make it readable, then run `lit health-check --fix`"
# A view checked after the refused one, and the link it misses.
ELSEWHERE = (
    "metadata implies views/by-status/inbox/p2 but the link is missing "
    "(derived view out of date)"
)


def unreadable(folder: str, reason: str = ACCESS_DENIED) -> str:
    return f"{folder} cannot be read ({reason}), so it was not checked"


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    """make_library's p1–p3 with views/ built, and one link gone from
    views/by-status/ for the check to find."""
    lib = make_library(tmp_path)
    code, output, exc = run_lit(lib, ["refresh-views"])
    assert code == 0, f"{output} {exc!r}"
    assert remove_link_if_present(_inbox_p2(lib))
    return lib


def _inbox_p2(lib: Library) -> Path:
    return lib.vault / "views" / "by-status" / "inbox" / "p2"


# by-project is the first view checked, by-status the last.
FOLDERS: dict[str, Callable[[Library], Path]] = {
    "views/": lambda lib: lib.vault / "views",
    "views/by-project/": lambda lib: lib.vault / "views" / "by-project",
    "views/by-project/pp/": lambda lib: lib.vault / "views" / "by-project" / "pp",
}


@pytest.mark.parametrize("folder", list(FOLDERS))
def test_health_check_names_a_views_folder_it_cannot_read(
    lib: Library, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    with monkeypatch.context() as patch:
        deny(patch, FOLDERS[folder](lib))
        code, output, exc = run_lit(lib, ["health-check"])

    assert isinstance(exc, SystemExit), exc
    assert code == 1
    assert output.count(unreadable(folder)) == 1, output
    assert output.count(HINT) == 1
    # The other views are still checked; with views/ refused there are none.
    assert (ELSEWHERE in output) == (folder != "views/")


@pytest.mark.parametrize("folder", list(FOLDERS))
def test_health_check_fix_finishes_and_the_view_is_fixed_once_readable(
    lib: Library, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    with monkeypatch.context() as patch:
        deny(patch, FOLDERS[folder](lib))
        code, output, exc = run_lit(lib, ["health-check", "--fix"])

    assert isinstance(exc, SystemExit), exc
    assert code == 1
    # Said once, naming the first view it could not update.
    if folder == "views/":
        assert len(ANY_VIEWS_LINE.findall(output)) == 1, output
    else:
        assert output.count(views_line(view="by-project")) == 1, output
    assert "views_vs_metadata: cleaned" not in output

    run_lit(lib, ["health-check", "--fix"])
    assert view_issues(lib) == []
    assert is_portable_link(_inbox_p2(lib))


@pytest.mark.parametrize("folder", list(FOLDERS))
def test_the_health_panel_names_a_views_folder_it_cannot_read(
    lib: Library, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    client = TestClient(create_app(lib.vault), raise_server_exceptions=False)
    with monkeypatch.context() as patch:
        deny(patch, FOLDERS[folder](lib))
        resp = client.get("/api/health")

    assert resp.status_code == 200, resp.text
    found = sorted(
        (i["severity"], i["message"])
        for i in resp.json()
        if i["category"] == "views_vs_metadata"
    )
    expected = [("error", unreadable(folder))]
    if folder != "views/":
        expected.append(("error", ELSEWHERE))
    assert found == sorted(expected)
    assert {i["hint"] for i in resp.json() if i["message"] == unreadable(folder)} == {HINT}


@posix_only
@not_root
def test_a_view_chmod_made_unreadable(lib: Library) -> None:
    """The real thing, nothing injected."""
    view = lib.vault / "views" / "by-project"
    view.chmod(0o000)
    try:
        code, output, exc = run_lit(lib, ["health-check"])
    finally:
        view.chmod(0o755)

    assert isinstance(exc, SystemExit), exc
    assert code == 1
    assert output.count(unreadable("views/by-project/", "Permission denied")) == 1
    assert ELSEWHERE in output
