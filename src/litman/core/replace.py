"""Replacing a file, riding out a program that holds it for a moment on Windows.

Windows refuses to replace a file another process has open, and something often
does for a few milliseconds: a virus scanner or the indexer reading what we just
wrote. It says ``WinError 5`` or ``32``, the write was fine, and a moment later
the same call succeeds. Every file litman replaces goes through here, so one
such moment cannot crash a command halfway through.
"""

from __future__ import annotations

import os
import sys
import time

# ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION.
_HELD_BY_ANOTHER_PROGRAM = frozenset({5, 32})
_BUDGET_SECONDS = 2.0
_FIRST_WAIT_SECONDS = 0.01
_LONGEST_WAIT_SECONDS = 0.2


def _platform() -> str:
    # Read at call time, so a test fakes the platform here instead of
    # patching ``sys.platform`` (the whole interpreter's).
    return sys.platform


def replace_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """``os.replace(src, dst)``, tried again for about 2 seconds on Windows.

    Only for a ``PermissionError`` with one of the two codes above; anything else
    is raised at once. Still refused at the end of the budget, the last error is
    raised as it is. A read-only target says ``WinError 5`` as well, so a caller
    unlocks one first, as it always had to.
    """
    if _platform() != "win32":
        os.replace(src, dst)
        return
    deadline = time.monotonic() + _BUDGET_SECONDS
    wait = _FIRST_WAIT_SECONDS
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError as err:
            left = deadline - time.monotonic()
            winerror = getattr(err, "winerror", None)
            if winerror not in _HELD_BY_ANOTHER_PROGRAM or left <= 0:
                raise
        time.sleep(min(wait, left))
        wait = min(wait * 2, _LONGEST_WAIT_SECONDS)
