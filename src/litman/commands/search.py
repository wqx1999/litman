"""``lit search <query>`` — substring search over notes / discussion (M33)."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from litman.commands._options import library_option, vault_option
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.query import split_csv
from litman.core.search import search_notes

console = Console()

# Hits kept when --limit is not given: enough to answer "did I write about X",
# small enough not to flood an agent's context on a large library. A stderr
# note says when more matched.
_DEFAULT_LIMIT = 20

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
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help=f"Keep only the first N hits (bounded retrieval). Default: "
    f"{_DEFAULT_LIMIT}; a note on stderr says when more matched.",
)
@library_option
@vault_option
def search_cmd(
    query: str,
    in_files_raw: str,
    output_format: str,
    limit: int | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Search your notes.md / discussion.md for a substring (case-insensitive).

    Searches only the markdown you author per paper — NOT the PDF full text,
    NOT trashed papers, NOT the views/ link hubs. Each hit is one matched
    line. Default output is JSON ({id,file,line,snippet}) for agent bounded
    retrieval; pass --format table for a human view. Only the first 20 hits
    are shown unless --limit N says otherwise; a note on stderr gives the
    total when more matched.
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
    total = len(hits)
    hits = hits[: limit if limit is not None else _DEFAULT_LIMIT]
    if total > len(hits):
        # stderr: --format json owns stdout.
        Console(stderr=True).print(
            f"[yellow]Showing the first {len(hits)} of {total} hits; pass "
            f"--limit {total} to see them all.[/]",
            soft_wrap=True,
        )

    if output_format == "json":
        click.echo(json.dumps(hits, ensure_ascii=False))
        return

    if not hits:
        console.print(f"[dim]No matches for {query!r} in {', '.join(in_files)}.[/]")
        return

    shown = f"{len(hits)} of {total}" if total > len(hits) else f"{len(hits)}"
    table = Table(title=f"Matches for {query!r} ({shown})", show_lines=False)
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
