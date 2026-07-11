"""Project-side ``litman_code/`` symlinks stay in sync with ``code-clones``.

Regression coverage for the staleness gap where ``lit code add``/``link``/
``unlink`` mutated a paper's ``code-clones`` without re-materializing the
derived ``<project_dir>/litman_code/<repo>`` symlinks, so a repo bound *after*
the paper was last ``lit link``ed never got a symlink (and one unbound
afterwards never lost it). The fix wires ``refresh_project_code_links`` into the
core bind/unbind mutation functions and adds a ``check_project_references`` arm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from litman.core.checks import check_project_references
from litman.core.code import (
    bind_paper_to_repo,
    unbind_paper_from_repo,
    unbind_repo_from_all_papers,
)
from litman.core.library import create_vault
from litman.core.project_link import (
    CODE_SUBDIR,
    add_project,
    link_paper_to_project,
)

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "pepforge_proj"
    p.mkdir()
    return p


def _make_paper(
    vault: Path,
    paper_id: str,
    *,
    projects: list[str] | None = None,
    code_clones: list[str] | None = None,
) -> Path:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": "Test paper",
        "authors": ["Doe, Jane"],
        "year": 2024,
        "doi": f"10.test/{paper_id}",
        "status": "inbox",
        "priority": "B",
        "type": "research",
        "projects": projects or [],
        "topics": [],
        "methods": [],
        "code-clones": code_clones or [],
        "created-at": "2026-05-11T10:00:00+02:00",
        "updated-at": "2026-05-11T10:00:00+02:00",
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)
    return paper_dir


def _make_repo(vault: Path, repo_name: str, papers: list[str] | None = None) -> Path:
    """Materialize codes/<repo>/repo/ + repo-meta.yaml — enough for a symlink."""
    repo_root = vault / "codes" / repo_name
    (repo_root / "repo").mkdir(parents=True)
    (repo_root / "repo" / "README.md").write_text("# fake\n", encoding="utf-8")
    papers_block = "".join(f"  - {p}\n" for p in (papers or []))
    (repo_root / "repo-meta.yaml").write_text(
        f"name: {repo_name}\nupstream: file:///fake\npapers:\n{papers_block}",
        encoding="utf-8",
    )
    return repo_root


def _register(vault: Path, project: str, project_dir: Path) -> dict[str, str]:
    add_project(vault, project, project_dir)
    return {project: str(project_dir)}


def _code_link(project_dir: Path, repo_name: str) -> Path:
    return project_dir / CODE_SUBDIR / repo_name


# ---------------------------------------------------------------------------
# bind — the reported bug: code bound AFTER the project was linked
# ---------------------------------------------------------------------------


def test_bind_after_link_materializes_code_symlink(
    vault: Path, project_dir: Path
) -> None:
    """The exact reported scenario: link first, bind code later."""
    registry = _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1")
    # 1) Link the paper — at this moment it has no code-clones.
    link_paper_to_project(vault, "p1", "pepforge", registry)
    assert (project_dir / "litman_reflib" / "p1").is_symlink()
    assert not _code_link(project_dir, "MyRepo").exists()
    # 2) Bind the repo AFTER the link. The symlink must appear now (the bug:
    #    it never did until `lit link --rebuild-all`).
    _make_repo(vault, "MyRepo")
    assert bind_paper_to_repo(vault, "p1", "MyRepo") is True
    link = _code_link(project_dir, "MyRepo")
    assert link.is_symlink()
    assert link.resolve() == (vault / "codes" / "MyRepo" / "repo").resolve()


def test_bind_before_link_still_works(vault: Path, project_dir: Path) -> None:
    """Reverse order (the case that accidentally worked) is unaffected."""
    registry = _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1")
    _make_repo(vault, "MyRepo")
    bind_paper_to_repo(vault, "p1", "MyRepo")
    link_paper_to_project(vault, "p1", "pepforge", registry)
    assert _code_link(project_dir, "MyRepo").is_symlink()


def test_bind_paper_in_multiple_projects(
    vault: Path, tmp_path: Path
) -> None:
    """A paper in two projects gets the symlink in BOTH on bind."""
    pa = tmp_path / "proj_a"
    pb = tmp_path / "proj_b"
    pa.mkdir()
    pb.mkdir()
    _register(vault, "a", pa)
    _register(vault, "b", pb)
    _make_paper(vault, "p1", projects=["a", "b"])
    _make_repo(vault, "MyRepo")
    bind_paper_to_repo(vault, "p1", "MyRepo")
    assert _code_link(pa, "MyRepo").is_symlink()
    assert _code_link(pb, "MyRepo").is_symlink()


def test_bind_paper_with_no_project_is_noop(
    vault: Path, project_dir: Path
) -> None:
    """Binding code to an unlinked paper does not create a litman_code dir."""
    _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1")  # NOT in any project
    _make_repo(vault, "MyRepo")
    bind_paper_to_repo(vault, "p1", "MyRepo")
    assert not (project_dir / CODE_SUBDIR).exists()


def test_bind_noop_self_heals_missing_symlink(
    vault: Path, project_dir: Path
) -> None:
    """An idempotent re-bind refreshes a previously-stale (missing) symlink."""
    registry = _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    _make_repo(vault, "MyRepo")
    bind_paper_to_repo(vault, "p1", "MyRepo")
    # Simulate the stale state: delete the symlink out of band.
    _code_link(project_dir, "MyRepo").unlink()
    # A second bind is a metadata no-op but must restore the symlink.
    assert bind_paper_to_repo(vault, "p1", "MyRepo") is False
    assert _code_link(project_dir, "MyRepo").is_symlink()


# ---------------------------------------------------------------------------
# unbind — the symmetric staleness (an EXTRA/stale symlink)
# ---------------------------------------------------------------------------


def test_unbind_removes_orphaned_code_symlink(
    vault: Path, project_dir: Path
) -> None:
    registry = _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    _make_repo(vault, "MyRepo")
    bind_paper_to_repo(vault, "p1", "MyRepo")
    assert _code_link(project_dir, "MyRepo").is_symlink()
    unbind_paper_from_repo(vault, "p1", "MyRepo")
    assert not _code_link(project_dir, "MyRepo").exists()


def test_unbind_keeps_symlink_shared_by_another_paper(
    vault: Path, project_dir: Path
) -> None:
    """A 1:N repo: unbinding one paper keeps the symlink while another uses it."""
    _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    _make_paper(vault, "p2", projects=["pepforge"])
    _make_repo(vault, "SharedRepo")
    bind_paper_to_repo(vault, "p1", "SharedRepo")
    bind_paper_to_repo(vault, "p2", "SharedRepo")
    assert _code_link(project_dir, "SharedRepo").is_symlink()
    unbind_paper_from_repo(vault, "p1", "SharedRepo")
    # p2 still binds it → symlink stays.
    assert _code_link(project_dir, "SharedRepo").is_symlink()


def test_cascade_unbind_all_removes_code_symlink(
    vault: Path, project_dir: Path
) -> None:
    """`lit code rm --cascade` (unbind_repo_from_all_papers) clears symlinks."""
    _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    _make_paper(vault, "p2", projects=["pepforge"])
    _make_repo(vault, "SharedRepo")
    bind_paper_to_repo(vault, "p1", "SharedRepo")
    bind_paper_to_repo(vault, "p2", "SharedRepo")
    assert _code_link(project_dir, "SharedRepo").is_symlink()
    affected = unbind_repo_from_all_papers(vault, "SharedRepo")
    assert set(affected) == {"p1", "p2"}
    assert not _code_link(project_dir, "SharedRepo").exists()


# ---------------------------------------------------------------------------
# health-check — defense-in-depth detection (ledger #3, code arm)
# ---------------------------------------------------------------------------


def test_health_check_flags_missing_code_symlink(
    vault: Path, project_dir: Path
) -> None:
    _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"], code_clones=["MyRepo"])
    _make_repo(vault, "MyRepo", papers=["p1"])
    link_paper_to_project(vault, "p1", "pepforge", {"pepforge": str(project_dir)})
    # Drop the code symlink out of band → drift.
    _code_link(project_dir, "MyRepo").unlink()
    issues = check_project_references(vault, _papers(vault))
    msgs = [i.message for i in issues]
    assert any("litman_code symlink for 'MyRepo'" in m for m in msgs), msgs
    assert all(i.category == "project_references" for i in issues)


def test_health_check_flags_extra_code_symlink(
    vault: Path, project_dir: Path
) -> None:
    _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    _make_repo(vault, "Ghost")
    # Fabricate a symlink with no backing code-clone membership.
    (project_dir / CODE_SUBDIR).mkdir()
    target = (vault / "codes" / "Ghost" / "repo").resolve()
    _code_link(project_dir, "Ghost").symlink_to(target)
    issues = check_project_references(vault, _papers(vault))
    assert any(
        "no matching code-clone membership" in i.message for i in issues
    ), [i.message for i in issues]


def test_health_check_clean_after_bind(
    vault: Path, project_dir: Path
) -> None:
    """No project_references drift once bind has refreshed the symlinks."""
    _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    link_paper_to_project(vault, "p1", "pepforge", {"pepforge": str(project_dir)})
    _make_repo(vault, "MyRepo")
    bind_paper_to_repo(vault, "p1", "MyRepo")
    issues = check_project_references(vault, _papers(vault))
    assert issues == [], [i.message for i in issues]


def _papers(vault: Path) -> list[dict[str, Any]]:
    from litman.core.document import list_papers

    return list_papers(vault)


# ---------------------------------------------------------------------------
# reconcile — dangling links count as absent (the vault-moved state)
# ---------------------------------------------------------------------------


def test_reconcile_repoints_dangling_expected_link(
    vault: Path, project_dir: Path
) -> None:
    """A dangling ``litman_code/<repo>`` link whose NAME matches the expected
    set used to be skipped as "already present" — reconcile was powerless on
    exactly the state it exists to repair. It must be re-pointed in place."""
    from litman.core.document import list_papers
    from litman.core.project_link import reconcile_project_code_links

    _register(vault, "pepforge", project_dir)
    _make_repo(vault, "alphafold", papers=["p1"])
    _make_paper(vault, "p1", projects=["pepforge"], code_clones=["alphafold"])

    # Plant a dangling link under the expected name — what a moved vault
    # leaves behind: right name, dead target.
    code_dir = project_dir / CODE_SUBDIR
    code_dir.mkdir(parents=True, exist_ok=True)
    link = code_dir / "alphafold"
    link.symlink_to("../nowhere/alphafold")
    assert link.is_symlink() and not link.exists()

    result = reconcile_project_code_links(
        vault, "pepforge", project_dir, list_papers(vault)
    )

    assert result["created"] == ["alphafold"]
    assert link.exists()
    assert link.resolve() == (vault / "codes" / "alphafold" / "repo").resolve()


def test_reconcile_removes_dangling_orphan_link(
    vault: Path, project_dir: Path
) -> None:
    """A dangling link with NO matching code-clone membership is an orphan —
    removed, not resurrected (the removal arm must keep seeing dangling
    entries even though the create arm now treats them as absent)."""
    from litman.core.document import list_papers
    from litman.core.project_link import reconcile_project_code_links

    _register(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])  # binds no repos

    code_dir = project_dir / CODE_SUBDIR
    code_dir.mkdir(parents=True, exist_ok=True)
    ghost = code_dir / "ghostrepo"
    ghost.symlink_to("../nowhere/ghostrepo")
    assert ghost.is_symlink() and not ghost.exists()

    result = reconcile_project_code_links(
        vault, "pepforge", project_dir, list_papers(vault)
    )

    assert result["removed"] == ["ghostrepo"]
    assert not ghost.is_symlink()
