"""Deterministic per-page PDF text-layer extraction.

The weak-LLM-tolerant fallback for "read this PDF" (invariant #5). The
skill's ingest ladder prefers a model's native multimodal read, then a PDF
MCP; this module backs the bottom rung — a pure pypdf text-layer pull that
works wherever ``lit`` is installed (pypdf is a hard dependency), with no
model capability, no network, and no system tool (poppler / pdftoppm) needed.

It reads only the *embedded text layer*: scanned / image-only PDFs yield
empty strings (the caller distinguishes that and routes back up the ladder
to a multimodal reader or OCR). Layout reconstruction is pypdf's, not
pdftotext's — adequate for harvesting page-1 metadata (title / authors /
year / DOI), not for faithful figure/table reading.

Distinct from :mod:`litman.core.code_scan`, which is a recall-only URL
scanner that swallows every error into ``[]``. Here a genuinely unreadable
file is an error the caller must see, so :func:`extract_pdf_text` raises
:class:`PdfTextError`; only per-page extraction hiccups degrade to ``""``.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


class PdfTextError(Exception):
    """Raised when a path cannot be opened/parsed as a PDF at all."""


def extract_pdf_text(
    pdf_path: Path, pages: list[int] | None = None
) -> list[str]:
    """Extract the embedded text layer, one string per requested page.

    Args:
        pdf_path: Filesystem path to the PDF.
        pages: 1-based page numbers to extract, in the given order. ``None``
            extracts every page in document order. Out-of-range page numbers
            yield ``""`` (the caller still gets a slot for each requested
            page) rather than raising.

    Returns:
        A list of per-page text strings aligned to the requested pages.

    Raises:
        PdfTextError: The file cannot be opened or parsed as a PDF. A
            per-page ``extract_text()`` failure does NOT raise — that single
            page degrades to ``""`` so one bad page never aborts the rest.
    """
    try:
        reader = PdfReader(str(pdf_path))
        all_pages = reader.pages
        n = len(all_pages)
    except Exception as exc:  # noqa: BLE001 — surface any pypdf failure uniformly
        raise PdfTextError(str(exc)) from exc

    wanted = range(1, n + 1) if pages is None else pages

    out: list[str] = []
    for p in wanted:
        if p < 1 or p > n:
            out.append("")
            continue
        try:
            out.append(all_pages[p - 1].extract_text() or "")
        except Exception:  # noqa: BLE001 — one unreadable page must not abort
            out.append("")
    return out
