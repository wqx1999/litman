"""Tests for `litman.core.graph_model` — M35 Phase 1 data-serialization layer.

Acceptance criteria (M35 §5 group A):

* A1 — layered JSON (aggregate project nodes + shared-paper edges; per-project
  drill-down subgraphs).
* A2 — relation edge types come from relations.py single source; extends /
  contradicts directed, related undirected; pairwise dedup; same pair under
  two relation types yields a multi-edge.
* A3 (P0) — a corrupt-YAML metadata fixture appears in the corrupt node list +
  summary count, never vanishes.
* A4 — dangling code-clone / deleted-project link / unregistered taxonomy /
  broken relation pairing get status:"invalid" + summary count.
* A5 — aggregate project--project edge weight = shared-paper count (threshold
  >= 1); topic produces no project--project edge.
* A6 — testable slice now: ``run_all_checks`` returns >= 1 error-severity Issue
  for a known-fault vault (the gate's data dependency). The CLI-refuses-to-
  launch part is Phase 3 and is NOT built here.
* A7 — zero new runtime Python deps (pyproject parse, no networkx) + adding a
  new RELATION_PAIRS type auto-flows through build_graph with no code change.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

import litman.core.graph_model as graph_model
from litman.core.checks import run_all_checks
from litman.core.document import list_papers
from litman.core.graph_model import UNASSIGNED, build_graph
from litman.core.library import create_vault
from litman.core.taxonomy import update_user_dict_section


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _write_paper(vault: Path, paper_id: str, *, pdf: bool = True, **fields: Any) -> Path:
    """Create a minimal paper folder with the given metadata fields.

    Mirrors tests/core/test_document.py's helper. ``pdf=True`` lands a stub
    paper.pdf so the paper is structurally complete for any health check that
    looks for it.
    """
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

    (paper_dir / "metadata.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
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
    """Create a paper folder with verbatim metadata.yaml content.

    Unlike ``_write_paper`` (which always emits a top-level mapping) this lets a
    test write a top-level non-mapping document (bare list / bare scalar) — a
    metadata.yaml that PARSES fine but yields a non-dict ``list_papers`` element.
    """
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
    """Register controlled-vocabulary values in TAXONOMY.md.

    TAXONOMY.md is read-only-locked by create_vault, so chmod it writable
    before rewriting its section (tests are not subject to the M32 lock UX).
    """
    taxonomy_file = vault / "TAXONOMY.md"
    taxonomy_file.chmod(0o644)
    text = taxonomy_file.read_text(encoding="utf-8")
    taxonomy_file.write_text(
        update_user_dict_section(text, dict_name, values), encoding="utf-8"
    )


def _find(items: list[dict[str, Any]], **kw: Any) -> dict[str, Any] | None:
    for it in items:
        if all(it.get(k) == v for k, v in kw.items()):
            return it
    return None


# ---------------------------------------------------------------------------
# A1 — layered JSON structure
# ---------------------------------------------------------------------------


def test_a1_layered_structure_keys(vault: Path) -> None:
    graph = build_graph(vault)
    assert set(graph) == {"summary", "aggregate", "drilldown"}
    assert set(graph["summary"]) == {
        "papers",
        "projects",
        "codes",
        "corrupt",
        "invalid_edges",
    }
    assert set(graph["aggregate"]) == {"nodes", "edges"}
    # The unassigned bucket always exists so consumers can rely on the key.
    assert UNASSIGNED in graph["drilldown"]


def test_a1_aggregate_has_project_nodes_and_drilldown_subgraphs(vault: Path) -> None:
    _set_config_projects(
        vault, {"pepforge": "/tmp/pepforge", "pepcodec": "/tmp/pepcodec"}
    )
    _register_taxonomy(vault, "projects", ["pepforge", "pepcodec"])
    _write_paper(vault, "p1", projects=["pepforge"])
    _write_paper(vault, "p2", projects=["pepcodec"])

    graph = build_graph(vault)
    agg_nodes = graph["aggregate"]["nodes"]
    assert {n["id"] for n in agg_nodes} == {"pepforge", "pepcodec"}
    assert all(n["type"] == "project" for n in agg_nodes)

    # Each project's drill-down subgraph carries the project node + its paper.
    pf = graph["drilldown"]["pepforge"]
    assert _find(pf["nodes"], id="pepforge", type="project") is not None
    assert _find(pf["nodes"], id="p1", type="paper") is not None
    pc = graph["drilldown"]["pepcodec"]
    assert _find(pc["nodes"], id="p2", type="paper") is not None


def test_a1_unassigned_bucket_holds_projectless_paper(vault: Path) -> None:
    _write_paper(vault, "lonely", projects=[])
    graph = build_graph(vault)
    bucket = graph["drilldown"][UNASSIGNED]
    node = _find(bucket["nodes"], id="lonely")
    assert node is not None
    assert node["type"] == "paper"
    assert node["group"] == UNASSIGNED


def test_a1_paper_node_size_is_degree(vault: Path) -> None:
    _register_taxonomy(vault, "projects", ["proj"])
    _set_config_projects(vault, {"proj": "/tmp/proj"})
    # a is in one project and relates to b -> degree 2.
    _write_paper(vault, "a", projects=["proj"], related=["b"])
    _write_paper(vault, "b", projects=[], related=["a"])
    graph = build_graph(vault)
    a = _find(graph["drilldown"]["proj"]["nodes"], id="a")
    assert a is not None
    assert a["size"] == 2  # project membership edge + related edge


def test_a1_project_node_size_is_paper_plus_code_count(vault: Path) -> None:
    _register_taxonomy(vault, "projects", ["proj"])
    _set_config_projects(vault, {"proj": "/tmp/proj"})
    # Build a real code clone on disk so it is not flagged dangling.
    repo_dir = vault / "codes" / "tool"
    repo_dir.mkdir(parents=True)
    (repo_dir / "repo-meta.yaml").write_text("name: tool\n", encoding="utf-8")
    _write_paper(vault, "a", projects=["proj"], **{"code-clones": ["tool"]})
    _write_paper(vault, "b", projects=["proj"])

    graph = build_graph(vault)
    proj = _find(graph["aggregate"]["nodes"], id="proj")
    assert proj is not None
    # 2 papers + 1 code reference contributed by paper a.
    assert proj["size"] == 3


# ---------------------------------------------------------------------------
# A2 — relation edges from single source, direction, dedup, multi-edge
# ---------------------------------------------------------------------------


def test_a2_related_is_undirected_and_deduped(vault: Path) -> None:
    _write_paper(vault, "a", related=["b"])
    _write_paper(vault, "b", related=["a"])
    graph = build_graph(vault)
    rels = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "related"
    ]
    # Symmetric A.related:[B] + B.related:[A] collapse to ONE undirected edge.
    # Both endpoints share no project so it lives once in the unassigned bucket.
    assert len(rels) == 1
    assert rels[0]["directed"] is False


def test_a2_extends_is_directed(vault: Path) -> None:
    _write_paper(vault, "a", extends=["b"], **{})
    _write_paper(vault, "b", **{"extended-by": ["a"]})
    graph = build_graph(vault)
    edges = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "extends"
    ]
    assert len(edges) == 1
    e = edges[0]
    assert e["directed"] is True
    assert e["source"] == "a" and e["target"] == "b"
    # Reverse field never produces its own edge (no double-draw).
    reverse_edges = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "extended-by"
    ]
    assert reverse_edges == []


def test_a2_same_pair_two_relation_types_is_multi_edge(vault: Path) -> None:
    # a both relates to and extends b.
    _write_paper(vault, "a", related=["b"], extends=["b"])
    _write_paper(vault, "b", related=["a"], **{"extended-by": ["a"]})
    graph = build_graph(vault)
    edge_types = sorted(
        {
            e["type"]
            for bucket in graph["drilldown"].values()
            for e in bucket["edges"]
            if e["type"] in ("related", "extends")
        }
    )
    assert edge_types == ["extends", "related"]


def test_a2_edge_types_sourced_from_relations_module() -> None:
    # The directed-field set is derived from RELATION_PAIRS, not hard-coded.
    from litman.core.relations import FORWARD_REF_FIELDS, RELATION_PAIRS

    expected_directed = {
        f for f in FORWARD_REF_FIELDS if RELATION_PAIRS.get(f) != f
    }
    assert set(graph_model._DIRECTED_FORWARD_FIELDS) == expected_directed


# ---------------------------------------------------------------------------
# A3 (P0) — corrupt paper never vanishes
# ---------------------------------------------------------------------------


def test_a3_corrupt_paper_surfaces_as_node_and_summary(vault: Path) -> None:
    _write_paper(vault, "good", projects=[])
    _write_corrupt_paper(vault, "2024_Bad_Paper")

    # list_papers (the production scan) drops the corrupt paper.
    assert "2024_Bad_Paper" not in {p["id"] for p in list_papers(vault)}

    graph = build_graph(vault)
    assert graph["summary"]["corrupt"] == 1
    corrupt_nodes = [
        n
        for bucket in graph["drilldown"].values()
        for n in bucket["nodes"]
        if n["type"] == "corrupt"
    ]
    assert [n["id"] for n in corrupt_nodes] == ["2024_Bad_Paper"]
    assert corrupt_nodes[0]["status"] == "corrupt"


def test_a3_corrupt_not_a_synthetic_aggregate_project(vault: Path) -> None:
    # OQ1: aggregate layer is real projects only; no corrupt pseudo-project.
    _write_corrupt_paper(vault, "2024_Bad_Paper")
    graph = build_graph(vault)
    assert graph["aggregate"]["nodes"] == []
    # But it is still present in the unassigned drill-down bucket.
    node = _find(graph["drilldown"][UNASSIGNED]["nodes"], id="2024_Bad_Paper")
    assert node is not None and node["type"] == "corrupt"


def test_a3_non_mapping_metadata_surfaces_as_corrupt_not_crash(vault: Path) -> None:
    # CRITICAL regression: a top-level non-mapping metadata.yaml (bare list /
    # bare scalar) PARSES but yields a non-dict list_papers element. Without the
    # isinstance(p, dict) filter at the top of build_graph, the loaded_ids
    # comprehension dies with AttributeError ('list'/'str' has no .get) and one
    # malformed paper takes down the graph for the ENTIRE vault. With the
    # filter, those ids stay out of loaded_ids and re-surface as corrupt nodes
    # (disk_ids - loaded_ids), honoring invariant #14 P0 / A3.
    _write_paper(vault, "good", projects=[])
    _write_raw_metadata_paper(vault, "2024_Bare_List", "- a\n- b\n")
    _write_raw_metadata_paper(vault, "2024_Bare_Scalar", "just-a-string\n")

    # list_papers leaks these through as non-dict elements (its `if not
    # metadata` guard only catches {}/None, not a non-empty list/str).
    leaked = [p for p in list_papers(vault) if not isinstance(p, dict)]
    assert leaked, "fixture precondition: list_papers should leak a non-dict"

    graph = build_graph(vault)  # must NOT raise
    assert graph["summary"]["corrupt"] >= 2
    corrupt_ids = {
        n["id"]
        for bucket in graph["drilldown"].values()
        for n in bucket["nodes"]
        if n["type"] == "corrupt"
    }
    assert "2024_Bare_List" in corrupt_ids
    assert "2024_Bare_Scalar" in corrupt_ids
    # The good paper is unaffected — graph still builds for the rest of the vault.
    assert _find(graph["drilldown"][UNASSIGNED]["nodes"], id="good") is not None


# ---------------------------------------------------------------------------
# A4 — invalid markers
# ---------------------------------------------------------------------------


def test_a4_dangling_code_clone_marked_invalid(vault: Path) -> None:
    # code-clones names a repo with no codes/<name>/repo-meta.yaml.
    _write_paper(vault, "a", **{"code-clones": ["ghost-repo"]})
    graph = build_graph(vault)
    code_node = _find(
        graph["drilldown"][UNASSIGNED]["nodes"], id="ghost-repo", type="code"
    )
    assert code_node is not None
    assert code_node["status"] == "invalid"
    code_edges = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "code-clones"
    ]
    assert any(e["status"] == "invalid" for e in code_edges)
    assert graph["summary"]["invalid_edges"] >= 1


def test_a4_deleted_project_link_marked_invalid(vault: Path) -> None:
    # Paper links a project absent from the config projects map. Register the
    # project in TAXONOMY.md so the ONLY fault is "present in TAXONOMY but
    # absent from config" — isolating the deleted-project (config) path from the
    # unregistered-taxonomy path, which would otherwise also flag the paper.
    _register_taxonomy(vault, "projects", ["ghost-project"])
    _set_config_projects(vault, {})  # empty projects map
    _write_paper(vault, "a", projects=["ghost-project"])
    graph = build_graph(vault)
    proj_edges = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "projects"
    ]
    assert proj_edges, "expected a project membership edge"
    assert all(e["status"] == "invalid" for e in proj_edges)
    assert graph["summary"]["invalid_edges"] >= 1


def test_a4_unregistered_taxonomy_marks_paper_invalid(vault: Path) -> None:
    # topics value not registered in TAXONOMY.md.
    _write_paper(vault, "a", topics=["unregistered-topic"])
    graph = build_graph(vault)
    node = _find(graph["drilldown"][UNASSIGNED]["nodes"], id="a", type="paper")
    assert node is not None
    assert node["status"] == "invalid"


def test_a4_registered_taxonomy_keeps_paper_ok(vault: Path) -> None:
    _register_taxonomy(vault, "topics", ["graph-viz"])
    _write_paper(vault, "a", topics=["graph-viz"])
    graph = build_graph(vault)
    node = _find(graph["drilldown"][UNASSIGNED]["nodes"], id="a", type="paper")
    assert node is not None
    assert node["status"] == "ok"


def test_a4_broken_relation_pairing_marked_invalid(vault: Path) -> None:
    # a.extends:[b] but b has no extended-by:[a] -> broken pairing.
    _write_paper(vault, "a", extends=["b"])
    _write_paper(vault, "b")  # missing the reverse field
    graph = build_graph(vault)
    edges = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "extends"
    ]
    assert edges
    assert all(e["status"] == "invalid" for e in edges)
    assert graph["summary"]["invalid_edges"] >= 1


def test_a4_relation_to_nonexistent_id_is_invalid_not_crash(vault: Path) -> None:
    # OQ4: a relation pointing at a non-existent / other-vault id is treated as
    # a missing-endpoint broken pairing — invalid edge, never a crash.
    _write_paper(vault, "a", related=["does-not-exist"])
    graph = build_graph(vault)  # must not raise
    rels = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "related"
    ]
    assert rels
    assert all(e["status"] == "invalid" for e in rels)


def test_a4_scalar_list_field_is_one_node_not_per_character(vault: Path) -> None:
    # WARNING regression: a list-typed field written as a bare scalar
    # (`projects: pepforge`, no `- ` list item) must coerce to a SINGLE value,
    # not iterate its characters into 6 phantom project nodes p,e,p,f,o,r,g,e.
    _write_raw_metadata_paper(
        vault,
        "a",
        "id: a\n"
        "title: Scalar Project Paper\n"
        "created-at: 2024-01-01T00:00:00+00:00\n"
        "updated-at: 2024-01-01T00:00:00+00:00\n"
        "status: inbox\n"
        "projects: pepforge\n",
    )
    graph = build_graph(vault)

    proj_edges = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "projects"
    ]
    # Exactly ONE membership edge to the single project "pepforge".
    assert len(proj_edges) == 1
    assert proj_edges[0]["source"] == "pepforge"
    assert proj_edges[0]["target"] == "a"
    # No per-character phantom project nodes leaked into the aggregate layer.
    assert {n["id"] for n in graph["aggregate"]["nodes"]} == {"pepforge"}


# ---------------------------------------------------------------------------
# A5 — aggregate project--project shared-paper edges
# ---------------------------------------------------------------------------


def test_a5_shared_paper_project_edge_weight(vault: Path) -> None:
    _register_taxonomy(vault, "projects", ["pepforge", "pepcodec"])
    _set_config_projects(
        vault, {"pepforge": "/tmp/pepforge", "pepcodec": "/tmp/pepcodec"}
    )
    _write_paper(vault, "shared1", projects=["pepforge", "pepcodec"])
    _write_paper(vault, "shared2", projects=["pepforge", "pepcodec"])
    _write_paper(vault, "solo", projects=["pepforge"])

    graph = build_graph(vault)
    agg_edges = graph["aggregate"]["edges"]
    assert len(agg_edges) == 1
    e = agg_edges[0]
    assert {e["source"], e["target"]} == {"pepforge", "pepcodec"}
    assert e["weight"] == 2  # shared1 + shared2
    assert e["directed"] is False


def test_a5_no_shared_paper_no_project_edge(vault: Path) -> None:
    _register_taxonomy(vault, "projects", ["pepforge", "pepcodec"])
    _set_config_projects(
        vault, {"pepforge": "/tmp/pepforge", "pepcodec": "/tmp/pepcodec"}
    )
    _write_paper(vault, "p1", projects=["pepforge"])
    _write_paper(vault, "p2", projects=["pepcodec"])
    graph = build_graph(vault)
    assert graph["aggregate"]["edges"] == []


def test_a5_topic_does_not_produce_project_edge(vault: Path) -> None:
    _register_taxonomy(vault, "projects", ["pepforge", "pepcodec"])
    _register_taxonomy(vault, "topics", ["shared-topic"])
    _set_config_projects(
        vault, {"pepforge": "/tmp/pepforge", "pepcodec": "/tmp/pepcodec"}
    )
    # Two papers in different projects share a topic but no project.
    _write_paper(vault, "p1", projects=["pepforge"], topics=["shared-topic"])
    _write_paper(vault, "p2", projects=["pepcodec"], topics=["shared-topic"])
    graph = build_graph(vault)
    # A shared topic must NOT create a project--project edge.
    assert graph["aggregate"]["edges"] == []


# ---------------------------------------------------------------------------
# A6 — run_all_checks gate data dependency (CLI refusal is Phase 3, not built)
# ---------------------------------------------------------------------------


def test_a6_run_all_checks_reports_error_for_known_fault_vault(vault: Path) -> None:
    # A6 testable slice: build_graph does NOT call run_all_checks (OQ2 — the
    # --check gate is Phase 3). This only asserts the gate's DATA DEPENDENCY:
    # run_all_checks surfaces >= 1 error-severity Issue on a known-fault vault.
    # The "CLI refuses to launch the GUI" wiring is Phase 3 and not built here.
    _write_paper(vault, "a", extends=["nonexistent-paper"])
    papers = list_papers(vault)
    issues = run_all_checks(vault, papers)
    errors = [i for i in issues if i.severity == "error"]
    assert len(errors) >= 1


# ---------------------------------------------------------------------------
# A7 — zero new runtime deps + RELATION_PAIRS auto-flow
# ---------------------------------------------------------------------------


def _pyproject_path() -> Path:
    # graph_model.py lives at src/litman/core/; pyproject.toml is the package
    # root two levels above src/.
    return Path(graph_model.__file__).resolve().parents[3] / "pyproject.toml"


def test_a7_no_new_runtime_dependency_and_no_networkx() -> None:
    data = tomllib.loads(_pyproject_path().read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    dep_names = {
        dep.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip().lower()
        for dep in deps
    }
    assert "networkx" not in dep_names
    # The runtime set is frozen at exactly these seven (no graph lib snuck in).
    assert dep_names == {
        "click",
        "ruamel.yaml",
        "httpx",
        "pypdf",
        "pydantic",
        "rich",
        "platformdirs",
    }


def test_a7_new_relation_type_auto_flows(vault: Path, monkeypatch: Any) -> None:
    # Adding a synthetic `cites`/`cited-by` pair to RELATION_PAIRS must flow
    # through build_graph with NO change to graph_model.py — edge types and the
    # directed/forward derivation read from the live RELATION_PAIRS map.
    from litman.core import relations

    new_pairs = dict(relations.RELATION_PAIRS)
    new_pairs["cites"] = "cited-by"
    new_pairs["cited-by"] = "cites"
    monkeypatch.setattr(relations, "RELATION_PAIRS", new_pairs)
    monkeypatch.setattr(
        relations,
        "REVERSE_REF_FIELDS",
        relations.REVERSE_REF_FIELDS | {"cited-by"},
    )
    new_forward = tuple(
        f for f in new_pairs if f not in (relations.REVERSE_REF_FIELDS | {"cited-by"})
    )
    monkeypatch.setattr(relations, "FORWARD_REF_FIELDS", new_forward)
    # graph_model binds FORWARD_REF_FIELDS / RELATION_PAIRS at import time, so
    # re-point those module-level references too (the auto-flow contract is
    # "no graph_model.py SOURCE change", patching the binding is fair game).
    monkeypatch.setattr(graph_model, "RELATION_PAIRS", new_pairs)
    monkeypatch.setattr(graph_model, "FORWARD_REF_FIELDS", new_forward)
    monkeypatch.setattr(
        graph_model,
        "_DIRECTED_FORWARD_FIELDS",
        tuple(f for f in new_forward if new_pairs.get(f) != f),
    )

    _write_paper(vault, "a", cites=["b"])
    _write_paper(vault, "b", **{"cited-by": ["a"]})
    graph = build_graph(vault)
    cites_edges = [
        e
        for bucket in graph["drilldown"].values()
        for e in bucket["edges"]
        if e["type"] == "cites"
    ]
    assert len(cites_edges) == 1
    assert cites_edges[0]["directed"] is True
    assert cites_edges[0]["status"] == "ok"  # properly paired -> valid
