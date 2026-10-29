"""A views/ folder litman can read but not change.

views/ lives in the library: a library that takes no change at all is refused
before any command starts, but one folder of it made read-only by hand is not.
A write that needs a change there finishes, says so in one line instead of
"views/ refreshed", and leaves the view for `lit health-check` to report; a
rebuild with nothing to change there leaves it be. ``deny_writes``
(tests/_project_folder.py) does that on every platform; the last test runs the
real thing with ``chmod``.
"""

from __future__ import annotations

import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.checks import check_views_vs_metadata
from litman.core.document import list_papers
from litman.core.portable_link import (
    is_portable_link,
    link_points_at,
    make_portable_link,
    remove_link_if_present,
    reset_warning_state,
)
from tests._project_folder import (
    ACCESS_DENIED,
    Library,
    deny_writes,
    make_library,
    not_root,
    paper_meta,
    posix_only,
    run_lit,
)


def views_line(reason: str = ACCESS_DENIED, view: str = "by-status") -> str:
    """The one line, naming the first view it could not change."""
    return (
        f"warning: could not update views/{view}/ ({reason}). "
        "Fix that, then run `lit health-check --fix`."
    )


# Any view: with views/ itself refused, the first to need a change is named.
ANY_VIEWS_LINE = re.compile(re.escape(views_line(view="@")).replace("@", "by-[a-z]+"))


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    """make_library's p1–p3, all `inbox`, with views/ built."""
    lib = make_library(tmp_path)
    code, output, exc = run_lit(lib, ["refresh-views"])
    assert code == 0, f"{output} {exc!r}"
    assert is_portable_link(_inbox(lib) / "p2")
    return lib


@pytest.fixture(autouse=True)
def _drive_warning_can_fire() -> None:
    """The drive warning prints once per process: left used up by an earlier
    test, "not blamed on the drive" would pass for nothing."""
    reset_warning_state()


def _inbox(lib: Library) -> Path:
    return lib.vault / "views" / "by-status" / "inbox"


def view_issues(lib: Library) -> list[str]:
    return [
        i.message
        for i in check_views_vs_metadata(lib.vault, list_papers(lib.vault))
    ]


def _view_not_there_yet(lib: Library) -> Path:
    shutil.rmtree(lib.vault / "views" / "by-status")
    return lib.vault / "views"


# Each returns the folder to refuse changes in (deny_writes takes in everything
# under it, as chmod -R would).
FAULTS: dict[str, Callable[[Library], Path]] = {
    "the view": lambda lib: lib.vault / "views" / "by-status",
    "a bucket": _inbox,
    "a view not there yet": _view_not_there_yet,
}


def _rm_p2(lib: Library) -> None:
    code, output, exc = run_lit(lib, ["rm", "p2", "-y"])
    assert code == 0, f"{output} {exc!r}"


def _drop_p2_link(lib: Library) -> None:
    assert remove_link_if_present(_inbox(lib) / "p2")


# setup (before the fault), argv, and how to tell the command's own write landed.
NEEDS_A_CHANGE: dict[str, tuple[
    Callable[[Library], None] | None,
    list[str],
    Callable[[Library], bool] | None,
]] = {
    "drop": (
        None, ["drop", "p2"],
        lambda lib: paper_meta(lib, "p2")["status"] == "dropped",
    ),
    "rename": (
        None, ["rename", "p2", "p2b"],
        lambda lib: paper_meta(lib, "p2b").get("id") == "p2b",
    ),
    "rm": (None, ["rm", "p2", "-y"], lambda lib: paper_meta(lib, "p2") == {}),
    "trash restore": (
        _rm_p2, ["trash", "restore", "p2", "-y"],
        lambda lib: paper_meta(lib, "p2").get("id") == "p2",
    ),
    "refresh-views": (_drop_p2_link, ["refresh-views"], None),
    "health-check --fix": (_drop_p2_link, ["health-check", "--fix"], None),
}

# A view that is not there holds no link for rm to take out.
CASES = [
    pytest.param(fault, name, id=f"{name}-{fault}")
    for name in NEEDS_A_CHANGE
    for fault in FAULTS
    if not (name == "rm" and fault == "a view not there yet")
]


@pytest.mark.parametrize(("fault", "name"), CASES)
def test_a_change_a_read_only_view_cannot_take_is_said_in_one_line(
    lib: Library, monkeypatch: pytest.MonkeyPatch, fault: str, name: str
) -> None:
    setup, argv, landed = NEEDS_A_CHANGE[name]
    if setup is not None:
        setup(lib)
    folder = FAULTS[fault](lib)
    with monkeypatch.context() as patch:
        deny_writes(patch, folder)
        code, output, exc = run_lit(lib, argv)

    if name == "health-check --fix":
        # The fixture's papers have no PDF: the report itself exits 1.
        assert isinstance(exc, SystemExit), exc
        assert "views_vs_metadata: cleaned" not in output
    else:
        assert code == 0, f"{output} {exc!r}"
    assert len(ANY_VIEWS_LINE.findall(output)) == 1, output
    assert output.count("could not update views") == 1, output
    if fault != "a view not there yet":
        assert views_line() in output
    assert "views/ refreshed" not in output
    assert "FAT32" not in output
    if landed is not None:
        assert landed(lib)
    assert view_issues(lib)  # left for `lit health-check` to report


def test_health_check_fix_claims_a_view_it_did_change(lib: Library) -> None:
    """The control for the claim above: the same drift, nothing refused."""
    _drop_p2_link(lib)
    _code, output, exc = run_lit(lib, ["health-check", "--fix"])

    assert isinstance(exc, SystemExit), exc
    assert "views_vs_metadata: cleaned" in output
    assert "could not update views" not in output
    assert view_issues(lib) == []


@pytest.mark.parametrize(
    "argv", [["drop", "p2"], ["rename", "p2", "p2b"], ["refresh-views"]],
    ids=["drop", "rename", "refresh-views"],
)
def test_the_view_is_fixed_once_the_folder_takes_changes_again(
    lib: Library, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    if argv == ["refresh-views"]:
        _drop_p2_link(lib)
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.vault / "views" / "by-status")
        run_lit(lib, argv)
    assert view_issues(lib)

    _code, output, _exc = run_lit(lib, ["health-check", "--fix"])
    assert "could not update views" not in output
    assert view_issues(lib) == []


@pytest.mark.parametrize("fault", ["the view", "a bucket"])
@pytest.mark.parametrize(
    "argv",
    [["refresh-views"], ["link", "p3", "--project", "pp"],
     ["modify", "p2", "--set", "title=New title"]],
    ids=["refresh-views", "link", "modify --set title"],
)
def test_a_command_with_nothing_to_change_in_a_read_only_view_leaves_it_be(
    lib: Library, monkeypatch: pytest.MonkeyPatch, fault: str, argv: list[str]
) -> None:
    """A rebuild keeps the links that already point at their paper, so a
    folder it has nothing to change in is neither touched nor reported."""
    folder = FAULTS[fault](lib)
    before = sorted(p.name for p in _inbox(lib).iterdir())
    with monkeypatch.context() as patch:
        deny_writes(patch, folder)
        code, output, exc = run_lit(lib, argv)

    assert code == 0, f"{output} {exc!r}"
    assert "could not update views" not in output
    assert sorted(p.name for p in _inbox(lib).iterdir()) == before
    assert view_issues(lib) == []


def test_the_line_goes_to_stderr(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stdout stays what the command prints there: a script that reads it
    sees no warning in it."""
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.vault / "views" / "by-status")
        result = CliRunner().invoke(
            cli, ["drop", "p2", "--library", str(lib.vault)]
        )

    assert result.exit_code == 0, result.output
    assert views_line() in " ".join(result.stderr.split())
    assert "could not update views" not in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["taxonomy", "rename", "topics", "alpha", "beta"],
        ["taxonomy", "merge", "topics", "alpha", "--into", "beta", "-y"],
    ],
    ids=["rename", "merge"],
)
def test_papers_moved_at_once_say_it_once(
    lib: Library, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """One line however many papers a command moves: `taxonomy rename`
    updates each tagged paper's buckets in turn, `taxonomy merge` rebuilds
    the views once."""
    for value in ["alpha", "beta"] if "merge" in argv else ["alpha"]:
        code, output, exc = run_lit(lib, ["taxonomy", "add", "topics", value])
        assert code == 0, f"{output} {exc!r}"
    for paper_id in ("p1", "p2", "p3"):
        code, output, exc = run_lit(
            lib, ["modify", paper_id, "--add-tag", "topics=alpha"]
        )
        assert code == 0, f"{output} {exc!r}"
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.vault / "views" / "by-topic")
        code, output, exc = run_lit(lib, argv)

    assert code == 0, f"{output} {exc!r}"
    assert all(
        paper_meta(lib, p)["topics"] == ["beta"] for p in ("p1", "p2", "p3")
    )
    assert output.count("could not update views/by-topic/") == 1, output


@posix_only
@not_root
def test_a_view_chmod_made_read_only(lib: Library) -> None:
    """The real thing, nothing injected: only the view folder itself, so the
    paper's old bucket still lets go of it and the new one cannot be made."""
    view = lib.vault / "views" / "by-status"
    view.chmod(0o555)
    try:
        code, output, exc = run_lit(lib, ["drop", "p2"])
        assert view_issues(lib)
    finally:
        view.chmod(0o755)

    assert code == 0, f"{output} {exc!r}"
    assert paper_meta(lib, "p2")["status"] == "dropped"
    assert output.count(views_line("Permission denied")) == 1, output
    assert "INDEX.json refreshed." in output


@posix_only
@not_root
def test_a_stale_bucket_in_a_view_chmod_made_read_only_is_emptied(
    lib: Library,
) -> None:
    """A bucket no paper is in any more gives up its links but not its own
    entry: nothing stale is left, so nothing is said."""
    view = lib.vault / "views" / "by-status"
    stale = view / "skim"
    stale.mkdir()
    (stale / "p2").symlink_to(Path("..", "..", "..", "papers", "p2"))
    assert view_issues(lib)
    view.chmod(0o555)
    try:
        code, output, exc = run_lit(lib, ["refresh-views"])
    finally:
        view.chmod(0o755)

    assert code == 0, f"{output} {exc!r}"
    assert "could not update views" not in output
    assert stale.is_dir() and not any(stale.iterdir())
    assert view_issues(lib) == []


def _edit_meta(lib: Library, paper_id: str, edit: Callable[[str], str]) -> None:
    path = lib.vault / "papers" / paper_id / "metadata.yaml"
    path.chmod(path.stat().st_mode | stat.S_IWRITE)  # the TRUTH lock
    path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")


def test_health_check_fix_says_it_once_however_many_fixes_rebuild(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regen rebuilds views/, then the dangling-relation fix rebuilds them
    again: one run, one line."""
    _edit_meta(
        lib, "p2",
        lambda t: t.replace("status: inbox", "status: skim") + "related: [ghost]\n",
    )
    _edit_meta(lib, "p1", lambda t: t + "extends: [ghost2]\n")
    with monkeypatch.context() as patch:
        deny_writes(patch, _inbox(lib))
        _code, output, exc = run_lit(lib, ["health-check", "--fix"])

    assert isinstance(exc, SystemExit), exc
    assert not paper_meta(lib, "p2").get("related")  # the second rebuild ran
    assert not paper_meta(lib, "p1").get("extends")
    assert output.count("could not update views") == 1, output
    assert views_line() in output


@pytest.mark.parametrize(
    "wrong",
    ["another paper", pytest.param("an absolute path", marks=posix_only)],
)
def test_a_link_to_the_wrong_place_in_a_bucket_is_made_again(
    lib: Library, wrong: str
) -> None:
    """Kept only when it points at its own paper the way litman makes it: a
    library moved on Windows leaves junctions to where it was before."""
    link = _inbox(lib) / "p1"
    assert remove_link_if_present(link)
    if wrong == "another paper":
        assert make_portable_link(link, lib.vault / "papers" / "p3")
    else:
        link.symlink_to(lib.vault / "papers" / "p1")
    code, output, exc = run_lit(lib, ["refresh-views"])

    assert code == 0, f"{output} {exc!r}"
    assert link_points_at(link, lib.vault / "papers" / "p1")
    assert view_issues(lib) == []


def test_a_stale_bucket_that_takes_no_change_is_said(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bucket no paper is in any more, holding a link it will not give up:
    something stale is left, so it is said, and `lit health-check` sees it."""
    stale = lib.vault / "views" / "by-status" / "skim"
    stale.mkdir()
    assert make_portable_link(stale / "p2", lib.vault / "papers" / "p2")
    with monkeypatch.context() as patch:
        deny_writes(patch, stale)
        code, output, exc = run_lit(lib, ["refresh-views"])

    assert code == 0, f"{output} {exc!r}"
    assert output.count(views_line()) == 1, output
    assert is_portable_link(stale / "p2")
    assert view_issues(lib)


def test_a_real_folder_where_a_link_belongs_is_cleared_without_a_word(
    lib: Library,
) -> None:
    spot = _inbox(lib) / "p2"
    assert remove_link_if_present(spot)
    spot.mkdir()
    (spot / "x.txt").write_text("x", encoding="utf-8")
    code, output, exc = run_lit(lib, ["refresh-views"])

    assert code == 0, f"{output} {exc!r}"
    assert "could not update views" not in output
    assert link_points_at(spot, lib.vault / "papers" / "p2")
    assert view_issues(lib) == []
