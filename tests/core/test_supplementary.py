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
    mentions_supplementary,
    name_matches,
    plan_names,
    prepare_items,
    remove_supplementary,
    si_extension,
    supplementary_drift,
    verify_batch,
)
from litman.core.trash import TRASH_DIRNAME, is_trash_entry_name
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
# §核验 0 — does the article mention SI?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Supporting Information is available free of charge.",
        "See the Supplementary Materials for the full protocol.",
        "Supplemental Table 3 lists all peptides.",
        "Additional file 1: Table S1 summarises the runs.",
        "The code is in Additional file2 of this article.",
        "Methods are detailed in SI Appendix.",
        "Electronic supplementary information (ESI) available.",
    ],
)
def test_mentions_supplementary_each_phrasing(
    make_text_pdf: MakePdf, line: str
) -> None:
    # On page 2: the whole text is searched, not the first page.
    pdf = make_text_pdf([["A paper about peptides"], ["Results", line]])
    assert mentions_supplementary(pdf) is True


def test_mentions_supplementary_esi_ms_is_not_si(make_text_pdf: MakePdf) -> None:
    pdf = make_text_pdf(
        [["Peptide masses were confirmed by ESI-MS and HPLC (ESI+ mode)."]]
    )
    assert mentions_supplementary(pdf) is False


def test_mentions_supplementary_none_without_a_text_layer(
    make_text_pdf: MakePdf, tmp_path: Path
) -> None:
    assert mentions_supplementary(make_text_pdf([[]], name="scan.pdf")) is None
    broken = _blob(tmp_path, "broken.pdf", b"%PDF-1.4\nnot really\n")
    assert mentions_supplementary(broken) is None


def test_mentions_supplementary_across_a_line_break(make_text_pdf: MakePdf) -> None:
    pdf = make_text_pdf([["see the Supporting", "Information for the tables"]])
    assert mentions_supplementary(pdf) is True


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


def test_f_only_when_this_title_is_not_covered(
    vault: Path, make_text_pdf: MakePdf
) -> None:
    # An SI that also cites another paper's title is still this paper's.
    both = make_text_pdf([["Supporting Information", A_TITLE, B_TITLE]], name="si.pdf")
    v = _verdict(vault, A_ID, both)
    assert (v.status, v.check) == ("ok", "title")


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
    since "this title is not covered" cannot be judged — rejects (f)."""
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


def test_plan_names_continues_past_every_taken_number() -> None:
    # Recorded, on disk (any case), or waiting in the trash: never reused.
    taken = ["si-01.pdf", "si-07.zip", "SI-09.PDF", "notes.txt"]
    assert plan_names(taken, ["a.pdf"]) == ["si-10.pdf"]
    assert plan_names(["si-99.pdf"], ["a.pdf"]) == ["si-100.pdf"]


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


def test_remove_moves_to_the_trash_with_its_record(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    ((name, dest),) = remove_supplementary(vault, A_ID, ["si-02"])

    assert name == "si-02.xlsx"
    assert dest is not None and dest.read_bytes() == b"2"
    assert dest.parent == vault / TRASH_DIRNAME / "removed-si" / A_ID
    assert dest.name.endswith("-si-02.xlsx")
    assert os.access(dest, os.W_OK)  # trashed copies are not locked
    sidecar = dest.parent / f"{dest.name}.meta.yaml"
    assert "original: ci9b00237_si_002.xlsx" in sidecar.read_text(encoding="utf-8")
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-01.csv"]
    assert not (vault / "papers" / A_ID / "supplementary" / "si-02.xlsx").exists()
    # The container is not a trash entry: `lit trash restore` never sees it.
    assert not is_trash_entry_name("removed-si")


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
    assert remove_supplementary(vault, A_ID, ["si-01"]) == [("si-01.csv", None)]
    assert [r["file"] for r in _meta(vault)["supplementary"]] == ["si-02.xlsx"]


@pytest.mark.parametrize("name", ["si-03", "si-02.pdf", "supplement.pdf", "S2"])
def test_remove_unknown_name(vault: Path, tmp_path: Path, name: str) -> None:
    _add_two(vault, tmp_path)
    with pytest.raises(SupplementaryError, match="has no SI file"):
        remove_supplementary(vault, A_ID, [name])
    assert len(_meta(vault)["supplementary"]) == 2


def test_remove_failed_move_puts_the_record_back(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_two(vault, tmp_path)

    def stuck(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError(13, "in use by another program (mocked)")

    monkeypatch.setattr(si_mod, "move_si_to_trash", stuck)
    with pytest.raises(SupplementaryError, match="its record is back"):
        remove_supplementary(vault, A_ID, ["si-02"])

    target = vault / "papers" / A_ID / "supplementary" / "si-02.xlsx"
    assert target.exists()
    assert sorted(r["file"] for r in _meta(vault)["supplementary"]) == [
        "si-01.csv", "si-02.xlsx"
    ]
    if not _REAL_WIN32:
        assert not os.access(target, os.W_OK)  # locked again


def test_put_back_from_the_trash_gets_a_new_number(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    ((_name, dest),) = remove_supplementary(vault, A_ID, ["si-02"])
    assert dest is not None

    items = prepare_items(vault, A_ID, [dest])
    # Judged by the name it was first added under: the trash name has no DOI.
    assert items[0].original == "ci9b00237_si_002.xlsx"
    result = add_supplementary(vault, A_ID, items, allow_unverified=False)

    assert [p.name for p in result.paths] == ["si-03.xlsx"]  # 02 is not reused
    assert not dest.exists()
    assert not (dest.parent / f"{dest.name}.meta.yaml").exists()
    assert result.records[0]["check"] == "filename"


def test_numbering_skips_numbers_waiting_in_the_trash(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    remove_supplementary(vault, A_ID, ["si-02"])
    new = _blob(tmp_path, "ci9b00237_si_009.pdf", b"9")
    result = add_supplementary(vault, A_ID, [SiItem(new, new.name)], allow_unverified=False)
    assert [p.name for p in result.paths] == ["si-03.pdf"]


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
    """W3: a record, a removed file in the trash, or an earlier stray of the
    same batch holding the number means the stray is renumbered instead."""
    _add_two(vault, tmp_path)  # si-01.csv, si-02.xlsx
    remove_supplementary(vault, A_ID, ["si-02"])  # 02 now waits in the trash
    si = vault / "papers" / A_ID / "supplementary"
    strays = [
        _blob(si, "si-01.txt", b"recorded number"),
        _blob(si, "si-02.pdf", b"number in the trash"),
        _blob(si, "si-05.csv", b"free number"),
        _blob(si, "si-05.txt", b"same number, same batch"),
    ]

    add_supplementary(
        vault, A_ID, prepare_items(vault, A_ID, strays), allow_unverified=True
    )

    expected = ["si-01.csv", "si-05.csv", "si-06.txt", "si-07.pdf", "si-08.txt"]
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


def test_only_removed_si_files_come_back_from_the_trash(
    vault: Path, tmp_path: Path
) -> None:
    """S3: a sidecar, or a file sitting loose in the container, is refused."""
    _add_two(vault, tmp_path)
    ((_name, dest),) = remove_supplementary(vault, A_ID, ["si-02"])
    assert dest is not None
    sidecar = dest.parent / f"{dest.name}.meta.yaml"
    loose = _blob(vault / TRASH_DIRNAME / "removed-si", "loose.csv", b"loose")
    for path in (sidecar, loose):
        with pytest.raises(SupplementaryError, match="inside the library"):
            prepare_items(vault, A_ID, [path])
        assert path.exists()


@_posix_only
def test_a_link_into_the_trash_takes_the_file_with_its_sidecar(
    vault: Path, tmp_path: Path
) -> None:
    """S3: the real trashed file and its sidecar go together, not just the
    link (which used to leave the file behind without its record)."""
    _add_two(vault, tmp_path)
    ((_name, dest),) = remove_supplementary(vault, A_ID, ["si-02"])
    assert dest is not None
    link = tmp_path / "back.xlsx"
    link.symlink_to(dest)

    items = prepare_items(vault, A_ID, [link])
    assert items[0].original == "ci9b00237_si_002.xlsx"
    add_supplementary(vault, A_ID, items, allow_unverified=False)

    assert not dest.exists()
    assert not (dest.parent / f"{dest.name}.meta.yaml").exists()
    assert not link.is_symlink()


def test_putting_the_last_file_back_prunes_no_higher_than_the_container(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    ((_name, dest),) = remove_supplementary(vault, A_ID, ["si-02"])
    assert dest is not None
    add_supplementary(
        vault, A_ID, prepare_items(vault, A_ID, [dest]), allow_unverified=False
    )
    assert not (vault / TRASH_DIRNAME / "removed-si").exists()
    assert (vault / TRASH_DIRNAME).is_dir()


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
