"""``lit link`` / ``lit unlink`` commands (M5.2)."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.core.config import load_config
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import (
    complete_paper_id,
    find_paper_id_by_doi,
    resolve_paper_id,
    resolve_paper_input,
)
from litman.core.project_link import (
    link_paper_to_project,
    rebuild_all_project_links,
    unlink_paper_from_project,
)
from litman.exceptions import LitmanError

console = Console()


@click.command("link")
@click.argument(
    "paper_id", required=False, shell_complete=complete_paper_id
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Reverse-lookup the paper by DOI instead of supplying the id. "
        "Mutually exclusive with the positional paper id and --rebuild-all."
    ),
)
@click.option(
    "--project",
    "project",
    default=None,
    help="Project name (must be registered in lit-config.yaml's projects:).",
)
@click.option(
    "--relevance",
    "relevance",
    default=None,
    help=(
        "Set the relevance-<project> annotation in one shot. Without "
        "this flag, the field is left untouched and you can set it later "
        "via lit modify <id> --set relevance-<project>='...'."
    ),
)
@click.option(
    "--rebuild-all",
    is_flag=True,
    default=False,
    help=(
        "Cross-machine recovery: skip <paper-id>/--project and instead "
        "rebuild every registered project's symlinks + REFERENCES.md "
        "from scratch, based on each paper's projects field."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
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
def link_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    project: str | None,
    relevance: str | None,
    rebuild_all: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Link a paper to a project: tag + symlinks + REFERENCES.md.

    Single-paper mode (the paper id accepts a full id, a unique substring,
    or --paper-doi <DOI>):

    \b
        lit link <paper-id> --project <name>
        lit link <paper-id> --project <name> --relevance "Direct baseline"
        lit link --paper-doi 10.1038/... --project <name>

    Cross-machine recovery mode (rebuild every project's symlinks +
    REFERENCES.md from each paper's projects field):

    \b
        lit link --rebuild-all

    The project must be registered in lit-config.yaml's projects map BEFORE
    linking, and its directory must exist on disk (lit-config.yaml stores
    only the path, not the directory itself).
    """
    if rebuild_all:
        if paper_id or project or paper_doi:
            raise LitmanError(
                "--rebuild-all is exclusive with <paper-id> / --paper-doi / "
                "--project. Pass either single-paper args OR --rebuild-all, "
                "not both."
            )
        vault = find_vault(resolve_library_or_vault(library, vault_name))
        config = load_config(vault)
        if not config.projects:
            console.print(
                "[yellow]No projects registered in lit-config.yaml. "
                "Nothing to rebuild.[/]"
            )
            return
        results = rebuild_all_project_links(vault, config.projects)
        for proj, info in results.items():
            status = info["status"]
            if status == "rebuilt":
                console.print(
                    f"[green]✓ {escape(proj)}[/]: "
                    f"{info['n_paper_links']} paper link(s), "
                    f"{info['n_code_links']} code link(s) "
                    f"({info['n_tagged']} paper(s) tagged)"
                )
            else:
                console.print(
                    f"[yellow]○ {escape(proj)}: {status}[/] — "
                    f"{escape(info['detail'])}"
                )
        return

    has_id = paper_id is not None and paper_id != ""
    has_doi = paper_doi is not None and paper_doi != ""
    if has_id and has_doi:
        raise LitmanError(
            "--paper-doi and the positional paper id are mutually "
            "exclusive. Pass one or the other, not both."
        )
    if not has_id and not has_doi:
        raise LitmanError(
            "Single-paper mode requires a paper id (or --paper-doi <DOI>) "
            "and --project <name>. Or pass --rebuild-all for cross-machine "
            "recovery."
        )
    if not project:
        raise LitmanError(
            "Single-paper mode requires --project <name>. "
            "Or pass --rebuild-all for cross-machine recovery."
        )

    vault = find_vault(resolve_library_or_vault(library, vault_name))
    if has_doi:
        assert paper_doi is not None
        paper_id = find_paper_id_by_doi(vault, paper_doi)
    else:
        assert paper_id is not None
        paper_id = resolve_paper_id(vault, paper_id)
    config = load_config(vault)
    result = link_paper_to_project(
        vault, paper_id, project, config.projects, relevance=relevance
    )

    body_lines = [
        f"[bold green]Linked:[/] {escape(paper_id)} → "
        f"project {escape(project)}",
        f"[dim]Project dir:[/] {result['project_dir']}",
    ]
    if result["metadata_changed"]:
        if result["added_to_projects"]:
            body_lines.append(
                f"[dim]Metadata:[/] added {escape(project)!r} to `projects`"
            )
        if result["set_relevance"]:
            body_lines.append(
                f"[dim]Metadata:[/] set `relevance-{escape(project)}`"
            )
    else:
        body_lines.append("[dim]Metadata:[/] unchanged (already linked)")
    body_lines.append(f"[dim]Paper symlink:[/] {result['paper_link']}")
    if result["code_links"]:
        body_lines.append(
            f"[dim]Code symlinks:[/] {', '.join(escape(r) for r in result['code_links'])}"
        )
    if result["code_links_skipped_missing_repo"]:
        body_lines.append(
            f"[yellow]Code symlinks skipped (repo missing locally):[/] "
            f"{', '.join(escape(r) for r in result['code_links_skipped_missing_repo'])} "
            "[dim](run `lit code restore-all` then `lit link --rebuild-all`)[/]"
        )
    body_lines.append(f"[dim]REFERENCES.md:[/] {result['references_md']}")
    if result["added_to_projects"] and not result["set_relevance"]:
        body_lines.append("")
        body_lines.append(
            f"[dim]Tip:[/] set the per-project note with "
            f"`lit modify {escape(paper_id)} --set "
            f"relevance-{escape(project)}='...'`."
        )
    console.print(
        Panel.fit("\n".join(body_lines), title="lit link", border_style="green")
    )


@click.command("unlink")
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
    "--project",
    "project",
    required=True,
    help="Project name to unlink from.",
)
@click.option(
    "--keep-relevance",
    is_flag=True,
    default=False,
    help=(
        "Preserve the relevance-<project> field in metadata. Default "
        "is to drop it (the value is echoed in the summary so you can "
        "recover it from terminal scrollback if needed)."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
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
def unlink_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    project: str,
    keep_relevance: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Unlink a paper from a project: remove tag + symlinks + REFERENCES.md.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead.

    The reverse of lit link. Code symlinks under the project are
    only removed if no OTHER linked paper in the project still
    references the same repo (shared-utility-lib case).
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    config = load_config(vault)
    result = unlink_paper_from_project(
        vault, paper_id, project, config.projects,
        purge_relevance=not keep_relevance,
    )

    body_lines = [
        f"[bold green]Unlinked:[/] {escape(paper_id)} from "
        f"project {escape(project)}",
        f"[dim]Project dir:[/] {result['project_dir']}",
    ]
    if result["metadata_changed"]:
        if result["was_in_projects"]:
            body_lines.append(
                f"[dim]Metadata:[/] removed {escape(project)!r} from `projects`"
            )
        if result["removed_relevance"]:
            body_lines.append(
                f"[dim]Metadata:[/] dropped `relevance-{escape(project)}` "
                f"= [dim]{escape(str(result['removed_relevance_value']))}[/]"
            )
    else:
        body_lines.append("[dim]Metadata:[/] unchanged (was not linked)")
    body_lines.append(
        f"[dim]Paper symlink:[/] "
        f"{'removed' if result['paper_link_removed'] else 'absent (already)'}"
    )
    if result["code_links_removed"]:
        body_lines.append(
            f"[dim]Code symlinks removed:[/] "
            f"{', '.join(escape(r) for r in result['code_links_removed'])}"
        )
    if result["code_links_kept"]:
        kept = ", ".join(
            f"{escape(r)} (still used by {', '.join(escape(p) for p in users)})"
            for r, users in result["code_links_kept"]
        )
        body_lines.append(f"[dim]Code symlinks kept:[/] {kept}")
    body_lines.append(f"[dim]REFERENCES.md:[/] {result['references_md']}")
    console.print(
        Panel.fit("\n".join(body_lines), title="lit unlink", border_style="green")
    )
