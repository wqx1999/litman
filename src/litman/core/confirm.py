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

import sys

import click
from rich.console import Console

console = Console()


def _stdin_is_tty() -> bool:
    """Whether stdin is an interactive terminal.

    Isolated as a one-line seam so tests can simulate a real tty without
    monkeypatching ``sys.stdin`` itself (which ``click.testing.CliRunner``
    swaps wholesale during ``invoke``).
    """
    return sys.stdin.isatty()


def _confirm_destructive(warning_lines: list[str], *, yes: bool) -> bool:
    """Gate a data-losing operation behind a confirmation.

    Decision order:

    1. ``yes`` (``--yes`` / ``-y`` was passed) → return ``True`` silently;
       the caller proceeds without rendering the warning block.
    2. stdin is not a tty (pipe / redirect / CI) and ``--yes`` absent →
       print a stderr hint and raise ``click.Abort`` *without reading
       stdin* so a stray ``y\\n`` on a pipe cannot bypass the confirmation
       and the command exits non-zero.
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
        click.Abort: non-interactive environment without ``--yes``.
    """
    if yes:
        return True
    # Gate on isatty() BEFORE touching click.confirm. A non-tty stdin may
    # still carry stray bytes (`echo y | lit ...`, CI heredoc, accidental
    # upstream output); if we let click.confirm read those, a non-interactive
    # invocation with no --yes would silently proceed with a cascade delete.
    # The spec ("不读不存在的 stdin") forbids reading stdin at all here.
    if not _stdin_is_tty():
        click.echo(
            "Non-interactive environment — pass --yes to confirm.",
            err=True,
        )
        raise click.Abort()
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
