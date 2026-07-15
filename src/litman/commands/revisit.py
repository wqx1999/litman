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

from pathlib import Path

import click

from litman.commands._options import library_option, vault_option
from litman.commands.modify import _apply_modify
from litman.core.dates import today_iso, validate_iso_date
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.notes import heal_wikilink_reminder
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input


def apply_revisit(vault: Path, paper_id: str, date_value: str) -> None:
    """Stamp ``last-revisited`` on one paper.

    The single backend for both ``lit revisit`` and the webUI's ``POST
    /api/paper/{id}/revisit`` (invariant #16: one semantics path). ``paper_id``
    must already be resolved; ``date_value`` is an already-validated
    ``YYYY-MM-DD`` string.

    A revisit presupposes a first read: ``_apply_modify``'s date-ordering guard
    raises ``ModifyError`` when the paper has no ``read-date`` (or the resulting
    pair is otherwise inconsistent). On success it stamps the date, bumps
    ``updated-at``, rebuilds INDEX/views atomically; the reading-session close
    then heals the wikilink reminder.

    Raises:
        PaperNotFoundError: ``papers/<id>/metadata.yaml`` does not exist.
        ModifyError: no ``read-date`` (revisit before first read) or any other
            date-ordering breach (invariant #11).
    """
    _apply_modify(
        vault,
        paper_id,
        set_ops=(f"last-revisited={date_value}",),
        skip_set_noop=True,
    )
    # Reading-session close: repair the wikilink reminder an agent overwrite
    # may have stripped from notes.md, so the next session sees it again.
    heal_wikilink_reminder(vault, paper_id)


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
@library_option
@vault_option
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
    A revisit presupposes a first read, so the paper must already have a
    read-date that does not postdate the revisit (mark it read first with
    lit read).

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead.
    """
    date_value = validate_iso_date(date_str) if date_str else today_iso()
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    apply_revisit(vault, paper_id, date_value)
