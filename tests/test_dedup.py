"""Tests for ``litman.core.dedup`` (M2.9 duplicate detection helpers)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from litman.core.dedup import (
    auto_suffix_id,
    find_paper_by_doi,
    normalize_doi,
    suggest_alternative_ids,
)
from litman.core.library import create_vault
from litman.exceptions import AddError

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _write_paper(
    vault: Path,
    paper_id: str,
    doi: str = "",
    title: str = "Test paper",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Materialize a minimal paper folder with a metadata.yaml."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": title,
        "doi": doi,
        "year": 2024,
        "created-at": "2026-04-28T15:00:00+02:00",
        "updated-at": "2026-04-28T15:00:00+02:00",
        **(extra or {}),
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)
    return paper_dir


# ---------------------------------------------------------------------------
# normalize_doi
# ---------------------------------------------------------------------------


def test_normalize_doi_lowercases_and_strips() -> None:
    assert normalize_doi("  10.1093/BIOINFORMATICS/btae364  ") == (
        "10.1093/bioinformatics/btae364"
    )


def test_normalize_doi_empty_string_stays_empty() -> None:
    assert normalize_doi("") == ""
    assert normalize_doi("   ") == ""


# ---------------------------------------------------------------------------
# find_paper_by_doi
# ---------------------------------------------------------------------------


def test_find_paper_by_doi_empty_query_returns_none(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    assert find_paper_by_doi(vault, "") is None


def test_find_paper_by_doi_no_papers_dir_returns_none(tmp_path: Path) -> None:
    # Bare directory, no vault init.
    assert find_paper_by_doi(tmp_path, "10.1/x") is None


def test_find_paper_by_doi_hit(vault: Path) -> None:
    _write_paper(vault, "2024_Chen_HELM", doi="10.1093/bioinformatics/btae364")
    hit = find_paper_by_doi(vault, "10.1093/bioinformatics/btae364")
    assert hit is not None
    pid, meta = hit
    assert pid == "2024_Chen_HELM"
    assert meta["doi"] == "10.1093/bioinformatics/btae364"


def test_find_paper_by_doi_case_insensitive(vault: Path) -> None:
    _write_paper(vault, "2024_Chen_HELM", doi="10.1093/Bioinformatics/btae364")
    hit = find_paper_by_doi(vault, "10.1093/BIOINFORMATICS/BTAE364")
    assert hit is not None
    assert hit[0] == "2024_Chen_HELM"


def test_find_paper_by_doi_no_match_returns_none(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    assert find_paper_by_doi(vault, "10.1/different") is None


def test_find_paper_by_doi_skips_paper_without_metadata(vault: Path) -> None:
    # Paper folder exists, but no metadata.yaml. Should be silently skipped.
    (vault / "papers" / "2024_Orphan").mkdir()
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    hit = find_paper_by_doi(vault, "10.1/x")
    assert hit is not None
    assert hit[0] == "2024_Smith_X"


def test_find_paper_by_doi_skips_corrupt_metadata(vault: Path) -> None:
    # Paper folder with garbage metadata.yaml.
    bad_dir = vault / "papers" / "2024_Corrupt"
    bad_dir.mkdir()
    (bad_dir / "metadata.yaml").write_text("not: : valid: yaml: [", encoding="utf-8")
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    hit = find_paper_by_doi(vault, "10.1/x")
    assert hit is not None
    assert hit[0] == "2024_Smith_X"


def test_find_paper_by_doi_ignores_papers_with_empty_doi(vault: Path) -> None:
    _write_paper(vault, "2024_NoDoi", doi="")
    assert find_paper_by_doi(vault, "10.1/x") is None


def test_find_paper_by_doi_ignores_non_directory_entries(vault: Path) -> None:
    # A stray file under papers/ should be ignored.
    (vault / "papers" / "stray.txt").write_text("not a paper", encoding="utf-8")
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    hit = find_paper_by_doi(vault, "10.1/x")
    assert hit is not None
    assert hit[0] == "2024_Smith_X"


# ---------------------------------------------------------------------------
# suggest_alternative_ids
# ---------------------------------------------------------------------------


def test_suggest_alternatives_returns_offset_candidates(vault: Path) -> None:
    title = "Cell-free biosynthesis of antimicrobial peptides at scale"
    alts = suggest_alternative_ids(
        vault,
        primary_id="2024_Smith_Cell-free-biosynthesis-antimicrobial",
        year=2024,
        family="Smith",
        title=title,
        n=3,
    )
    assert len(alts) == 3
    assert all(a.startswith("2024_Smith_") for a in alts)
    # Different alternatives, none equal primary.
    assert len(set(alts)) == 3
    assert "2024_Smith_Cell-free-biosynthesis-antimicrobial" not in alts


def test_suggest_alternatives_skips_existing_disk_collisions(vault: Path) -> None:
    title = "Cell-free biosynthesis of antimicrobial peptides at scale"
    # Pre-create a folder that matches the first alternative offset.
    _write_paper(vault, "2024_Smith_Biosynthesis-antimicrobial-peptides", doi="")

    alts = suggest_alternative_ids(
        vault,
        primary_id="2024_Smith_Cell-free-biosynthesis-antimicrobial",
        year=2024,
        family="Smith",
        title=title,
        n=3,
    )
    assert "2024_Smith_Biosynthesis-antimicrobial-peptides" not in alts


def test_suggest_alternatives_empty_for_short_title(vault: Path) -> None:
    alts = suggest_alternative_ids(
        vault,
        primary_id="2024_Smith_BERT",
        year=2024,
        family="Smith",
        title="BERT",
        n=3,
    )
    assert alts == []


def test_suggest_alternatives_n_zero(vault: Path) -> None:
    alts = suggest_alternative_ids(
        vault,
        primary_id="2024_Smith_Foo",
        year=2024,
        family="Smith",
        title="Foo bar baz qux",
        n=0,
    )
    assert alts == []


# ---------------------------------------------------------------------------
# auto_suffix_id
# ---------------------------------------------------------------------------


def test_auto_suffix_returns_base_when_free(vault: Path) -> None:
    assert auto_suffix_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo"


def test_auto_suffix_returns_b_when_base_taken(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_Foo")
    assert auto_suffix_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo_b"


def test_auto_suffix_returns_c_when_b_also_taken(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_Foo")
    _write_paper(vault, "2024_Smith_Foo_b")
    assert auto_suffix_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo_c"


def test_auto_suffix_skips_taken_slots_in_order(vault: Path) -> None:
    # Hole at _c: base, _b, _d taken → expect _c (smallest free).
    _write_paper(vault, "2024_Smith_Foo")
    _write_paper(vault, "2024_Smith_Foo_b")
    _write_paper(vault, "2024_Smith_Foo_d")
    assert auto_suffix_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo_c"


def test_auto_suffix_exhausted_raises(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_Foo")
    for c in "bcdefghijklmnopqrstuvwxyz":
        _write_paper(vault, f"2024_Smith_Foo_{c}")
    with pytest.raises(AddError, match="exhausted"):
        auto_suffix_id(vault, "2024_Smith_Foo")
