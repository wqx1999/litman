"""M30 Phase 4 — funnel write-command derived rebuild through one shared helper.

Covers three Phase-4 deliverables (spec §9 Phase 4 + verification tasks 1/2):

1. The shared :func:`reconcile_derived` helper couples INDEX + views so no
   command can rebuild one and forget the other.
2. ``lit add`` now indexes the new paper immediately (the pre-existing lag bug
   is fixed) without breaking its rollback semantics.
3. ``lit project rm`` / ``lit project rename`` cascade-clean the paired
   ``relevance-<project>`` field so ``check_relevance_orphan`` no longer fires
   from the normal command path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import checks, correctors
from litman.core.library import create_vault
from litman.core.views import load_index_ids

_yaml = YAML(typ="safe")
_FAKE_PDF_BYTES = b"%PDF-1.4\n% fake content for tests\n%%EOF\n"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


@pytest.fixture
def proj_dir(tmp_path: Path) -> Path:
    d = tmp_path / "projA"
    d.mkdir()
    return d


def _write_paper(vault: Path, paper_id: str, **extra: Any) -> None:
    """Write a minimal-but-complete metadata.yaml, allowing arbitrary keys.

    Unlike test_project's fixed-payload helper, ``extra`` keys are merged
    verbatim so a test can inject ``relevance-<project>`` annotations.
    """
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": paper_id,
        "authors": ["Doe, Jane"],
        "year": 2024,
        "journal": "Test J.",
        "doi": f"10.0/{paper_id}",
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": [],
        "topics": [],
        "methods": [],
        "data": [],
        "type": "research",
        "status": "inbox",
        "priority": "B",
        "related": [],
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    payload.update(extra)
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


def _meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


def _index_ids(vault: Path) -> set[str]:
    return load_index_ids(vault) or set()


def _by_status_ids(vault: Path, status: str) -> set[str]:
    bucket = vault / "views" / "by-status" / status
    if not bucket.is_dir():
        return set()
    return {c.name for c in bucket.iterdir()}


SAMPLE_MESSAGE: dict[str, Any] = {
    "title": ["A Funnel Test Paper"],
    "author": [{"family": "Smith", "given": "Ada"}],
    "published-print": {"date-parts": [[2024]]},
    "container-title": ["J. Testing"],
    "DOI": "10.1234/funnel.2024",
}


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(_FAKE_PDF_BYTES)
    return pdf


@pytest.fixture
def mock_crossref(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(doi: str, client=None) -> dict[str, Any]:
        return SAMPLE_MESSAGE

    monkeypatch.setattr("litman.commands.add.fetch_crossref", _fake)


# ---------------------------------------------------------------------------
# 1. The shared helper is the single coupled rebuild path
# ---------------------------------------------------------------------------


def test_reconcile_derived_writes_index_and_views(vault: Path) -> None:
    """reconcile_derived rewrites INDEX.json AND views/ from truth (coupled)."""
    _write_paper(vault, "2024_A", status="inbox")
    # Nothing derived yet.
    assert _index_ids(vault) == set()

    counts = correctors.reconcile_derived(vault, project_refs=False)
    assert counts["index"] == 1
    assert _index_ids(vault) == {"2024_A"}
    assert _by_status_ids(vault, "inbox") == {"2024_A"}


def test_reconcile_derived_accepts_preloaded_papers(vault: Path) -> None:
    """Passing papers= avoids a re-read and produces identical derived state."""
    _write_paper(vault, "2024_A")
    from litman.core.document import list_papers

    papers = list_papers(vault)
    correctors.reconcile_derived(vault, papers=papers, project_refs=False)
    assert _index_ids(vault) == {"2024_A"}


def test_regen_is_a_thin_wrapper_over_reconcile_derived(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --fix regen corrector funnels through the same shared path.

    Asserts A-class ``regen`` and write commands cannot diverge: ``regen``
    delegates to ``reconcile_derived`` (with the full project_refs=True set).
    """
    seen: dict[str, Any] = {}
    real = correctors.reconcile_derived

    def _spy(v: Path, **kwargs: Any) -> dict[str, int]:
        seen.update(kwargs)
        return real(v, **kwargs)

    monkeypatch.setattr(correctors, "reconcile_derived", _spy)
    _write_paper(vault, "2024_A")
    correctors.regen(vault)
    assert seen.get("project_refs") is True


def test_no_write_command_calls_write_index_without_rebuild_views() -> None:
    """Funnel guarantee: no write command imports a bare write_index/render_index
    without also routing the post-commit rebuild through reconcile_derived.

    The structural invariant of Phase 4 is that INDEX and views are rebuilt by
    one helper. We assert every write-command module that touches the INDEX
    (``render_index``/``write_index``) also imports ``reconcile_derived`` — so
    the views rebuild can never be silently dropped.

    ``litman.commands.project`` is intentionally absent: its rename / set-path /
    add / rm logic was hoisted into ``core.project_link`` (shared with the webUI,
    invariant #16), so the command module is now a thin delegator that no longer
    touches the INDEX. The funnel routing for those writes is guarded at its new
    home below.
    """
    import importlib

    for mod_name in (
        "litman.commands.add",
        "litman.commands.modify",
        "litman.commands.rename",
        "litman.commands.rm",
        "litman.commands.taxonomy",
        "litman.commands.trash",
    ):
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "reconcile_derived"), mod_name
        # The bare per-command derived-write calls must be gone (they now go
        # through the funnel). NEITHER rebuild_views NOR write_index may remain
        # a module attribute — a command importing either could rebuild one
        # derived artifact and forget the other, which is exactly the drift the
        # funnel exists to prevent (W2: asserting only rebuild_views left the
        # write_index back-door open). render_index stays allowed — it only
        # renders the INDEX text that is staged inside the crash-safe
        # transaction; the funnel still owns the on-disk INDEX + views rebuild.
        assert not hasattr(mod, "rebuild_views"), mod_name
        assert not hasattr(mod, "write_index"), mod_name


def test_core_write_modules_route_index_rebuild_through_funnel() -> None:
    """Funnel guarantee at the core layer: the core modules that took over the
    INDEX-rebuild responsibility from the thin command wrappers (``core.taxonomy``
    rename/rm, ``core.project_link`` rename/set-path/add/rm) must route every
    post-commit rebuild through ``reconcile_derived``.

    These modules import ``reconcile_derived`` LAZILY inside each function (to
    break a core import cycle), so a module-level ``hasattr`` check would not see
    it. We assert on the source text instead: any module that renders the INDEX
    (``render_index``) must also reference ``reconcile_derived`` — and must not
    reach for the bare ``write_index`` / ``rebuild_views`` back-doors.
    """
    import importlib
    import inspect

    for mod_name in ("litman.core.taxonomy", "litman.core.project_link"):
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(mod)
        if "render_index" not in src:
            continue
        assert "reconcile_derived" in src, mod_name
        assert "write_index" not in src, mod_name
        assert "rebuild_views" not in src, mod_name


# ---------------------------------------------------------------------------
# 2. lit add now indexes immediately (lag bug fixed) + atomicity preserved
# ---------------------------------------------------------------------------


def test_add_indexes_paper_immediately(
    vault: Path, fake_pdf: Path, mock_crossref: None
) -> None:
    """A freshly-added paper is in INDEX + views without a separate refresh."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--doi", "10.1234/funnel.2024",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    paper_id = "2024_Smith_Funnel-Test-Paper"
    assert (vault / "papers" / paper_id).is_dir()
    # The lag bug fix: INDEX + views reflect the paper immediately.
    assert paper_id in _index_ids(vault)
    assert paper_id in _by_status_ids(vault, "inbox")


def test_add_index_matches_list(
    vault: Path, fake_pdf: Path, mock_crossref: None
) -> None:
    """`lit list` and INDEX.json agree right after add (no refresh needed)."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["add", str(fake_pdf), "--doi", "10.1234/funnel.2024",
         "--library", str(vault)],
    )
    payload = json.loads((vault / "INDEX.json").read_text())
    assert payload["n_papers"] == 1
    assert payload["papers"][0]["id"] == "2024_Smith_Funnel-Test-Paper"


def test_add_rollback_leaves_index_clean(
    vault: Path, fake_pdf: Path, mock_crossref: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed add rolls back the paper dir AND never half-writes INDEX.

    The reconcile happens only after the atomic creation block commits, so a
    failure inside that block (here: notes.md write) must rmtree the half-built
    dir and leave INDEX untouched (empty).
    """
    # Pre-seed INDEX with the empty state so we can assert it is unchanged.
    correctors.reconcile_derived(vault, project_refs=False)
    assert _index_ids(vault) == set()

    import litman.commands.add as add_mod

    real_copy2 = add_mod.shutil.copy2

    def _boom(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        raise OSError("simulated disk failure mid-add")

    monkeypatch.setattr(add_mod.shutil, "copy2", _boom)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["add", str(fake_pdf), "--doi", "10.1234/funnel.2024",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    # Paper dir rolled back fully.
    assert not (vault / "papers" / "2024_Smith_Funnel-Test-Paper").exists()
    # INDEX not half-written: still empty, source PDF still present.
    assert _index_ids(vault) == set()
    assert fake_pdf.exists()
    monkeypatch.setattr(add_mod.shutil, "copy2", real_copy2)


# ---------------------------------------------------------------------------
# 3. relevance cascade in project rm / rename (verification task 2)
# ---------------------------------------------------------------------------


def _no_relevance_orphans(vault: Path) -> bool:
    from litman.core.document import list_papers

    papers = list_papers(vault)
    return checks.check_relevance_orphan(vault, papers) == []


def test_project_rm_drops_relevance_field(
    vault: Path, proj_dir: Path
) -> None:
    """project rm cascades: relevance-<name> dropped alongside projects membership."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(
        vault, "2024_A", projects=["pepforge"],
        **{"relevance-pepforge": "core method paper"},
    )
    # Sanity: the orphan check would fire if rm stripped projects but kept
    # relevance — confirm the field is present pre-rm.
    assert _meta(vault, "2024_A").get("relevance-pepforge") == "core method paper"

    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _meta(vault, "2024_A")
    assert meta["projects"] == []
    assert "relevance-pepforge" not in meta
    assert _no_relevance_orphans(vault)


def test_project_rename_remaps_relevance_field(
    vault: Path, proj_dir: Path
) -> None:
    """project rename carries relevance-<old> → relevance-<new>, value preserved."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(
        vault, "2024_A", projects=["pepforge"],
        **{"relevance-pepforge": "core method paper"},
    )

    result = runner.invoke(
        cli,
        ["project", "rename", "pepforge", "pepcodec", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _meta(vault, "2024_A")
    assert meta["projects"] == ["pepcodec"]
    assert "relevance-pepforge" not in meta
    assert meta["relevance-pepcodec"] == "core method paper"
    assert _no_relevance_orphans(vault)


def test_project_rename_remaps_stray_relevance_without_membership(
    vault: Path, proj_dir: Path
) -> None:
    """A stray relevance-<old> (no membership) is still remapped, never stranded."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    # Paper carries the relevance annotation but is NOT a member (hand-edit
    # orphan). Rename must still carry the key over so it does not strand.
    _write_paper(
        vault, "2024_A", projects=[],
        **{"relevance-pepforge": "leftover note"},
    )

    result = runner.invoke(
        cli,
        ["project", "rename", "pepforge", "pepcodec", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _meta(vault, "2024_A")
    assert "relevance-pepforge" not in meta
    assert meta["relevance-pepcodec"] == "leftover note"


def test_project_rm_drops_stray_relevance_without_membership(
    vault: Path, proj_dir: Path
) -> None:
    """project rm strips a stray relevance-<name> even with no membership (W1).

    Symmetric with ``test_project_rename_remaps_stray_relevance_without_membership``:
    a hand-edit orphan (relevance-<name> present, projects empty) must not be
    stranded by ``lit project rm`` — after rm, no ``relevance-<name>`` survives
    anywhere, matching the rename path.
    """
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    # Orphan: carries relevance but is NOT a member.
    _write_paper(
        vault, "2024_A", projects=[],
        **{"relevance-pepforge": "leftover note"},
    )

    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _meta(vault, "2024_A")
    assert "relevance-pepforge" not in meta
    assert _no_relevance_orphans(vault)


def _by_project_ids(vault: Path, project: str) -> set[str]:
    bucket = vault / "views" / "by-project" / project
    if not bucket.is_dir():
        return set()
    return {c.name for c in bucket.iterdir()}


def _no_views_drift(vault: Path) -> bool:
    from litman.core.document import list_papers

    return checks.check_views_vs_metadata(vault, list_papers(vault)) == []


def test_link_rebuilds_by_project_view(vault: Path, proj_dir: Path) -> None:
    """`lit link` propagates the membership to views/by-project/ (W3).

    Pre-fix, link wrote INDEX + the project-side litman_reflib but never
    rebuilt views/, so views/by-project/<name>/<id> was missing and
    check_views_vs_metadata flagged it on the normal command path.
    """
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=[])
    correctors.reconcile_derived(vault, project_refs=False)

    result = runner.invoke(
        cli,
        ["link", "2024_A", "--project", "pepforge", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert _by_project_ids(vault, "pepforge") == {"2024_A"}
    assert _no_views_drift(vault)


def test_unlink_clears_by_project_view(vault: Path, proj_dir: Path) -> None:
    """`lit unlink` drops the stale views/by-project/<name>/<id> symlink (W3)."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=[])
    correctors.reconcile_derived(vault, project_refs=False)
    runner.invoke(
        cli,
        ["link", "2024_A", "--project", "pepforge", "--library", str(vault)],
    )
    assert _by_project_ids(vault, "pepforge") == {"2024_A"}

    result = runner.invoke(
        cli,
        ["unlink", "2024_A", "--project", "pepforge", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert _by_project_ids(vault, "pepforge") == set()
    assert _no_views_drift(vault)


def test_taxonomy_rm_does_not_touch_relevance(vault: Path) -> None:
    """A taxonomy topic rm never strips a relevance-<topic> key (project-only).

    relevance is project-scoped; the generic ripple helpers must not delete a
    coincidentally-named ``relevance-<topic>`` for a topic/method/data dict.
    """
    runner = CliRunner()
    runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "diffusion", "--library", str(vault)],
    )
    _write_paper(
        vault, "2024_A", topics=["diffusion"],
        **{"relevance-diffusion": "should survive"},
    )
    result = runner.invoke(
        cli,
        ["taxonomy", "rm", "topics", "diffusion", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    meta = _meta(vault, "2024_A")
    assert meta["topics"] == []
    # The relevance-diffusion key is NOT a project relevance annotation; the
    # taxonomy path leaves it untouched.
    assert meta["relevance-diffusion"] == "should survive"


# ---------------------------------------------------------------------------
# Project-side derived rebuild on write commands (review F14/F15/F16)
# ---------------------------------------------------------------------------


def _reflib(proj_dir: Path) -> Path:
    return proj_dir / "litman_reflib"


def test_modify_add_project_tag_builds_project_side(
    vault: Path, proj_dir: Path
) -> None:
    """F15: `lit modify --add-tag projects=X` writes member TRUTH AND builds the
    project's litman_reflib/<id> symlink + REFERENCES.md (pre-fix it changed
    only the paper's metadata, leaving the project dir stale)."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=[])
    correctors.reconcile_derived(vault, project_refs=False)

    result = runner.invoke(
        cli,
        ["modify", "2024_A", "--add-tag", "projects=pepforge",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert (_reflib(proj_dir) / "2024_A").is_symlink()
    refs = (_reflib(proj_dir) / "REFERENCES.md").read_text(encoding="utf-8")
    assert "2024_A" in refs


def test_modify_rm_project_tag_clears_project_side(
    vault: Path, proj_dir: Path
) -> None:
    """F15 (symmetric): `--rm-tag projects=X` drops the symlink too."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=[])
    runner.invoke(
        cli,
        ["link", "2024_A", "--project", "pepforge", "--library", str(vault)],
    )
    assert (_reflib(proj_dir) / "2024_A").is_symlink()

    result = runner.invoke(
        cli,
        ["modify", "2024_A", "--rm-tag", "projects=pepforge",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert not (_reflib(proj_dir) / "2024_A").exists()


def test_rename_project_linked_paper_rebuilds_project_side(
    vault: Path, proj_dir: Path
) -> None:
    """F14: renaming a project-linked paper moves its litman_reflib/<id> symlink
    and re-renders REFERENCES.md (pre-fix it left a dangling <old> symlink)."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=[])
    runner.invoke(
        cli,
        ["link", "2024_A", "--project", "pepforge", "--library", str(vault)],
    )
    assert (_reflib(proj_dir) / "2024_A").is_symlink()

    result = runner.invoke(
        cli,
        ["rename", "2024_A", "2024_B", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert not (_reflib(proj_dir) / "2024_A").exists()
    assert (_reflib(proj_dir) / "2024_B").is_symlink()
    refs = (_reflib(proj_dir) / "REFERENCES.md").read_text(encoding="utf-8")
    assert "2024_B" in refs


def test_refresh_views_rebuilds_litman_reflib_symlinks(
    vault: Path, proj_dir: Path
) -> None:
    """F16: refresh-views rebuilds the litman_reflib/<id> symlinks, not just
    REFERENCES.md (pre-fix a deleted symlink stayed missing)."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=[])
    runner.invoke(
        cli,
        ["link", "2024_A", "--project", "pepforge", "--library", str(vault)],
    )
    link = _reflib(proj_dir) / "2024_A"
    assert link.is_symlink()

    # Simulate drift: the symlink is gone but membership TRUTH remains.
    link.unlink()
    assert not link.exists()

    result = runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert link.is_symlink()


def test_rebuild_views_neutralizes_dotdot_tag(vault: Path) -> None:
    # Review A3: a tag value ".." must not make views/by-topic/.. resolve to
    # views/ itself — _safe_name neutralizes it to a single, non-traversing
    # path segment ("_..").
    from litman.core.document import list_papers
    from litman.core.views import rebuild_views

    _write_paper(vault, "2024_A", topics=[".."])
    rebuild_views(vault, list_papers(vault))

    assert (vault / "views" / "by-topic" / "_..").is_dir()
    assert (vault / "views" / "by-topic" / "_.." / "2024_A").is_symlink()
    # The symlink did NOT escape up into views/ (the pre-fix ".." bucket).
    assert not (vault / "views" / "2024_A").exists()
