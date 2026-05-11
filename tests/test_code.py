"""Tests for ``lit code add`` (M3.1) — helpers in core/code.py + CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.code import (
    bind_paper_to_repo,
    derive_repo_name,
    is_valid_repo_name,
    make_repo_meta,
    read_repo_meta,
    write_notes,
    write_repo_meta,
)
from litman.core.library import create_vault
from litman.exceptions import CodeError, PaperNotFoundError

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


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """A real local git repo wangq can clone from a file:// URL.

    Some tests bypass this and clone from a literal directory path — both work
    with git as long as the directory holds a .git/ dir.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(upstream)],
        check=True,
    )
    (upstream / "README.md").write_text("# upstream test repo\n")
    subprocess.run(
        ["git", "-C", str(upstream), "add", "."],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(upstream),
         "-c", "user.email=test@example.com",
         "-c", "user.name=test",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    return upstream


# ---------------------------------------------------------------------------
# derive_repo_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/molecularsets/HELM-GPT", "HELM-GPT"),
        ("https://github.com/molecularsets/HELM-GPT.git", "HELM-GPT"),
        ("git@github.com:foo/bar.git", "bar"),
        ("git@github.com:foo/bar", "bar"),
        ("file:///tmp/some/repo", "repo"),
        ("ssh://user@host/path/to/X", "X"),
        # Trailing slash tolerated.
        ("https://github.com/molecularsets/HELM-GPT/", "HELM-GPT"),
        # Repo names commonly contain dots — must survive.
        ("https://github.com/foo/bar.baz", "bar.baz"),
        # ".git" stripped only when it's the terminal suffix.
        ("https://github.com/foo/bar.git.git", "bar.git"),
    ],
)
def test_derive_repo_name(url: str, expected: str) -> None:
    assert derive_repo_name(url) == expected


def test_derive_repo_name_empty_raises() -> None:
    with pytest.raises(CodeError, match="empty"):
        derive_repo_name("")
    with pytest.raises(CodeError, match="empty"):
        derive_repo_name("   ")


def test_derive_repo_name_invalid_chars_raise() -> None:
    # Repo name starts with hyphen after stripping → invalid (would confuse shells).
    with pytest.raises(CodeError, match="valid repo name"):
        derive_repo_name("https://example.com/-hyphen-leader")


# ---------------------------------------------------------------------------
# is_valid_repo_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,valid",
    [
        ("HELM-GPT", True),
        ("repo_with_underscores", True),
        ("repo.with.dots", True),
        ("Repo123", True),
        ("_leading-under", True),
        ("", False),
        ("-leading-hyphen", False),  # would parse as a shell flag
        (".hidden", False),
        ("with/slash", False),
        ("with\\backslash", False),
        ("with..traversal", False),
        ("with space", False),
    ],
)
def test_is_valid_repo_name(name: str, valid: bool) -> None:
    assert is_valid_repo_name(name) is valid


# ---------------------------------------------------------------------------
# make_repo_meta
# ---------------------------------------------------------------------------


def test_make_repo_meta_schema() -> None:
    meta = make_repo_meta(
        name="HELM-GPT",
        upstream="https://github.com/foo/HELM-GPT",
        papers=["2024_Chen_X"],
        now="2026-05-11T10:00:00+02:00",
    )
    assert meta == {
        "name": "HELM-GPT",
        "upstream": "https://github.com/foo/HELM-GPT",
        "created-at": "2026-05-11T10:00:00+02:00",
        "updated-at": "2026-05-11T10:00:00+02:00",
        "papers": ["2024_Chen_X"],
        "framework": None,
        "runs-on": None,
        "status": None,
    }


def test_make_repo_meta_empty_papers_default() -> None:
    meta = make_repo_meta(name="X", upstream="u", now="t")
    assert meta["papers"] == []


def test_make_repo_meta_papers_is_copied_not_aliased() -> None:
    src = ["2024_A_x"]
    meta = make_repo_meta(name="X", upstream="u", papers=src, now="t")
    src.append("mutation")
    assert meta["papers"] == ["2024_A_x"]


# ---------------------------------------------------------------------------
# write_repo_meta + write_notes + read_repo_meta round-trip
# ---------------------------------------------------------------------------


def test_write_and_read_repo_meta_roundtrip(vault: Path) -> None:
    repo_dir = vault / "codes" / "TestRepo"
    repo_dir.mkdir(parents=True)
    meta = make_repo_meta(
        name="TestRepo",
        upstream="file:///tmp/x",
        papers=["2024_Y_z"],
        now="2026-05-11T10:00:00+02:00",
    )
    write_repo_meta(repo_dir, meta)

    loaded = read_repo_meta(vault, "TestRepo")
    assert loaded["name"] == "TestRepo"
    assert loaded["papers"] == ["2024_Y_z"]
    assert loaded["framework"] is None


def test_read_repo_meta_missing_raises(vault: Path) -> None:
    with pytest.raises(CodeError, match="No repo-meta.yaml"):
        read_repo_meta(vault, "DoesNotExist")


def test_write_notes_template(vault: Path) -> None:
    repo_dir = vault / "codes" / "X"
    repo_dir.mkdir(parents=True)
    write_notes(repo_dir, name="X", upstream="file:///tmp/u")
    body = (repo_dir / "notes.md").read_text(encoding="utf-8")
    assert "# X" in body
    assert "file:///tmp/u" in body


# ---------------------------------------------------------------------------
# bind_paper_to_repo
# ---------------------------------------------------------------------------


def test_bind_appends_to_code_clones(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    changed = bind_paper_to_repo(vault, "2024_Smith_X", "MyRepo")
    assert changed is True

    paper_meta = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    assert paper_meta["code-clones"] == ["MyRepo"]
    # updated-at should have advanced from the seed timestamp.
    assert paper_meta["updated-at"] != "2026-05-11T10:00:00+02:00"


def test_bind_idempotent_returns_false(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X", **{"code-clones": ["MyRepo"]})
    seed_updated_at = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )["updated-at"]

    changed = bind_paper_to_repo(vault, "2024_Smith_X", "MyRepo")
    assert changed is False
    # updated-at should NOT have advanced — the bind was a no-op.
    after = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    assert after["updated-at"] == seed_updated_at


def test_bind_updates_index_json(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    bind_paper_to_repo(vault, "2024_Smith_X", "MyRepo")
    # INDEX.json doesn't carry code-clones in its projection (see
    # views.INDEX_PAPER_FIELDS), but the write should still succeed and
    # be self-consistent — n_papers reflects on-disk state.
    payload = json.loads((vault / "INDEX.json").read_text())
    ids = {p["id"] for p in payload["papers"]}
    assert "2024_Smith_X" in ids


def test_bind_missing_paper_raises(vault: Path) -> None:
    with pytest.raises(PaperNotFoundError):
        bind_paper_to_repo(vault, "2024_Nope_X", "MyRepo")


# ---------------------------------------------------------------------------
# CLI: lit code add
# ---------------------------------------------------------------------------


def test_cli_code_add_creates_clone(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "TestRepo",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    repo_root = vault / "codes" / "TestRepo"
    assert repo_root.is_dir()
    assert (repo_root / "repo").is_dir()
    assert (repo_root / "repo" / ".git").exists()
    assert (repo_root / "repo" / "README.md").is_file()
    assert (repo_root / "repo-meta.yaml").is_file()
    assert (repo_root / "notes.md").is_file()

    meta = _yaml_safe.load((repo_root / "repo-meta.yaml").read_text())
    assert meta["name"] == "TestRepo"
    assert meta["upstream"] == str(upstream_repo)
    assert meta["papers"] == []
    assert meta["framework"] is None


def test_cli_code_add_auto_derives_name(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    # Pass the path with a trailing .git-style segment — derive_repo_name
    # would strip a real ".git" suffix; here the last segment is "upstream".
    result = runner.invoke(
        cli,
        ["code", "add", str(upstream_repo), "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "codes" / "upstream" / "repo" / ".git").exists()


def test_cli_code_add_binds_paper(
    vault: Path, upstream_repo: Path
) -> None:
    _make_paper(vault, "2024_Smith_X")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "MyRepo",
            "--paper", "2024_Smith_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    paper_meta = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    assert paper_meta["code-clones"] == ["MyRepo"]
    # repo-meta.yaml back-reference also set.
    repo_meta = _yaml_safe.load(
        (vault / "codes" / "MyRepo" / "repo-meta.yaml").read_text()
    )
    assert repo_meta["papers"] == ["2024_Smith_X"]


def test_cli_code_add_existing_repo_refused(
    vault: Path, upstream_repo: Path
) -> None:
    """Same --name twice → CodeError, no half-built state."""
    runner = CliRunner()
    args = [
        "code", "add", str(upstream_repo),
        "--name", "MyRepo",
        "--library", str(vault),
    ]
    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, args)
    assert second.exit_code != 0
    assert isinstance(second.exception, CodeError)
    # Original clone untouched.
    assert (vault / "codes" / "MyRepo" / "repo-meta.yaml").is_file()


def test_cli_code_add_missing_paper_refused(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "MyRepo",
            "--paper", "2024_Nope_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)
    # No half-built codes/ dir.
    assert not (vault / "codes" / "MyRepo").exists()


def test_cli_code_add_clone_failure_rolls_back(
    vault: Path,
) -> None:
    """git clone failure (URL doesn't exist) → CodeError + no half-built dir."""
    bogus = "/nonexistent/path/that/is/not/a/repo"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", bogus,
            "--name", "MyRepo",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)
    # The pre-clone mkdir created codes/MyRepo/; the rollback should remove it.
    assert not (vault / "codes" / "MyRepo").exists()


def test_cli_code_add_invalid_name_refused(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "-bad-leading-hyphen",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)
    assert "Invalid --name" in str(result.exception)


def test_cli_code_add_full_clone_with_depth_zero(
    vault: Path, upstream_repo: Path
) -> None:
    """--depth 0 → full clone (no --depth flag passed to git)."""
    # The upstream has only one commit; --depth 0 should still work.
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "FullRepo",
            "--depth", "0",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "codes" / "FullRepo" / "repo" / ".git").exists()


def test_cli_code_add_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["code", "add", "--help"])
    assert result.exit_code == 0
    assert "--depth" in result.output
    assert "--paper" in result.output


def test_cli_code_group_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["code", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
