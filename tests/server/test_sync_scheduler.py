"""The GUI's auto sync, as two GUIs open on machines A and B at once.

Two vaults stand for the two machines, a ``type = local`` rclone remote for the
cloud (as in ``tests/commands/test_sync_bisync.py``), and each machine gets its
own :class:`SyncScheduler`, driven one look at a time (:meth:`SyncScheduler.step`)
on a fake clock: no test waits for a timer. Every sync is a real ``rclone
bisync``; CI installs rclone on all three platforms.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from litman.core import locking
from litman.core import sync as core_sync
from litman.exceptions import SyncError
from litman.server import sync_scheduler as sched_mod
from litman.server.sync_scheduler import SyncScheduler
from tests.commands.test_sync_bisync import (
    _conflict_leftovers,
    _edit_metadata,
    _local_backup_files,
    _sync,
    _target,
    _tree,
    _two_synced_machines,
    _write_paper,
    cloud,
    needs_rclone,
    tmp_path,
)

__all__ = ["cloud", "tmp_path"]  # fixtures, used by name

pytestmark = needs_rclone


class _Clock:
    """The scheduler's clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class _Machine:
    """One machine's library and the auto sync of a GUI open on it."""

    def __init__(self, vault: Path, clock: _Clock) -> None:
        self.vault = vault
        self.state = SimpleNamespace(vault=vault.resolve())
        self.scheduler = SyncScheduler(self.state, clock=clock)

    def step(self) -> None:
        self.scheduler.step()

    @property
    def status(self) -> dict[str, Any]:
        return self.scheduler.status()


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """How many bisync runs and stamp writes happen from here on."""
    counts = {"bisync": 0, "stamp": 0}
    real_run, real_write = core_sync.run_bisync, core_sync.write_remote_stamp

    def run(args: list[str], on_log: Any = None, **kwargs: Any) -> core_sync.BisyncRun:
        counts["bisync"] += 1
        return real_run(args, on_log, **kwargs)

    def write(target_url: str, *, prev: str | None) -> core_sync.Stamp:
        counts["stamp"] += 1
        return real_write(target_url, prev=prev)

    monkeypatch.setattr(core_sync, "run_bisync", run)
    monkeypatch.setattr(core_sync, "write_remote_stamp", write)
    return counts


def _machines(tmp_path: Path, cloud: Path, n_papers: int = 4) -> tuple[_Machine, _Machine, _Clock]:
    """A and B synced with each other by `lit sync`, each with a GUI just started."""
    a, b = _two_synced_machines(tmp_path, cloud, n_papers)
    clock = _Clock()
    return _Machine(a, clock), _Machine(b, clock), clock


def _settle_start(*machines: _Machine) -> None:
    for m in machines:
        m.step()
        assert m.status["state"] == "ok", m.status


def _last_run(vault: Path) -> core_sync.LastRun:
    run = core_sync.read_sync_state(vault).last_run
    assert run is not None
    return run


def _still_for_a_minute(m: _Machine, clock: _Clock) -> None:
    """Let ``m`` notice a change, then keep it still past the settle time."""
    m.step()
    clock.advance(sched_mod.SETTLE_S + 1)
    m.step()


def _stamp_check(m: _Machine, clock: _Clock) -> None:
    clock.advance(sched_mod.STAMP_EVERY_S * (1 + sched_mod.STAMP_JITTER) + 1)
    m.step()


# ---------------------------------------------------------------------------
# 1-4: changes travel, conflicts settle, idle machines stay quiet
# ---------------------------------------------------------------------------


def test_a_change_on_one_machine_reaches_the_other_and_back(
    tmp_path: Path, cloud: Path
) -> None:
    ma, mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma, mb)

    _write_paper(ma.vault, "batch1", mtime=time.time() - 50)
    _still_for_a_minute(ma, clock)
    assert (cloud / "lib" / "papers" / "batch1" / "metadata.yaml").is_file()
    _stamp_check(mb, clock)
    assert (mb.vault / "papers" / "batch1" / "metadata.yaml").is_file()

    _write_paper(mb.vault, "batch2", mtime=time.time() - 40)
    _still_for_a_minute(mb, clock)
    _stamp_check(ma, clock)
    assert (ma.vault / "papers" / "batch2" / "metadata.yaml").is_file()
    assert _tree(ma.vault) == _tree(mb.vault) == _tree(cloud / "lib")
    assert ma.status["state"] == mb.status["state"] == "ok"


def test_a_change_is_synced_only_once_it_has_been_still_for_a_minute(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    ma, _mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma)
    runs = counted["bisync"]

    _write_paper(ma.vault, "p9", mtime=time.time() - 50)
    ma.step()
    clock.advance(sched_mod.SETTLE_S - 5)
    ma.step()
    assert counted["bisync"] == runs
    clock.advance(10)
    ma.step()
    assert counted["bisync"] == runs + 1
    assert (cloud / "lib" / "papers" / "p9").is_dir()


def test_edits_of_different_papers_on_both_machines_end_up_everywhere(
    tmp_path: Path, cloud: Path
) -> None:
    ma, mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma, mb)

    now = time.time()
    _edit_metadata(ma.vault, "p1", "reading", now - 60)
    _edit_metadata(mb.vault, "p2", "read", now - 50)
    ma.step()
    mb.step()
    clock.advance(sched_mod.SETTLE_S + 1)
    ma.step()
    mb.step()
    _stamp_check(ma, clock)
    _stamp_check(mb, clock)

    assert _tree(ma.vault) == _tree(mb.vault) == _tree(cloud / "lib")
    assert "status: reading" in (mb.vault / "papers/p1/metadata.yaml").read_text(encoding="utf-8")
    assert "status: read" in (ma.vault / "papers/p2/metadata.yaml").read_text(encoding="utf-8")
    assert ma.status["notice"] is None and mb.status["notice"] is None
    assert ma.status["state"] == mb.status["state"] == "ok"


def test_the_same_file_changed_on_both_keeps_the_newer_and_tells_the_page_once(
    tmp_path: Path, cloud: Path
) -> None:
    ma, mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma, mb)

    now = time.time()
    _edit_metadata(ma.vault, "p1", "reading", now - 120)
    _edit_metadata(mb.vault, "p1", "read", now - 60)
    _still_for_a_minute(ma, clock)
    seq = mb.status["seq"]
    _still_for_a_minute(mb, clock)

    status = mb.status
    assert status["state"] == "conflicts"
    assert status["seq"] == seq + 1
    ((row,),) = [status["notice"]["conflicts"]]
    assert row["path"] == "papers/p1/metadata.yaml"
    assert row["kept"] == "local"
    assert "status: reading" in Path(row["backup"]).read_text(encoding="utf-8")
    _stamp_check(ma, clock)
    for vault in (ma.vault, mb.vault):
        text = (vault / "papers/p1/metadata.yaml").read_text(encoding="utf-8")
        assert "status: read\n" in text
    assert _conflict_leftovers(ma.vault, mb.vault, cloud / "lib") == []
    backups = _local_backup_files(mb.vault, cloud)
    assert any(rel.startswith("papers/p1/metadata.yaml.sync-conflict") for rel in backups)


def test_a_sync_with_nothing_new_writes_no_stamp_and_idle_machines_stay_quiet(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    ma, mb, clock = _machines(tmp_path, cloud)
    stamps = counted["stamp"]
    _settle_start(ma, mb)
    assert counted["stamp"] == stamps

    runs = counted["bisync"]
    for _ in range(20):
        clock.advance(sched_mod.STAMP_EVERY_S * (1 + sched_mod.STAMP_JITTER) + 1)
        ma.step()
        mb.step()
    assert counted["bisync"] == runs
    assert counted["stamp"] == stamps


def test_a_stamp_written_over_unseen_is_written_again_once_and_then_all_is_quiet(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    target = _target(cloud)
    work_a = core_sync.sync_workdir(a.resolve(), target)
    work_b = core_sync.sync_workdir(b.resolve(), target)

    # A's sync reads the stamp, B syncs a change and writes its stamp X, then
    # A writes its own over X without having seen it.
    a_round = core_sync.StampRound.begin(work_a, target)
    _write_paper(b, "from-b", mtime=time.time() - 50)
    core_sync.two_way_sync(b, target)
    x = core_sync.read_remote_stamp(target)
    a_round.finish(remote_changed=True)
    u = core_sync.read_remote_stamp(target)
    assert x is not None and u is not None and u.prev != x.id

    # B notices its stamp went unseen: it syncs and writes a new one, even
    # though nothing of its own goes up.
    assert core_sync.check_remote_stamp(work_b, target).moved
    again = core_sync.two_way_sync(b, target)
    assert again.went_up is not None and again.went_up.total == 0
    assert again.stamp_written
    # A then syncs once, picks B's paper up, and writes nothing.
    assert core_sync.check_remote_stamp(work_a, target).moved
    after = core_sync.two_way_sync(a, target)
    assert not after.stamp_written
    assert (a / "papers" / "from-b").is_dir()

    writes = counted["stamp"]
    for work in (work_a, work_b) * 5:
        assert not core_sync.check_remote_stamp(work, target).moved
    assert counted["stamp"] == writes


# ---------------------------------------------------------------------------
# 5-6: offline, and errors that need the user
# ---------------------------------------------------------------------------


def _unreachable_cloud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the same remote name at a server that refuses connections.

    The target string stays the same, so does the machine's sync state; a
    local folder moved away would instead read as an emptied cloud folder,
    which needs the user, not a retry.
    """
    conf = tmp_path / "rclone-offline.conf"
    conf.write_text(
        "[fake-cloud]\ntype = sftp\nhost = 127.0.0.1\nport = 9\nuser = nobody\n"
        # `rclone obscure x`: sftp needs a password to try, any will do.
        "pass = 2Tri8fPTZ6vIFJV9X8HB3Ls1Y7Yf\nknown_hosts_file =\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    # Fail fast: one connection attempt, a short timeout.
    monkeypatch.setenv("RCLONE_LOW_LEVEL_RETRIES", "1")
    monkeypatch.setenv("RCLONE_CONTIMEOUT", "2s")
    return conf


def test_offline_retries_after_two_then_four_minutes_and_recovers(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch, counted: dict[str, int]
) -> None:
    import os

    ma, _mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma)
    online = os.environ["RCLONE_CONFIG"]
    _write_paper(ma.vault, "offline-edit", mtime=time.time() - 50)
    _unreachable_cloud(monkeypatch, tmp_path)

    _still_for_a_minute(ma, clock)
    status = ma.status
    assert status["state"] == "offline", status
    assert status["nextTryAt"] is not None
    runs = counted["bisync"]

    clock.advance(sched_mod.BACKOFF_S[0] - 10)
    ma.step()
    assert counted["bisync"] == runs
    clock.advance(20)
    ma.step()
    assert counted["bisync"] == runs + 1
    assert ma.status["state"] == "offline"

    clock.advance(sched_mod.BACKOFF_S[1] - 10)
    ma.step()
    assert counted["bisync"] == runs + 1
    monkeypatch.setenv("RCLONE_CONFIG", online)
    clock.advance(20)
    ma.step()
    assert ma.status["state"] == "ok", ma.status
    assert (cloud / "lib" / "papers" / "offline-edit").is_dir()


def _delete_most_papers(vault: Path) -> None:
    for i in range(1, 8):
        locking.rmtree(vault / "papers" / f"p{i}")


def test_rclones_safety_stop_stops_auto_sync_until_the_user_decides(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    ma, _mb, clock = _machines(tmp_path, cloud, n_papers=10)
    _settle_start(ma)
    _delete_most_papers(ma.vault)

    _still_for_a_minute(ma, clock)
    status = ma.status
    assert status["state"] == "error"
    assert status["reason"] == "safety-stop"
    assert "lit sync --force" in status["message"]
    assert status["notice"]["kind"] == "stopped"
    # What `lit sync status` shows the agent the user asks.
    run = _last_run(ma.vault)
    assert (run.trigger, run.result, run.reason) == ("auto", "failed", "safety-stop")
    assert run.error == status["message"]
    runs = counted["bisync"]
    for _ in range(5):
        _stamp_check(ma, clock)
    assert counted["bisync"] == runs
    assert ma.status["state"] == "error"

    # The user decides in a terminal; the auto sync goes on from there.
    assert _sync(ma.vault, "--force").exit_code == 0
    assert (_last_run(ma.vault).trigger, _last_run(ma.vault).result) == ("terminal", "ok")
    ma.step()
    assert ma.status["state"] == "ok", ma.status


def test_a_click_retries_a_stopped_auto_sync(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    ma, _mb, clock = _machines(tmp_path, cloud, n_papers=10)
    _settle_start(ma)
    _delete_most_papers(ma.vault)
    _still_for_a_minute(ma, clock)
    assert ma.status["state"] == "error"
    runs = counted["bisync"]

    ma.scheduler.request_sync()
    ma.step()
    assert counted["bisync"] == runs + 1


# ---------------------------------------------------------------------------
# 7: a write while a sync runs is not lost
# ---------------------------------------------------------------------------


class _Tap:
    """bisync's output stream, calling ``hook`` once on the line that holds ``when``."""

    def __init__(self, stream: Any, when: str, hook: Any) -> None:
        self._stream, self._when, self._hook = stream, when, hook

    def __iter__(self) -> Any:
        for line in self._stream:
            if self._hook is not None and self._when in line:
                hook, self._hook = self._hook, None
                hook()
            yield line

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _write_during_the_next_pass(monkeypatch: pytest.MonkeyPatch, hook: Any) -> None:
    """Run ``hook`` once, while the next bisync runs, after it listed this machine's files."""
    real = core_sync.subprocess.Popen
    pending = [hook]

    class Tapped(real):  # type: ignore[misc, valid-type]
        def __init__(self, args: Any, *a: Any, **k: Any) -> None:
            super().__init__(args, *a, **k)
            if "bisync" in args and pending:
                self.stdout: Any = _Tap(self.stdout, "Path2 checking for diffs", pending.pop())

    monkeypatch.setattr(core_sync.subprocess, "Popen", Tapped)


def test_a_file_written_here_during_a_pass_goes_up_with_a_follow_up_sync(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ma, _mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma)
    _write_paper(ma.vault, "first", mtime=time.time() - 60)
    late = ma.vault / "papers" / "p1" / "notes.md"

    def write_late() -> None:
        late.write_text("# Paper p1\nwritten while the sync ran\n", encoding="utf-8")

    _write_during_the_next_pass(monkeypatch, write_late)
    _still_for_a_minute(ma, clock)
    cloud_copy = cloud / "lib" / "papers" / "p1" / "notes.md"
    assert "while the sync ran" not in cloud_copy.read_text(encoding="utf-8")

    # The follow-up: still for a minute after the sync, then synced.
    clock.advance(sched_mod.SETTLE_S + 1)
    ma.step()
    assert "while the sync ran" in cloud_copy.read_text(encoding="utf-8")


def test_without_the_record_of_late_writes_the_late_write_would_stay_behind(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse of the test above: the follow-up depends on that record."""
    ma, _mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma)
    _write_paper(ma.vault, "first", mtime=time.time() - 60)
    late = ma.vault / "papers" / "p1" / "notes.md"
    monkeypatch.setattr(core_sync, "_changed_during", lambda *a, **k: ((), False))
    _write_during_the_next_pass(
        monkeypatch,
        lambda: late.write_text("# Paper p1\nwritten while the sync ran\n", encoding="utf-8"),
    )
    _still_for_a_minute(ma, clock)
    clock.advance(sched_mod.SETTLE_S + 1)
    ma.step()
    clock.advance(sched_mod.SETTLE_S + 1)
    ma.step()
    cloud_copy = cloud / "lib" / "papers" / "p1" / "notes.md"
    assert "while the sync ran" not in cloud_copy.read_text(encoding="utf-8")


def test_a_terminal_sync_waits_for_the_gui_and_the_gui_retries_after_a_terminal_one(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    ma, _mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma)
    work = core_sync.sync_workdir(ma.vault.resolve(), _target(cloud))
    _write_paper(ma.vault, "p9", mtime=time.time() - 50)

    with core_sync.hold_sync_lock(work, by="lit sync", wait=0):
        _still_for_a_minute(ma, clock)
        status = ma.status
        assert status["state"] == "running"
        assert status["reason"] == "busy"
        assert "lit sync" in status["message"]
    runs = counted["bisync"]
    clock.advance(sched_mod.BUSY_RETRY_S + 1)
    ma.step()
    assert counted["bisync"] == runs + 1
    assert ma.status["state"] == "ok"


# ---------------------------------------------------------------------------
# The library switch, the status the page gets, and the real thread
# ---------------------------------------------------------------------------


def test_a_library_without_sync_has_no_status_for_the_page(tmp_path: Path) -> None:
    from litman.core.library import create_vault

    parent = tmp_path / "plain"
    parent.mkdir()
    vault = create_vault(parent)
    scheduler = SyncScheduler(SimpleNamespace(vault=vault))
    assert scheduler.status_for(vault) is None
    scheduler.step()
    assert scheduler.status()["state"] == "not-configured"
    assert scheduler.status_for(vault) is None


def test_a_machine_that_never_synced_is_left_to_lit_sync(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    from tests.commands.test_sync_bisync import _machine

    vault = _machine(tmp_path, cloud, "fresh")
    scheduler = SyncScheduler(SimpleNamespace(vault=vault))
    scheduler.step()
    assert scheduler.status()["state"] == "not-initialised"
    assert counted["bisync"] == 0


def test_switching_libraries_syncs_a_change_left_on_the_old_one(
    tmp_path: Path, cloud: Path
) -> None:
    from litman.core.library import create_vault

    ma, _mb, _clock = _machines(tmp_path, cloud)
    _settle_start(ma)
    _write_paper(ma.vault, "left-behind", mtime=time.time() - 50)
    other = tmp_path / "other"
    other.mkdir()
    ma.state.vault = create_vault(other)
    ma.step()
    assert (cloud / "lib" / "papers" / "left-behind").is_dir()
    assert ma.status["state"] == "not-configured"


def test_the_real_thread_syncs_on_start_and_stops(tmp_path: Path, cloud: Path) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    scheduler = SyncScheduler(SimpleNamespace(vault=a.resolve()))
    scheduler.start()
    try:
        deadline = time.monotonic() + 60
        while scheduler.status()["seq"] == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert scheduler.status()["state"] == "ok"
    finally:
        scheduler.stop()
        assert scheduler._thread is not None
        scheduler._thread.join(timeout=30)
        assert not scheduler._thread.is_alive()


# ---------------------------------------------------------------------------
# 8: the sync before exit
# ---------------------------------------------------------------------------


def _started(vault: Path) -> SyncScheduler:
    """A scheduler whose thread ran its start sync and stands by."""
    scheduler = SyncScheduler(SimpleNamespace(vault=vault.resolve()))
    scheduler.start()
    deadline = time.monotonic() + 60
    while scheduler.status()["seq"] == 0 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert scheduler.status()["state"] == "ok"
    return scheduler


def test_the_exit_sync_takes_a_change_up(tmp_path: Path, cloud: Path) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    scheduler = _started(a)
    _write_paper(a, "at-exit", mtime=time.time() - 50)
    said: list[str] = []

    assert scheduler.final_sync(on_start=lambda: said.append("syncing")) is True
    assert said == ["syncing"]
    assert (cloud / "lib" / "papers" / "at-exit" / "metadata.yaml").is_file()


def test_the_exit_sync_does_nothing_when_nothing_changed_or_after_a_self_update(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    scheduler = _started(a)
    runs = counted["bisync"]
    assert scheduler.final_sync() is False
    assert counted["bisync"] == runs

    updating = _started(a)
    _write_paper(a, "after-update", mtime=time.time() - 50)
    updating.self_update_pending = True
    runs = counted["bisync"]
    assert updating.final_sync() is False
    assert counted["bisync"] == runs
    assert not (cloud / "lib" / "papers" / "after-update").exists()


def test_the_exit_sync_asks_rclone_to_stop_and_the_next_sync_finishes(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    scheduler = _started(a)
    (a / "papers" / "p1" / "notes.md").write_bytes(b"x" * 200_000)
    # 20 KB/s: the upload takes ten seconds.
    monkeypatch.setenv("RCLONE_BWLIMIT", "20k")

    started = time.monotonic()
    scheduler.final_sync(budget=45, interrupt_after=1)
    assert time.monotonic() - started < 40
    # Stopped (POSIX: asked to stop at 1 s, it finishes the file in flight and
    # saves its state) or done (Windows: a windowless process cannot ask), and
    # either way rclone took its lock away.
    work = core_sync.sync_workdir(a.resolve(), _target(cloud))
    assert list(work.glob("*.lck")) == []

    monkeypatch.delenv("RCLONE_BWLIMIT")
    result = _sync(a)
    assert result.exit_code == 0, result.output
    assert (cloud / "lib" / "papers" / "p1" / "notes.md").stat().st_size == 200_000


def test_a_killed_rclone_ends_at_once(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    (a / "papers" / "p1" / "notes.md").write_bytes(b"x" * 400_000)
    monkeypatch.setenv("RCLONE_BWLIMIT", "20k")
    paths = core_sync.bisync_paths(a.resolve(), _target(cloud))
    name = core_sync._reserve_backup_name(paths.local_backup_root)
    args = core_sync.bisync_args(a.resolve(), _target(cloud), paths, backup_name=name)
    control = core_sync.SyncControl()
    killer = threading.Timer(1.0, control.kill.set)
    killer.start()

    started = time.monotonic()
    run = core_sync.run_bisync(args, control=control)
    assert time.monotonic() - started < 10
    assert run.returncode != 0


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no SIGTERM to send: it ends a process outright, with no way to run code",
)
def test_a_sigterm_to_a_running_gui_syncs_a_change_before_it_exits(
    tmp_path: Path, cloud: Path
) -> None:
    import json
    import os
    import signal
    import socket
    import subprocess
    import urllib.request

    import litman
    from tests.commands.test_sync_bisync import _machine

    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    home = tmp_path / "gui-home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("XDG_", "LITMAN_"))
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        LITMAN_REGISTRY_DIR=str(tmp_path / "registry"),
        LITMAN_NO_UPDATE_CHECK="1",
        PYTHONPATH=str(Path(litman.__file__).resolve().parents[1]),
    )
    lit = [sys.executable, "-c", "from litman.cli import main; main()"]
    first = subprocess.run(
        [*lit, "sync", "--library", str(a)],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log_path = tmp_path / "gui.log"
    with open(log_path, "w", encoding="utf-8") as log:
        gui = subprocess.Popen(
            [*lit, "gui", "--no-browser", "--port", str(port), "--library", str(a)],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            state = None
            deadline = time.monotonic() + 90
            while state != "ok" and time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/sync/status", timeout=5
                    ) as answer:
                        state = json.load(answer)["state"]
                except OSError:
                    pass
                time.sleep(0.3)
            assert state == "ok", log_path.read_text(encoding="utf-8")

            _write_paper(a, "at-exit", mtime=time.time() - 50)
            gui.send_signal(signal.SIGTERM)
            code = gui.wait(timeout=120)
        finally:
            if gui.poll() is None:
                gui.kill()
                gui.wait()
    output = log_path.read_text(encoding="utf-8")
    assert code == 128 + signal.SIGTERM, output
    assert "Syncing changes before exit" in output
    assert (cloud / "lib" / "papers" / "at-exit" / "metadata.yaml").is_file()


# ---------------------------------------------------------------------------
# The exit, a self-update, an encrypted rclone config, an owed stamp
# ---------------------------------------------------------------------------


def _running(tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch) -> SyncScheduler:
    """A scheduler whose thread is in the middle of a slow sync (20 s of upload)."""
    a, _b = _two_synced_machines(tmp_path, cloud)
    scheduler = _started(a)
    (a / "papers" / "p1" / "notes.md").write_bytes(b"x" * 400_000)
    monkeypatch.setenv("RCLONE_BWLIMIT", "20k")
    scheduler.request_sync()
    deadline = time.monotonic() + 30
    while scheduler._control is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert scheduler._control is not None
    return scheduler


def test_a_self_update_stops_a_running_sync_at_once(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update's helper waits for this process to end; on Windows rclone
    cannot be asked to stop, only killed, which the exit does at 60 s."""
    scheduler = _running(tmp_path, cloud, monkeypatch)
    thread = scheduler._thread
    assert thread is not None
    scheduler.self_update_pending = True

    started = time.monotonic()
    assert scheduler.final_sync() is False

    assert time.monotonic() - started < 15
    assert not thread.is_alive()


def test_a_ctrl_c_while_the_exit_waits_stops_the_running_sync(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running sync's rclone has a process group of its own, so the
    terminal's Ctrl+C never reached it. The wait was announced first."""
    scheduler = _running(tmp_path, cloud, monkeypatch)
    thread = scheduler._thread
    assert thread is not None

    class _CtrlC:
        """The thread, whose first join is broken off by a Ctrl+C."""

        pressed = False

        def is_alive(self) -> bool:
            return thread.is_alive()

        def join(self, timeout: float | None = None) -> None:
            if not self.pressed:
                self.pressed = True
                raise KeyboardInterrupt
            thread.join(timeout)

    scheduler._thread = _CtrlC()  # type: ignore[assignment]
    said: list[str] = []

    started = time.monotonic()
    assert scheduler.final_sync(on_start=lambda: said.append("waiting")) is False

    assert said == ["waiting"]
    assert time.monotonic() - started < 15
    assert not thread.is_alive()


def test_no_exit_sync_while_the_auto_sync_waits_to_retry_offline(
    tmp_path: Path, cloud: Path, counted: dict[str, int]
) -> None:
    """Offline at its last try: the exit would only wait a minute on a network
    that is not there. The next start syncs. (The reverse, online:
    test_the_exit_sync_takes_a_change_up.)"""
    a, _b = _two_synced_machines(tmp_path, cloud)
    scheduler = _started(a)
    real = scheduler.sync_fn

    def offline(*args: object, **kwargs: object) -> core_sync.TwoWayResult:
        raise SyncError("Failed to bisync: dial tcp: lookup www.googleapis.com: no such host")

    scheduler.sync_fn = offline
    scheduler.request_sync()
    deadline = time.monotonic() + 30
    while scheduler.status()["state"] != "offline" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert scheduler.status()["state"] == "offline"
    scheduler.sync_fn = real
    _write_paper(a, "while-offline", mtime=time.time() - 50)
    runs = counted["bisync"]

    assert scheduler.final_sync() is False
    assert counted["bisync"] == runs


def test_the_sync_on_leaving_a_library_runs_where_the_exit_can_stop_it(
    tmp_path: Path, cloud: Path
) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    state = SimpleNamespace(vault=a.resolve())
    scheduler = SyncScheduler(state)
    scheduler.step()
    assert scheduler.status()["state"] == "ok"
    _write_paper(a, "left-behind", mtime=time.time() - 50)
    real = scheduler.sync_fn
    seen: list[bool] = []

    def recording(vault: Path, target: str, **kwargs: Any) -> core_sync.TwoWayResult:
        seen.append(scheduler._control is kwargs["control"])
        return real(vault, target, **kwargs)

    scheduler.sync_fn = recording
    other = tmp_path / "other-library"
    other.mkdir()
    state.vault = other.resolve()  # the page switched libraries

    scheduler.step()

    assert seen == [True]
    assert scheduler._control is None
    assert (cloud / "lib" / "papers" / "left-behind" / "metadata.yaml").is_file()


def test_an_owed_stamp_is_not_written_over_one_that_moved_after_the_look(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B's look found the stamp unmoved and a stamp owed; A synced right
    after. B must sync A's change first, not write a stamp naming A's as seen."""
    ma, mb, clock = _machines(tmp_path, cloud)
    _settle_start(ma, mb)
    work_b = mb.scheduler._library.workdir  # type: ignore[union-attr]
    assert work_b is not None
    core_sync.write_stamp_record(work_b, replace(core_sync.read_stamp_record(work_b), owed=True))
    monkeypatch.setattr(
        core_sync,
        "check_remote_stamp",
        lambda work, target: core_sync.StampCheck(moved=False, owed=True),
    )
    _write_paper(ma.vault, "from-a", mtime=time.time() - 50)
    assert _sync(ma.vault).exit_code == 0
    from_a = core_sync.read_remote_stamp(_target(cloud))
    assert from_a is not None

    _stamp_check(mb, clock)

    assert (mb.vault / "papers" / "from-a" / "metadata.yaml").is_file()
    after = core_sync.read_remote_stamp(_target(cloud))
    assert after is not None and after.prev == from_a.id


#: An rclone config encrypted with the password "secret": it holds the
#: fake-cloud remote, but no rclone can read it without being told the password.
_ENCRYPTED_CONFIG = """# Encrypted rclone configuration File

RCLONE_ENCRYPT_V0:
2lQ5QeXOuBtBDk+Q6R9alx6LJyI+W5Xd83hUfDUBJgr4dqUa90hSEgYKYKZaHz+buedaTHGy08gbwrITkpeyLswYSQ==
"""


def test_with_an_encrypted_rclone_config_the_auto_sync_says_so_instead_of_waiting(
    tmp_path: Path, cloud: Path
) -> None:
    """rclone would ask for the config's password on the terminal, and the
    auto sync's thread would wait for an answer for good."""
    a, _b = _two_synced_machines(tmp_path, cloud)
    Path(os.environ["RCLONE_CONFIG"]).write_text(_ENCRYPTED_CONFIG, encoding="utf-8")
    scheduler = SyncScheduler(SimpleNamespace(vault=a.resolve()))
    scheduler.start()
    try:
        deadline = time.monotonic() + 60
        while scheduler.status()["state"] != "error" and time.monotonic() < deadline:
            time.sleep(0.1)
        status = scheduler.status()
    finally:
        scheduler.stop()
        assert scheduler._thread is not None
        scheduler._thread.join(30)
    assert status["state"] == "error", status
    # Said by rclone only when told not to ask (--ask-password=false).
    assert "not allowed to ask for password" in (status["message"] or "")


# ---------------------------------------------------------------------------
# The record of the last run, for `lit sync status`
# ---------------------------------------------------------------------------


def test_the_auto_sync_and_the_exit_sync_leave_their_own_record(
    tmp_path: Path, cloud: Path
) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    assert _last_run(a).trigger == "terminal"

    scheduler = _started(a)
    assert (_last_run(a).trigger, _last_run(a).result) == ("auto", "ok")
    _write_paper(a, "at-exit", mtime=time.time() - 50)
    assert scheduler.final_sync() is True

    assert (_last_run(a).trigger, _last_run(a).result) == ("exit", "ok")


def test_an_rclone_the_gui_cannot_use_is_recorded_for_lit_sync_status(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ma, _mb, _clock = _machines(tmp_path, cloud)
    assert _last_run(ma.vault).trigger == "terminal"
    too_old = (
        "rclone 1.66.0 is too old for two-way sync, which needs 1.74.2 or newer. "
        "Update it: `rclone selfupdate`."
    )

    def require() -> None:
        raise SyncError(too_old)

    monkeypatch.setattr(core_sync, "require_bisync_rclone", require)
    ma.step()

    assert (ma.status["state"], ma.status["reason"]) == ("error", "rclone")
    run = _last_run(ma.vault)
    assert (run.trigger, run.result, run.reason, run.error) == ("auto", "failed", "rclone", too_old)
