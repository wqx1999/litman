"""The sync locks: one holder at a time, never left behind by a process that died."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from litman.core import git as git_mod
from litman.core import sync as core_sync
from litman.core import terminal
from litman.core.sync_lock import FileLock, SharedHold, read_holder

_SRC = str(Path(__file__).resolve().parents[2] / "src")


def test_a_held_lock_is_refused_to_a_second_holder_in_this_process(tmp_path: Path) -> None:
    first, second = FileLock(tmp_path / "sync.lock"), FileLock(tmp_path / "sync.lock")
    assert first.try_acquire()
    assert not second.try_acquire()
    first.release()
    assert second.try_acquire()
    second.release()


def _hold_in_another_process(path: Path) -> subprocess.Popen[str]:
    """A python that takes the lock, says so, and then waits to be killed."""
    code = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {_SRC!r})
        from pathlib import Path
        from litman.core.sync_lock import FileLock
        lock = FileLock(Path({str(path)!r}))
        assert lock.try_acquire()
        lock.write_holder("lit sync")
        print("held", flush=True)
        time.sleep(600)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, encoding="utf-8"
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_another_process_holding_it_keeps_it_and_its_death_frees_it(tmp_path: Path) -> None:
    path = tmp_path / "sync.lock"
    proc = _hold_in_another_process(path)
    try:
        lock = FileLock(path)
        assert not lock.try_acquire()
        holder = read_holder(path)
        assert holder is not None
        assert holder.pid == proc.pid
        assert holder.by == "lit sync"
        assert holder.describe().startswith("lit sync, since ")
    finally:
        proc.kill()
        proc.wait()
    # Killed, not released: the system dropped the lock with the process.
    assert lock.try_acquire()
    lock.release()


def test_a_wait_tells_once_and_gives_up_at_its_end(tmp_path: Path) -> None:
    held = FileLock(tmp_path / "pass.lock")
    assert held.try_acquire()
    told: list[str] = []
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    waiter = FileLock(tmp_path / "pass.lock")
    got = waiter.acquire(
        3.0, on_wait=lambda: told.append("waiting"), sleep=sleep, clock=lambda: clock[0]
    )
    assert not got
    assert told == ["waiting"]
    assert clock[0] >= 3.0
    held.release()
    assert waiter.acquire(3.0, sleep=sleep, clock=lambda: clock[0])
    waiter.release()


def test_a_wait_ends_as_soon_as_the_holder_lets_go(tmp_path: Path) -> None:
    held = FileLock(tmp_path / "sync.lock")
    assert held.try_acquire()
    calls = [0]

    def sleep(seconds: float) -> None:
        calls[0] += 1
        if calls[0] == 2:
            held.release()

    waiter = FileLock(tmp_path / "sync.lock")
    assert waiter.acquire(90.0, sleep=sleep, clock=time.monotonic)
    assert calls[0] == 2
    waiter.release()


def test_overlapping_saves_share_the_lock_and_the_last_one_lets_it_go(tmp_path: Path) -> None:
    path = tmp_path / "pass.lock"
    saves = SharedHold(path)
    sync = FileLock(path)
    assert saves.try_enter()
    assert saves.try_enter()
    assert not sync.try_acquire()
    saves.leave()
    assert not sync.try_acquire()
    saves.leave()
    assert sync.try_acquire()
    # Now the sync holds it: a save cannot get in.
    assert not saves.try_enter()
    sync.release()


def test_only_a_windowless_windows_process_asks_for_no_console_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal, "_on_windows", lambda: True)
    monkeypatch.setattr(terminal, "_has_console", lambda: False)
    assert terminal.hidden_console_flags() == 0x08000000
    monkeypatch.setattr(terminal, "_has_console", lambda: True)
    assert terminal.hidden_console_flags() == 0
    monkeypatch.setattr(terminal, "_on_windows", lambda: False)
    monkeypatch.setattr(terminal, "_has_console", lambda: False)
    assert terminal.hidden_console_flags() == 0


class _Stop(Exception):
    pass


def test_rclone_and_git_are_started_with_the_no_window_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flags = 0x08000000
    seen: list[tuple[str, Any]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((cmd[0], kwargs.get("creationflags")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def popen(cmd: list[str], **kwargs: Any) -> Any:
        seen.append((cmd[0] + " (Popen)", kwargs.get("creationflags")))
        raise _Stop

    # The flag is decided where these modules ask for it; the subprocess
    # calls are recorded, never made (the flag is Windows-only).
    monkeypatch.setattr(git_mod, "hidden_console_flags", lambda: flags)
    monkeypatch.setattr(core_sync, "hidden_console_flags", lambda: flags)
    monkeypatch.setattr(core_sync, "_require_rclone", lambda: None)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)

    git_mod.run_git(["--version"])
    core_sync.run_rclone(["version"])
    with pytest.raises(_Stop):
        git_mod.run_git(["fetch"], progress=True)
    with pytest.raises(_Stop):
        core_sync.run_bisync(["bisync"])

    assert seen == [
        ("git", flags),
        ("rclone", flags),
        ("git (Popen)", flags),
        ("rclone (Popen)", flags),
    ]


def test_a_file_system_that_refuses_locks_is_an_error_not_a_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOLCK (a network mount without lock support) would otherwise read as
    "another sync is running" for good."""
    from litman.core import sync_lock
    from litman.exceptions import SyncLockError

    real_os_lock = sync_lock._os_lock

    def refuse(fd: int) -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(sync_lock, "_os_lock", refuse)
    lock = FileLock(tmp_path / "sync.lock")
    with pytest.raises(SyncLockError, match="refuses file locks"):
        lock.try_acquire()
    assert not lock.held
    assert core_sync.classify_sync_error(SyncLockError("x")) == ("needs-user", "lock")

    # The reverse: what the system says when another holder has it is "held".
    for code in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):

        def held(fd: int, code: int = code) -> None:
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(sync_lock, "_os_lock", held)
        assert FileLock(tmp_path / "sync.lock").try_acquire() is False
    # Each refusal let go of the in-memory half too.
    monkeypatch.setattr(sync_lock, "_os_lock", real_os_lock)
    again = FileLock(tmp_path / "sync.lock")
    assert again.try_acquire()
    again.release()


def test_in_the_gui_rclone_gets_no_terminal_to_ask_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """An encrypted rclone config asks for its password on the terminal; the
    GUI's rclone runs (rclone_never_asks) must fail instead of waiting."""
    seen: list[tuple[str, list[str], Any]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(("run", cmd, kwargs.get("stdin")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def popen(cmd: list[str], **kwargs: Any) -> Any:
        seen.append(("popen", cmd, kwargs.get("stdin")))
        raise _Stop

    monkeypatch.setattr(core_sync, "_require_rclone", lambda: None)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)

    with core_sync.rclone_never_asks():
        core_sync.run_rclone(["listremotes"])
        core_sync.run_rclone(["rcat", "x:y"], input="payload")
        with pytest.raises(_Stop):
            core_sync.run_bisync(["bisync"])
    # The reverse: `lit sync` in a terminal may still be asked.
    core_sync.run_rclone(["listremotes"])

    never = "--ask-password=false"
    assert seen == [
        ("run", ["rclone", never, "listremotes"], subprocess.DEVNULL),
        ("run", ["rclone", never, "rcat", "x:y"], None),
        ("popen", ["rclone", never, "bisync"], subprocess.DEVNULL),
        ("run", ["rclone", "listremotes"], None),
    ]


def test_rclone_s_version_and_remotes_are_asked_with_a_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits: dict[str, Any] = {}

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        limits[cmd[-1]] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="rclone v1.75.2\n", stderr="")

    monkeypatch.setattr(core_sync, "_require_rclone", lambda: None)
    monkeypatch.setattr(subprocess, "run", run)

    core_sync.rclone_version()
    core_sync.list_remotes()

    assert limits == {"version": core_sync._GATE_TIMEOUT, "listremotes": core_sync._GATE_TIMEOUT}
