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

    return True
