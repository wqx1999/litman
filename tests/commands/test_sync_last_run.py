"""This machine's record of its last two-way run, and ``lit sync status`` showing it.

Every run ``two_way_sync`` starts, from a terminal or from the GUI, leaves a
record in the per-machine state file: when, who, how it ended, its conflicts
with their backups, the repos it left behind, or its whole error. ``lit sync
status`` prints it, even when the cloud cannot be listed, and then the folders
of conflict backups made here in the last days, which outlast the record.
While the record says a run did not go through, every other command says so.
Real rclone against a ``type = local`` remote, as in ``test_sync_bisync.py``.
"""

from __future__ import annotations

import io
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from litman.cli import cli
from litman.commands import sync as sync_cmd
from litman.core import locking
from litman.core import sync as core_sync
from litman.core.code_sync import RepoNotRestored, RepoSyncReport
from litman.core.library import create_vault
from litman.core.sync import (
    CONFLICT_BACKUP_FILES_CAP,
    RUN_ERROR_CAP,
    RUN_RECORD_CAP,
    SAVE_BACKUP_LABEL,
    SYNC_STATE_FILENAME,
    FileChanges,
    LastRun,
    RunConflict,
    SyncConflict,
    TwoWayResult,
    bisync_paths,
    conflict_backups_root,
    keep_replaced_copy,
    read_sync_state,
    recent_conflict_backups,
    record_run,
    run_from_error,
    run_from_result,
)
from litman.core.vault_registry import add_vault, load_registry, save_registry
from litman.exceptions import SyncChoiceRequiredError, SyncError
from tests.commands.test_sync_bisync import (
    _edit_metadata,
    _machine,
    _sync,
    _target,
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


# ---------------------------------------------------------------------------
# A run that did not go through, named after the other commands
# ---------------------------------------------------------------------------


@needs_rclone
def test_a_stopped_run_is_named_after_other_commands_until_a_run_goes_through(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    save_registry(add_vault(load_registry(), "a", a))
    real_bisync = core_sync.run_bisync
    monkeypatch.setattr(
        core_sync,
        "run_bisync",
        lambda args, on_log=None, **kwargs: core_sync.BisyncRun(1, _AUTH_LINES, ()),
    )
    assert _sync(a).exit_code == 1
    at = datetime.fromisoformat(_last_run(a).at)

    listed = CliRunner().invoke(cli, ["list"])

    assert listed.exit_code == 0, listed.output
    assert (
        f"Cloud sync stopped at {at:%H:%M} (sign-in expired). "
        "Run `lit sync status` for why and what to do." in listed.stderr.splitlines()
    )
    # The reverse: a run that goes through takes the line away.
    monkeypatch.setattr(core_sync, "run_bisync", real_bisync)
    assert _sync(a).exit_code == 0
    assert "Cloud sync" not in CliRunner().invoke(cli, ["list"]).output


# ---------------------------------------------------------------------------
# The conflict backups of the last days
# ---------------------------------------------------------------------------

_BACKUP_TARGET = "gdrive:lib"


def _backup_folder(root: Path, at: datetime, *files: str, label: str = "") -> Path:
    """A folder of conflict backups made at ``at``, holding ``files``."""
    folder = root / (at.strftime("%Y-%m-%d_%H%M%S") + label)
    folder.mkdir(parents=True)
    for rel in files:
        path = folder / Path(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel, encoding="utf-8")
    return folder


def _printed_backups(
    monkeypatch: pytest.MonkeyPatch, vault: Path, run: LastRun | None
) -> list[str]:
    """``lit sync status``'s lines on the conflict backups, on a 40-column console."""
    out = io.StringIO()
    monkeypatch.setattr(sync_cmd, "console", Console(file=out, width=40))
    sync_cmd._print_conflict_backups(vault, _BACKUP_TARGET, run)
    return out.getvalue().splitlines()


def test_the_folders_of_the_last_days_are_taken_by_the_time_in_their_name(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 11, 6, 12, 0, 0)
    root = tmp_path / "conflicts"
    day = _backup_folder(root, now - timedelta(days=1), "papers/p1/notes.md.sync-conflict2")
    week = _backup_folder(
        root, now - timedelta(days=6, hours=23), "papers/p2/metadata.yaml.sync-conflict1"
    )
    old = _backup_folder(
        root, now - timedelta(days=7, minutes=1), "papers/p3/notes.md.sync-conflict2"
    )
    _backup_folder(root, now - timedelta(hours=1))  # nothing in it
    _backup_folder(root, now, "x.md", label="-notes")  # not a backup folder's name

    found = recent_conflict_backups(root, now=now)

    assert [(f.path, f.at, f.by_save) for f in found] == [
        (day, now - timedelta(days=1), False),
        (week, now - timedelta(days=6, hours=23), False),
    ]
    assert (found[0].files, found[0].files_total) == (("papers/p1/notes.md.sync-conflict2",), 1)
    # The reverse: a window one day longer takes the older folder too.
    assert [f.path for f in recent_conflict_backups(root, now=now, days=8)] == [day, week, old]


def test_a_folder_a_gui_save_made_is_told_from_a_syncs_by_its_name(tmp_path: Path) -> None:
    parent = tmp_path / "home"
    parent.mkdir()
    vault = create_vault(parent)
    notes = vault / "papers" / "p1" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("the copy a sync brought\n", encoding="utf-8")

    backup = keep_replaced_copy(vault, "papers/p1/notes.md")

    saved = backup.parents[2]
    root = saved.parent
    assert root == conflict_backups_root(vault, "")
    assert saved.name.endswith(SAVE_BACKUP_LABEL)
    # A sync's folder can hold one file under its own name too: the one file
    # changed in a folder another machine deleted.
    synced = _backup_folder(root, datetime.now() - timedelta(minutes=5), "papers/p2/notes.md")
    assert {f.path: f.by_save for f in recent_conflict_backups(root)} == {
        saved: True,
        synced: False,
    }
    # The reverse: without its label, the save's folder reads as a sync's.
    unlabelled = saved.rename(root / (saved.name[: -len(SAVE_BACKUP_LABEL)] + "-9"))
    assert {f.path: f.by_save for f in recent_conflict_backups(root)} == {
        unlabelled: False,
        synced: False,
    }


def test_status_lists_the_folders_of_the_last_days_but_the_last_runs_unwrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "lib"
    vault.mkdir()
    root = conflict_backups_root(vault, _BACKUP_TARGET)
    assert root == bisync_paths(vault, _BACKUP_TARGET).conflicts_root
    now = datetime.now().replace(microsecond=0)
    last = _backup_folder(root, now - timedelta(hours=1), "papers/p0/metadata.yaml.sync-conflict2")
    synced = _backup_folder(
        root,
        now - timedelta(days=2),
        "papers/p1/notes.md.sync-conflict1",
        "papers/p1/notes.md.sync-conflict2",
    )
    saved = _backup_folder(
        root, now - timedelta(days=3), "papers/p9/notes.md", label=SAVE_BACKUP_LABEL
    )
    # The one file changed in a folder deleted on the other side.
    gone = _backup_folder(root, now - timedelta(days=4), "papers/p5/notes.md")
    old = _backup_folder(root, now - timedelta(days=8), "papers/p8/notes.md.sync-conflict2")
    named = last / "papers" / "p0" / "metadata.yaml.sync-conflict2"
    run = LastRun(
        at=now.astimezone().isoformat(),
        trigger="auto",
        result="conflicts",
        conflicts=(RunConflict("papers/p0/metadata.yaml", "local", str(named)),),
        conflicts_total=1,
    )

    lines = _printed_backups(monkeypatch, vault, run)

    assert lines == [
        "",
        "Conflict backups from the last 7 days, besides the last run's:",
        "  A sync keeps the copy that lost as <file>.sync-conflict1 (this machine's) "
        "or .sync-conflict2 (the cloud's).",
        "  A file under its own name in a sync's folder was changed in a folder "
        "deleted on the other side; the deletion won.",
        f"  • {now - timedelta(days=2):%Y-%m-%d %H:%M}, a sync's conflicts, 2 files: {synced}",
        "      papers/p1/notes.md.sync-conflict1",
        "      papers/p1/notes.md.sync-conflict2",
        f"  • {now - timedelta(days=3):%Y-%m-%d %H:%M}, a GUI save over a changed file, "
        f"1 file: {saved}",
        "      papers/p9/notes.md",
        f"  • {now - timedelta(days=4):%Y-%m-%d %H:%M}, a sync's conflicts, 1 file: {gone}",
        "      papers/p5/notes.md",
    ]
    # The reverse: with no record of a run, its folder is listed too.
    lines = _printed_backups(monkeypatch, vault, None)
    assert lines[1] == "Conflict backups from the last 7 days:"
    assert (
        f"  • {now - timedelta(hours=1):%Y-%m-%d %H:%M}, a sync's conflicts, 1 file: {last}"
        in lines
    )
    assert str(old) not in "\n".join(lines)


def test_status_says_what_a_syncs_files_are_only_for_the_kinds_it_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "lib"
    vault.mkdir()
    root = conflict_backups_root(vault, _BACKUP_TARGET)
    now = datetime.now().replace(microsecond=0)
    _backup_folder(root, now - timedelta(hours=1), "papers/p9/notes.md", label=SAVE_BACKUP_LABEL)

    def said(lines: list[str]) -> list[str]:
        return [line for line in lines if not line.startswith(("  • ", "      "))]

    # A save's file is under its own name too, and is no deleted folder's.
    assert said(_printed_backups(monkeypatch, vault, None)) == [
        "",
        "Conflict backups from the last 7 days:",
    ]
    # The reverse: a sync's folder with a copy that lost brings the line on those.
    _backup_folder(root, now - timedelta(hours=2), "papers/p1/notes.md.sync-conflict2")
    assert said(_printed_backups(monkeypatch, vault, None)) == [
        "",
        "Conflict backups from the last 7 days:",
        "  A sync keeps the copy that lost as <file>.sync-conflict1 (this machine's) "
        "or .sync-conflict2 (the cloud's).",
    ]


@pytest.mark.parametrize("extra", [0, 2])
def test_status_names_a_bounded_number_of_folders_and_files_and_counts_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    vault = tmp_path / "lib"
    vault.mkdir()
    root = conflict_backups_root(vault, _BACKUP_TARGET)
    now = datetime.now().replace(microsecond=0)
    n_files = CONFLICT_BACKUP_FILES_CAP + extra
    files = [f"papers/p{i}/notes.md.sync-conflict2" for i in range(n_files)]
    folders = [
        _backup_folder(root, now - timedelta(hours=hours), *files)
        for hours in range(1, sync_cmd._BACKUP_FOLDERS_CAP + extra + 1)
    ]

    lines = _printed_backups(monkeypatch, vault, None)

    heads = [line for line in lines if line.startswith("  • 20")]
    assert len(heads) == sync_cmd._BACKUP_FOLDERS_CAP
    assert heads[0] == (
        f"  • {now - timedelta(hours=1):%Y-%m-%d %H:%M}, a sync's conflicts, "
        f"{n_files} files: {folders[0]}"
    )
    named = [line for line in lines if line.startswith("      papers/")]
    assert len(named) == CONFLICT_BACKUP_FILES_CAP * sync_cmd._BACKUP_FOLDERS_CAP
    # Past the caps, what is left out is counted; at them (the reverse), nothing is.
    more_files = f"      … and {extra} more"
    assert lines.count(more_files) == (sync_cmd._BACKUP_FOLDERS_CAP if extra else 0)
    assert (f"  • … and {extra} more folders in {root}" in lines) is bool(extra)


@needs_rclone
def test_a_conflict_stays_in_status_after_a_later_sync_replaced_the_record_of_its_run(
    tmp_path: Path, cloud: Path
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    now = time.time()
    _edit_metadata(a, "p1", "edited-on-a", now - 200)
    _edit_metadata(b, "p1", "edited-on-b-longer", now - 100)
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == sync_cmd.EXIT_SYNC_CONFLICTS
    (conflict,) = _last_run(b).conflicts
    root = conflict_backups_root(b, _target(cloud))
    backup = Path(conflict.backup)
    folder = root / backup.relative_to(root).parts[0]
    # While the record is that run's, it names the backup, and the folder is
    # not listed again.
    first = _sync(b, "status")
    assert first.exit_code == 0, first.output
    assert conflict.backup in first.output
    assert "Conflict backups from the last" not in first.output

    assert _sync(b).exit_code == 0
    assert _last_run(b).result == "ok"
    later = _sync(b, "status")

    assert later.exit_code == 0, later.output
    lines = later.output.splitlines()
    assert "Conflict backups from the last 7 days:" in lines
    assert any(
        line.startswith("  • ") and line.endswith(f", a sync's conflicts, 1 file: {folder}")
        for line in lines
    ), later.output
    assert f"      {backup.relative_to(folder).as_posix()}" in lines
