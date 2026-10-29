"""A project folder litman can read but not change.

``chmod 555``, a read-only mount, a folder another user shares: litman can see
everything in ``litman_reflib/`` and ``litman_code/`` but cannot create, remove
or rewrite anything there. ``deny_writes`` (tests/_project_folder.py) does that
on every platform; one test per area also runs the real thing with ``chmod``
where that exists.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from litman.core.portable_link import is_portable_link
from tests._project_folder import (
    HUBS,
    Library,
    deny_writes,
    make_library,
    not_root,
    posix_only,
    project_issues,
    run_lit,
)


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    return make_library(tmp_path)


def _hub_state(hub: Path) -> dict[str, str]:
    """Each entry by name: where a link points, what a file says."""
    state = {}
    for child in sorted(hub.iterdir()):
        if child.is_symlink():
            state[child.name] = os.readlink(child)
        elif is_portable_link(child):
            state[child.name] = os.path.realpath(child)
        else:
            state[child.name] = child.read_text(encoding="utf-8")
    return state


# ---------------------------------------------------------------------------
# Nothing to change: the folder is left as it is and nothing is said
# ---------------------------------------------------------------------------

# argv, and the hubs it has nothing to change in
NOTHING_TO_CHANGE = {
    "link --rebuild-all": (["link", "--rebuild-all"], HUBS),
    "refresh-views": (["refresh-views"], HUBS),
    # Exits 1 all the same: the papers here have no paper.pdf.
    "health-check --fix": (["health-check", "--fix"], HUBS),
    "link a linked paper again": (["link", "p2", "--project", "pp"], HUBS),
    # p3 and p2 bind no repo: litman_code/ stays as it is.
    "modify --add-tag projects": (
        ["modify", "p3", "--add-tag", "projects=pp"], ["litman_code"]
    ),
    "rename": (["rename", "p2", "p2b"], ["litman_code"]),
    "project rename": (["project", "rename", "pp", "pp2"], ["litman_code"]),
}


@pytest.mark.parametrize(
    ("name", "hub"),
    [(name, hub) for name, (_, hubs) in NOTHING_TO_CHANGE.items() for hub in hubs],
)
def test_a_command_with_nothing_to_change_in_a_read_only_folder_leaves_it_be(
    lib: Library, monkeypatch: pytest.MonkeyPatch, name: str, hub: str
) -> None:
    argv, _ = NOTHING_TO_CHANGE[name]
    before = _hub_state(lib.hub(hub))
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.hub(hub))
        code, output, exc = run_lit(lib, argv)

    assert code == (1 if argv[0] == "health-check" else 0), f"{output} {exc!r}"
    assert exc is None or isinstance(exc, SystemExit), exc
    assert "warning:" not in output
    assert "project_references" not in output
    assert _hub_state(lib.hub(hub)) == before
    assert project_issues(lib) == []


@posix_only
@not_root
def test_a_rebuild_leaves_folders_chmod_made_read_only_be(lib: Library) -> None:
    """The real thing, nothing injected."""
    for hub in HUBS:
        lib.hub(hub).chmod(0o555)
    try:
        code, output, exc = run_lit(lib, ["link", "--rebuild-all"])
    finally:
        for hub in HUBS:
            lib.hub(hub).chmod(0o755)

    assert code == 0, f"{output} {exc!r}"
    assert "warning:" not in output
    assert project_issues(lib) == []
