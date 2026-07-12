"""Tests for the ``lit project`` command group (M15).

Covers add / list / rename / set-path / rm including the
cascade-with-confirm UX, the non-tty abort path, and dual-write
atomicity.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.config import load_config
from litman.core.library import create_vault
from litman.core.taxonomy import parse_taxonomy
from litman.exceptions import TaxonomyError

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": fields.get("title", paper_id),
        "authors": fields.get("authors", ["Doe, Jane"]),
        "year": fields.get("year", 2024),
        "journal": fields.get("journal", "Test J."),
        "doi": fields.get("doi", f"10.0/{paper_id}"),
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": fields.get("projects", []),
        "topics": fields.get("topics", []),
        "methods": fields.get("methods", []),
        "data": fields.get("data", []),
        "type": fields.get("type", "research"),
        "status": fields.get("status", "inbox"),
        "priority": fields.get("priority", "B"),
        "read-date": None,
        "last-revisited": None,
        "related": [],
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


def _write_relevance_orphan(vault: Path, paper_id: str, project: str) -> None:
    """Write a schema-less paper carrying a stray ``relevance-<project>`` key
    but NO ``projects`` key at all.

    This is the hand-edit / legacy-migration orphan that invariant #7 permits
    (a missing field means "this dimension does not apply"). It reproduces the
    ``KeyError: 'projects'`` that ``_ripple_removals`` / ``_ripple_replacements``
    raised before the bug-report 2026-06-02_3 #4 fix: the paper is pulled into
    the ripple loop by its stray relevance key, but the ``projects`` key is
    never fabricated, so the post-change ``rt_metadata[field]`` subscript blew
    up. Mirrors ``_write_paper`` minus the ``projects`` key, plus the relevance
    annotation.
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
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        # NOTE: deliberately NO "projects" key — that is the whole point.
        f"relevance-{project}": "high",
        "topics": [],
        "methods": [],
        "data": [],
        "type": "research",
        "status": "inbox",
        "priority": "B",
        "read-date": None,
        "last-revisited": None,
        "related": [],
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


@pytest.fixture
def proj_dir(tmp_path: Path) -> Path:
    d = tmp_path / "projA"
    d.mkdir()
    return d


def _taxonomy_projects(vault: Path) -> list[str]:
    return parse_taxonomy((vault / "TAXONOMY.md").read_text())["projects"]


def _config_projects(vault: Path) -> dict[str, str]:
    return load_config(vault).projects


def _meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


def _vault_snapshot(vault: Path) -> dict[str, bytes]:
    """Byte-exact map of every vault file (skips the staging scratch dir)."""
    snap: dict[str, bytes] = {}
    for path in sorted(vault.rglob("*")):
        if path.is_dir() or ".litman-staging" in path.parts:
            continue
        snap[str(path.relative_to(vault))] = path.read_bytes()
    return snap


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_project_add_happy_dual_write(vault: Path, proj_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _taxonomy_projects(vault) == ["pepforge"]
    assert _config_projects(vault) == {"pepforge": str(proj_dir)}


def test_project_add_path_not_exist_rejected(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["project", "add", "ghost", "--path", "/nonexistent/path/xyz",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "does not exist" in str(result.exception)
    # Neither truth source mutated.
    assert _taxonomy_projects(vault) == []
    assert _config_projects(vault) == {}


def test_project_add_path_is_file_rejected(
    vault: Path, tmp_path: Path
) -> None:
    f = tmp_path / "afile.txt"
    f.write_text("x")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(f), "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, (TaxonomyError, SystemExit))


def test_project_add_duplicate_name_rejected(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    result = runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "already registered" in str(result.exception)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_project_list_join_clean(vault: Path, proj_dir: Path) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    result = runner.invoke(
        cli, ["project", "list", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "pepforge" in result.output
    assert "✓" in result.output


def test_project_list_drift_config_only(
    vault: Path, proj_dir: Path
) -> None:
    # Hand-write config with a project absent from TAXONOMY.md.
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n"
        f"  orphan: {proj_dir}\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["project", "list", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "orphan" in result.output
    assert "config-only" in result.output


def test_project_list_drift_path_missing(
    vault: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "gone"
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  p: {missing}\n",
        encoding="utf-8",
    )
    # Register the name in TAXONOMY too so it counts as "in both".
    from litman.core.taxonomy import update_user_dict_section

    txt = (vault / "TAXONOMY.md").read_text()
    os.chmod(vault / "TAXONOMY.md", 0o644)  # unlock for hand-edit (M32)
    (vault / "TAXONOMY.md").write_text(
        update_user_dict_section(txt, "projects", ["p"])
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["project", "list", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "path-missing" in result.output


def test_project_list_empty(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["project", "list", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "no projects registered" in result.output


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_project_rename_happy_ripple(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    _write_paper(vault, "2024_B", projects=["pepforge", "other"])

    result = runner.invoke(
        cli,
        ["project", "rename", "pepforge", "pepcodec",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _taxonomy_projects(vault) == ["pepcodec"]
    assert _config_projects(vault) == {"pepcodec": str(proj_dir)}
    assert _meta(vault, "2024_A")["projects"] == ["pepcodec"]
    assert "pepcodec" in _meta(vault, "2024_B")["projects"]
    assert "pepforge" not in _meta(vault, "2024_B")["projects"]


def test_project_rename_to_existing_rejected(
    vault: Path, tmp_path: Path
) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "alpha", "--path", str(a),
         "--library", str(vault)],
    )
    runner.invoke(
        cli,
        ["project", "add", "beta", "--path", str(b),
         "--library", str(vault)],
    )
    result = runner.invoke(
        cli,
        ["project", "rename", "alpha", "beta", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "already registered" in str(result.exception)


# ---------------------------------------------------------------------------
# set-path
# ---------------------------------------------------------------------------


def test_project_set_path_happy(
    vault: Path, tmp_path: Path
) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(a), "--library", str(vault)],
    )
    result = runner.invoke(
        cli,
        ["project", "set-path", "p", str(b), "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _config_projects(vault) == {"p": str(b)}
    # Non-interactive: no prompt possible, so the manual hint survives here.
    assert "lit link --rebuild-all" in result.output


def test_project_set_path_interactive_rebuilds_links_with_one_enter(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """House rule: repairable drift gets a [Y/n] default-Y at the point of
    use. set-path dangles every project link at the old location — pressing
    Enter must leave litman_reflib rebuilt at the new one, with no
    "remember to run lit link --rebuild-all later" homework."""
    from litman.commands import project as project_mod

    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    paper_dir = vault / "papers" / "2024_P_One"
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_P_One\ntitle: T\nprojects: [p]\n", encoding="utf-8"
    )
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(a), "--library", str(vault)],
    )

    monkeypatch.setattr(project_mod, "_stdin_isatty", lambda: True)
    result = runner.invoke(
        cli,
        ["project", "set-path", "p", str(b), "--library", str(vault)],
        input="\n",  # the one Enter (default Y)
    )

    assert result.exit_code == 0, result.output
    assert (b / "litman_reflib" / "2024_P_One").is_symlink()
    assert "lit link --rebuild-all" not in result.output


def test_project_set_path_same_is_noop(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    result = runner.invoke(
        cli,
        ["project", "set-path", "p", str(proj_dir),
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output


def test_project_set_path_missing_dir_rejected(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    result = runner.invoke(
        cli,
        ["project", "set-path", "p", "/no/such/dir/xyz",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "does not exist" in str(result.exception)


def test_project_set_path_unregistered_rejected(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["project", "set-path", "ghost", "/tmp", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "not registered" in str(result.exception)


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def test_project_rm_of_an_empty_project_still_confirms(
    vault: Path, proj_dir: Path
) -> None:
    """Zero papers is not zero damage, so the gate is unconditional.

    Removing an unreferenced project still drops its path binding from
    lit-config.yaml and deletes litman_reflib/ + REFERENCES.md from the
    user's own folder — outside the vault, where the trash cannot reach.
    """
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    # Non-tty (CliRunner's default) and no --yes: refuse, change nothing.
    result = runner.invoke(
        cli, ["project", "rm", "p", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert "Non-interactive environment" in result.output
    assert _taxonomy_projects(vault) == ["p"]
    assert _config_projects(vault) != {}


def test_project_rm_of_an_empty_project_says_no_papers_are_affected(
    vault: Path, proj_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning names what IS destroyed, so the Enter is an informed one."""
    monkeypatch.setattr("litman.core.confirm._stdin_is_tty", lambda: True)
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    result = runner.invoke(
        cli, ["project", "rm", "p", "--library", str(vault)], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert "0 paper(s)" in result.output
    assert "TAXONOMY.md and lit-config.yaml" in result.output
    assert str(proj_dir) in result.output
    assert _taxonomy_projects(vault) == []
    assert _config_projects(vault) == {}


def test_project_rm_of_an_empty_project_aborts_on_n(
    vault: Path, proj_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("litman.core.confirm._stdin_is_tty", lambda: True)
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    result = runner.invoke(
        cli, ["project", "rm", "p", "--library", str(vault)], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output
    assert _taxonomy_projects(vault) == ["p"]
    assert _config_projects(vault) != {}


def test_project_rm_of_an_empty_project_still_honours_yes(
    vault: Path, proj_dir: Path
) -> None:
    """The scripted path is unchanged: --yes removes it with no prompt."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "p", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    result = runner.invoke(
        cli, ["project", "rm", "p", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _taxonomy_projects(vault) == []
    assert _config_projects(vault) == {}


def test_project_rm_with_refs_prompt_then_cascade(
    vault: Path, proj_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate an interactive tty so the confirmation prompt is reached;
    # the supplied input is then consumed by click.confirm as keystrokes.
    monkeypatch.setattr(
        "litman.core.confirm._stdin_is_tty", lambda: True
    )
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    _write_paper(vault, "2024_B", projects=["pepforge", "other"])
    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--library", str(vault)],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "referenced by" in result.output
    assert _taxonomy_projects(vault) == []
    assert _config_projects(vault) == {}
    assert _meta(vault, "2024_A")["projects"] == []
    assert _meta(vault, "2024_B")["projects"] == ["other"]


def test_project_rm_prompt_abort_on_n(
    vault: Path, proj_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "litman.core.confirm._stdin_is_tty", lambda: True
    )
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--library", str(vault)],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output
    # Nothing changed.
    assert _taxonomy_projects(vault) == ["pepforge"]
    assert _meta(vault, "2024_A")["projects"] == ["pepforge"]


def test_project_rm_yes_skips_prompt(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _taxonomy_projects(vault) == []
    assert _meta(vault, "2024_A")["projects"] == []


def test_project_rm_non_tty_no_yes_aborts(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    # CliRunner default stdin is not a tty; no --yes and refs exist.
    result = runner.invoke(
        cli, ["project", "rm", "pepforge", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert "Non-interactive environment" in result.output
    # Nothing changed.
    assert _taxonomy_projects(vault) == ["pepforge"]
    assert _meta(vault, "2024_A")["projects"] == ["pepforge"]


def test_project_rm_unregistered_rejected(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["project", "rm", "ghost", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "not registered" in str(result.exception)


def test_project_rm_atomicity_index_matches_metadata(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    _write_paper(vault, "2024_B", projects=["pepforge"])
    runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--yes", "--library", str(vault)],
    )
    payload = json.loads((vault / "INDEX.json").read_text())
    for p in payload["papers"]:
        meta = _meta(vault, p["id"])
        assert meta["projects"] == p["projects"]
        assert p["projects"] == []


def test_project_rm_stray_stdin_no_yes_aborts_unmutated(
    vault: Path, proj_dir: Path
) -> None:
    """C1 regression: a non-tty stdin carrying a stray ``y\\n`` must NOT
    satisfy the confirmation. The spec ("不读不存在的 stdin") forbids
    reading stdin at all in a non-interactive environment without --yes.

    CliRunner's stdin is non-tty, so supplying ``input="y\\n"`` here
    reproduces the real-pipe ``printf 'y\\n' | lit project rm`` case.
    Pre-fix this aborted at exit 0 with the cascade applied; post-fix it
    must abort non-zero with the vault byte-identical.
    """
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    _write_paper(vault, "2024_B", projects=["pepforge", "other"])

    before = _vault_snapshot(vault)
    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--library", str(vault)],
        input="y\n",
    )
    assert result.exit_code != 0
    assert "Non-interactive environment" in result.output
    assert _vault_snapshot(vault) == before


def test_project_rm_atomicity_rollback_on_staged_write_failure(
    vault: Path, proj_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §测试覆盖 'rm atomicity rollback' + §边界 case line 164
    ("用户敲 y 后任何 staged_write 步骤失败 → 全部回滚").

    Injects a failure in the SECOND ``StagedWrite.write_text`` call
    (i.e. after the first file is staged but before commit) and asserts
    TAXONOMY.md + lit-config.yaml + every paper metadata.yaml +
    INDEX.json are byte-identical to the pre-op state, with non-zero
    exit. A body exception means ``__exit__`` skips ``_commit`` entirely,
    so nothing is promoted.
    """
    from litman.core.atomic import StagedWrite

    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    _write_paper(vault, "2024_B", projects=["pepforge"])

    before = _vault_snapshot(vault)

    real_write_text = StagedWrite.write_text
    calls = {"n": 0}

    def flaky_write_text(
        self: StagedWrite, relpath: str, content: str
    ) -> Path:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("injected mid-staged_write failure")
        return real_write_text(self, relpath, content)

    monkeypatch.setattr(StagedWrite, "write_text", flaky_write_text)

    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--yes", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert calls["n"] >= 2  # failure was actually reached
    assert _vault_snapshot(vault) == before


# ---------------------------------------------------------------------------
# W2 regression: rm / rename must rebuild vault-internal views/by-project
# ---------------------------------------------------------------------------


def test_project_rm_rebuilds_views_drops_stale_by_project(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    by_project = vault / "views" / "by-project" / "pepforge"
    assert by_project.exists()  # stale entry present before rm

    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert not by_project.exists()  # rm rebuilt views, stale entry gone


def test_project_rename_rebuilds_views_swaps_by_project(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_paper(vault, "2024_A", projects=["pepforge"])
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert (vault / "views" / "by-project" / "pepforge").exists()

    result = runner.invoke(
        cli,
        ["project", "rename", "pepforge", "pepcodec",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "views" / "by-project" / "pepforge").exists()
    assert (vault / "views" / "by-project" / "pepcodec").exists()


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------


def test_project_help_lists_five_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["project", "--help"])
    assert result.exit_code == 0
    for sub in ("add", "list", "rename", "set-path", "rm"):
        assert sub in result.output


# ---------------------------------------------------------------------------
# Regression: stray relevance key with no `projects` key (bug-report
# 2026-06-02_3 #4) — project rm / rename must not KeyError on the orphan.
# ---------------------------------------------------------------------------


def test_project_rm_relevance_orphan_without_projects_key_no_crash(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_relevance_orphan(vault, "2024_A", "pepforge")

    result = runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    # The stray relevance is dropped; no `projects` key was ever fabricated.
    meta = _meta(vault, "2024_A")
    assert "relevance-pepforge" not in meta


def test_project_rename_relevance_orphan_without_projects_key_no_crash(
    vault: Path, proj_dir: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj_dir),
         "--library", str(vault)],
    )
    _write_relevance_orphan(vault, "2024_A", "pepforge")

    result = runner.invoke(
        cli,
        ["project", "rename", "pepforge", "pepcodec", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    # The stray relevance is carried old -> new, never stranded.
    meta = _meta(vault, "2024_A")
    assert "relevance-pepforge" not in meta
    assert meta.get("relevance-pepcodec") == "high"
