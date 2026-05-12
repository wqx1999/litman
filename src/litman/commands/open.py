"""``lit open <id>`` — resolve id and launch a PDF viewer (M9.1).

Thin wrapper: resolves the paper id (exact or substring match), validates
``paper.pdf`` exists, dispatches to a viewer per ``core.viewer``. No state
file, no skill hook — the rationale lives in ADR-004 (lit-open-no-state).

Spawning the viewer is fire-and-forget so the user's shell isn't blocked
waiting for the GUI viewer to close.

Exit codes (per M9 spec):
    0 — viewer launched.
    1 — paper resolve failure (id not found, multiple matches, pdf missing).
    2 — viewer launch failure (no usable viewer); path printed for manual
        handling so the shell pipe / scripts can distinguish "paper missing"
        from "you don't have a viewer".
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape

from litman.core.config import load_config
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.viewer import launch_pdf, resolve_paper_id
from litman.exceptions import AmbiguousPaperIdError, PaperNotFoundError

console = Console()


@click.command("open")
@click.argument("paper_id")
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
        "Vault name from ~/.config/litman/vaults.yaml (M8). "
        "Mutually exclusive with --library."
    ),
)
def open_cmd(
    paper_id: str, library: Path | None, vault_name: str | None
) -> None:
    """Open a paper's PDF in the configured (or platform default) viewer.

    Id resolution accepts exact match first, then a case-insensitive
    substring match if unique. Multiple substring matches print the
    candidate list and exit 1 so the user can re-run with a more
    specific id.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))

    try:
        resolved_id = resolve_paper_id(vault, paper_id)
    except AmbiguousPaperIdError as e:
        console.print(
            f"[bold yellow]Ambiguous id[/] {escape(paper_id)!r} — "
            f"{len(e.candidates)} matches:"
        )
        for candidate in e.candidates:
            console.print(f"  - {escape(candidate)}")
        console.print(
            "[dim]Re-run `lit open` with a longer / exact id.[/]"
        )
        sys.exit(1)

    pdf_path = vault / "papers" / resolved_id / "paper.pdf"
    if not pdf_path.is_file():
        raise PaperNotFoundError(
            f"Paper {resolved_id!r} has no paper.pdf at {pdf_path}."
        )

    config = load_config(vault)
    try:
        cmd, source = launch_pdf(pdf_path, config.default_pdf_viewer)
    except FileNotFoundError as e:
        console.print(f"[bold red]error:[/] {escape(str(e))}")
        console.print(f"[dim]PDF path:[/] {escape(str(pdf_path))}")
        sys.exit(2)

    console.print(
        f"[green]Opened[/] [bold]{escape(resolved_id)}[/] "
        f"[dim](viewer: {escape(cmd)}, {source})[/]"
    )
