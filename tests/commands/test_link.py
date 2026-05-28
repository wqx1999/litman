"""Tests for `lit link` / `lit unlink` + `lit link --rebuild-all` (M5.2)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.project_link import (
    LinkError,
    link_paper_to_project,
    rebuild_all_project_links,
    unlink_paper_from_project,
)
from litman.exceptions import PaperNotFoundError

_yaml_safe = YAML(typ="safe")
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
    title: str = "Test paper",
    year: int | None = 2024,
    authors: list[str] | None = None,
    priority: str | None = "B",
    projects: list[str] | None = None,
    code_clones: list[str] | None = None,
    **extra: Any,
) -> Path:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": title,
        "authors": authors or ["Doe, Jane"],
        "year": year,
        "doi": f"10.test/{paper_id}",
        "status": "inbox",
        "priority": priority,
        "type": "research",
        "projects": projects or [],
        "topics": [],
        "methods": [],
        "code-clones": code_clones or [],
        "created-at": "2026-05-11T10:00:00+02:00",
        "updated-at": "2026-05-11T10:00:00+02:00",
        **extra,
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)
    return paper_dir


def _make_fake_repo(vault: Path, repo_name: str) -> Path:
    """Materialize codes/<repo>/repo/ + repo-meta.yaml — enough for symlink."""
    repo_root = vault / "codes" / repo_name
    repo_root.mkdir(parents=True)
    repo_checkout = repo_root / "repo"
    repo_checkout.mkdir()
    (repo_checkout / "README.md").write_text("# fake\n")
    (repo_root / "repo-meta.yaml").write_text(
        f"name: {repo_name}\nupstream: file:///fake\npapers: []\n",
        encoding="utf-8",
    )
    return repo_root


def _read_paper_meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml_safe.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text(encoding="utf-8")
    )


def _resolve_symlink_relative(link_path: Path) -> str:
    """Return the relative target stored in the symlink (NOT the resolved abs)."""
    return os.readlink(link_path)


# ---------------------------------------------------------------------------
# link_paper_to_project — happy paths
# ---------------------------------------------------------------------------


def test_link_adds_to_projects_and_symlinks(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1")
    result = link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["added_to_projects"] is True
    meta = _read_paper_meta(vault, "p1")
    assert meta["projects"] == ["pepforge"]
    link = project_dir / "litman_reflib" / "p1"
    assert link.is_symlink()
    assert link.resolve() == (vault / "papers" / "p1").resolve()
    # Symlink stores a RELATIVE path (M0 invariant).
    assert not Path(_resolve_symlink_relative(link)).is_absolute()


def test_link_idempotent_no_metadata_change(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1", projects=["pepforge"])
    result = link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["added_to_projects"] is False
    assert result["metadata_changed"] is False
    # Symlink still created (defensive — self-heal partial state).
    assert (project_dir / "litman_reflib" / "p1").is_symlink()


def test_link_dedups_projects(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1", projects=["pepforge"])
    link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    meta = _read_paper_meta(vault, "p1")
    assert meta["projects"] == ["pepforge"]  # no double entry


def test_link_sorts_projects_alphabetically(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1", projects=["zebra"])
    link_paper_to_project(
        vault, "p1", "alpha", {"alpha": str(project_dir)}
    )
    meta = _read_paper_meta(vault, "p1")
    assert meta["projects"] == ["alpha", "zebra"]


def test_link_with_relevance_sets_field(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1")
    link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)},
        relevance="Direct baseline",
    )
    meta = _read_paper_meta(vault, "p1")
    assert meta["relevance-pepforge"] == "Direct baseline"


def test_link_preserves_user_set_relevance_when_none_passed(
    vault: Path, project_dir: Path
) -> None:
    """Re-linking should not clobber existing relevance text."""
    _make_paper(
        vault, "p1",
        projects=["pepforge"],
        **{"relevance-pepforge": "Manually set note"},
    )
    link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    meta = _read_paper_meta(vault, "p1")
    assert meta["relevance-pepforge"] == "Manually set note"


def test_link_bumps_updated_at(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1")
    before = _read_paper_meta(vault, "p1")["updated-at"]
    link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    after = _read_paper_meta(vault, "p1")["updated-at"]
    assert before != after


def test_link_writes_references_md(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1", title="Hello paper")
    link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    refs = project_dir / "litman_reflib" / "REFERENCES.md"
    assert refs.is_file()
    body = refs.read_text(encoding="utf-8")
    assert "Hello paper" in body
    assert "[[p1]]" in body


def test_link_creates_code_symlinks_when_repo_present(
    vault: Path, project_dir: Path
) -> None:
    _make_fake_repo(vault, "MyRepo")
    _make_paper(vault, "p1", code_clones=["MyRepo"])
    result = link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["code_links"] == ["MyRepo"]
    code_link = project_dir / "litman_code" / "MyRepo"
    assert code_link.is_symlink()
    assert code_link.resolve() == (vault / "codes" / "MyRepo" / "repo").resolve()


def test_link_skips_code_symlink_when_repo_missing_locally(
    vault: Path, project_dir: Path
) -> None:
    """code-clones names a repo whose codes/<name>/repo/ is absent (e.g. before
    `lit code restore-all`) -> skip the symlink, don't error."""
    _make_paper(vault, "p1", code_clones=["GhostRepo"])
    result = link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["code_links"] == []
    assert result["code_links_skipped_missing_repo"] == ["GhostRepo"]
    assert not (project_dir / "litman_code" / "GhostRepo").exists()


# ---------------------------------------------------------------------------
# link_paper_to_project — error paths
# ---------------------------------------------------------------------------


def test_link_unregistered_project_refused(vault: Path) -> None:
    _make_paper(vault, "p1")
    with pytest.raises(LinkError, match="not registered"):
        link_paper_to_project(vault, "p1", "pepforge", {})


def test_link_missing_project_dir_refused(vault: Path, tmp_path: Path) -> None:
    _make_paper(vault, "p1")
    with pytest.raises(LinkError, match="does not exist"):
        link_paper_to_project(
            vault, "p1", "pepforge",
            {"pepforge": str(tmp_path / "does_not_exist")},
        )


def test_link_missing_paper_refused(vault: Path, project_dir: Path) -> None:
    with pytest.raises(PaperNotFoundError):
        link_paper_to_project(
            vault, "ghost_paper", "pepforge", {"pepforge": str(project_dir)}
        )


# ---------------------------------------------------------------------------
# unlink_paper_from_project
# ---------------------------------------------------------------------------


def test_unlink_removes_from_projects_and_symlink(
    vault: Path, project_dir: Path
) -> None:
    _make_paper(vault, "p1")
    link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    result = unlink_paper_from_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["was_in_projects"] is True
    assert result["paper_link_removed"] is True
    meta = _read_paper_meta(vault, "p1")
    assert meta["projects"] == []
    assert not (project_dir / "litman_reflib" / "p1").is_symlink()


def test_unlink_drops_relevance_by_default(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1")
    link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)},
        relevance="some note",
    )
    result = unlink_paper_from_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["removed_relevance"] is True
    assert result["removed_relevance_value"] == "some note"
    meta = _read_paper_meta(vault, "p1")
    assert "relevance-pepforge" not in meta


def test_unlink_keep_relevance_preserves_field(
    vault: Path, project_dir: Path
) -> None:
    _make_paper(vault, "p1")
    link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)},
        relevance="some note",
    )
    unlink_paper_from_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)},
        purge_relevance=False,
    )
    meta = _read_paper_meta(vault, "p1")
    assert meta["relevance-pepforge"] == "some note"


def test_unlink_keeps_shared_code_symlink(vault: Path, project_dir: Path) -> None:
    """Repo bound to two papers; unlinking one keeps the project's code symlink."""
    _make_fake_repo(vault, "SharedRepo")
    _make_paper(vault, "p1", code_clones=["SharedRepo"])
    _make_paper(vault, "p2", code_clones=["SharedRepo"])
    registry = {"pepforge": str(project_dir)}
    link_paper_to_project(vault, "p1", "pepforge", registry)
    link_paper_to_project(vault, "p2", "pepforge", registry)
    assert (project_dir / "litman_code" / "SharedRepo").is_symlink()

    result = unlink_paper_from_project(vault, "p1", "pepforge", registry)
    # Symlink stays because p2 still uses SharedRepo under pepforge.
    assert (project_dir / "litman_code" / "SharedRepo").is_symlink()
    assert result["code_links_removed"] == []
    assert len(result["code_links_kept"]) == 1


def test_unlink_removes_exclusive_code_symlink(
    vault: Path, project_dir: Path
) -> None:
    _make_fake_repo(vault, "SoloRepo")
    _make_paper(vault, "p1", code_clones=["SoloRepo"])
    registry = {"pepforge": str(project_dir)}
    link_paper_to_project(vault, "p1", "pepforge", registry)
    assert (project_dir / "litman_code" / "SoloRepo").is_symlink()

    result = unlink_paper_from_project(vault, "p1", "pepforge", registry)
    assert not (project_dir / "litman_code" / "SoloRepo").exists()
    assert result["code_links_removed"] == ["SoloRepo"]


def test_unlink_paper_never_linked_is_noop(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1")  # not in projects, no symlinks
    result = unlink_paper_from_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["was_in_projects"] is False
    assert result["metadata_changed"] is False
    assert result["paper_link_removed"] is False


def test_unlink_regenerates_references_md(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1", title="To be unlinked")
    registry = {"pepforge": str(project_dir)}
    link_paper_to_project(vault, "p1", "pepforge", registry)
    body_before = (project_dir / "litman_reflib" / "REFERENCES.md").read_text(
        encoding="utf-8"
    )
    assert "To be unlinked" in body_before

    unlink_paper_from_project(vault, "p1", "pepforge", registry)
    body_after = (project_dir / "litman_reflib" / "REFERENCES.md").read_text(
        encoding="utf-8"
    )
    assert "To be unlinked" not in body_after
    assert "No papers tagged" in body_after


# ---------------------------------------------------------------------------
# rebuild_all_project_links
# ---------------------------------------------------------------------------


def test_rebuild_all_creates_links_for_each_tagged_paper(
    vault: Path, project_dir: Path
) -> None:
    _make_paper(vault, "p1", projects=["pepforge"])
    _make_paper(vault, "p2", projects=["pepforge"])
    _make_paper(vault, "p3", projects=["other"])  # not in pepforge

    results = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    assert results["pepforge"]["status"] == "rebuilt"
    assert results["pepforge"]["n_paper_links"] == 2
    assert (project_dir / "litman_reflib" / "p1").is_symlink()
    assert (project_dir / "litman_reflib" / "p2").is_symlink()
    assert not (project_dir / "litman_reflib" / "p3").exists()


def test_rebuild_all_wipes_stale_symlinks(vault: Path, project_dir: Path) -> None:
    """Stale symlink from a prior state -> wiped on rebuild."""
    (project_dir / "litman_reflib").mkdir()
    stale = project_dir / "litman_reflib" / "old_paper_no_longer_tagged"
    os.symlink("../../irrelevant", stale)
    _make_paper(vault, "p1", projects=["pepforge"])

    rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    assert not stale.exists()
    assert (project_dir / "litman_reflib" / "p1").is_symlink()


def test_rebuild_all_preserves_references_md_during_wipe(
    vault: Path, project_dir: Path
) -> None:
    """REFERENCES.md is content, not a symlink — must survive symlink wipe."""
    (project_dir / "litman_reflib").mkdir()
    refs = project_dir / "litman_reflib" / "REFERENCES.md"
    refs.write_text("placeholder body\n", encoding="utf-8")
    _make_paper(vault, "p1", projects=["pepforge"])

    rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    # File was overwritten with fresh content (not deleted-then-recreated).
    assert refs.is_file()
    assert "[[p1]]" in refs.read_text(encoding="utf-8")


def test_rebuild_all_skips_missing_project_dir(
    vault: Path, tmp_path: Path
) -> None:
    _make_paper(vault, "p1", projects=["ghost"])
    results = rebuild_all_project_links(
        vault, {"ghost": str(tmp_path / "does_not_exist")}
    )
    assert results["ghost"]["status"] == "skipped"
    assert results["ghost"]["n_tagged"] == 1


def test_rebuild_all_recreates_code_symlinks(
    vault: Path, project_dir: Path
) -> None:
    _make_fake_repo(vault, "MyRepo")
    _make_paper(vault, "p1", projects=["pepforge"], code_clones=["MyRepo"])
    results = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    assert results["pepforge"]["n_code_links"] == 1
    assert (project_dir / "litman_code" / "MyRepo").is_symlink()


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _write_config_with_project(vault: Path, project: str, path: Path) -> None:
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  {project}: {path}\n",
        encoding="utf-8",
    )


def test_cli_link_happy_path(vault: Path, project_dir: Path) -> None:
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["link", "p1", "--project", "pepforge", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Linked" in result.output
    assert (project_dir / "litman_reflib" / "p1").is_symlink()


def test_cli_link_with_relevance(vault: Path, project_dir: Path) -> None:
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["link", "p1", "--project", "pepforge",
         "--relevance", "Direct baseline",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    meta = _read_paper_meta(vault, "p1")
    assert meta["relevance-pepforge"] == "Direct baseline"


def test_cli_link_unregistered_project_friendly_error(
    vault: Path,
) -> None:
    _make_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["link", "p1", "--project", "pepforge", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, LinkError)
    assert "not registered" in str(result.exception)


def test_cli_unlink_happy_path(vault: Path, project_dir: Path) -> None:
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "p1")
    runner = CliRunner()
    runner.invoke(
        cli, ["link", "p1", "--project", "pepforge", "--library", str(vault)]
    )
    result = runner.invoke(
        cli, ["unlink", "p1", "--project", "pepforge", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Unlinked" in result.output
    assert not (project_dir / "litman_reflib" / "p1").exists()


def test_cli_link_rebuild_all(vault: Path, project_dir: Path) -> None:
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    _make_paper(vault, "p2", projects=["pepforge"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["link", "--rebuild-all", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "pepforge" in result.output
    assert "2 paper link" in result.output
    assert (project_dir / "litman_reflib" / "p1").is_symlink()
    assert (project_dir / "litman_reflib" / "p2").is_symlink()


def test_cli_link_rebuild_all_no_projects(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["link", "--rebuild-all", "--library", str(vault)]
    )
    assert result.exit_code == 0
    assert "No projects" in result.output


def test_cli_link_mutually_exclusive_modes(
    vault: Path, project_dir: Path
) -> None:
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["link", "p1", "--project", "pepforge",
         "--rebuild-all", "--library", str(vault)],
    )
    assert result.exit_code != 0


def test_cli_link_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["link", "--help"])
    assert result.exit_code == 0
    assert "--project" in result.output
    assert "--rebuild-all" in result.output
    assert "--relevance" in result.output
    assert "--paper-doi" in result.output


def test_cli_link_accepts_fuzzy_substring(
    vault: Path, project_dir: Path
) -> None:
    """M11 smoke: fuzzy substring resolves to the unique paper."""
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "2024_Pandi_Cellfree")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "link",
            "Pandi",
            "--project",
            "pepforge",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2024_Pandi_Cellfree" in result.output


def test_cli_link_accepts_paper_doi(
    vault: Path, project_dir: Path
) -> None:
    """M11 smoke: --paper-doi reverse-looks-up the paper."""
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "link",
            "--paper-doi",
            "10.test/p1",
            "--project",
            "pepforge",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output


def test_cli_unlink_accepts_fuzzy_substring(
    vault: Path, project_dir: Path
) -> None:
    """M11 smoke: unlink also routes through the fuzzy resolver."""
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "2024_Pandi_Cellfree")
    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "link",
            "2024_Pandi_Cellfree",
            "--project",
            "pepforge",
            "--library",
            str(vault),
        ],
    )
    result = runner.invoke(
        cli,
        [
            "unlink",
            "Pandi",
            "--project",
            "pepforge",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Unlinked" in result.output
