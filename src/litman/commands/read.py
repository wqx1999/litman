"""``lit read <id>`` — semantic sugar for ``--set read-date=<date>`` (M13).

Compresses the most common agent decision chain (recall field name +
compute today's date + assemble ``--set`` syntax + invoke modify) into
one command. Date defaults to today (ISO 8601, local timezone); pass
``--date <YYYY-MM-DD>`` to backdate when logging older reads.

``read-date`` is the immutable first-read stamp: once set, ``lit read`` is
a no-op and will not overwrite it (so the first read can never be pushed
past a later ``last-revisited``). Log a return visit with ``lit revisit``;
correct a mistaken date through ``lit modify --set read-date=`` — the repair
path is deliberately more explicit than the everyday one (friction as a
feature).
"""

from __future__ import annotations

from pathlib import Path

import click

from litman.commands.modify import _apply_modify
from litman.core.dates import today_iso, validate_iso_date
from litman.core.document import find_paper
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.notes import heal_wikilink_reminder
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input


def apply_read(vault: Path, paper_id: str, date_value: str) -> tuple[bool, str]:
    """Stamp ``read-date`` (the immutable first-read date) on one paper.

    The single backend for both ``lit read`` and the webUI's ``POST
    /api/paper/{id}/read`` (invariant #16: one semantics path). ``paper_id``
    must already be resolved (the CLI runs it through ``resolve_paper_input``;
    the server passes the canonical id straight through). ``date_value`` is an
    already-validated ``YYYY-MM-DD`` string.

    read-date is the immutable first-read stamp: if it is already set this is a
    no-op that leaves it unchanged (so a re-read cannot push the first read past
    a later ``last-revisited``). On a change, ``_apply_modify`` stamps the date,
    bumps ``updated-at``, and rebuilds INDEX/views atomically; the reading-
    session close then heals the wikilink reminder in notes.md.

    Returns:
        ``(changed, message)``. ``changed`` is ``True`` when the date was newly
        stamped, ``False`` on the already-read no-op. ``message`` is the
        human-readable "already read on …" notice on a no-op, else an empty
        string (``_apply_modify`` prints its own diff on a change).

    Raises:
        PaperNotFoundError: no paper with ``paper_id`` (via ``find_paper``).
    """
    existing = find_paper(vault, paper_id)
    if existing.get("read-date"):
        message = (
            f"{paper_id} already read on {existing['read-date']}; "
            "leaving read-date unchanged. Log a return visit with "
            f"`lit revisit {paper_id}`, or correct a wrong date with "
            f"`lit modify {paper_id} --set read-date=<YYYY-MM-DD>`."
        )
        return False, message
    _apply_modify(
        vault,
        paper_id,
        set_ops=(f"read-date={date_value}",),
        skip_set_noop=True,
    )
    # Reading-session close: repair the wikilink reminder an agent overwrite
    # may have stripped from notes.md, so the next session sees it again.
    heal_wikilink_reminder(vault, paper_id)
    return True, ""


def apply_unread(vault: Path, paper_id: str) -> tuple[bool, str]:
    """Clear ``read-date`` (and its dependent ``last-revisited``) on one paper.

    The guarded reversal of :func:`apply_read`, and the single backend for the
    webUI's ``POST /api/paper/{id}/unread`` — the "I mis-clicked Mark read"
    repair. There is deliberately NO ``lit unread`` sugar command: read-date is
    the immutable-by-default first-read stamp (invariant #11), so the CLI repair
    path stays the explicit ``lit modify --set read-date=`` (friction as a
    feature); the GUI gets this constrained undo behind a confirm dialog instead.

    The date-ordering guard forbids a ``last-revisited`` without a ``read-date``,
    so clearing read-date REQUIRES clearing last-revisited in the same write —
    the two stamps are unset atomically (clearing last-revisited is a no-op when
    it was already empty). Any revisit record is therefore discarded; warning the
    user of that loss is the caller's job (the GUI confirm dialog, default-No).

    ``paper_id`` must already be resolved. Goes through ``_apply_modify``, so the
    write is the same atomic validate + staged_write + INDEX/views recompute as
    every other structured edit — no second write path (invariant #16).

    Returns:
        ``(changed, message)``. ``changed`` is ``False`` on the
        not-currently-read no-op (``message`` explains); ``True`` when the stamps
        were cleared (``message`` empty — ``_apply_modify`` prints its own diff).

    Raises:
        PaperNotFoundError: no paper with ``paper_id`` (via ``find_paper``).
    """
    existing = find_paper(vault, paper_id)
    if not existing.get("read-date"):
        return False, f"{paper_id} is not marked read; nothing to undo."
    changed = _apply_modify(
        vault,
        paper_id,
        set_ops=("read-date=", "last-revisited="),
        skip_set_noop=True,
    )
    return changed, ""


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
    """Mark a paper as read by stamping read-date (the first-read date).

    Equivalent to lit modify <id> --set read-date=<YYYY-MM-DD>.
    Defaults to today (local timezone); use --date to backdate.

    read-date is the immutable first-read stamp: if it is already set this
    command is a no-op and leaves it unchanged. Log a return visit with
    lit revisit; correct a wrong date with
    lit modify <id> --set read-date=<YYYY-MM-DD>.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead.
    """
    date_value = validate_iso_date(date_str) if date_str else today_iso()
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    # read-date is the immutable "first read" stamp: once set, this command is
    # a no-op so a re-read can't silently overwrite it (and push it past
    # last-revisited). Correcting a wrong first-read date is deliberately less
    # convenient — go through `lit modify` (friction as a feature: the everyday
    # path can't foot-gun, the repair path is explicit). apply_read raises
    # PaperNotFoundError for a ghost id; on a no-op it returns the notice for
    # echoing, on a change _apply_modify (inside it) printed its own diff.
    changed, message = apply_read(vault, paper_id, date_value)
    if not changed:
        click.echo(message)
