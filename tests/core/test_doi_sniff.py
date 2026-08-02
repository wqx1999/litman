"""Tests for core.doi_sniff — DOI candidates from a PDF's text layer.

The regex/cleanup core is unit-tested through the pure ``_dois_from_texts``;
``sniff_dois`` itself is exercised end-to-end on a real (hand-assembled but
valid) PDF so the pypdf extraction path — the production default — is driven
for real, not mocked (the inject-seam rule: the real default gets at least
one end-to-end test).
"""

from __future__ import annotations

from pathlib import Path

from litman.core.doi_sniff import _MAX_CANDIDATES, _dois_from_texts, sniff_dois


def _minimal_pdf_with_text(text: str) -> bytes:
    """Assemble a tiny valid one-page PDF whose text layer contains ``text``.

    Hand-built (correct xref offsets included) because the test suite has no
    PDF-writing dependency; pypdf parses it like any real article PDF. The
    text must stay latin-1 and parenthesis-free (PDF literal-string syntax).
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i
        out += body
        out += b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref_pos,
    )
    return bytes(out)


# ---------------------------------------------------------------------------
# Pure extraction core
# ---------------------------------------------------------------------------


def test_finds_doi_and_strips_trailing_punctuation() -> None:
    pages = ["This article: doi:10.1093/nar/gkab813. See details inside."]
    assert _dois_from_texts(pages) == ["10.1093/nar/gkab813"]


def test_finds_doi_inside_resolver_url() -> None:
    pages = ["Available at https://doi.org/10.1021/ja00006a076 (accessed)"]
    assert _dois_from_texts(pages) == ["10.1021/ja00006a076"]


def test_first_seen_order_and_case_insensitive_dedup() -> None:
    pages = [
        "Main DOI 10.1000/OwnPaper here",
        "References: 10.1000/ownpaper and 10.2000/cited.one",
    ]
    assert _dois_from_texts(pages) == ["10.1000/OwnPaper", "10.2000/cited.one"]


def test_caps_candidates() -> None:
    pages = [" ".join(f"10.1000/ref{i}" for i in range(10))]
    assert len(_dois_from_texts(pages)) == _MAX_CANDIDATES


def test_bare_prefix_without_suffix_is_not_a_doi() -> None:
    # "10.1234/" with the suffix eaten by punctuation stripping must not
    # surface as an empty-suffix candidate.
    assert _dois_from_texts(["prefix 10.1234/. and nothing else"]) == []


def test_no_doi_yields_empty() -> None:
    assert _dois_from_texts(["no identifiers here", ""]) == []


# ---------------------------------------------------------------------------
# End-to-end through pypdf (real extraction path, real file)
# ---------------------------------------------------------------------------


def test_sniff_dois_reads_real_pdf_text_layer(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_minimal_pdf_with_text("doi:10.1093/nar/gkab813"))
    assert sniff_dois(pdf) == ["10.1093/nar/gkab813"]


def test_sniff_dois_unparseable_file_yields_empty(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.4\nnot really a pdf\n%%EOF\n")
    assert sniff_dois(junk) == []
