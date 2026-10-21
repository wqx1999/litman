"""Tests for `litman.core.textclean` — metadata text as one plain line.

The dirty rows are Crossref titles and journal names as the REST API returns
them (re-fetched 2026-10-21), written with escape sequences so every line
break and indent is visible, and so the file means the same bytes on every
platform. Sample A is `10.1021/jacs.6c11517`, B `10.1021/acs.jcim.6c00587`,
C `10.1093/bib/bbae714`; the rest come from a probe of ~1150 recent titles.
"""

from __future__ import annotations

import pytest

from litman.core.textclean import clean_metadata_text, dirty_text_fields

_PP = "\n" + " " * 20  # one pretty-printed line break, as ACS deposits it
_MATH = '<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML">'


def _mathml(symbol: str) -> str:
    """One inline MathML identifier, pretty-printed the way Crossref returns it."""
    inner = "\n" + " " * 22
    return (
        f"{_MATH}{inner}<mml:mrow>{inner}  <mml:mi>{symbol}</mml:mi>"
        f"{inner}</mml:mrow>{_PP}</mml:math>"
    )


_ROWS: list[tuple[str, str]] = [
    # The three samples the bug was found on.
    (
        f"({_PP}<i>N</i>{_PP}-Hydroxy)amide-Mediated\nRapid Head-to-Tail "
        "Cyclization of Unprotected Peptides in Water",
        "(N-Hydroxy)amide-Mediated Rapid Head-to-Tail Cyclization of "
        "Unprotected Peptides in Water",
    ),
    (
        "Journal of the American\nChemical Society",
        "Journal of the American Chemical Society",
    ),
    (
        "Advancing Biochemical\nMolecule Registration, Representation\nand "
        "Search for New Drug Modalities",
        "Advancing Biochemical Molecule Registration, Representation and "
        "Search for New Drug Modalities",
    ),
    (
        "Journal of Chemical Information\nand Modeling",
        "Journal of Chemical Information and Modeling",
    ),
    (
        "<i>cyclicpeptide</i>: a Python package for cyclic peptide drug design",
        "cyclicpeptide: a Python package for cyclic peptide drug design",
    ),
    # Entities, markup and whitespace on their own.
    ("R&D", "R&D"),
    ("Salt &amp; Pepper", "Salt & Pepper"),
    ("CO<sub>2</sub> capture", "CO2 capture"),
    ("A B\r\nC\tD", "A B C D"),
    ("Ca<sup>2+</sup> Signaling", "Ca2+ Signaling"),
    ("non\xa0breaking  spaces ", "non breaking spaces"),
    ("&#233;tude &#x3b1;-helix", "étude α-helix"),
    ("&amp;amp; twice", "& twice"),
    ("&amp;lt;i&amp;gt;N&amp;lt;/i&amp;gt;-acyl", "N-acyl"),
    # A legacy entity name without its semicolon is a word, not an escape.
    ("Prot&not; and &para", "Prot¬ and &para"),
    ("a &notanentity text", "a &notanentity text"),
    # Angle brackets that are not markup are text.
    ("p < 0.05 in A<B>C", "p < 0.05 in A<B>C"),
    ("x<y and y>z", "x<y and y>z"),
    # Already clean, and empty.
    (
        "HELM-GPT: De novo macrocyclic peptide design",
        "HELM-GPT: De novo macrocyclic peptide design",
    ),
    ("", ""),
    ("   ", ""),
    # Non-ASCII passes through untouched.
    ("Anaïs F. M.", "Anaïs F. M."),
    ("关于化合物A的合成方法", "关于化合物A的合成方法"),
    # Inline elements on their own pretty-printed line where the original had
    # a space: joining them up would glue words together.
    (
        f"Rapid Screening of Methicillin-Resistant{_PP}<i>Staphylococcus "
        f"aureus</i>{_PP}Using MALDI-TOF MS",
        "Rapid Screening of Methicillin-Resistant Staphylococcus aureus Using "
        "MALDI-TOF MS",
    ),
    (
        f"via Selective{_PP}<i>Trans</i>{_PP}-Cleavage Preferences",
        "via Selective Trans-Cleavage Preferences",
    ),
    (
        f"for Efficient ε-Poly-{_PP}<scp>l</scp>{_PP}-lysine Production",
        "for Efficient ε-Poly-l-lysine Production",
    ),
    (
        f"via quantum{_PP}{_mathml('α')}{_PP}–{_PP}{_mathml('z')}{_PP}"
        "Jensen-Tsallis divergence",
        "via quantum α–z Jensen-Tsallis divergence",
    ),
    # Self-closing, attributes, namespaced; a line break tag separates words.
    ("line<br/>break<br />here", "line break here"),
    ('<span class="x">Spanned</span> <jats:italic>word</jats:italic>', "Spanned word"),
]


@pytest.mark.parametrize(("dirty", "clean"), _ROWS)
def test_clean_metadata_text(dirty: str, clean: str) -> None:
    assert clean_metadata_text(dirty) == clean


@pytest.mark.parametrize(("dirty", "clean"), _ROWS)
def test_cleaning_twice_changes_nothing(dirty: str, clean: str) -> None:
    once = clean_metadata_text(dirty)
    assert clean_metadata_text(once) == once


def test_markup_spelled_by_a_dropped_tag_is_dropped_too() -> None:
    """Removing one tag can spell another, or an entity; the result is still a
    fixed point, so a second clean never changes what the first wrote."""
    assert clean_metadata_text("<<i></i>i>x</i>") == "x"
    assert clean_metadata_text("R&am<i></i>p;D") == "R&D"


def test_dirty_text_fields_names_only_what_cleaning_changes() -> None:
    meta = {
        "title": "Advancing Biochemical\nMolecule Registration",
        "journal": "Journal of Chemical Information and Modeling",
        "booktitle": None,
        "publisher": "<i>ACS</i>",
        "authors": ["Pinto-Gil, Kevin", "Noisier,\n  Anaïs", 42],
        "doi": "10.1021/acs.jcim.6c00587",
    }
    assert dirty_text_fields(meta) == {
        "title": "Advancing Biochemical Molecule Registration",
        "publisher": "ACS",
        # Whole list, in order; a value that is not a string stays as it was.
        "authors": ["Pinto-Gil, Kevin", "Noisier, Anaïs", 42],
    }


def test_dirty_text_fields_is_empty_for_clean_or_missing_fields() -> None:
    assert dirty_text_fields({"title": "Clean title", "authors": ["A, B"]}) == {}
    assert dirty_text_fields({}) == {}
    assert dirty_text_fields({"title": 1984, "authors": "Doe, J."}) == {}
