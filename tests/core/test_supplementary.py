"""Tests for ``litman.core.supplementary`` — SI verification, naming, add/remove.

PDFs are built with the ``make_text_pdf`` fixture (tests/conftest.py): an SI's
first page repeating its paper's title, an article's first page, another
paper's SI. Files offered as SI sit in ``tmp_path`` — outside the vault, as a
download folder would.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from litman.core import atomic as atomic_mod
from litman.core import supplementary as si_mod
from litman.core.atomic import recover_staging
from litman.core.checks import (
    AUTO_FIXABLE_CATEGORIES,
    apply_autofix,
    check_supplementary,
    run_all_checks,
    run_push_integrity_errors,
)
from litman.core.document import find_paper, list_papers
from litman.core.library import create_vault
from litman.core.locking import lock_truth_file, unlock_truth_file
from litman.core.portable_link import make_portable_link
from litman.core.supplementary import (
    SiItem,
    add_supplementary,
    doi_tokens,
    field_problems,
    list_supplementary,
    name_matches,
    plan_names,
    prepare_items,
    remove_supplementary,
    si_extension,
    supplementary_drift,
    verify_batch,
)
from litman.core.trash import TRASH_DIRNAME
from litman.core.views import write_index
from litman.exceptions import PaperNotFoundError, SupplementaryError

_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
# Read once at import (ci.md, trap 1): a test body must never consult the live
# value, which a fixture elsewhere may have patched.
_REAL_WIN32 = sys.platform == "win32"
_not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the permission bits these tests take away",
)
_undecodable_names = pytest.mark.skipif(
    sys.platform != "linux" or sys.getfilesystemencoding() != "utf-8",
    reason="needs Linux (APFS refuses a file name that is not UTF-8; a Windows "
    "name is UTF-16) and a UTF-8 file-name encoding, under which a bad byte "
    "becomes a lone surrogate",
)

A_ID = "2019_Yang_Concepts"
A_DOI = "10.1021/acs.jcim.9b00237"
A_TITLE = "Concepts of Artificial Intelligence for Computer-Assisted Drug Discovery"
A_PAGE1 = [
    A_TITLE,
    "Xin Yang, Yifei Wang, Ryan Byrne, Gisbert Schneider",
    "Abstract: machine learning methods reshape how molecules are proposed",
    "and ranked in early drug discovery campaigns across the industry.",
    "Supporting Information is available free of charge.",
]
B_ID = "2023_Zhong_Peptides"
B_DOI = "10.1038/s41467-023-38851-5"
B_TITLE = "Deep generative design of cyclic peptides binding protein targets"
# Three content words: too short to decide anything by title (Q1).
C_ID = "2021_Zhang_Helm"
C_DOI = "10.1021/ci3001925"
C_TITLE = "HELM Software for Biopolymers"
# Its DOI number "2836" sits inside Elsevier-style digit runs (Q1).
D_ID = "2020_Lee_Stapling"
D_DOI = "10.1002/psc.2836"
D_TITLE = "Peptide stapling chemistry reviewed across many recent studies"

MakePdf = Callable[..., Path]


def _write_paper(
    vault: Path,
    make_text_pdf: MakePdf,
    paper_id: str,
    *,
    doi: str,
    title: str,
    page1: list[str] | None = None,
) -> Path:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\n"
        f"title: {title}\n"
        "authors:\n  - Doe, Jane\n"
        "year: 2020\n"
        f"doi: {doi}\n"
        "status: inbox\n"
        "created-at: '2026-01-01T00:00:00+00:00'\n"
        "updated-at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    if page1 is not None:
        article = make_text_pdf([page1], name=f"{paper_id}-article.pdf")
        shutil.move(str(article), str(paper_dir / "paper.pdf"))
    return paper_dir


@pytest.fixture
def vault(tmp_path: Path, make_text_pdf: MakePdf) -> Path:
    """Four papers: A has a text article; B, C, D are other library papers."""
    v = create_vault(tmp_path)
    _write_paper(v, make_text_pdf, A_ID, doi=A_DOI, title=A_TITLE, page1=A_PAGE1)
    _write_paper(
        v, make_text_pdf, B_ID, doi=B_DOI, title=B_TITLE,
        page1=[B_TITLE, "We design macrocycles with a diffusion model."],
    )
    _write_paper(v, make_text_pdf, C_ID, doi=C_DOI, title=C_TITLE)
    _write_paper(v, make_text_pdf, D_ID, doi=D_DOI, title=D_TITLE)
    write_index(v, list_papers(v))
    return v


def _si_pdf(make_text_pdf: MakePdf, name: str, title: str) -> Path:
    """An SI PDF: a cover page that repeats the paper's title, then tables."""
    return make_text_pdf(
        [
            [
                "Supporting Information",
                title,
                "Table S1 lists every training hyperparameter we tuned",
                "Figure S1 shows the validation curves per fold",
            ],
            ["Table S2 dataset statistics and split sizes"],
        ],
        name=name,
    )


def _blob(tmp_path: Path, name: str, payload: bytes = b"PK\x03\x04 sheet") -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _verdict(vault: Path, paper_id: str, path: Path, original: str | None = None):
    return verify_batch(vault, paper_id, [SiItem(path, original or path.name)])[0]


# ---------------------------------------------------------------------------
# §核验 2 — DOI numbers in file names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doi", "tokens"),
    [
        ("10.1021/acs.jcim.9b00237", {"9b00237"}),
        ("10.1021/ci3001925", {"ci3001925"}),
        ("10.1038/s41467-023-38851-5", {"s41467", "38851"}),
        ("10.1039/d4sc07642g", {"d4sc07642g"}),
        ("10.1093/bib/bbaf367", {"bbaf367"}),
        ("10.1002/psc.3666", {"3666"}),
        ("10.1126/science.abj8754", {"abj8754"}),
        ("10.1073/pnas.2016239118", {"2016239118"}),
        ("10.1101/2026.08.14.744816", {"744816"}),
        ("10.1016/j.cell.2020.01.021", set()),
        # Bare years go; a piece that only contains one stays.
        ("10.1000/abc.1999.x2020y", {"x2020y"}),
        # Resolver prefix and case do not matter.
        ("https://doi.org/10.1021/ACS.JCIM.9B00237", {"9b00237"}),
        ("", set()),
        ("no slash at all 12345", set()),
    ],
)
def test_doi_tokens(doi: str, tokens: set[str]) -> None:
    assert doi_tokens(doi) == tokens


@pytest.mark.parametrize(
    ("filename", "doi"),
    [
        ("ci9b00237_si_001.pdf", "10.1021/acs.jcim.9b00237"),  # ACS
        ("d4sc07642g1.pdf", "10.1039/d4sc07642g"),  # RSC
        ("13321_2023_748_MOESM1_ESM.pdf", "10.1186/s13321-023-00748-2"),  # BMC
        ("41467_2023_38851_MOESM1_ESM.pdf", "10.1038/s41467-023-38851-5"),  # Nature
        ("pnas.2016239118.sapp.pdf", "10.1073/pnas.2016239118"),  # PNAS
        ("bbaf367_supplementary_data.pdf", "10.1093/bib/bbaf367"),  # OUP
        ("science.abj8754_sm.pdf", "10.1126/science.abj8754"),  # Science
        ("psc3666-sup-0001-suppinfo.pdf", "10.1002/psc.3666"),  # Wiley
        ("CI9B00237_SI_002.XLSX", "10.1021/acs.jcim.9b00237"),  # any case
    ],
)
def test_name_matches_publisher_si_names(filename: str, doi: str) -> None:
    assert name_matches(filename, doi)


@pytest.mark.parametrize(
    ("filename", "doi"),
    [
        # A 4-digit number inside an Elsevier PII is not that paper's number.
        ("1-s2.0-S0022283620301662-mmc1.pdf", "10.1002/psc.2836"),
        # A digit-shaped token must not run into a neighbouring digit.
        ("x19b00237.pdf", "10.1021/acs.jcim.9b00237"),
        ("9b002371.pdf", "10.1021/acs.jcim.9b00237"),
        # Springer rule: same journal and article number, nothing less.
        ("13321_2023_749_MOESM1_ESM.pdf", "10.1186/s13321-023-00748-2"),
        ("12345_2023_748_MOESM1_ESM.pdf", "10.1186/s13321-023-00748-2"),
        ("Table_S2.xlsx", "10.1021/acs.jcim.9b00237"),
        ("media-1.pdf", "10.1016/j.cell.2020.01.021"),
    ],
)
def test_name_matches_refuses_near_misses(filename: str, doi: str) -> None:
    assert not name_matches(filename, doi)


# ---------------------------------------------------------------------------
# §核验 1 — the verdict matrix, one test per cell
# ---------------------------------------------------------------------------


def test_a_already_in_the_library(vault: Path, tmp_path: Path) -> None:
    first = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"same bytes")
    add_supplementary(vault, A_ID, [SiItem(first, first.name)], allow_unverified=False)
    again = _blob(tmp_path, "copy.xlsx", b"same bytes")
    v = _verdict(vault, A_ID, again)
    assert (v.status, v.reason) == ("rejected", "already in the library")


def test_a_second_identical_file_in_one_batch(vault: Path, tmp_path: Path) -> None:
    one = _blob(tmp_path, "ci9b00237_si_001.csv", b"x,y\n")
    two = _blob(tmp_path, "ci9b00237_si_002.csv", b"x,y\n")
    first, second = verify_batch(
        vault, A_ID, [SiItem(one, one.name), SiItem(two, two.name)]
    )
    assert first.status == "ok"
    assert second.status == "rejected"
    assert second.reason == "same file as ci9b00237_si_001.csv"


def test_an_empty_file_is_refused_whatever_its_name(vault: Path, tmp_path: Path) -> None:
    """An empty file is a download that never finished; the DOI in its name
    does not save it."""
    empty = _blob(tmp_path, "ci9b00237_si_001.pdf", b"")
    v = _verdict(vault, A_ID, empty)
    assert (v.status, v.reason, v.size) == ("rejected", "empty file", 0)


def test_two_empty_files_are_each_refused_as_empty(vault: Path, tmp_path: Path) -> None:
    """Two empty files share a sha256, yet neither is a copy of the other; a
    file next to them is judged as usual."""
    one = _blob(tmp_path, "test.dat", b"")
    two = _blob(tmp_path, "New Microsoft Word Document.docx", b"")
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    verdicts = verify_batch(vault, A_ID, [SiItem(p, p.name) for p in (one, two, sheet)])
    assert [(v.status, v.reason) for v in verdicts] == [
        ("rejected", "empty file"),
        ("rejected", "empty file"),
        ("ok", "name matches the paper's DOI"),
    ]


def test_d_the_article_itself_even_when_named_by_its_doi(
    vault: Path, make_text_pdf: MakePdf
) -> None:
    """d runs before c: a downloaded article is usually named after its DOI."""
    other_copy = make_text_pdf(
        [[*A_PAGE1, "Downloaded via the publisher website on 2026-10-20"]],
        name="acs.jcim.9b00237.pdf",
    )
    v = _verdict(vault, A_ID, other_copy)
    assert (v.status, v.reason) == ("rejected", "this is the article itself")


def test_d_exact_copy_of_an_article_without_a_text_layer(
    vault: Path, make_text_pdf: MakePdf, tmp_path: Path
) -> None:
    _write_paper(
        vault, make_text_pdf, "2018_Scan_Article", doi="10.1/scan9876",
        title="A scanned article", page1=[],
    )
    copy = tmp_path / "scan9876.pdf"
    shutil.copyfile(vault / "papers" / "2018_Scan_Article" / "paper.pdf", copy)
    v = _verdict(vault, "2018_Scan_Article", copy)
    assert (v.status, v.reason) == ("rejected", "this is the article itself")


def test_d_skipped_when_the_paper_pdf_has_no_text(
    vault: Path, make_text_pdf: MakePdf
) -> None:
    _write_paper(
        vault, make_text_pdf, "2018_Scan_Article", doi="10.1/scan9876",
        title="A scanned article", page1=[],
    )
    candidate = make_text_pdf([["A scanned article", "body"]], name="scan9876.pdf")
    v = _verdict(vault, "2018_Scan_Article", candidate)
    assert (v.status, v.check) == ("ok", "filename")


def test_b_another_papers_number(vault: Path, make_text_pdf: MakePdf) -> None:
    other_si = _si_pdf(make_text_pdf, "41467_2023_38851_MOESM1_ESM.pdf", B_TITLE)
    v = _verdict(vault, A_ID, other_si)
    assert v.status == "rejected"
    assert v.reason == f"looks like the SI of {B_ID}"
    assert v.other_id == B_ID


def test_b_needs_this_papers_number_absent(vault: Path, tmp_path: Path) -> None:
    both = _blob(tmp_path, "ci9b00237_vs_41467_2023_38851_MOESM1.csv")
    v = _verdict(vault, A_ID, both)
    assert (v.status, v.check) == ("ok", "filename")


def test_c_this_papers_number(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    v = _verdict(vault, A_ID, sheet)
    assert (v.status, v.check) == ("ok", "filename")


def test_e_title_on_the_first_pages(vault: Path, make_text_pdf: MakePdf) -> None:
    media = _si_pdf(make_text_pdf, "media-1.pdf", A_TITLE)
    v = _verdict(vault, A_ID, media)
    assert (v.status, v.check) == ("ok", "title")


def test_f_another_papers_title(vault: Path, make_text_pdf: MakePdf) -> None:
    media = _si_pdf(make_text_pdf, "media-1.pdf", B_TITLE)
    v = _verdict(vault, A_ID, media)
    assert v.status == "rejected"
    assert v.reason == f"looks like the SI of {B_ID}"


def test_f_only_when_this_title_is_not_on_the_page(
    vault: Path, make_text_pdf: MakePdf
) -> None:
    # An SI that also cites another paper's title is still this paper's.
    both = make_text_pdf([["Supporting Information", A_TITLE, B_TITLE]], name="si.pdf")
    v = _verdict(vault, A_ID, both)
    assert (v.status, v.check) == ("ok", "title")


def test_e_title_words_scattered_do_not_confirm(
    vault: Path, make_text_pdf: MakePdf
) -> None:
    """Every word of A's title, but not as the title: any paper from the
    same field has them."""
    scattered = make_text_pdf(
        [
            [
                "Supporting Information",
                "Drug discovery concepts, revisited",
                "Computer assisted screening meets artificial intelligence",
            ]
        ],
        name="media-1.pdf",
    )
    v = _verdict(vault, A_ID, scattered)
    assert (v.status, v.check) == ("unverified", None)


def test_e_title_broken_by_a_line_end_hyphen(
    vault: Path, make_text_pdf: MakePdf
) -> None:
    broken = make_text_pdf(
        [
            [
                "Supporting Information",
                "Concepts of Artificial Intelligence for Computer-Assis-",
                "ted Drug Discovery",
                "Table S1 lists every training hyperparameter we tuned",
            ]
        ],
        name="media-1.pdf",
    )
    v = _verdict(vault, A_ID, broken)
    assert (v.status, v.check) == ("ok", "title")


def test_e_stored_title_with_markup(vault: Path, make_text_pdf: MakePdf) -> None:
    """The page shows the title without the markup Crossref stored."""
    _write_paper(
        vault, make_text_pdf, "2022_Kim_Methylated", doi="10.1039/d2cb00123k",
        title="<i>N</i>-Methylated cyclic peptides resist proteolysis in human serum",
    )
    write_index(vault, list_papers(vault))
    media = _si_pdf(
        make_text_pdf, "media-1.pdf",
        "N-Methylated Cyclic Peptides Resist Proteolysis in Human Serum",
    )
    v = _verdict(vault, "2022_Kim_Methylated", media)
    assert (v.status, v.check) == ("ok", "title")


def test_title_key_ignores_how_an_accent_is_set() -> None:
    """A TeX-made PDF gives an accent as its own character before the letter."""
    assert si_mod._title_key("Schr\u00a8odinger equation") == si_mod._title_key(
        "Schr\u00f6dinger Equation"
    )
    assert si_mod._title_key("r\u00f4le") == si_mod._title_key("ro\u02c6le")
    assert si_mod._title_key("Caf\u00e9") == si_mod._title_key("Cafe\u0301")


def test_f_another_papers_title_words_scattered_do_not_reject(
    vault: Path, make_text_pdf: MakePdf
) -> None:
    scattered = make_text_pdf(
        [
            [
                "Supporting Information",
                "Peptides were screened for binding to each protein",
                "Deep generative models design cyclic scaffolds",
                "Targets are listed in Table S1",
            ]
        ],
        name="media-1.pdf",
    )
    v = _verdict(vault, A_ID, scattered)
    assert (v.status, v.other_id) == ("unverified", None)


# One id sorts before D, one after: neither the first nor the last title
# found may decide.
@pytest.mark.parametrize("longer_id", ["2018_Wu_Hydrocarbon", "2024_Wu_Hydrocarbon"])
def test_f_names_the_paper_whose_title_contains_the_other(
    vault: Path, make_text_pdf: MakePdf, longer_id: str
) -> None:
    """D's title is inside this longer one, so the page carries both."""
    longer = "Hydrocarbon peptide stapling chemistry reviewed across many recent studies"
    _write_paper(
        vault, make_text_pdf, longer_id, doi="10.1021/acs.jmedchem.4c01234",
        title=longer,
    )
    write_index(vault, list_papers(vault))
    media = _si_pdf(make_text_pdf, "media-1.pdf", longer)
    v = _verdict(vault, A_ID, media)
    assert v.status == "rejected"
    assert v.reason == f"looks like the SI of {longer_id}"
    assert v.other_id == longer_id


def test_g_cannot_confirm(vault: Path, make_text_pdf: MakePdf, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "Table_S2.xlsx")
    scanned = make_text_pdf([[]], name="scanned_si.pdf")
    unrelated = make_text_pdf([["Raw spectra for compounds 1 to 40"]], name="x.pdf")
    for path in (sheet, scanned, unrelated):
        v = _verdict(vault, A_ID, path)
        assert (v.status, v.check) == ("unverified", None), path.name
        assert v.reason == "can't confirm it belongs to this paper"


def test_short_own_title_decides_nothing(vault: Path, make_text_pdf: MakePdf) -> None:
    """Q1: a title of under five content words neither confirms (e) nor —
    since "this title is not on the page" cannot be judged — rejects (f)."""
    own = _si_pdf(make_text_pdf, "own.pdf", C_TITLE)
    assert _verdict(vault, C_ID, own).status == "unverified"
    other = _si_pdf(make_text_pdf, "other.pdf", B_TITLE)
    assert _verdict(vault, C_ID, other).status == "unverified"


def test_short_other_title_does_not_reject(vault: Path, make_text_pdf: MakePdf) -> None:
    pages = _si_pdf(make_text_pdf, "media-2.pdf", C_TITLE)
    assert _verdict(vault, A_ID, pages).status == "unverified"


def test_number_inside_a_longer_number_does_not_reject(
    vault: Path, tmp_path: Path
) -> None:
    """Q1: D's number 2836 occurs inside an Elsevier PII digit run."""
    elsevier = _blob(tmp_path, "1-s2.0-S0022283620301662-mmc1.pdf", b"not a pdf")
    assert _verdict(vault, A_ID, elsevier).status == "unverified"


def test_verdict_carries_hash_and_size(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"12345")
    v = _verdict(vault, A_ID, sheet)
    assert v.size == 5
    assert len(v.sha256) == 64


# ---------------------------------------------------------------------------
# §数据模型 3 — names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "ext"),
    [
        ("ci9b00237_si_001.PDF", ".pdf"),
        ("data.tar.gz", ".tar.gz"),
        ("data.TAR.BZ2", ".tar.bz2"),
        ("data.tar.xz", ".tar.xz"),
        ("README", ""),
        (".hidden", ""),
        ("odd.pdf~", ""),
        ("x.abcdefghijklm", ""),  # 13 characters is not an extension
        ("x.abcdefghijkl", ".abcdefghijkl"),
        ("Table S2 (final).xlsx", ".xlsx"),
    ],
)
def test_si_extension(original: str, ext: str) -> None:
    assert si_extension(original) == ext


def test_plan_names_orders_by_publisher_number_then_given_order() -> None:
    names = plan_names(
        ["si-01.pdf"],
        ["notes.xlsx", "ci9b00237_si_002.pdf", "ci9b00237_si_001.pdf", "z.csv"],
    )
    assert names == ["si-04.xlsx", "si-03.pdf", "si-02.pdf", "si-05.csv"]


@pytest.mark.parametrize(
    "originals",
    [
        ["41467_2023_38851_MOESM2_ESM.xlsx", "41467_2023_38851_MOESM1_ESM.pdf"],
        ["1-s2.0-S0022283620301662-mmc2.pdf", "1-s2.0-S0022283620301662-mmc1.pdf"],
        ["media-2.pdf", "media-1.pdf"],
        ["psc3666-sup-0002-x.pdf", "psc3666-sup-0001-x.pdf"],
        ["pone.0123-s002.pdf", "pone.0123-s001.pdf"],
    ],
)
def test_plan_names_reads_each_publishers_number(originals: list[str]) -> None:
    names = plan_names([], originals)
    assert [n.split(".")[0] for n in names] == ["si-02", "si-01"]


def test_plan_names_keeps_given_order_when_nothing_is_recognised() -> None:
    assert plan_names([], ["b.csv", "a.csv"]) == ["si-01.csv", "si-02.csv"]


def test_plan_names_fills_the_lowest_free_numbers() -> None:
    # Recorded or on disk (any case) holds a number; a gap is given again.
    taken = ["si-01.pdf", "si-07.zip", "SI-09.PDF", "notes.txt"]
    assert plan_names(taken, ["a.pdf"]) == ["si-02.pdf"]
    assert plan_names(["si-99.pdf"], ["a.pdf"]) == ["si-01.pdf"]
    gaps = ["si-01.pdf", "si-03.csv", "SI-05.txt"]
    assert plan_names(gaps, ["a.csv", "b.csv", "c.csv"]) == [
        "si-02.csv", "si-04.csv", "si-06.csv"
    ]
    # The publisher's own order still decides who gets the lower gap.
    assert plan_names(
        gaps, ["notes.xlsx", "ci9b00237_si_002.pdf", "ci9b00237_si_001.pdf"]
    ) == ["si-06.xlsx", "si-04.pdf", "si-02.pdf"]
    # No gap: the next number up.
    assert plan_names(["si-01.pdf", "si-02.csv"], ["a.pdf"]) == ["si-03.pdf"]


# ---------------------------------------------------------------------------
# The field
# ---------------------------------------------------------------------------


_GOOD = {"file": "si-01.pdf", "sha256": "a" * 64, "bytes": 3}


@pytest.mark.parametrize(
    ("value", "problems"),
    [
        (None, []),
        ([], []),
        ([_GOOD], []),
        ("si-01.pdf", ["is not a list"]),
        ([_GOOD, "si-02.pdf"], ["entry 2 is not a mapping"]),
        ([{"file": "S1.pdf", "sha256": "a" * 64, "bytes": 3}], [
            "entry 1 has no valid file name (si-NN.ext)"
        ]),
        ([{"original": "x.pdf"}], ["entry 1 has no valid file name (si-NN.ext)"]),
        ([_GOOD, dict(_GOOD)], ["lists si-01.pdf twice"]),
        # No checksum is not a structural fault: such an entry is kept and
        # carried along; only its content goes unverified (a warning).
        ([{"file": "si-01.pdf"}], []),
        ([{"file": "si-01.pdf", "sha256": "xyz", "bytes": "3"}], []),
    ],
)
def test_field_problems(value: object, problems: list[str]) -> None:
    assert field_problems(value) == problems


def test_list_supplementary_keeps_only_well_formed_records() -> None:
    meta = {"supplementary": [_GOOD, "junk", {"file": "../x"}, dict(_GOOD)]}
    assert [r["file"] for r in list_supplementary(meta)] == ["si-01.pdf"]
    assert list_supplementary({"supplementary": "si-01.pdf"}) == []
    assert list_supplementary({}) == []


# ---------------------------------------------------------------------------
# add_supplementary
# ---------------------------------------------------------------------------


def _meta(vault: Path, paper_id: str = A_ID) -> dict:
    return find_paper(vault, paper_id)


def test_add_writes_file_record_and_index(vault: Path, make_text_pdf: MakePdf) -> None:
    src = _si_pdf(make_text_pdf, "ci9b00237_si_001.pdf", A_TITLE)
    payload = src.read_bytes()

    result = add_supplementary(
        vault, A_ID, prepare_items(vault, A_ID, [src]), allow_unverified=False
    )

    target = vault / "papers" / A_ID / "supplementary" / "si-01.pdf"
    assert result.paths == [target]
    assert result.warnings == []
    assert target.read_bytes() == payload
    assert not src.exists()  # mv semantics
    meta = _meta(vault)
    (record,) = meta["supplementary"]
    assert set(record) == {"file", "original", "sha256", "bytes", "added", "check"}
    assert record["file"] == "si-01.pdf"
    assert record["original"] == "ci9b00237_si_001.pdf"
    assert record["bytes"] == len(payload)
    assert record["check"] == "filename"
    assert isinstance(record["added"], str)
    assert meta["updated-at"] == record["added"]
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    entry = next(p for p in index["papers"] if p["id"] == A_ID)
    assert entry["updated-at"] == record["added"]
    assert "supplementary" not in entry  # not projected into INDEX
    assert supplementary_drift(vault, A_ID, meta) == si_mod.SiDrift()


@_posix_only
def test_add_locks_the_si_file(vault: Path, tmp_path: Path) -> None:
    src = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    add_supplementary(vault, A_ID, [SiItem(src, src.name)], allow_unverified=False)
    target = vault / "papers" / A_ID / "supplementary" / "si-01.xlsx"
    assert not os.access(target, os.W_OK)


def test_add_is_all_or_nothing(vault: Path, make_text_pdf: MakePdf, tmp_path: Path) -> None:
    good = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    bad = _si_pdf(make_text_pdf, "41467_2023_38851_MOESM1_ESM.pdf", B_TITLE)
    before = (vault / "papers" / A_ID / "metadata.yaml").read_bytes()

    with pytest.raises(SupplementaryError, match="looks like the SI of"):
        add_supplementary(
            vault, A_ID, prepare_items(vault, A_ID, [good, bad]),
            allow_unverified=True,
        )

    assert not (vault / "papers" / A_ID / "supplementary").exists()
    assert (vault / "papers" / A_ID / "metadata.yaml").read_bytes() == before
    assert good.exists() and bad.exists()


def test_add_never_takes_a_rejected_file(vault: Path, tmp_path: Path) -> None:
    """allow_unverified (--force / "add anyway") only covers the "?" tier."""
    first = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"one")
    add_supplementary(vault, A_ID, [SiItem(first, first.name)], allow_unverified=False)
    duplicate = _blob(tmp_path, "dup.xlsx", b"one")
    with pytest.raises(SupplementaryError, match="already in the library"):
        add_supplementary(
            vault, A_ID, [SiItem(duplicate, duplicate.name)], allow_unverified=True
        )
    assert duplicate.exists()


def test_add_never_takes_an_empty_file(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    empty = _blob(tmp_path, "ci9b00237_si_003.xlsx", b"")
    items = [SiItem(sheet, sheet.name), SiItem(empty, empty.name, approved=True)]
    with pytest.raises(
        SupplementaryError,
        match=r"^Refused 1 file \(ci9b00237_si_003\.xlsx: empty file\)\. Nothing was added\.$",
    ):
        add_supplementary(vault, A_ID, items, allow_unverified=True)
    assert sheet.exists() and empty.exists()
    assert not (vault / "papers" / A_ID / "supplementary").exists()


def test_add_unverified_needs_consent(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "Table_S2.xlsx")
    with pytest.raises(SupplementaryError, match="Can't confirm"):
        add_supplementary(vault, A_ID, [SiItem(sheet, sheet.name)], allow_unverified=False)
    assert sheet.exists()

    add_supplementary(vault, A_ID, [SiItem(sheet, sheet.name)], allow_unverified=True)
    assert _meta(vault)["supplementary"][0]["check"] == "user"


def test_add_failure_mid_write_leaves_no_trace(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = _blob(tmp_path, "ci9b00237_si_001.csv", b"1")
    two = _blob(tmp_path, "ci9b00237_si_002.csv", b"2")
    meta_before = (vault / "papers" / A_ID / "metadata.yaml").read_bytes()
    index_before = (vault / "INDEX.json").read_bytes()
    real_copy = atomic_mod.shutil.copyfile
    calls: list[object] = []

    def flaky_copy(src: object, dst: object) -> object:
        calls.append(src)
        if len(calls) == 2:
            raise OSError(28, "No space left on device (mocked)")
        return real_copy(src, dst)

    monkeypatch.setattr(atomic_mod.shutil, "copyfile", flaky_copy)
    with pytest.raises(OSError, match="mocked"):
        add_supplementary(
            vault, A_ID, [SiItem(one, one.name), SiItem(two, two.name)],
            allow_unverified=False,
        )

    assert not (vault / "papers" / A_ID / "supplementary").exists()
    assert (vault / "papers" / A_ID / "metadata.yaml").read_bytes() == meta_before
    assert (vault / "INDEX.json").read_bytes() == index_before
    assert list((vault / ".litman-staging").iterdir()) == []
    assert one.exists() and two.exists()


def test_add_refuses_a_malformed_field(vault: Path, tmp_path: Path) -> None:
    meta_path = vault / "papers" / A_ID / "metadata.yaml"
    with meta_path.open("a", encoding="utf-8") as f:
        f.write("supplementary: si-01.pdf\n")
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    with pytest.raises(SupplementaryError, match="'supplementary' is not a list"):
        add_supplementary(vault, A_ID, [SiItem(sheet, sheet.name)], allow_unverified=True)
    assert sheet.exists()


def test_sources_inside_the_library_are_refused(
    vault: Path, tmp_path: Path, make_text_pdf: MakePdf
) -> None:
    """Moving the file out of the vault would delete it from where it is."""
    _write_paper(vault, make_text_pdf, "2017_Other_Paper", doi="10.1/o", title="O", page1=["x"])
    trashed = vault / TRASH_DIRNAME / "2016_Gone_Paper-20260101T000000Z"
    trashed.mkdir(parents=True)
    _blob(trashed, "paper.pdf", b"%PDF-1.4\n")
    for inside in (
        vault / "papers" / "2017_Other_Paper" / "paper.pdf",
        vault / "papers" / A_ID / "paper.pdf",
        trashed / "paper.pdf",
        vault / "INDEX.json",
    ):
        with pytest.raises(SupplementaryError, match="inside the library"):
            prepare_items(vault, A_ID, [inside])
        with pytest.raises(SupplementaryError, match="inside the library"):
            add_supplementary(vault, A_ID, [SiItem(inside, "ci9b00237_si_1.pdf")],
                              allow_unverified=True)
        assert inside.exists()


def test_sources_missing_or_folders_are_refused(vault: Path, tmp_path: Path) -> None:
    with pytest.raises(SupplementaryError, match="No such file"):
        prepare_items(vault, A_ID, [tmp_path / "nope.pdf"])
    folder = tmp_path / "unzipped"
    folder.mkdir()
    with pytest.raises(SupplementaryError, match="is a folder"):
        prepare_items(vault, A_ID, [folder])


def test_source_from_the_upload_stash_is_accepted(vault: Path) -> None:
    stash = vault / ".litman-upload"
    stash.mkdir(exist_ok=True)
    upload = _blob(stash, "si-abc-def.xlsx")
    add_supplementary(
        vault, A_ID, [SiItem(upload, "ci9b00237_si_003.xlsx")], allow_unverified=False
    )
    assert not upload.exists()
    assert _meta(vault)["supplementary"][0]["original"] == "ci9b00237_si_003.xlsx"


def test_stray_si_named_file_is_recorded_in_place(vault: Path) -> None:
    """A stray already called si-NN keeps its number (notes may cite it)."""
    si = vault / "papers" / A_ID / "supplementary"
    si.mkdir()
    stray = _blob(si, "si-05.xlsx")
    lock_truth_file(stray)  # what the health-check re-lock sweep does first

    result = add_supplementary(
        vault, A_ID, prepare_items(vault, A_ID, [stray]), allow_unverified=True
    )

    assert result.paths == [stray]
    assert stray.exists()
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-05.xlsx"]
    assert supplementary_drift(vault, A_ID, _meta(vault)) == si_mod.SiDrift()


def test_stray_with_its_own_name_gets_the_next_number(vault: Path) -> None:
    si = vault / "papers" / A_ID / "supplementary"
    si.mkdir()
    stray = _blob(si, "ci9b00237_si_004.xlsx")
    lock_truth_file(stray)

    result = add_supplementary(
        vault, A_ID, prepare_items(vault, A_ID, [stray]), allow_unverified=False
    )

    assert result.paths == [si / "si-01.xlsx"]
    assert not stray.exists()
    assert sorted(os.listdir(si)) == ["si-01.xlsx"]


def test_recorded_file_is_not_a_source(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    add_supplementary(vault, A_ID, [SiItem(sheet, sheet.name)], allow_unverified=False)
    recorded = vault / "papers" / A_ID / "supplementary" / "si-01.xlsx"
    with pytest.raises(SupplementaryError, match="inside the library"):
        prepare_items(vault, A_ID, [recorded])


# ---------------------------------------------------------------------------
# remove_supplementary + putting a file back
# ---------------------------------------------------------------------------


def _add_two(vault: Path, tmp_path: Path) -> None:
    one = _blob(tmp_path, "ci9b00237_si_001.csv", b"1")
    two = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"2")
    add_supplementary(
        vault, A_ID, [SiItem(one, one.name), SiItem(two, two.name)],
        allow_unverified=False,
    )


def _all_files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


def test_remove_deletes_the_file_and_its_record(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    before = _all_files(vault)

    result = remove_supplementary(vault, A_ID, ["si-02"])

    assert [r["file"] for r in result.records] == ["si-02.xlsx"]
    assert result.records[0]["original"] == "ci9b00237_si_002.xlsx"
    assert (result.gone, result.warnings) == ([], [])
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-01.csv"]
    gone = vault / "papers" / A_ID / "supplementary" / "si-02.xlsx"
    assert not gone.exists()
    # Deleted, not moved: no copy of it turned up anywhere in the library.
    new = {p.relative_to(vault).as_posix() for p in _all_files(vault) - before}
    assert not {n for n in new if not n.startswith((".litman-staging", "views/"))}
    assert not (vault / TRASH_DIRNAME).exists() or not any(
        (vault / TRASH_DIRNAME).iterdir()
    )


def test_remove_last_drops_key_and_folder(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    _blob(vault / "papers" / A_ID / "supplementary", ".DS_Store", b"\0")
    remove_supplementary(vault, A_ID, ["si-01.csv", "si-02.xlsx"])
    assert "supplementary" not in _meta(vault)
    assert not (vault / "papers" / A_ID / "supplementary").exists()


def test_remove_of_a_missing_file_drops_only_the_record(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    gone = vault / "papers" / A_ID / "supplementary" / "si-01.csv"
    unlock_truth_file(gone)
    gone.unlink()
    result = remove_supplementary(vault, A_ID, ["si-01"])
    assert [r["file"] for r in result.records] == ["si-01.csv"]
    assert result.gone == ["si-01.csv"]
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-02.xlsx"]


@pytest.mark.parametrize("name", ["si-03", "si-02.pdf", "supplement.pdf", "S2"])
def test_remove_unknown_name(vault: Path, tmp_path: Path, name: str) -> None:
    _add_two(vault, tmp_path)
    with pytest.raises(SupplementaryError, match="has no SI file"):
        remove_supplementary(vault, A_ID, [name])
    assert len(_meta(vault)["supplementary"]) == 2


def _stuck_delete(*_args: object, **_kwargs: object) -> None:
    raise PermissionError(13, "in use by another program (mocked)")


def test_remove_failed_unlink_puts_the_record_back(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_two(vault, tmp_path)
    monkeypatch.setattr(si_mod, "_delete_si_entry", _stuck_delete)

    with pytest.raises(SupplementaryError) as raised:
        remove_supplementary(vault, A_ID, ["si-01", "si-02"])

    assert str(raised.value) == (
        "Could not delete si-01.csv, si-02.xlsx (PermissionError); their records "
        "are back. Close anything that has them open, then retry."
    )
    target = vault / "papers" / A_ID / "supplementary" / "si-02.xlsx"
    assert target.read_bytes() == b"2"
    assert sorted(r["file"] for r in _meta(vault)["supplementary"]) == [
        "si-01.csv", "si-02.xlsx"
    ]
    if not _REAL_WIN32:
        assert not os.access(target, os.W_OK)  # locked again


@_posix_only
def test_a_file_that_will_not_go_is_locked_again(vault: Path, tmp_path: Path) -> None:
    """The real deleting seam: a read-only folder refuses the unlink, and the
    file it unlocked first is locked again with its record back."""
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    os.chmod(si, 0o555)
    try:
        if os.access(si, os.W_OK):
            pytest.skip("this user can write a read-only folder (root)")
        with pytest.raises(SupplementaryError, match="its record is back"):
            remove_supplementary(vault, A_ID, ["si-02"])
    finally:
        os.chmod(si, 0o755)
    assert not os.access(si / "si-02.xlsx", os.W_OK)
    assert len(_meta(vault)["supplementary"]) == 2


def test_a_partly_done_delete_names_what_went(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_two(vault, tmp_path)
    real = si_mod._delete_si_entry

    def only_csv(path: Path, kind: str) -> None:
        if path.name == "si-02.xlsx":
            _stuck_delete()
        real(path, kind)

    monkeypatch.setattr(si_mod, "_delete_si_entry", only_csv)
    with pytest.raises(SupplementaryError, match=r"record is back\..* Deleted: si-01\.csv\.$"):
        remove_supplementary(vault, A_ID, ["si-01", "si-02"])
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-02.xlsx"]
    assert not (vault / "papers" / A_ID / "supplementary" / "si-01.csv").exists()


def test_a_failed_views_rebuild_after_a_failed_delete_keeps_the_real_message(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_two(vault, tmp_path)
    monkeypatch.setattr(si_mod, "_delete_si_entry", _stuck_delete)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("views (mocked)")

    monkeypatch.setattr(si_mod, "_reconcile", broken)
    with pytest.raises(SupplementaryError) as raised:
        remove_supplementary(vault, A_ID, ["si-02"])

    assert str(raised.value) == (
        "Could not delete si-02.xlsx (PermissionError); its record is back. "
        "Close anything that has it open, then retry. INDEX/views rebuild failed "
        "(RuntimeError); run `lit refresh-views` to reconcile."
    )
    assert len(_meta(vault)["supplementary"]) == 2


@_posix_only
@_not_root
def test_a_link_that_will_not_go_names_the_real_error(
    vault: Path, tmp_path: Path
) -> None:
    """The directory fallback for a junction must not hide why the link
    stayed: a read-only folder says PermissionError, not NotADirectoryError."""
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    _outside, personal = _outside_folder(tmp_path)
    unlock_truth_file(si / "si-02.xlsx")
    (si / "si-02.xlsx").unlink()
    (si / "si-02.xlsx").symlink_to(personal)
    os.chmod(si, 0o555)
    try:
        with pytest.raises(SupplementaryError, match=r"\(PermissionError\); its record"):
            remove_supplementary(vault, A_ID, ["si-02"])
    finally:
        os.chmod(si, 0o755)
    assert (si / "si-02.xlsx").is_symlink()
    assert personal.read_bytes() == b"mine"


def test_deleting_a_folder_link_leaves_the_folder_it_points_at(
    vault: Path, tmp_path: Path
) -> None:
    """A symlink on POSIX, a junction on Windows: the link goes, the folder
    behind it and everything in it stay."""
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    outside, personal = _outside_folder(tmp_path)
    unlock_truth_file(si / "si-02.xlsx")
    (si / "si-02.xlsx").unlink()
    if not make_portable_link(si / "si-02.xlsx", outside):
        pytest.skip("this filesystem cannot hold folder links")

    remove_supplementary(vault, A_ID, ["si-02"])

    assert not os.path.lexists(si / "si-02.xlsx")
    assert personal.read_bytes() == b"mine"
    assert sorted(os.listdir(outside)) == ["personal.docx"]
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-01.csv"]


def test_a_double_failure_on_several_files_speaks_of_them(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_two(vault, tmp_path)
    monkeypatch.setattr(si_mod, "_delete_si_entry", _stuck_delete)
    real_commit = si_mod._commit_metadata

    def undo_fails(vault_: Path, paper_id: str, meta: object, op_id: str, *a: object):
        if op_id.startswith("si-rm-undo-"):
            raise OSError(28, "No space left on device (mocked)")
        return real_commit(vault_, paper_id, meta, op_id, *a)

    monkeypatch.setattr(si_mod, "_commit_metadata", undo_fails)
    with pytest.raises(SupplementaryError) as raised:
        remove_supplementary(vault, A_ID, ["si-01", "si-02"])

    assert str(raised.value) == (
        "Could not delete si-01.csv, si-02.xlsx (PermissionError), nor put their "
        "records back (OSError): the files are still in supplementary/. Run "
        "`lit health-check --fix` to record them again."
    )


def test_a_failed_delete_whose_record_cannot_go_back_says_how_to_record_it(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both writes failed: the file stays without a record — the one
    inconsistency allowed, a warning `--fix` records again — never a record
    without its file."""
    _add_two(vault, tmp_path)
    monkeypatch.setattr(si_mod, "_delete_si_entry", _stuck_delete)
    real_commit = si_mod._commit_metadata

    def undo_fails(vault_: Path, paper_id: str, meta: object, op_id: str, *a: object):
        if op_id.startswith("si-rm-undo-"):
            raise OSError(28, "No space left on device (mocked)")
        return real_commit(vault_, paper_id, meta, op_id, *a)

    monkeypatch.setattr(si_mod, "_commit_metadata", undo_fails)
    with pytest.raises(SupplementaryError) as raised:
        remove_supplementary(vault, A_ID, ["si-02"])

    assert str(raised.value) == (
        "Could not delete si-02.xlsx (PermissionError), nor put its record back "
        "(OSError): the file is still in supplementary/. Run `lit health-check "
        "--fix` to record it again."
    )
    assert (vault / "papers" / A_ID / "supplementary" / "si-02.xlsx").exists()
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-01.csv"]
    drift = supplementary_drift(vault, A_ID, _meta(vault))
    assert (drift.unrecorded, drift.missing) == (["si-02.xlsx"], [])


def test_a_folder_at_a_recorded_name_is_refused_before_anything_is_written(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    unlock_truth_file(si / "si-02.xlsx")
    (si / "si-02.xlsx").unlink()
    (si / "si-02.xlsx").mkdir()
    (si / "si-02.xlsx" / "mine.txt").write_bytes(b"the user's")
    meta_file = vault / "papers" / A_ID / "metadata.yaml"
    before = meta_file.read_bytes()

    for attempt in (
        lambda: si_mod.preview_si_removal(vault, A_ID, ["si-01", "si-02"]),
        lambda: remove_supplementary(vault, A_ID, ["si-01", "si-02"]),
    ):
        with pytest.raises(SupplementaryError) as raised:
            attempt()
        assert str(raised.value) == (
            f"papers/{A_ID}/supplementary/si-02.xlsx is a folder, not the "
            "recorded file — nothing was deleted. Move it out of "
            "supplementary/, then retry."
        )

    assert meta_file.read_bytes() == before
    assert (si / "si-01.csv").exists()
    assert (si / "si-02.xlsx" / "mine.txt").read_bytes() == b"the user's"


@_posix_only
def test_deleting_a_link_removes_the_link_not_its_target(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    _outside, personal = _outside_folder(tmp_path)
    unlock_truth_file(si / "si-02.xlsx")
    (si / "si-02.xlsx").unlink()
    (si / "si-02.xlsx").symlink_to(personal)

    remove_supplementary(vault, A_ID, ["si-02"])

    assert not os.path.lexists(si / "si-02.xlsx")
    assert personal.read_bytes() == b"mine"
    assert os.access(personal, os.W_OK)  # never chmod-ed through the link


def test_a_failed_views_rebuild_is_a_warning(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_two(vault, tmp_path)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("views (mocked)")

    monkeypatch.setattr(si_mod, "_reconcile", broken)
    result = remove_supplementary(vault, A_ID, ["si-02"])

    assert result.warnings == [
        "INDEX/views rebuild failed (RuntimeError); the SI file was deleted. "
        "Run `lit refresh-views` to reconcile."
    ]
    assert not (vault / "papers" / A_ID / "supplementary" / "si-02.xlsx").exists()
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-01.csv"]


def test_a_deleted_number_is_given_again(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    remove_supplementary(vault, A_ID, ["si-01"])

    same = _blob(tmp_path, "ci9b00237_si_001.csv", b"1")
    result = add_supplementary(vault, A_ID, [SiItem(same, same.name)], allow_unverified=False)
    assert [p.name for p in result.paths] == ["si-01.csv"]

    remove_supplementary(vault, A_ID, ["si-01"])
    other = _blob(tmp_path, "ci9b00237_si_009.pdf", b"9")
    result = add_supplementary(vault, A_ID, [SiItem(other, other.name)], allow_unverified=False)
    assert [p.name for p in result.paths] == ["si-01.pdf"]
    assert [r["file"] for r in _meta(vault)["supplementary"]] == [
        "si-01.pdf", "si-02.xlsx"
    ]


def test_drop_missing_records_keeps_a_record_whose_file_came_back(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()

    assert si_mod.drop_missing_records(vault, A_ID, ["si-01.csv", "si-02.xlsx"]) == [
        "si-01.csv"
    ]
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-02.xlsx"]
    assert (si / "si-02.xlsx").read_bytes() == b"2"


def test_fix_never_deletes_a_file_that_came_back_while_it_asked(
    vault: Path, tmp_path: Path
) -> None:
    """`--fix` asks before dropping a dead record; a file put back during the
    question keeps its record and is not touched."""
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    unlock_truth_file(si / "si-02.xlsx")
    (si / "si-02.xlsx").unlink()

    def put_back_then_agree(paper_id: str, names: list[str]) -> bool:
        assert (paper_id, names) == (A_ID, ["si-02.xlsx"])
        (si / "si-02.xlsx").write_bytes(b"2")
        return True

    policy = si_mod.SiFixPolicy(
        confirm_drop=put_back_then_agree, confirm_unverified=lambda *_a: False
    )
    counts = si_mod.fix_supplementary(vault, [A_ID], policy=policy)

    assert counts["supplementary_missing"] == 0
    assert (si / "si-02.xlsx").read_bytes() == b"2"
    assert [r["file"] for r in _meta(vault)["supplementary"]] == [
        "si-01.csv", "si-02.xlsx"
    ]


def test_fix_drops_only_the_record_of_a_missing_file(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()
    policy = si_mod.SiFixPolicy(
        confirm_drop=lambda *_a: True, confirm_unverified=lambda *_a: False
    )

    counts = si_mod.fix_supplementary(vault, [A_ID], policy=policy)

    assert counts["supplementary_missing"] == 1
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-02.xlsx"]
    assert (si / "si-02.xlsx").read_bytes() == b"2"


def test_fix_leaves_an_empty_stray_where_it_is(vault: Path) -> None:
    si = vault / "papers" / A_ID / "supplementary"
    si.mkdir()
    _blob(si, "ci9b00237_si_004.pdf", b"")
    refused: list[tuple[str, str, str]] = []
    policy = si_mod.SiFixPolicy(
        confirm_drop=lambda *_a: True,
        confirm_unverified=lambda *_a: True,
        on_rejected=lambda paper_id, path, v: refused.append((paper_id, path.name, v.reason)),
    )

    counts = si_mod.fix_supplementary(vault, [A_ID], policy=policy)

    assert counts["supplementary_unrecorded"] == 0
    assert refused == [(A_ID, "ci9b00237_si_004.pdf", "empty file")]
    assert os.listdir(si) == ["ci9b00237_si_004.pdf"]
    assert not _meta(vault).get("supplementary")


def test_preview_counts_the_lines_citing_each_file(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    paper = vault / "papers" / A_ID
    (paper / "notes.md").write_text(
        "# Notes\nsee si-02.xlsx（ci9b00237_si_002.xlsx）\n", encoding="utf-8"
    )
    (paper / "discussion.md").write_text(
        "# Discussion log\n\n## 2026-10-20 10:00\n"
        "si-02 twice: si-02.xlsx and si-02 on one line\n"
        "si-02.xlsx (another_name.xlsx) is some other file\n"
        "si-02 again\n",
        encoding="utf-8",
    )
    before = {p: p.read_bytes() for p in _all_files(vault)}

    preview = si_mod.preview_si_removal(vault, A_ID, ["si-01", "si-02.xlsx"])

    assert [r["file"] for r in preview.records] == ["si-01.csv", "si-02.xlsx"]
    assert preview.cited == {"si-02.xlsx": {"notes.md": 1, "discussion.md": 2}}
    assert preview.unchecked == []
    assert {p: p.read_bytes() for p in _all_files(vault)} == before  # reads only


def test_preview_names_a_notes_file_it_cannot_even_look_at(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lit rm --si` asks for this before its prompt: a notes file in a folder
    that cannot be searched is unchecked, never a traceback."""
    _add_two(vault, tmp_path)
    (vault / "papers" / A_ID / "notes.md").write_text("si-01\n", encoding="utf-8")
    real_is_file = Path.is_file

    def denied(self: Path) -> bool:
        if self.name == "notes.md":
            raise PermissionError(13, "Permission denied (mocked)")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", denied)
    preview = si_mod.preview_si_removal(vault, A_ID, ["si-01"])

    assert preview.unchecked == [("notes.md", "PermissionError")]


def test_preview_names_a_notes_file_it_cannot_read(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    (vault / "papers" / A_ID / "notes.md").write_bytes(b"si-01 caf\xe9\n")

    preview = si_mod.preview_si_removal(vault, A_ID, ["si-01"])

    assert preview.cited == {}
    assert preview.unchecked == [("notes.md", "UnicodeDecodeError")]


# ---------------------------------------------------------------------------
# Drift (the health-check side)
# ---------------------------------------------------------------------------


def test_drift_reports_missing_unrecorded_and_changed(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()
    unlock_truth_file(si / "si-02.xlsx")
    (si / "si-02.xlsx").write_bytes(b"22")  # size differs
    _blob(si, "Table S9.xlsx")
    _blob(si, ".DS_Store", b"\0")
    _blob(si, "Thumbs.db", b"\0")

    drift = supplementary_drift(vault, A_ID, _meta(vault))

    assert drift.missing == ["si-01.csv"]
    assert drift.changed == ["si-02.xlsx"]
    assert drift.unrecorded == ["Table S9.xlsx"]
    assert drift.problems == []


def test_drift_rehashes_only_when_the_mtime_moved(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_two(vault, tmp_path)
    target = vault / "papers" / A_ID / "supplementary" / "si-02.xlsx"
    meta = _meta(vault)

    def no_hashing(_path: Path) -> str:
        raise AssertionError("hashed a file whose size and mtime still match")

    with monkeypatch.context() as patched:
        patched.setattr(si_mod, "_sha256_file", no_hashing)
        assert supplementary_drift(vault, A_ID, meta).changed == []

    # Same size, other bytes, mtime moved: only the hash can tell.
    unlock_truth_file(target)
    target.write_bytes(b"X")
    later = target.stat().st_mtime + 3600
    os.utime(target, (later, later))
    assert supplementary_drift(vault, A_ID, meta).changed == ["si-02.xlsx"]


def test_drift_with_a_malformed_field_reports_only_that(vault: Path) -> None:
    si = vault / "papers" / A_ID / "supplementary"
    si.mkdir()
    _blob(si, "si-01.pdf")
    drift = supplementary_drift(vault, A_ID, {"supplementary": {"file": "si-01.pdf"}})
    assert drift.problems == ["is not a list"]
    assert (drift.missing, drift.unrecorded, drift.changed) == ([], [], [])



# ---------------------------------------------------------------------------
# Round-1 review: folder shapes, numbering, trash sources, unreadable entries
# ---------------------------------------------------------------------------


_DROPPINGS = (".DS_Store", "._si-01.csv", "Thumbs.db", "desktop.ini")


def test_removing_the_last_si_never_deletes_a_users_hidden_file(
    vault: Path, tmp_path: Path
) -> None:
    """C1: only file-manager droppings may go with the folder — nothing here
    passes through the trash, so a hidden note of the user's keeps it."""
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    mine = _blob(si, ".my notes on this SI.txt", b"hand-written, irreplaceable")
    for name in _DROPPINGS:
        _blob(si, name, b"\0")

    remove_supplementary(vault, A_ID, ["si-01", "si-02"])

    assert mine.read_bytes() == b"hand-written, irreplaceable"
    assert "supplementary" not in _meta(vault)


def test_removing_the_last_si_clears_only_droppings(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    for name in _DROPPINGS:
        _blob(si, name, b"\0")
    remove_supplementary(vault, A_ID, ["si-01", "si-02"])
    assert not si.exists()


def _outside_folder(tmp_path: Path) -> tuple[Path, Path]:
    """A folder of the user's outside the library, holding a writable file."""
    outside = tmp_path / "Downloads"
    outside.mkdir()
    personal = outside / "personal.docx"
    personal.write_bytes(b"mine")
    return outside, personal


def _link_si_folder(vault: Path, target: Path, paper_id: str = A_ID) -> Path:
    """Make papers/<id>/supplementary a folder link (symlink, or a junction on
    Windows) to ``target``."""
    link = vault / "papers" / paper_id / "supplementary"
    if not make_portable_link(link, target):
        pytest.skip("this filesystem cannot hold folder links")
    return link


def test_a_linked_folder_is_reported_not_followed(vault: Path, tmp_path: Path) -> None:
    """W1: what is behind the link is never listed, checked or recorded."""
    outside, _personal = _outside_folder(tmp_path)
    _blob(outside, "ci9b00237_si_009.csv", b"would verify by name")
    _link_si_folder(vault, outside)

    drift = supplementary_drift(vault, A_ID, _meta(vault))
    issues = check_supplementary(vault, list_papers(vault))

    assert drift.folder == "link"
    assert drift.unrecorded == []
    assert [(i.category, i.severity) for i in issues] == [
        ("supplementary_layout", "error")
    ]
    assert "supplementary_layout" not in AUTO_FIXABLE_CATEGORIES
    assert "--fix" not in (issues[0].hint or "")

    apply_autofix(vault, run_all_checks(vault, list_papers(vault)))
    assert sorted(os.listdir(outside)) == ["ci9b00237_si_009.csv", "personal.docx"]
    assert "supplementary" not in _meta(vault)


def test_add_and_remove_refuse_a_linked_folder(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    moved_away = tmp_path / "moved-away"
    shutil.move(str(si), str(moved_away))  # the SI files now live outside
    _link_si_folder(vault, moved_away)
    new = _blob(tmp_path, "ci9b00237_si_003.csv", b"3")

    for attempt in (
        lambda: prepare_items(vault, A_ID, [new]),
        lambda: add_supplementary(
            vault, A_ID, [SiItem(new, new.name)], allow_unverified=False
        ),
        lambda: remove_supplementary(vault, A_ID, ["si-01"]),
    ):
        with pytest.raises(SupplementaryError, match="is a link, and litman"):
            attempt()

    assert sorted(os.listdir(moved_away)) == ["si-01.csv", "si-02.xlsx"]
    assert new.exists()
    assert len(_meta(vault)["supplementary"]) == 2


def test_add_refuses_when_supplementary_is_a_file(vault: Path, tmp_path: Path) -> None:
    """W2: refused before anything is staged, so there is no half-done
    commit for the next vault-open to roll forward."""
    placeholder = vault / "papers" / A_ID / "supplementary"
    placeholder.write_text("oops, a file", encoding="utf-8")
    src = _blob(tmp_path, "ci9b00237_si_001.csv", b"1,2\n")

    with pytest.raises(SupplementaryError, match="is a file, not a folder"):
        add_supplementary(vault, A_ID, [SiItem(src, src.name)], allow_unverified=False)

    assert src.exists()
    assert list((vault / ".litman-staging").iterdir()) == []
    issues = check_supplementary(vault, list_papers(vault))
    assert [(i.category, i.severity) for i in issues] == [
        ("supplementary_layout", "error")
    ]
    placeholder.unlink()
    assert recover_staging(vault) == []
    assert "supplementary" not in _meta(vault)


def test_a_stray_keeps_its_number_only_while_it_is_free(
    vault: Path, tmp_path: Path
) -> None:
    """W3: a record or an earlier stray of the same batch holding the number
    means the stray is renumbered instead — into the lowest free number. A
    deleted file's number is free again."""
    _add_two(vault, tmp_path)  # si-01.csv, si-02.xlsx
    remove_supplementary(vault, A_ID, ["si-02"])  # 02 is free again
    si = vault / "papers" / A_ID / "supplementary"
    strays = [
        _blob(si, "si-01.txt", b"recorded number"),
        _blob(si, "si-02.pdf", b"number deleted"),
        _blob(si, "si-05.csv", b"free number"),
        _blob(si, "si-05.txt", b"same number, same batch"),
    ]

    add_supplementary(
        vault, A_ID, prepare_items(vault, A_ID, strays), allow_unverified=True
    )

    expected = ["si-01.csv", "si-02.pdf", "si-03.txt", "si-04.txt", "si-05.csv"]
    assert [r["file"] for r in _meta(vault)["supplementary"]] == expected
    assert sorted(os.listdir(si)) == sorted(expected)


def test_records_are_kept_in_number_order(vault: Path, tmp_path: Path) -> None:
    """S7: whatever order the files came in, si-01 is listed before si-02."""
    notes = _blob(tmp_path, "notes.xlsx", b"no publisher number")
    first = _blob(tmp_path, "ci9b00237_si_001.csv", b"numbered")
    result = add_supplementary(
        vault, A_ID, [SiItem(notes, notes.name), SiItem(first, first.name)],
        allow_unverified=True,
    )
    assert [p.name for p in result.paths] == ["si-01.csv", "si-02.xlsx"]
    assert [r["file"] for r in _meta(vault)["supplementary"]] == [
        "si-01.csv", "si-02.xlsx"
    ]


def test_a_file_in_a_leftover_removed_si_folder_is_inside_the_library(
    vault: Path, tmp_path: Path
) -> None:
    """``.trash/removed-si/`` means nothing any more: a file found there is
    like any other in the library — the move would delete it."""
    folder = vault / TRASH_DIRNAME / "removed-si" / A_ID
    folder.mkdir(parents=True)
    leftover = _blob(folder, "20261020T083000Z-si-02.xlsx", b"2")
    _blob(folder, "20261020T083000Z-si-02.xlsx.meta.yaml", b"record: {}\n")

    with pytest.raises(SupplementaryError, match="inside the library"):
        prepare_items(vault, A_ID, [leftover])
    with pytest.raises(SupplementaryError, match="inside the library"):
        add_supplementary(
            vault, A_ID, [SiItem(leftover, leftover.name)], allow_unverified=True
        )
    assert leftover.read_bytes() == b"2"
    assert "supplementary" not in _meta(vault)


def test_inside_the_library_is_decided_by_identity_not_spelling(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S8: where resolve() keeps a path as typed (macOS keeps the typed case),
    the path to another paper's PDF does not start with the library's own
    spelling — it is inside all the same."""
    alias = tmp_path / "alias-of-the-library"
    if not make_portable_link(alias, vault):
        pytest.skip("this filesystem cannot hold folder links")
    inside = alias / "papers" / B_ID / "paper.pdf"
    monkeypatch.setattr(
        Path, "resolve", lambda self, strict=False: Path(os.path.abspath(self))
    )
    with pytest.raises(SupplementaryError, match="inside the library"):
        prepare_items(vault, A_ID, [inside])
    assert inside.exists()


def _fs_ignores_case(where: Path) -> bool:
    """Probed, not read off sys.platform (as in commands/test_health.py)."""
    probe = where / "LitmanCaseProbe"
    probe.mkdir()
    try:
        return (where / "LITMANCASEPROBE").is_dir()
    finally:
        probe.rmdir()


def test_a_wrong_case_path_into_the_library_is_refused(
    vault: Path, tmp_path: Path
) -> None:
    if not _fs_ignores_case(tmp_path):
        pytest.skip(
            "Only where the filesystem itself ignores case does a case "
            "variant reach the library; here it names nothing."
        )
    wrong = vault.parent / vault.name.upper() / "PAPERS" / B_ID / "paper.pdf"
    with pytest.raises(SupplementaryError, match="inside the library"):
        prepare_items(vault, A_ID, [wrong])
    assert (vault / "papers" / B_ID / "paper.pdf").exists()


@_posix_only
@_not_root
def test_an_unreadable_folder_is_a_finding_not_a_crash(
    vault: Path, tmp_path: Path
) -> None:
    """S1: nothing that cannot be read may stop a health check or a push."""
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    si.chmod(0o000)
    try:
        drift = supplementary_drift(vault, A_ID, _meta(vault))
        issues = check_supplementary(vault, list_papers(vault))
        blocking = run_push_integrity_errors(vault, list_papers(vault))
    finally:
        si.chmod(0o755)
    assert drift.unchecked == [
        ("si-01.csv", "PermissionError"),
        ("si-02.xlsx", "PermissionError"),
        ("", "PermissionError"),
    ]
    assert {(i.category, i.severity) for i in issues} == {
        ("supplementary_unchecked", "warning")
    }
    assert not [i for i in blocking if i.category.startswith("supplementary")]


@_posix_only
@_not_root
def test_an_unreadable_file_is_unchecked(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    target = vault / "papers" / A_ID / "supplementary" / "si-02.xlsx"
    target.chmod(0o000)
    later = target.stat().st_mtime + 3600  # so the content has to be hashed
    os.utime(target, (later, later))
    try:
        drift = supplementary_drift(vault, A_ID, _meta(vault))
    finally:
        target.chmod(0o444)
    assert drift.unchecked == [("si-02.xlsx", "PermissionError")]
    assert drift.changed == []


@_posix_only
def test_a_failed_stamp_relocks_the_stray(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S4: the in-place unlock + stamp sit inside the try that re-locks."""
    si = vault / "papers" / A_ID / "supplementary"
    si.mkdir()
    stray = _blob(si, "si-05.xlsx")
    lock_truth_file(stray)
    meta_before = (vault / "papers" / A_ID / "metadata.yaml").read_bytes()

    def boom(_path: Path, _timestamp: float) -> None:
        raise PermissionError(1, "Operation not permitted (mocked)")

    monkeypatch.setattr(si_mod, "_stamp_mtime", boom)
    with pytest.raises(PermissionError, match="mocked"):
        add_supplementary(
            vault, A_ID, [SiItem(stray, stray.name)], allow_unverified=True
        )

    assert not os.access(stray, os.W_OK)
    assert (vault / "papers" / A_ID / "metadata.yaml").read_bytes() == meta_before


def test_a_record_without_checksum_is_kept_and_only_warned_about(
    vault: Path, tmp_path: Path
) -> None:
    """Deviation-5 ruling: no sha256 / bytes is a warning, not a structural
    error — the entry rides through add and remove untouched, and only its
    content goes unverified."""
    si = vault / "papers" / A_ID / "supplementary"
    si.mkdir()
    _blob(si, "si-01.pdf", b"%PDF-1.4 hand-recorded\n")
    meta_path = vault / "papers" / A_ID / "metadata.yaml"
    with meta_path.open("a", encoding="utf-8") as f:
        f.write("supplementary:\n  - file: si-01.pdf\n    original: old.pdf\n")
    legacy = {"file": "si-01.pdf", "original": "old.pdf"}

    issues = check_supplementary(vault, list_papers(vault))
    assert [(i.category, i.severity) for i in issues] == [
        ("supplementary_unchecked", "warning")
    ]
    assert "content can't be verified" in issues[0].message
    blocking = run_push_integrity_errors(vault, list_papers(vault))
    assert not [i for i in blocking if i.category.startswith("supplementary")]

    new = _blob(tmp_path, "ci9b00237_si_002.csv", b"2")
    add_supplementary(vault, A_ID, [SiItem(new, new.name)], allow_unverified=False)
    records = _meta(vault)["supplementary"]
    assert records[0] == legacy
    assert records[1]["file"] == "si-02.csv"

    remove_supplementary(vault, A_ID, ["si-02"])
    assert _meta(vault)["supplementary"] == [legacy]


# ---------------------------------------------------------------------------
# M2 carry-over ①: a volume without inode numbers (st_ino == 0)
# ---------------------------------------------------------------------------


def test_without_inodes_a_file_outside_the_library_is_taken(
    vault: Path, tmp_path: Path, no_inodes: None
) -> None:
    """Same disk as the library, yet outside it: compared by inode 0 alone,
    every such file used to read as "inside the library"."""
    src = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    items = prepare_items(vault, A_ID, [src])
    add_supplementary(vault, A_ID, items, allow_unverified=False)
    assert not src.exists()
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-01.xlsx"]


def test_without_inodes_the_upload_stash_is_taken(
    vault: Path, no_inodes: None
) -> None:
    stash = vault / ".litman-upload"
    stash.mkdir(exist_ok=True)
    upload = _blob(stash, "si-abc-def.xlsx")
    add_supplementary(
        vault, A_ID, [SiItem(upload, "ci9b00237_si_003.xlsx")],
        allow_unverified=False,
    )
    assert not upload.exists()
    assert _meta(vault)["supplementary"][0]["original"] == "ci9b00237_si_003.xlsx"


def test_without_inodes_the_library_is_still_the_library(
    vault: Path, no_inodes: None
) -> None:
    for inside in (
        vault / "papers" / B_ID / "paper.pdf",
        vault / "INDEX.json",
    ):
        with pytest.raises(SupplementaryError, match="inside the library"):
            prepare_items(vault, A_ID, [inside])
        with pytest.raises(SupplementaryError, match="inside the library"):
            add_supplementary(
                vault, A_ID, [SiItem(inside, "ci9b00237_si_1.pdf")],
                allow_unverified=True,
            )
        assert inside.exists()


# ---------------------------------------------------------------------------
# M2 carry-over ②: a link to a stray in the paper's own folder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["own-folder", "outside"])
def test_a_link_to_a_stray_takes_the_stray_with_it(
    vault: Path, tmp_path: Path, where: str
) -> None:
    """Recording the file behind the link removes the file, not only the link:
    left behind it would be a stray of the very SI just recorded, refused by
    `health-check --fix` forever as "already in the library"."""
    si = vault / "papers" / A_ID / "supplementary"
    si.mkdir()
    stray = _blob(si, "data.xlsx", b"the stray")
    lock_truth_file(stray)  # what the health-check re-lock sweep does first
    link = (si if where == "own-folder" else tmp_path) / "linked.xlsx"
    try:
        os.symlink(stray, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this system cannot make a symlink here: {exc}")

    items = prepare_items(vault, A_ID, [link])
    result = add_supplementary(vault, A_ID, items, allow_unverified=True)

    (target,) = result.paths
    assert target.read_bytes() == b"the stray"
    assert not os.path.lexists(link)
    assert not os.path.lexists(stray)
    assert supplementary_drift(vault, A_ID, _meta(vault)) == si_mod.SiDrift()


def test_a_link_from_outside_to_outside_loses_only_the_link(
    vault: Path, tmp_path: Path
) -> None:
    """Unchanged from M1: a source outside the library is the user's own
    file; going through a link to it, only the link is taken."""
    outside, _personal = _outside_folder(tmp_path)
    original = _blob(outside, "ci9b00237_si_002.xlsx", b"the user's file")
    link = tmp_path / "shortcut.xlsx"
    try:
        os.symlink(original, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this system cannot make a symlink here: {exc}")

    # Judged by the link's own name, which carries no DOI number.
    result = add_supplementary(
        vault, A_ID, prepare_items(vault, A_ID, [link]), allow_unverified=True
    )

    (target,) = result.paths
    assert target.read_bytes() == b"the user's file"
    assert not os.path.lexists(link)
    assert original.read_bytes() == b"the user's file"


# ---------------------------------------------------------------------------
# SiItem.approved — the user's "add anyway" for one file
# ---------------------------------------------------------------------------


def _set_doi(vault: Path, paper_id: str, doi: str) -> None:
    meta_path = vault / "papers" / paper_id / "metadata.yaml"
    text = meta_path.read_text(encoding="utf-8")
    old = next(line for line in text.splitlines() if line.startswith("doi:"))
    meta_path.write_text(text.replace(old, f"doi: {doi}"), encoding="utf-8")


def test_approved_lets_one_unconfirmed_file_in(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "Table_S2.xlsx")
    with pytest.raises(SupplementaryError, match="Can't confirm"):
        add_supplementary(vault, A_ID, [SiItem(sheet, sheet.name)], allow_unverified=False)
    add_supplementary(
        vault, A_ID, [SiItem(sheet, sheet.name, approved=True)], allow_unverified=False
    )
    assert _meta(vault)["supplementary"][0]["check"] == "user"


def test_a_verified_file_that_no_longer_verifies_needs_approval(
    vault: Path, tmp_path: Path
) -> None:
    """The GUI showed ✓ (its name carried the DOI number); the DOI was edited
    before Done. Without the user's own "add anyway" for it, core refuses."""
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    assert _verdict(vault, A_ID, sheet).status == "ok"
    _set_doi(vault, A_ID, "10.1000/zz.other")
    assert _verdict(vault, A_ID, sheet).status == "unverified"

    with pytest.raises(SupplementaryError, match="Can't confirm"):
        add_supplementary(vault, A_ID, [SiItem(sheet, sheet.name)], allow_unverified=False)
    assert sheet.exists()

    add_supplementary(
        vault, A_ID, [SiItem(sheet, sheet.name, approved=True)], allow_unverified=False
    )
    assert _meta(vault)["supplementary"][0]["check"] == "user"


def test_approved_never_lets_a_rejected_file_in(
    vault: Path, tmp_path: Path, make_text_pdf: MakePdf
) -> None:
    other = _si_pdf(make_text_pdf, "41467_2023_38851_MOESM1_ESM.pdf", B_TITLE)
    with pytest.raises(SupplementaryError, match="looks like the SI of"):
        add_supplementary(
            vault, A_ID, [SiItem(other, other.name, approved=True)],
            allow_unverified=True,
        )
    assert other.exists()
    assert "supplementary" not in _meta(vault)


# ---------------------------------------------------------------------------
# official_link / si_write_problem / recorded_si_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("meta", "link"),
    [
        ({"doi": A_DOI}, "https://doi.org/10.1021/acs.jcim.9b00237"),
        ({"doi": "https://doi.org/" + A_DOI}, "https://doi.org/10.1021/acs.jcim.9b00237"),
        ({"doi": "10.1002/(SICI)1097 <x>"}, "https://doi.org/10.1002/%28SICI%291097%20%3Cx%3E"),
        ({"doi": A_DOI, "arxiv-id": "2101.00001"}, "https://doi.org/10.1021/acs.jcim.9b00237"),
        ({"doi": None, "arxiv-id": "2101.00001"}, "https://arxiv.org/abs/2101.00001"),
        ({"doi": "", "arxiv-id": "hep-th/9901001"}, "https://arxiv.org/abs/hep-th/9901001"),
        ({"doi": None, "arxiv-id": None}, None),
        ({}, None),
    ],
)
def test_official_link(meta: dict[str, object], link: str | None) -> None:
    assert si_mod.official_link(meta) == link


def test_si_write_problem(vault: Path) -> None:
    assert si_mod.si_write_problem(vault, A_ID) is None
    meta_path = vault / "papers" / B_ID / "metadata.yaml"
    with meta_path.open("a", encoding="utf-8") as f:
        f.write("supplementary: si-01.pdf\n")
    assert "'supplementary' is not a list" in (
        si_mod.si_write_problem(vault, B_ID) or ""
    )
    (vault / "papers" / C_ID / "supplementary").write_text("x", encoding="utf-8")
    assert "is a file, not a folder" in (si_mod.si_write_problem(vault, C_ID) or "")
    with pytest.raises(PaperNotFoundError):
        si_mod.si_write_problem(vault, "2000_Nobody_Here")


def test_si_write_problem_names_a_linked_folder(vault: Path, tmp_path: Path) -> None:
    outside, _personal = _outside_folder(tmp_path)
    _link_si_folder(vault, outside)
    assert "is a link" in (si_mod.si_write_problem(vault, A_ID) or "")


def test_recorded_si_file_gives_only_recorded_files(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    path, record = si_mod.recorded_si_file(vault, A_ID, "si-02.xlsx")
    assert path == vault / "papers" / A_ID / "supplementary" / "si-02.xlsx"
    assert record["original"] == "ci9b00237_si_002.xlsx"

    _blob(vault / "papers" / A_ID / "supplementary", "si-07.csv", b"stray")
    for name in (
        "si-02",  # the short form is the CLI's, not a file name
        "SI-02.xlsx",
        "si-07.csv",  # on disk, never recorded
        "../paper.pdf",
        "..",
        "si-02.xlsx/..",
        "",
    ):
        with pytest.raises(SupplementaryError):
            si_mod.recorded_si_file(vault, A_ID, name)
    with pytest.raises(PaperNotFoundError):
        si_mod.recorded_si_file(vault, "2000_Nobody_Here", "si-01.csv")


def test_recorded_si_file_refuses_a_missing_or_linked_file(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    gone = si / "si-01.csv"
    unlock_truth_file(gone)
    gone.unlink()
    with pytest.raises(SupplementaryError, match="missing"):
        si_mod.recorded_si_file(vault, A_ID, "si-01.csv")

    _outside, personal = _outside_folder(tmp_path)
    try:
        os.symlink(personal, gone)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this system cannot make a symlink here: {exc}")
    with pytest.raises(SupplementaryError, match="not a plain file"):
        si_mod.recorded_si_file(vault, A_ID, "si-01.csv")


def test_recorded_si_file_refuses_a_linked_folder(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    si = vault / "papers" / A_ID / "supplementary"
    moved_away = tmp_path / "moved-away"
    shutil.move(str(si), str(moved_away))
    _link_si_folder(vault, moved_away)
    with pytest.raises(SupplementaryError, match="is a link"):
        si_mod.recorded_si_file(vault, A_ID, "si-01.csv")


# ---------------------------------------------------------------------------
# is_runnable_si / original_label
# ---------------------------------------------------------------------------

# The red line's own list: opening any of these with the system's default app
# runs it.
_SPEC_RUNNABLE = [
    "exe", "com", "bat", "cmd", "msi", "msp", "scr", "pif", "ps1", "vbs", "vbe",
    "js", "jse", "wsf", "wsh", "hta", "lnk", "reg", "cpl", "msc",
    "app", "command", "tool", "pkg", "scpt", "workflow",
    "sh", "run", "desktop", "appimage", "bin",
    "jar", "py",
]


@pytest.mark.parametrize("ext", _SPEC_RUNNABLE)
def test_is_runnable_si_refuses_every_type_the_spec_names(ext: str) -> None:
    assert si_mod.is_runnable_si(f"si-01.{ext}")
    assert si_mod.is_runnable_si(f"si-01.{ext.upper()}")


@pytest.mark.parametrize(
    "name",
    [
        "si-01.msix",
        "si-02.application",
        "si-03.url",
        "si-04.chm",
        "si-05.pyw",
        "si-06.terminal",
        "si-07.webloc",
        "si-08.mobileconfig",
        "si-09.bash",
        "si-10.deb",
        "si-11.flatpakref",
        "si-12.pl",
        "si-13.jnlp",
        "si-01.pdf.exe",  # recorded by hand: the last suffix is what runs
        "si-01",  # no suffix: what a script or a binary looks like
        "si-01.",
        ".bashrc",
    ],
)
def test_is_runnable_si_refuses_what_would_run(name: str) -> None:
    assert si_mod.is_runnable_si(name)


@pytest.mark.parametrize(
    "name",
    [
        "si-01.pdf",
        "si-02.xlsx",
        "si-03.docx",
        "si-04.docm",  # Office asks before it runs a macro
        "si-05.zip",
        "si-06.tar.gz",
        "si-07.7z",
        "si-08.dmg",
        "si-09.iso",
        "si-10.csv",
        "si-11.html",
        "si-12.svg",
        "si-13.txt",
        "si-14.ipynb",
        "si-15.exe.pdf",
    ],
)
def test_is_runnable_si_lets_documents_and_archives_open(name: str) -> None:
    assert not si_mod.is_runnable_si(name)


@pytest.mark.parametrize(
    "original", ["x.appref-ms", "x.settingcontent-ms", "x.library-ms", "README"]
)
def test_an_si_stored_without_a_suffix_is_refused(original: str) -> None:
    assert si_extension(original) == ""
    assert si_mod.is_runnable_si(f"si-01{si_extension(original)}")


@pytest.mark.parametrize(
    ("record", "label"),
    [
        ({"original": "ci9b00237_si_001.pdf"}, "ci9b00237_si_001.pdf"),
        ({"original": "Table S2 (final).xlsx"}, "Table S2 (final).xlsx"),
        ({"original": "Table\tS2.xlsx"}, "Table S2.xlsx"),
        ({"original": "Table\nS2.xlsx"}, "Table S2.xlsx"),
        ({"original": "Table\r\nS2.xlsx"}, "Table  S2.xlsx"),
        ({"original": "Table\u2028S2.xlsx"}, "Table S2.xlsx"),
        ({"original": "fdp\u202e.exe"}, "fdp .exe"),
        ({"original": "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645.pdf"},
         "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645.pdf"),
        ({"original": "  padded.csv\n"}, "padded.csv"),
        ({"original": "\t\n\u202e"}, None),
        ({"original": ""}, None),
        ({"original": None}, None),
        ({"original": 7}, None),
        ({}, None),
    ],
    ids=[
        "plain",
        "spaces",
        "tab",
        "newline",
        "crlf",
        "line-separator",
        "rtl-override",
        "zero-width-non-joiner-kept",
        "padding",
        "only-control-characters",
        "empty",
        "null",
        "not-text",
        "missing",
    ],
)
def test_original_label(record: dict[str, object], label: str | None) -> None:
    assert si_mod.original_label(record) == label


@pytest.mark.parametrize(
    ("code", "unprintable"),
    [
        (0x09, True),  # tab
        (0x0A, True),  # newline
        (0x202E, True),  # right-to-left override
        (0x2028, True),  # line separator
        (0xDCFF, True),  # lone surrogate
        (0x200C, False),  # zero-width non-joiner
        (0x200D, False),  # zero-width joiner
        (0x20, False),
        (0x61, False),
        (0x00E9, False),
    ],
)
def test_is_unprintable(code: int, unprintable: bool) -> None:
    assert si_mod.is_unprintable(chr(code)) is unprintable


def test_an_undecodable_byte_in_an_original_becomes_a_space() -> None:
    """A POSIX file name that is not UTF-8 reaches Python with a lone
    surrogate in place of each bad byte, which no UTF-8 stream can print."""
    original = b"Table\xffS2.xlsx".decode("utf-8", "surrogateescape")
    assert si_mod.original_label({"original": original}) == "Table S2.xlsx"


# ---------------------------------------------------------------------------
# SiItem.original — a name metadata.yaml can hold
# ---------------------------------------------------------------------------


def test_si_item_records_a_lone_surrogate_as_a_replacement_character() -> None:
    """A POSIX file name that is not UTF-8 reaches Python with a lone
    surrogate in place of each bad byte, and metadata.yaml's reader rejects
    one: the name to record carries U+FFFD instead. The path is left alone —
    it still has to name the real file."""
    path = Path("Downloads") / "caf\udce9.pdf"
    item = SiItem(path, path.name, approved=True)
    assert item.original == "caf\ufffd.pdf"
    assert item.path == path
    assert item.approved
    assert SiItem(path, "Table\ud800S2.xlsx").original == "Table\ufffdS2.xlsx"
    for name in (
        "ci9b00237_si_001.pdf",
        "\u8865\u5145\u6750\u6599 S1.pdf",
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645.pdf",
        "\U0001f468\u200d\U0001f52c notes.csv",
    ):
        assert SiItem(path, name).original == name


@_undecodable_names
def test_fix_records_an_undecodable_stray_under_a_name_that_reads_back(
    vault: Path,
) -> None:
    """``health-check --fix`` names a stray straight from the folder listing.
    Recorded with its lone surrogate, the paper's metadata.yaml no longer
    parsed: find_paper, lit show and the GUI all failed on it."""
    si = vault / "papers" / A_ID / "supplementary"
    si.mkdir()
    raw = b"ci9b00237_si_003_caf\xe9.xlsx"
    stray = _blob(si, os.fsdecode(raw))
    assert raw in os.listdir(os.fsencode(si))  # the name on disk is not UTF-8
    lock_truth_file(stray)  # what the health-check re-lock sweep does first

    counts = si_mod.fix_supplementary(vault, [A_ID], policy=si_mod.NO_PROMPT_POLICY)

    assert counts["supplementary_unrecorded"] == 1
    (record,) = _meta(vault)["supplementary"]
    assert record["original"] == "ci9b00237_si_003_caf\ufffd.xlsx"
    assert record["file"] == "si-01.xlsx"
    assert os.listdir(si) == ["si-01.xlsx"]
