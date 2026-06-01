"""``lit list`` — query papers in the vault, with filters."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from litman.core.dates import validate_iso_date
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.query import matches_filters, split_csv
from litman.core.views import project_paper

console = Console()


def _iso_date_option(ctx: object, param: object, value: str | None) -> date | None:
    """Click callback for --read-since / --added-since.

    Reuses the strict ``validate_iso_date`` (the same gate ``lit read`` /
    ``lit revisit`` use) so non-zero-padded forms like ``2026-5-1`` and ISO
    basic / week dates are rejected with a clear error, then converts the
    validated string to a ``date`` for the ``>=`` comparison. ``click.DateTime``
    is NOT used because its strptime-based parsing accepts ``2026-5-1``.
    """
    if value is None:
        return None
    return date.fromisoformat(validate_iso_date(value))

# Title column display cap. Beyond this, an ellipsis is appended.
_TITLE_MAX = 60

# Row cap for ``--sort recent`` in the table view. "Most-recently-engaged"
# implies top-N — printing hundreds of rows defeats the intent. JSON output
# is NOT capped so agent retrieval still gets the full ranked list.
_RECENT_TABLE_CAP = 10


def _format_cell(value: Any) -> str:
    """Render a metadata field for the list table.

    None becomes "-" (the M29 "not yet evaluated" sentinel for optional
    fixed-enum fields like priority and type); everything else falls
    through to ``str()``.
    """
    return "-" if value is None else str(value)


def _recency_key(vault: Path, paper: dict[str, Any]) -> float:
    """Sort key for ``--sort recent``: the more recent of two engagement
    signals, as a POSIX timestamp.

    1. ``paper.pdf`` filesystem mtime — bumps when the user annotates the
       PDF in a viewer that writes back to the file (the reading signal,
       viewer-agnostic because mtime is OS-maintained).
    2. ``updated-at`` metadata field — bumps on any litman write
       (lit read / lit modify / tag / link = agent-mediated curation).

    Returns the later of the two. A missing PDF or a missing/malformed
    ``updated-at`` contributes 0.0, so a paper with neither engagement
    signal sinks to the bottom.
    """
    pdf = vault / "papers" / str(paper.get("id", "")) / "paper.pdf"
    try:
        pdf_mtime = pdf.stat().st_mtime
    except OSError:
        pdf_mtime = 0.0
    # The YAML safe-loader parses an ISO 8601 timestamp into a datetime
    # object, so updated-at usually arrives already typed. A plain string
    # is still accepted (e.g. a non-roundtripped value) via fromisoformat.
    raw = paper.get("updated-at")
    try:
        if isinstance(raw, datetime):
            updated = raw.timestamp()
        elif isinstance(raw, date):
            # A bare date (YAML safe-loader parses "2026-05-30" into a
            # datetime.date, NOT a string) has no .timestamp() and is not a
            # valid fromisoformat() argument — treat it as that day's midnight
            # instead of sinking the paper to 0.0 (review F29). datetime is a
            # date subclass, so this branch only catches pure dates (the
            # datetime check above already handled the common case).
            updated = datetime(raw.year, raw.month, raw.day).timestamp()
        elif raw:
            updated = datetime.fromisoformat(str(raw)).timestamp()
        else:
            updated = 0.0
    except (ValueError, TypeError, OSError, OverflowError):
        updated = 0.0
    return max(pdf_mtime, updated)


def _as_date(raw: Any) -> date | None:
    """Coerce a metadata date/timestamp field to a ``datetime.date``.

    The YAML safe-loader parses ``read-date: 2026-05-26`` into a
    ``datetime.date`` and ``created-at: ...T...+08:00`` into a
    ``datetime.datetime`` (M25 lesson), but a non-roundtripped value can
    still arrive as a plain string. Normalizes all four states to a bare
    ``date`` and returns ``None`` for missing / empty / unparseable values,
    so the time filter excludes such papers instead of raising. ``datetime``
    is a ``date`` subclass, so it is checked first.
    """
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if raw:
        try:
            return datetime.fromisoformat(str(raw)).date()
        except ValueError:
            return None
    return None


@click.command("list")
@click.option(
    "--year", type=str,
    help="Filter by publication year. Comma-separated = OR (e.g. 2023,2024).",
)
@click.option(
    "--type", "type_filter",
    help="Filter by paper type (research/review/position/...). "
         "Comma-separated = OR.",
)
@click.option(
    "--status",
    help="Filter by status (deep-read/skim/inbox/dropped). "
         "Comma-separated = OR (e.g. deep-read,skim).",
)
@click.option(
    "--priority",
    help="Filter by priority (A/B/C). Comma-separated = OR (e.g. A,B).",
)
@click.option(
    "--topic",
    help="Match papers whose topics list contains this value. "
         "Comma-separated = OR.",
)
@click.option(
    "--method",
    help="Match papers whose methods list contains this value. "
         "Comma-separated = OR.",
)
@click.option(
    "--project",
    help="Match papers whose projects list contains this value. "
         "Comma-separated = OR.",
)
@click.option(
    "--data", "data_filter",
    help="Match papers whose data list contains this value. "
         "Comma-separated = OR.",
)
@click.option(
    "--author",
    help="Case-insensitive substring match against any author entry. "
         "Comma-separated = OR.",
)
@click.option(
    "--read-since",
    "read_since",
    default=None,
    callback=_iso_date_option,
    metavar="YYYY-MM-DD",
    help="Only papers with read-date on or after this date (read-date >= DATE).",
)
@click.option(
    "--added-since",
    "added_since",
    default=None,
    callback=_iso_date_option,
    metavar="YYYY-MM-DD",
    help="Only papers added on or after this date (created-at >= DATE).",
)
@click.option(
    "--unread", is_flag=True, default=False,
    help="Only papers not yet finished reading (read-date is empty).",
)
@click.option(
    "--sort", "sort_by",
    type=click.Choice(["id", "recent"]), default="id",
    help="Sort order. 'id' = ascending by paper id (default, stable, "
         "matches INDEX.json). 'recent' = most-recently-engaged first "
         "(max of paper.pdf mtime and updated-at); the table view shows "
         "only the top 10, use --format json for the full ranked list.",
)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits the same per-paper projection "
         "as INDEX.json (for agent bounded retrieval).",
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
def list_cmd(
    year: str | None,
    type_filter: str | None,
    status: str | None,
    priority: str | None,
    topic: str | None,
    method: str | None,
    project: str | None,
    data_filter: str | None,
    author: str | None,
    read_since: date | None,
    added_since: date | None,
    unread: bool,
    sort_by: str,
    output_format: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """List papers in the vault, optionally filtered.

    Filters are AND-combined; within one flag, comma-separated values are
    OR-combined. Multi-valued fields (topics/methods/projects/data) use list
    intersection; --author uses case-insensitive substring; year/type/status/
    priority match by exact value. --read-since / --added-since filter by a
    date lower-bound on read-date / created-at respectively.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    all_papers = list_papers(vault)

    filters = {
        "year": split_csv(year),
        "type": split_csv(type_filter),
        "status": split_csv(status),
        "priority": split_csv(priority),
        "topic": split_csv(topic),
        "method": split_csv(method),
        "project": split_csv(project),
        "data": split_csv(data_filter),
        "author": split_csv(author),
    }
    filtered = [p for p in all_papers if matches_filters(p, filters)]

    # Time filtering: a date lower-bound (>=), kept out of matches_filters
    # because its semantics differ from set-membership. --read-since reads
    # ONLY read-date, --added-since ONLY created-at (invariant #11). A
    # missing / None / unparseable value coerces to None and excludes the
    # paper rather than raising.
    if read_since is not None:
        filtered = [
            p for p in filtered
            if (d := _as_date(p.get("read-date"))) is not None and d >= read_since
        ]
    if added_since is not None:
        filtered = [
            p for p in filtered
            if (d := _as_date(p.get("created-at"))) is not None and d >= added_since
        ]

    if unread:
        filtered = [p for p in filtered if not p.get("read-date")]

    if sort_by == "recent":
        # list.sort is stable: papers with equal recency keep the incoming
        # id-ascending order (list_papers returns id-asc). So no explicit
        # tie-break is needed — equal-recency ties stay deterministic.
        filtered.sort(key=lambda p: _recency_key(vault, p), reverse=True)

    if output_format == "json":
        click.echo(json.dumps([project_paper(p) for p in filtered],
                               ensure_ascii=False))
        return

    if not filtered:
        if not all_papers:
            console.print(
                "[dim]No papers in vault yet. Run "
                "`lit add <pdf> --doi <doi>` to add one.[/]"
            )
        else:
            console.print(
                f"[dim]No papers match the given filters "
                f"({len(all_papers)} total in vault).[/]"
            )
        return

    # Truncate to the top-N for table display when sorted by recency. The
    # underlying ``filtered`` list is mutated *after* the JSON branch has
    # already returned, so JSON output stays full-length for agents.
    matched_count = len(filtered)
    truncated = sort_by == "recent" and matched_count > _RECENT_TABLE_CAP
    if truncated:
        filtered = filtered[:_RECENT_TABLE_CAP]
        title = f"Papers (recent {_RECENT_TABLE_CAP} of {matched_count})"
    else:
        title = f"Papers ({matched_count} of {len(all_papers)})"

    table = Table(title=title, show_lines=False)
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("year", justify="right")
    table.add_column("type")
    table.add_column("status")
    table.add_column("pri", justify="center")
    table.add_column("title", style="dim")

    for p in filtered:
        title = (p.get("title") or "").strip()
        if len(title) > _TITLE_MAX:
            title = title[: _TITLE_MAX - 1] + "…"
        table.add_row(
            _format_cell(p.get("id")),
            _format_cell(p.get("year")),
            _format_cell(p.get("type")),
            _format_cell(p.get("status")),
            _format_cell(p.get("priority")),
            title,
        )

    console.print(table)
