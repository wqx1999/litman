"""``lit vault`` command group (M8.2).

Five subcommands wired thinly over ``litman.core.vault_registry``:

- ``add NAME PATH`` — register an existing vault directory.
- ``use NAME`` — switch the active vault (the discovery-chain fallback).
- ``list`` — show every registered vault in a Rich table.
- ``info NAME`` — show one vault's details (path, paper count, size,
  provenance, active flag).
- ``remove NAME`` — unregister a vault. The vault directory on disk is
  NOT deleted; only the registry entry is removed.

The data-layer rules (name shape, uniqueness, at-most-one-active) live
in ``core/vault_registry.py`` and are enforced by the helpers we call
into; this module is the user-facing surface (Click options, Rich
rendering, friendly error wording).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from litman.core.sync import humanize_bytes
from litman.core.vault_registry import (
    REGISTRY_ENV_VAR,
    VaultEntry,
    add_vault,
    find_active,
    find_by_name,
    load_registry,
    registry_path,
    registry_path_default,
    remove_vault,
    save_registry,
    set_active,
)
from litman.exceptions import VaultRegistryError

console = Console()


@click.group("vault")
def vault_group() -> None:
    """Manage the set of vaults registered with litman.

    Multiple vaults can coexist on one machine: typically your own main
    vault plus any forks received from colleagues (each evolves
    independently from a snapshot). The registry is managed entirely
    through this command group; do not hand-edit the file.

    The registry file location resolves at command time:

    \b
      1. $LITMAN_REGISTRY_DIR/vaults.yaml when that env var is set — use
         it to redirect the registry into a cloud-synced directory.
      2. Otherwise the platform-default config dir:
           ~/.config/litman/                    on Linux
           ~/Library/Application Support/litman/ on macOS
           %APPDATA%\\litman\\                    on Windows

    At any moment exactly one vault is active: the vault every other
    lit command resolves to by default (after the explicit
    --library / $LIT_LIBRARY checks). lit vault use NAME
    switches the active vault.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vault_summary(vault_path: Path) -> dict[str, Any]:
    """Compute paper count + on-disk byte total for a registered vault.

    Paper count comes from ``INDEX.json`` (cheap, accurate). If the
    index is missing or unparseable we fall back to counting non-hidden
    subdirs of ``papers/``. Size is a recursive filesystem walk — O(N
    file stats), tolerable for ``lit vault info`` which runs on demand.
    """
    paper_count = 0
    index_path = vault_path / "INDEX.json"
    if index_path.is_file():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            paper_count = len(payload.get("papers") or [])
        except Exception:
            # Fall through to filesystem count.
            paper_count = -1
    if paper_count < 0:
        papers_dir = vault_path / "papers"
        if papers_dir.is_dir():
            paper_count = sum(
                1 for child in papers_dir.iterdir() if child.is_dir()
            )
        else:
            paper_count = 0

    total_bytes = 0
    for p in vault_path.rglob("*"):
        if not p.is_file():
            continue
        try:
            total_bytes += p.stat().st_size
        except OSError:
            continue

    return {"papers": paper_count, "bytes": total_bytes}


def _provenance_label(entry: VaultEntry) -> str:
    """Render an entry's provenance as a single short string."""
    if entry.imported_from is None and entry.imported_at is None:
        return "(local)"
    parts: list[str] = []
    if entry.imported_from:
        parts.append(entry.imported_from)
    if entry.imported_at:
        parts.append(entry.imported_at)
    return ", ".join(parts)


def _maybe_first_time_registry_prompt() -> None:
    """One-shot hint shown the first time ``lit vault add`` creates the registry.

    Triggers iff:
    * The registry file does not yet exist on disk (registry is about to
      be created by this very command).
    * ``$LITMAN_REGISTRY_DIR`` is NOT already set — if the user has
      explicitly redirected, they don't need the hint.
    * stdin is a TTY — non-TTY (CI / scripts / docker init) silently
      defaults so automation doesn't hang.

    Prints a shell-agnostic hint about pointing ``$LITMAN_REGISTRY_DIR``
    at a cloud-synced directory for backup, then asks for confirmation.
    Aborting here gives the user a chance to set the env var and re-run.
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
            f"sessions. Then rerun `lit vault add`.\n\n"
            f"[dim]Note: when syncing across machines, the registry stores "
            f"absolute vault paths — sync works cleanly only if each vault "
            f"is at the same path on every machine.[/]",
            title="First-time registry setup",
            border_style="cyan",
        )
    )
    if not click.confirm(
        "Continue with the default registry location?",
        default=True,
    ):
        raise click.Abort()


# ---------------------------------------------------------------------------
# lit vault add
# ---------------------------------------------------------------------------


@vault_group.command("add")
@click.argument("name")
@click.argument(
    "vault_path",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--import-from",
    "imported_from",
    default=None,
    help=(
        "Free-form provenance label for a vault wangq received from "
        "elsewhere (e.g. 'Zhang via USB drop'). When given without "
        "--import-at, today's date is auto-filled."
    ),
)
@click.option(
    "--import-at",
    "imported_at",
    default=None,
    help=(
        "ISO 8601 date the vault was imported (default: today when "
        "--import-from is given, omitted otherwise)."
    ),
)
@click.option(
    "--use",
    "set_active_flag",
    is_flag=True,
    default=False,
    help=(
        "Switch the active vault to this new entry immediately after "
        "registering. Default is to leave the existing active alone "
        "(unless this is the first vault, in which case it becomes "
        "active automatically)."
    ),
)
def vault_add_cmd(
    name: str,
    vault_path: Path,
    imported_from: str | None,
    imported_at: str | None,
    set_active_flag: bool,
) -> None:
    """Register an existing vault directory under NAME.

    The directory must already exist AND contain a lit-config.yaml
    (i.e. it must already be a litman vault, created by lit init or
    obtained as a snapshot). lit vault add does NOT create a new
    vault; use lit init for that.
    """
    if imported_from is not None and imported_at is None:
        imported_at = date.today().isoformat()

    _maybe_first_time_registry_prompt()

    registry = load_registry()
    updated = add_vault(
        registry,
        name,
        vault_path,
        imported_from=imported_from,
        imported_at=imported_at,
        set_active=set_active_flag,
    )
    save_registry(updated)

    new_entry = find_by_name(updated, name)
    assert new_entry is not None  # just added, must exist
    active_str = "active" if new_entry.is_active else "not active"
    provenance = _provenance_label(new_entry)
    console.print(
        Panel.fit(
            f"[bold green]Registered:[/] {escape(name)} ({active_str})\n"
            f"[bold]Path:[/] {escape(new_entry.path)}\n"
            f"[bold]Provenance:[/] {escape(provenance)}\n\n"
            f"[dim]`lit vault list` to view all vaults.[/]",
            title="lit vault add",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# lit vault use
# ---------------------------------------------------------------------------


@vault_group.command("use")
@click.argument("name")
def vault_use_cmd(name: str) -> None:
    """Switch the active vault to NAME.

    Subsequent lit commands without an explicit --library /
    --vault / $LIT_LIBRARY will resolve to this vault.
    """
    registry = load_registry()
    updated = set_active(registry, name)
    save_registry(updated)

    entry = find_by_name(updated, name)
    assert entry is not None
    console.print(
        Panel.fit(
            f"[bold green]Active vault:[/] {escape(name)}\n"
            f"[bold]Path:[/] {escape(entry.path)}\n\n"
            f"[dim]`lit list`, `lit show <id>`, etc. now resolve to this vault.[/]",
            title="lit vault use",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# lit vault list
# ---------------------------------------------------------------------------


@vault_group.command("list")
def vault_list_cmd() -> None:
    """Show every registered vault.

    Each row shows: name, active marker (✓ for the active vault), path,
    paper count (from INDEX.json), and provenance. Paths are folded if
    they exceed the column width; copy from a wider terminal if needed.
    """
    registry = load_registry()
    if not registry.vaults:
        console.print(
            "[yellow]No vaults registered.[/] "
            "Run `lit vault add <name> <path>` to register one."
        )
        return

    table = Table(
        title=f"Registered vaults ({len(registry.vaults)})",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Name", style="bold")
    table.add_column("Active", justify="center")
    table.add_column("Path", overflow="fold")
    table.add_column("Papers", justify="right")
    table.add_column("Provenance", overflow="fold")

    for entry in registry.vaults:
        active_marker = "[bold green]✓[/]" if entry.is_active else ""
        vault_path = Path(entry.path)
        if vault_path.is_dir():
            paper_count = str(_vault_summary(vault_path)["papers"])
        else:
            paper_count = "[red]?[/]"
        table.add_row(
            escape(entry.name),
            active_marker,
            escape(entry.path),
            paper_count,
            escape(_provenance_label(entry)),
        )

    console.print(table)
    active = find_active(registry)
    if active is None:
        console.print(
            "[yellow]No active vault.[/] "
            "Run `lit vault use <name>` to set one."
        )


# ---------------------------------------------------------------------------
# lit vault info
# ---------------------------------------------------------------------------


@vault_group.command("info")
@click.argument("name")
def vault_info_cmd(name: str) -> None:
    """Show a single vault's details (path, paper count, on-disk size,
    provenance, active flag).

    Size is computed by walking the vault directory; for very large
    vaults this can take a couple seconds.
    """
    registry = load_registry()
    entry = find_by_name(registry, name)
    if entry is None:
        names = ", ".join(v.name for v in registry.vaults) or "(none)"
        raise VaultRegistryError(
            f"No vault named {name!r} in the registry. Available: {names}."
        )

    vault_path = Path(entry.path)
    if not vault_path.is_dir():
        console.print(
            Panel.fit(
                f"[bold red]Vault directory missing:[/] {escape(entry.path)}\n"
                f"[bold]Name:[/] {escape(entry.name)}\n"
                f"[bold]Active:[/] {'yes' if entry.is_active else 'no'}\n"
                f"[bold]Provenance:[/] {escape(_provenance_label(entry))}\n\n"
                f"[dim]Fix the path (restore from backup, re-mount, etc.) "
                "or `lit vault remove` and re-add to recover.[/]",
                title=f"lit vault info — {name}",
                border_style="red",
            )
        )
        return

    summary = _vault_summary(vault_path)
    body = (
        f"[bold]Path:[/]        {escape(entry.path)}\n"
        f"[bold]Active:[/]      {'yes' if entry.is_active else 'no'}\n"
        f"[bold]Papers:[/]      {summary['papers']}\n"
        f"[bold]Total size:[/]  {humanize_bytes(summary['bytes'])}\n"
        f"[bold]Provenance:[/]  {escape(_provenance_label(entry))}"
    )
    if entry.imported_at:
        body += f"\n[bold]Imported at:[/] {escape(entry.imported_at)}"

    console.print(
        Panel.fit(
            body,
            title=f"lit vault info — {name}",
            border_style="cyan" if entry.is_active else "white",
        )
    )


# ---------------------------------------------------------------------------
# lit vault remove
# ---------------------------------------------------------------------------


@vault_group.command("remove")
@click.argument("name")
@click.option(
    "--yes", "-y",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the y/N confirmation prompt.",
)
def vault_remove_cmd(name: str, yes: bool) -> None:
    """Unregister NAME from the registry.

    The vault directory itself is NOT deleted — only the registry entry
    is removed. To delete the directory too, rm -rf <path> after the
    unregister. Removing the active vault leaves the registry with no
    active vault; pick the next one with lit vault use <other>.
    """
    registry = load_registry()
    entry = find_by_name(registry, name)
    if entry is None:
        # Trigger the same error our data layer would.
        remove_vault(registry, name)
        return  # unreachable — remove_vault raised; here for type-checkers

    if not yes:
        msg = f"Unregister vault {name!r} (path {entry.path} unchanged)?"
        if entry.is_active:
            msg += " This is the ACTIVE vault — you'll need `lit vault use` after."
        click.confirm(msg, abort=True, default=False)

    updated = remove_vault(registry, name)
    save_registry(updated)

    note = ""
    if entry.is_active:
        note = "\n\n[yellow]The active vault was removed. Run `lit vault use <name>` to pick the next one.[/]"
    console.print(
        Panel.fit(
            f"[bold green]Unregistered:[/] {escape(name)}\n"
            f"[bold]Path (unchanged on disk):[/] {escape(entry.path)}"
            f"{note}",
            title="lit vault remove",
            border_style="green",
        )
    )
