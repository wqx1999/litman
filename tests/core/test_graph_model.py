"""Unit tests for ``core/graph_model.build_graph`` (M35, rev-2 paper-centric).

The graph is a network of *papers*: nodes are papers (+ corrupt papers),
edges are paper<->paper relations only, and project / topic / method / data /
code-clone membership rides on each node's ``dims`` map so the frontend can
colour / cluster / focus by any dimension. There are no project or code nodes.

Covered:

* structure — ``summary`` / ``nodes`` / ``edges`` / ``dimensions`` keys; every
  node is a paper or corrupt; ``dims`` carries all five dimensions.
* relations — ``related`` undirected + deduped, ``extends`` / ``contradicts``
  directed, same-pair-two-types multi-edge, edge types sourced from the
  relations module (D7), degree = relation degree.
* corrupt — surfaces as a ``corrupt`` node + ``summary["corrupt"]`` with empty
  dims, never a synthetic project (invariant #14 / OQ1); a non-mapping
  metadata.yaml surfaces as corrupt, not a crash (A3).
* drift — dangling code-clone, deleted-project link, unregistered taxonomy land
  in ``dimensions[d].invalid`` and flip the carrying paper to ``invalid``;
  broken relation pairing marks the edge invalid + the source paper invalid +
  bumps ``summary["invalid_edges"]``; a relation to a non-existent id does not
  crash (OQ4).
* coercion — a scalar list field is one value, not one per character.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from litman.core.graph_model import _MAX_AUTHORS, DIMENSIONS, build_graph
from litman.core.library import create_vault
from litman.core.relations import FORWARD_REF_FIELDS
from litman.core.taxonomy import update_user_dict_section


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _write_paper(vault: Path, paper_id: str, *, pdf: bool = True, **fields: Any) -> Path:
    """Create a minimal paper folder with the given metadata fields."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)

    base: dict[str, Any] = {
        "id": paper_id,
        "title": fields.pop("title", f"Title of {paper_id}"),
        "created-at": "2024-01-01T00:00:00+00:00",
        "updated-at": "2024-01-01T00:00:00+00:00",
        "status": "inbox",
    }
    base.update(fields)

    lines: list[str] = []
    for key, value in base.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")

    (paper_dir / "metadata.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if pdf:
        (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    return paper_dir


def _write_corrupt_paper(vault: Path, paper_id: str) -> Path:
    """Create a paper folder whose metadata.yaml is unparseable YAML."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text("{not: valid: yaml:", encoding="utf-8")
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    return paper_dir


def _write_raw_metadata_paper(vault: Path, paper_id: str, raw: str) -> Path:
    """Create a paper folder with verbatim metadata.yaml content (may be non-mapping)."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(raw, encoding="utf-8")
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    return paper_dir


def _set_config_projects(vault: Path, projects: dict[str, str]) -> None:
    """Rewrite lit-config.yaml with a projects: map (preserving library_name)."""
    cfg = vault / "lit-config.yaml"
    lines = ["library_name: literature_vault"]
    if projects:
        lines.append("projects:")
        for name, path in projects.items():
            lines.append(f"  {name}: {path}")
    else:
        lines.append("projects: {}")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _register_taxonomy(vault: Path, dict_name: str, values: list[str]) -> None:
    """Register controlled-vocabulary values in TAXONOMY.md (chmod past the lock)."""
    taxonomy_file = vault / "TAXONOMY.md"
    taxonomy_file.chmod(0o644)
    text = taxonomy_file.read_text(encoding="utf-8")
    taxonomy_file.write_text(
        update_user_dict_section(text, dict_name, values), encoding="utf-8"
    )


def _node(graph: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    return None


def _edges(graph: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
    return [e for e in graph["edges"] if all(e.get(k) == v for k, v in kw.items())]


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_structure_top_level_keys(vault: Path) -> None:
    graph = build_graph(vault)
    assert set(graph) == {"summary", "nodes", "edges", "dimensions"}
    assert set(graph["summary"]) == {"papers", "corrupt", "invalid_edges", "dimensions"}
    assert set(graph["dimensions"]) == set(DIMENSIONS)
    for dim in DIMENSIONS:
        assert set(graph["dimensions"][dim]) == {"values", "invalid"}


def test_nodes_are_only_papers_and_corrupt(vault: Path) -> None:
    _set_config_projects(vault, {"pepforge": "/tmp/pf", "pepcodec": "/tmp/pc"})
    _write_paper(vault, "a", projects=["pepforge"])
    _write_paper(vault, "b", projects=["pepcodec"])
    _write_paper(vault, "c", **{"code-clones": ["repo-x"]})
    graph = build_graph(vault)

    # No project or code nodes — only the three papers.
    assert {n["id"] for n in graph["nodes"]} == {"a", "b", "c"}
    assert {n["type"] for n in graph["nodes"]} == {"paper"}


def test_node_carries_all_dimensions(vault: Path) -> None:
    _set_config_projects(vault, {"pepforge": "/tmp/pf"})
    _register_taxonomy(vault, "topics", ["amp"])
    _register_taxonomy(vault, "methods", ["dl"])
    _write_paper(
        vault,
        "a",
        projects=["pepforge"],
        topics=["amp"],
        methods=["dl"],
        **{"code-clones": ["repo-x"]},
    )
    node = _node(build_graph(vault), "a")
    assert node is not None
    assert set(node["dims"]) == set(DIMENSIONS)
    assert node["dims"]["projects"] == ["pepforge"]
    assert node["dims"]["topics"] == ["amp"]
    assert node["dims"]["methods"] == ["dl"]
    assert node["dims"]["data"] == []
    assert node["dims"]["codes"] == ["repo-x"]


def test_dimensions_values_are_distinct_present_values(vault: Path) -> None:
    _set_config_projects(vault, {"p1": "/tmp/p1", "p2": "/tmp/p2"})
    _write_paper(vault, "a", projects=["p1"])
    _write_paper(vault, "b", projects=["p1", "p2"])
    dims = build_graph(vault)["dimensions"]
    assert dims["projects"]["values"] == ["p1", "p2"]
    assert dims["projects"]["invalid"] == []


def test_summary_counts(vault: Path) -> None:
    _set_config_projects(vault, {"p1": "/tmp/p1"})
    _register_taxonomy(vault, "topics", ["t1", "t2"])
    _write_paper(vault, "a", projects=["p1"], topics=["t1"])
    _write_paper(vault, "b", topics=["t2"])
    _write_corrupt_paper(vault, "2020_bad")
    summary = build_graph(vault)["summary"]
    assert summary["papers"] == 2
    assert summary["corrupt"] == 1
    assert summary["dimensions"]["projects"] == 1
    assert summary["dimensions"]["topics"] == 2


# ---------------------------------------------------------------------------
# Relations (paper <-> paper edges)
# ---------------------------------------------------------------------------


def test_related_is_undirected_and_deduped(vault: Path) -> None:
    _write_paper(vault, "a", related=["b"])
    _write_paper(vault, "b", related=["a"])
    rel = _edges(build_graph(vault), type="related")
    assert len(rel) == 1
    assert rel[0]["directed"] is False


def test_extends_is_directed(vault: Path) -> None:
    _write_paper(vault, "a", extends=["b"])
    _write_paper(vault, "b", **{"extended-by": ["a"]})
    ext = _edges(build_graph(vault), type="extends")
    assert len(ext) == 1
    assert ext[0]["directed"] is True
    assert ext[0]["source"] == "a" and ext[0]["target"] == "b"
    assert ext[0]["status"] == "ok"


def test_same_pair_two_relation_types_is_multi_edge(vault: Path) -> None:
    _write_paper(vault, "a", related=["b"], extends=["b"])
    _write_paper(vault, "b", related=["a"], **{"extended-by": ["a"]})
    graph = build_graph(vault)
    types = {e["type"] for e in graph["edges"] if {e["source"], e["target"]} == {"a", "b"}}
    assert types == {"related", "extends"}


def test_edge_types_sourced_from_relations_module(vault: Path) -> None:
    # Every forward relation field should be representable; using all of them
    # produces edges of exactly those types (D7 — no hard-coded name list).
    _write_paper(vault, "a", related=["b"], extends=["b"], contradicts=["b"])
    _write_paper(
        vault,
        "b",
        related=["a"],
        **{"extended-by": ["a"], "contradicted-by": ["a"]},
    )
    types = {e["type"] for e in build_graph(vault)["edges"]}
    assert types <= set(FORWARD_REF_FIELDS)
    assert types == {"related", "extends", "contradicts"}


def test_degree_is_relation_degree(vault: Path) -> None:
    _write_paper(vault, "a", related=["b"], extends=["c"])
    _write_paper(vault, "b", related=["a"])
    _write_paper(vault, "c", **{"extended-by": ["a"]})
    graph = build_graph(vault)
    assert _node(graph, "a")["degree"] == 2  # related b + extends c
    assert _node(graph, "b")["degree"] == 1
    assert _node(graph, "c")["degree"] == 1


# ---------------------------------------------------------------------------
# Corrupt papers (invariant #14 / OQ1)
# ---------------------------------------------------------------------------


def test_corrupt_surfaces_as_node_with_empty_dims(vault: Path) -> None:
    _write_paper(vault, "good")
    _write_corrupt_paper(vault, "2024_bad")
    graph = build_graph(vault)
    bad = _node(graph, "2024_bad")
    assert bad is not None
    assert bad["type"] == "corrupt" and bad["status"] == "corrupt"
    assert all(bad["dims"][d] == [] for d in DIMENSIONS)
    assert graph["summary"]["corrupt"] == 1


def test_corrupt_is_never_a_project_value(vault: Path) -> None:
    _write_corrupt_paper(vault, "2024_bad")
    graph = build_graph(vault)
    # A corrupt paper contributes no project / dimension value.
    for dim in DIMENSIONS:
        assert graph["dimensions"][dim]["values"] == []


def test_non_mapping_metadata_surfaces_as_corrupt_not_crash(vault: Path) -> None:
    # A bare-list metadata.yaml parses but is not a dict; list_papers lets it
    # through as a non-dict element. build_graph must drop it from loaded papers
    # and re-surface it as corrupt rather than crashing on the first .get().
    _write_raw_metadata_paper(vault, "2024_list", "- just\n- a\n- list\n")
    _write_paper(vault, "good")
    graph = build_graph(vault)  # must NOT raise
    assert _node(graph, "2024_list")["type"] == "corrupt"
    assert _node(graph, "good") is not None


# ---------------------------------------------------------------------------
# Drift (dimensions[d].invalid + node status)
# ---------------------------------------------------------------------------


def test_dangling_code_clone_marks_value_and_node_invalid(vault: Path) -> None:
    _write_paper(vault, "a", **{"code-clones": ["ghost-repo"]})
    graph = build_graph(vault)
    assert graph["dimensions"]["codes"]["invalid"] == ["ghost-repo"]
    assert _node(graph, "a")["status"] == "invalid"


def test_deleted_project_link_marks_value_and_node_invalid(vault: Path) -> None:
    _set_config_projects(vault, {"live": "/tmp/live"})
    _write_paper(vault, "a", projects=["deleted"])
    graph = build_graph(vault)
    assert "deleted" in graph["dimensions"]["projects"]["invalid"]
    assert _node(graph, "a")["status"] == "invalid"


def test_unregistered_taxonomy_marks_value_and_node_invalid(vault: Path) -> None:
    _register_taxonomy(vault, "topics", ["registered"])
    _write_paper(vault, "a", topics=["unregistered"])
    graph = build_graph(vault)
    assert "unregistered" in graph["dimensions"]["topics"]["invalid"]
    assert _node(graph, "a")["status"] == "invalid"


def test_registered_values_keep_node_ok(vault: Path) -> None:
    _set_config_projects(vault, {"pepforge": "/tmp/pf"})
    _register_taxonomy(vault, "topics", ["amp"])
    _write_paper(vault, "a", projects=["pepforge"], topics=["amp"])
    graph = build_graph(vault)
    assert _node(graph, "a")["status"] == "ok"
    assert graph["dimensions"]["projects"]["invalid"] == []
    assert graph["dimensions"]["topics"]["invalid"] == []


def test_broken_relation_pairing_marks_edge_and_source_invalid(vault: Path) -> None:
    # a extends b, but b lacks the matching extended-by -> broken pairing.
    _write_paper(vault, "a", extends=["b"])
    _write_paper(vault, "b")
    graph = build_graph(vault)
    ext = _edges(graph, type="extends")
    assert len(ext) == 1 and ext[0]["status"] == "invalid"
    assert _node(graph, "a")["status"] == "invalid"  # source surfaces
    assert graph["summary"]["invalid_edges"] == 1


def test_relation_to_nonexistent_id_is_invalid_not_crash(vault: Path) -> None:
    _write_paper(vault, "a", extends=["nope-not-here"])
    graph = build_graph(vault)  # must not raise
    ext = _edges(graph, type="extends")
    assert len(ext) == 1 and ext[0]["status"] == "invalid"
    assert _node(graph, "a")["status"] == "invalid"
    assert graph["summary"]["invalid_edges"] == 1


# ---------------------------------------------------------------------------
# Detail-card meta projection
# ---------------------------------------------------------------------------


def test_node_meta_carries_bibliographic_fields(vault: Path) -> None:
    _write_paper(
        vault,
        "a",
        title="A title",
        year=2021,
        authors=["Smith, John", "Doe, Jane"],
        journal="Bioinformatics",
        doi="10.1/x",
        type="research",
        priority="A",
        status="deep-read",
    )
    meta = _node(build_graph(vault), "a")["meta"]
    assert meta["year"] == 2021
    assert meta["authors"] == ["Smith, John", "Doe, Jane"]
    assert meta["n_authors"] == 2
    assert meta["journal"] == "Bioinformatics"
    assert meta["doi"] == "10.1/x"
    assert meta["type"] == "research"
    assert meta["priority"] == "A"
    # read_status is the paper's triage status, distinct from the node's graph
    # status (which is "ok" here).
    assert meta["read_status"] == "deep-read"


def test_node_meta_caps_authors_but_keeps_true_count(vault: Path) -> None:
    many = [f"Author{i}, X" for i in range(30)]
    _write_paper(vault, "a", authors=many)
    meta = _node(build_graph(vault), "a")["meta"]
    assert meta["n_authors"] == 30
    assert len(meta["authors"]) == _MAX_AUTHORS


def test_corrupt_node_has_empty_meta(vault: Path) -> None:
    _write_corrupt_paper(vault, "2020_bad")
    meta = _node(build_graph(vault), "2020_bad")["meta"]
    assert meta["year"] is None
    assert meta["authors"] == []
    assert meta["n_authors"] == 0
    assert meta["journal"] == ""
    assert meta["read_status"] == ""


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def test_scalar_field_is_one_value_not_per_character(vault: Path) -> None:
    # A scalar (non-list) projects field must coerce to a single value, never
    # one phantom value per character.
    _set_config_projects(vault, {"pepforge": "/tmp/pf"})
    _write_paper(vault, "a", projects="pepforge")  # scalar, not a list
    graph = build_graph(vault)
    assert _node(graph, "a")["dims"]["projects"] == ["pepforge"]
    assert graph["dimensions"]["projects"]["values"] == ["pepforge"]
