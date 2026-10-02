"""Canonical paper-to-paper relation field map (ADR-012).

litman stores three kinds of literature relations as list-typed
metadata.yaml fields. ADR-012 makes all three *symmetric* by giving the
two directional kinds an explicit reverse field and having the CLI write
both ends automatically:

    related         ↔ related            (symmetric, self-paired)
    extends         ↔ extended-by        ("A extends B" ⇒ "B extended-by A")
    contradicts     ↔ contradicted-by    ("C contradicts A" ⇒ "A contradicted-by C")

The direction is locked into the *field name*, so restore / deletion
cascade / bidirectional validation are a fixed table lookup with no
direction inference.

This module is the single de-drift source for that map. Previously
``REF_FIELDS = ("related", "contradicts", "extends")`` was hard-coded in
four places (rename / checks / modify / add); they now import from here so
the reverse fields are covered uniformly.
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from typing import Any

# Maps each relation field to its paired reverse field. Symmetric fields
# (only ``related`` today) map to themselves. Both members of every
# directional pair appear as keys so a lookup never needs to know which
# side it started on.
RELATION_PAIRS: dict[str, str] = {
    "related": "related",  # symmetric, self-paired
    "extends": "extended-by",
    "extended-by": "extends",
    "contradicts": "contradicted-by",
    "contradicted-by": "contradicts",
}

# Every relation field, forward and reverse. Used by dangling-ref and
# rename scans so reverse-field references are treated identically to
# forward ones.
ALL_REF_FIELDS: tuple[str, ...] = tuple(RELATION_PAIRS)

# Reverse relation fields. These are CLI-maintained by the auto
# double-write only; a user must never name them in --add-tag / --rm-tag,
# or the pairing breaks. Single de-drift source for the forward/reverse
# split so modify (user-facing allow-set) and checks (remediation hints)
# agree.
REVERSE_REF_FIELDS: frozenset[str] = frozenset({"extended-by", "contradicted-by"})

# Forward (user-settable) relation fields. Writing one triggers the paired
# reverse write on the opposite paper. A re-sync of a broken pairing is
# always phrased as a command on the forward field, since the reverse
# fields are not user-settable.
FORWARD_REF_FIELDS: tuple[str, ...] = tuple(
    f for f in RELATION_PAIRS if f not in REVERSE_REF_FIELDS
)


def relation_refs(value: object) -> list[str]:
    """The ids one relation field holds.

    A list holds its entries. A bare string is a hand-edit (``related: X``)
    and holds that one id — iterating it would yield characters. Anything else
    holds none.
    """
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def dangling_relation_edges(
    paper: Mapping[str, Any], known_ids: Container[str]
) -> list[tuple[str, str]]:
    """``(field, ref)`` for each relation edge on ``paper`` naming no paper.

    The one definition of a dangling edge: ``lit health-check`` reports exactly
    these, ``--fix`` drops exactly these, and the GUI's remove on a link to a
    missing paper refuses anything else. Forward and reverse fields alike.
    ``known_ids`` comes from ``document.library_paper_ids`` — the trash is not
    in it, so an edge to a trashed paper dangles.
    """
    return [
        (field, ref)
        for field in ALL_REF_FIELDS
        for ref in relation_refs(paper.get(field))
        if ref not in known_ids
    ]
