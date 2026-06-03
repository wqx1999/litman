"""Knowledge-graph serialization layer for ``lit graph`` (M35 Phase 1).

Reconstructs the *emergent* knowledge graph (identity.md) from vault metadata
into a layered JSON snapshot the frontend renders. This module is **read-only**
and produces nothing on disk — the caller (a future ``commands/graph.py``)
injects the returned dict into a static page.

Two layers (D5):

* **aggregate** — one node per project plus a project--project edge whose weight
  is the count of papers shared by both projects (D5, A5). Topics never create
  project edges. This is the always-readable top view for any library size.
* **drilldown** — per-project subgraphs (project->paper / paper->code membership
  edges plus paper<->paper relation edges) keyed by project name, with an
  ``"(unassigned)"`` bucket for papers in no project.

Design contracts (from the M35 spec §2.2 + proposal D4--D7):

* Papers are enumerated via :func:`document.list_papers` — no second scan loop.
* A corrupt-metadata paper that ``list_papers`` silently drops is re-enumerated
  here and surfaces as a ``type:"corrupt"`` node + ``summary["corrupt"]`` count.
  It must never vanish from the output (invariant #14, P0 / OQ1).
* Relation edge types come from :data:`relations.RELATION_PAIRS` via
  :data:`relations.FORWARD_REF_FIELDS` (single source, D7) — adding a new
  relation pair to that map auto-flows here with no change to this module.
* ``related`` is undirected with symmetric dedup; ``extends`` / ``contradicts``
  are directed A->B; the same node pair with two relation types yields two
  edges (multi-edge, D6).
* Invalid conditions (dangling code-clone, deleted-project link, unregistered
  taxonomy value, broken relation pairing) are re-derived here from the same
  *primitives* the health checks use (RELATION_PAIRS, parse_taxonomy + the
  user-dict set, the config projects map, the codes dir / repo-meta filename),
  not by string-parsing ``run_all_checks`` output (OQ2). A relation pointing at
  a non-existent / other-vault id is treated as a missing-endpoint broken
  pairing — the edge is marked invalid, never a crash (OQ4).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from litman.core.code import CODES_DIRNAME, REPO_META_FILENAME
from litman.core.config import load_config
from litman.core.document import list_papers
from litman.core.id import is_valid_id
from litman.core.relations import FORWARD_REF_FIELDS, RELATION_PAIRS
from litman.core.taxonomy import USER_DICTS, parse_taxonomy
from litman.exceptions import ConfigError

# Bucket label for papers that belong to no project. A literal (not a project
# name) so it can never collide with a real project — project names are
# TAXONOMY-governed identifiers and parentheses are not valid id chars there.
UNASSIGNED = "(unassigned)"

# Relation fields that draw a *directed* A->B arrow. ``related`` (the remaining
# forward field) is undirected. Derived from RELATION_PAIRS so a future
# self-paired (symmetric) field is undirected and any new directional pair is
# directed automatically (D7) — never a hard-coded relation name list.
_DIRECTED_FORWARD_FIELDS: tuple[str, ...] = tuple(
    f for f in FORWARD_REF_FIELDS if RELATION_PAIRS.get(f) != f
)


def _as_str_list(value: Any) -> list[str]:
    """Coerce a metadata list-field value into a list of strings.

    metadata.yaml is schema-less (invariant #7): a list-typed field may be
    absent, ``None``, a proper list, or — when the user wrote ``projects: x``
    without a ``- x`` list item — a bare scalar. Iterating a bare scalar string
    with ``for x in value`` yields its CHARACTERS, exploding ``pepforge`` into
    six phantom nodes. This normalizes:

    * ``None`` / missing -> ``[]``
    * a ``list`` -> its elements stringified
    * a bare scalar -> a single-element ``[str(value)]``

    A bare scalar is wrapped, never dropped, so the value still SURFACES as one
    node and gets invalid-marked if unregistered (no silent-skip, invariant #14).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]


# ---------------------------------------------------------------------------
# Corrupt-paper enumeration (P0 — no silent disappearance, invariant #14)
# ---------------------------------------------------------------------------


def _enumerate_corrupt_ids(vault: Path, loaded_ids: set[str]) -> list[str]:
    """Paper dirs with a ``metadata.yaml`` that ``list_papers`` dropped.

    Mirrors :func:`checks.check_paper_dir_validity`'s truth-side directory
    enumeration (``papers/`` scandir, NOT ``list_papers``) but stays
    independent of it. A directory qualifies as a corrupt paper when it (a) has
    a valid paper id for a name, (b) contains a ``metadata.yaml``, and (c) is
    absent from ``loaded_ids`` — i.e. ``list_papers`` skipped it because the
    YAML was unparseable, non-UTF-8, or empty. Such a paper is invisible to
    every metadata-keyed path, so the graph re-surfaces it as a ``corrupt``
    node instead of letting it vanish (P0 / OQ1).

    Returns ids sorted ascending so the output order is deterministic.
    """
    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return []

    out: list[str] = []
    for entry in os.scandir(papers_dir):
        if not entry.is_dir():
            continue
        if not is_valid_id(entry.name):
            continue
        if entry.name in loaded_ids:
            continue
        if (Path(entry.path) / "metadata.yaml").is_file():
            out.append(entry.name)
    return sorted(out)


# ---------------------------------------------------------------------------
# Relation edges (paper <-> paper)
# ---------------------------------------------------------------------------


def _extract_relation_edges(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Paper<->paper relation edges from the forward relation fields only.

    Iterates :data:`FORWARD_REF_FIELDS` (``related`` / ``extends`` /
    ``contradicts``) so each relation is drawn once — reverse fields
    (``extended-by`` / ``contradicted-by``) are skipped to avoid double edges
    (D6). ``related`` is undirected; a directional field draws A->B.

    Multi-edge: the same node pair under two relation types yields two edges
    (one per type). Symmetric ``related`` is deduped on a sorted-tuple key so
    A.related:[B] and B.related:[A] collapse to one undirected edge; directed
    edges are not collapsed (A->B and B->A are genuinely different arrows).

    The endpoint id is taken verbatim from the field — a dangling / cross-vault
    target is still emitted as an edge here (its validity is stamped later by
    :func:`_invalid_markers`), so a broken relation never silently drops.
    """
    out: list[dict[str, Any]] = []
    seen_related: set[tuple[str, str]] = set()
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        pid = str(pid)
        for field in FORWARD_REF_FIELDS:
            directed = field in _DIRECTED_FORWARD_FIELDS
            for ref in _as_str_list(p.get(field)):
                if not directed:
                    key = tuple(sorted((pid, ref)))
                    if key in seen_related:
                        continue
                    seen_related.add(key)
                out.append(
                    {
                        "source": pid,
                        "target": ref,
                        "type": field,
                        "directed": directed,
                        "weight": 1,
                        "status": "ok",
                    }
                )
    return out


# ---------------------------------------------------------------------------
# Membership edges (drill-down layer)
# ---------------------------------------------------------------------------


def _project_paper_edges(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """project->paper membership edges, one per (project, paper) pair."""
    out: list[dict[str, Any]] = []
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        pid = str(pid)
        for project in _as_str_list(p.get("projects")):
            out.append(
                {
                    "source": project,
                    "target": pid,
                    "type": "projects",
                    "directed": True,
                    "weight": 1,
                    "status": "ok",
                }
            )
    return out


def _paper_code_edges(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """paper->code membership edges, one per (paper, code-clone) pair."""
    out: list[dict[str, Any]] = []
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        pid = str(pid)
        for name in _as_str_list(p.get("code-clones")):
            out.append(
                {
                    "source": pid,
                    "target": name,
                    "type": "code-clones",
                    "directed": True,
                    "weight": 1,
                    "status": "ok",
                }
            )
    return out


# ---------------------------------------------------------------------------
# Aggregate project--project edges (shared-paper weight)
# ---------------------------------------------------------------------------


def _aggregate_project_edges(
    papers: list[dict[str, Any]], project_names: list[str]
) -> list[dict[str, Any]]:
    """One undirected edge per project pair that shares >= 1 paper (D5 / A5).

    Edge weight is the number of papers whose ``projects`` list contains BOTH
    project names. Topics never produce a project edge (only the ``projects``
    field is consulted). Pairs with zero shared papers emit no edge.
    """
    shared: dict[tuple[str, str], int] = {}
    known = set(project_names)
    for p in papers:
        members = sorted({x for x in _as_str_list(p.get("projects")) if x in known})
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                shared[(members[i], members[j])] = (
                    shared.get((members[i], members[j]), 0) + 1
                )

    out: list[dict[str, Any]] = []
    for (a, b), weight in sorted(shared.items()):
        out.append(
            {
                "source": a,
                "target": b,
                "type": "shared-papers",
                "directed": False,
                "weight": weight,
                "status": "ok",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Invalid-marker re-derivation (the four conditions, from primitives)
# ---------------------------------------------------------------------------


def _load_registered_taxonomy(vault: Path) -> dict[str, list[str]] | None:
    """Parse ``TAXONOMY.md`` into its registered-value map, or ``None``.

    Returns ``None`` when the file is missing or unreadable — the caller then
    skips taxonomy invalid-marking rather than flagging every value as
    unregistered (a missing TAXONOMY.md is owned by ``check_taxonomy_drift``,
    not by the graph layer).
    """
    taxonomy_file = vault / "TAXONOMY.md"
    if not taxonomy_file.is_file():
        return None
    try:
        return parse_taxonomy(taxonomy_file.read_text(encoding="utf-8"))
    except OSError:
        return None


def _on_disk_repo_names(vault: Path) -> set[str]:
    """Repo names under ``codes/`` that have a ``repo-meta.yaml`` on disk.

    A ``code-clones`` entry naming a repo absent from this set is dangling
    (invariant #12). This is the only access to the codes dir — the stat for
    dangling detection — and never opens ``repo-meta.yaml`` for labels (OQ3).
    """
    codes_dir = vault / CODES_DIRNAME
    if not codes_dir.is_dir():
        return set()
    out: set[str] = set()
    for entry in os.scandir(codes_dir):
        if entry.is_dir() and (Path(entry.path) / REPO_META_FILENAME).is_file():
            out.add(entry.name)
    return out


def _config_project_names(vault: Path) -> set[str] | None:
    """Project names registered in ``lit-config.yaml``, or ``None``.

    Returns ``None`` when the config is unparseable — the caller then skips
    deleted-project invalid-marking (an unreadable config is owned by
    ``check_config_readable``, not flagged as a broken link per project here).
    """
    try:
        config = load_config(vault)
    except ConfigError:
        return None
    return set(config.projects)


def _invalid_markers(
    vault: Path, papers: list[dict[str, Any]]
) -> dict[str, set]:
    """Re-derive the four invalid conditions into lookup sets.

    Reuses the underlying primitives the health checks use (RELATION_PAIRS,
    parse_taxonomy + USER_DICTS, the config projects map, the codes dir +
    repo-meta filename) — never parses ``run_all_checks`` output (OQ2). The
    returned sets are consumed by :func:`build_graph` to stamp
    ``status:"invalid"`` onto matching nodes / edges.

    Keys:

    * ``"invalid_code_names"`` — repo names listed in some paper's
      ``code-clones`` with no ``codes/<name>/repo-meta.yaml`` on disk
      (dangling code-clone).
    * ``"invalid_project_names"`` — project names a paper links to that are
      absent from the lit-config projects map (deleted-project link).
    * ``"invalid_taxonomy_nodes"`` — paper ids carrying a topics/methods/data/
      projects value absent from ``TAXONOMY.md`` (unregistered taxonomy).
    * ``"invalid_relation_edges"`` — ``(source, target, type)`` triples whose
      forward edge has no matching reverse field on the endpoint, including
      the endpoint being a non-existent / other-vault id (broken pairing /
      missing endpoint, OQ4).
    """
    by_id = {str(p.get("id")): p for p in papers if p.get("id")}

    # 1. dangling code-clones.
    disk_repos = _on_disk_repo_names(vault)
    invalid_code_names: set[str] = set()
    for p in papers:
        for name in _as_str_list(p.get("code-clones")):
            if name and name not in disk_repos:
                invalid_code_names.add(name)

    # 2. project link to a name absent from the config projects map.
    config_projects = _config_project_names(vault)
    invalid_project_names: set[str] = set()
    if config_projects is not None:
        for p in papers:
            for project in _as_str_list(p.get("projects")):
                if project not in config_projects:
                    invalid_project_names.add(project)

    # 3. unregistered taxonomy value on a paper.
    registered = _load_registered_taxonomy(vault)
    invalid_taxonomy_nodes: set[str] = set()
    if registered is not None:
        for p in papers:
            pid = p.get("id")
            if not pid:
                continue
            for dict_name in USER_DICTS:
                allowed = registered.get(dict_name, [])
                for value in _as_str_list(p.get(dict_name)):
                    if value not in allowed:
                        invalid_taxonomy_nodes.add(str(pid))

    # 4. broken relation pairing (forward edge whose endpoint lacks the
    #    matching reverse field, or whose endpoint does not exist here).
    invalid_relation_edges: set[tuple[str, str, str]] = set()
    for pid, paper in by_id.items():
        for field in FORWARD_REF_FIELDS:
            reverse = RELATION_PAIRS[field]
            for ref in _as_str_list(paper.get(field)):
                other = by_id.get(ref)
                if other is None:
                    # Missing endpoint (deleted / cross-vault id): broken
                    # pairing, never a crash (OQ4).
                    invalid_relation_edges.add((pid, ref, field))
                    continue
                back = set(_as_str_list(other.get(reverse)))
                if pid not in back:
                    invalid_relation_edges.add((pid, ref, field))

    return {
        "invalid_code_names": invalid_code_names,
        "invalid_project_names": invalid_project_names,
        "invalid_taxonomy_nodes": invalid_taxonomy_nodes,
        "invalid_relation_edges": invalid_relation_edges,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_graph(vault: Path) -> dict[str, Any]:
    """Build the layered knowledge-graph JSON for a vault.

    Scans the full metadata set (this is an explicit Tier-2 read, invariant #15
    permits per-paper metadata here), re-surfaces corrupt papers, and returns
    the aggregate + per-project drill-down structure described in the module
    docstring. Pure read — never writes the vault.

    Returns a dict shaped as::

        {
          "summary": {papers, projects, codes, corrupt, invalid_edges},
          "aggregate": {"nodes": [...], "edges": [...]},
          "drilldown": {"<project>": {"nodes": [...], "edges": [...]}, ...},
        }

    A paper<->paper relation edge is render-duplicated into every shared-project
    drill-down bucket whose project both endpoints belong to (so the relation
    stays visible in each subgraph it spans). Node degree / sizing is computed
    once on the deduped relation list, so per-bucket repetition does not inflate
    sizes — but a consumer aggregating edges ACROSS buckets must dedup on
    ``(source, target, type)`` to avoid counting the same relation twice.
    """
    # A top-level non-mapping metadata.yaml (bare list / bare scalar) is truthy
    # so list_papers' ``if not metadata`` guard lets it through as a non-dict
    # element. Treat such a paper as "not loaded": dropping it here keeps its id
    # out of ``loaded_ids``, so ``_enumerate_corrupt_ids`` (disk_ids - loaded_ids)
    # re-surfaces it as a ``corrupt`` node instead of crashing the build on the
    # first ``.get`` call (invariant #14 P0 / A3, OQ1).
    papers = [p for p in list_papers(vault) if isinstance(p, dict)]
    loaded_ids = {str(p.get("id")) for p in papers if p.get("id")}
    corrupt_ids = _enumerate_corrupt_ids(vault, loaded_ids)

    markers = _invalid_markers(vault, papers)

    # --- collect entity sets --------------------------------------------
    # Projects seen on any paper PLUS those registered in the config, so a
    # registered-but-empty project still shows as a node. Project nodes are
    # always real projects (never a synthetic corrupt pseudo-project, OQ1).
    project_names: set[str] = set()
    for p in papers:
        for project in _as_str_list(p.get("projects")):
            project_names.add(project)
    config_projects = _config_project_names(vault)
    if config_projects is not None:
        project_names |= config_projects
    project_list = sorted(project_names)

    # Per-project + per-code membership counts for node sizing.
    project_paper_count: dict[str, int] = dict.fromkeys(project_list, 0)
    project_code_count: dict[str, int] = dict.fromkeys(project_list, 0)
    code_ref_count: dict[str, int] = {}
    for p in papers:
        paper_projects = _as_str_list(p.get("projects"))
        paper_codes = _as_str_list(p.get("code-clones"))
        for project in paper_projects:
            project_paper_count[project] = project_paper_count.get(project, 0) + 1
            project_code_count[project] = (
                project_code_count.get(project, 0) + len(paper_codes)
            )
        for name in paper_codes:
            code_ref_count[name] = code_ref_count.get(name, 0) + 1

    # --- edges -----------------------------------------------------------
    relation_edges = _extract_relation_edges(papers)
    project_edges = _project_paper_edges(papers)
    code_edges = _paper_code_edges(papers)
    aggregate_edges = _aggregate_project_edges(papers, project_list)

    # Stamp invalid status onto relation edges (broken pairing / missing
    # endpoint) and membership edges (deleted-project / dangling code-clone).
    invalid_edge_count = 0
    for e in relation_edges:
        if (e["source"], e["target"], e["type"]) in markers["invalid_relation_edges"]:
            e["status"] = "invalid"
            invalid_edge_count += 1
    for e in project_edges:
        if e["source"] in markers["invalid_project_names"]:
            e["status"] = "invalid"
            invalid_edge_count += 1
    for e in code_edges:
        if e["target"] in markers["invalid_code_names"]:
            e["status"] = "invalid"
            invalid_edge_count += 1

    # --- paper degree (for paper-node sizing) ---------------------------
    degree: dict[str, int] = dict.fromkeys(loaded_ids, 0)
    for e in relation_edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1
    for e in project_edges:
        degree[e["target"]] = degree.get(e["target"], 0) + 1
    for e in code_edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1

    # --- node builders ---------------------------------------------------
    def _paper_node(p: dict[str, Any]) -> dict[str, Any]:
        pid = str(p.get("id"))
        projects = _as_str_list(p.get("projects"))
        status = "invalid" if pid in markers["invalid_taxonomy_nodes"] else "ok"
        return {
            "id": pid,
            "type": "paper",
            "label": str(p.get("title") or pid),
            "size": degree.get(pid, 0),
            "status": status,
            "group": projects[0] if projects else UNASSIGNED,
        }

    def _project_node(name: str) -> dict[str, Any]:
        size = project_paper_count.get(name, 0) + project_code_count.get(name, 0)
        return {
            "id": name,
            "type": "project",
            "label": name,
            "size": size,
            "status": "ok",
            "group": name,
        }

    def _code_node(name: str, group: str) -> dict[str, Any]:
        status = "invalid" if name in markers["invalid_code_names"] else "ok"
        return {
            "id": name,
            "type": "code",
            "label": name,
            "size": code_ref_count.get(name, 0),
            "status": status,
            "group": group,
        }

    def _corrupt_node(pid: str) -> dict[str, Any]:
        return {
            "id": pid,
            "type": "corrupt",
            "label": pid,
            "size": 0,
            "status": "corrupt",
            "group": UNASSIGNED,
        }

    # --- aggregate layer (real projects only) ---------------------------
    aggregate_nodes = [_project_node(n) for n in project_list]

    # --- drill-down layer ------------------------------------------------
    # Each project bucket holds its project node, member paper nodes, the
    # codes those papers reference, and the membership + relation edges
    # internal to it. Papers in no project (and every corrupt paper) live in
    # the "(unassigned)" bucket so nothing disappears (OQ1).
    drilldown: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def _bucket(name: str) -> dict[str, list[dict[str, Any]]]:
        return drilldown.setdefault(name, {"nodes": [], "edges": []})

    papers_by_id = {str(p.get("id")): p for p in papers if p.get("id")}

    # Project buckets: project node + its papers + their codes.
    for name in project_list:
        b = _bucket(name)
        b["nodes"].append(_project_node(name))

    seen_paper_in_bucket: set[tuple[str, str]] = set()
    seen_code_in_bucket: set[tuple[str, str]] = set()
    for pid, p in papers_by_id.items():
        paper_projects = _as_str_list(p.get("projects"))
        target_buckets = paper_projects if paper_projects else [UNASSIGNED]
        for bucket_name in target_buckets:
            b = _bucket(bucket_name)
            if (bucket_name, pid) not in seen_paper_in_bucket:
                b["nodes"].append(_paper_node(p))
                seen_paper_in_bucket.add((bucket_name, pid))
            for name in _as_str_list(p.get("code-clones")):
                if (bucket_name, name) not in seen_code_in_bucket:
                    b["nodes"].append(_code_node(name, bucket_name))
                    seen_code_in_bucket.add((bucket_name, name))

    # Corrupt papers always show as corrupt nodes in the unassigned bucket.
    unassigned = _bucket(UNASSIGNED)
    for cid in corrupt_ids:
        unassigned["nodes"].append(_corrupt_node(cid))

    # Membership edges into their owning project bucket.
    for e in project_edges:
        _bucket(e["source"])["edges"].append(e)
    # Code edges follow their paper into every bucket the paper belongs to.
    for e in code_edges:
        src = e["source"]
        p = papers_by_id.get(src)
        paper_projects = _as_str_list(p.get("projects")) if p else []
        for bucket_name in (paper_projects if paper_projects else [UNASSIGNED]):
            _bucket(bucket_name)["edges"].append(e)
    # Relation edges: place in each bucket whose project both endpoints share;
    # if the endpoints share no project (or one is unassigned), it goes to the
    # unassigned bucket so the relation stays visible somewhere.
    for e in relation_edges:
        src_p = papers_by_id.get(e["source"])
        tgt_p = papers_by_id.get(e["target"])
        src_projects = set(_as_str_list(src_p.get("projects"))) if src_p else set()
        tgt_projects = set(_as_str_list(tgt_p.get("projects"))) if tgt_p else set()
        common = src_projects & tgt_projects
        for bucket_name in (sorted(common) if common else [UNASSIGNED]):
            _bucket(bucket_name)["edges"].append(e)

    # Ensure the unassigned bucket always exists (even on an all-projected
    # vault) so consumers can rely on the key.
    _bucket(UNASSIGNED)

    summary = {
        "papers": len(papers),
        "projects": len(project_list),
        "codes": len(code_ref_count),
        "corrupt": len(corrupt_ids),
        "invalid_edges": invalid_edge_count,
    }

    return {
        "summary": summary,
        "aggregate": {"nodes": aggregate_nodes, "edges": aggregate_edges},
        "drilldown": drilldown,
    }
