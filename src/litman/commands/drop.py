"""``lit drop <id>`` — semantic sugar for ``--set status=dropped`` (M13).

State-machine sugar (no ``--date`` option): the value is fixed to the
command name's intended state. The command name itself doubles as
typo-prevention — ``lit drop`` cannot misspell ``status=dropd`` the way
``lit modify --set status=dropd`` can.

Repeating ``lit drop`` on an already-dropped paper is a no-op:
``updated-at`` is not bumped. State-machine legality (can this paper
move to ``dropped`` from its current status?) is not enforced — the
sugar just writes; semantic gating is the user/skill's responsibility.
"""

from __future__ import annotations

from pathlib import Path

import click

from litman.commands.modify import _apply_modify
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input


@click.command("drop")
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
def drop_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Mark a paper as dropped by setting status=dropped.

    Equivalent to lit modify <id> --set status=dropped.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead. Reverse via
    lit modify <id> --set status=<new-status>.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    _apply_modify(
        vault,
        paper_id,
        set_ops=("status=dropped",),
        skip_set_noop=True,
    )
