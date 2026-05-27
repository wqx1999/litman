"""Tests for ``lit refresh-views`` and the underlying view-builder helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
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


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the vault registry. ``lit init`` now registers vaults, so the
    init-based tests below would otherwise write to / collide on the real
    ``~/.config/litman/vaults.yaml``. Mirrors the fixture in test_init.py.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


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


def test_render_index_empty_papers_emits_empty_array() -> None:
    text = render_index([], "2026-04-27T16:00:00+02:00")
    payload = json.loads(text)
    assert "AUTO-GENERATED" in payload["_comment"]
    assert payload["generated_at"] == "2026-04-27T16:00:00+02:00"
    assert payload["n_papers"] == 0
    assert payload["papers"] == []


def test_render_index_single_paper_projection() -> None:
    paper = {
        "id": "2024_Smith_Test",
        "title": "A test",
        "year": 2024,
        "type": "research",
        "priority": "A",
        "status": "deep-read",
        "topics": ["alpha", "beta"],
        "projects": ["proj1"],
        "methods": ["transformer"],
        "data": ["GDP-2"],
        "doi": "10.1/x",
    }
    text = render_index([paper], "2026-04-27T16:00:00+02:00")
    payload = json.loads(text)
    assert payload["n_papers"] == 1
    p = payload["papers"][0]
    assert p["id"] == "2024_Smith_Test"
    assert p["title"] == "A test"
    assert p["topics"] == ["alpha", "beta"]
    assert p["projects"] == ["proj1"]
    assert p["methods"] == ["transformer"]
    assert p["data"] == ["GDP-2"]
    assert p["doi"] == "10.1/x"


def test_render_index_handles_missing_optional_fields() -> None:
    paper = {"id": "x", "year": 2024, "status": "inbox"}
    text = render_index([paper], "2026-04-27T16:00:00+02:00")
    payload = json.loads(text)
    p = payload["papers"][0]
    # List fields default to [] so AI consumers don't have to special-case None.
    assert p["topics"] == []
    assert p["projects"] == []
    assert p["methods"] == []
    assert p["data"] == []
    # Scalar absences stay as None / null.
    assert p["doi"] is None
    assert p["title"] is None


def test_render_index_sorts_by_id() -> None:
    papers = [
        {"id": "2024_Z_x", "year": 2024},
        {"id": "2023_A_x", "year": 2023},
    ]
    text = render_index(papers, "t")
    payload = json.loads(text)
    ids = [p["id"] for p in payload["papers"]]
    assert ids == ["2023_A_x", "2024_Z_x"]


def test_render_index_emits_by_doi_map() -> None:
    """M2.9: INDEX.json carries a ``by_doi`` reverse map for fast lookup."""
    papers = [
        {"id": "2024_Smith_X", "doi": "10.1/x"},
        {"id": "2024_Jones_Y", "doi": "10.1/y"},
        {"id": "2024_NoDoi_Z", "doi": ""},  # empty doi excluded
    ]
    text = render_index(papers, "t")
    payload = json.loads(text)
    assert payload["by_doi"] == {
        "10.1/x": "2024_Smith_X",
        "10.1/y": "2024_Jones_Y",
    }


def test_render_index_by_doi_normalizes_case() -> None:
    """by_doi keys are lowercase + stripped so callers don't normalize again."""
    papers = [{"id": "p1", "doi": "  10.1/Mixed-CASE  "}]
    text = render_index(papers, "t")
    payload = json.loads(text)
    assert payload["by_doi"] == {"10.1/mixed-case": "p1"}


def test_render_index_empty_papers_empty_by_doi() -> None:
    text = render_index([], "t")
    payload = json.loads(text)
    assert payload["by_doi"] == {}


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
    seed_payload = json.loads((vault / "INDEX.json").read_text())
    assert seed_payload["n_papers"] == 0
    assert seed_payload["papers"] == []

    papers = [{"id": "2024_X_y", "year": 2024, "status": "inbox"}]
    write_index(vault, papers)
    new_payload = json.loads((vault / "INDEX.json").read_text())
    assert new_payload["n_papers"] == 1
    assert new_payload["papers"][0]["id"] == "2024_X_y"


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
    payload = json.loads((vault / "INDEX.json").read_text())
    assert payload["n_papers"] == 0
    assert payload["papers"] == []


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

    payload = json.loads((vault / "INDEX.json").read_text())
    assert payload["n_papers"] == 1
    p = payload["papers"][0]
    assert p["id"] == "2024_Test_Paper"
    assert "alpha" in p["topics"]
    assert "pepforge" in p["projects"]
    assert "transformer" in p["methods"]

    # Symlinks
    assert (vault / "views" / "by-topic" / "alpha" / "2024_Test_Paper").is_symlink()
    assert (vault / "views" / "by-project" / "pepforge" / "2024_Test_Paper").is_symlink()
    assert (vault / "views" / "by-status" / "inbox" / "2024_Test_Paper").is_symlink()


def test_lit_refresh_views_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["refresh-views", "--help"])
    assert result.exit_code == 0
    assert "Rebuild" in result.output or "rebuild" in result.output
