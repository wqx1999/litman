"""``lit read <id>`` — semantic sugar for ``--set read-date=<date>`` (M13).

Compresses the most common agent decision chain (recall field name +
compute today's date + assemble ``--set`` syntax + invoke modify) into
one command. Date defaults to today (ISO 8601, local timezone); pass
``--date <YYYY-MM-DD>`` to backdate when logging older reads.

Repeating ``lit read <id>`` the same day is a no-op: ``read-date`` is
already that day, so ``updated-at`` is not bumped either. Mirrors
``lit modify`` no-op semantics via the shared ``_apply_modify`` backend.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import click

from litman.commands.modify import _apply_modify
from litman.core.dates import validate_iso_date
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.notes import heal_wikilink_reminder
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


@click.command("read")
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
def read_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    date_str: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Mark a paper as read by stamping read-date.

    Equivalent to lit modify <id> --set read-date=<YYYY-MM-DD>.
    Defaults to today (local timezone); use --date to backdate.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead.
    """
    date_value = validate_iso_date(date_str) if date_str else _today_iso()
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    _apply_modify(
        vault,
        paper_id,
        set_ops=(f"read-date={date_value}",),
        skip_set_noop=True,
    )
    # Reading-session close: repair the wikilink reminder an agent overwrite
    # may have stripped from notes.md, so the next session sees it again.
    heal_wikilink_reminder(vault, paper_id)
