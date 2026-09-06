"""task-write-perf: the incremental write paths must equal a full rebuild.

Every converted op (rm / trash restore / taxonomy rename & rm / project
link & unlink / modify's member-upgrade case) now renders INDEX.json from
spliced INDEX projections and updates views/ through the single-paper
delta — this suite pins the two invariants that make that safe:

1. **Equivalence**: after the op, the derived artifacts (INDEX.json,
   views/ tree, REFERENCES.md) are identical to what a wholesale
   ``reconcile_derived`` rebuild of the same TRUTH produces.
2. **The scan is really gone / the fallback really lives** (reverse
   verification): with a fresh INDEX the op performs ZERO ``list_papers``
   scans; with a corrupted INDEX it scans and still produces the same
   correct result.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.commands.rm import discover_rm_impact, execute_rm
from litman.core.correctors import reconcile_derived
from litman.core.library import create_vault
from litman.core.project_link import (
    link_paper_to_project,
    unlink_paper_from_project,
)
from litman.core.ripple import _ripple_removals, _ripple_replacements
from litman.core.taxonomy import (
    add_taxonomy_values,
    remove_taxonomy_value,
    rename_taxonomy_value,
)
from litman.core.portable_link import is_portable_link

# Modules whose namespace holds a reachable ``list_papers`` reference on the
# write paths under test. ``litman.core.document`` covers every lazy
# ``from ... import list_papers`` executed at call time (papers_for_index,
# reconcile_derived); the rest are module-level aliases.
_SCAN_NAMESPACES = (
    "litman.core.document",
    "litman.core.project_refs",
    "litman.core.ripple",
    "litman.core.project_link",
    "litman.commands.modify",
)


@pytest.fixture
def count_scans(monkeypatch: pytest.MonkeyPatch):
    """Count every full-vault ``list_papers`` scan, wherever it is called from."""
    import importlib

    from litman.core import document

    counter = {"n": 0}
    real = document.list_papers

    def counting(vault: Path):
        counter["n"] += 1
        return real(vault)

    for name in _SCAN_NAMESPACES:
        module = importlib.import_module(name)
        if getattr(module, "list_papers", None) is not None:
            monkeypatch.setattr(module, "list_papers", counting)
    return counter


def _write_paper(
    vault: Path,
    paper_id: str,
    *,
    topics: list[str] | None = None,
    methods: list[str] | None = None,
    status: str = "inbox",
    projects: list[str] | None = None,
    relevance: dict[str, str] | None = None,
    related: list[str] | None = None,
) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": f"Title of {paper_id}",
        "authors": ["Family, Given"],
        "year": int(paper_id[:4]),
        "journal": "Test J.",
        "doi": f"10.1/{paper_id}",
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": projects or [],
        "topics": topics or [],
        "methods": methods or [],
        "data": [],
        "type": "research",
        "status": status,
        "read-date": None,
        "last-revisited": None,
        "related": related or [],
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    for proj, note in (relevance or {}).items():
        payload[f"relevance-{proj}"] = note
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """A vault exercising every derived artifact + two registered projects."""
    vault = create_vault(tmp_path)
    proj_dirs = {name: tmp_path / f"proj_{name}" for name in ("alpha", "beta")}
    for d in proj_dirs.values():
        d.mkdir()
    (vault / "lit-config.yaml").write_text(
        "library_name: literature_vault\n"
        "projects:\n"
        + "".join(f"  {n}: {d}\n" for n, d in proj_dirs.items()),
        encoding="utf-8",
    )
    # TAXONOMY registers the values the taxonomy tests ripple — through the
    # write API (TAXONOMY.md is lock-protected TRUTH; a bare write_text
    # would trip the read-only lock).
    add_taxonomy_values(vault, "topics", ["deep-learning", "peptides"])
    add_taxonomy_values(vault, "methods", ["docking"])
    _write_paper(
        vault,
        "2020_Alpha_One",
        topics=["deep-learning"],
        status="reading",
        projects=["alpha"],
        relevance={"alpha": "seed relevance note"},
        related=["2021_Beta_Two"],
    )
    _write_paper(
        vault,
        "2021_Beta_Two",
        topics=["deep-learning", "peptides"],
        methods=["docking"],
        projects=["alpha"],
        relevance={"alpha": "second member"},
        related=["2020_Alpha_One"],
    )
    _write_paper(vault, "2022_Gamma_Three", topics=["peptides"], projects=["beta"])
    _write_paper(vault, "2023_Delta_Four")
    notes = vault / "papers" / "2020_Alpha_One" / "notes.md"
    notes.write_text(
        "# Notes\n\nBuilds on [[2021_Beta_Two]] heavily.\n", encoding="utf-8"
    )
    unrelated = vault / "papers" / "2023_Delta_Four" / "notes.md"
    unrelated.write_text("# Notes\n\nNothing linked here.\n", encoding="utf-8")
    # Establish fresh derived state (INDEX + views + REFERENCES).
    reconcile_derived(vault)
    return vault, proj_dirs


def _normalized_index(vault: Path) -> dict[str, Any]:
    payload = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    payload.pop("generated_at", None)
    return payload


def _views_tree(vault: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    views = vault / "views"
    for p in sorted(views.rglob("*")):
        rel = str(p.relative_to(views))
        if is_portable_link(p):
            out[rel] = str(p.resolve().name)
        elif p.is_dir():
            out[rel] = "<dir>"
    return out


def _normalized_references(proj_dir: Path) -> str | None:
    ref = proj_dir / "litman_reflib" / "REFERENCES.md"
    if not ref.is_file():
        return None
    text = ref.read_text(encoding="utf-8")
    return re.sub(r"<!-- Last updated: .*? -->", "<ts>", text)


def _derived_state(vault: Path, proj_dirs: dict[str, Path]) -> dict[str, Any]:
    return {
        "index": _normalized_index(vault),
        "views": _views_tree(vault),
        "refs": {n: _normalized_references(d) for n, d in proj_dirs.items()},
    }


def _assert_equals_full_rebuild(
    vault: Path, proj_dirs: dict[str, Path]
) -> None:
    """The op's derived output must match a wholesale rebuild of TRUTH."""
    incremental = _derived_state(vault, proj_dirs)
    reconcile_derived(vault)  # papers=None → full scan + full views rebuild
    assert incremental == _derived_state(vault, proj_dirs)


def _corrupt_index(vault: Path) -> None:
    (vault / "INDEX.json").write_text("{ not json", encoding="utf-8")


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def test_rm_zero_scan_and_equivalent(seeded, count_scans) -> None:
    vault, proj_dirs = seeded
    plan = discover_rm_impact(vault, "2021_Beta_Two")
    execute_rm(plan, purge=False)
    assert count_scans["n"] == 0, "fresh INDEX → the delete must not scan"

    index = _normalized_index(vault)
    assert all(p["id"] != "2021_Beta_Two" for p in index["papers"])
    # Cascade + annotation really happened (the fast path is not a no-op).
    notes = (vault / "papers" / "2020_Alpha_One" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert "(deleted)" in notes
    refs = _normalized_references(proj_dirs["alpha"]) or ""
    assert "2021_Beta_Two" not in refs
    assert "seed relevance note" in refs, "member relevance must survive"
    _assert_equals_full_rebuild(vault, proj_dirs)


def test_rm_untouched_notes_not_rewritten(seeded) -> None:
    vault, _ = seeded
    unrelated = vault / "papers" / "2023_Delta_Four" / "notes.md"
    before = unrelated.stat().st_mtime_ns
    plan = discover_rm_impact(vault, "2021_Beta_Two")
    execute_rm(plan, purge=False)
    assert unrelated.stat().st_mtime_ns == before
    assert unrelated.read_text(encoding="utf-8").count("deleted") == 0


def test_rm_stale_index_falls_back_and_stays_correct(
    seeded, count_scans
) -> None:
    vault, proj_dirs = seeded
    _corrupt_index(vault)
    plan = discover_rm_impact(vault, "2021_Beta_Two")
    execute_rm(plan, purge=False)
    assert count_scans["n"] > 0, "corrupt INDEX must fall back to scanning"
    _assert_equals_full_rebuild(vault, proj_dirs)


# ---------------------------------------------------------------------------
# trash restore (through the CLI = the shared backend path)
# ---------------------------------------------------------------------------


def test_restore_zero_scan_and_equivalent(seeded, count_scans) -> None:
    vault, proj_dirs = seeded
    plan = discover_rm_impact(vault, "2021_Beta_Two")
    execute_rm(plan, purge=False)
    count_scans["n"] = 0

    runner = CliRunner()
    result = runner.invoke(
        cli, ["trash", "restore", "2021_Beta_Two", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert count_scans["n"] == 0, "fresh INDEX → the restore must not scan"

    index = _normalized_index(vault)
    assert any(p["id"] == "2021_Beta_Two" for p in index["papers"])
    notes = (vault / "papers" / "2020_Alpha_One" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert "(deleted)" not in notes
    _assert_equals_full_rebuild(vault, proj_dirs)


# ---------------------------------------------------------------------------
# taxonomy rename / rm (core backend shared by CLI + GUI, invariant #16)
# ---------------------------------------------------------------------------


def test_taxonomy_rename_zero_scan_and_equivalent(seeded, count_scans) -> None:
    vault, proj_dirs = seeded
    n_changed, referencing = rename_taxonomy_value(
        vault, "topics", "deep-learning", "geometric-dl"
    )
    assert n_changed == 2
    assert referencing == ["2020_Alpha_One", "2021_Beta_Two"]
    assert count_scans["n"] == 0

    assert (vault / "views" / "by-topic" / "geometric-dl").is_dir()
    assert not (vault / "views" / "by-topic" / "deep-learning").exists()
    _assert_equals_full_rebuild(vault, proj_dirs)


def test_taxonomy_rm_value_zero_scan_and_equivalent(seeded, count_scans) -> None:
    vault, proj_dirs = seeded
    n_changed, _ = remove_taxonomy_value(vault, "methods", "docking")
    assert n_changed == 1
    assert count_scans["n"] == 0
    assert not (vault / "views" / "by-method" / "docking").exists()
    _assert_equals_full_rebuild(vault, proj_dirs)


def test_taxonomy_rename_stale_index_falls_back(seeded, count_scans) -> None:
    vault, proj_dirs = seeded
    _corrupt_index(vault)
    n_changed, _ = rename_taxonomy_value(
        vault, "topics", "deep-learning", "geometric-dl"
    )
    assert n_changed == 2
    assert count_scans["n"] > 0
    _assert_equals_full_rebuild(vault, proj_dirs)


def test_ripple_refuses_papers_with_project_key_probes(seeded) -> None:
    """The stray-key probe reads relevance-<x> AND priority-<x> off every
    paper; an INDEX projection carries neither, so combining the two must stay
    a hard error rather than a silently incomplete cascade."""
    vault, _ = seeded
    with pytest.raises(ValueError, match="rename_project_keys"):
        _ripple_replacements(
            vault, "projects", {"alpha": "gamma"},
            rename_project_keys=True, papers=[],
        )
    with pytest.raises(ValueError, match="drop_project_keys"):
        _ripple_removals(
            vault, "projects", "alpha", drop_project_keys=True, papers=[]
        )


# ---------------------------------------------------------------------------
# project link / unlink
# ---------------------------------------------------------------------------


def test_link_zero_scan_and_equivalent(seeded, count_scans) -> None:
    vault, proj_dirs = seeded
    registry = {n: str(d) for n, d in proj_dirs.items()}
    link_paper_to_project(
        vault, "2023_Delta_Four", "beta", registry, relevance="now relevant"
    )
    assert count_scans["n"] == 0

    assert (vault / "views" / "by-project" / "beta" / "2023_Delta_Four").exists()
    refs = _normalized_references(proj_dirs["beta"]) or ""
    assert "2023_Delta_Four" in refs and "now relevant" in refs
    _assert_equals_full_rebuild(vault, proj_dirs)


def test_unlink_zero_scan_and_equivalent(seeded, count_scans) -> None:
    vault, proj_dirs = seeded
    registry = {n: str(d) for n, d in proj_dirs.items()}
    unlink_paper_from_project(vault, "2021_Beta_Two", "alpha", registry)
    assert count_scans["n"] == 0

    assert not (
        vault / "views" / "by-project" / "alpha" / "2021_Beta_Two"
    ).exists()
    refs = _normalized_references(proj_dirs["alpha"]) or ""
    assert "2021_Beta_Two" not in refs
    assert "2020_Alpha_One" in refs, "the other member must survive"
    _assert_equals_full_rebuild(vault, proj_dirs)


def test_link_stale_index_falls_back(seeded, count_scans) -> None:
    vault, proj_dirs = seeded
    registry = {n: str(d) for n, d in proj_dirs.items()}
    _corrupt_index(vault)
    link_paper_to_project(vault, "2023_Delta_Four", "beta", registry)
    assert count_scans["n"] > 0
    _assert_equals_full_rebuild(vault, proj_dirs)


# ---------------------------------------------------------------------------
# modify: refs-field edit on a project member (the former worst case)
# ---------------------------------------------------------------------------


def test_modify_title_on_member_zero_scan_and_refreshes_refs(
    seeded, count_scans
) -> None:
    vault, proj_dirs = seeded
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "modify", "2020_Alpha_One",
            "--set", "title=A Sharper Title",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert count_scans["n"] == 0, (
        "member title edit must upgrade members via INDEX, not scan the vault"
    )
    refs = _normalized_references(proj_dirs["alpha"]) or ""
    assert "A Sharper Title" in refs
    assert "second member" in refs, "other member's relevance intact"
    _assert_equals_full_rebuild(vault, proj_dirs)
