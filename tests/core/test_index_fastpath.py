"""The INDEX read fast path + incremental views (single-paper-edit perf).

Two contracts under test:

* ``load_index_papers`` — returns INDEX.json's projections ONLY when every
  freshness probe passes (file readable, schema current, id set == papers/
  listing); any doubt returns None so callers fall back to the full scan.
  INDEX must never act as a second source of truth.
* ``update_views_for_paper`` — incremental bucket maintenance must land the
  views/ tree in exactly the state a full ``rebuild_views`` would produce
  for the same paper list (equivalence by construction, pinned here by
  tree comparison), while never visiting buckets outside the edit.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from litman.core.correctors import reconcile_derived
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.views import (
    INDEX_PAPER_FIELDS,
    load_index_papers,
    project_paper,
    rebuild_views,
    update_views_for_paper,
    view_fields_snapshot,
)


def _seed_paper(vault: Path, paper_id: str, **fields: object) -> None:
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


def _fresh_vault(tmp_path: Path) -> Path:
    """A reconciled two-paper vault whose INDEX passes every probe."""
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2023_One_Alpha",
        year=2023, status="inbox", priority="B",
        topics=["peptide", "deep-learning"], methods=["transformer"],
        projects=["PepForge"], authors=["One, A."], title="Alpha",
    )
    _seed_paper(
        v, "2024_Two_Beta",
        year=2024, status="deep-read",
        topics=["peptide"], authors=["Two, B."], title="Beta",
    )
    reconcile_derived(v, project_refs=False)
    return v


# ---------------------------------------------------------------------------
# load_index_papers — freshness probes
# ---------------------------------------------------------------------------


def test_fresh_index_loads_as_projections(tmp_path: Path) -> None:
    v = _fresh_vault(tmp_path)
    papers = load_index_papers(v)
    assert papers is not None
    assert [p["id"] for p in papers] == ["2023_One_Alpha", "2024_Two_Beta"]
    assert all(set(p) == set(INDEX_PAPER_FIELDS) for p in papers)
    # Projection-equal to a full scan: the two sources are interchangeable
    # for every consumer of projection fields.
    scanned = [project_paper(p) for p in list_papers(v)]
    assert [project_paper(p) for p in papers] == scanned


def test_empty_reconciled_vault_loads_as_empty_list_not_none(
    tmp_path: Path,
) -> None:
    v = create_vault(tmp_path)
    reconcile_derived(v, project_refs=False)
    assert load_index_papers(v) == []


def test_missing_index_is_none(tmp_path: Path) -> None:
    v = _fresh_vault(tmp_path)
    (v / "INDEX.json").unlink()
    assert load_index_papers(v) is None


def test_corrupt_index_is_none(tmp_path: Path) -> None:
    v = _fresh_vault(tmp_path)
    (v / "INDEX.json").write_text("{not json", encoding="utf-8")
    assert load_index_papers(v) is None


def test_paper_dir_added_behind_indexs_back_is_none(tmp_path: Path) -> None:
    v = _fresh_vault(tmp_path)
    _seed_paper(v, "2025_Three_Gamma", title="Gamma", authors=["T."])
    assert load_index_papers(v) is None


def test_paper_dir_removed_behind_indexs_back_is_none(tmp_path: Path) -> None:
    v = _fresh_vault(tmp_path)
    shutil.rmtree(v / "papers" / "2024_Two_Beta")
    assert load_index_papers(v) is None


def test_older_schema_index_is_none(tmp_path: Path) -> None:
    """An INDEX written by an older litman (no ``authors`` key) must not be
    served — its projections would silently lack fields consumers filter on."""
    v = _fresh_vault(tmp_path)
    target = v / "INDEX.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    for entry in payload["papers"]:
        entry.pop("authors")
    target.write_text(json.dumps(payload), encoding="utf-8")
    assert load_index_papers(v) is None


def test_index_with_duplicate_entries_is_none(tmp_path: Path) -> None:
    v = _fresh_vault(tmp_path)
    target = v / "INDEX.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["papers"].append(dict(payload["papers"][0]))
    target.write_text(json.dumps(payload), encoding="utf-8")
    assert load_index_papers(v) is None


# ---------------------------------------------------------------------------
# update_views_for_paper — equivalence with a full rebuild
# ---------------------------------------------------------------------------


def _views_tree(vault: Path) -> dict[str, str]:
    """Flatten views/ into {relative_path: kind} for exact comparison."""
    out: dict[str, str] = {}
    root = vault / "views"
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = f"link:{os.readlink(p)}"
        elif p.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = "file"
    return out


def _edit_and_compare(
    tmp_path: Path, paper_id: str, mutate: dict[str, Any]
) -> None:
    """Apply ``mutate`` to one paper's view fields both ways and compare.

    Incremental (``update_views_for_paper`` on the reconciled tree) must
    produce the identical views/ tree as a wholesale ``rebuild_views`` over
    the post-edit paper list.
    """
    v = _fresh_vault(tmp_path)
    papers = list_papers(v)
    paper = next(p for p in papers if p["id"] == paper_id)

    old_fields = view_fields_snapshot(paper)
    paper.update(mutate)
    new_fields = view_fields_snapshot(paper)

    update_views_for_paper(v, paper_id, old_fields, new_fields)
    incremental = _views_tree(v)

    rebuild_views(v, papers)
    full = _views_tree(v)

    assert incremental == full


def test_status_move_matches_full_rebuild(tmp_path: Path) -> None:
    _edit_and_compare(tmp_path, "2023_One_Alpha", {"status": "deep-read"})


def test_status_unset_matches_full_rebuild(tmp_path: Path) -> None:
    # inbox bucket empties out and must disappear, like a rebuild's tree.
    _edit_and_compare(tmp_path, "2023_One_Alpha", {"status": None})


def test_new_topic_bucket_matches_full_rebuild(tmp_path: Path) -> None:
    _edit_and_compare(
        tmp_path, "2024_Two_Beta", {"topics": ["peptide", "benchmark"]}
    )


def test_leaving_shared_topic_bucket_keeps_it_for_the_other_paper(
    tmp_path: Path,
) -> None:
    # Both papers sit in by-topic/peptide; one leaves, bucket must survive.
    _edit_and_compare(tmp_path, "2024_Two_Beta", {"topics": []})


def test_leaving_sole_topic_bucket_removes_it(tmp_path: Path) -> None:
    _edit_and_compare(
        tmp_path, "2023_One_Alpha", {"topics": ["peptide"]}
    )  # drops deep-learning, whose bucket held only this paper


def test_project_membership_change_matches_full_rebuild(
    tmp_path: Path,
) -> None:
    _edit_and_compare(tmp_path, "2023_One_Alpha", {"projects": []})


def test_multi_field_edit_matches_full_rebuild(tmp_path: Path) -> None:
    _edit_and_compare(
        tmp_path,
        "2023_One_Alpha",
        {
            "status": "skim",
            "topics": ["benchmark"],
            "methods": ["transformer", "diffusion"],
        },
    )


def test_non_view_edit_touches_nothing(tmp_path: Path) -> None:
    v = _fresh_vault(tmp_path)
    before = _views_tree(v)
    paper = next(p for p in list_papers(v) if p["id"] == "2023_One_Alpha")
    old_fields = view_fields_snapshot(paper)
    paper["title"] = "Renamed"
    created = update_views_for_paper(
        v, "2023_One_Alpha", old_fields, view_fields_snapshot(paper)
    )
    assert _views_tree(v) == before
    assert all(n == 0 for n in created.values())


def test_incremental_maintains_but_never_repairs_foreign_damage(
    tmp_path: Path,
) -> None:
    """The trust boundary: a hand-vandalized bucket outside the edit stays
    damaged (incremental never visits it) until a full rebuild repairs it."""
    v = _fresh_vault(tmp_path)
    vandalized = v / "views" / "by-topic" / "peptide" / "2024_Two_Beta"
    vandalized.unlink()

    papers = list_papers(v)
    paper = next(p for p in papers if p["id"] == "2023_One_Alpha")
    old_fields = view_fields_snapshot(paper)
    paper["status"] = "skim"
    reconcile_derived(
        v,
        papers=papers,
        project_refs=False,
        views_delta=[
            ("2023_One_Alpha", old_fields, view_fields_snapshot(paper))
        ],
    )

    assert not vandalized.exists()  # untouched by the incremental path
    assert (v / "views" / "by-status" / "skim" / "2023_One_Alpha").is_symlink()

    reconcile_derived(v, project_refs=False)  # the repair path
    assert vandalized.is_symlink()


# ---------------------------------------------------------------------------
# 300-paper scale smoke (loose ratio, guards the O(vault) regression)
# ---------------------------------------------------------------------------


def test_300_paper_index_load_beats_full_scan(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    for i in range(300):
        _seed_paper(
            v, f"20{i % 25:02d}_Auth{i}_Paper{i}",
            year=2000 + (i % 25), status="inbox",
            topics=[f"topic-{i % 12}"], authors=[f"Auth{i}, X."],
            title=f"Synthetic paper {i}",
        )
    reconcile_derived(v, project_refs=False)

    def _best_of_two(fn) -> float:
        best = float("inf")
        for _ in range(2):
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        return best

    scan_t = _best_of_two(lambda: list_papers(v))
    fast_t = _best_of_two(lambda: load_index_papers(v))

    loaded = load_index_papers(v)
    assert loaded is not None and len(loaded) == 300
    assert [project_paper(p) for p in loaded] == [
        project_paper(p) for p in list_papers(v)
    ]
    # Loose bound: the JSON read must be several times faster than the YAML
    # scan (measured ~50x; 3x keeps slow CI honest without flaking).
    assert fast_t < scan_t / 3
