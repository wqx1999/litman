"""``lit vault`` command group (M8.2).

Six subcommands wired thinly over ``litman.core.vault_registry``:

- ``add NAME PATH`` — register an existing vault directory.
- ``use NAME`` — switch the active vault (the discovery-chain fallback).
- ``set-path NAME NEW_PATH`` — re-point a registered vault at its new
  directory after the folder was moved on disk (active flag + provenance
  unchanged).
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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from litman.commands._options import format_option
from litman.commands._registry_first_time import maybe_first_time_registry_prompt
from litman.core.sync import humanize_bytes
from litman.core.vault_registry import (
    VaultEntry,
    add_vault,
    apply_vault_set_path,
    apply_vault_use,
    find_active,
    find_by_name,
    load_registry,
    mark_health_checked,
    remove_vault,
    save_registry,
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


def _vault_summary(vault_path: Path, *, with_size: bool = True) -> dict[str, Any]:
    """Compute paper count (+ optional on-disk byte total) for a vault.

    Paper count comes from ``INDEX.json`` (cheap, accurate). If the
    index is missing or unparseable we fall back to counting non-hidden
    subdirs of ``papers/``. The byte total is a recursive filesystem walk
    — O(N file stats) — so it is computed only when ``with_size`` is set:
    ``lit vault list`` needs the count alone and must not pay a full walk
    for every registered vault, while ``lit vault info`` runs on demand.
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
    if with_size:
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

    maybe_first_time_registry_prompt()

    registry = load_registry()
    updated = add_vault(
        registry,
        name,
        vault_path,
        imported_from=imported_from,
        imported_at=imported_at,
        set_active=set_active_flag,
    )
    # Mirror `lit init` (review F17): the dir was validated as a real vault
    # above, so start its health-check staleness clock now. Otherwise
    # last_health_check_at=None reads as "never checked == stale" and the
    # post-command nudge fires on the very first command after registering.
    updated = mark_health_checked(
        updated,
        name,
        datetime.now(timezone.utc).isoformat(),
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
    entry = apply_vault_use(name)
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
# lit vault set-path
# ---------------------------------------------------------------------------


@vault_group.command("set-path")
@click.argument("name")
@click.argument(
    "new_path",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
)
def vault_set_path_cmd(name: str, new_path: Path) -> None:
    """Re-point registered vault NAME at NEW_PATH.

    For when the vault directory was moved or renamed on disk and the registry's
    stored path went stale. NEW_PATH must already be a litman vault (an existing
    directory containing a lit-config.yaml). This does NOT move any files — move
    the folder yourself first, then run this to point the registry at its new
    home. The active flag and provenance are left unchanged.
    """
    entry = apply_vault_set_path(name, new_path)
    active_str = "active" if entry.is_active else "not active"
    console.print(
        Panel.fit(
            f"[bold green]Repointed:[/] {escape(name)} → {escape(entry.path)}\n"
            f"[bold]Active:[/] {active_str}\n\n"
            f"[dim]Active flag and provenance are unchanged.[/]",
            title="lit vault set-path",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# lit vault list
# ---------------------------------------------------------------------------


def _vault_row(entry: VaultEntry) -> dict[str, Any]:
    """One registered vault as a JSON row, keyed the way vaults.yaml is.

    ``papers`` is null — not 0 — when the vault's folder is not on this
    machine: the table's red "?" means "cannot know", and 0 would read as
    "an empty vault". The table's single Provenance column is the join of
    the registry's two provenance fields; JSON keeps them apart.
    """
    vault_path = Path(entry.path)
    papers = (
        _vault_summary(vault_path, with_size=False)["papers"]
        if vault_path.is_dir()
        else None
    )
    return {
        "name": entry.name,
        "path": entry.path,
        "is_active": entry.is_active,
        "papers": papers,
        "imported_from": entry.imported_from,
        "imported_at": entry.imported_at,
    }


@vault_group.command("list")
@format_option
def vault_list_cmd(output_format: str) -> None:
    """Show every registered vault.

    Each row shows: name, active marker (✓ for the active vault), path,
    paper count (from INDEX.json), and provenance. Paths are folded if
    they exceed the column width; copy from a wider terminal if needed.
    """
    registry = load_registry()

    if output_format == "json":
        # Before the empty-registry message: no vaults is `[]`, not prose.
        click.echo(
            json.dumps(
                [_vault_row(e) for e in registry.vaults],
                ensure_ascii=False,
            )
        )
        return

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
            paper_count = str(_vault_summary(vault_path, with_size=False)["papers"])
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
