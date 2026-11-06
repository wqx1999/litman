"""Reading which side a bisync run changed, from its operation lines.

The reports in ``bisync_reports/`` were recorded from rclone 1.75.2 runs of
litman's own bisync command line, one per kind of run, with each run's root
folders written ``<P>``. ``cloud_changed`` is what the run did to the cloud in
fact. The rule may report a change that did not happen (it costs one sync that
finds nothing to do), never miss one: also when rclone words its lines
differently, or stops printing its counts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from litman.core.sync import (
    BisyncOps,
    BisyncRun,
    classify_sync_error,
    parse_bisync_ops,
)
from litman.exceptions import (
    BisyncLockedError,
    SyncAuthError,
    SyncBusyError,
    SyncChoiceRequiredError,
    SyncError,
    SyncStoppedError,
)

_REPORTS = sorted((Path(__file__).parent / "bisync_reports").glob("*.json"))
_COUNT_RE = re.compile(r"^INFO\s*: (Path1|Path2): +(\d+) changes:")


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _record(stem: str) -> dict[str, Any]:
    return _load(next(p for p in _REPORTS if p.stem == stem))


def _ops(record: dict[str, Any], edit: Any = None, *, windows: bool = False) -> BisyncOps:
    """Read ``record`` (recorded on Linux) after ``edit``; ``windows``: by rclone's rule there."""
    report, lines = record["report"], record["lines"]
    if edit is not None:
        report, lines = edit(report), edit(lines)
    run = BisyncRun(record["returncode"], tuple(lines), tuple(report))
    return parse_bisync_ops(run, windows=windows)


def _counted(record: dict[str, Any], side: str) -> int:
    for line in record["report"]:
        match = _COUNT_RE.match(line)
        if match is not None and match.group(1) == side:
            return int(match.group(2))
    return 0


def _reworded(lines: list[str]) -> list[str]:
    """As if a later rclone named its operations differently."""
    return [
        line.replace("Queue copy to", "Queueing copy to")
        .replace("Queue delete", "Queueing delete")
        .replace("Renaming Path", "Rename Path")
        for line in lines
    ]


def _without_counts(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if not _COUNT_RE.match(line) and "No changes found" not in line
    ]


def test_every_kind_of_run_was_recorded() -> None:
    names = [p.stem for p in _REPORTS]
    assert len(names) == 11
    assert all(_load(p)["rclone"] == "1.75.2" for p in _REPORTS)


@pytest.mark.parametrize("path", _REPORTS, ids=lambda p: p.stem)
def test_a_cloud_change_is_never_missed(path: Path) -> None:
    record = _load(path)
    ops = _ops(record)
    if record["cloud_changed"]:
        assert ops.remote_changed
    # Reported a change only where one happened, but for one case: both
    # machines made the same change, so bisync found both sides equal and
    # did nothing, and its count still said a file changed here.
    expected = record["cloud_changed"] or record["scenario"] == "10-after-conflict-cleanup"
    assert ops.remote_changed == expected


def test_the_files_a_run_wrote_here_are_read_relative_to_the_library() -> None:
    by_name = {p.stem: _ops(_load(p)) for p in _REPORTS}
    assert by_name["02-no-op"].path1_writes == frozenset()
    assert by_name["03-local-new"].path1_writes == frozenset()
    assert by_name["06-cloud-new-pull-only"].path1_writes == {"papers/d", "papers/d/notes.md"}
    assert by_name["07-cloud-changed-pull-only"].path1_writes == {"papers/b/notes.md"}
    assert by_name["08-cloud-deleted-pull-only"].path1_writes == {"papers/b/notes.md"}
    assert "papers/a/notes.md" in (by_name["09-conflict-cloud-wins"].path1_writes or ())
    # A full comparison writes here too, file by file unnamed.
    assert by_name["01-first-sync-resync"].path1_writes is None


@pytest.mark.parametrize("path", _REPORTS, ids=lambda p: p.stem)
def test_reworded_operation_lines_over_report_instead_of_going_unnoticed(path: Path) -> None:
    record = _load(path)
    ops = _ops(record, _reworded)
    if _counted(record, "Path1") > 0 or record["scenario"].startswith("01-"):
        assert ops.remote_changed
    if _counted(record, "Path2") > 0:
        assert ops.path1_writes is None
    if record["cloud_changed"]:
        assert ops.remote_changed


@pytest.mark.parametrize("path", _REPORTS, ids=lambda p: p.stem)
def test_a_report_without_counts_reads_as_changed_both_ways(path: Path) -> None:
    ops = _ops(_load(path), _without_counts)
    assert ops.remote_changed
    assert ops.path1_writes is None


def test_a_file_outside_the_library_folder_leaves_the_writes_unknown() -> None:
    record = _record("07-cloud-changed-pull-only")

    def elsewhere(lines: list[str]) -> list[str]:
        return [line.replace("- <P>/vault/papers", "- <P>/other/papers") for line in lines]

    assert _ops(record, elsewhere).path1_writes is None


#: The library folder as rclone on Windows prints it (``bilib.FsPath``: the
#: ``\\?\`` dropped, backslashes, one at the end), and whether bisync escapes
#: it. bisync's ``escapePath`` reads a backslash there as a slash: it only puts
#: quotes around the folder in "Synching Path1" and prints it as it is in front
#: of an operation's path, unless it holds a character to escape; then it
#: writes the whole path as Go's ``strconv.Quote`` does, every backslash doubled.
_WINDOWS_ROOTS = [
    pytest.param(
        "C:\\Users\\runneradmin\\AppData\\Local\\Temp\\lt-im822zks\\vault\\", False, id="ci-temp"
    ),
    pytest.param("D:\\文献 库\\vault\\", False, id="space-and-cjk"),
    pytest.param("C:\\Users\\a\u00a0b\\vault\\", True, id="escaped-no-break-space"),
]
#: The recorded runs that wrote here.
_WROTE_HERE = [
    "06-cloud-new-pull-only",
    "07-cloud-changed-pull-only",
    "08-cloud-deleted-pull-only",
    "09-conflict-cloud-wins",
    "11-conflict-local-wins",
]


def _on_windows(root: str, escaped: bool) -> Callable[[list[str]], list[str]]:
    """The recorded lines with the library folder printed as rclone on Windows prints ``root``."""

    def printed(path: str, *, quotes: bool) -> str:
        if escaped:
            # As strconv.Quote writes it: the path is ASCII but for the one
            # character escaped, which json.dumps escapes alike.
            return json.dumps(path)
        return f'"{path}"' if quotes else path

    def edit(lines: list[str]) -> list[str]:
        out = []
        for line in lines:
            line = line.replace('"<P>/vault/"', printed(root, quotes=True))
            head, marker, rel = line.partition("- <P>/vault/")
            out.append(f"{head}- {printed(root + rel, quotes=False)}" if marker else line)
        return out

    return edit


@pytest.mark.parametrize(("root", "escaped"), _WINDOWS_ROOTS)
@pytest.mark.parametrize("stem", _WROTE_HERE)
def test_a_run_on_windows_names_the_files_it_wrote_here_as_elsewhere(
    stem: str, root: str, escaped: bool
) -> None:
    record = _record(stem)
    on_windows = _ops(record, _on_windows(root, escaped), windows=True)
    assert on_windows.path1_writes
    assert on_windows == _ops(record)


@pytest.mark.parametrize(
    ("root", "escaped"), [p for p in _WINDOWS_ROOTS if p.values[1] is False]
)
def test_the_folder_from_windows_read_by_the_rule_for_elsewhere_leaves_the_writes_unknown(
    root: str, escaped: bool
) -> None:
    """The reverse: read as if bisync had escaped it, the folder cannot be read."""
    record = _record("07-cloud-changed-pull-only")
    ops = _ops(record, _on_windows(root, escaped), windows=False)
    assert ops.path1_writes is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SyncBusyError("Another sync of this library is running."), ("busy", "busy")),
        (BisyncLockedError("The last sync left its lock behind."), ("busy", "busy")),
        (SyncChoiceRequiredError("Pick the winner."), ("needs-user", "choice")),
        (SyncStoppedError("rclone stopped: more than half ..."), ("needs-user", "safety-stop")),
        # rclone's words for a sign-in that expired, from three providers.
        (
            SyncError(
                "rclone could not finish the sync (exit 1).\nERROR : couldn't fetch token: "
                'invalid_grant: maybe token expired? - try refreshing with "rclone config '
                'reconnect gdrive:"'
            ),
            ("needs-user", "auth"),
        ),
        (SyncError("ERROR : expired_access_token/"), ("needs-user", "auth")),
        (
            SyncError("AADSTS700082: The refresh token has expired due to inactivity."),
            ("needs-user", "auth"),
        ),
        # A WebDAV server's answer to a wrong or revoked password, as rclone 1.75
        # passes it on (measured against `rclone serve webdav`).
        (
            SyncError(
                "rclone could not finish the sync (exit 1).\nCRITICAL: Failed to create file "
                'system for destination "wd:lib": read metadata failed: 401 Unauthorized: '
                "401 Unauthorized"
            ),
            ("needs-user", "auth"),
        ),
        (
            SyncError(
                "rclone could not read the sync stamp in wd:lib (exit 1):\nCRITICAL: Failed "
                'to create file system for "wd:lib/.litman-sync-stamp.json": read metadata '
                "failed: 401 Unauthorized: 401 Unauthorized"
            ),
            ("needs-user", "auth"),
        ),
        (SyncAuthError("wd refused the password, so nothing was synced."), ("needs-user", "auth")),
        (
            SyncError("rclone could not read the sync stamp (exit 1):\ndial tcp: i/o timeout"),
            ("offline", "offline"),
        ),
        (SyncError("Failed to bisync: connection refused"), ("offline", "offline")),
        # Not a sign-in: a file whose name holds 401, and a server that forbids.
        (SyncError("ERROR : papers/2401_Smith_x/notes.md: md5 differ"), ("offline", "offline")),
        (SyncError("ERROR : couldn't list files: 403 Forbidden"), ("offline", "offline")),
    ],
)
def test_failed_syncs_are_told_apart_for_the_auto_sync(
    error: SyncError, expected: tuple[str, str]
) -> None:
    assert classify_sync_error(error) == expected
