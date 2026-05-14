"""Tests for `litman.core.document` — pure metadata-loading helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from litman.core.document import find_paper, list_papers, read_metadata
from litman.core.library import create_vault
from litman.exceptions import PaperNotFoundError


def _write_paper(vault: Path, paper_id: str, **fields: object) -> Path:
    """Create a minimal paper folder with the given metadata fields."""
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
    return paper_dir


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ---------------------------------------------------------------------------
# read_metadata
# ---------------------------------------------------------------------------


def test_read_metadata_simple(vault: Path) -> None:
    paper_dir = _write_paper(vault, "2024_X_Foo", year=2024, title="Foo")
    metadata = read_metadata(paper_dir / "metadata.yaml")
    assert metadata == {"id": "2024_X_Foo", "year": 2024, "title": "Foo"}


def test_read_metadata_empty_file_returns_empty_dict(tmp_path: Path) -> None:
    f = tmp_path / "empty.yaml"
    f.write_text("", encoding="utf-8")
    assert read_metadata(f) == {}


def test_read_metadata_only_comments_returns_empty_dict(tmp_path: Path) -> None:
    f = tmp_path / "comment.yaml"
    f.write_text("# just a comment\n", encoding="utf-8")
    assert read_metadata(f) == {}


# ---------------------------------------------------------------------------
# list_papers
# ---------------------------------------------------------------------------


def test_list_papers_empty_vault(vault: Path) -> None:
    assert list_papers(vault) == []


def test_list_papers_returns_sorted_by_id(vault: Path) -> None:
    _write_paper(vault, "2025_C_Baz", year=2025)
    _write_paper(vault, "2023_B_Foo", year=2023)
    _write_paper(vault, "2024_A_Bar", year=2024)

    papers = list_papers(vault)
    assert [p["id"] for p in papers] == [
        "2023_B_Foo",
        "2024_A_Bar",
        "2025_C_Baz",
    ]


def test_list_papers_skips_subdir_without_metadata(vault: Path) -> None:
    (vault / "papers" / "stray-dir").mkdir()
    _write_paper(vault, "2024_X_Foo", year=2024)
    papers = list_papers(vault)
    assert [p["id"] for p in papers] == ["2024_X_Foo"]


def test_list_papers_skips_corrupted_yaml(vault: Path) -> None:
    _write_paper(vault, "2024_X_Good", year=2024)
    bad_dir = vault / "papers" / "2024_Y_Bad"
    bad_dir.mkdir()
    (bad_dir / "metadata.yaml").write_text(
        "{not: valid: yaml:", encoding="utf-8"
    )
    papers = list_papers(vault)
    assert [p["id"] for p in papers] == ["2024_X_Good"]


def test_list_papers_no_papers_dir(tmp_path: Path) -> None:
    # tmp_path has no `papers/` subdirectory.
    assert list_papers(tmp_path) == []


def test_list_papers_skips_files_in_papers_dir(vault: Path) -> None:
    # A stray file directly under papers/ shouldn't crash anything.
    (vault / "papers" / "stray.txt").write_text("nope")
    _write_paper(vault, "2024_X_Foo", year=2024)
    papers = list_papers(vault)
    assert [p["id"] for p in papers] == ["2024_X_Foo"]


# ---------------------------------------------------------------------------
# find_paper
# ---------------------------------------------------------------------------


def test_find_paper_success(vault: Path) -> None:
    _write_paper(vault, "2024_X_Foo", year=2024, title="Foo")
    metadata = find_paper(vault, "2024_X_Foo")
    assert metadata["id"] == "2024_X_Foo"
    assert metadata["title"] == "Foo"


def test_find_paper_missing_raises(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError, match="No paper with id"):
        find_paper(vault, "2024_X_Missing")


def test_find_paper_rejects_path_traversal(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError, match="Invalid paper id"):
        find_paper(vault, "../etc/passwd")


def test_find_paper_rejects_slash(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError, match="Invalid paper id"):
        find_paper(vault, "foo/bar")


def test_find_paper_rejects_leading_dot(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError, match="Invalid paper id"):
        find_paper(vault, ".hidden")


def test_find_paper_rejects_empty(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError, match="Invalid paper id"):
        find_paper(vault, "")
