"""Unit tests for the bibtex exporter (M12.1)."""

from __future__ import annotations

import pytest

from litman.exporters.bibtex import (
    emit_bib,
    emit_entry,
    entry_type_for,
    escape_bibtex,
    has_sentinel,
)


# ---------------------------------------------------------------------------
# entry_type_for — CrossRef type to bibtex entry mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "venue_type, expected",
    [
        ("journal-article", "article"),
        ("proceedings-article", "inproceedings"),
        ("posted-content", "misc"),
        ("preprint", "misc"),
        ("book", "book"),
        ("book-chapter", "incollection"),
        ("dissertation", "phdthesis"),
        ("report", "techreport"),
    ],
)
def test_entry_type_known_mapping(venue_type: str, expected: str) -> None:
    assert entry_type_for(venue_type) == expected


def test_entry_type_none_falls_back_to_misc() -> None:
    assert entry_type_for(None) == "misc"


def test_entry_type_empty_string_falls_back_to_misc() -> None:
    assert entry_type_for("") == "misc"


def test_entry_type_unknown_value_falls_back_to_misc() -> None:
    assert entry_type_for("strange-future-type") == "misc"


def test_entry_type_strips_surrounding_whitespace() -> None:
    """Tolerate stray whitespace from hand-edited metadata."""
    assert entry_type_for("  journal-article  ") == "article"


# ---------------------------------------------------------------------------
# escape_bibtex — character escaping
# ---------------------------------------------------------------------------


def test_escape_empty_string() -> None:
    assert escape_bibtex("") == ""


def test_escape_ampersand() -> None:
    assert escape_bibtex("a & b") == "a \\& b"


def test_escape_underscore() -> None:
    assert escape_bibtex("foo_bar") == "foo\\_bar"


def test_escape_percent() -> None:
    assert escape_bibtex("50%") == "50\\%"


def test_escape_dollar_hash() -> None:
    assert escape_bibtex("$x #y") == "\\$x \\#y"


def test_escape_braces() -> None:
    assert escape_bibtex("a {b} c") == "a \\{b\\} c"


def test_escape_backslash_first() -> None:
    """Backslash escape must run first so its output isn't double-escaped."""
    # A raw backslash in input becomes \textbackslash{} in output. The
    # subsequent brace pass must NOT escape the braces of \textbackslash{}.
    out = escape_bibtex("foo\\bar")
    assert out == "foo\\textbackslash{}bar"


def test_escape_unicode_passthrough() -> None:
    """UTF-8 input survives unchanged — biblatex+biber handle accents."""
    assert escape_bibtex("Müller") == "Müller"
    assert escape_bibtex("naïve résumé") == "naïve résumé"


# ---------------------------------------------------------------------------
# emit_entry — single-record rendering
# ---------------------------------------------------------------------------


_FULL_META: dict[str, object] = {
    "id": "2024_Chen_HELM-GPT",
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


def test_emit_entry_full_metadata() -> None:
    out = emit_entry(_FULL_META)
    assert out.startswith("@article{2024_Chen_HELM-GPT,")
    # Title wrapped in extra braces to defeat biblatex lowercasing.
    assert "title = {{HELM-GPT: De novo macrocyclic peptide design}}," in out
    # Authors joined with ' and '.
    assert "author = {Chen, Yi and Wang, Lin and Liu, Mei}," in out
    assert "year = {2024}," in out
    assert "journal = {Bioinformatics}," in out
    # CrossRef 'issue' renders as bibtex 'number'.
    assert "number = {6}," in out
    assert "volume = {40}," in out
    assert "publisher = {Oxford University Press}," in out
    assert "doi = {10.1093/bioinformatics/btae364}," in out
    assert out.endswith("\n}")


def test_emit_entry_sparse_skips_empty_fields() -> None:
    """A minimal preprint produces a clean @misc with only present fields."""
    meta = {
        "id": "2023_Doe_Preprint",
        "title": "A Preprint",
        "authors": ["Doe, Jane"],
        "year": 2023,
        "venue-type": "posted-content",
        # everything else empty / missing
    }
    out = emit_entry(meta)
    assert out.startswith("@misc{2023_Doe_Preprint,")
    assert "title = {{A Preprint}}," in out
    assert "author = {Doe, Jane}," in out
    assert "year = {2023}," in out
    # None of the absent fields should appear at all.
    for absent in ("volume", "number", "pages", "publisher", "journal",
                   "booktitle", "doi"):
        assert f"{absent} = " not in out, f"unexpected empty {absent!r} key"


def test_emit_entry_proceedings_uses_booktitle_not_journal() -> None:
    meta = {
        "id": "2023_Zhang_MoE",
        "title": "Sparse MoE",
        "authors": ["Zhang, Wei"],
        "year": 2023,
        "venue-type": "proceedings-article",
        "journal": "",
        "booktitle": "NeurIPS Proceedings",
        "pages": "1234-1250",
        "publisher": "NeurIPS Foundation",
    }
    out = emit_entry(meta)
    assert out.startswith("@inproceedings{2023_Zhang_MoE,")
    assert "booktitle = {NeurIPS Proceedings}," in out
    assert "journal = " not in out
    # Page range normalized to bibtex double-dash convention.
    assert "pages = {1234--1250}," in out


def test_emit_entry_missing_id_raises() -> None:
    """Without an id we have no cite key — refuse to render."""
    with pytest.raises(ValueError, match="cite key"):
        emit_entry({"title": "x", "authors": ["Doe, J."]})


def test_emit_entry_arxiv_preprint_emits_eprint_and_url() -> None:
    # Review F1: an arXiv preprint (arxiv-id, no DOI) must carry a locator,
    # not export as an unresolvable @misc.
    meta = {
        "id": "2024_Doe_Diffusion",
        "title": "Diffusion Models",
        "authors": ["Doe, Jane"],
        "year": 2024,
        "venue-type": "preprint",
        "arxiv-id": "2401.12345",
    }
    out = emit_entry(meta)
    assert "eprint = {2401.12345}," in out
    assert "archivePrefix = {arXiv}," in out
    assert "url = {https://arxiv.org/abs/2401.12345}," in out


def test_emit_entry_arxiv_with_doi_skips_redundant_url() -> None:
    # When a DOI is present it is the canonical locator; eprint is still
    # emitted (useful for arXiv) but the redundant abs URL is not.
    meta = {
        "id": "2024_Doe_Published",
        "title": "Published Later",
        "authors": ["Doe, Jane"],
        "year": 2024,
        "venue-type": "journal-article",
        "doi": "10.1/x",
        "arxiv-id": "2401.99999",
    }
    out = emit_entry(meta)
    assert "doi = {10.1/x}," in out
    assert "eprint = {2401.99999}," in out
    assert "url = " not in out


def test_emit_entry_pages_single_value_passes_through() -> None:
    """A non-range pages value (e.g. 'e12345') is left alone, just escaped."""
    meta = {
        "id": "2024_Smith_X",
        "title": "X",
        "authors": ["Smith, J."],
        "year": 2024,
        "venue-type": "journal-article",
        "pages": "e12345",
    }
    out = emit_entry(meta)
    assert "pages = {e12345}," in out


def test_emit_entry_unicode_title_preserved() -> None:
    meta = {
        "id": "2024_Müller_X",
        "title": "Naïve approach to résumé optimisation",
        "authors": ["Müller, Hans"],
        "year": 2024,
        "venue-type": "journal-article",
    }
    out = emit_entry(meta)
    assert "Naïve approach to résumé optimisation" in out
    assert "Müller, Hans" in out


def test_emit_entry_escapes_special_chars_in_title() -> None:
    meta = {
        "id": "2024_X_Y",
        "title": "Foo & Bar: 50% gain via X_test",
        "authors": ["X, Y"],
        "year": 2024,
        "venue-type": "journal-article",
    }
    out = emit_entry(meta)
    assert "Foo \\& Bar: 50\\% gain via X\\_test" in out


def test_emit_entry_no_year_omits_year_field() -> None:
    """Year=None should simply skip the year key, not emit `year = {None}`."""
    meta = {
        "id": "x_id",
        "title": "X",
        "authors": ["X, Y"],
        "year": None,
        "venue-type": "journal-article",
    }
    out = emit_entry(meta)
    assert "year = " not in out


# ---------------------------------------------------------------------------
# emit_bib — multi-record file rendering
# ---------------------------------------------------------------------------


_SENTINEL = "% Generated by litman v0.9.0 on 2026-05-14T15:23:00+08:00. Do not hand-edit — re-run `lit export`."


def test_emit_bib_empty_list_writes_only_sentinel() -> None:
    out = emit_bib([], _SENTINEL)
    assert out == f"{_SENTINEL}\n"


def test_emit_bib_multiple_entries_separated_by_blank_line() -> None:
    out = emit_bib([_FULL_META, {
        "id": "2023_Doe_Preprint",
        "title": "A Preprint",
        "authors": ["Doe, Jane"],
        "year": 2023,
        "venue-type": "posted-content",
    }], _SENTINEL)
    # First line is sentinel.
    assert out.splitlines()[0] == _SENTINEL
    # Both entries present.
    assert "@article{2024_Chen_HELM-GPT," in out
    assert "@misc{2023_Doe_Preprint," in out
    # Trailing newline keeps POSIX tools happy.
    assert out.endswith("\n")


def test_emit_bib_preserves_input_order() -> None:
    """Caller decides the sort order; emit_bib doesn't impose one."""
    a = {"id": "2024_A_x", "title": "A", "authors": ["A, B"], "year": 2024,
         "venue-type": "journal-article"}
    b = {"id": "2020_Z_y", "title": "Z", "authors": ["Z, Q"], "year": 2020,
         "venue-type": "journal-article"}
    out_ab = emit_bib([a, b], _SENTINEL)
    out_ba = emit_bib([b, a], _SENTINEL)
    assert out_ab.index("2024_A_x") < out_ab.index("2020_Z_y")
    assert out_ba.index("2020_Z_y") < out_ba.index("2024_A_x")


def test_emit_bib_skips_id_less_entry_not_all_or_nothing() -> None:
    # Review F2: a single id-less paper must not crash the whole export. It is
    # skipped; the valid entries still render.
    good = {"id": "2024_Good_X", "title": "Good", "authors": ["A, B"],
            "year": 2024, "venue-type": "journal-article"}
    bad = {"title": "No Id", "authors": ["C, D"], "year": 2024}
    out = emit_bib([good, bad], _SENTINEL)
    assert "@article{2024_Good_X," in out
    assert "No Id" not in out


# ---------------------------------------------------------------------------
# has_sentinel — overwrite-protection helper
# ---------------------------------------------------------------------------


def test_has_sentinel_true_when_first_line_is_litman_marker() -> None:
    assert has_sentinel(f"{_SENTINEL}\n\n@article{{x,}}\n") is True


def test_has_sentinel_false_when_file_is_hand_edited() -> None:
    text = "@article{x,\n  title = {Hand-written},\n}\n"
    assert has_sentinel(text) is False


def test_has_sentinel_false_when_first_line_unrelated_comment() -> None:
    text = "% My own bibliography\n\n@article{x,}\n"
    assert has_sentinel(text) is False


def test_has_sentinel_ignores_leading_blank_lines() -> None:
    """A litman-generated file with extra whitespace at the top still counts."""
    text = f"\n\n{_SENTINEL}\n\n@article{{x,}}\n"
    assert has_sentinel(text) is True


def test_has_sentinel_empty_file_is_false() -> None:
    assert has_sentinel("") is False
