"""``lit revisit <id>`` — semantic sugar for ``--set last-revisited=<date>`` (M13).

Distinct from ``lit read``: ``read-date`` records the first read (the
moment you decided this paper was worth curating), while
``last-revisited`` records that you came back to it. Invariant #11
forbids merging the two fields, so the CLI surface keeps them separate
too — see ``dev_docs/invariants.md`` §11.

Date defaults to today (ISO 8601, local timezone); pass ``--date`` to
backdate when logging older revisits. Same-day repeats are no-ops.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import click

from litman.commands.modify import _apply_modify
from litman.core.dates import validate_iso_date
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


@click.command("revisit")
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
    "--date",
    "date_str",
    default=None,
    metavar="YYYY-MM-DD",
    help="Date to record. Defaults to today (local timezone).",
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
def revisit_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    date_str: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Mark a paper as revisited by stamping last-revisited.

    Equivalent to lit modify <id> --set last-revisited=<YYYY-MM-DD>.
    Defaults to today (local timezone); use --date to backdate.

    Distinct from lit read (which stamps read-date, the first
    read); invariant #11 keeps the two fields semantically separate.
    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead.
    """
    date_value = validate_iso_date(date_str) if date_str else _today_iso()
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    _apply_modify(
        vault,
        paper_id,
        set_ops=(f"last-revisited={date_value}",),
        skip_set_noop=True,
    )
