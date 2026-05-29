"""Tests for litman.core.pdf_text (deterministic PDF text-layer extraction)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from litman.core.pdf_text import PdfTextError, extract_pdf_text


def test_extract_all_pages_in_order(
    make_text_pdf: Callable[..., Path],
) -> None:
    pdf = make_text_pdf(
        [["Alpha page"], ["Bravo page"], ["Charlie page"]]
    )
    out = extract_pdf_text(pdf)

    assert len(out) == 3
    assert "Alpha" in out[0]
    assert "Bravo" in out[1]
    assert "Charlie" in out[2]


def test_extract_specific_pages_preserves_requested_order(
    make_text_pdf: Callable[..., Path],
) -> None:
    pdf = make_text_pdf([["one"], ["two"], ["three"]])

    out = extract_pdf_text(pdf, pages=[3, 1])

    assert len(out) == 2
    assert "three" in out[0]
    assert "one" in out[1]


def test_out_of_range_page_yields_empty_string(
    make_text_pdf: Callable[..., Path],
) -> None:
    pdf = make_text_pdf([["only page"]])

    out = extract_pdf_text(pdf, pages=[1, 99])

    assert "only page" in out[0]
    assert out[1] == ""


def test_no_text_layer_page_yields_blank(
    make_text_pdf: Callable[..., Path],
) -> None:
    pdf = make_text_pdf([[" "]])  # whitespace-only == no usable text layer

    out = extract_pdf_text(pdf)

    assert len(out) == 1
    assert out[0].strip() == ""


def test_unreadable_file_raises(tmp_path: Path) -> None:
    not_a_pdf = tmp_path / "notes.txt"
    not_a_pdf.write_text("this is plainly not a PDF")

    with pytest.raises(PdfTextError):
        extract_pdf_text(not_a_pdf)
