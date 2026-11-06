"""Native terminal-window spawner backing the GUI agent button.

``spawn_terminal`` opens a new terminal window running ``argv`` with ``cwd``
as working directory, returning True on a successful fire-and-forget spawn
and False whenever this session cannot pop a window (headless Linux, no
known terminal emulator installed, or any spawn failure). It never raises —
the caller (``POST /api/agent/launch``) degrades to copy-the-command mode on
False, which is the normal outcome when ``lit gui`` serves from a remote box
and the browser sits on the user's laptop.

Spawns are fire-and-forget ``Popen`` calls with the stdio triple detached —
unlike ``core.viewer``, which waits a moment to hear its opener fail. Each
Linux emulator gets its own exec syntax — they are not interchangeable
(``gnome-terminal`` wants ``--``, ``xfce4-terminal`` wants a single command
string, the rest take ``-e`` + argv).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
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


#: Windows ``CREATE_NO_WINDOW``: the child gets a console of its own, with no window.
_CREATE_NO_WINDOW = 0x08000000


def _on_windows() -> bool:
    """Seam: tests take the Windows branch of :func:`hidden_console_flags` with it."""
    return sys.platform == "win32"


def _has_console() -> bool:
    """Whether this Windows process has a console; True when that cannot be told."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True


def hidden_console_flags() -> int:
    """``creationflags`` for a console program litman runs in the background (rclone, git).

    A process with no console (``litw``, the desktop shortcut's launcher) gives
    every such child a new console window, which would flash on screen at each
    auto sync. ``CREATE_NO_WINDOW`` gives the child a console with no window;
    its output still reaches the pipes litman reads. A process with a console
    (a terminal ``lit``) shares it with the child as before: 0. Off Windows: 0.
    """
    if not _on_windows() or _has_console():
        return 0
    return _CREATE_NO_WINDOW


def _linux_terminal_argv(name: str, exe: str, argv: list[str]) -> list[str]:
    """The full spawn argv for one Linux terminal emulator."""
    if name == "gnome-terminal":
        return [exe, "--", *argv]
    if name == "xfce4-terminal":
        # Its -e takes ONE string, not trailing argv.
        return [exe, "-e", shlex.join(argv)]
    # x-terminal-emulator / konsole / xterm: -e followed by command + args.
    return [exe, "-e", *argv]


def _popen_detached(
    spawn_argv: list[str], cwd: Path, env: Mapping[str, str] | None
) -> None:
    subprocess.Popen(
        spawn_argv,
        cwd=str(cwd),
        env=env,
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


def spawn_terminal(
    argv: list[str], cwd: Path, env: Mapping[str, str] | None = None
) -> bool:
    """Open a native terminal window running ``argv`` in ``cwd``.

    ``env`` is the environment of the process that opens the window; None
    inherits this one's. Returns True when a window spawn was fired, False when this session
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
                env,
            )
            return True

        if sys.platform == "win32":
            wt = shutil.which("wt")
            if wt:
                _popen_detached(
                    [wt, "-d", str(cwd), *_windows_terminal_argv(argv)], cwd, env
                )
            else:
                # `start` opens a new console; /K keeps it open after exit.
                _popen_detached(
                    ["cmd", "/c", "start", "cmd", "/K", *argv], cwd, env
                )
            return True

        # Linux / BSD. Without a display no window can appear.
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        for name in _LINUX_TERMINALS:
            exe = shutil.which(name)
            if exe is None:
                continue
            _popen_detached(_linux_terminal_argv(name, exe, argv), cwd, env)
            return True
        return False
    except Exception:
        return False
