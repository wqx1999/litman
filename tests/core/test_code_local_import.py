"""Tests for M3.4 — ``lit code add`` local-import branch.

`lit code add` accepts either a clone URL or a local directory path as its
first argument. URL inputs (``http://``, ``https://``, ``git@``, ``ssh://``,
``file://``) keep going through the unchanged M3.1 ``git clone`` path; bare
local-path inputs route through the new ``import_local_repo`` branch.

URL-branch coverage stays in ``tests/test_code.py``. After M3.4 those tests
explicitly pass ``f"file://{upstream_repo}"`` rather than ``str(upstream_repo)``
so the URL/clone path remains under test even though ``lit code add`` now
also accepts bare paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.code import make_repo_meta, write_repo_meta
from litman.core.library import create_vault
from litman.exceptions import CodeError

_yaml_safe = YAML(typ="safe")
_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A fresh vault under tmp_path."""
    return create_vault(tmp_path)


def _make_paper(vault: Path, paper_id: str, **extra: Any) -> Path:
    """Materialize a minimal paper folder with metadata.yaml."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": "Test paper",
        "doi": "10.1/test",
        "year": 2024,
        "status": "inbox",
        "priority": "B",
        "type": "research",
        "projects": [],
        "topics": [],
        "methods": [],
        "code-clones": [],
        "created-at": "2026-05-11T10:00:00+02:00",
        "updated-at": "2026-05-11T10:00:00+02:00",
        **extra,
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)
    return paper_dir


def _git_init_with_commit(repo_dir: Path, *, file_contents: str = "x\n") -> None:
    """Materialize ``repo_dir`` as a real git repo with one commit.

    Identical setup pattern to the ``upstream_repo`` fixture in
    ``tests/test_code.py``: ``git init``, write a file, ``git add -A``,
    ``git commit`` with a per-command ``user.email``/``user.name`` injection
    so the operation succeeds without any git global config.
    """
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    (repo_dir / "README.md").write_text(file_contents)
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "."],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(repo_dir),
            "-c", "user.email=test@example.com",
            "-c", "user.name=test",
            "commit", "-q", "-m", "init",
        ],
        check=True,
    )


@pytest.fixture
def local_git_repo(tmp_path: Path) -> Path:
    """A local git repo (with an `origin` remote) ready for cp/mv import."""
    repo = tmp_path / "local-git-src"
    repo.mkdir()
    _git_init_with_commit(repo, file_contents="# local git repo\n")
    subprocess.run(
        [
            "git", "-C", str(repo), "remote", "add",
            "origin", "https://github.com/example/local-git-src.git",
        ],
        check=True,
    )
    return repo


@pytest.fixture
def local_git_repo_no_origin(tmp_path: Path) -> Path:
    """A local git repo without an `origin` remote."""
    repo = tmp_path / "no-origin-src"
    repo.mkdir()
    _git_init_with_commit(repo, file_contents="# no origin\n")
    return repo


@pytest.fixture
def local_dirty_git_repo(tmp_path: Path) -> Path:
    """A local git repo with one committed file and one uncommitted file."""
    repo = tmp_path / "dirty-src"
    repo.mkdir()
    _git_init_with_commit(repo, file_contents="# clean part\n")
    (repo / "dirty.txt").write_text("uncommitted change\n")
    return repo


@pytest.fixture
def local_non_git_dir(tmp_path: Path) -> Path:
    """A non-empty plain directory with no .git/."""
    src = tmp_path / "plain-src"
    src.mkdir()
    (src / "main.py").write_text("print('hi')\n")
    (src / "README.md").write_text("# plain source\n")
    return src


# ---------------------------------------------------------------------------
# Local git repo — default `cp` (source preserved)
# ---------------------------------------------------------------------------


def test_local_git_repo_default_copy_preserves_source(
    vault: Path, local_git_repo: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(local_git_repo),
            "--name", "Imported",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    target = vault / "codes" / "Imported"
    assert (target / "repo" / ".git").exists()
    assert (target / "repo" / "README.md").is_file()
    assert (target / "repo-meta.yaml").is_file()
    assert (target / "notes.md").is_file()

    meta = _yaml_safe.load((target / "repo-meta.yaml").read_text())
    assert meta["name"] == "Imported"
    # origin URL was picked up from the source repo's .git/config.
    assert meta["upstream"] == "https://github.com/example/local-git-src.git"
    assert meta["papers"] == []

    # Source still present (default cp -r behaviour).
    assert local_git_repo.exists()
    assert (local_git_repo / "README.md").is_file()


def test_local_git_repo_without_origin_records_none_upstream(
    vault: Path, local_git_repo_no_origin: Path
) -> None:
    """Local git repo with no origin remote → upstream is None, not local:..."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(local_git_repo_no_origin),
            "--name", "NoOrigin",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    meta = _yaml_safe.load(
        (vault / "codes" / "NoOrigin" / "repo-meta.yaml").read_text()
    )
    assert meta["upstream"] is None
    assert (vault / "codes" / "NoOrigin" / "repo" / ".git").exists()


# ---------------------------------------------------------------------------
# Local git repo — `--move` (source consumed)
# ---------------------------------------------------------------------------


def test_local_git_repo_move_consumes_source(
    vault: Path, local_git_repo: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(local_git_repo),
            "--name", "Moved",
            "--move",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    target = vault / "codes" / "Moved"
    assert (target / "repo" / ".git").exists()
    assert (target / "repo" / "README.md").is_file()
    meta = _yaml_safe.load((target / "repo-meta.yaml").read_text())
    assert meta["upstream"] == "https://github.com/example/local-git-src.git"

    # Source gone — --move consumed it.
    assert not local_git_repo.exists()


# ---------------------------------------------------------------------------
# Non-git directory — auto `git init` + commit
# ---------------------------------------------------------------------------


def test_local_non_git_dir_gets_initialised(
    vault: Path, local_non_git_dir: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(local_non_git_dir),
            "--name", "FreshInit",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    repo_dir = vault / "codes" / "FreshInit" / "repo"
    assert (repo_dir / ".git").exists()
    assert (repo_dir / "main.py").is_file()
    assert (repo_dir / "README.md").is_file()

    # The freshly initialised repo has exactly one commit, naming the import
    # origin in the message.
    log = subprocess.run(
        ["git", "-C", str(repo_dir), "log", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    commits = [line for line in log.stdout.strip().splitlines() if line]
    assert len(commits) == 1
    assert "import from" in commits[0]
    assert str(local_non_git_dir) in commits[0]

    # upstream uses the local: prefix for provenance tracing.
    meta = _yaml_safe.load(
        (vault / "codes" / "FreshInit" / "repo-meta.yaml").read_text()
    )
    assert isinstance(meta["upstream"], str)
    assert meta["upstream"].startswith("local:")
    assert str(local_non_git_dir) in meta["upstream"]

    # Source preserved (no --move).
    assert local_non_git_dir.exists()


# ---------------------------------------------------------------------------
# Dirty git repo — uncommitted changes preserved, not auto-committed
# ---------------------------------------------------------------------------


def test_local_dirty_git_repo_preserves_uncommitted_changes(
    vault: Path, local_dirty_git_repo: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(local_dirty_git_repo),
            "--name", "Dirty",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    repo_dir = vault / "codes" / "Dirty" / "repo"
    assert (repo_dir / ".git").exists()
    # The uncommitted file should appear in the copy.
    assert (repo_dir / "dirty.txt").is_file()
    assert (repo_dir / "dirty.txt").read_text() == "uncommitted change\n"

    # It must remain uncommitted (no auto-stage / auto-commit on import).
    status = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "dirty.txt" in status.stdout


# ---------------------------------------------------------------------------
# Error cases — empty dir, non-existent path
# ---------------------------------------------------------------------------


def test_local_empty_dir_refused(vault: Path, tmp_path: Path) -> None:
    """Empty directory → CodeError, no half-built target."""
    empty = tmp_path / "empty"
    empty.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(empty),
            "--name", "EmptyTry",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)
    assert "empty" in str(result.exception).lower()

    # No partial state inside the vault.
    assert not (vault / "codes" / "EmptyTry").exists()


def test_local_nonexistent_path_refused(vault: Path, tmp_path: Path) -> None:
    """A path that does not exist → CodeError before any work happens."""
    ghost = tmp_path / "does-not-exist"
    assert not ghost.exists()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(ghost),
            "--name", "Ghost",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)
    assert not (vault / "codes" / "Ghost").exists()


# ---------------------------------------------------------------------------
# Same-name collision — existing repo dir in vault
# ---------------------------------------------------------------------------


def test_local_import_refuses_existing_repo_name(
    vault: Path, local_git_repo: Path
) -> None:
    """Pre-existing codes/<name>/ → CodeError, source untouched."""
    # Seed an existing repo entry that would collide.
    (vault / "codes" / "Collide").mkdir(parents=True)
    write_repo_meta(
        vault / "codes" / "Collide",
        make_repo_meta(name="Collide", upstream="https://example.com/x"),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(local_git_repo),
            "--name", "Collide",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)
    assert "already exists" in str(result.exception).lower()

    # Original repo-meta untouched; source still present (we never reached cp).
    meta = _yaml_safe.load(
        (vault / "codes" / "Collide" / "repo-meta.yaml").read_text()
    )
    assert meta["upstream"] == "https://example.com/x"
    assert local_git_repo.exists()


# ---------------------------------------------------------------------------
# Auto-derived name (no --name flag) — uses source basename
# ---------------------------------------------------------------------------


def test_local_import_auto_derives_name_from_basename(
    vault: Path, local_git_repo: Path
) -> None:
    """Without --name, repo_name = local_git_repo.name."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "add", str(local_git_repo), "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "codes" / "local-git-src" / "repo" / ".git").exists()


# ---------------------------------------------------------------------------
# --paper binding works with local imports too
# ---------------------------------------------------------------------------


def test_local_import_with_paper_binds_both_sides(
    vault: Path, local_git_repo: Path
) -> None:
    _make_paper(vault, "2024_Smith_X")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(local_git_repo),
            "--name", "Bound",
            "--paper", "2024_Smith_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    paper_meta = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    assert paper_meta["code-clones"] == ["Bound"]
    repo_meta = _yaml_safe.load(
        (vault / "codes" / "Bound" / "repo-meta.yaml").read_text()
    )
    assert repo_meta["papers"] == ["2024_Smith_X"]


# ---------------------------------------------------------------------------
# Help text — URL OR local path is documented
# ---------------------------------------------------------------------------


def test_cli_code_add_help_mentions_url_and_local_path() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["code", "add", "--help"])
    assert result.exit_code == 0
    assert "URL" in result.output or "url" in result.output
    assert "local" in result.output.lower()
    assert "--move" in result.output
