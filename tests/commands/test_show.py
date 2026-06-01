"""Tests for `lit show <id>`."""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# --format json (M33)
# ---------------------------------------------------------------------------


def test_show_json_emits_full_metadata(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["show", "2024_X_Foo", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    meta = json.loads(result.output)
    assert meta["id"] == "2024_X_Foo"
    assert meta["title"] == "Foo paper"
    assert meta["doi"] == "10.1234/test-foo"
    assert meta["authors"] == ["Smith, J"]
    # Full metadata, not the INDEX projection: `doi` + `authors` are present
    # together (authors is NOT in the INDEX projection).


def test_show_json_serializes_date_and_datetime(vault: Path) -> None:
    """The §9 trap: a paper WITH read-date / created-at must not raise.

    The YAML safe-loader parses created-at into a datetime and read-date into a
    date; json.dumps cannot serialize either natively, so default=str must
    bridge them.
    """
    paper_dir = vault / "papers" / "2024_X_Foo"
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_X_Foo\n"
        "year: 2024\n"
        "title: Foo paper\n"
        "created-at: 2026-05-26T10:00:00+02:00\n"
        "updated-at: 2026-05-27T11:00:00+02:00\n"
        "read-date: 2026-05-28\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["show", "2024_X_Foo", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert result.exception is None
    meta = json.loads(result.output)
    assert meta["read-date"] == "2026-05-28"
    assert meta["created-at"].startswith("2026-05-26")


def test_show_json_tolerates_missing_fields(vault: Path) -> None:
    """A minimal metadata file (only identity fields) serializes fine."""
    paper_dir = vault / "papers" / "2024_X_Foo"
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_X_Foo\nyear: 2024\ntitle: Foo\n", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["show", "2024_X_Foo", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    meta = json.loads(result.output)
    assert meta == {"id": "2024_X_Foo", "year": 2024, "title": "Foo"}


def test_show_table_is_default(vault: Path) -> None:
    """Default (no --format) renders the Panel view, not JSON."""
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "PDF:" in result.output
    assert "Notes:" in result.output


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
