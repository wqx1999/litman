"""Replacing a file or moving a folder, riding out a program that holds it on Windows.

Windows refuses to replace a file another process has open, and something often
does for a few milliseconds: a virus scanner or the indexer reading what we just
wrote. It says ``WinError 5`` or ``32``, the write was fine, and a moment later
the same call succeeds. A folder cannot be moved while a file in it is open, for
the same reason. Everything litman replaces or moves goes through here, so one
such moment cannot crash a command halfway through.
"""

from __future__ import annotations

import errno
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from litman.core.locking import rmtree

# ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION.
_HELD_BY_ANOTHER_PROGRAM = frozenset({5, 32})
_BUDGET_SECONDS = 2.0
_FIRST_WAIT_SECONDS = 0.01
_LONGEST_WAIT_SECONDS = 0.2

_Path = str | os.PathLike[str]


def _platform() -> str:
    # Read at call time, so a test fakes the platform here instead of
    # patching ``sys.platform`` (the whole interpreter's).
    return sys.platform


def refused_by_windows(err: OSError) -> bool:
    """Is this the refusal a program holding the file gives?

    Only as far as Windows says: code 5 is also what a permission or a sandbox
    gives, so nothing may claim from this alone that a program holds the file.
    """
    return (
        _platform() == "win32"
        and getattr(err, "winerror", None) in _HELD_BY_ANOTHER_PROGRAM
    )


def refusal_message(err: OSError) -> str:
    """The one line for a refusal that outlasted the wait."""
    # A replace names the file it would overwrite second, and that is the one
    # held. A folder move names a place that does not exist yet there.
    named = err.filename2 if err.filename2 and os.path.lexists(err.filename2) else err.filename
    where = os.fsdecode(named) if named else "a file"
    return (
        f"Windows refused access to {where}; another program may have it open. "
        "Close it and run the command again."
    )


def _ride_out(call: Callable[[_Path, _Path], None], src: _Path, dst: _Path) -> None:
    """``call(src, dst)``, tried again for about 2 seconds on Windows.

    Only for a ``PermissionError`` with one of the two codes above; anything else
    is raised at once. Still refused at the end of the budget, the last error is
    raised as it is.
    """
    if _platform() != "win32":
        call(src, dst)
        return
    deadline = time.monotonic() + _BUDGET_SECONDS
    wait = _FIRST_WAIT_SECONDS
    while True:
        try:
            call(src, dst)
            return
        except PermissionError as err:
            left = deadline - time.monotonic()
            if not refused_by_windows(err) or left <= 0:
                raise
        time.sleep(min(wait, left))
        wait = min(wait * 2, _LONGEST_WAIT_SECONDS)


def replace_file(src: _Path, dst: _Path) -> None:
    """``os.replace(src, dst)``, riding out a program holding the file.

    A read-only target says ``WinError 5`` as well, so a caller unlocks one
    first, as it always had to.
    """
    _ride_out(os.replace, src, dst)


def rename_path(src: _Path, dst: _Path) -> None:
    """``os.rename(src, dst)``, riding out a program holding a file in the folder."""
    _ride_out(os.rename, src, dst)


def move_dir(src: Path, dst: Path) -> None:
    """Move the folder ``src`` to ``dst``, copying only across drives.

    NOT ``shutil.move``, which copies after ANY failed rename and then deletes
    the source: a file held open in the folder leaves a full copy at ``dst`` and
    the source deleted up to that file. Any refusal other than a cross-device one
    must stay a failure with nothing touched. Windows maps
    ``ERROR_NOT_SAME_DEVICE`` to ``EXDEV`` too.

    Its delete would also trip over the read-only ``metadata.yaml`` the copy
    inherited from the vault (ADR-005), which ``locking.rmtree`` clears.
    """
    try:
        rename_path(src, dst)
        return
    except OSError as err:
        if err.errno != errno.EXDEV:
            raise
    try:
        shutil.copytree(src, dst, symlinks=True)
    except BaseException:
        rmtree(dst, ignore_errors=True)
        raise
    rmtree(src)
