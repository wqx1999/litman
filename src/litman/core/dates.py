"""Shared validation for user-supplied semantic date fields.

``read-date`` (``lit read``) and ``last-revisited`` (``lit revisit``) are
calendar dates the user types. Both must be stored as strict zero-padded
``YYYY-MM-DD`` so they sort and compare correctly as strings in INDEX.json.
Centralised here so the two commands cannot drift (review F28).
"""

from __future__ import annotations

import re
from datetime import date, datetime

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
