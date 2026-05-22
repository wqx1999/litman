"""``lit promote <id>`` — semantic sugar for ``--set status=deep-read`` (M13).

Single-effect contract (OQ4 decision): ``lit promote`` ONLY changes
``status``. It does NOT also stamp ``read-date`` — those are separate
user actions and conflating them would silently overwrite an existing
``read-date`` with today's date. To record both, run
``lit read <id>`` and ``lit promote <id>`` separately.

Repeating ``lit promote`` on a paper already at ``status=deep-read`` is
a no-op: ``updated-at`` is not bumped.
"""

from __future__ import annotations

from pathlib import Path

import click

from litman.commands.modify import _apply_modify
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input


@click.command("promote")
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
def promote_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Promote a paper to status=deep-read.

    Equivalent to lit modify <id> --set status=deep-read.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead. Does NOT modify
    read-date — run lit read <id> separately if you also want to
    record that you read it today.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    _apply_modify(
        vault,
        paper_id,
        set_ops=("status=deep-read",),
        skip_set_noop=True,
    )
