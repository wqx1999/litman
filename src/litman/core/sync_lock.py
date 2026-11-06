"""The locks that keep two syncs of one library apart, and GUI saves out of rclone's way.

Two lock files in the library's bisync workdir (one pair per library and
remote, :func:`litman.core.sync.sync_workdir`):

* ``sync.lock`` is held for a whole sync: ``lit sync`` (two-way, push and
  pull) and the GUI's auto sync. A second sync of the same library waits for
  it, or gives up.
* ``pass.lock`` is held by a sync only while rclone runs, and by the GUI while
  it saves. A save waits for rclone; a long git fetch after the file sync does
  not hold it up.

Both are locks the operating system holds on an open file: ``fcntl.flock`` on
POSIX, ``msvcrt.locking`` on Windows (on a byte far past the file's text). The
system drops a lock when its process ends, however it ends, so no lock is ever
left behind. Inside one process a lock is also taken in memory first: where
``flock`` is emulated by per-process record locks (some network filesystems),
two holders in one process would otherwise both get it.

The holder of ``sync.lock`` writes one line into it (pid, since, by), read only
to tell a waiting user who holds it.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from litman.core.dates import now_iso
from litman.exceptions import SyncLockError

#: Where the Windows lock sits: one byte this far into the file, past any
#: holder line, so the line stays readable while the lock is held.
_WINDOWS_LOCK_OFFSET = 1 << 30

#: What the system answers when another holder has the lock: ``flock`` says
#: EWOULDBLOCK (EAGAIN), ``msvcrt.locking`` EACCES. Any other error is the file
#: system refusing locks altogether (ENOLCK on some network mounts), which
#: waiting would never end.
_HELD_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})

#: Locks this process holds, by normalised path: the in-memory half.
_held: set[str] = set()
_held_guard = threading.Lock()


@dataclass(frozen=True)
class LockHolder:
    """Who holds ``sync.lock``, as its holder wrote it."""

    pid: int | None
    since: str | None
    by: str | None

    def describe(self) -> str:
        """``lit gui, since 14:02`` (the parts known), for a message."""
        parts = [self.by] if self.by else []
        when = _clock_time(self.since)
        if when is not None:
            parts.append(f"since {when}")
        return ", ".join(parts)


def _clock_time(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except ValueError:
        return None


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _os_lock(fd: int) -> None:
    """Take the system lock on ``fd`` without waiting; ``OSError`` when it is held."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """One of the two lock files: taken without waiting, or waited for a bounded time."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock if it is free; False when another holder has it.

        Raises:
            SyncLockError: the file system refused the lock itself.
        """
        if self._fd is not None:
            raise RuntimeError(f"{self.path} is already held by this lock object")
        key = _key(self.path)
        with _held_guard:
            if key in _held:
                return False
            _held.add(key)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            with _held_guard:
                _held.discard(key)
            raise
        try:
            _os_lock(fd)
        except OSError as err:
            os.close(fd)
            with _held_guard:
                _held.discard(key)
            if err.errno in _HELD_ERRNOS:
                return False
            raise SyncLockError(
                f"The file system under {self.path.parent} refuses file locks "
                f"({err.strerror or err}), so no sync can run. Set XDG_DATA_HOME "
                "to a folder on a local disk."
            ) from err
        self._fd = fd
        return True

    def acquire(
        self,
        wait: float,
        *,
        on_wait: Callable[[], None] | None = None,
        poll: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Take the lock, waiting up to ``wait`` seconds; False when it stayed held.

        ``on_wait`` is called once, when the lock is first found held.
        """
        if self.try_acquire():
            return True
        if on_wait is not None:
            on_wait()
        deadline = clock() + wait
        while clock() < deadline:
            sleep(min(poll, max(0.0, deadline - clock())))
            if self.try_acquire():
                return True
        return False

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _os_unlock(fd)
        except OSError:
            pass
        os.close(fd)
        with _held_guard:
            _held.discard(_key(self.path))

    def write_holder(self, by: str) -> None:
        """Record this process as the holder, for :func:`read_holder` (the lock must be held)."""
        if self._fd is None:
            return
        line = json.dumps({"pid": os.getpid(), "since": now_iso(), "by": by}) + "\n"
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, line.encode("utf-8"))
        except OSError:
            pass


def read_holder(path: Path) -> LockHolder | None:
    """The holder line of the lock file at ``path``; ``None`` when there is none to read."""
    try:
        with open(path, encoding="utf-8") as f:
            line = f.readline(4096)
        payload = json.loads(line)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    since = payload.get("since")
    by = payload.get("by")
    return LockHolder(
        pid=pid if isinstance(pid, int) else None,
        since=since if isinstance(since, str) else None,
        by=by if isinstance(by, str) else None,
    )


class SharedHold:
    """One lock, held for any number of overlapping holders in this process.

    The GUI's saves share ``pass.lock`` this way: the first save in takes it,
    the last one out lets it go, and saves never wait for each other.
    """

    def __init__(self, path: Path) -> None:
        self._lock = FileLock(path)
        self._count = 0
        self._guard = threading.Lock()

    def try_enter(self) -> bool:
        with self._guard:
            if self._count == 0 and not self._lock.try_acquire():
                return False
            self._count += 1
            return True

    def leave(self) -> None:
        with self._guard:
            self._count -= 1
            if self._count == 0:
                self._lock.release()
