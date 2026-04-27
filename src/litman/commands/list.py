"""``lit list`` — query papers in the vault, with filters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from litman.core.document import list_papers
from litman.core.library import find_vault

console = Console()

# Title column display cap. Beyond this, an ellipsis is appended.
_TITLE_MAX = 60


def _matches_filters(paper: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Return True if the paper matches every non-None filter."""
    # List-membership filters (exact value-in-list).
    for filter_name, field_name in (
        ("topic", "topics"),
        ("method", "methods"),
        ("project", "projects"),
        ("data", "data"),
    ):
        wanted = filters.get(filter_name)
        if wanted is not None and wanted not in (paper.get(field_name) or []):
            return False

    # Author: case-insensitive substring against any author entry.
    author_q = filters.get("author")
    if author_q is not None:
        haystack = paper.get("authors") or []
        if not any(author_q.lower() in (a or "").lower() for a in haystack):
            return False

    # Equality filters.
    for name in ("year", "type", "status", "priority"):
        wanted = filters.get(name)
        if wanted is not None and paper.get(name) != wanted:
            return False

    return True


@click.command("list")
@click.option("--year", type=int, help="Filter by exact publication year.")
@click.option(
    "--type", "type_filter",
    help="Filter by paper type (research/review/position/...).",
)
@click.option("--status", help="Filter by status (deep-read/skim/inbox/dropped).")
@click.option("--priority", help="Filter by priority (A/B/C).")
@click.option("--topic", help="Match papers whose topics list contains this value.")
@click.option("--method", help="Match papers whose methods list contains this value.")
@click.option("--project", help="Match papers whose projects list contains this value.")
@click.option(
    "--data", "data_filter",
    help="Match papers whose data list contains this value.",
)
@click.option(
    "--author",
    help="Case-insensitive substring match against any author entry.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
def list_cmd(
    year: int | None,
    type_filter: str | None,
    status: str | None,
    priority: str | None,
    topic: str | None,
    method: str | None,
    project: str | None,
    data_filter: str | None,
    author: str | None,
    library: Path | None,
) -> None:
    """List papers in the vault, optionally filtered.

    Filters are AND-combined. Multi-valued fields (topics/methods/projects/data)
    use exact list-membership; ``--author`` uses case-insensitive substring;
    other filters use exact equality.
    """
    vault = find_vault(library)
    all_papers = list_papers(vault)

    filters = {
        "year": year,
        "type": type_filter,
        "status": status,
        "priority": priority,
        "topic": topic,
        "method": method,
        "project": project,
        "data": data_filter,
        "author": author,
    }
    filtered = [p for p in all_papers if _matches_filters(p, filters)]

    if not filtered:
        if not all_papers:
            console.print(
                "[dim]No papers in vault yet. Run "
                "`lit add <pdf> --doi <doi>` to add one.[/]"
            )
        else:
            console.print(
                f"[dim]No papers match the given filters "
                f"({len(all_papers)} total in vault).[/]"
            )
        return

    table = Table(
        title=f"Papers ({len(filtered)} of {len(all_papers)})",
        show_lines=False,
    )
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("year", justify="right")
    table.add_column("type")
    table.add_column("status")
    table.add_column("pri", justify="center")
    table.add_column("title", style="dim")

    for p in filtered:
        title = (p.get("title") or "").strip()
        if len(title) > _TITLE_MAX:
            title = title[: _TITLE_MAX - 1] + "…"
        table.add_row(
            str(p.get("id", "?")),
            str(p.get("year", "?")),
            str(p.get("type", "?")),
            str(p.get("status", "?")),
            str(p.get("priority", "?")),
            title,
        )

    console.print(table)
