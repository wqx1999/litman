"""``lit cite`` — print a compact ACS-style citation for one paper.

The citation text goes to **stdout** as a single clean line, so
``lit cite <id> | pbcopy`` (or ``| xclip``) copies a paste-ready string. Any
caveats (unverified journal abbreviation, missing volume/pages, preprint venue)
go to **stderr** so they never contaminate the piped citation.

Formatting lives in ``litman.core.cite`` and is shared verbatim with the webUI
cite endpoint (invariant #16: one citation path, no second implementation).
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from litman.commands._options import library_option, vault_option
from litman.core.cite import format_acs
from litman.core.document import find_paper
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input

err_console = Console(stderr=True)


@click.command("cite")
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
@library_option
@vault_option
def cite_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Print a compact ACS-style citation for PAPER_ID.

    The paper id accepts a full id or a unique case-insensitive substring, or
    omit it and pass --paper-doi <DOI> instead (parity with `lit show` /
    `lit open`). The form is ``<journal abbrev.> <year>, <volume>, <pages>.``
    with no author list or title — the version you drop on a presentation slide.
    The journal abbreviation comes from a shipped ISO4 table; an unknown journal
    is printed verbatim with a warning on stderr so you can verify it.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    # find_paper raises PaperNotFoundError / CorruptMetadataError (both
    # LitmanError subclasses), which `main()` renders as a friendly one-liner.
    meta = find_paper(vault, paper_id)
    citation = format_acs(meta)

    # Citation to stdout (paste-clean); caveats to stderr.
    click.echo(citation.text)
    for warning in citation.warnings:
        err_console.print(f"[yellow]warning:[/] {warning}")
