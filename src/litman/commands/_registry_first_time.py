"""Shared first-time registry setup prompt (used by ``lit init`` and
``lit vault add`` — whichever first creates the user-level registry)."""

from __future__ import annotations

import os
import sys

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.core.vault_registry import (
    REGISTRY_ENV_VAR,
    registry_path,
    registry_path_default,
)

console = Console()


def maybe_first_time_registry_prompt() -> None:
    """One-shot hint shown the first time litman is about to create the
    user-level registry. Triggers iff the registry file does not yet exist,
    ``$LITMAN_REGISTRY_DIR`` is unset, and stdin is a TTY (so CI / scripts /
    docker never hang). Aborting (answering no) gives the user a chance to
    set the env var and re-run.
    """
    if registry_path().is_file():
        return
    if os.environ.get(REGISTRY_ENV_VAR, "").strip():
        return
    if not sys.stdin.isatty():
        return

    default_path = registry_path_default()
    console.print(
        Panel.fit(
            f"litman is about to create its vault registry at:\n"
            f"  [bold]{escape(str(default_path))}[/]\n\n"
            f"[bold]💡 Tip — optional backup setup[/]\n"
            f"To redirect the registry to a cloud-synced directory "
            f"(GoogleDrive / Dropbox / Syncthing) and get free backup + "
            f"cross-machine sync, set:\n\n"
            f"  [bold cyan]export {REGISTRY_ENV_VAR}=\"/path/to/cloud/litman-config\"[/]\n\n"
            f"Add that line to your shell's startup file (e.g. "
            f"[dim]~/.bashrc[/], [dim]~/.zshrc[/], or "
            f"[dim]~/.config/fish/config.fish[/]) so it persists across "
            f"sessions. Then rerun your command.\n\n"
            f"[dim]Note: when syncing across machines, the registry stores "
            f"absolute vault paths — sync works cleanly only if each vault "
            f"is at the same path on every machine.[/]",
            title="First-time registry setup",
            border_style="cyan",
        )
    )
    if not click.confirm(
        "Continue with the default registry location?", default=True
    ):
        raise click.Abort()
