"""Shared validation for user-supplied semantic date fields.

``read-date`` (``lit read``) and ``last-revisited`` (``lit revisit``) are
calendar dates the user types. Both must be stored as strict zero-padded
``YYYY-MM-DD`` so they sort and compare correctly as strings in INDEX.json.
Centralised here so the two commands cannot drift (review F28).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

import click

# Canonical ISO 8601 *extended* calendar-date shape. fullmatch rejects ISO
# basic (20260530) and week dates (2026-W22-1) up front.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_iso_date(value: object) -> bool:
    """True if ``value`` is a strict ``YYYY-MM-DD`` calendar-date string.

    Non-raising predicate (cf. :func:`validate_iso_date`) for read-side
    validation. Rejects non-strings, ISO basic / week forms, and impossible
    calendar values. Used by ``check_schema`` for the semantic date fields
    ``read-date`` / ``last-revisited`` (invariant #11 / review F9).
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_iso_datetime(value: object) -> bool:
    """True if ``value`` parses as an ISO 8601 datetime string.

    Non-raising predicate for the machine-maintained audit timestamps
    ``created-at`` / ``updated-at`` (invariant #11 / review F9). Lenient on
    timezone representation (the tool emits tz-aware stamps, but legacy data
    may vary); the goal is catching outright garbage that ``check_inbox_
    staleness`` would otherwise silently skip on ``datetime.fromisoformat``.
    """
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_iso_date(date_str: str) -> str:
    """Return ``date_str`` if it is a strict ``YYYY-MM-DD`` calendar date.

    Python 3.11+ relaxed ``date.fromisoformat`` to also accept ISO basic
    (``20260530``) and week-date (``2026-W22-1``) forms (review F28). Those
    parse to a real date but, stored verbatim, sort and compare wrong against
    extended-form dates. Gate on the canonical shape first, then confirm the
    calendar value is real (rejecting e.g. ``2026-13-40``), before any write.

    Raises:
        click.BadParameter: shape mismatch or impossible calendar date.
    """
    if not is_iso_date(date_str):
        raise click.BadParameter(
            f"{date_str!r} is not a valid ISO 8601 date (expected YYYY-MM-DD)."
        )
    return date_str


def now_iso() -> str:
    """Local-timezone ISO 8601 timestamp with seconds precision.

    The single source for the machine-maintained ``created-at`` /
    ``updated-at`` audit stamps and every command's metadata write-back
    (invariant #11). Centralised here so the formerly per-module ``_now_iso``
    copies cannot drift. Distinct from ``trash``'s compact
    ``%Y%m%dT%H%M%SZ`` entry-name stamp, which stays local to that module.
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    """Local calendar date as a strict ``YYYY-MM-DD`` string.

    The default stamp for the user-typed semantic date fields ``read-date``
    (``lit read``) and ``last-revisited`` (``lit revisit``). Distinct from
    :func:`now_iso`, which carries seconds + timezone for the machine audit
    stamps. Centralised so the two sugar commands and the date-ordering guard
    share one definition of "today".
    """
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def _as_iso_date_str(value: object) -> str | None:
    """Normalise a metadata date value to ``YYYY-MM-DD``, or None.

    ruamel round-trips an unquoted YAML date into a ``datetime.date``; the
    sugar commands write quoted strings. Accept both and return the canonical
    string; return None for empty / unparseable input — a format breach is
    reported separately by :func:`is_iso_date` in ``check_schema``, so the
    ordering guard simply skips what it cannot compare.
    """
    if value is None or value == "":
        return None
    s = value.isoformat() if isinstance(value, date) else str(value)
    return s if is_iso_date(s) else None


def date_ordering_violations(
    read_date: object, last_revisited: object, today: str | None = None
) -> list[str]:
    """Return one human-readable string per read-date / last-revisited breach.

    The single rule shared by the modify-time guard (which raises
    ``ModifyError``) and ``check_schema`` (which emits ``Issue`` records), so
    the two paths cannot drift (review F28 spirit). Empty list = consistent.
    The contract, per invariant #11:

    * neither date may be in the future;
    * ``last-revisited`` implies a ``read-date`` (a revisit presupposes a
      first read);
    * ``read-date`` (the immutable first-read stamp) cannot postdate
      ``last-revisited``.

    Zero-padded ``YYYY-MM-DD`` strings order correctly under ``<`` / ``>``.
    """
    rd = _as_iso_date_str(read_date)
    lr = _as_iso_date_str(last_revisited)
    today = today or today_iso()
    out: list[str] = []
    if rd and rd > today:
        out.append(f"read-date {rd} is in the future (today is {today})")
    if lr and lr > today:
        out.append(f"last-revisited {lr} is in the future (today is {today})")
    if lr and not rd:
        out.append(
            "last-revisited is set but read-date is not — a revisit "
            "presupposes a first read"
        )
    if rd and lr and rd > lr:
        out.append(
            f"read-date {rd} is after last-revisited {lr} — the first read "
            "cannot postdate a revisit"
        )
    return out
