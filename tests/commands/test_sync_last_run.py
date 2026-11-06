"""This machine's record of its last two-way run, and ``lit sync status`` showing it.

Every run ``two_way_sync`` starts, from a terminal or from the GUI, leaves a
record in the per-machine state file: when, who, how it ended, its conflicts
with their backups, the repos it left behind, or its whole error. ``lit sync
status`` prints it, even when the cloud cannot be listed. Real rclone against
a ``type = local`` remote, as in ``test_sync_bisync.py``.
"""

from __future__ import annotations

import io
import time
from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console

from litman.commands import sync as sync_cmd
from litman.core import locking
from litman.core import sync as core_sync
from litman.core.code_sync import RepoNotRestored, RepoSyncReport
from litman.core.sync import (
    RUN_ERROR_CAP,
    RUN_RECORD_CAP,
    SYNC_STATE_FILENAME,
    FileChanges,
    LastRun,
    RunConflict,
    SyncConflict,
    TwoWayResult,
    read_sync_state,
    record_run,
    run_from_error,
    run_from_result,
)
from litman.exceptions import SyncChoiceRequiredError, SyncError
from tests.commands.test_sync_bisync import (
    _edit_metadata,
    _machine,
    _sync,
    _two_synced_machines,
    _wide_consoles,
    _write_paper,
    cloud,
    needs_rclone,
    tmp_path,
)
from tests.commands.test_sync_stamp import _hold_lock

__all__ = ["_wide_consoles", "cloud", "tmp_path"]  # fixtures, used by name

# rclone's words when a remote's sign-in has expired.
_AUTH_LINES = (
    "ERROR : Bisync critical error: couldn't fetch token: invalid_grant: maybe "
    'token expired? - try refreshing with "rclone config reconnect fake-cloud:"',
)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _last_run(vault: Path) -> LastRun:
    run = read_sync_state(vault).last_run
    assert run is not None
    return run


# ---------------------------------------------------------------------------
# What a run leaves
# ---------------------------------------------------------------------------


@needs_rclone
def test_a_terminal_sync_records_that_it_went_through(tmp_path: Path, cloud: Path) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    # The reverse: no run, no record.
    assert read_sync_state(a).last_run is None

    assert _sync(a).exit_code == 0

    run = _last_run(a)
    assert (run.trigger, run.result, run.reason, run.error) == ("terminal", "ok", None, None)
    assert run.conflicts == () and run.repos == ()
    status = _sync(a, "status")
    assert status.exit_code == 0, status.output
    said = _one_line(status.output)
    assert "Last run:" in said
    assert "`lit sync` in a terminal: synced." in said
    assert "What to do" not in said


@needs_rclone
def test_a_conflict_is_recorded_with_the_backup_that_holds_the_other_copy(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    now = time.time()
    _edit_metadata(a, "p1", "edited-on-a", now - 200)
    _edit_metadata(b, "p1", "edited-on-b-longer", now - 100)
    a_copy = (a / "papers" / "p1" / "metadata.yaml").read_bytes()
    assert _sync(a).exit_code == 0

    assert _sync(b).exit_code == sync_cmd.EXIT_SYNC_CONFLICTS

    run = _last_run(b)
    assert (run.result, run.conflicts_total) == ("conflicts", 1)
    (conflict,) = run.conflicts
    assert (conflict.path, conflict.kept) == ("papers/p1/metadata.yaml", "local")
    assert Path(conflict.backup).read_bytes() == a_copy
    # The reverse: A's record is its own run's.
    assert _last_run(a).result == "ok"

    # A narrow console: the backup's path still prints whole, for an agent to use.
    monkeypatch.setattr(sync_cmd, "console", Console(width=60))
    status = _sync(b, "status")
    assert status.exit_code == 0, status.output
    assert conflict.backup in status.output
    said = _one_line(status.output)
    assert "synced, 1 conflict." in said
    assert "papers/p1/metadata.yaml: changed on both machines; this machine's copy kept" in said
    assert "What to do: Nothing is lost" in said


@needs_rclone
def test_a_stopped_run_keeps_its_whole_error_until_a_run_goes_through(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    for i in range(1, 11):
        _write_paper(a, f"p{i}")
    assert _sync(a).exit_code == 0
    for i in range(1, 8):
        locking.rmtree(a / "papers" / f"p{i}")

    stopped = _sync(a)

    assert stopped.exit_code == 1
    run = _last_run(a)
    assert (run.trigger, run.result, run.reason) == ("terminal", "failed", "safety-stop")
    assert run.error == str(stopped.exception)
    said = _one_line(_sync(a, "status").output)
    assert "failed (rclone's safety check stopped it)." in said
    assert "lit sync --force" in said
    # The error names the way out; status adds none.
    assert "What to do" not in said

    assert _sync(a, "--force").exit_code == 0
    assert _last_run(a).result == "ok"


@needs_rclone
def test_a_run_another_sync_held_off_leaves_the_record_alone(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    marker = LastRun(at="2026-01-01T00:00:00+00:00", trigger="auto", result="failed", error="x")
    record_run(a, marker)
    monkeypatch.setattr(sync_cmd, "SYNC_LOCK_WAIT", 0.3)

    holder = _hold_lock(a, cloud, 3)
    try:
        held = _sync(a)
    finally:
        holder.join()

    assert held.exit_code == sync_cmd.EXIT_SYNC_BUSY, held.output
    assert read_sync_state(a).last_run == marker
    # The reverse: a run that starts replaces it.
    assert _sync(a).exit_code == 0
    assert _last_run(a).result == "ok"


@needs_rclone
def test_an_expired_sign_in_shows_in_status_with_the_way_out_when_the_cloud_cannot_be_listed(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    real_bisync = core_sync.run_bisync
    monkeypatch.setattr(
        core_sync,
        "run_bisync",
        lambda args, on_log=None, **kwargs: core_sync.BisyncRun(1, _AUTH_LINES, ()),
    )

    assert _sync(a).exit_code == 1

    run = _last_run(a)
    assert (run.result, run.reason) == ("failed", "auth")
    assert "invalid_grant" in (run.error or "")
    monkeypatch.setattr(core_sync, "run_bisync", real_bisync)

    def expired(target_url: str) -> frozenset[str]:
        raise SyncError("rclone could not list the cloud folder: couldn't fetch token")

    real_listing = core_sync.list_cloud_root
    monkeypatch.setattr(core_sync, "list_cloud_root", expired)
    status = _sync(a, "status")

    assert status.exit_code == 1
    assert isinstance(status.exception, SyncError)
    said = _one_line(status.output)
    assert "`lit sync` in a terminal: failed (the sign-in to the remote expired)." in said
    assert "invalid_grant" in said
    assert (
        "What to do: Sign in again with `rclone config reconnect fake-cloud:`, "
        "then run `lit sync`." in said
    )
    # The reverse: with the cloud listed, the table comes first, then the record.
    monkeypatch.setattr(core_sync, "list_cloud_root", real_listing)
    status = _sync(a, "status")
    assert status.exit_code == 0, status.output
    said = _one_line(status.output)
    assert said.index("Delta") < said.index("Last run:")


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def _conflict(i: int) -> SyncConflict:
    return SyncConflict(
        path=f"papers/p{i}/notes.md",
        paper_id=f"p{i}",
        kept="cloud",
        kept_mtime=None,
        moved_mtime=0.0,
        backup=Path("backup") / f"p{i}" / "notes.md",
    )


def _result(n_conflicts: int) -> TwoWayResult:
    return TwoWayResult(
        target_url="fake-cloud:lib",
        mode="sync",
        chosen=False,
        came_down=FileChanges(),
        went_up=None,
        conflicts=tuple(_conflict(i) for i in range(n_conflicts)),
        remote_changed=True,
        local_backups=(),
        projects_without_folder=(),
    )


@pytest.mark.parametrize("n", [RUN_RECORD_CAP, RUN_RECORD_CAP + 5])
def test_the_record_lists_a_bounded_number_and_counts_them_all(tmp_path: Path, n: int) -> None:
    record_run(tmp_path, run_from_result(_result(n), "auto"))

    run = _last_run(tmp_path)
    assert (run.trigger, run.result, run.conflicts_total) == ("auto", "conflicts", n)
    assert len(run.conflicts) == min(n, RUN_RECORD_CAP)
    assert run.conflicts[0] == RunConflict(
        "papers/p0/notes.md", "cloud", str(Path("backup") / "p0" / "notes.md")
    )


def test_status_prints_the_record_unwrapped_and_says_what_it_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = (
        "/home/someone/.local/share/litman/sync-backup/lib-3f2a9c1e7b04/conflicts/"
        "2026-11-06_141502/papers/p0/notes.md.sync-conflict2"
    )
    run = LastRun(
        at="2026-11-06T11:30:00+01:00",
        trigger="auto",
        result="conflicts",
        conflicts=(RunConflict("papers/p0/notes.md", "local", backup),),
        conflicts_total=6,
    )
    out = io.StringIO()
    monkeypatch.setattr(sync_cmd, "console", Console(file=out, width=40))

    sync_cmd._print_last_run(run, "gdrive:lib")

    printed = out.getvalue()
    assert backup in printed
    assert "… and 5 more" in printed
    assert "the GUI's auto sync: synced, 6 conflicts." in _one_line(printed)
    # The reverse: printed the usual way, the same line breaks the path.
    wrapped = io.StringIO()
    Console(file=wrapped, width=40).print(f"  • papers/p0/notes.md: … {backup}")
    assert backup not in wrapped.getvalue()


def test_a_malformed_record_reads_as_none_and_the_rest_of_the_state_still_reads(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / SYNC_STATE_FILENAME
    state_file.write_text(
        "last-sync: '2026-11-06T10:00:00+01:00'\nlast-run: [not, a, mapping]\n",
        encoding="utf-8",
    )

    state = read_sync_state(tmp_path)

    assert state.last_run is None
    assert state.last_sync == "2026-11-06T10:00:00+01:00"
    # The reverse: a well-formed record reads; a total below its rows is raised to them.
    state_file.write_text(
        "last-run:\n  at: '2026-11-06T10:00:00+01:00'\n  trigger: auto\n"
        "  result: conflicts\n  conflicts-total: 0\n"
        "  conflicts:\n  - {path: a.md, kept: local, backup: b.md}\n",
        encoding="utf-8",
    )
    run = _last_run(tmp_path)
    assert (run.conflicts_total, run.conflicts) == (1, (RunConflict("a.md", "local", "b.md"),))


def test_the_record_names_the_reason_where_it_is_known() -> None:
    assert run_from_error(SyncError("ERROR : invalid_grant"), "auto").reason == "auth"
    assert run_from_error(SyncError("dial tcp: no such host"), "auto").reason is None
    choice = run_from_error(SyncChoiceRequiredError("Pick a side."), "terminal")
    assert (choice.result, choice.reason) == ("needs-choice", "choice")
    assert run_from_error(SyncError("too old"), "auto", reason="rclone").reason == "rclone"
    assert len(run_from_error(SyncError("x" * (RUN_ERROR_CAP + 1)), "auto").error or "") == (
        RUN_ERROR_CAP
    )


def test_each_repo_row_tells_the_page_its_kind_and_keeps_its_words_for_status(
    tmp_path: Path,
) -> None:
    """The page words a repo row by its kind; the server's own sentences stay
    in the payload and on disk, for ``lit sync status``."""
    from litman.server import sync_scheduler as sched_mod

    not_restored = RepoNotRestored(
        "tools", "its checkout is at another commit", "Commit or drop your changes there."
    )
    result = replace(
        _result(0),
        repos=RepoSyncReport(not_restored=(not_restored,), left_behind=("old",)),
        repos_error="git could not run: x",
    )

    notice = sched_mod._notice(result)

    assert notice is not None
    rows = notice["reposNotRestored"]
    assert [(r["name"], r["kind"]) for r in rows] == [
        ("tools", "not-restored"),
        ("old", "left-behind"),
        ("", "failed"),
    ]
    assert (rows[0]["what"], rows[0]["wayOut"]) == (not_restored.what, not_restored.way_out)
    assert rows[1]["what"].startswith("codes/old/repo/ stays on this machine")
    assert rows[2]["what"] == "git could not run: x"
    record_run(tmp_path, run_from_result(result, "auto"))
    run = _last_run(tmp_path)
    assert [(r.name, r.kind) for r in run.repos] == [
        ("tools", "not-restored"),
        ("old", "left-behind"),
        ("", "failed"),
    ]
    assert run.repos[0].way_out == not_restored.way_out
    # The reverse: a sync that left no repo behind has nothing to tell.
    assert sched_mod._notice(_result(0)) is None
