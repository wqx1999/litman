"""``lit refresh-views`` — rebuild INDEX.md and views/by-*/ from metadata."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from litman.core.document import list_papers
from litman.core.library import find_vault
from litman.core.views import rebuild_views, write_index

console = Console()


@click.command("refresh-views")
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
def refresh_views_cmd(library: Path | None) -> None:
    """Rebuild INDEX.md and views/by-*/ symlink hubs from papers/*/metadata.yaml.

    The metadata files are the single source of truth. INDEX.md and the
    by-* directories are wiped and rewritten on each invocation, so any
    paper or tag value no longer present in the metadata disappears
    cleanly from the derived views.
    """
    vault = find_vault(library)
    papers = list_papers(vault)

    write_index(vault, papers)
    counts = rebuild_views(vault, papers)

    n = len(papers)
    console.print(
        f"[green]✓[/] INDEX.md updated ({n} paper{'s' if n != 1 else ''})"
    )
    for view_name, k in counts.items():
        console.print(f"  views/{view_name}: {k} symlink{'s' if k != 1 else ''}")
    console.print(f"[dim]Vault: {vault}[/]")
