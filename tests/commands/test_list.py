"""Tests for `lit list`."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault


def _seed_paper(vault: Path, paper_id: str, **fields: object) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines: list[str] = [f"id: {paper_id}"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    (paper_dir / "metadata.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault with three papers spanning diverse field values."""
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2023_Pandi_Cellfree",
        year=2023, type="research", status="inbox", priority="B",
        topics=["AMP-prediction", "deep-learning"],
        methods=["transformer"],
        projects=["PepForge"],
        authors=["Pandi, Amir", "Smith, John"],
        title="Cell-free biosynthesis paper",
    )
    _seed_paper(
        v, "2024_Smith_BERT",
        year=2024, type="review", status="deep-read", priority="A",
        topics=["NLP", "transformer"],
        methods=["BERT-style"],
        projects=["PepCodec"],
        authors=["Smith, John", "Doe, Jane"],
        title="A review of BERT-based methods",
    )
    _seed_paper(
        v, "2024_Doe_GNN",
        year=2024, type="research", status="skim", priority="C",
        topics=["GNN"],
        methods=["GNN"],
        projects=["PepForge", "PepCodec"],
        authors=["Doe, Jane"],
        title="GNN survey",
    )
    return v


def _invoke(vault: Path, *args: str):
    runner = CliRunner()
    return runner.invoke(cli, ["list", "--library", str(vault), *args])


# ---------------------------------------------------------------------------
# Basic listing
# ---------------------------------------------------------------------------


def test_list_no_filter_shows_all(vault: Path) -> None:
    result = _invoke(vault)
    assert result.exit_code == 0
    assert "2023_Pandi_Cellfree" in result.output
    assert "2024_Smith_BERT" in result.output
    assert "2024_Doe_GNN" in result.output
    assert "3 of 3" in result.output


def test_list_empty_vault(tmp_path: Path) -> None:
    empty_vault = create_vault(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--library", str(empty_vault)])
    assert result.exit_code == 0
    assert "No papers in vault yet" in result.output


def test_list_no_match_with_papers_present(vault: Path) -> None:
    result = _invoke(vault, "--year", "1900")
    assert result.exit_code == 0
    assert "No papers match" in result.output
    assert "3 total" in result.output


# ---------------------------------------------------------------------------
# Equality filters
# ---------------------------------------------------------------------------


def test_list_year_filter(vault: Path) -> None:
    result = _invoke(vault, "--year", "2024")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "2024_Doe_GNN" in result.output
    assert "2023_Pandi_Cellfree" not in result.output
    assert "2 of 3" in result.output


def test_list_type_filter(vault: Path) -> None:
    result = _invoke(vault, "--type", "review")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "2024_Doe_GNN" not in result.output
    assert "1 of 3" in result.output


def test_list_status_filter(vault: Path) -> None:
    result = _invoke(vault, "--status", "deep-read")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "1 of 3" in result.output


def test_list_priority_filter(vault: Path) -> None:
    result = _invoke(vault, "--priority", "C")
    assert result.exit_code == 0
    assert "2024_Doe_GNN" in result.output
    assert "1 of 3" in result.output


# ---------------------------------------------------------------------------
# Multi-valued list-membership filters
# ---------------------------------------------------------------------------


def test_list_topic_filter(vault: Path) -> None:
    result = _invoke(vault, "--topic", "transformer")
    assert result.exit_code == 0
    # 2024_Smith_BERT has 'transformer' in topics
    assert "2024_Smith_BERT" in result.output
    # 2023_Pandi has 'deep-learning', not 'transformer'
    assert "2023_Pandi_Cellfree" not in result.output


def test_list_method_filter(vault: Path) -> None:
    result = _invoke(vault, "--method", "GNN")
    assert result.exit_code == 0
    assert "2024_Doe_GNN" in result.output
    assert "1 of 3" in result.output


def test_list_project_filter_returns_multiple(vault: Path) -> None:
    result = _invoke(vault, "--project", "PepForge")
    assert result.exit_code == 0
    assert "2023_Pandi_Cellfree" in result.output
    assert "2024_Doe_GNN" in result.output
    assert "2024_Smith_BERT" not in result.output
    assert "2 of 3" in result.output


# ---------------------------------------------------------------------------
# Author substring (case-insensitive)
# ---------------------------------------------------------------------------


def test_list_author_substring_case_insensitive(vault: Path) -> None:
    result = _invoke(vault, "--author", "doe")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output  # Doe is 2nd author
    assert "2024_Doe_GNN" in result.output     # Doe is sole
    assert "2023_Pandi_Cellfree" not in result.output


def test_list_author_substring_partial(vault: Path) -> None:
    result = _invoke(vault, "--author", "andi")  # part of "Pandi"
    assert result.exit_code == 0
    assert "2023_Pandi_Cellfree" in result.output
    assert "1 of 3" in result.output


# ---------------------------------------------------------------------------
# Combined AND filters
# ---------------------------------------------------------------------------


def test_list_combined_and_filter(vault: Path) -> None:
    result = _invoke(vault, "--year", "2024", "--type", "research")
    assert result.exit_code == 0
    # only 2024_Doe_GNN matches both
    assert "2024_Doe_GNN" in result.output
    assert "2024_Smith_BERT" not in result.output
    assert "1 of 3" in result.output


def test_list_combined_three_filters(vault: Path) -> None:
    result = _invoke(
        vault,
        "--project", "PepForge",
        "--year", "2024",
        "--method", "GNN",
    )
    assert result.exit_code == 0
    assert "2024_Doe_GNN" in result.output
    assert "1 of 3" in result.output
