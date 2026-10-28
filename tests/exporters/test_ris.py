"""Unit tests for the RIS exporter."""

from __future__ import annotations

import pytest

from litman.exporters.authors import clean_author
from litman.exporters.ris import emit_entry, emit_ris, has_sentinel, ris_type_for


def _lines(record: str) -> list[str]:
    return record.split("\r\n")


# ---------------------------------------------------------------------------
# ris_type_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "venue_type, expected",
    [
        ("journal-article", "JOUR"),
        ("proceedings-article", "CPAPER"),
        ("posted-content", "UNPB"),
        ("preprint", "UNPB"),
        ("book", "BOOK"),
        ("book-chapter", "CHAP"),
        ("dissertation", "THES"),
        ("report", "RPRT"),
        ("patent", "PAT"),
    ],
)
def test_ris_type_known_mapping(venue_type: str, expected: str) -> None:
    assert ris_type_for(venue_type) == expected


@pytest.mark.parametrize("venue_type", [None, "", "dataset", "  "])
def test_ris_type_unknown_falls_back_to_gen(venue_type: str | None) -> None:
    assert ris_type_for(venue_type) == "GEN"


# ---------------------------------------------------------------------------
# clean_author
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Yang, ", "Yang"),
        ("Yang,", "Yang"),
        (" Yang ", "Yang"),
        ("Corso, Gabriele", "Corso, Gabriele"),
        ("Barati Farimani, Amir", "Barati Farimani, Amir"),
    ],
)
def test_clean_author(raw: str, expected: str) -> None:
    assert clean_author(raw) == expected


# ---------------------------------------------------------------------------
# emit_entry
# ---------------------------------------------------------------------------


def test_emit_entry_journal_article_full() -> None:
    record = emit_entry({
        "id": "2025_Mao_Multi-feature",
        "title": "Multi-feature fusion network",
        "authors": ["Mao, Yijun", "Weng, Yurong"],
        "year": 2025,
        "journal": "PLOS Computational Biology",
        "volume": "21",
        "issue": "10",
        "pages": "e1013622",
        "publisher": "PLOS",
        "doi": "10.1371/journal.pcbi.1013622",
        "venue-type": "journal-article",
    })
    assert _lines(record) == [
        "TY  - JOUR",
        "TI  - Multi-feature fusion network",
        "AU  - Mao, Yijun",
        "AU  - Weng, Yurong",
        "PY  - 2025",
        "T2  - PLOS Computational Biology",
        "VL  - 21",
        "IS  - 10",
        "SP  - e1013622",
        "PB  - PLOS",
        "DO  - 10.1371/journal.pcbi.1013622",
        "UR  - https://doi.org/10.1371/journal.pcbi.1013622",
        "LB  - 2025_Mao_Multi-feature",
        "ER  - ",
    ]


def test_emit_entry_page_range_splits_into_start_and_end() -> None:
    lines = _lines(emit_entry({"id": "x", "pages": "45-67"}))
    assert "SP  - 45" in lines
    assert "EP  - 67" in lines


def test_emit_entry_family_only_author_loses_dangling_comma() -> None:
    lines = _lines(emit_entry({"id": "x", "authors": ["Yang, "]}))
    assert "AU  - Yang" in lines


def test_emit_entry_arxiv_preprint_links_abs_page_and_notes_id() -> None:
    lines = _lines(emit_entry({
        "id": "2022_Corso_DiffDock",
        "title": "DiffDock",
        "arxiv-id": "2210.01776",
        "venue-type": "preprint",
    }))
    assert lines[0] == "TY  - UNPB"
    assert "UR  - https://arxiv.org/abs/2210.01776" in lines
    assert "N1  - arXiv:2210.01776" in lines
    assert not any(line.startswith("DO  - ") for line in lines)


def test_emit_entry_arxiv_with_doi_links_the_doi() -> None:
    lines = _lines(emit_entry({
        "id": "x", "doi": "10.1/abc", "arxiv-id": "2101.00001",
    }))
    assert "UR  - https://doi.org/10.1/abc" in lines
    assert "N1  - arXiv:2101.00001" in lines
    assert sum(line.startswith("UR  - ") for line in lines) == 1


def test_emit_entry_proceedings_puts_booktitle_in_t2() -> None:
    lines = _lines(emit_entry({
        "id": "x", "booktitle": "NeurIPS Proceedings",
        "venue-type": "proceedings-article",
    }))
    assert lines[0] == "TY  - CPAPER"
    assert "T2  - NeurIPS Proceedings" in lines


def test_emit_entry_sparse_drops_empty_fields() -> None:
    lines = _lines(emit_entry({
        "id": "x", "title": "T", "journal": "", "volume": "", "doi": None,
        "authors": [],
    }))
    assert lines == ["TY  - GEN", "TI  - T", "LB  - x", "ER  - "]


def test_emit_entry_keeps_values_plain_and_on_one_line() -> None:
    """No bibtex escaping or braces; a line break inside a value is folded."""
    lines = _lines(emit_entry({
        "id": "x",
        "title": "β-VAE & 100% {braces}\nsecond line",
    }))
    assert "TI  - β-VAE & 100% {braces} second line" in lines


def test_emit_entry_missing_id_raises() -> None:
    with pytest.raises(ValueError, match="id"):
        emit_entry({"title": "No id"})


# ---------------------------------------------------------------------------
# emit_ris / has_sentinel
# ---------------------------------------------------------------------------


def test_emit_ris_sentinel_then_records_with_crlf() -> None:
    text = emit_ris(
        [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
        "Generated by litman vX on T.",
    )
    assert text.startswith("Generated by litman vX on T.\r\n\r\nTY  - GEN\r\n")
    assert text.count("ER  - ") == 2
    assert "\r\n\r\nTY  - GEN\r\nTI  - B" in text
    assert text.endswith("ER  - \r\n")
    assert "\n" not in text.replace("\r\n", "")


def test_emit_ris_empty_list_writes_only_sentinel() -> None:
    assert emit_ris([], "Generated by litman") == "Generated by litman\r\n"


def test_emit_ris_skips_id_less_entry() -> None:
    text = emit_ris([{"title": "No id"}, {"id": "ok"}], "Generated by litman")
    assert text.count("ER  - ") == 1
    assert "LB  - ok" in text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Generated by litman v1 on T.\r\n\r\nTY  - GEN\r\n", True),
        ("﻿Generated by litman v1 on T.\r\n", True),
        ("\r\n\r\nGenerated by litman v1\r\n", True),
        ("TY  - JOUR\r\nTI  - Mine\r\nER  - \r\n", False),
        ("", False),
    ],
)
def test_has_sentinel(text: str, expected: bool) -> None:
    assert has_sentinel(text) is expected
