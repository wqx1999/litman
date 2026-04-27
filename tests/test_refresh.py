"""Tests for ``lit refresh-views`` and the underlying view-builder helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.views import (
    LIST_VIEW_FIELDS,
    SCALAR_VIEW_FIELDS,
    rebuild_views,
    render_index,
    write_index,
)


_yaml = YAML(typ="safe")


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    """Create a minimal paper dir under vault/papers/<id> with the given metadata."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    metadata = {"id": paper_id, **fields}
    (paper_dir / "metadata.yaml").write_text(
        "\n".join(f"{k}: {_yaml_dump_inline(v)}" for k, v in metadata.items()),
        encoding="utf-8",
    )


def _yaml_dump_inline(value: Any) -> str:
    """Tiny inline YAML emitter sufficient for test fixtures."""
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


# ---------------------------------------------------------------------------
# render_index
# ---------------------------------------------------------------------------


def test_render_index_empty_papers_shows_fallback() -> None:
    text = render_index([], "2026-04-27 16:00")
    assert "AUTO-GENERATED" in text
    assert "Literature Index" in text
    assert "| id |" in text
    assert "(No papers yet" in text


def test_render_index_single_paper_row() -> None:
    paper = {
        "id": "2024_Smith_Test",
        "year": 2024,
        "type": "research",
        "priority": "A",
        "status": "deep-read",
        "topics": ["alpha", "beta"],
        "projects": ["proj1"],
        "doi": "10.1/x",
    }
    text = render_index([paper], "2026-04-27 16:00")
    assert "2024_Smith_Test" in text
    assert "alpha, beta" in text
    assert "proj1" in text
    assert "10.1/x" in text


def test_render_index_handles_missing_optional_fields() -> None:
    paper = {"id": "x", "year": 2024, "status": "inbox"}
    text = render_index([paper], "2026-04-27 16:00")
    # Missing topics/projects/doi rendered as em dash.
    assert "—" in text


def test_render_index_sorts_by_id() -> None:
    papers = [
        {"id": "2024_Z_x", "year": 2024},
        {"id": "2023_A_x", "year": 2023},
    ]
    text = render_index(papers, "t")
    a_pos = text.index("2023_A_x")
    z_pos = text.index("2024_Z_x")
    assert a_pos < z_pos


# ---------------------------------------------------------------------------
# rebuild_views
# ---------------------------------------------------------------------------


def test_rebuild_views_creates_relative_symlinks(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    _write_paper(
        vault,
        "2024_Foo_Bar",
        topics=["alpha", "beta"],
        projects=["pepforge"],
        methods=["transformer"],
        status="deep-read",
    )
    papers = [
        {
            "id": "2024_Foo_Bar",
            "topics": ["alpha", "beta"],
            "projects": ["pepforge"],
            "methods": ["transformer"],
            "status": "deep-read",
        }
    ]
    counts = rebuild_views(vault, papers)

    # by-topic: 2 entries (one per tag)
    assert counts["by-topic"] == 2
    assert counts["by-project"] == 1
    assert counts["by-method"] == 1
    assert counts["by-status"] == 1

    # Symlinks resolve to the actual paper directory.
    link = vault / "views" / "by-topic" / "alpha" / "2024_Foo_Bar"
    assert link.is_symlink()
    assert link.resolve() == (vault / "papers" / "2024_Foo_Bar").resolve()

    # Symlink target is RELATIVE, not absolute (cross-machine portability).
    raw = os.readlink(link)
    assert not os.path.isabs(raw)
    assert raw == "../../../papers/2024_Foo_Bar"


def test_rebuild_views_empty_lists_create_no_symlinks(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    _write_paper(vault, "2024_Foo_Bar", topics=[], projects=[], methods=[])
    papers = [{"id": "2024_Foo_Bar", "topics": [], "projects": [], "methods": [], "status": "inbox"}]
    counts = rebuild_views(vault, papers)
    assert counts["by-topic"] == 0
    assert counts["by-project"] == 0
    assert counts["by-method"] == 0
    # status is scalar so still 1
    assert counts["by-status"] == 1


def test_rebuild_views_clears_stale_entries(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    # First: paper with topic alpha
    papers_v1 = [{"id": "p1", "topics": ["alpha"], "status": "inbox"}]
    rebuild_views(vault, papers_v1)
    assert (vault / "views" / "by-topic" / "alpha" / "p1").is_symlink()

    # Second: same paper, topic changed to beta
    papers_v2 = [{"id": "p1", "topics": ["beta"], "status": "inbox"}]
    rebuild_views(vault, papers_v2)
    assert (vault / "views" / "by-topic" / "beta" / "p1").is_symlink()
    # alpha bucket should be gone
    assert not (vault / "views" / "by-topic" / "alpha").exists()


def test_rebuild_views_skips_papers_missing_id(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    papers = [{"topics": ["alpha"], "status": "inbox"}]
    counts = rebuild_views(vault, papers)
    assert counts["by-topic"] == 0
    assert counts["by-status"] == 0


def test_view_field_constants_cover_design_spec() -> None:
    """Sanity: the four design-spec views are all wired up somewhere."""
    all_views = set(LIST_VIEW_FIELDS) | set(SCALAR_VIEW_FIELDS)
    assert all_views == {"by-project", "by-topic", "by-method", "by-status"}


# ---------------------------------------------------------------------------
# write_index
# ---------------------------------------------------------------------------


def test_write_index_overwrites_seed_file(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    seed = (vault / "INDEX.md").read_text()
    assert "(No papers yet" in seed

    papers = [{"id": "2024_X_y", "year": 2024, "status": "inbox"}]
    write_index(vault, papers)
    new = (vault / "INDEX.md").read_text()
    assert "2024_X_y" in new
    assert "(No papers yet" not in new


# ---------------------------------------------------------------------------
# CLI: lit refresh-views
# ---------------------------------------------------------------------------


def test_lit_refresh_views_empty_vault(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["init", str(tmp_path)])
    vault = tmp_path / "literature_vault"

    result = runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "0 papers" in result.output
    assert "(No papers yet" in (vault / "INDEX.md").read_text()


def test_lit_refresh_views_with_paper(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["init", str(tmp_path)])
    vault = tmp_path / "literature_vault"

    _write_paper(
        vault,
        "2024_Test_Paper",
        title="A test",
        year=2024,
        type="research",
        status="inbox",
        priority="B",
        topics=["alpha"],
        projects=["pepforge"],
        methods=["transformer"],
    )

    result = runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "1 paper" in result.output

    index_text = (vault / "INDEX.md").read_text()
    assert "2024_Test_Paper" in index_text
    assert "alpha" in index_text
    assert "pepforge" in index_text

    # Symlinks
    assert (vault / "views" / "by-topic" / "alpha" / "2024_Test_Paper").is_symlink()
    assert (vault / "views" / "by-project" / "pepforge" / "2024_Test_Paper").is_symlink()
    assert (vault / "views" / "by-status" / "inbox" / "2024_Test_Paper").is_symlink()


def test_lit_refresh_views_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["refresh-views", "--help"])
    assert result.exit_code == 0
    assert "Rebuild" in result.output or "rebuild" in result.output
