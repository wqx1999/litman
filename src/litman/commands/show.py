"""``lit show <id>`` — display a single paper's metadata + file paths."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from litman.core.document import find_paper
from litman.core.library import find_vault

console = Console()


@click.command("show")
@click.argument("paper_id")
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
def show_cmd(paper_id: str, library: Path | None) -> None:
    """Show one paper's metadata.yaml plus PDF / notes paths."""
    vault = find_vault(library)

    # find_paper validates id shape and existence; raises PaperNotFoundError.
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
