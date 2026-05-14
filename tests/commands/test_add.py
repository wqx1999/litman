"""End-to-end tests for `lit add` (CrossRef fetch is mocked via monkeypatch)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.exceptions import (
    AddError,
    DuplicateDOIError,
    IDError,
    LibraryNotFoundError,
)

_PAPER_ID = "2024_Chen_HELM-GPT-Macrocyclic"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A fresh vault under tmp_path."""
    return create_vault(tmp_path)


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    """A small file masquerading as a PDF."""
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% fake content for tests\n%%EOF\n")
    return pdf


SAMPLE_MESSAGE: dict[str, Any] = {
    "title": ["HELM-GPT: De novo macrocyclic peptide design"],
    "author": [
        {"family": "Chen", "given": "Yi"},
        {"family": "Wang", "given": "Lin"},
    ],
    "published-print": {"date-parts": [[2024]]},
    "container-title": ["Bioinformatics"],
    "DOI": "10.1093/bioinformatics/btae364",
}


@pytest.fixture
def mock_crossref(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `fetch_crossref` with one that returns SAMPLE_MESSAGE."""
    captured: dict[str, Any] = {}

    def _fake(doi: str, client=None) -> dict[str, Any]:
        captured["doi"] = doi
        return SAMPLE_MESSAGE

    monkeypatch.setattr("litman.commands.add.fetch_crossref", _fake)
    return captured


# ---------------------------------------------------------------------------
# CLI happy-path tests
# ---------------------------------------------------------------------------


def test_add_creates_paper_folder(
    vault: Path,
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    paper_id = _PAPER_ID
    paper_dir = vault / "papers" / paper_id
    assert paper_dir.is_dir()
    assert (paper_dir / "paper.pdf").is_file()
    assert (paper_dir / "metadata.yaml").is_file()
    assert (paper_dir / "notes.md").is_file()

    # Original PDF preserved (copy, not move).
    assert fake_pdf.is_file()


def test_add_writes_metadata_yaml_correctly(
    vault: Path,
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    paper_dir = vault / "papers" / _PAPER_ID
    yaml = YAML(typ="safe")
    metadata = yaml.load((paper_dir / "metadata.yaml").read_text())

    # Identity layer
    assert metadata["id"] == _PAPER_ID
    assert metadata["title"] == "HELM-GPT: De novo macrocyclic peptide design"
    assert metadata["authors"] == ["Chen, Yi", "Wang, Lin"]
    assert metadata["year"] == 2024
    assert metadata["journal"] == "Bioinformatics"
    assert metadata["doi"] == "10.1093/bioinformatics/btae364"
    # Audit layer (machine-maintained, ISO 8601 with timezone)
    assert "created-at" in metadata
    assert "updated-at" in metadata
    assert metadata["created-at"] == metadata["updated-at"]  # equal at creation
    # Parses cleanly as ISO 8601 with offset (datetime.fromisoformat handles
    # the "+02:00" suffix on Python 3.11+).
    parsed_ts = datetime.fromisoformat(metadata["created-at"])
    assert parsed_ts.tzinfo is not None
    # Default classification
    assert metadata["projects"] == []
    assert metadata["topics"] == []
    assert metadata["type"] == "research"
    # Default evaluation
    assert metadata["status"] == "inbox"
    assert metadata["priority"] == "B"
    # Default relations
    assert metadata["related"] == []
    # Code-binding layer (M3 will populate via `lit code add`)
    assert metadata["code-clones"] == []


def test_add_id_override(
    vault: Path,
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--id", "custom-id",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "custom-id").is_dir()
    assert not (vault / "papers" / _PAPER_ID).exists()


def test_add_uses_lit_library_env(
    vault: Path,
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIT_LIBRARY", str(vault))
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--doi", "10.1093/bioinformatics/btae364"],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / _PAPER_ID).is_dir()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_add_duplicate_doi_refused(
    vault: Path,
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
) -> None:
    """Second add with the same DOI fails with DuplicateDOIError (M2.9 layer 1)."""
    runner = CliRunner()
    args = [
        "add", str(fake_pdf),
        "--doi", "10.1093/bioinformatics/btae364",
        "--library", str(vault),
    ]
    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, args)
    assert second.exit_code != 0
    assert isinstance(second.exception, DuplicateDOIError)
    # DuplicateDOIError is a subclass of AddError, so legacy catches still work.
    assert isinstance(second.exception, AddError)
    # The error message should name the existing id so the user can jump to it.
    assert _PAPER_ID in str(second.exception)


def test_add_duplicate_doi_case_insensitive(
    vault: Path,
    fake_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DOI dedup is case-insensitive per the DOI Handbook."""
    # First add: mixed-case DOI returned by CrossRef.
    msg1 = {
        **SAMPLE_MESSAGE,
        "DOI": "10.1093/Bioinformatics/btae364",
    }
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: msg1,
    )
    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/Bioinformatics/btae364",
            "--library", str(vault),
        ],
    )
    assert first.exit_code == 0, first.output

    # Second add: ALL-CAPS DOI should still hit the dedup.
    msg2 = {
        **SAMPLE_MESSAGE,
        "DOI": "10.1093/BIOINFORMATICS/BTAE364",
    }
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: msg2,
    )
    second = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/BIOINFORMATICS/BTAE364",
            "--library", str(vault),
        ],
    )
    assert second.exit_code != 0
    assert isinstance(second.exception, DuplicateDOIError)


def test_add_id_collision_with_auto_suffix(
    vault: Path,
    fake_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same family/year/title but different DOI → auto-suffix _b (M2.9 layer 3)."""
    # First paper: standard SAMPLE_MESSAGE.
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: SAMPLE_MESSAGE,
    )
    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--library", str(vault),
        ],
    )
    assert first.exit_code == 0, first.output

    # Second paper: same author/year/title, DIFFERENT DOI (e.g., conference vs
    # journal version). DOI precheck passes; id collides; auto-suffix kicks in.
    msg2 = {**SAMPLE_MESSAGE, "DOI": "10.1093/different/btae999"}
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: msg2,
    )
    second = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/different/btae999",
            "--auto-suffix",
            "--library", str(vault),
        ],
    )
    assert second.exit_code == 0, second.output
    assert (vault / "papers" / _PAPER_ID).is_dir()
    assert (vault / "papers" / f"{_PAPER_ID}_b").is_dir()


def test_add_id_collision_non_tty_without_auto_suffix_refused(
    vault: Path,
    fake_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-TTY (CliRunner) with id collision and no --auto-suffix → AddError."""
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: SAMPLE_MESSAGE,
    )
    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--library", str(vault),
        ],
    )
    assert first.exit_code == 0, first.output

    msg2 = {**SAMPLE_MESSAGE, "DOI": "10.1093/different/btae999"}
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: msg2,
    )
    second = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/different/btae999",
            "--library", str(vault),
        ],
    )
    assert second.exit_code != 0
    assert isinstance(second.exception, AddError)
    # Should not be a duplicate-DOI error since the DOI differs.
    assert not isinstance(second.exception, DuplicateDOIError)
    assert "TTY" in str(second.exception) or "auto-suffix" in str(second.exception)


def test_add_id_override_collision_hard_fails(
    vault: Path,
    fake_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--id override + collision is a hard error even with --auto-suffix.

    Rationale: when the user passes --id explicitly, auto-suffixing their
    explicit choice would be surprising. If they want a suffix, they can pass
    it directly in the --id value.
    """
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: SAMPLE_MESSAGE,
    )
    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--id", "my-fixed-id",
            "--library", str(vault),
        ],
    )
    assert first.exit_code == 0, first.output

    msg2 = {**SAMPLE_MESSAGE, "DOI": "10.1093/different/btae999"}
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: msg2,
    )
    second = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/different/btae999",
            "--id", "my-fixed-id",
            "--auto-suffix",
            "--library", str(vault),
        ],
    )
    assert second.exit_code != 0
    assert isinstance(second.exception, AddError)
    assert not isinstance(second.exception, DuplicateDOIError)


def test_add_no_library_discoverable(
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("LIT_LIBRARY", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd has no lit-config.yaml
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--doi", "10.1/x"],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, LibraryNotFoundError)


def test_add_nonexistent_pdf_rejected_by_click(
    vault: Path,
    mock_crossref: dict[str, Any],
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", "/nonexistent.pdf",
            "--doi", "10.1/x",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    # Click rejects pre-flight; no fetch happens.
    assert "doi" not in mock_crossref


def test_add_missing_year_raises_id_error(
    vault: Path,
    fake_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_year_message = {
        "title": ["X"],
        "author": [{"family": "Smith", "given": "Jane"}],
        "container-title": ["J"],
        "DOI": "10.1/x",
    }
    monkeypatch.setattr(
        "litman.commands.add.fetch_crossref",
        lambda doi, client=None: no_year_message,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1/x",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, IDError)


def test_add_passes_doi_to_fetcher(
    vault: Path,
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert mock_crossref["doi"] == "10.1093/bioinformatics/btae364"
