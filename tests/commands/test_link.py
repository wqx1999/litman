"""Tests for `lit link` / `lit unlink` + `lit link --rebuild-all` (M5.2)."""

from __future__ import annotations

import os
import sys
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
from litman.core.portable_link import is_portable_link

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
    assert is_portable_link(link)
    assert link.resolve() == (vault / "papers" / "p1").resolve()
    # Symlink stores a RELATIVE path (M0 invariant). A junction cannot: it
    # records an absolute target by construction (ADR-005 accepts that), so
    # the invariant is asserted on the arm able to hold it. Everything above
    # this line still runs on Windows.
    if sys.platform != "win32":
        assert not Path(_resolve_symlink_relative(link)).is_absolute()


def test_link_idempotent_no_metadata_change(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1", projects=["pepforge"])
    result = link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["added_to_projects"] is False
    assert result["metadata_changed"] is False
    # Symlink still created (defensive — self-heal partial state).
    assert is_portable_link(project_dir / "litman_reflib" / "p1")


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
    assert is_portable_link(code_link)
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
    assert result["code_links_skipped_links_unsupported"] == []
    assert not (project_dir / "litman_code" / "GhostRepo").exists()


def test_link_links_unsupported_not_reported_as_missing_repo(
    vault: Path, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review F31: the repo IS present but the filesystem refuses links
    # (make_portable_link -> False). It must be bucketed as
    # "links unsupported", NOT "missing repo" — the latter dead-ends the
    # user at `lit code restore-all`, which would not help.
    import litman.core.project_link as pl

    _make_fake_repo(vault, "PresentRepo")
    _make_paper(vault, "p1", code_clones=["PresentRepo"])
    monkeypatch.setattr(pl, "make_portable_link", lambda *a, **k: False)

    result = link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["code_links"] == []
    assert result["code_links_skipped_missing_repo"] == []
    assert result["code_links_skipped_links_unsupported"] == ["PresentRepo"]


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
    assert not is_portable_link(project_dir / "litman_reflib" / "p1")


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
    assert is_portable_link(project_dir / "litman_code" / "SharedRepo")

    result = unlink_paper_from_project(vault, "p1", "pepforge", registry)
    # Symlink stays because p2 still uses SharedRepo under pepforge.
    assert is_portable_link(project_dir / "litman_code" / "SharedRepo")
    assert result["code_links_removed"] == []
    assert len(result["code_links_kept"]) == 1


def test_unlink_removes_exclusive_code_symlink(
    vault: Path, project_dir: Path
) -> None:
    _make_fake_repo(vault, "SoloRepo")
    _make_paper(vault, "p1", code_clones=["SoloRepo"])
    registry = {"pepforge": str(project_dir)}
    link_paper_to_project(vault, "p1", "pepforge", registry)
    assert is_portable_link(project_dir / "litman_code" / "SoloRepo")

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
    assert is_portable_link(project_dir / "litman_reflib" / "p1")
    assert is_portable_link(project_dir / "litman_reflib" / "p2")
    assert not (project_dir / "litman_reflib" / "p3").exists()


def test_rebuild_all_wipes_stale_symlinks(vault: Path, project_dir: Path) -> None:
    """Stale symlink from a prior state -> wiped on rebuild."""
    (project_dir / "litman_reflib").mkdir()
    stale = project_dir / "litman_reflib" / "old_paper_no_longer_tagged"
    os.symlink("../../irrelevant", stale)
    _make_paper(vault, "p1", projects=["pepforge"])

    rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    assert not stale.exists()
    assert is_portable_link(project_dir / "litman_reflib" / "p1")


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
    assert is_portable_link(project_dir / "litman_code" / "MyRepo")


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
    assert is_portable_link(project_dir / "litman_reflib" / "p1")


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
    assert is_portable_link(project_dir / "litman_reflib" / "p1")
    assert is_portable_link(project_dir / "litman_reflib" / "p2")


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


# ---------------------------------------------------------------------------
# ADR-025: grade the paper FOR THIS PROJECT at link time
# ---------------------------------------------------------------------------


def test_link_with_priority_sets_the_per_project_grade(
    vault: Path, project_dir: Path
) -> None:
    """The "link and grade in one gesture" path — the whole point of decision
    5, which is also what the GUI panel row does."""
    _make_paper(vault, "p1")
    result = link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}, priority="A"
    )
    assert result["set_priority"] is True
    meta = _read_paper_meta(vault, "p1")
    assert meta["projects"] == ["pepforge"]
    assert meta["priority-pepforge"] == "A"


def test_link_without_priority_leaves_the_link_ungraded(
    vault: Path, project_dir: Path
) -> None:
    """Linked-but-ungraded is a LEGAL state (decision 5): a link happens at
    ingest, a grade after reading. Absence is that state, not a null."""
    _make_paper(vault, "p1")
    result = link_paper_to_project(
        vault, "p1", "pepforge", {"pepforge": str(project_dir)}
    )
    assert result["set_priority"] is False
    assert "priority-pepforge" not in _read_paper_meta(vault, "p1")


def test_link_priority_is_idempotent(vault: Path, project_dir: Path) -> None:
    _make_paper(vault, "p1")
    reg = {"pepforge": str(project_dir)}
    link_paper_to_project(vault, "p1", "pepforge", reg, priority="A")
    before = _read_paper_meta(vault, "p1")["updated-at"]

    again = link_paper_to_project(vault, "p1", "pepforge", reg, priority="A")
    assert again["set_priority"] is False
    assert again["metadata_changed"] is False
    assert _read_paper_meta(vault, "p1")["updated-at"] == before


def test_link_priority_regrades_an_existing_link(
    vault: Path, project_dir: Path
) -> None:
    """Same "set in one shot" semantics as --relevance: passing the flag
    overwrites, omitting it leaves the field alone."""
    _make_paper(vault, "p1")
    reg = {"pepforge": str(project_dir)}
    link_paper_to_project(vault, "p1", "pepforge", reg, priority="A")

    result = link_paper_to_project(vault, "p1", "pepforge", reg, priority="C")
    assert result["set_priority"] is True
    assert _read_paper_meta(vault, "p1")["priority-pepforge"] == "C"

    link_paper_to_project(vault, "p1", "pepforge", reg)  # flag omitted
    assert _read_paper_meta(vault, "p1")["priority-pepforge"] == "C"


def test_link_rejects_a_priority_outside_abc(
    vault: Path, project_dir: Path
) -> None:
    """LinkError, not a Click UsageError: a bad metadata value must surface in
    this repo's error shape (exit 1 + panel), like every other one."""
    _make_paper(vault, "p1")
    with pytest.raises(LinkError, match="Invalid priority"):
        link_paper_to_project(
            vault, "p1", "pepforge", {"pepforge": str(project_dir)}, priority="Q"
        )
    # Refused before anything was written.
    assert "priority-pepforge" not in _read_paper_meta(vault, "p1")


def test_link_cli_priority_flag_is_not_a_click_choice(
    vault: Path, project_dir: Path
) -> None:
    """Exit 1 (LitmanError), not exit 2 (Click UsageError). Click validates
    shapes it owns — formats, shell names; a metadata value is ours."""
    _make_paper(vault, "p1")
    _write_config_with_project(vault, "pepforge", project_dir)
    result = CliRunner().invoke(
        cli,
        ["link", "p1", "--project", "pepforge", "--priority", "Q",
         "--library", str(vault)],
    )
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, LinkError)


def test_link_cli_priority_flag_writes_and_echoes(
    vault: Path, project_dir: Path
) -> None:
    _make_paper(vault, "p1")
    _write_config_with_project(vault, "pepforge", project_dir)
    result = CliRunner().invoke(
        cli,
        ["link", "p1", "--project", "pepforge", "--priority", "B",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "priority-pepforge" in result.output
    assert _read_paper_meta(vault, "p1")["priority-pepforge"] == "B"


def test_unlink_always_drops_the_grade_even_with_keep_relevance(
    vault: Path, project_dir: Path
) -> None:
    """Decision 15: no --keep-priority. Relevance is authored prose worth
    offering to keep; a grade is one letter that means nothing without the
    link it grades, so it goes regardless — including on the path that
    explicitly spares relevance."""
    _make_paper(vault, "p1")
    reg = {"pepforge": str(project_dir)}
    link_paper_to_project(
        vault, "p1", "pepforge", reg, priority="A", relevance="core baseline"
    )

    result = unlink_paper_from_project(
        vault, "p1", "pepforge", reg, purge_relevance=False
    )
    assert result["removed_priority"] is True
    assert result["removed_priority_value"] == "A"
    assert result["removed_relevance"] is False

    meta = _read_paper_meta(vault, "p1")
    assert "priority-pepforge" not in meta
    assert meta["relevance-pepforge"] == "core baseline"   # spared, as asked


def test_unlink_cli_has_no_keep_priority_flag(
    vault: Path, project_dir: Path
) -> None:
    _make_paper(vault, "p1")
    _write_config_with_project(vault, "pepforge", project_dir)
    result = CliRunner().invoke(cli, ["unlink", "--help"])
    assert "--keep-relevance" in result.output
    assert "--keep-priority" not in result.output


def test_cli_link_replaces_folder_copy_with_link(
    vault: Path, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project directory carried over from another machine holds a real
    folder where the link belongs; `lit link` settles it instead of refusing."""
    import shutil

    import litman.core.portable_link as portable_link

    _write_config_with_project(vault, "pepforge", project_dir)
    paper_dir = _make_paper(vault, "p1", projects=["pepforge"])
    link = project_dir / "litman_reflib" / "p1"
    link.parent.mkdir(parents=True)
    shutil.copytree(paper_dir, link)

    said: list[str] = []

    class _RecordingConsole:
        def print(self, *args: object, **_kw: object) -> None:
            said.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(portable_link, "_console", _RecordingConsole())
    portable_link.reset_warning_state()

    runner = CliRunner()
    result = runner.invoke(
        cli, ["link", "p1", "--project", "pepforge", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    assert is_portable_link(link)
    assert (link / "metadata.yaml").is_file()
    assert said == []
    # The copy matched the vault, so nothing needed keeping.
    assert not (vault / ".trash" / "replaced-folders").exists()


def test_cli_link_keeps_a_folder_copy_that_differs(
    vault: Path, project_dir: Path
) -> None:
    """The same position, but the copy carries a note nobody else has."""
    import shutil

    _write_config_with_project(vault, "pepforge", project_dir)
    paper_dir = _make_paper(vault, "p1", projects=["pepforge"])
    link = project_dir / "litman_reflib" / "p1"
    link.parent.mkdir(parents=True)
    shutil.copytree(paper_dir, link)
    (link / "notes.md").write_text("only on the laptop\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["link", "p1", "--project", "pepforge", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    assert is_portable_link(link)
    aside_root = (
        vault / ".trash" / "replaced-folders" / "pepforge" / "litman_reflib"
    )
    kept = sorted(aside_root.iterdir())
    assert len(kept) == 1
    assert (kept[0] / "notes.md").read_text(encoding="utf-8") == (
        "only on the laptop\n"
    )


def test_cli_rebuild_all_says_what_it_did_with_a_folder_copy(
    vault: Path, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same event, same words as `health-check --fix` and `refresh-views`."""
    import shutil

    from litman.core.portable_link import remove_link_if_present

    monkeypatch.setenv("COLUMNS", "400")
    _write_config_with_project(vault, "pepforge", project_dir)
    _make_paper(vault, "p1", projects=["pepforge"])
    _make_paper(vault, "p2", projects=["pepforge"])
    runner = CliRunner()
    runner.invoke(cli, ["link", "--rebuild-all", "--library", str(vault)])
    for pid in ("p1", "p2"):
        link = project_dir / "litman_reflib" / pid
        assert remove_link_if_present(link)
        shutil.copytree(vault / "papers" / pid, link)
    (project_dir / "litman_reflib" / "p2" / "MY_NOTES.md").write_text(
        "hand-written on the laptop\n", encoding="utf-8"
    )

    result = runner.invoke(
        cli, ["link", "--rebuild-all", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "replaced 1 folder copy with a link" in flat
    assert "kept 1 folder that does not match the vault:" in flat
    kept_root = (
        vault / ".trash" / "replaced-folders" / "pepforge" / "litman_reflib"
    )
    kept = sorted(kept_root.iterdir())
    assert len(kept) == 1
    assert str(kept[0]) in flat


def test_cli_link_reports_a_moved_aside_folder_but_not_a_replaced_copy(
    vault: Path, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-paper `lit link` is silent about a deleted copy and loud about a
    preserved one.

    The deleted copy's original is in the vault; the preserved one is the only
    copy of what the user put in it.
    """
    import shutil

    monkeypatch.setenv("COLUMNS", "400")
    _write_config_with_project(vault, "pepforge", project_dir)
    paper_dir = _make_paper(vault, "p1", projects=["pepforge"])
    link = project_dir / "litman_reflib" / "p1"
    link.parent.mkdir(parents=True)
    shutil.copytree(paper_dir, link)

    runner = CliRunner()
    quiet = runner.invoke(
        cli, ["link", "p1", "--project", "pepforge", "--library", str(vault)]
    )
    assert quiet.exit_code == 0, quiet.output
    assert "kept 1 folder" not in " ".join(quiet.output.split())

    # The first run replaced the copy with a link. Expand it again, this time
    # with something of the user's own inside.
    from litman.core.portable_link import remove_link_if_present

    assert remove_link_if_present(link)
    shutil.copytree(paper_dir, link)
    (link / "MY_NOTES.md").write_text("hand-written\n", encoding="utf-8")

    loud = runner.invoke(
        cli, ["link", "p1", "--project", "pepforge", "--library", str(vault)]
    )

    assert loud.exit_code == 0, loud.output
    flat = " ".join(loud.output.split())
    assert "kept 1 folder that does not match the vault:" in flat
    kept_root = (
        vault / ".trash" / "replaced-folders" / "pepforge" / "litman_reflib"
    )
    kept = sorted(kept_root.iterdir())
    assert len(kept) == 1
    assert str(kept[0]) in flat
    assert (kept[0] / "MY_NOTES.md").is_file()
