"""Tests for `lit show <id>`."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault
from litman.exceptions import PaperNotFoundError


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = create_vault(tmp_path)
    paper_dir = v / "papers" / "2024_X_Foo"
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_X_Foo\n"
        "year: 2024\n"
        "title: Foo paper\n"
        "doi: 10.1234/test-foo\n"
        "authors:\n"
        "  - Smith, J\n",
        encoding="utf-8",
    )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4 fake\n")
    (paper_dir / "notes.md").write_text("# notes\n", encoding="utf-8")
    return v


def test_show_existing_paper_prints_metadata_and_paths(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0
    assert "2024_X_Foo" in result.output
    assert "Foo paper" in result.output
    assert "Smith, J" in result.output
    assert "PDF:" in result.output
    assert "Notes:" in result.output
    assert "paper.pdf" in result.output
    assert "notes.md" in result.output


def test_show_missing_paper_raises_paper_not_found(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Missing", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_show_warns_about_missing_pdf(vault: Path) -> None:
    (vault / "papers" / "2024_X_Foo" / "paper.pdf").unlink()
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0
    assert "missing!" in result.output


def test_show_warns_about_missing_notes(vault: Path) -> None:
    (vault / "papers" / "2024_X_Foo" / "notes.md").unlink()
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0
    assert "missing!" in result.output


def test_show_rejects_invalid_id_with_path_traversal(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "../../../etc/passwd", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_show_accepts_fuzzy_substring(vault: Path) -> None:
    """M11: a unique case-insensitive substring resolves to the paper id."""
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "foo", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "2024_X_Foo" in result.output


def test_show_accepts_paper_doi(vault: Path) -> None:
    """M11: --paper-doi reverse-looks-up the id via INDEX-less linear scan."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["show", "--paper-doi", "10.1234/test-foo", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "2024_X_Foo" in result.output


def test_show_paper_id_and_doi_mutually_exclusive(vault: Path) -> None:
    """M11: setting both is an error from the unified XOR helper."""
    from litman.exceptions import LitmanError

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "show",
            "2024_X_Foo",
            "--paper-doi",
            "10.1234/test-foo",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    assert "mutually exclusive" in str(result.exception)


def test_show_neither_id_nor_doi_errors(vault: Path) -> None:
    from litman.exceptions import LitmanError

    runner = CliRunner()
    result = runner.invoke(cli, ["show", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)


def test_show_ambiguous_substring_lists_candidates(vault: Path) -> None:
    """M11: 2+ matches must surface the candidate ids in the error message."""
    (vault / "papers" / "2025_X_Foobar").mkdir(parents=True)
    (vault / "papers" / "2025_X_Foobar" / "metadata.yaml").write_text(
        "id: 2025_X_Foobar\nyear: 2025\ntitle: Foobar\nauthors:\n  - Jones, K\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "foo", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)
    msg = str(result.exception)
    assert "Ambiguous" in msg
    assert "2024_X_Foo" in msg
    assert "2025_X_Foobar" in msg
