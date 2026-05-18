"""Shared pytest fixtures for litman tests.

Provides ``make_text_pdf``: a zero-dependency builder for small, multi-page,
text-bearing PDFs. pypdf (the only declared PDF dep) cannot synthesize text
content streams, and reportlab/fpdf are intentionally not dependencies, so
the fixture hand-assembles a minimal valid PDF whose pages carry the given
lines as extractable text. Used by the M20 code-URL scanner tests and the
``lit add`` full-text-scan integration tests.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest


def _build_text_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """Hand-assemble a minimal multi-page PDF carrying the given text lines.

    ``pages`` is a sequence of pages; each page is a sequence of text lines.
    The produced bytes parse with ``pypdf.PdfReader`` and each page's
    ``extract_text()`` returns the lines in order.
    """
    n_pages = len(pages)
    page_obj_nums = [4 + i * 2 for i in range(n_pages)]
    content_obj_nums = [4 + i * 2 + 1 for i in range(n_pages)]

    pieces: dict[int, bytes] = {}
    pieces[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{p} 0 R" for p in page_obj_nums)
    pieces[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    )
    pieces[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for i, lines in enumerate(pages):
        pn = page_obj_nums[i]
        cn = content_obj_nums[i]
        pieces[pn] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {cn} 0 R >>"
        ).encode()
        ops = "BT /F1 12 Tf 50 700 Td 14 TL\n"
        for ln in lines:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops += f"({esc}) Tj T*\n"
        ops += "ET"
        stream = ops.encode()
        pieces[cn] = (
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    max_obj = max(pieces)
    for num in range(1, max_obj + 1):
        offsets[num] = out.tell()
        out.write(f"{num} 0 obj\n".encode())
        out.write(pieces[num])
        out.write(b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {max_obj + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for num in range(1, max_obj + 1):
        out.write(f"{offsets[num]:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {max_obj + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF".encode()
    )
    return out.getvalue()


@pytest.fixture
def make_text_pdf(tmp_path: Path) -> Callable[..., Path]:
    """Factory: write a multi-page text PDF to tmp_path, return its path.

    Usage::

        pdf = make_text_pdf([["page 1 line"], ["page 2 line"]])
        pdf = make_text_pdf([["one page"]], name="custom.pdf")
    """

    def _make(pages: Sequence[Sequence[str]], name: str = "doc.pdf") -> Path:
        path = tmp_path / name
        path.write_bytes(_build_text_pdf(pages))
        return path

    return _make
