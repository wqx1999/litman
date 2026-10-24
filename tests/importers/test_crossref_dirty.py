"""`parse_crossref` cleans what publishers deposit (no network).

The messages carry the fields as `api.crossref.org/works/<doi>` returned them
on 2026-10-21, trimmed to what parsing reads, with escape sequences for every
line break so the file means the same bytes on every platform.
"""

from __future__ import annotations

from typing import Any

from litman.importers.crossref import parse_crossref

_PP = "\n" + " " * 20

# Sample A: 10.1021/jacs.6c11517 (ACS pretty-print plus <i>).
_JACS: dict[str, Any] = {
    "title": [
        f"({_PP}<i>N</i>{_PP}-Hydroxy)amide-Mediated\nRapid Head-to-Tail "
        "Cyclization of Unprotected Peptides in Water"
    ],
    "author": [
        {"family": "Zhang", "given": "Yue"},
        {"family": "Wang", "given": "Weifeng"},
    ],
    "published-print": {"date-parts": [[2026, 10, 7]]},
    "container-title": ["Journal of the American\nChemical Society"],
    "DOI": "10.1021/jacs.6c11517",
    "type": "journal-article",
    "volume": "148",
    "issue": "39",
    "page": "42190-42199",
    "publisher": "American Chemical Society (ACS)",
}

# Sample B: 10.1021/acs.jcim.6c00587 (line breaks only).
_JCIM: dict[str, Any] = {
    "title": [
        "Advancing Biochemical\nMolecule Registration, Representation\nand "
        "Search for New Drug Modalities"
    ],
    "author": [
        {"family": "Pinto-Gil", "given": "Kevin"},
        {"family": "Noisier", "given": "Anaïs F. M."},
    ],
    "published-print": {"date-parts": [[2026, 7, 27]]},
    "container-title": ["Journal of Chemical Information\nand Modeling"],
    "DOI": "10.1021/acs.jcim.6c00587",
    "type": "journal-article",
}

# Sample C: 10.1093/bib/bbae714 (face markup, no line breaks).
_BIB: dict[str, Any] = {
    "title": [
        "<i>cyclicpeptide</i>: a Python package for cyclic peptide drug design"
    ],
    "author": [
        {"family": "Yang", "given": "Liu"},
        {"family": "Cao", "given": "Suqi"},
    ],
    "published-print": {"date-parts": [[2024, 11, 22]]},
    "container-title": ["Briefings in Bioinformatics"],
    "DOI": "10.1093/bib/bbae714",
    "type": "journal-article",
}


def test_sample_a_title_and_journal_come_out_clean() -> None:
    parsed = parse_crossref(_JACS)
    assert parsed["title"] == (
        "(N-Hydroxy)amide-Mediated Rapid Head-to-Tail Cyclization of "
        "Unprotected Peptides in Water"
    )
    assert parsed["journal"] == "Journal of the American Chemical Society"
    assert parsed["authors"] == ["Zhang, Yue", "Wang, Weifeng"]
    assert parsed["pages"] == "42190-42199"
    assert parsed["publisher"] == "American Chemical Society (ACS)"


def test_sample_b_line_breaks_become_spaces() -> None:
    parsed = parse_crossref(_JCIM)
    assert parsed["title"] == (
        "Advancing Biochemical Molecule Registration, Representation and "
        "Search for New Drug Modalities"
    )
    assert parsed["journal"] == "Journal of Chemical Information and Modeling"
    assert parsed["authors"] == ["Pinto-Gil, Kevin", "Noisier, Anaïs F. M."]


def test_sample_c_markup_is_dropped_and_its_text_kept() -> None:
    parsed = parse_crossref(_BIB)
    assert parsed["title"] == (
        "cyclicpeptide: a Python package for cyclic peptide drug design"
    )
    assert parsed["journal"] == "Briefings in Bioinformatics"
    assert parsed["authors"] == ["Yang, Liu", "Cao, Suqi"]


def test_every_string_field_is_cleaned() -> None:
    """Author names, the proceedings venue, publisher, volume, issue and pages
    pass the same door as the title, and an author whose name cleans to
    nothing is skipped like any empty entry."""
    parsed = parse_crossref({
        "title": ["T"],
        "author": [
            {"family": "<i>Zhang</i>", "given": "Wei\n   Ming"},
            {"family": "<b></b>", "given": " "},
        ],
        "issued": {"date-parts": [[2024]]},
        "container-title": ["Proceedings of\n  <i>NeurIPS</i>"],
        "DOI": "10.1/x",
        "type": "proceedings-article",
        "publisher": "Curran &amp; Associates",
        "volume": " 37\n",
        "issue": "<b>2</b>",
        "page": "\n  1-9 ",
    })
    assert parsed["authors"] == ["Zhang, Wei Ming"]
    assert parsed["booktitle"] == "Proceedings of NeurIPS"
    assert parsed["journal"] == ""
    assert parsed["publisher"] == "Curran & Associates"
    assert (parsed["volume"], parsed["issue"], parsed["pages"]) == ("37", "2", "1-9")
