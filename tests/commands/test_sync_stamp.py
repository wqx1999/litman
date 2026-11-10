"""The auto-sync stamp from the terminal's side, and the lock that keeps syncs apart.

The stamp is one small file in the cloud folder that tells a machine another
one has synced. ``lit sync`` writes it when it changed the cloud, or found
none: it also marks the folder as synced two-way, which ``push`` and ``pull``
refuse (``test_sync_one_way.py``). Neither of them may carry it into the
library or delete it from the cloud. Real rclone against a ``type = local``
remote, as in ``test_sync_bisync.py``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from litman.commands import sync as sync_cmd
from litman.core import sync as core_sync
from litman.core.sync import SYNC_STAMP_FILENAME, StampRound
from tests.commands.test_sync_bisync import (
    _edit_notes,
    _machine,
    _sync,
    _target,
    _wide_consoles,
    _write_paper,
    cloud,
    needs_rclone,
    tmp_path,
)

__all__ = ["_wide_consoles", "cloud", "tmp_path"]  # fixtures, used by name

pytestmark = needs_rclone


def _stamp(cloud: Path) -> core_sync.Stamp | None:
    return core_sync.read_remote_stamp(_target(cloud))


def _record(vault: Path, cloud: Path) -> core_sync.StampRecord:
    return core_sync.read_stamp_record(core_sync.sync_workdir(vault.resolve(), _target(cloud)))


def _one_line(output: str) -> str:
    return " ".join(output.split())


# ---------------------------------------------------------------------------
# Two-way sync
# ---------------------------------------------------------------------------


def test_a_sync_that_changed_the_cloud_writes_a_stamp_and_one_that_did_not_writes_none(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    first = _stamp(cloud)
    assert first is not None
    assert _record(a, cloud) == core_sync.StampRecord(seen=first.id, written=first.id)

    assert _sync(a).exit_code == 0
    assert _stamp(cloud) == first

    _edit_notes(a, "p1", "# Paper p1\nmore\n", time.time() - 50)
    assert _sync(a).exit_code == 0
    second = _stamp(cloud)
    assert second is not None and second.id != first.id
    assert second.prev == first.id


def test_the_stamp_stays_in_the_cloud_and_out_of_every_library(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    b = _machine(tmp_path, cloud, "b")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0
    _edit_notes(b, "p1", "# Paper p1\nfrom b\n", time.time() - 40)
    assert _sync(b).exit_code == 0
    assert _sync(a).exit_code == 0

    assert (cloud / "lib" / SYNC_STAMP_FILENAME).is_file()
    assert not (a / SYNC_STAMP_FILENAME).exists()
    assert not (b / SYNC_STAMP_FILENAME).exists()


def test_a_sync_remembers_the_stamp_it_started_from_not_one_written_meanwhile(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    work = core_sync.sync_workdir(a.resolve(), _target(cloud))
    started_from = _stamp(cloud)
    assert started_from is not None

    stamps = StampRound.begin(work, _target(cloud))
    meanwhile = core_sync.write_remote_stamp(_target(cloud), prev=started_from.id)
    stamps.finish(remote_changed=False)

    assert _record(a, cloud).seen == started_from.id
    # So the next look still sees a stamp to sync for.
    assert core_sync.check_remote_stamp(work, _target(cloud)).moved
    assert _stamp(cloud) == meanwhile


def test_a_file_changed_here_during_the_sync_is_named_for_the_next_one(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.server.test_sync_scheduler import _write_during_the_next_pass

    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    _write_paper(a, "p2", mtime=time.time() - 60)
    notes = a / "papers" / "p1" / "notes.md"
    _write_during_the_next_pass(
        monkeypatch, lambda: notes.write_text("# p1\nlate\n", encoding="utf-8")
    )

    result = _sync(a)

    assert result.exit_code == 0, result.output
    said = _one_line(result.output)
    assert "1 file changed here while the sync ran" in said
    assert "papers/p1/notes.md" in said
    assert "The next `lit sync` takes it up." in said
    again = _sync(a)
    assert again.exit_code == 0
    assert "late" in (cloud / "lib" / "papers" / "p1" / "notes.md").read_text(encoding="utf-8")


def test_a_sync_that_finds_no_stamp_writes_one_with_nothing_to_sync(
    tmp_path: Path, cloud: Path
) -> None:
    """The stamp also marks the folder as synced two-way, so one that went
    missing (a push from an older litman deletes it) comes back at the next
    sync, even one that changed nothing in the cloud."""
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    (cloud / "lib" / SYNC_STAMP_FILENAME).unlink()
    # This machine no longer remembers writing one either.
    work = core_sync.sync_workdir(a.resolve(), _target(cloud))
    (work / core_sync.STAMP_RECORD_NAME).unlink()

    result = core_sync.two_way_sync(a, _target(cloud))

    assert result.remote_changed is False
    assert result.stamp_written is True
    stamp = _stamp(cloud)
    assert stamp is not None and stamp.prev is None
    # The next sync finds it and writes none.
    again = core_sync.two_way_sync(a, _target(cloud))
    assert again.stamp_written is False
    assert _stamp(cloud) == stamp


# ---------------------------------------------------------------------------
# push / pull and the stamp
# ---------------------------------------------------------------------------


def _synced_with_a_stamp(tmp_path: Path, cloud: Path) -> tuple[Path, core_sync.Stamp]:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    stamp = _stamp(cloud)
    assert stamp is not None
    return a, stamp


def test_a_one_way_push_or_pull_leaves_a_stamp_where_it_is(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lit sync push` and `pull` refuse a folder with a stamp, but one written
    by another machine's first two-way sync while they run must neither be
    deleted from the cloud nor land in the library. The transfers alone, past
    the refusal."""
    a, before = _synced_with_a_stamp(tmp_path, cloud)
    b = _machine(tmp_path, cloud, "b")
    target = _target(cloud)

    core_sync.push(a, target)
    assert _stamp(cloud) == before
    core_sync.pull(b, target)
    assert (b / "papers" / "p1" / "metadata.yaml").is_file()
    assert not (b / SYNC_STAMP_FILENAME).exists()
    assert _stamp(cloud) == before
    # Neither touched this machine's record of the stamp.
    assert _record(b, cloud) == core_sync.StampRecord()

    # The reverse: without the exclude a pull carries it in, a push deletes it.
    monkeypatch.setattr(core_sync, "_STAMP_EXCLUDE_ARGS", ())
    core_sync.pull(b, target)
    assert (b / SYNC_STAMP_FILENAME).is_file()
    (b / SYNC_STAMP_FILENAME).unlink()
    core_sync.push(a, target)
    assert _stamp(cloud) is None


# ---------------------------------------------------------------------------
# One sync at a time
# ---------------------------------------------------------------------------


def test_the_shipped_wait_is_ninety_seconds() -> None:
    assert core_sync.SYNC_LOCK_WAIT == 90
    assert sync_cmd.SYNC_LOCK_WAIT == 90
    assert sync_cmd.EXIT_SYNC_BUSY == 23


def _hold_lock(vault: Path, cloud: Path, seconds: float) -> threading.Thread:
    """Another sync (the GUI's) holding the library's lock for ``seconds``."""
    work = core_sync.sync_workdir(vault.resolve(), _target(cloud))
    work.mkdir(parents=True, exist_ok=True)
    held = threading.Event()

    def hold() -> None:
        with core_sync.hold_sync_lock(work, by="lit gui", wait=0):
            held.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(10)
    return thread


def test_a_sync_waits_for_the_other_one_and_then_runs(tmp_path: Path, cloud: Path) -> None:
    a, _stamp_before = _synced_with_a_stamp(tmp_path, cloud)
    holder = _hold_lock(a, cloud, 1.5)
    try:
        result = _sync(a)
    finally:
        holder.join()

    assert result.exit_code == 0, result.output
    said = _one_line(result.output)
    assert "Another sync of this library is running (lit gui, since" in said
    assert "waiting up to 90 seconds" in said


@pytest.mark.parametrize("sub", [[], ["push", "--yes"], ["pull"]], ids=["sync", "push", "pull"])
def test_a_sync_that_cannot_get_the_lock_exits_23_and_says_why(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch, sub: list[str]
) -> None:
    if sub:
        # push and pull refuse a library that syncs two-way: one that only pushed.
        a = _machine(tmp_path, cloud, "a")
        _write_paper(a, "p1")
        core_sync.push(a, _target(cloud))
        assert not core_sync.bisync_initialised(core_sync.sync_workdir(a.resolve(), _target(cloud)))
    else:
        a, _stamp_before = _synced_with_a_stamp(tmp_path, cloud)
    monkeypatch.setattr(sync_cmd, "SYNC_LOCK_WAIT", 0.3)
    before = (a / "papers" / "p1" / "notes.md").read_bytes()
    _edit_notes(a, "p1", "# Paper p1\nnot synced\n", time.time() - 30)
    holder = _hold_lock(a, cloud, 3)
    try:
        result = _sync(a, *sub)
    finally:
        holder.join()

    assert result.exit_code == sync_cmd.EXIT_SYNC_BUSY, result.output
    said = _one_line(result.output)
    assert "Another sync of this library is running (lit gui, since" in said
    assert "so this one did not start" in said
    # Nothing moved either way.
    assert (cloud / "lib" / "papers" / "p1" / "notes.md").read_bytes() == before
    assert "not synced" in (a / "papers" / "p1" / "notes.md").read_text(encoding="utf-8")


def test_exit_23_is_documented() -> None:
    doc = (Path(__file__).parents[2] / "docs" / "4-commands.md").read_text(encoding="utf-8")
    section = doc.split("### `lit sync`", 1)[1].split("\n### ", 1)[0]
    assert f"| `{sync_cmd.EXIT_SYNC_BUSY}` |" in section
    assert "90 seconds" in section


def test_a_real_pull_names_the_files_it_wrote_here(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On every platform the operation lines name the library folder as the
    run's "Synching Path1" line does, so a pull's writes here are known
    file by file (unknown would cost a second sync after every pull)."""
    a = _machine(tmp_path, cloud, "a")
    b = _machine(tmp_path, cloud, "b")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0
    _edit_notes(a, "p1", "# Paper p1\nfrom a\n", time.time() - 40)
    _write_paper(a, "p2", mtime=time.time() - 40)
    assert _sync(a).exit_code == 0
    runs: list[core_sync.BisyncRun] = []
    real = core_sync.run_bisync

    def recording(args: list[str], on_log: object = None, **kwargs: object) -> core_sync.BisyncRun:
        run = real(args, on_log, **kwargs)  # type: ignore[arg-type]
        runs.append(run)
        return run

    monkeypatch.setattr(core_sync, "run_bisync", recording)
    result = core_sync.two_way_sync(b, _target(cloud))

    (ops,) = [core_sync.parse_bisync_ops(run) for run in runs]
    assert not ops.remote_changed
    # On failure, the lines that name the library folder, as this rclone printed them.
    named = [line for line in runs[0].report if "Synching" in line or "Queue" in line]
    assert ops.path1_writes is not None, named
    assert {"papers/p1/notes.md", "papers/p2/metadata.yaml", "papers/p2/paper.pdf"} <= set(
        ops.path1_writes
    )
    assert result.unsynced == ()
    assert core_sync.local_changes_pending(b, _target(cloud)) is False


# ---------------------------------------------------------------------------
# A stamp for every upload, even one cut off; never over an unseen stamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marked", [True, False], ids=["marked", "reverse-unmarked"])
def test_a_sync_cut_off_before_its_stamp_leaves_one_owed(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch, marked: bool
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    before = _stamp(cloud)
    work = core_sync.sync_workdir(a.resolve(), _target(cloud))
    real_begin, real_finish = StampRound.begin.__func__, StampRound.finish  # type: ignore[attr-defined]
    if not marked:
        # The reverse: a sync that does not say it started.
        monkeypatch.setattr(
            StampRound, "begin",
            classmethod(lambda cls, w, t, mark=True: real_begin(cls, w, t, mark=False)),
        )

    def cut_off(self: StampRound, **kwargs: object) -> bool:
        raise KeyboardInterrupt  # killed right where the stamp would go

    monkeypatch.setattr(StampRound, "finish", cut_off)
    _write_paper(a, "p2", mtime=time.time() - 60)
    with pytest.raises(KeyboardInterrupt):
        core_sync.two_way_sync(a, _target(cloud))
    monkeypatch.setattr(StampRound, "finish", real_finish)

    assert (cloud / "lib" / "papers" / "p2" / "metadata.yaml").is_file()
    assert _stamp(cloud) == before
    assert core_sync.check_remote_stamp(work, _target(cloud)).owed is marked
    # The next sync has nothing left to upload; it still owes the stamp.
    again = core_sync.two_way_sync(a, _target(cloud))
    assert again.remote_changed is False
    assert again.stamp_written is marked
    assert not core_sync.check_remote_stamp(work, _target(cloud)).owed


def test_the_stamp_goes_up_before_the_code_repos_are_fetched(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The git step can take minutes; a run cut off there (an agent's time
    limit, Ctrl+C) has uploaded already, and the other machines must hear."""
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    before = _stamp(cloud)
    assert before is not None

    def cut_off(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(core_sync, "_bring_repos_in", cut_off)
    _write_paper(a, "p2", mtime=time.time() - 60)
    with pytest.raises(KeyboardInterrupt):
        core_sync.two_way_sync(a, _target(cloud))

    after = _stamp(cloud)
    assert after is not None and after.id != before.id
    assert after.prev == before.id
    assert _record(a, cloud) == core_sync.StampRecord(seen=after.id, written=after.id)


def test_an_owed_stamp_is_never_written_over_one_not_synced_here(
    tmp_path: Path, cloud: Path
) -> None:
    """Written, it would name the other machine's stamp as seen, though its
    change never came down: neither machine would sync again for it."""
    a = _machine(tmp_path, cloud, "a")
    b = _machine(tmp_path, cloud, "b")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0
    work_b = core_sync.sync_workdir(b.resolve(), _target(cloud))
    # B owes a stamp (its write failed); A then syncs a change B has not pulled.
    core_sync.write_stamp_record(work_b, replace(_record(b, cloud), owed=True))
    _write_paper(a, "p2", mtime=time.time() - 60)
    assert _sync(a).exit_code == 0
    from_a = _stamp(cloud)
    assert from_a is not None

    assert core_sync.write_owed_stamp(work_b, _target(cloud)) is False
    assert _stamp(cloud) == from_a
    assert _record(b, cloud).owed

    # B syncs instead, and writes the owed stamp after A's, having pulled it.
    assert _sync(b).exit_code == 0
    assert (b / "papers" / "p2" / "metadata.yaml").is_file()
    after = _stamp(cloud)
    assert after is not None and after.prev == from_a.id
    # The reverse: with nothing new in the cloud, the owed stamp goes at once.
    core_sync.write_stamp_record(work_b, replace(_record(b, cloud), owed=True))
    assert core_sync.write_owed_stamp(work_b, _target(cloud)) is True
    latest = _stamp(cloud)
    assert latest is not None and latest.prev == after.id
    assert not _record(b, cloud).owed


def test_an_unreadable_stamp_reads_as_moved_never_as_overwritten(
    tmp_path: Path, cloud: Path
) -> None:
    """Two stamp files of one name (Google Drive allows that) read as one
    damaged stamp. Taken as overwritten, it would set both machines writing
    and syncing for good."""
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    work = core_sync.sync_workdir(a.resolve(), _target(cloud))
    assert _record(a, cloud).written is not None
    (cloud / "lib" / SYNC_STAMP_FILENAME).write_text("{not json\n{not json\n", encoding="utf-8")

    check = core_sync.check_remote_stamp(work, _target(cloud))
    assert check.moved and not check.owed
    result = core_sync.two_way_sync(a, _target(cloud))
    assert result.stamp_written is False
    assert not core_sync.check_remote_stamp(work, _target(cloud)).moved
    # The reverse: a readable stamp that names neither ours nor ours as prev
    # did replace ours unseen.
    record = core_sync.StampRecord(seen="x", written="mine")
    assert core_sync.stamp_overwritten(record, core_sync.Stamp(id="theirs", prev="older"))
    assert not core_sync.stamp_overwritten(record, core_sync.Stamp(id="unreadable-0123"))


# ---------------------------------------------------------------------------
# What changed here while a sync ran, and litman's own late fix-ups
# ---------------------------------------------------------------------------


def test_a_file_rewritten_here_after_the_sync_wrote_it_is_named_for_the_next_one(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sync wrote A's notes here, then an agent on B wrote them again
    before the sync ended: B's version must still read as a change to upload.
    (The reverse, nothing written after the sync:
    test_a_real_pull_names_the_files_it_wrote_here.)"""
    a = _machine(tmp_path, cloud, "a")
    b = _machine(tmp_path, cloud, "b")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0
    _edit_notes(a, "p1", "# Paper p1\nfrom a\n", time.time() - 40)
    assert _sync(a).exit_code == 0
    notes_b = b / "papers" / "p1" / "notes.md"
    real_sweep = core_sync.sweep_conflicts

    def sweep_then_an_agent_writes(vault: Path, dest: Path) -> list[core_sync.SyncConflict]:
        rows = real_sweep(vault, dest)
        if vault == b and "from a" in notes_b.read_text(encoding="utf-8"):
            notes_b.write_text("# Paper p1\nfrom an agent on b, after the pass\n", encoding="utf-8")
        return rows

    monkeypatch.setattr(core_sync, "sweep_conflicts", sweep_then_an_agent_writes)

    result = core_sync.two_way_sync(b, _target(cloud))

    assert result.unsynced == ("papers/p1/notes.md",)
    assert core_sync.local_changes_pending(b, _target(cloud))
    monkeypatch.setattr(core_sync, "sweep_conflicts", real_sweep)
    assert _sync(b).exit_code == 0
    assert "from an agent on b" in (cloud / "lib" / "papers" / "p1" / "notes.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "reverse-not-stopped"])
def test_fix_ups_no_pass_took_up_make_the_next_look_sync(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch, stopped: bool
) -> None:
    a = _machine(tmp_path, cloud, "a")
    b = _machine(tmp_path, cloud, "b")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0
    _edit_notes(a, "p1", "# Paper p1\nfrom a\n", time.time() - 40)
    _edit_notes(b, "p1", "# Paper p1\nfrom b\n", time.time() - 30)
    assert _sync(a).exit_code == 0
    control = core_sync.SyncControl()
    real_sweep = core_sync.sweep_conflicts

    def sweep_then_stop(vault: Path, dest: Path) -> list[core_sync.SyncConflict]:
        rows = real_sweep(vault, dest)
        if stopped and rows:
            control.interrupt.set()  # the GUI is closing
        return rows

    monkeypatch.setattr(core_sync, "sweep_conflicts", sweep_then_stop)

    result = core_sync.two_way_sync(b, _target(cloud), control=control)

    assert [c.path for c in result.conflicts] == ["papers/p1/notes.md"]
    assert core_sync.local_changes_pending(b, _target(cloud)) is stopped


def test_the_rebuild_after_a_sync_holds_the_gui_s_saves_off(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save landing while INDEX.json is rebuilt from an earlier read would
    be missing from it until the next rebuild."""
    from litman.core import correctors
    from litman.core.sync_lock import FileLock

    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    real = correctors.reconcile_derived
    held: list[bool] = []

    def rebuild(vault: Path, **kwargs: object) -> object:
        probe = FileLock(core_sync.pass_lock_path(vault.resolve(), _target(cloud)))
        got = probe.try_acquire()
        held.append(not got)
        if got:
            probe.release()
        return real(vault, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(correctors, "reconcile_derived", rebuild)

    assert _sync(a).exit_code == 0
    assert held == [True]
