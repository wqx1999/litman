"""Tests for `lit taxonomy` and the underlying TAXONOMY.md helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.taxonomy import (
    ALL_DICTS,
    FIXED_DICTS,
    USER_DICTS,
    parse_taxonomy,
    update_user_dict_section,
)
from litman.exceptions import TaxonomyError

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    """Materialise a paper with full M2.0-schema metadata (audit + code-clones).

    Lets taxonomy ripple operations re-roundtrip the file cleanly.
    """
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": fields.get("title", paper_id),
        "authors": fields.get("authors", ["Doe, Jane"]),
        "year": fields.get("year", 2024),
        "journal": fields.get("journal", "Test J."),
        "doi": fields.get("doi", f"10.0/{paper_id}"),
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": fields.get("projects", []),
        "topics": fields.get("topics", []),
        "methods": fields.get("methods", []),
        "data": fields.get("data", []),
        "type": fields.get("type", "research"),
        "status": fields.get("status", "inbox"),
        "priority": fields.get("priority", "B"),
        "read-date": None,
        "last-revisited": None,
        "related": [],
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _read_meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


def _read_taxonomy(vault: Path) -> dict[str, list[str]]:
    return parse_taxonomy((vault / "TAXONOMY.md").read_text())


# ===========================================================================
# core/taxonomy.py — pure helpers
# ===========================================================================


def test_parse_seed_yields_all_known_dicts(vault: Path) -> None:
    parsed = _read_taxonomy(vault)
    # All known dicts present as keys.
    assert set(parsed.keys()) == set(ALL_DICTS)
    # User dicts start empty.
    for d in USER_DICTS:
        assert parsed[d] == []
    # Fixed enums populated from the seed.
    assert "research" in parsed["type"]
    assert "deep-read" in parsed["status"]
    assert "A" in parsed["priority"]


def test_update_user_dict_replaces_only_target_section(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text()
    new_text = update_user_dict_section(text, "topics", ["alpha", "beta"])
    parsed = parse_taxonomy(new_text)
    assert parsed["topics"] == ["alpha", "beta"]
    # Other user dicts unchanged.
    assert parsed["projects"] == []
    assert parsed["methods"] == []
    assert parsed["data"] == []
    # Fixed enums preserved.
    assert "research" in parsed["type"]
    assert "deep-read" in parsed["status"]


def test_update_user_dict_emits_sorted_values(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text()
    new_text = update_user_dict_section(text, "topics", ["zeta", "alpha", "mu"])
    # Body must contain values in sorted order.
    body = new_text.split("## topics")[1].split("## methods")[0]
    a = body.index("- alpha")
    m = body.index("- mu")
    z = body.index("- zeta")
    assert a < m < z


def test_update_user_dict_empty_marker(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text()
    populated = update_user_dict_section(text, "topics", ["alpha"])
    cleared = update_user_dict_section(populated, "topics", [])
    parsed = parse_taxonomy(cleared)
    assert parsed["topics"] == []
    assert "(empty)" in cleared.split("## topics")[1].split("## methods")[0]


def test_update_user_dict_rejects_fixed_enum(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text()
    with pytest.raises(ValueError):
        update_user_dict_section(text, "type", ["evil"])


# ===========================================================================
# CLI: list
# ===========================================================================


def test_taxonomy_list_all(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["taxonomy", "list", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "projects" in result.output
    assert "topics" in result.output
    assert "research" in result.output  # fixed-enum value


def test_taxonomy_list_single_user_dict(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "list", "topics", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "user-extensible" in result.output
    assert "(empty)" in result.output


def test_taxonomy_list_fixed_enum(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "list", "status", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "fixed enum" in result.output
    assert "deep-read" in result.output


def test_taxonomy_list_unknown(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "list", "ghosts", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)


# ===========================================================================
# CLI: add
# ===========================================================================


def test_taxonomy_add_single(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "peptide", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _read_taxonomy(vault)["topics"] == ["peptide"]


def test_taxonomy_add_multiple(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "peptide", "AMP", "diffusion",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    # Sorted in TAXONOMY.md
    assert _read_taxonomy(vault)["topics"] == ["AMP", "diffusion", "peptide"]


def test_taxonomy_add_dedupes_with_existing(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide", "--library", str(vault)])
    # Re-add same value
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "peptide", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output
    assert _read_taxonomy(vault)["topics"] == ["peptide"]


def test_taxonomy_add_rejects_fixed_enum(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "status", "rejected", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "fixed-enum" in str(result.exception)


def test_taxonomy_add_rejects_unknown_dict(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "unknown", "x", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)


def test_taxonomy_add_does_not_touch_papers(vault: Path) -> None:
    """Registering a value doesn't ripple into existing metadata.yaml."""
    _write_paper(vault, "2024_Foo_X", topics=["existing"])
    before = _read_meta(vault, "2024_Foo_X")
    runner = CliRunner()
    runner.invoke(
        cli, ["taxonomy", "add", "topics", "newtopic", "--library", str(vault)]
    )
    after = _read_meta(vault, "2024_Foo_X")
    # topics list unchanged on the paper itself.
    assert after["topics"] == before["topics"]
    assert after["updated-at"] == before["updated-at"]


# ===========================================================================
# CLI: rename
# ===========================================================================


def test_taxonomy_rename_ripples_to_papers(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["peptide", "diffusion"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    _write_paper(vault, "2024_C", topics=["other"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide", "diffusion", "other",
                        "--library", str(vault)])

    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "peptide", "AMP-design",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated 2 paper" in result.output

    parsed = _read_taxonomy(vault)
    assert "peptide" not in parsed["topics"]
    assert "AMP-design" in parsed["topics"]

    # Affected papers updated.
    a = _read_meta(vault, "2024_A")
    b = _read_meta(vault, "2024_B")
    c = _read_meta(vault, "2024_C")
    assert "peptide" not in a["topics"]
    assert "AMP-design" in a["topics"]
    assert b["topics"] == ["AMP-design"]
    # Untouched paper unchanged.
    assert c["topics"] == ["other"]
    assert c["updated-at"] == "2026-04-28T10:00:00+02:00"


def test_taxonomy_rename_refreshes_index_and_views(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert (vault / "views/by-topic/peptide/2024_A").is_symlink()

    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "peptide", "AMP",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads((vault / "INDEX.json").read_text())
    p = payload["papers"][0]
    assert p["topics"] == ["AMP"]
    # Old view bucket gone, new one present.
    assert not (vault / "views/by-topic/peptide").exists()
    assert (vault / "views/by-topic/AMP/2024_A").is_symlink()


def test_taxonomy_rename_missing_old(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "ghost", "spirit",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "not registered" in str(result.exception)


def test_taxonomy_rename_collision_redirects_to_merge(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha", "beta",
                        "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "alpha", "beta",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert "merge" in str(result.exception)


def test_taxonomy_rename_identity_rejected(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha",
                        "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "alpha", "alpha",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)


# ===========================================================================
# CLI: merge
# ===========================================================================


def test_taxonomy_merge_into_new_value(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["AMP", "peptide"])
    _write_paper(vault, "2024_B", topics=["AMP"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "AMP", "peptide",
                        "--library", str(vault)])

    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "AMP", "peptide",
         "--into", "AMP-design", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated 2 paper" in result.output

    parsed = _read_taxonomy(vault)
    assert "AMP" not in parsed["topics"]
    assert "peptide" not in parsed["topics"]
    assert "AMP-design" in parsed["topics"]

    a = _read_meta(vault, "2024_A")
    # Both AMP and peptide collapsed into a single AMP-design.
    assert a["topics"] == ["AMP-design"]
    b = _read_meta(vault, "2024_B")
    assert b["topics"] == ["AMP-design"]


def test_taxonomy_merge_into_existing_source(vault: Path) -> None:
    """`merge a b --into a` keeps a, drops b."""
    _write_paper(vault, "2024_A", topics=["alpha", "beta"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha", "beta",
                        "--library", str(vault)])

    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "alpha", "beta",
         "--into", "alpha", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    parsed = _read_taxonomy(vault)
    assert parsed["topics"] == ["alpha"]
    a = _read_meta(vault, "2024_A")
    assert a["topics"] == ["alpha"]


def test_taxonomy_merge_missing_source(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha",
                        "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "alpha", "ghost",
         "--into", "merged", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "Sources not registered" in str(result.exception)


def test_taxonomy_merge_all_sources_equal_dest(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha",
                        "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "alpha",
         "--into", "alpha", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert "nothing to merge" in str(result.exception)


# ===========================================================================
# CLI: rm
# ===========================================================================


def test_taxonomy_rm_unreferenced_value(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha", "beta",
                        "--library", str(vault)])
    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "alpha", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_taxonomy(vault)["topics"] == ["beta"]


def test_taxonomy_rm_referenced_refused(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["peptide"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])

    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "peptide", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    err = str(result.exception)
    assert "2 paper" in err
    assert "2024_A" in err
    assert "2024_B" in err
    # Taxonomy NOT modified.
    assert _read_taxonomy(vault)["topics"] == ["peptide"]


def test_taxonomy_rm_unknown_value(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "ghost", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert "not registered" in str(result.exception)


def test_taxonomy_rm_fixed_enum(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "rm", "status", "inbox", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)


# ===========================================================================
# CLI: smoke
# ===========================================================================


def test_taxonomy_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["taxonomy", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "add", "rename", "merge", "rm"):
        assert sub in result.output


def test_taxonomy_atomicity_metadata_and_index_match(vault: Path) -> None:
    """After rename, every paper's metadata and INDEX.json topics agree."""
    _write_paper(vault, "2024_A", topics=["peptide"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])
    runner.invoke(cli, ["taxonomy", "rename", "topics", "peptide", "AMP",
                        "--library", str(vault)])

    payload = json.loads((vault / "INDEX.json").read_text())
    for p in payload["papers"]:
        meta = _read_meta(vault, p["id"])
        assert meta["topics"] == p["topics"]
        assert "peptide" not in meta["topics"]
