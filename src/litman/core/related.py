"""Knowledge-graph neighbour traversal for ``lit related`` (M33).

identity calls the emergent knowledge graph litman's "最大杠杆" yet, before
this module, no command walked it — an agent wanting "what else relates to
this paper" had to pull the whole library JSON and recompute shared keys by
hand. :func:`find_related` does that traversal once, deterministically, with
no LLM (invariant #5) and read-only (invariant #1).

Two neighbour kinds, merged by default (OQ4):

* **Explicit edges** (strong signal, author-asserted): the paper ids listed in
  the target's :data:`ALL_REF_FIELDS` (``related`` / ``extends`` /
  ``extended-by`` / ``contradicts`` / ``contradicted-by``). These are reliable
  and come FIRST.
* **Shared taxonomy** (weak signal, inferred): papers sharing at least one
  ``topics`` or ``methods`` key. Ranked by shared-key count descending and
  placed AFTER the edges. There is no hard threshold — a litman vault is a
  300–500 paper curation library with few keys per paper, so ``N>=2`` would
  starve the result; noise is filtered by the rank-and-truncate (top-K), not a
  cutoff. ``min_shared`` lets a caller tighten the floor.

Each neighbour reuses the INDEX projection (:func:`project_paper`,
invariant #10 — no second field set) plus a heterogeneous ``via`` annotation
so the agent can read *why* / *how strongly* a paper is a neighbour:

* edge neighbour → ``{..., "via": "edge", "edge": "extends"}``
* taxonomy neighbour → ``{..., "via": "taxonomy",
  "shared": ["topics:transformer", "methods:attention"]}``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from litman.core.document import find_paper, list_papers
from litman.core.relations import ALL_REF_FIELDS
from litman.core.views import project_paper


def _edge_neighbours(target_meta: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``[(neighbour_id, edge_field), ...]`` from the target's ref fields.

    Walks :data:`ALL_REF_FIELDS` in declaration order. A paper named in more
    than one field keeps the FIRST edge label it was found under (first wins),
    so a single neighbour appears once with one representative edge.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for field in ALL_REF_FIELDS:
        for neighbour_id in target_meta.get(field) or []:
            nid = str(neighbour_id)
            if nid in seen:
                continue
            seen.add(nid)
            out.append((nid, field))
    return out


def _shared_taxonomy(
    target_meta: dict[str, Any], other: dict[str, Any]
) -> list[str]:
    """Return the ``topics:``/``methods:``-prefixed keys shared by two papers.

    Each entry is prefixed with its axis so the consumer can tell which
    dimension drove the match (``topics:transformer`` vs ``methods:transformer``
    are distinct). Sorted for a stable order.
    """
    shared: list[str] = []
    for axis in ("topics", "methods"):
        target_values = set(target_meta.get(axis) or [])
        other_values = set(other.get(axis) or [])
        for value in sorted(target_values & other_values):
            shared.append(f"{axis}:{value}")
    return shared


def find_related(
    vault: Path,
    paper_id: str,
    *,
    by: str | None = None,
    min_shared: int = 1,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find neighbours of ``paper_id`` (explicit edges + shared taxonomy).

    Args:
        vault: the vault root.
        paper_id: the target paper (raises ``PaperNotFoundError`` via
            :func:`find_paper` when malformed / absent).
        by: ``"edges"`` or ``"taxonomy"`` narrows to a single neighbour kind;
            ``None`` (default) merges both.
        min_shared: minimum shared topic/method keys for a taxonomy candidate
            (default 1, i.e. any shared key). Ignored for edge neighbours.
        limit: top-K cap applied to the merged list (default 20). Edges come
            first, so a large edge set can fill the budget before taxonomy.

    Returns:
        Edge neighbours first (in :data:`ALL_REF_FIELDS` order), then taxonomy
        neighbours sorted by shared-key count descending, the whole list
        truncated to ``limit``. Each item is ``project_paper(p)`` plus a ``via``
        annotation (``edge`` key for edges, ``shared`` key for taxonomy). A
        paper already present as an edge neighbour is NOT repeated as a taxonomy
        neighbour. The target itself is never a neighbour.
    """
    target_meta = find_paper(vault, paper_id)
    all_papers = list_papers(vault)
    by_id = {str(p.get("id")): p for p in all_papers}

    results: list[dict[str, Any]] = []
    claimed: set[str] = {paper_id}

    if by in (None, "edges"):
        for neighbour_id, edge_field in _edge_neighbours(target_meta):
            if neighbour_id in claimed:
                continue
            neighbour = by_id.get(neighbour_id)
            if neighbour is None:
                # Dangling forward edge (target → gone). Skipped here; the edge
                # rides in the target's own metadata and `lit health-check`
                # owns the dangling-ref finding. `related` stays read-only.
                continue
            claimed.add(neighbour_id)
            results.append(
                {**project_paper(neighbour), "via": "edge", "edge": edge_field}
            )

    if by in (None, "taxonomy"):
        taxonomy_hits: list[tuple[int, dict[str, Any]]] = []
        for other in all_papers:
            other_id = str(other.get("id"))
            if other_id in claimed:
                continue
            shared = _shared_taxonomy(target_meta, other)
            if len(shared) < min_shared:
                continue
            taxonomy_hits.append(
                (
                    len(shared),
                    {**project_paper(other), "via": "taxonomy", "shared": shared},
                )
            )
        # Sort by shared-key count descending; ties keep id-ascending order
        # (list_papers returns id-asc and Python sort is stable).
        taxonomy_hits.sort(key=lambda item: item[0], reverse=True)
        results.extend(item for _, item in taxonomy_hits)

    return results[:limit]
