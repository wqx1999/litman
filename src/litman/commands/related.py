"""``lit related <id>`` — knowledge-graph neighbour traversal (M33)."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input
from litman.core.related import find_related

console = Console()


@click.command("related")
@click.argument(
    "paper_id", required=False, shell_complete=complete_paper_id
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Reverse-lookup the paper by DOI instead of supplying the id. "
        "Mutually exclusive with the positional paper id."
    ),
)
@click.option(
    "--by",
    type=click.Choice(["edges", "taxonomy"]),
    default=None,
    help="Narrow to one neighbour kind. 'edges' = author-asserted relation "
    "fields; 'taxonomy' = shared topics/methods. Default: both, edges first.",
)
@click.option(
    "--min-shared",
    "min_shared",
    type=int,
    default=1,
    help="Minimum shared topic/method keys for a taxonomy neighbour "
    "(default 1 = any shared key). Does not affect edge neighbours.",
)
@click.option(
    "--limit",
    type=int,
    default=20,
    help="Top-K cap on the merged neighbour list (default 20).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "table"]),
    default="json",
    help="Output format. 'json' (default, agent-facing) emits an array of "
    "the INDEX projection plus a 'via' annotation; 'table' is human-readable.",
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
def related_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    by: str | None,
    min_shared: int,
    limit: int,
    output_format: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Find papers related to <id> via explicit edges + shared taxonomy.

    Two neighbour kinds, merged by default: author-asserted relation edges
    (related / extends / extended-by / contradicts / contradicted-by) come
    first, then papers sharing topics/methods keys, ranked by shared-key count.
    Each neighbour carries a 'via' annotation explaining why it matched.
    Default output is JSON for agent bounded retrieval; --format table for a
    human view.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)

    neighbours = find_related(
        vault, paper_id, by=by, min_shared=min_shared, limit=limit
    )

    if output_format == "json":
        click.echo(json.dumps(neighbours, ensure_ascii=False))
        return

    if not neighbours:
        console.print(f"[dim]No related papers found for {paper_id}.[/]")
        return

    table = Table(
        title=f"Related to {paper_id} ({len(neighbours)})", show_lines=False
    )
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("year", justify="right")
    table.add_column("title", style="dim")
    table.add_column("via")
    for n in neighbours:
        if n.get("via") == "edge":
            via = f"edge:{n.get('edge')}"
        else:
            via = "taxonomy: " + ", ".join(n.get("shared") or [])
        title = (n.get("title") or "").strip()
        table.add_row(
            str(n.get("id")),
            "-" if n.get("year") is None else str(n.get("year")),
            title,
            via,
        )
    console.print(table)
