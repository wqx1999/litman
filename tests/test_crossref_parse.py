"""Tests for `litman.importers.crossref.parse_crossref` (no network)."""

from __future__ import annotations

from litman.importers.crossref import parse_crossref


def test_parse_full_message() -> None:
    message = {
        "title": ["HELM-GPT: De novo macrocyclic peptide design"],
        "author": [
            {"family": "Chen", "given": "Yi"},
            {"family": "Wang", "given": "Lin"},
            {"family": "Liu", "given": "Mei"},
        ],
        "published-print": {"date-parts": [[2024, 6, 1]]},
        "container-title": ["Bioinformatics"],
        "DOI": "10.1093/bioinformatics/btae364",
    }
    parsed = parse_crossref(message)
    assert parsed == {
        "title": "HELM-GPT: De novo macrocyclic peptide design",
        "authors": ["Chen, Yi", "Wang, Lin", "Liu, Mei"],
        "year": 2024,
        "journal": "Bioinformatics",
        "doi": "10.1093/bioinformatics/btae364",
    }


def test_parse_falls_back_year_to_published_online() -> None:
    message = {
        "title": ["X"],
        "author": [{"family": "A", "given": "B"}],
        "published-online": {"date-parts": [[2023]]},
        "container-title": ["J"],
        "DOI": "10.1/x",
    }
    assert parse_crossref(message)["year"] == 2023


def test_parse_falls_back_year_to_issued() -> None:
    message = {
        "title": ["X"],
        "author": [{"family": "A", "given": "B"}],
        "issued": {"date-parts": [[2022]]},
        "container-title": ["J"],
        "DOI": "10.1/x",
    }
    assert parse_crossref(message)["year"] == 2022


def test_parse_no_year_returns_none() -> None:
    message = {
        "title": ["X"],
        "author": [{"family": "A", "given": "B"}],
        "container-title": ["J"],
        "DOI": "10.1/x",
    }
    assert parse_crossref(message)["year"] is None


def test_parse_missing_title_is_empty() -> None:
    message = {
        "author": [{"family": "Smith", "given": "Jane"}],
        "issued": {"date-parts": [[2024]]},
        "DOI": "10.1/x",
    }
    parsed = parse_crossref(message)
    assert parsed["title"] == ""
    assert parsed["authors"] == ["Smith, Jane"]


def test_parse_missing_journal_is_empty() -> None:
    message = {
        "title": ["X"],
        "author": [{"family": "A", "given": "B"}],
        "issued": {"date-parts": [[2024]]},
        "DOI": "10.1/x",
    }
    assert parse_crossref(message)["journal"] == ""


def test_parse_author_with_only_family() -> None:
    message = {
        "title": ["X"],
        "author": [{"family": "Madonna"}],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["J"],
        "DOI": "10.1/x",
    }
    assert parse_crossref(message)["authors"] == ["Madonna"]


def test_parse_skips_empty_author_entries() -> None:
    message = {
        "title": ["X"],
        "author": [
            {"family": "Smith", "given": "Jane"},
            {},  # empty entry
            {"family": "", "given": ""},  # empty fields
        ],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["J"],
        "DOI": "10.1/x",
    }
    assert parse_crossref(message)["authors"] == ["Smith, Jane"]


def test_parse_no_authors_returns_empty_list() -> None:
    message = {
        "title": ["X"],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["J"],
        "DOI": "10.1/x",
    }
    assert parse_crossref(message)["authors"] == []
