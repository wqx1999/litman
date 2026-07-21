"""Native terminal-window spawner backing the GUI agent button.

``spawn_terminal`` opens a new terminal window running ``argv`` with ``cwd``
as working directory, returning True on a successful fire-and-forget spawn
and False whenever this session cannot pop a window (headless Linux, no
known terminal emulator installed, or any spawn failure). It never raises —
the caller (``POST /api/agent/launch``) degrades to copy-the-command mode on
False, which is the normal outcome when ``lit gui`` serves from a remote box
and the browser sits on the user's laptop.

Platform dispatch mirrors ``core.viewer`` (fire-and-forget ``Popen`` with the
stdio triple detached). Each Linux emulator gets its own exec syntax — they
are not interchangeable (``gnome-terminal`` wants ``--``, ``xfce4-terminal``
wants a single command string, the rest take ``-e`` + argv).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# Probe order: the Debian alternatives symlink first (it points at whatever
# the user picked as their terminal), then the major desktop terminals.
_LINUX_TERMINALS: tuple[str, ...] = (
    "x-terminal-emulator",
    "gnome-terminal",
    "konsole",
    "xfce4-terminal",
    "xterm",
)


def _linux_terminal_argv(name: str, exe: str, argv: list[str]) -> list[str]:
    """The full spawn argv for one Linux terminal emulator."""
    if name == "gnome-terminal":
        return [exe, "--", *argv]
    if name == "xfce4-terminal":
        # Its -e takes ONE string, not trailing argv.
        return [exe, "-e", shlex.join(argv)]
    # x-terminal-emulator / konsole / xterm: -e followed by command + args.
    return [exe, "-e", *argv]


def _popen_detached(spawn_argv: list[str], cwd: Path) -> None:
    subprocess.Popen(
        spawn_argv,
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def _windows_terminal_argv(argv: list[str]) -> list[str]:
    """Wrap Windows command-script shims so Terminal can execute them.

    npm-installed CLIs commonly resolve to ``*.cmd``. Windows Terminal cannot
    use a batch file as the tab's executable directly, so hand it to ``cmd``.
    Native ``.exe`` commands remain direct.
    """
    if argv and Path(argv[0]).suffix.casefold() in {".bat", ".cmd"}:
        return ["cmd", "/K", *argv]
    return argv


def spawn_terminal(argv: list[str], cwd: Path) -> bool:
    """Open a native terminal window running ``argv`` in ``cwd``.

    Returns True when a window spawn was fired, False when this session
    cannot open one (the caller falls back to showing the command). Never
    raises.
    """
    try:
        if sys.platform == "darwin":
            # Terminal.app is always present. `do script` opens a new window;
            # the script string cd's first because Terminal has no cwd flag.
            script = f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}"
            escaped = script.replace("\\", "\\\\").replace('"', '\\"')
            _popen_detached(
                [
                    "osascript",
                    "-e",
                    f'tell application "Terminal" to do script "{escaped}"',
                ],
                cwd,
            )
            return True

        if sys.platform == "win32":
            wt = shutil.which("wt")
            if wt:
                _popen_detached(
                    [wt, "-d", str(cwd), *_windows_terminal_argv(argv)], cwd
                )
            else:
                # `start` opens a new console; /K keeps it open after exit.
                _popen_detached(
                    ["cmd", "/c", "start", "cmd", "/K", *argv], cwd
                )
            return True

        # Linux / BSD. Without a display no window can appear.
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        for name in _LINUX_TERMINALS:
            exe = shutil.which(name)
            if exe is None:
                continue
            _popen_detached(_linux_terminal_argv(name, exe, argv), cwd)
            return True
        return False
    except Exception:
        return False
