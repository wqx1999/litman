"""Tests for `litman.importers.crossref.parse_crossref` (no network)."""

from __future__ import annotations

from litman.importers.crossref import parse_crossref


def test_parse_full_message() -> None:
    """A typical journal article with every bib-oriented M12.0 field."""
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
        "type": "journal-article",
        "volume": "40",
        "issue": "6",
        "page": "btae364",
        "publisher": "Oxford University Press",
    }
    parsed = parse_crossref(message)
    assert parsed == {
        "title": "HELM-GPT: De novo macrocyclic peptide design",
        "authors": ["Chen, Yi", "Wang, Lin", "Liu, Mei"],
        "year": 2024,
        "journal": "Bioinformatics",
        "doi": "10.1093/bioinformatics/btae364",
        "volume": "40",
        "issue": "6",
        "pages": "btae364",
        "publisher": "Oxford University Press",
        "venue-type": "journal-article",
        "booktitle": "",
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


# ---------------------------------------------------------------------------
# M12.0 — bib-oriented fields: 6 new fields + venue-type-driven routing
# ---------------------------------------------------------------------------


def test_parse_proceedings_article_routes_container_to_booktitle() -> None:
    """proceedings-article: container-title -> booktitle, journal empty."""
    message = {
        "title": ["Sparse Mixture-of-Experts for Peptide Encoding"],
        "author": [{"family": "Zhang", "given": "Wei"}],
        "issued": {"date-parts": [[2023]]},
        "container-title": ["NeurIPS Proceedings"],
        "DOI": "10.1/conf.2023.42",
        "type": "proceedings-article",
        "publisher": "NeurIPS Foundation",
        "page": "1234-1250",
    }
    parsed = parse_crossref(message)
    assert parsed["journal"] == ""
    assert parsed["booktitle"] == "NeurIPS Proceedings"
    assert parsed["venue-type"] == "proceedings-article"
    assert parsed["publisher"] == "NeurIPS Foundation"
    assert parsed["pages"] == "1234-1250"


def test_parse_book_chapter_routes_container_to_booktitle() -> None:
    """book-chapter: container-title -> booktitle, journal empty."""
    message = {
        "title": ["Chapter 5: Sequence Models for Biology"],
        "author": [{"family": "Doe", "given": "Jane"}],
        "issued": {"date-parts": [[2022]]},
        "container-title": ["Handbook of Computational Biology"],
        "DOI": "10.1/book.5",
        "type": "book-chapter",
        "publisher": "Springer",
        "volume": "3",
        "page": "112-145",
    }
    parsed = parse_crossref(message)
    assert parsed["journal"] == ""
    assert parsed["booktitle"] == "Handbook of Computational Biology"
    assert parsed["venue-type"] == "book-chapter"
    assert parsed["publisher"] == "Springer"
    assert parsed["volume"] == "3"


def test_parse_preprint_keeps_minimal_fields() -> None:
    """posted-content / preprint: venue-type set, vol/issue/pages empty."""
    message = {
        "title": ["A Preprint About Peptides"],
        "author": [{"family": "Smith", "given": "John"}],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["bioRxiv"],
        "DOI": "10.1101/2024.01.001",
        "type": "posted-content",
        # No volume / issue / page / publisher — typical for preprints
    }
    parsed = parse_crossref(message)
    assert parsed["venue-type"] == "posted-content"
    # bioRxiv is the "container" so by default goes to journal
    assert parsed["journal"] == "bioRxiv"
    assert parsed["booktitle"] == ""
    assert parsed["volume"] == ""
    assert parsed["issue"] == ""
    assert parsed["pages"] == ""
    assert parsed["publisher"] == ""


def test_parse_book_carries_publisher() -> None:
    """book: container-title may be empty; publisher populated."""
    message = {
        "title": ["Foundations of Cheminformatics"],
        "author": [{"family": "Lee", "given": "Anna"}],
        "issued": {"date-parts": [[2021]]},
        "DOI": "10.1/book.foundations",
        "type": "book",
        "publisher": "Wiley",
    }
    parsed = parse_crossref(message)
    assert parsed["venue-type"] == "book"
    assert parsed["publisher"] == "Wiley"
    # No container-title -> both journal and booktitle empty
    assert parsed["journal"] == ""
    assert parsed["booktitle"] == ""


def test_parse_missing_type_falls_back_to_journal_routing() -> None:
    """No venue-type: legacy 5-field behaviour preserved."""
    message = {
        "title": ["X"],
        "author": [{"family": "A", "given": "B"}],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["Some Journal"],
        "DOI": "10.1/x",
    }
    parsed = parse_crossref(message)
    assert parsed["journal"] == "Some Journal"
    assert parsed["booktitle"] == ""
    assert parsed["venue-type"] == ""
    # All M12.0 fields default to empty string, not None
    assert parsed["volume"] == ""
    assert parsed["issue"] == ""
    assert parsed["pages"] == ""
    assert parsed["publisher"] == ""


def test_parse_explicit_none_volume_normalizes_to_empty_string() -> None:
    """CrossRef occasionally returns null for absent fields — accept gracefully."""
    message = {
        "title": ["X"],
        "author": [{"family": "A", "given": "B"}],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["J"],
        "DOI": "10.1/x",
        "type": "journal-article",
        "volume": None,
        "issue": None,
        "page": None,
    }
    parsed = parse_crossref(message)
    assert parsed["volume"] == ""
    assert parsed["issue"] == ""
    assert parsed["pages"] == ""
