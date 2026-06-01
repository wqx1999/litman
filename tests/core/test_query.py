"""Unit tests for the shared filter primitives (M31, ``core/query.py``)."""

from __future__ import annotations

import pytest

from litman.core.query import matches_filters, split_csv

# ---------------------------------------------------------------------------
# split_csv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a,b", ["a", "b"]),
        ("a, ,b,", ["a", "b"]),          # blanks + trailing comma dropped
        ("  a  ,  b  ", ["a", "b"]),     # surrounding whitespace trimmed
        ("solo", ["solo"]),
        (None, None),
        ("", None),
        ("  ", None),                    # whitespace-only -> no filter
        (",", None),                     # only-separators -> no filter
    ],
)
def test_split_csv(value: str | None, expected: list[str] | None) -> None:
    assert split_csv(value) == expected


# ---------------------------------------------------------------------------
# matches_filters — list-intersection fields
# ---------------------------------------------------------------------------


def test_list_field_intersection_hit_miss_and_or() -> None:
    paper = {"topics": ["x", "y"]}
    assert matches_filters(paper, {"topic": ["x"]}) is True
    assert matches_filters(paper, {"topic": ["z"]}) is False
    # OR within field: any token in the wanted set is enough.
    assert matches_filters(paper, {"topic": ["z", "y"]}) is True


def test_list_field_methods_projects_data() -> None:
    paper = {"methods": ["BERT"], "projects": ["PepForge"], "data": ["GDP-2"]}
    assert matches_filters(paper, {"method": ["BERT"]}) is True
    assert matches_filters(paper, {"project": ["PepCodec"]}) is False
    assert matches_filters(paper, {"data": ["GDP-2", "other"]}) is True


def test_list_field_missing_or_none_is_miss_no_raise() -> None:
    # No topics field at all -> --topic x cannot match, must not raise.
    assert matches_filters({}, {"topic": ["x"]}) is False
    assert matches_filters({"topics": None}, {"topic": ["x"]}) is False
    assert matches_filters({"topics": []}, {"topic": ["x"]}) is False


# ---------------------------------------------------------------------------
# matches_filters — scalar enum fields (status / priority / type)
# ---------------------------------------------------------------------------


def test_scalar_or_within_field() -> None:
    paper = {"status": "skim"}
    assert matches_filters(paper, {"status": ["deep-read", "skim"]}) is True
    assert matches_filters(paper, {"status": ["deep-read"]}) is False


def test_scalar_none_value_does_not_match_named_token() -> None:
    # str(None or "") == "" so a None scalar never matches a real token.
    assert matches_filters({"priority": None}, {"priority": ["A"]}) is False
    assert matches_filters({}, {"type": ["research"]}) is False


# ---------------------------------------------------------------------------
# matches_filters — year (scalar int compared as string)
# ---------------------------------------------------------------------------


def test_year_int_compared_as_string_or() -> None:
    paper = {"year": 2023}
    assert matches_filters(paper, {"year": ["2023", "2024"]}) is True
    assert matches_filters(paper, {"year": ["2024"]}) is False


def test_year_none_is_miss() -> None:
    assert matches_filters({"year": None}, {"year": ["2023"]}) is False
    assert matches_filters({}, {"year": ["2023"]}) is False


# ---------------------------------------------------------------------------
# matches_filters — author substring (case-insensitive, OR)
# ---------------------------------------------------------------------------


def test_author_substring_case_insensitive_or() -> None:
    paper = {"authors": ["Jane Smith"]}
    assert matches_filters(paper, {"author": ["smith"]}) is True
    assert matches_filters(paper, {"author": ["jones", "smith"]}) is True
    assert matches_filters(paper, {"author": ["jones"]}) is False


def test_author_missing_is_miss_no_raise() -> None:
    assert matches_filters({}, {"author": ["smith"]}) is False
    assert matches_filters({"authors": [None]}, {"author": ["smith"]}) is False


# ---------------------------------------------------------------------------
# Cross-field AND + backward-compat single value
# ---------------------------------------------------------------------------


def test_cross_field_and() -> None:
    paper = {"status": "skim", "topics": ["x"]}
    assert matches_filters(paper, {"status": ["skim"], "topic": ["x"]}) is True
    # One side fails -> overall miss.
    assert matches_filters(paper, {"status": ["skim"], "topic": ["z"]}) is False
    assert matches_filters(paper, {"status": ["deep-read"], "topic": ["x"]}) is False


def test_none_filter_is_ignored() -> None:
    # A None filter value means "no constraint" and never excludes.
    paper = {"status": "skim", "topics": ["x"]}
    assert matches_filters(paper, {"status": None, "topic": None}) is True
    assert matches_filters(paper, {"status": ["skim"], "topic": None}) is True


def test_backward_compat_single_element_list() -> None:
    # Single value == single-element list (the iron law): behaves the same
    # as the legacy single-valued filters.
    paper = {
        "topics": ["x", "y"],
        "status": "skim",
        "priority": "A",
        "type": "research",
        "year": 2023,
        "authors": ["Jane Smith"],
    }
    assert matches_filters(paper, {"topic": ["x"]}) is True
    assert matches_filters(paper, {"status": ["skim"]}) is True
    assert matches_filters(paper, {"priority": ["A"]}) is True
    assert matches_filters(paper, {"type": ["research"]}) is True
    assert matches_filters(paper, {"year": ["2023"]}) is True
    assert matches_filters(paper, {"author": ["smith"]}) is True
    # All combined still matches (AND).
    assert matches_filters(
        paper,
        {
            "topic": ["x"],
            "status": ["skim"],
            "priority": ["A"],
            "type": ["research"],
            "year": ["2023"],
            "author": ["smith"],
        },
    ) is True


def test_empty_filters_dict_matches_everything() -> None:
    assert matches_filters({"id": "anything"}, {}) is True
