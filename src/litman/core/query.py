"""Shared paper-filtering primitives for ``lit list`` and ``lit export``.

Both commands used to carry their own ``_matches_filters`` helper, and the
two drifted: list covered every dimension but only single-valued, export
supported comma-OR but lacked topic/method/data/author. This module is the
single, unified implementation both now import, so the two cannot diverge
again (M31).

Pure functions only — no IO, no CLI rendering. This is Layer 2 business
logic, callable directly from tests.

Time filtering (``--read-since`` / ``--added-since``) deliberately lives
*outside* this module: its semantics are a date lower-bound (``>=``), not
set-membership, so folding it into :func:`matches_filters` would make the
helper carry two unlike comparison kinds. It stays in the list command layer.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any


def split_csv(value: str | None) -> list[str] | None:
    """Parse a comma-separated CLI value into a list of trimmed tokens.

    Returns ``None`` (meaning "no filter") when ``value`` is ``None`` or
    empty. Empty tokens after splitting (e.g. trailing comma) are discarded
    silently rather than triggering an error; this matches the "be permissive
    in what you accept" CLI norm.
    """
    if value is None:
        return None
    tokens = [t.strip() for t in value.split(",") if t.strip()]
    return tokens or None


# filter name -> metadata list-field; the paper's list and the wanted tokens
# must share at least one exact value.
_LIST_FILTERS: tuple[tuple[str, str], ...] = (
    ("topic", "topics"),
    ("method", "methods"),
    ("project", "projects"),
    ("data", "data"),
)

# filter name -> metadata scalar field; str(paper value) must be in the
# wanted token set. year is handled here too — it is a scalar int that we
# compare as a string so "2023,2024" works.
_SCALAR_FILTERS: tuple[tuple[str, str], ...] = (
    ("status", "status"),
    ("priority", "priority"),
    ("type", "type"),
    ("year", "year"),
)


def matches_filters(
    paper: dict[str, Any], filters: dict[str, list[str] | None]
) -> bool:
    """Return True iff ``paper`` matches every non-None filter.

    All filter values are ``list[str] | None``. Within one filter the tokens
    are OR-combined; across different filters they are AND-combined. A
    single-valued CLI flag is just a one-element list, so the legacy
    single-value behaviour is preserved exactly (``--topic x`` is
    ``{"topic": ["x"]}``).

    Per-field match semantics:

    - topic / method / project / data: the paper's list field intersects the
      wanted tokens (any token, exact value).
    - status / priority / type: ``str(paper value or "")`` is in the wanted
      tokens.
    - year: paper year is not None AND ``str(paper year)`` is in the wanted
      tokens.
    - author: any author entry *contains* any wanted token (case-insensitive
      substring).
    - title: the title string *contains* any wanted token (case-insensitive
      substring). title is a scalar, so unlike author there is no list to
      iterate.
    """
    for filter_name, field_name in _LIST_FILTERS:
        wanted = filters.get(filter_name)
        if wanted is None:
            continue
        paper_values = paper.get(field_name) or []
        if not any(token in paper_values for token in wanted):
            return False

    for filter_name, field_name in _SCALAR_FILTERS:
        wanted = filters.get(filter_name)
        if wanted is None:
            continue
        paper_value = paper.get(field_name)
        if filter_name == "year" and paper_value is None:
            return False
        if str(paper_value or "") not in wanted:
            return False

    wanted_authors = filters.get("author")
    if wanted_authors is not None:
        haystack = paper.get("authors") or []
        if not any(
            token.lower() in (entry or "").lower()
            for entry in haystack
            for token in wanted_authors
        ):
            return False

    wanted_titles = filters.get("title")
    if wanted_titles is not None:
        haystack_title = (paper.get("title") or "").lower()
        if not any(token.lower() in haystack_title for token in wanted_titles):
            return False

    return True


def recency_key(vault: Path, paper: dict[str, Any]) -> float:
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

    Shared by ``lit list --sort recent`` and the webUI smart-list ``view=``
    query (invariant #16: the GUI reuses the same recency ranking the CLI
    uses, never a second sort path).
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
