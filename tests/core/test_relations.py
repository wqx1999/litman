"""Tests for the shared relation-pair map (ADR-012, M23.0)."""

from __future__ import annotations

from litman.core.relations import ALL_REF_FIELDS, RELATION_PAIRS


def test_relation_pairs_are_involutive() -> None:
    """Applying the pair map twice returns the original field."""
    for field, reverse in RELATION_PAIRS.items():
        assert reverse in RELATION_PAIRS
        assert RELATION_PAIRS[reverse] == field


def test_related_is_self_paired() -> None:
    assert RELATION_PAIRS["related"] == "related"


def test_directional_pairs() -> None:
    assert RELATION_PAIRS["extends"] == "extended-by"
    assert RELATION_PAIRS["extended-by"] == "extends"
    assert RELATION_PAIRS["contradicts"] == "contradicted-by"
    assert RELATION_PAIRS["contradicted-by"] == "contradicts"


def test_all_ref_fields_covers_every_key() -> None:
    assert set(ALL_REF_FIELDS) == set(RELATION_PAIRS)
    # Forward + reverse fields are all present.
    assert "extended-by" in ALL_REF_FIELDS
    assert "contradicted-by" in ALL_REF_FIELDS
