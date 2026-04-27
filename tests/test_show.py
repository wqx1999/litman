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
