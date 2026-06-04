"""Knowledge-graph serialization layer for ``lit graph`` (M35).

Reconstructs the *emergent* knowledge graph (identity.md) from vault metadata
into a flat, paper-centric JSON snapshot the frontend renders. This module is
**read-only** and produces nothing on disk — the caller (``commands/graph.py``)
injects the returned dict into a static page.

Model (rev 2 — paper-centric):

* **Nodes are papers, full stop.** A library is a *network of papers*; projects,
  topics, methods, data, and code-clones are not nodes — they are *dimensions*
  the frontend colours and clusters papers by, or focuses into. There is no
  project hub node, no code node. A paper that belongs to several projects is a
  *pivot*: under the "projects" lens it gets pulled between clusters, which is
  exactly the structure the user wants to see.
* **Edges are paper<->paper relations only** (``related`` / ``extends`` /
  ``contradicts``). Membership ("paper X is in project A") is carried on the
  node as ``dims.projects = [...]``, not as an edge, so the default view is the
  pure relation network rather than a hairball of membership spokes.
* Every node carries its membership across *all* dimensions (``dims``), so the
  frontend can recolour / recluster / focus by any dimension client-side with
  no second fetch.

Dimensions (the closed set — invariant: user-added free-text fields are NOT
graphable, only the 4 TAXONOMY keys + code-clones):

    projects, topics, methods, data, codes   (codes <- the ``code-clones`` field)

Design contracts (from the M35 spec §2.2 + proposal D4--D7):

* Papers are enumerated via :func:`document.list_papers` — no second scan loop.
* A corrupt-metadata paper that ``list_papers`` silently drops is re-enumerated
  here and surfaces as a ``type:"corrupt"`` node + ``summary["corrupt"]`` count.
  It must never vanish from the default view (invariant #14, P0 / OQ1).
* Relation edge types come from :data:`relations.RELATION_PAIRS` via
  :data:`relations.FORWARD_REF_FIELDS` (single source, D7) — adding a new
  relation pair to that map auto-flows here with no change to this module.
* ``related`` is undirected with symmetric dedup; ``extends`` / ``contradicts``
  are directed A->B; the same node pair with two relation types yields two
  edges (multi-edge, D6).
* Drift conditions (dangling code-clone, deleted-project link, unregistered
  taxonomy value, broken relation pairing) are re-derived here from the same
  *primitives* the health checks use (RELATION_PAIRS, parse_taxonomy + the
  user-dict set, the config projects map, the codes dir / repo-meta filename),
  not by string-parsing ``run_all_checks`` output (OQ2). A relation pointing at
  a non-existent / other-vault id is a missing-endpoint broken pairing — the
  edge is marked invalid and its source paper gains an ``invalid`` status so it
  still surfaces even though the dangling endpoint has no node to draw to (OQ4).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from litman.core.code import CODES_DIRNAME, REPO_META_FILENAME
from litman.core.coerce import as_str_list as _as_str_list
from litman.core.config import load_config
from litman.core.document import list_papers
from litman.core.id import is_valid_id
from litman.core.relations import FORWARD_REF_FIELDS, RELATION_PAIRS
from litman.core.taxonomy import parse_taxonomy
from litman.exceptions import ConfigError

# The closed set of colour/cluster/focus dimensions and the metadata field each
# reads. Only the 4 TAXONOMY keys + code-clones are graphable; arbitrary
# user-added fields are deliberately excluded (they have no controlled
# vocabulary, so they cannot anchor a clean cluster or a legend).
DIMENSIONS: tuple[str, ...] = ("projects", "topics", "methods", "data", "codes")
_DIM_FIELD: dict[str, str] = {
    "projects": "projects",
    "topics": "topics",
    "methods": "methods",
    "data": "data",
    "codes": "code-clones",
}

# Relation fields that draw a *directed* A->B arrow. ``related`` (the remaining
# forward field) is undirected. Derived from RELATION_PAIRS so a future
# self-paired (symmetric) field is undirected and any new directional pair is
# directed automatically (D7) — never a hard-coded relation name list.
_DIRECTED_FORWARD_FIELDS: tuple[str, ...] = tuple(
    f for f in FORWARD_REF_FIELDS if RELATION_PAIRS.get(f) != f
)

# Cap on how many authors travel into the render payload. The detail card shows
# "First Author et al." past a few, so shipping a 50-author list would only
# bloat the JSON; ``n_authors`` keeps the true count for the "et al." cue.
_MAX_AUTHORS = 12


def _coerce_year(value: Any) -> int | None:
    """Best-effort int year (schema says int|null, but tolerate a digit string)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _node_meta(p: dict[str, Any]) -> dict[str, Any]:
    """Cheap bibliographic projection for the read-only detail card.

    Only small scalar / short-list fields the card displays — never abstract,
    notes, or PDF text (the GUI is offline & self-contained; the full record is
    ``lit show <id>``). Corrupt papers pass ``{}`` and get the all-empty shape.
    ``read_status`` is the paper's triage ``status`` field (inbox / skim /
    deep-read / dropped), kept distinct from the node's graph ``status``
    (ok / invalid / corrupt).
    """
    authors = _as_str_list(p.get("authors"))
    return {
        "year": _coerce_year(p.get("year")),
        "authors": authors[:_MAX_AUTHORS],
        "n_authors": len(authors),
        "journal": str(p.get("journal") or ""),
        "doi": str(p.get("doi") or ""),
        "type": str(p.get("type") or ""),
        "priority": str(p.get("priority") or ""),
        "read_status": str(p.get("status") or ""),
    }


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
    :func:`_invalid_relation_edges`), so a broken relation never silently drops.
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


def _invalid_relation_edges(
    papers: list[dict[str, Any]],
) -> set[tuple[str, str, str]]:
    """``(source, target, type)`` triples whose forward edge is broken.

    Broken = the endpoint lacks the matching reverse field, OR the endpoint is
    a non-existent / other-vault id (missing endpoint). Re-derived from
    RELATION_PAIRS (OQ2), never a crash on a dangling id (OQ4).
    """
    by_id = {str(p.get("id")): p for p in papers if p.get("id")}
    out: set[tuple[str, str, str]] = set()
    for pid, paper in by_id.items():
        for field in FORWARD_REF_FIELDS:
            reverse = RELATION_PAIRS[field]
            for ref in _as_str_list(paper.get(field)):
                other = by_id.get(ref)
                if other is None:
                    out.add((pid, ref, field))
                    continue
                back = set(_as_str_list(other.get(reverse)))
                if pid not in back:
                    out.add((pid, ref, field))
    return out


# ---------------------------------------------------------------------------
# Dimension drift (per-value invalid sets, from primitives)
# ---------------------------------------------------------------------------


def _load_registered_taxonomy(vault: Path) -> dict[str, list[str]] | None:
    """Parse ``TAXONOMY.md`` into its registered-value map, or ``None``.

    Returns ``None`` when the file is missing or unreadable — the caller then
    skips taxonomy drift-marking rather than flagging every value as
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
    deleted-project drift-marking (an unreadable config is owned by
    ``check_config_readable``, not flagged per project here).
    """
    try:
        config = load_config(vault)
    except ConfigError:
        return None
    return set(config.projects)


def _dimension_drift(
    vault: Path, dim_values: dict[str, set[str]]
) -> dict[str, set[str]]:
    """Per-dimension set of *invalid* values (drift), keyed by dimension name.

    * ``projects`` — values absent from the lit-config projects map
      (deleted-project link). Empty when the config is unreadable (owned by
      ``check_config_readable``).
    * ``codes`` — repo names with no ``codes/<name>/repo-meta.yaml`` on disk
      (dangling code-clone).
    * ``topics`` / ``methods`` / ``data`` — values absent from ``TAXONOMY.md``
      (unregistered taxonomy). Empty when TAXONOMY.md is unreadable.

    Re-uses the health-check primitives (config map, codes dir, parse_taxonomy)
    rather than parsing ``run_all_checks`` output (OQ2).
    """
    invalid: dict[str, set[str]] = {d: set() for d in DIMENSIONS}

    config_projects = _config_project_names(vault)
    if config_projects is not None:
        invalid["projects"] = {
            v for v in dim_values["projects"] if v not in config_projects
        }

    disk_repos = _on_disk_repo_names(vault)
    invalid["codes"] = {v for v in dim_values["codes"] if v not in disk_repos}

    registered = _load_registered_taxonomy(vault)
    if registered is not None:
        for dim in ("topics", "methods", "data"):
            allowed = set(registered.get(dim, []))
            invalid[dim] = {v for v in dim_values[dim] if v not in allowed}

    return invalid


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_graph(vault: Path) -> dict[str, Any]:
    """Build the paper-centric knowledge-graph JSON for a vault.

    Scans the full metadata set (an explicit Tier-2 read, invariant #15 permits
    per-paper metadata here), re-surfaces corrupt papers, and returns the flat
    node/edge/dimension structure described in the module docstring. Pure read —
    never writes the vault.

    Returns a dict shaped as::

        {
          "summary": {papers, corrupt, invalid_edges, dimensions: {<dim>: count}},
          "nodes": [
            {id, label, type, status, degree, dims: {<dim>: [values...]},
             meta: {year, authors, n_authors, journal, doi, type, priority,
                    read_status}}, ...
          ],
          "edges": [{source, target, type, directed, weight, status}, ...],
          "dimensions": {<dim>: {values: [...], invalid: [...]}, ...},
        }

    ``nodes`` holds every loaded paper (``type:"paper"``) plus every corrupt
    paper (``type:"corrupt"``, empty ``dims``). ``edges`` holds only paper<->
    paper relation edges; membership is encoded in each node's ``dims`` instead.
    ``dimensions[d].values`` is the sorted distinct set of values present on any
    paper for dimension ``d``; ``dimensions[d].invalid`` flags the drift subset.
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

    # --- distinct dimension values + their drift subset ------------------
    dim_values: dict[str, set[str]] = {d: set() for d in DIMENSIONS}
    for p in papers:
        for dim in DIMENSIONS:
            for v in _as_str_list(p.get(_DIM_FIELD[dim])):
                if v:
                    dim_values[dim].add(v)
    dim_invalid = _dimension_drift(vault, dim_values)

    # --- relation edges + their validity --------------------------------
    relation_edges = _extract_relation_edges(papers)
    broken = _invalid_relation_edges(papers)
    invalid_edge_count = 0
    invalid_rel_sources: set[str] = set()
    for e in relation_edges:
        if (e["source"], e["target"], e["type"]) in broken:
            e["status"] = "invalid"
            invalid_edge_count += 1
            invalid_rel_sources.add(e["source"])

    # --- paper degree (relation connectivity, for node sizing) ----------
    degree: dict[str, int] = dict.fromkeys(loaded_ids, 0)
    for e in relation_edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        if e["target"] in degree:  # only loaded endpoints count
            degree[e["target"]] = degree[e["target"]] + 1

    # --- nodes -----------------------------------------------------------
    nodes: list[dict[str, Any]] = []
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        pid = str(pid)
        dims = {
            dim: sorted({v for v in _as_str_list(p.get(_DIM_FIELD[dim])) if v})
            for dim in DIMENSIONS
        }
        # Drift: a value of any dimension is unregistered/dangling, OR the paper
        # is the source of a broken relation pairing (so it surfaces even though
        # the dangling endpoint has no node to draw the red edge to).
        has_drift_value = any(
            any(v in dim_invalid[dim] for v in dims[dim]) for dim in DIMENSIONS
        )
        invalid = has_drift_value or pid in invalid_rel_sources
        nodes.append(
            {
                "id": pid,
                "label": str(p.get("title") or pid),
                "type": "paper",
                "status": "invalid" if invalid else "ok",
                "degree": degree.get(pid, 0),
                "dims": dims,
                "meta": _node_meta(p),
            }
        )
    for cid in corrupt_ids:
        nodes.append(
            {
                "id": cid,
                "label": cid,
                "type": "corrupt",
                "status": "corrupt",
                "degree": 0,
                "dims": {dim: [] for dim in DIMENSIONS},
                "meta": _node_meta({}),
            }
        )

    dimensions = {
        dim: {
            "values": sorted(dim_values[dim]),
            "invalid": sorted(dim_invalid[dim]),
        }
        for dim in DIMENSIONS
    }
    summary = {
        "papers": len(loaded_ids),
        "corrupt": len(corrupt_ids),
        "invalid_edges": invalid_edge_count,
        "dimensions": {dim: len(dim_values[dim]) for dim in DIMENSIONS},
    }

    return {
        "summary": summary,
        "nodes": nodes,
        "edges": relation_edges,
        "dimensions": dimensions,
    }
