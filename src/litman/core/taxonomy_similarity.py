"""Near-duplicate detection for controlled-vocabulary values.

A vocabulary that grows one value at a time drifts the same way every time:
``peptidedrug`` next to an existing ``peptide``, ``Peptide Design`` next to
``peptide-design``, ``cheminformatic`` next to ``cheminformatics``. Nothing
here knows what a word means; it compares spellings. Three rules, the first
that fires wins:

1. **same after normalisation** — the two values read the same once case,
   ``-`` / ``_`` / spaces / punctuation and a plural ``s`` are set aside.
2. **prefix** — one normalised form starts the other and the shorter one has
   at least 6 characters (``peptide`` / ``peptidedrug``; ``spps`` is too short
   to claim anything).
3. **edit distance N** — at most two insertions, deletions, substitutions or
   adjacent swaps apart, and the shorter form has at least 8 characters.

Normalisation is Unicode-aware: a character counts when ``str.isalnum`` says
so, so two different Chinese values are compared as written. A value with
nothing left after normalisation (only punctuation) matches nothing.

:func:`similar_values` is the one entry point. ``lit taxonomy add`` (and the
web UI's create) refuses a new value it matches, and ``lit health-check``
reports the pairs already in TAXONOMY.md.
"""

from __future__ import annotations

from collections.abc import Iterable

_SAME = "same after normalisation"
_PREFIX = "prefix"

_PREFIX_MIN = 6
_EDIT_MIN = 8
_EDIT_MAX = 2


def _normalise(value: str) -> str:
    """The form two values are compared in: words joined, no case, no plural ``s``."""
    text = value.casefold().replace("_", " ").replace("-", " ")
    text = "".join(ch for ch in text if ch.isalnum() or ch == " ")
    words = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in text.split()]
    return "".join(words)


def _edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance (Damerau-Levenshtein without re-edits)."""
    before_prev: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], before_prev[j - 2] + 1)
        before_prev, prev = prev, row
    return prev[len(b)]


def _match(a: str, b: str) -> tuple[int, int, str] | None:
    """``(rule, closeness, reason)`` for two normalised forms, or None."""
    if not a or not b:
        return None
    if a == b:
        return (1, 0, _SAME)
    short, long_ = sorted((a, b), key=len)
    if len(short) >= _PREFIX_MIN and long_.startswith(short):
        return (2, len(long_) - len(short), _PREFIX)
    if len(short) >= _EDIT_MIN and len(long_) - len(short) <= _EDIT_MAX:
        n = _edit_distance(a, b)
        if n <= _EDIT_MAX:
            return (3, n, f"edit distance {n}")
    return None


def similar_values(
    candidate: str, existing: Iterable[str]
) -> list[tuple[str, str]]:
    """The values in ``existing`` that ``candidate`` looks like, closest first.

    Returns ``(existing value, reason)`` pairs, where the reason is
    ``same after normalisation``, ``prefix`` or ``edit distance N``. Ordered
    by rule, then by how close (length difference for a prefix, N for an edit
    distance), then alphabetically. ``candidate`` itself is skipped, so a dict
    can be checked against itself.
    """
    key = _normalise(candidate)
    hits: list[tuple[int, int, str, str]] = []
    for value in dict.fromkeys(existing):
        if value == candidate:
            continue
        found = _match(key, _normalise(value))
        if found is not None:
            rule, closeness, reason = found
            hits.append((rule, closeness, value, reason))
    hits.sort()
    return [(value, reason) for _, _, value, reason in hits]
