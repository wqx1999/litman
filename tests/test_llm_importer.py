"""Tests for the LLM-JSON importer + `lit add --from-llm-json` (M4.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.exceptions import AddError, ImporterError
from litman.importers.llm import LLMCandidateMeta, parse_llm_json

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%test\n%%EOF\n")
    return pdf


_FULL_LLM_PAYLOAD = {
    "title": "De novo macrocyclic peptide design",
    "authors": ["Chen, Yi", "Wang, Lin"],
    "year": 2024,
    "doi": "10.1093/bioinformatics/btae364",
    "journal": "Bioinformatics",
    "arxiv-id": None,
    "abstract": "We present a generative model for ...",
}


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# LLMCandidateMeta schema
# ---------------------------------------------------------------------------


def test_schema_full_payload_parses() -> None:
    meta = LLMCandidateMeta.model_validate(_FULL_LLM_PAYLOAD)
    assert meta.title == _FULL_LLM_PAYLOAD["title"]
    assert meta.authors == _FULL_LLM_PAYLOAD["authors"]
    assert meta.year == 2024
    assert meta.doi == "10.1093/bioinformatics/btae364"
    assert meta.journal == "Bioinformatics"


def test_schema_minimal_required_only() -> None:
    """Only title + authors are required; others default to None."""
    meta = LLMCandidateMeta.model_validate({
        "title": "Some paper",
        "authors": ["Doe, J."],
    })
    assert meta.year is None
    assert meta.doi is None
    assert meta.journal is None
    assert meta.arxiv_id is None
    assert meta.abstract is None


def test_schema_missing_title_rejected() -> None:
    with pytest.raises(Exception):
        LLMCandidateMeta.model_validate({"authors": ["Doe, J."]})


def test_schema_missing_authors_rejected() -> None:
    with pytest.raises(Exception):
        LLMCandidateMeta.model_validate({"title": "x"})


def test_schema_empty_authors_rejected() -> None:
    """First-author family drives id derivation -> at least one required."""
    with pytest.raises(Exception):
        LLMCandidateMeta.model_validate({"title": "x", "authors": []})


def test_schema_empty_title_rejected() -> None:
    with pytest.raises(Exception):
        LLMCandidateMeta.model_validate({"title": "", "authors": ["Doe, J."]})


def test_schema_unknown_key_rejected() -> None:
    """`extra='forbid'` so the agent learns the contract via failures."""
    with pytest.raises(Exception):
        LLMCandidateMeta.model_validate({
            "title": "x",
            "authors": ["Doe, J."],
            "topics": ["GNN"],  # not in schema yet
        })


def test_schema_arxiv_id_alias() -> None:
    """JSON key is hyphenated; Python attribute uses underscore."""
    meta = LLMCandidateMeta.model_validate({
        "title": "x",
        "authors": ["Doe, J."],
        "arxiv-id": "2401.12345",
    })
    assert meta.arxiv_id == "2401.12345"


def test_schema_bad_year_type_rejected() -> None:
    with pytest.raises(Exception):
        LLMCandidateMeta.model_validate({
            "title": "x",
            "authors": ["Doe, J."],
            "year": "two thousand",
        })


def test_schema_is_frozen() -> None:
    meta = LLMCandidateMeta.model_validate({"title": "x", "authors": ["Doe, J."]})
    with pytest.raises(Exception):
        meta.title = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# M12.0 — bib-oriented LLM fields
# ---------------------------------------------------------------------------


def test_schema_accepts_m12_bib_fields() -> None:
    """Pydantic accepts the 6 M12.0 fields with their hyphenated aliases."""
    meta = LLMCandidateMeta.model_validate({
        "title": "Paper",
        "authors": ["Doe, J."],
        "volume": "42",
        "issue": "3",
        "pages": "100-120",
        "publisher": "ACM",
        "venue-type": "proceedings-article",
        "booktitle": "ICML Proceedings",
    })
    assert meta.volume == "42"
    assert meta.issue == "3"
    assert meta.pages == "100-120"
    assert meta.publisher == "ACM"
    assert meta.venue_type == "proceedings-article"
    assert meta.booktitle == "ICML Proceedings"


def test_schema_m12_fields_optional_default_none() -> None:
    """Old payloads (without the 6 new fields) still parse — backwards compat."""
    meta = LLMCandidateMeta.model_validate({
        "title": "Paper",
        "authors": ["Doe, J."],
    })
    assert meta.volume is None
    assert meta.issue is None
    assert meta.pages is None
    assert meta.publisher is None
    assert meta.venue_type is None
    assert meta.booktitle is None


def test_parse_llm_json_full_m12_fields_normalize_to_empty_string(
    tmp_path: Path,
) -> None:
    """parse_llm_json returns "" (not None) so the dict matches parse_crossref."""
    p = _write_json(tmp_path / "meta.json", {
        "title": "Paper",
        "authors": ["Doe, J."],
        "volume": "42",
        "venue-type": "journal-article",
    })
    parsed = parse_llm_json(p)
    assert parsed["volume"] == "42"
    assert parsed["venue-type"] == "journal-article"
    # Absent ones come back as "" (matching parse_crossref shape).
    assert parsed["issue"] == ""
    assert parsed["pages"] == ""
    assert parsed["publisher"] == ""
    assert parsed["booktitle"] == ""


def test_parse_llm_json_minimal_payload_has_empty_m12_fields(
    tmp_path: Path,
) -> None:
    """A minimal payload still produces all 6 M12.0 keys (as empty strings)."""
    p = _write_json(tmp_path / "meta.json", {
        "title": "Minimal",
        "authors": ["Doe, J."],
    })
    parsed = parse_llm_json(p)
    for key in ("volume", "issue", "pages", "publisher", "venue-type", "booktitle"):
        assert parsed[key] == "", f"{key!r} should default to empty string"


# ---------------------------------------------------------------------------
# parse_llm_json file I/O
# ---------------------------------------------------------------------------


def test_parse_llm_json_full(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "meta.json", _FULL_LLM_PAYLOAD)
    parsed = parse_llm_json(p)
    # Shape matches parse_crossref output (used by downstream add.py).
    assert parsed["title"] == _FULL_LLM_PAYLOAD["title"]
    assert parsed["authors"] == _FULL_LLM_PAYLOAD["authors"]
    assert parsed["year"] == 2024
    assert parsed["doi"] == "10.1093/bioinformatics/btae364"
    assert parsed["journal"] == "Bioinformatics"


def test_parse_llm_json_minimal(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "meta.json", {
        "title": "Minimal",
        "authors": ["Doe, J."],
    })
    parsed = parse_llm_json(p)
    assert parsed["year"] is None
    assert parsed["doi"] == ""  # normalized empty string
    assert parsed["journal"] == ""


def test_parse_llm_json_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ImporterError, match="No LLM metadata JSON"):
        parse_llm_json(tmp_path / "does_not_exist.json")


def test_parse_llm_json_malformed(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ImporterError, match="Failed to parse"):
        parse_llm_json(p)


def test_parse_llm_json_non_object_root(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(ImporterError, match="JSON object"):
        parse_llm_json(p)


def test_parse_llm_json_validation_error_message(tmp_path: Path) -> None:
    p = _write_json(tmp_path / "missing.json", {"authors": ["x"]})
    with pytest.raises(ImporterError, match="title"):
        parse_llm_json(p)


def test_parse_llm_json_doi_stripped(tmp_path: Path) -> None:
    """Whitespace in DOI is stripped — the agent may have added padding."""
    p = _write_json(tmp_path / "meta.json", {
        "title": "x",
        "authors": ["Doe, J."],
        "doi": "  10.1/test  ",
    })
    parsed = parse_llm_json(p)
    assert parsed["doi"] == "10.1/test"


# ---------------------------------------------------------------------------
# CLI: lit add --from-llm-json
# ---------------------------------------------------------------------------


def test_cli_add_from_llm_json_creates_paper(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    payload_path = _write_json(tmp_path / "meta.json", _FULL_LLM_PAYLOAD)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(payload_path),
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    paper_id = "2024_Chen_De-novo-macrocyclic"
    paper_dir = vault / "papers" / paper_id
    assert paper_dir.is_dir()
    assert (paper_dir / "paper.pdf").is_file()
    assert (paper_dir / "metadata.yaml").is_file()

    meta = _yaml.load((paper_dir / "metadata.yaml").read_text())
    assert meta["title"] == _FULL_LLM_PAYLOAD["title"]
    assert meta["authors"] == _FULL_LLM_PAYLOAD["authors"]
    assert meta["doi"] == _FULL_LLM_PAYLOAD["doi"]
    assert meta["journal"] == "Bioinformatics"


def test_cli_add_from_llm_json_no_doi_skips_dedup(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    """LLM may have failed to find a DOI -> precheck is skipped, not error."""
    payload_path = _write_json(tmp_path / "meta.json", {
        "title": "Preprint without DOI",
        "authors": ["Doe, Jane"],
        "year": 2023,
    })
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(payload_path),
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    # Some paper folder was created — the exact id depends on the keyword
    # heuristic but it must exist under papers/.
    papers = list((vault / "papers").iterdir())
    assert len(papers) == 1


def test_cli_add_from_llm_json_doi_dedup_still_applies(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    """If the LLM JSON carries a DOI already in the vault, refuse."""
    payload_path = _write_json(tmp_path / "meta.json", _FULL_LLM_PAYLOAD)
    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(payload_path),
            "--library", str(vault),
        ],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(payload_path),
            "--library", str(vault),
        ],
    )
    assert second.exit_code != 0
    assert second.exception is not None


def test_cli_add_mutually_exclusive_flags(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    payload_path = _write_json(tmp_path / "meta.json", _FULL_LLM_PAYLOAD)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--doi", "10.1/x",
            "--from-llm-json", str(payload_path),
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, AddError)
    assert "mutually exclusive" in str(result.exception)


def test_cli_add_neither_source_provided(
    vault: Path, fake_pdf: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, AddError)
    assert "mutually exclusive" in str(result.exception)


def test_cli_add_from_llm_json_id_override(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    payload_path = _write_json(tmp_path / "meta.json", {
        "title": "x",
        "authors": ["Doe, J."],
        "year": 2024,
    })
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(payload_path),
            "--id", "2024_Custom_Override",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_Custom_Override").is_dir()


def test_cli_add_from_llm_json_missing_year_id_error(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    """No year + no --id override -> IDError with source-aware message."""
    payload_path = _write_json(tmp_path / "meta.json", {
        "title": "Undated paper",
        "authors": ["Doe, J."],
    })
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(payload_path),
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    msg = str(result.exception)
    assert "LLM JSON" in msg
    assert "pass --id" in msg


def test_cli_add_from_llm_json_id_collision_autosuffix(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    """Same year + author + title-keyword -> _b suffix under --auto-suffix."""
    payload_path = _write_json(tmp_path / "meta.json", {
        "title": "Twice-imported paper",
        "authors": ["Doe, Jane"],
        "year": 2024,
        "doi": None,  # no DOI -> dedup skipped, lets us hit id collision
    })
    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(payload_path),
            "--library", str(vault),
        ],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(payload_path),
            "--auto-suffix",
            "--library", str(vault),
        ],
    )
    assert second.exit_code == 0, second.output

    papers = sorted(p.name for p in (vault / "papers").iterdir())
    assert len(papers) == 2
    assert any(name.endswith("_b") for name in papers)


def test_cli_add_from_llm_json_path_must_exist(
    vault: Path, fake_pdf: Path, tmp_path: Path
) -> None:
    """Click validates the path exists before our function runs."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "add", str(fake_pdf),
            "--from-llm-json", str(tmp_path / "missing.json"),
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output.lower() or \
           "no such file" in result.output.lower()
