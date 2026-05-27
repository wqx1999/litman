"""``lit refresh-views`` — rebuild INDEX.json and views/by-*/ from metadata."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape

from litman.core.config import load_config
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.project_refs import rebuild_all_project_refs
from litman.core.views import rebuild_views, write_index

console = Console()


@click.command("refresh-views")
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
def refresh_views_cmd(
    library: Path | None, vault_name: str | None
) -> None:
    """Rebuild derived views from papers/*/metadata.yaml.

    Three things get refreshed, in order:

    \b
    1. INDEX.json — global paper summary + by-doi reverse map.
    2. views/by-*/ symlink hubs — wiped and rebuilt; stale tag buckets
       disappear.
    3. <project_dir>/literature/REFERENCES.md for each project in
       lit-config.yaml's projects map. Per-project failures (missing
       project dir on this machine) are skipped, not aborted.

    The metadata files are the single source of truth; every output here
    is derived and can be regenerated wholesale.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    papers = list_papers(vault)
    config = load_config(vault)

    write_index(vault, papers)
    counts = rebuild_views(vault, papers)
    project_results = rebuild_all_project_refs(vault, config.projects)

    n = len(papers)
    console.print(
        f"[green]✓[/] INDEX.json updated ({n} paper{'s' if n != 1 else ''})"
    )
    for view_name, k in counts.items():
        console.print(f"  views/{view_name}: {k} symlink{'s' if k != 1 else ''}")
    if project_results:
        console.print(f"[green]✓[/] Project REFERENCES.md ({len(project_results)} project{'s' if len(project_results) != 1 else ''})")
        for project, info in project_results.items():
            status = info["status"]
            n_papers = info["n_papers"]
            if status == "written":
                console.print(
                    f"  {escape(project)}: {n_papers} paper(s) → "
                    f"[dim]{info['path']}[/]"
                )
            elif status == "skipped":
                console.print(
                    f"  [yellow]{escape(project)}: skipped[/] "
                    f"({n_papers} paper(s) tagged) — {escape(info['detail'])}"
                )
            else:
                console.print(
                    f"  [red]{escape(project)}: error[/] — {escape(info['detail'])}"
                )
    elif config.projects:
        # registry has entries but rebuild returned nothing — should not happen
        console.print("[yellow]Project registry populated but no results.[/]")
    console.print(f"[dim]Vault: {vault}[/]")
