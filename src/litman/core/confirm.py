"""Shared destructive-operation confirmation helper (M15).

``lit taxonomy rm`` / ``lit taxonomy merge`` / ``lit project rm`` all share
the same cascade-with-confirm UX: render a warning block describing what will
be destroyed, then either honor an explicit ``--yes``, abort cleanly in a
non-interactive environment, or prompt the user interactively.

This logic lives here (not duplicated in each command module, and not
cross-imported between command modules) so the three call sites stay byte
identical and a future change to the non-tty policy is made in one place.
"""

from __future__ import annotations

import io
import sys
from typing import Any

import click
from rich.console import Console

console = Console()


def _on_windows() -> bool:
    """Seam: tests take the Windows branch of :func:`is_interactive` with it."""
    return sys.platform == "win32"


def _is_console(fd: int) -> bool:
    """Whether ``fd`` is a Windows console; False on any failure."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        mode = ctypes.c_ulong()
        return bool(
            ctypes.windll.kernel32.GetConsoleMode(
                ctypes.c_void_p(handle), ctypes.byref(mode)
            )
        )
    except Exception:
        return False


def is_interactive(stream: Any) -> bool:
    """Whether ``stream`` is a terminal a person is at.

    Windows reports any character device as a tty, the NUL device too, and NUL
    is the stdin an agent's commands often get. Only a console handle passes
    ``GetConsoleMode``, so on Windows that is asked as well.
    """
    try:
        if not stream.isatty():
            return False
    except Exception:  # None under the litw launcher, or a closed stream
        return False
    if not _on_windows():
        return True
    try:
        fd = stream.fileno()
    except (AttributeError, io.UnsupportedOperation):
        # No OS handle behind it (a test's stand-in): its own answer stands.
        return True
    except Exception:
        return False
    return _is_console(fd)


def _stdin_is_tty() -> bool:
    """Whether stdin is an interactive terminal.

    Isolated as a one-line seam so tests can simulate a real tty without
    monkeypatching ``sys.stdin`` itself (which ``click.testing.CliRunner``
    swaps wholesale during ``invoke``).
    """
    return is_interactive(sys.stdin)


def refuse_without_terminal(how: str) -> None:
    """Stop before a prompt that nobody can answer, without reading stdin.

    ``how`` names the way out, e.g. ``"pass --yes to confirm"``. Exits 1 with
    that one line and no ``Aborted!`` after it.
    """
    if _stdin_is_tty():
        return
    click.echo(f"Non-interactive environment — {how}.", err=True)
    raise click.exceptions.Exit(1)


def _confirm_destructive(warning_lines: list[str], *, yes: bool) -> bool:
    """Gate a data-losing operation behind a confirmation.

    Decision order:

    1. ``yes`` (``--yes`` / ``-y`` was passed) → return ``True`` silently;
       the caller proceeds without rendering the warning block.
    2. stdin is not a tty (pipe / redirect / CI) and ``--yes`` absent →
       print a stderr hint and exit 1 *without reading stdin* so a stray
       ``y\\n`` on a pipe cannot bypass the confirmation.
    3. Interactive tty → render the warning block, then a default-No
       ``Continue?`` prompt. Returns the user's choice.

    Args:
        warning_lines: Pre-formatted lines (Rich markup allowed) describing
            the destruction. Rendered verbatim before the prompt.
        yes: Whether ``--yes`` / ``-y`` was supplied.

    Returns:
        ``True`` if the operation should proceed, ``False`` if the user
        declined at the interactive prompt.

    Raises:
        click.exceptions.Exit: non-interactive environment without ``--yes``.
    """
    if yes:
        return True
    # Gate on the terminal check BEFORE touching click.confirm. A non-tty stdin may
    # still carry stray bytes (`echo y | lit ...`, CI heredoc, accidental
    # upstream output); if we let click.confirm read those, a non-interactive
    # invocation with no --yes would silently proceed with a cascade delete.
    # The spec ("不读不存在的 stdin") forbids reading stdin at all here.
    refuse_without_terminal("pass --yes to confirm")
    for line in warning_lines:
        console.print(line)
    try:
        return click.confirm("Continue?", default=False)
    except (click.Abort, EOFError):
        # Secondary safety net: an interactive tty that EOFs mid-prompt
        # (Ctrl-D, terminal closed). click.confirm raises Abort on EOF;
        # surface the actionable hint and re-raise so the command exits
        # non-zero instead of hanging or silently proceeding.
        click.echo(
            "Non-interactive environment — pass --yes to confirm.",
            err=True,
        )
        raise click.Abort()
