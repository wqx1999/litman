"""Shared validation for user-supplied semantic date fields.

``read-date`` (``lit read``) and ``last-revisited`` (``lit revisit``) are
calendar dates the user types. Both must be stored as strict zero-padded
``YYYY-MM-DD`` so they sort and compare correctly as strings in INDEX.json.
Centralised here so the two commands cannot drift (review F28).
"""

from __future__ import annotations

import re
from datetime import date

import click

# Canonical ISO 8601 *extended* calendar-date shape. fullmatch rejects ISO
# basic (20260530) and week dates (2026-W22-1) up front.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
    if not _ISO_DATE_RE.fullmatch(date_str):
        raise click.BadParameter(
            f"{date_str!r} is not a valid ISO 8601 date (expected YYYY-MM-DD)."
        )
    try:
        date.fromisoformat(date_str)
    except ValueError as e:
        raise click.BadParameter(
            f"{date_str!r} is not a valid ISO 8601 date (expected YYYY-MM-DD)."
        ) from e
    return date_str
