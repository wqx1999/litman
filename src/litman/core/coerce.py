"""Low-level metadata coercion helpers shared across the data layer.

Kept dependency-free (stdlib only) so any core module — ``graph_model``,
``checks``, future consumers — can import it without an import cycle.
"""

from __future__ import annotations

from typing import Any


def as_str_list(value: Any) -> list[str]:
    """Coerce a metadata list-field value into a list of strings.

    metadata.yaml is schema-less (invariant #7): a list-typed field may be
    absent, ``None``, a proper list, or — when the user wrote ``projects: x``
    without a ``- x`` list item — a bare scalar. Iterating a bare scalar string
    with ``for x in value`` yields its CHARACTERS, exploding ``pepforge`` into
    six phantom values. This normalizes:

    * ``None`` / missing -> ``[]``
    * a ``list`` -> its elements stringified
    * a bare scalar -> a single-element ``[str(value)]``

    A bare scalar is wrapped, never dropped, so the value still SURFACES as one
    item and is checked / marked downstream rather than silently mangled into
    per-character phantoms (no silent-skip, invariant #14).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]
