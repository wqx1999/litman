"""``lit show <id>`` — display a single paper's metadata + file paths."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

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
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Show one paper's metadata.yaml plus PDF / notes paths.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)

    find_paper(vault, paper_id)

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
