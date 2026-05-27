"""``lit skim <id>`` — semantic sugar for ``--set status=skim`` (M13).

Records that a paper was skimmed (not deep-read). State-machine sugar:
the value is fixed to ``skim`` (cannot misspell). Repeating on an
already-skim paper is a no-op.
"""

from __future__ import annotations

from pathlib import Path

import click

from litman.commands.modify import _apply_modify
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input


@click.command("skim")
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
def skim_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Mark a paper as skimmed by setting status=skim.

    Equivalent to lit modify <id> --set status=skim.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    _apply_modify(
        vault,
        paper_id,
        set_ops=("status=skim",),
        skip_set_noop=True,
    )
