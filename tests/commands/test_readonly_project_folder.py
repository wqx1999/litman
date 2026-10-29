"""A project folder litman can read but not change.

``chmod 555``, a read-only mount, a folder another user shares: litman can see
everything in ``litman_reflib/`` and ``litman_code/`` but cannot add or remove
anything there (on a mount, not rewrite REFERENCES.md either). ``deny_writes``
(tests/_project_folder.py) does that on every platform; one test per area also
runs the real thing with ``chmod`` where that exists.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from litman.core.portable_link import (
    is_portable_link,
    remove_link_if_present,
    reset_link_probe_cache,
    reset_warning_state,
)
from tests._project_folder import (
    ACCESS_DENIED,
    HUBS,
    LINE,
    Library,
    deny_writes,
    make_library,
    not_root,
    paper_meta,
    posix_only,
    project_issues,
    registered_projects,
    run_lit,
)


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    return make_library(tmp_path)


@pytest.fixture(autouse=True)
def _drive_warning_can_fire() -> None:
    """The drive warning prints once per process: left used up by an earlier
    test, "not blamed on the drive" would pass for nothing."""
    reset_warning_state()


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
        # The mount: not even an unchanged REFERENCES.md may be rewritten.
        deny_writes(patch, lib.hub(hub), mount=True)
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


# ---------------------------------------------------------------------------
# Something to change: the command finishes and one line says so
# ---------------------------------------------------------------------------


def _drop_links(lib: Library) -> None:
    """A rebuild with something to do: a link gone from each hub."""
    remove_link_if_present(lib.reflib / "p2")
    remove_link_if_present(lib.code_dir / "repoS")


# argv, did the command's own write land, the hubs it has a link to add or
# remove in, and what to do to the library first. REFERENCES.md is rewritten
# as usual (chmod 555 lets a file already there be), so the link is the one
# thing that can fail.
NEEDS_A_CHANGE: dict[str, tuple[list[str], Any, list[str], Any]] = {
    "link": (
        ["link", "p3", "--project", "pp"],
        lambda lib: "pp" in (paper_meta(lib, "p3").get("projects") or []),
        ["litman_reflib"],
        None,
    ),
    "unlink": (
        ["unlink", "p2", "--project", "pp"],
        lambda lib: "pp" not in (paper_meta(lib, "p2").get("projects") or []),
        ["litman_reflib"],
        None,
    ),
    "rm": (["rm", "p1", "-y"], lambda lib: not paper_meta(lib, "p1"), HUBS, None),
    "rm --purge": (
        ["rm", "p1", "--purge", "-y"], lambda lib: not paper_meta(lib, "p1"), HUBS,
        None,
    ),
    "modify --add-tag projects": (
        ["modify", "p3", "--add-tag", "projects=pp"],
        lambda lib: "pp" in (paper_meta(lib, "p3").get("projects") or []),
        ["litman_reflib"],
        None,
    ),
    "rename": (
        ["rename", "p2", "p2b"],
        lambda lib: bool(paper_meta(lib, "p2b")) and not paper_meta(lib, "p2"),
        ["litman_reflib"],
        None,
    ),
    "trash restore": (
        ["trash", "restore", "p2", "-y"],
        lambda lib: bool(paper_meta(lib, "p2")),
        ["litman_reflib"],
        lambda lib: run_lit(lib, ["rm", "p2", "-y"]),
    ),
    "link --rebuild-all": (["link", "--rebuild-all"], None, HUBS, _drop_links),
    "refresh-views": (["refresh-views"], None, HUBS, _drop_links),
}


@pytest.mark.parametrize(
    ("name", "hub"),
    [(name, hub) for name, case in NEEDS_A_CHANGE.items() for hub in case[2]],
)
def test_a_change_a_read_only_folder_cannot_take_is_said_in_one_line(
    lib: Library, monkeypatch: pytest.MonkeyPatch, name: str, hub: str
) -> None:
    argv, landed, _, setup = NEEDS_A_CHANGE[name]
    if setup is not None:
        setup(lib)
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.hub(hub))
        code, output, exc = run_lit(lib, argv)

    assert code == 0, f"{output} {exc!r}"
    assert output.count(LINE) == 1, output
    assert "cannot hold folder links" not in output
    assert "NOT a link-support problem" not in output
    if landed is not None:
        assert landed(lib)
    assert lib.project_dir.joinpath("my_own_file.txt").is_file()

    # The way out it names.
    run_lit(lib, ["health-check", "--fix"])
    assert project_issues(lib) == []


@pytest.mark.parametrize("hub", HUBS)
def test_health_check_fix_does_not_claim_a_read_only_folder_it_could_not_change(
    lib: Library, monkeypatch: pytest.MonkeyPatch, hub: str
) -> None:
    _drop_links(lib)
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.hub(hub))
        code, output, exc = run_lit(lib, ["health-check", "--fix"])

    assert isinstance(exc, SystemExit), exc
    assert code == 1
    assert "project_references: cleaned" not in output
    assert output.count(LINE) == 1
    assert "cannot hold folder links" not in output

    run_lit(lib, ["health-check", "--fix"])
    assert project_issues(lib) == []


def test_code_unlink_says_it_could_not_update_litman_code(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.code_dir)
        code, output, exc = run_lit(
            lib, ["code", "unlink", "repoS", "--paper", "p1"]
        )

    assert code == 0, f"{output} {exc!r}"
    assert "repoS" not in (paper_meta(lib, "p1").get("code-clones") or [])
    assert output.count(
        f"warning: could not update a project's litman_code/ folder "
        f"({ACCESS_DENIED}). Fix that, then run `lit health-check --fix`."
    ) == 1

    run_lit(lib, ["health-check", "--fix"])
    assert project_issues(lib) == []


@pytest.mark.parametrize("hub", HUBS)
def test_project_rm_names_a_read_only_folder_it_could_not_clean_up(
    lib: Library, monkeypatch: pytest.MonkeyPatch, hub: str
) -> None:
    what = "its links and REFERENCES.md" if hub == "litman_reflib" else "its links"
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.hub(hub))
        code, output, exc = run_lit(lib, ["project", "rm", "pp", "-y"])

    assert code == 0, f"{output} {exc!r}"
    assert "pp" not in registered_projects(lib)
    assert output.count(
        f"warning: could not clean up {lib.hub(hub)} ({ACCESS_DENIED}). "
        f"Delete {what} by hand: lit no longer looks there."
    ) == 1


@posix_only
@not_root
@pytest.mark.parametrize(
    "argv", [["unlink", "p2", "--project", "pp"], ["rm", "p1", "-y"]], ids=" ".join
)
def test_a_folder_chmod_made_read_only_is_said_in_one_line(
    lib: Library, argv: list[str]
) -> None:
    """The real thing, nothing injected."""
    for hub in HUBS:
        lib.hub(hub).chmod(0o555)
    try:
        code, output, exc = run_lit(lib, argv)
    finally:
        for hub in HUBS:
            lib.hub(hub).chmod(0o755)

    assert code == 0, f"{output} {exc!r}"
    assert output.count(
        "warning: could not update a project folder (Permission denied). "
        "Fix that, then run `lit health-check --fix`."
    ) == 1
    run_lit(lib, ["health-check", "--fix"])
    assert project_issues(lib) == []


@posix_only
@not_root
def test_project_rm_names_a_folder_chmod_made_read_only(lib: Library) -> None:
    lib.code_dir.chmod(0o555)
    try:
        code, output, exc = run_lit(lib, ["project", "rm", "pp", "-y"])
    finally:
        lib.code_dir.chmod(0o755)

    assert code == 0, f"{output} {exc!r}"
    assert (
        f"warning: could not clean up {lib.code_dir} (Permission denied). "
        "Delete its links by hand: lit no longer looks there."
    ) in output


def test_a_views_bucket_that_cannot_be_changed_does_not_stop_a_write(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """views/ lives in the library itself. A bucket of it made read-only is
    skipped, as a drive without links always was; `lit health-check` reports
    the view out of date."""
    run_lit(lib, ["refresh-views"])
    bucket = lib.vault / "views" / "by-status" / "inbox"
    assert is_portable_link(bucket / "p2")
    with monkeypatch.context() as patch:
        deny_writes(patch, bucket)
        code, output, exc = run_lit(lib, ["modify", "p2", "--set", "status=skim"])

    assert code == 0, f"{output} {exc!r}"
    assert paper_meta(lib, "p2")["status"] == "skim"


# ---------------------------------------------------------------------------
# A folder that cannot be written is not a drive without links
# ---------------------------------------------------------------------------


def test_health_check_does_not_blame_the_drive_for_a_read_only_project_folder(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link probe writes in the project folder. One that takes nothing
    read as "this drive cannot hold links": the missing links went
    unreported and the hint sent the user to a different drive."""
    _drop_links(lib)
    reset_link_probe_cache()
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.project_dir)
        issues = project_issues(lib)
        code, output, exc = run_lit(lib, ["health-check"])

    assert (
        "project 'pp' membership implies a litman_reflib link for 'p2' "
        "but it is missing"
    ) in issues
    assert "links_unsupported" not in output
    assert "FAT32" not in output
    assert code == 1, f"{output} {exc!r}"

    reset_link_probe_cache()
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.project_dir)
        code, output, exc = run_lit(lib, ["health-check", "--fix"])
    assert output.count(LINE) == 1
    assert "project_references: cleaned" not in output


@pytest.mark.parametrize("content", ["a copy", "a copy with a file of the user's"])
def test_a_real_folder_in_a_read_only_hub_is_left_and_said_in_one_line(
    lib: Library, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """A copy is deleted, anything else moved to .trash/ — neither can happen
    in a hub that takes no change, and "close anything using that folder"
    pointed at the wrong cause."""
    spot = lib.reflib / "p2"
    remove_link_if_present(spot)
    shutil.copytree(lib.vault / "papers" / "p2", spot)
    if content != "a copy":
        spot.joinpath("mine.txt").write_text("mine", encoding="utf-8")
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.reflib)
        code, output, exc = run_lit(lib, ["link", "--rebuild-all"])

    assert code == 0, f"{output} {exc!r}"
    assert output.count(LINE) == 1
    assert "Close anything using that folder" not in output
    assert spot.is_dir() and not is_portable_link(spot)
    assert not (lib.vault / ".trash" / "replaced-folders").exists()


@posix_only
@not_root
def test_health_check_on_a_project_folder_chmod_made_read_only(
    lib: Library,
) -> None:
    """The real thing, nothing injected."""
    _drop_links(lib)
    reset_link_probe_cache()
    lib.project_dir.chmod(0o555)
    try:
        code, output, exc = run_lit(lib, ["health-check"])
    finally:
        lib.project_dir.chmod(0o755)

    assert code == 1, f"{output} {exc!r}"
    assert "litman_reflib link for 'p2' but it is missing" in output
    assert "FAT32" not in output
