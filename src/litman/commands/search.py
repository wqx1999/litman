"""``lit search <query>`` — substring search over notes / discussion (M33)."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.query import split_csv
from litman.core.search import search_notes

console = Console()

# Allowed --in stems. The search corpus is exactly the two authored markdown
# files per paper (paper.pdf full text is `lit open`'s job; metadata is
# `lit list`'s).
_IN_CHOICES = ("notes", "discussion")


@click.command("search")
@click.argument("query")
@click.option(
    "--in",
    "in_files_raw",
    default="notes,discussion",
    metavar="notes,discussion",
    help="Which files to search (comma-separated). Default: both. "
    "Narrow with --in notes or --in discussion.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "table"]),
    default="json",
    help="Output format. 'json' (default, agent-facing) emits an array of "
    "{id,file,line,snippet}; 'table' renders a human-readable table.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def search_cmd(
    query: str,
    in_files_raw: str,
    output_format: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Search your notes.md / discussion.md for a substring (case-insensitive).

    Searches only the markdown you author per paper — NOT the PDF full text,
    NOT trashed papers, NOT the views/ symlink hubs. Each hit is one matched
    line. Default output is JSON ({id,file,line,snippet}) for agent bounded
    retrieval; pass --format table for a human view.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))

    in_files = split_csv(in_files_raw) or list(_IN_CHOICES)
    unknown = [f for f in in_files if f not in _IN_CHOICES]
    if unknown:
        raise click.BadParameter(
            f"--in accepts only {', '.join(_IN_CHOICES)}; got {', '.join(unknown)}.",
            param_hint="--in",
        )

    hits = search_notes(vault, query, in_files=tuple(in_files))

    if output_format == "json":
        click.echo(json.dumps(hits, ensure_ascii=False))
        return

    if not hits:
        console.print(f"[dim]No matches for {query!r} in {', '.join(in_files)}.[/]")
        return

    table = Table(title=f"Matches for {query!r} ({len(hits)})", show_lines=False)
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("file")
    table.add_column("line", justify="right")
    table.add_column("snippet", style="dim")
    for hit in hits:
        table.add_row(
            str(hit["id"]),
            str(hit["file"]),
            str(hit["line"]),
            str(hit["snippet"]),
        )
    console.print(table)
