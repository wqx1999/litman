"""``lit show <id>`` — display a single paper's metadata + file paths."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from litman.core.code import missing_code_clones
from litman.core.document import find_paper
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input

console = Console()


@click.command("show")
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
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'table' (default) renders a Panel of metadata.yaml + "
    "file paths; 'json' emits the FULL metadata dict (every field, not the "
    "INDEX projection) for agent bounded retrieval.",
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
def show_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    output_format: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Show one paper's metadata.yaml plus PDF / notes paths.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead. --format json emits the
    full metadata dict (all fields) for agents.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)

    meta = find_paper(vault, paper_id)

    if output_format == "json":
        # default=str bridges the YAML safe-loader's datetime (created-at /
        # updated-at) and date (read-date / last-revisited) values, which
        # json.dumps cannot serialize natively — without it a paper WITH a
        # read-date raises TypeError (the M25/M31 trap, spec §9).
        click.echo(
            json.dumps(meta, default=str, ensure_ascii=False, indent=2)
        )
        return

    paper_dir = vault / "papers" / paper_id
    meta_file = paper_dir / "metadata.yaml"
    pdf_file = paper_dir / "paper.pdf"
    notes_file = paper_dir / "notes.md"

    yaml_text = meta_file.read_text(encoding="utf-8")

    console.print(
        Panel(
            Syntax(
                yaml_text,
                "yaml",
                theme="monokai",
                background_color="default",
                line_numbers=False,
            ),
            title=f"[bold cyan]{paper_id}[/]",
            border_style="cyan",
        )
    )

    pdf_status = "" if pdf_file.is_file() else "  [bold red](missing!)[/]"
    notes_status = "" if notes_file.is_file() else "  [bold red](missing!)[/]"
    console.print(f"[dim]PDF:  [/] {pdf_file}{pdf_status}")
    console.print(f"[dim]Notes:[/] {notes_file}{notes_status}")

    # Code-clones: one line per bound repo, with the same (missing!) marker used
    # for PDF/Notes when codes/<name>/ is gone (the dangling-link case lit
    # health-check flags under invariant #12). The link is kept by design (it
    # records the re-clone target); this only stops it from reading as live.
    code_clones = meta.get("code-clones") or []
    if code_clones:
        gone = set(missing_code_clones(vault, code_clones))
        codes_dir = vault / "codes"
        for name in code_clones:
            status = "  [bold red](missing!)[/]" if name in gone else ""
            console.print(f"[dim]Code: [/] {codes_dir / name}{status}")
