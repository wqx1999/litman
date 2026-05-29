"""CLI-level tests for ``lit pdf-text``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from click.testing import CliRunner

from litman.cli import cli


def test_pdf_text_prints_all_pages_joined_by_formfeed(
    make_text_pdf: Callable[..., Path],
) -> None:
    pdf = make_text_pdf([["FirstPageMarker"], ["SecondPageMarker"]])

    result = CliRunner().invoke(cli, ["pdf-text", str(pdf)])

    assert result.exit_code == 0
    assert "FirstPageMarker" in result.output
    assert "SecondPageMarker" in result.output
    assert "\f" in result.output  # page separator


def test_pdf_text_pages_filter(
    make_text_pdf: Callable[..., Path],
) -> None:
    pdf = make_text_pdf([["FirstPageMarker"], ["SecondPageMarker"]])

    result = CliRunner().invoke(cli, ["pdf-text", str(pdf), "--pages", "1"])

    assert result.exit_code == 0
    assert "FirstPageMarker" in result.output
    assert "SecondPageMarker" not in result.output


def test_pdf_text_missing_file_is_usage_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["pdf-text", str(tmp_path / "nope.pdf")]
    )
    assert result.exit_code == 2  # Click's exists=True


def test_pdf_text_bad_pages_spec_is_usage_error(
    make_text_pdf: Callable[..., Path],
) -> None:
    pdf = make_text_pdf([["x"]])

    result = CliRunner().invoke(
        cli, ["pdf-text", str(pdf), "--pages", "abc"]
    )
    assert result.exit_code == 2


def test_pdf_text_non_pdf_exits_1(tmp_path: Path) -> None:
    bogus = tmp_path / "notes.txt"
    bogus.write_text("plainly not a PDF")

    result = CliRunner().invoke(cli, ["pdf-text", str(bogus)])

    assert result.exit_code == 1
    assert "cannot read" in result.output


def test_pdf_text_scanned_pdf_exits_3(
    make_text_pdf: Callable[..., Path],
) -> None:
    pdf = make_text_pdf([[" "]])  # whitespace-only == no text layer

    result = CliRunner().invoke(cli, ["pdf-text", str(pdf)])

    assert result.exit_code == 3
    assert "no text extracted" in result.output
