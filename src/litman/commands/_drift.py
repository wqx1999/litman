"""Registry drift surfacing (M28).

litman's design premise is that users are lazy and forgetful — any drift
between data sources must be surfaced at the user's next interaction, not
deferred to a manual ``lit health-check`` they have to remember to run.
This module implements that principle for vault registry drift: a vault is
registered but its on-disk directory has been removed.

The hook fires in ``LitGroup.invoke`` (cli.py) before every non-trivial
subcommand. In a TTY it asks ``[Y/n]`` (default Y) and prunes on Y. In
non-TTY contexts (scripts / CI / agents) it prints a single stderr warning
and does not block. Saying N keeps the entry; the next ``lit *`` invocation
will prompt again — we deliberately do NOT add an ``acknowledged_missing``
persistent ignore flag, because that would train a lazy ``N`` reflex into
permanent drift (see ``feedback_surface_drift_eagerly.md`` for the
anti-pattern list).
"""

from __future__ import annotations

import sys
from typing import Callable

import click
from rich.console import Console

from litman.core.vault_registry import (
    VaultRegistryError,
    find_dangling,
    load_registry,
    remove_vault,
    save_registry,
)


def _default_tty_probe() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def check_and_prompt_registry_drift(
    stdin_is_tty: Callable[[], bool] | None = None,
) -> None:
    """Surface dangling vault registrations and offer one-Enter cleanup.

    Behavior:

    * Loads the registry. A corrupt registry is silently skipped (the next
      ``lit vault`` command will surface the parse error — not our job to
      duplicate that diagnostic here).
    * Computes dangling entries (path no longer exists).
    * If none, returns silently — the common path must add zero noise.
    * TTY → prints the list + ``Remove now? [Y/n]`` (default Y). On Y, drops
      each entry and saves the registry. On N, prints a one-line "kept; will
      ask again next time" note.
    * Non-TTY → emits a single stderr warning listing the names + the
      ``lit vault remove`` command to run, and returns. Never blocks
      automation on an interactive prompt.

    Args:
        stdin_is_tty: Indirection so tests can force either branch without
            faking stdin. Defaults to ``sys.stdin.isatty() and sys.stdout.isatty()``
            — both streams must be TTY before we prompt, so a piped stdout
            (``lit list | less``) does not get a drift question mid-pipe.
    """
    probe = stdin_is_tty or _default_tty_probe
    try:
        reg = load_registry()
    except VaultRegistryError:
        # Corrupt registry. Surfacing it here would double-warn the user
        # (they'll see the parse error when they hit a vault-using command);
        # prefer one clear diagnostic over two.
        return

    dangling = find_dangling(reg)
    if not dangling:
        return

    names = [v.name for v in dangling]
    paths = [v.path for v in dangling]

    # Build consoles INSIDE the function (not at module level) so pytest's
    # capsys fixture, which swaps sys.stderr/sys.stdout per-test, can capture
    # them. A module-level Console(stderr=True) would bind to the import-time
    # sys.stderr and bypass capsys.
    if not probe():
        # Non-interactive (script / CI / agent). One-line stderr warning,
        # no prompt, no auto-prune (mutating registry without consent in
        # automation is a foot-gun even with default Y).
        err = Console(stderr=True)
        joined = ", ".join(f"{n} ({p})" for n, p in zip(names, paths))
        err.print(
            f"[yellow]warning:[/] vault registry has {len(dangling)} dangling "
            f"registration(s): {joined}. Run "
            f"[bold]lit vault remove <name>[/] to clean up."
        )
        return

    # TTY path.
    console = Console()
    console.print()  # blank line before, separating from prior output
    console.print(
        f"[yellow]⚠[/]  Found {len(dangling)} dangling vault "
        f"registration(s) (path no longer exists):"
    )
    for entry in dangling:
        console.print(f"    [bold]{entry.name}[/] → {entry.path}")
    if click.confirm(
        "Remove these stale entries from the registry now?",
        default=True,
    ):
        current = reg
        for entry in dangling:
            current = remove_vault(current, entry.name)
        save_registry(current)
        console.print(
            f"[green]Removed {len(dangling)} dangling registration(s).[/]\n"
        )
    else:
        console.print(
            "[dim]Kept for now. You'll be reminded again next time.[/]\n"
        )
