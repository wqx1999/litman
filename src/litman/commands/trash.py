"""``lit trash`` — manage the recoverable-delete bin.

Three subcommands:

* ``list``    — enumerate trash entries (id, deleted_at, cascade flag, title)
* ``restore`` — move an entry back to ``papers/<id>/`` and refresh INDEX/views
* ``empty``   — permanently delete every entry (with y/N confirmation)

Trash storage layout and atomicity rules live in :mod:`litman.core.trash`.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from litman.core.document import list_papers
from litman.core.library import find_vault
from litman.core.trash import (
    empty_trash,
    list_trash,
    resolve_trash_entry,
    restore_from_trash,
)
from litman.core.views import rebuild_views, write_index
from litman.exceptions import TrashError

console = Console()


@click.group("trash")
def trash_group() -> None:
    """Manage the recoverable-delete bin under ``<vault>/.trash/``."""


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@trash_group.command("list")
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
def trash_list_cmd(library: Path | None) -> None:
    """Show trash entries, newest first."""
    vault = find_vault(library)
    entries = list_trash(vault)
    if not entries:
        console.print("[dim](trash is empty)[/]")
        return

    table = Table(
        title=f"Trash ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("paper id")
    table.add_column("deleted_at")
    table.add_column("cascade", justify="center")
    table.add_column("title", overflow="fold")
    table.add_column("entry_name", style="dim")
    for e in entries:
        title = e.title if e.title else "[dim]—[/]"
        cascade_mark = "✓" if e.cascade_was_used else ""
        table.add_row(
            escape(e.paper_id),
            escape(e.deleted_at),
            cascade_mark,
            escape(title) if e.title else "[dim]—[/]",
            escape(e.entry_name),
        )
    console.print(table)
    console.print(
        "[dim]Restore via `lit trash restore <paper_id>` "
        "(or pass full entry_name to disambiguate).[/]"
    )


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


@trash_group.command("restore")
@click.argument("paper_id_or_entry")
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
)
def trash_restore_cmd(paper_id_or_entry: str, library: Path | None) -> None:
    """Restore a trashed paper to ``papers/<id>/``.

    Pass either the paper id (must be unambiguous) or the full entry name
    (``<id>-<UTC-timestamp>``). Refreshes INDEX.json and views/.
    """
    vault = find_vault(library)
    entry = resolve_trash_entry(vault, paper_id_or_entry)

    restored_path = restore_from_trash(vault, entry)

    # Refresh INDEX and views from the post-restore paper list.
    fresh_papers = list_papers(vault)
    write_index(vault, fresh_papers)
    rebuild_views(vault, fresh_papers)

    console.print(
        f"[bold green]✓ Restored[/] {escape(entry.paper_id)} "
        f"[dim]→ papers/{escape(entry.paper_id)}/[/]"
    )
    if entry.cascade_was_used:
        console.print(
            "  [yellow]Note:[/] this paper was removed with --cascade. "
            "Other papers' ref fields and notes wikilinks were modified at "
            "the time and were [bold]not[/] reverted by this restore."
        )
    console.print("[dim]INDEX.json + views/ refreshed.[/]")


# ---------------------------------------------------------------------------
# empty
# ---------------------------------------------------------------------------


@trash_group.command("empty")
@click.option(
    "--yes",
    "-y",
    "skip_confirm",
    is_flag=True,
    default=False,
    help="Skip the y/N confirmation prompt.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
)
def trash_empty_cmd(skip_confirm: bool, library: Path | None) -> None:
    """Permanently delete every trash entry."""
    vault = find_vault(library)
    entries = list_trash(vault)
    if not entries:
        console.print("[dim](trash is already empty)[/]")
        return

    if not skip_confirm:
        console.print(
            f"[bold yellow]About to permanently delete[/] "
            f"{len(entries)} trash entr{'y' if len(entries) == 1 else 'ies'}:"
        )
        for e in entries[:10]:
            console.print(
                f"  - {escape(e.paper_id)} [dim]({escape(e.deleted_at)})[/]"
            )
        if len(entries) > 10:
            console.print(f"  ... and {len(entries) - 10} more")
        console.print("[bold red]Not recoverable.[/]")
        if not click.confirm("Continue?", default=False):
            console.print("[dim]Aborted. Trash unchanged.[/]")
            return

    n = empty_trash(vault)
    console.print(
        f"[bold green]✓ Emptied[/] trash "
        f"[dim]({n} entr{'y' if n == 1 else 'ies'} removed)[/]"
    )
