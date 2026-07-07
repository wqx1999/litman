"""End-to-end tests for `lit add` (CrossRef fetch is mocked via monkeypatch)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
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


_FAKE_PDF_BYTES = b"%PDF-1.4\n% fake content for tests\n%%EOF\n"


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    """A small file masquerading as a PDF."""
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(_FAKE_PDF_BYTES)
    return pdf


def _restore_fake_pdf(p: Path) -> None:
    """Re-create the fake PDF after `lit add` consumed it (mv semantics).

    `lit add` now removes the source after a successful ingest, so any test
    that drives a second `lit add` invocation pointed at the same path needs
    to mint a fresh source first — otherwise click rejects the second call
    at the usage layer (path does not exist) before reaching the duplicate /
    collision logic the test is actually exercising.
    """
    p.write_bytes(_FAKE_PDF_BYTES)


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

    # Original PDF moved into the vault (mv semantics, not cp): the source
    # disappearing is the user-visible "ingest succeeded" signal.
    assert not fake_pdf.exists()

    # D2: the success panel states the mv semantics explicitly so the source
    # file "disappearing" reads as intended, not as data loss.
    assert "Source PDF moved into the vault" in result.output


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_add_locks_truth_files_readonly(
    vault: Path,
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
) -> None:
    """After `lit add`: metadata.yaml + paper.pdf read-only, notes.md writable.

    Also TAXONOMY.md (seeded + locked at create_vault) stays read-only. (AC#1)
    """
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
    assert not os.access(paper_dir / "metadata.yaml", os.W_OK)
    assert not os.access(paper_dir / "paper.pdf", os.W_OK)
    assert not os.access(vault / "TAXONOMY.md", os.W_OK)
    # Not locked, by design.
    assert os.access(paper_dir / "notes.md", os.W_OK)
    assert os.access(vault / "lit-config.yaml", os.W_OK)
    assert os.access(vault / "INDEX.json", os.W_OK)


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
    assert metadata["type"] is None
    # Default evaluation
    assert metadata["status"] == "inbox"
    assert metadata["priority"] is None
    # Default relations
    assert metadata["related"] == []
    # Code-binding layer (M3 will populate via `lit code add`)
    assert metadata["code-clones"] == []


def test_add_llm_json_persists_arxiv_id(
    vault: Path,
    fake_pdf: Path,
    tmp_path: Path,
) -> None:
    # Regression (bug-report 2026-06-02_3 #1): a no-DOI preprint added via the
    # LLM-JSON path had its arxiv-id silently dropped at _build_metadata (the
    # field was hardcoded None instead of read from the parsed dict), so the
    # exported BibTeX entry lost its only locator. The arxiv-id the LLM
    # extracts from the PDF must land in metadata.yaml. (LLM path, no mock.)
    runner = CliRunner()
    j = tmp_path / "preprint.json"
    j.write_text(
        json.dumps({
            "title": "A peptide preprint",
            "authors": ["Chen, Yi"],
            "year": 2024,
            "arxiv-id": "2401.12345",
        }),
        encoding="utf-8",
    )
    result = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--from-llm-json", str(j),
         "--id", "2024_preprint", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    yaml = YAML(typ="safe")
    metadata = yaml.load(
        (vault / "papers" / "2024_preprint" / "metadata.yaml").read_text()
    )
    assert metadata["arxiv-id"] == "2401.12345"
    # No DOI on this preprint: arxiv-id is the sole locator.
    assert metadata["doi"] in ("", None)


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


@pytest.mark.parametrize(
    "bad_id",
    ["../../escape", "../escape", "foo/bar", "..", ".hidden", "a\\b"],
)
def test_add_id_override_rejects_path_traversal(
    vault: Path,
    fake_pdf: Path,
    bad_id: str,
) -> None:
    # Review F23: a malformed --id (path traversal, slash, leading dot) must
    # be rejected during parsing — before the command body reads or, fatally,
    # *deletes* the source PDF. The _validate_id_override callback guarantees
    # this. No CrossRef mock is needed: the body never runs.
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--id", bad_id,
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert "Invalid id" in result.output
    # The source PDF is untouched (never reached the move-and-delete path).
    assert fake_pdf.is_file()
    assert fake_pdf.read_bytes() == _FAKE_PDF_BYTES
    # Nothing was written anywhere under papers/.
    assert list((vault / "papers").iterdir()) == []


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

    _restore_fake_pdf(fake_pdf)
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
    _restore_fake_pdf(fake_pdf)
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


def test_add_llm_url_form_doi_dedupes_against_bare(
    vault: Path,
    fake_pdf: Path,
    tmp_path: Path,
) -> None:
    # Review F10: add a paper via the LLM path with a bare DOI, then try to add
    # the same paper with a resolver-URL DOI. Without canonicalization the two
    # forms compared unequal and the paper was ingested twice; now the second
    # add is refused as a duplicate. (LLM path, so no network mock needed.)
    runner = CliRunner()
    j1 = tmp_path / "m1.json"
    j1.write_text(
        json.dumps({
            "title": "Macrocyclic peptide design",
            "authors": ["Chen, Yi"],
            "year": 2024,
            "doi": "10.1093/bioinformatics/btae364",
        }),
        encoding="utf-8",
    )
    first = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--from-llm-json", str(j1), "--library", str(vault)],
    )
    assert first.exit_code == 0, first.output

    _restore_fake_pdf(fake_pdf)
    j2 = tmp_path / "m2.json"
    j2.write_text(
        json.dumps({
            "title": "Macrocyclic peptide design",
            "authors": ["Chen, Yi"],
            "year": 2024,
            "doi": "https://doi.org/10.1093/bioinformatics/btae364",
        }),
        encoding="utf-8",
    )
    second = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--from-llm-json", str(j2), "--library", str(vault)],
    )
    assert second.exit_code != 0
    assert isinstance(second.exception, DuplicateDOIError)


def test_add_title_with_rich_markup_does_not_crash(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    # Review F24: a title carrying Rich markup ("[/]" / "[red]") must not crash
    # the final success Panel, which renders AFTER the paper is ingested and
    # the source PDF deleted (so a crash there looks like a failed add).
    j = tmp_path / "m.json"
    j.write_text(
        json.dumps({
            "title": "Weird [/] [red]Title",
            "authors": ["Doe, Jane"],
            "year": 2024,
            "doi": "10.9/markup",
        }),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--from-llm-json", str(j), "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert list((vault / "papers").iterdir())  # paper actually ingested


def test_add_reconcile_failure_warns_not_crash(
    vault: Path,
    fake_pdf: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review F25: a post-commit derived-rebuild failure must not crash lit add
    # or skip the source-PDF cleanup — the paper is committed, INDEX just lags.
    def _boom(*a: object, **k: object) -> dict[str, int]:
        raise OSError("simulated views rebuild failure")

    monkeypatch.setattr("litman.commands.add.reconcile_derived", _boom)
    j = tmp_path / "m.json"
    j.write_text(
        json.dumps({
            "title": "Fine Paper",
            "authors": ["Doe, Jane"],
            "year": 2024,
            "doi": "10.9/ok",
        }),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--from-llm-json", str(j), "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "rebuild failed" in result.output.lower()
    assert list((vault / "papers").iterdir())  # paper committed
    assert not fake_pdf.exists()  # source PDF still removed (mv semantics)


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
    _restore_fake_pdf(fake_pdf)
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
    _restore_fake_pdf(fake_pdf)
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
    _restore_fake_pdf(fake_pdf)
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


# ---------------------------------------------------------------------------
# M20: full-text code-URL recall scan surfaced in the add panel
# ---------------------------------------------------------------------------


def test_add_surfaces_code_candidates(
    vault: Path,
    mock_crossref: dict[str, Any],
    make_text_pdf: Callable[..., Path],
) -> None:
    """A PDF whose only github URL sits on a tail page is ingested AND the
    URL is surfaced in the structurally-stable code_candidates block."""
    pdf = make_text_pdf(
        [
            ["Title page, metadata only."],
            ["Methods, no links."],
            [
                "Code availability: https://github.com/team/deliverable "
                "is the released code."
            ],
        ],
        name="tail.pdf",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    # Paper still fully ingested.
    paper_dir = vault / "papers" / _PAPER_ID
    assert (paper_dir / "paper.pdf").is_file()
    assert (paper_dir / "metadata.yaml").is_file()
    # Stable block markers + candidate line are present in the panel.
    assert "[code_candidates]" in result.output
    assert "[/code_candidates]" in result.output
    assert "https://github.com/team/deliverable" in result.output
    assert "p3" in result.output


def test_add_no_code_candidate_no_error(
    vault: Path,
    mock_crossref: dict[str, Any],
    make_text_pdf: Callable[..., Path],
) -> None:
    """A PDF with no code-host URL ingests cleanly and the block renders the
    explicit no-false-positive sentinel line."""
    pdf = make_text_pdf(
        [["Plain prose, no repository link at all."]],
        name="plain.pdf",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(pdf),
            "--doi", "10.1093/bioinformatics/btae364",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / _PAPER_ID).is_dir()
    assert "no code repo URL found in full text" in result.output


def test_add_scan_failure_does_not_block_ingest(
    vault: Path,
    fake_pdf: Path,
    mock_crossref: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected scan failure: paper is STILL fully ingested, command exits 0,
    and a warning is surfaced (ingest not held hostage to scan success)."""

    def _boom(_path: Path) -> list[dict]:
        raise RuntimeError("injected scan explosion")

    monkeypatch.setattr("litman.commands.add.scan_code_urls", _boom)

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
    assert (paper_dir / "paper.pdf").is_file()
    assert (paper_dir / "metadata.yaml").is_file()
    assert (paper_dir / "notes.md").is_file()
    assert "Warning" in result.output
    assert "scan failed" in result.output
