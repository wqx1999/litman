"""Unit tests for the ACS citation formatter (``litman.core.cite``).

The journal abbreviation table is the shipped CC0 dataset
(``litman/data/journal_abbrev.csv``), so the table-hit tests double as a
smoke test that the vendored file is present and parseable.
"""

from __future__ import annotations

import pytest

from litman.core.cite import Citation, abbreviate_journal, format_acs

# ---------------------------------------------------------------------------
# abbreviate_journal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "full, abbrev",
    [
        ("Journal of the American Chemical Society", "J. Am. Chem. Soc."),
        ("Journal of Medicinal Chemistry", "J. Med. Chem."),
        ("Journal of Chemical Information and Modeling", "J. Chem. Inf. Model."),
        ("Nucleic Acids Research", "Nucleic Acids Res."),
    ],
)
def test_abbreviate_known_journal(full: str, abbrev: str) -> None:
    out, known = abbreviate_journal(full)
    assert (out, known) == (abbrev, True)


def test_abbreviate_is_case_and_article_insensitive() -> None:
    # Leading "The", odd casing, and extra spaces all normalize to one key.
    out, known = abbreviate_journal("the   JOURNAL of the American Chemical Society")
    assert (out, known) == ("J. Am. Chem. Soc.", True)


def test_abbreviate_single_word_is_self_abbreviating() -> None:
    # ISO4 never abbreviates a one-word title — it is its own abbreviation and
    # must NOT be flagged as unknown.
    assert abbreviate_journal("Bioinformatics") == ("Bioinformatics", True)


def test_abbreviate_unknown_multiword_is_flagged() -> None:
    out, known = abbreviate_journal("Journal of Imaginary Nonexistent Studies")
    assert out == "Journal of Imaginary Nonexistent Studies"
    assert known is False


def test_abbreviate_empty_is_unknown() -> None:
    assert abbreviate_journal("   ") == ("", False)


# ---------------------------------------------------------------------------
# format_acs
# ---------------------------------------------------------------------------


def test_format_full_journal_article() -> None:
    meta = {
        "id": "smith2021",
        "journal": "Journal of the American Chemical Society",
        "year": 2021,
        "volume": "143",
        "pages": "1234-1240",
    }
    cite = format_acs(meta)
    assert isinstance(cite, Citation)
    assert cite.text == "J. Am. Chem. Soc. 2021, 143, 1234-1240."
    assert cite.warnings == []


def test_format_missing_pages_warns_and_drops_pages() -> None:
    cite = format_acs(
        {"journal": "Journal of Medicinal Chemistry", "year": 2020, "volume": "63"}
    )
    assert cite.text == "J. Med. Chem. 2020, 63."
    assert any("pages" in w for w in cite.warnings)


def test_format_missing_volume_warns() -> None:
    cite = format_acs(
        {"journal": "Journal of Medicinal Chemistry", "year": 2020, "pages": "10-20"}
    )
    assert cite.text == "J. Med. Chem. 2020, 10-20."
    assert any("volume" in w for w in cite.warnings)


def test_format_unknown_journal_used_verbatim_with_warning() -> None:
    cite = format_acs(
        {"journal": "Journal of Imaginary Nonexistent Studies", "year": 2019, "volume": "1"}
    )
    assert cite.text.startswith("Journal of Imaginary Nonexistent Studies 2019, 1")
    assert any("abbreviation" in w for w in cite.warnings)


def test_format_preprint_uses_arxiv_id() -> None:
    cite = format_acs({"arxiv-id": "2101.12345", "year": 2021})
    assert "arXiv:2101.12345" in cite.text
    assert any("preprint" in w for w in cite.warnings)


def test_format_proceedings_uses_booktitle_with_warning() -> None:
    cite = format_acs(
        {"booktitle": "Advances in Neural Information Processing Systems", "year": 2022}
    )
    assert cite.text.startswith("Advances in Neural Information Processing Systems 2022")
    assert any("non-journal" in w for w in cite.warnings)


def test_format_empty_metadata_is_placeholder() -> None:
    cite = format_acs({"id": "x"})
    assert cite.text == "(insufficient metadata for a citation)"
    assert cite.warnings  # at least the "no venue" warning


def test_format_does_not_double_period() -> None:
    # A journal abbreviation already ends in a period; the trailing-period rule
    # must not produce "Soc.." at the end.
    cite = format_acs(
        {"journal": "Journal of the American Chemical Society", "year": 2021,
         "volume": "143", "pages": "1"}
    )
    assert cite.text.endswith("1.")
    assert not cite.text.endswith("..")
