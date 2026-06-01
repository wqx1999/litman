"""Tests for `lit related <id>` (M33)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault


def _seed_paper(vault: Path, paper_id: str, **fields: object) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines: list[str] = [f"id: {paper_id}", f"title: Title of {paper_id}"]
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


def _invoke(vault: Path, *args: str):
    runner = CliRunner()
    return runner.invoke(cli, ["related", *args, "--library", str(vault)])


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Target T plus neighbours reachable by edge and by taxonomy.

    Edges (on T): T.extends -> E1; T.related -> E2.
    Reverse edge: R names T in its `extended-by`? No — reverse reachability is
    tested via T's own field, since the CLI double-writes both ends. We seed a
    reverse case explicitly with T.extended-by -> RV.
    Taxonomy: TX1 shares 2 keys (topics:nlp + methods:attn); TX2 shares 1
    (topics:nlp); NOISE shares 0.
    """
    v = create_vault(tmp_path)
    _seed_paper(
        v, "T",
        topics=["nlp", "vision"],
        methods=["attn", "cnn"],
        extends=["E1"],
        related=["E2"],
        **{"extended-by": ["RV"]},
    )
    _seed_paper(v, "E1", topics=["other"], methods=["other"])
    _seed_paper(v, "E2", topics=["other"], methods=["other"])
    _seed_paper(v, "RV", topics=["other"], methods=["other"])
    _seed_paper(v, "TX1", topics=["nlp", "x"], methods=["attn", "y"])
    _seed_paper(v, "TX2", topics=["nlp"], methods=["z"])
    _seed_paper(v, "NOISE", topics=["unrelated"], methods=["unrelated"])
    return v


def test_related_edge_neighbours(vault: Path) -> None:
    result = _invoke(vault, "T", "--by", "edges")
    assert result.exit_code == 0, result.output
    items = json.loads(result.output)
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {"E1", "E2", "RV"}
    assert by_id["E1"]["via"] == "edge"
    assert by_id["E1"]["edge"] == "extends"
    assert by_id["E2"]["edge"] == "related"
    # Reverse field is reachable via T's own extended-by.
    assert by_id["RV"]["edge"] == "extended-by"
    # Edge items carry no `shared` key (heterogeneous via schema).
    assert "shared" not in by_id["E1"]


def test_related_taxonomy_neighbours(vault: Path) -> None:
    result = _invoke(vault, "T", "--by", "taxonomy")
    assert result.exit_code == 0, result.output
    items = json.loads(result.output)
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {"TX1", "TX2"}
    assert by_id["TX1"]["via"] == "taxonomy"
    assert set(by_id["TX1"]["shared"]) == {"topics:nlp", "methods:attn"}
    assert by_id["TX2"]["shared"] == ["topics:nlp"]
    # Taxonomy items carry no `edge` key.
    assert "edge" not in by_id["TX1"]


def test_related_taxonomy_sorted_by_shared_desc(vault: Path) -> None:
    result = _invoke(vault, "T", "--by", "taxonomy")
    items = json.loads(result.output)
    ids = [i["id"] for i in items]
    # TX1 (2 shared) before TX2 (1 shared).
    assert ids == ["TX1", "TX2"]


def test_related_edges_first_in_merged(vault: Path) -> None:
    result = _invoke(vault, "T")
    items = json.loads(result.output)
    vias = [i["via"] for i in items]
    # All edges precede all taxonomy entries.
    first_taxonomy = vias.index("taxonomy")
    assert all(v == "edge" for v in vias[:first_taxonomy])
    assert all(v == "taxonomy" for v in vias[first_taxonomy:])


def test_related_min_shared_tightening(vault: Path) -> None:
    result = _invoke(vault, "T", "--by", "taxonomy", "--min-shared", "2")
    items = json.loads(result.output)
    assert {i["id"] for i in items} == {"TX1"}


def test_related_limit_truncation(vault: Path) -> None:
    result = _invoke(vault, "T", "--limit", "2")
    items = json.loads(result.output)
    assert len(items) == 2
    # Edges come first, so the top-2 are edges.
    assert all(i["via"] == "edge" for i in items)


def test_related_projection_schema(vault: Path) -> None:
    """Each neighbour reuses the INDEX projection plus the via annotation."""
    from litman.core.views import INDEX_PAPER_FIELDS

    result = _invoke(vault, "T", "--by", "taxonomy")
    items = json.loads(result.output)
    item = items[0]
    for field in INDEX_PAPER_FIELDS:
        assert field in item
    assert "via" in item


def test_related_isolated_paper_returns_empty(vault: Path) -> None:
    result = _invoke(vault, "NOISE")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_related_empty_vault(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    _seed_paper(v, "Lonely", topics=["x"], methods=["y"])
    result = _invoke(v, "Lonely")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_related_does_not_include_self(vault: Path) -> None:
    result = _invoke(vault, "T")
    items = json.loads(result.output)
    assert "T" not in {i["id"] for i in items}


def test_related_table_format(vault: Path) -> None:
    result = _invoke(vault, "T", "--format", "table")
    assert result.exit_code == 0, result.output
    assert "E1" in result.output
    assert "edge" in result.output


def test_related_dangling_edge_skipped(tmp_path: Path) -> None:
    """A forward edge to a missing paper is silently skipped (read-only)."""
    v = create_vault(tmp_path)
    _seed_paper(v, "T", extends=["GHOST"], topics=["x"], methods=["y"])
    result = _invoke(v, "T", "--by", "edges")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_related_edge_and_taxonomy_overlap_dedups_as_edge(tmp_path: Path) -> None:
    """A paper that is BOTH an edge neighbour AND a taxonomy match (spec §3.4 /
    AC #2): in merged mode it appears exactly ONCE, as the edge, never repeated
    as a taxonomy neighbour."""
    v = create_vault(tmp_path)
    _seed_paper(v, "T", topics=["nlp"], extends=["P"])
    # P is reachable by edge (T.extends -> P) AND shares topics:nlp with T.
    _seed_paper(v, "P", topics=["nlp"])

    result = _invoke(v, "T")
    assert result.exit_code == 0, result.output
    items = json.loads(result.output)
    p_entries = [i for i in items if i["id"] == "P"]
    assert len(p_entries) == 1
    assert p_entries[0]["via"] == "edge"
    assert p_entries[0]["edge"] == "extends"
    # The edge claim must suppress the taxonomy duplicate entirely.
    assert "shared" not in p_entries[0]


def test_related_edge_paper_still_surfaces_under_by_taxonomy(tmp_path: Path) -> None:
    """The edge pre-claim in merged mode must NOT leak into --by taxonomy:
    when the edge loop is skipped, an edge-and-taxonomy overlap paper still
    surfaces as a taxonomy match."""
    v = create_vault(tmp_path)
    _seed_paper(v, "T", topics=["nlp"], extends=["P"])
    _seed_paper(v, "P", topics=["nlp"])

    result = _invoke(v, "T", "--by", "taxonomy")
    assert result.exit_code == 0, result.output
    items = json.loads(result.output)
    p_entries = [i for i in items if i["id"] == "P"]
    assert len(p_entries) == 1
    assert p_entries[0]["via"] == "taxonomy"
    assert p_entries[0]["shared"] == ["topics:nlp"]
