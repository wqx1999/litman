"""Tests for ``lit code`` group — M3.1 (add) + M3.2 (list/link/update/rm)."""

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
    bump_repo_updated_at,
    delete_repo,
    derive_repo_name,
    git_pull,
    is_valid_repo_name,
    list_repos,
    make_repo_meta,
    read_repo_meta,
    unbind_repo_from_all_papers,
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


def _make_repo(
    vault: Path,
    repo_name: str,
    upstream: str = "file:///tmp/upstream",
    papers: list[str] | None = None,
    **extra: Any,
) -> Path:
    """Materialize a minimal codes/<repo_name>/ tree with repo-meta.yaml.

    Does NOT create a real git checkout. Tests that exercise ``git_pull`` /
    ``--unshallow`` use the `upstream_repo` fixture and the CLI's `add`
    subcommand to get a real .git/ in place.
    """
    repo_dir = vault / "codes" / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    meta = make_repo_meta(
        name=repo_name,
        upstream=upstream,
        papers=papers or [],
        now="2026-05-11T10:00:00+02:00",
    )
    meta.update(extra)
    write_repo_meta(repo_dir, meta)
    return repo_dir


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


def test_bind_appends_to_both_sides(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    _make_repo(vault, "MyRepo")
    changed = bind_paper_to_repo(vault, "2024_Smith_X", "MyRepo")
    assert changed is True

    paper_meta = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    assert paper_meta["code-clones"] == ["MyRepo"]
    assert paper_meta["updated-at"] != "2026-05-11T10:00:00+02:00"

    # Repo side also picked up the back-reference.
    repo_meta = _yaml_safe.load(
        (vault / "codes" / "MyRepo" / "repo-meta.yaml").read_text()
    )
    assert repo_meta["papers"] == ["2024_Smith_X"]
    assert repo_meta["updated-at"] != "2026-05-11T10:00:00+02:00"


def test_bind_idempotent_returns_false(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X", **{"code-clones": ["MyRepo"]})
    _make_repo(vault, "MyRepo", papers=["2024_Smith_X"])

    seed_paper_updated_at = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )["updated-at"]
    seed_repo_updated_at = _yaml_safe.load(
        (vault / "codes" / "MyRepo" / "repo-meta.yaml").read_text()
    )["updated-at"]

    changed = bind_paper_to_repo(vault, "2024_Smith_X", "MyRepo")
    assert changed is False
    # Neither side should have advanced — both already recorded the binding.
    after_paper = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    after_repo = _yaml_safe.load(
        (vault / "codes" / "MyRepo" / "repo-meta.yaml").read_text()
    )
    assert after_paper["updated-at"] == seed_paper_updated_at
    assert after_repo["updated-at"] == seed_repo_updated_at


def test_bind_half_present_updates_only_missing_side(vault: Path) -> None:
    """Paper already records binding but repo doesn't → repo-side write only."""
    _make_paper(vault, "2024_Smith_X", **{"code-clones": ["MyRepo"]})
    _make_repo(vault, "MyRepo")  # papers=[]
    seed_paper_updated_at = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )["updated-at"]

    changed = bind_paper_to_repo(vault, "2024_Smith_X", "MyRepo")
    assert changed is True

    # Paper side untouched (its updated-at must not have moved).
    after_paper = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    assert after_paper["updated-at"] == seed_paper_updated_at
    # Repo side now has the back-reference.
    after_repo = _yaml_safe.load(
        (vault / "codes" / "MyRepo" / "repo-meta.yaml").read_text()
    )
    assert after_repo["papers"] == ["2024_Smith_X"]


def test_bind_updates_index_json(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    _make_repo(vault, "MyRepo")
    bind_paper_to_repo(vault, "2024_Smith_X", "MyRepo")
    payload = json.loads((vault / "INDEX.json").read_text())
    ids = {p["id"] for p in payload["papers"]}
    assert "2024_Smith_X" in ids


def test_bind_missing_paper_raises(vault: Path) -> None:
    _make_repo(vault, "MyRepo")
    with pytest.raises(PaperNotFoundError):
        bind_paper_to_repo(vault, "2024_Nope_X", "MyRepo")


def test_bind_missing_repo_raises(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    with pytest.raises(CodeError, match="No repo"):
        bind_paper_to_repo(vault, "2024_Smith_X", "NoSuchRepo")


# ---------------------------------------------------------------------------
# unbind_repo_from_all_papers (M3.2 cascade)
# ---------------------------------------------------------------------------


def test_unbind_strips_from_all_referrers(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["X", "Y"]})
    _make_paper(vault, "p2", **{"code-clones": ["X"]})
    _make_paper(vault, "p3", **{"code-clones": ["Y"]})  # not affected

    affected = unbind_repo_from_all_papers(vault, "X")
    assert sorted(affected) == ["p1", "p2"]

    after_p1 = _yaml_safe.load(
        (vault / "papers" / "p1" / "metadata.yaml").read_text()
    )
    after_p2 = _yaml_safe.load(
        (vault / "papers" / "p2" / "metadata.yaml").read_text()
    )
    after_p3 = _yaml_safe.load(
        (vault / "papers" / "p3" / "metadata.yaml").read_text()
    )
    assert after_p1["code-clones"] == ["Y"]
    assert after_p2["code-clones"] == []
    assert after_p3["code-clones"] == ["Y"]  # untouched


def test_unbind_no_referrers_returns_empty(vault: Path) -> None:
    _make_paper(vault, "p1")
    affected = unbind_repo_from_all_papers(vault, "X")
    assert affected == []


# ---------------------------------------------------------------------------
# list_repos
# ---------------------------------------------------------------------------


def test_list_repos_empty(vault: Path) -> None:
    assert list_repos(vault) == []


def test_list_repos_returns_sorted(vault: Path) -> None:
    _make_repo(vault, "ZRepo")
    _make_repo(vault, "ARepo")
    repos = list_repos(vault)
    names = [r["name"] for r in repos]
    assert names == ["ARepo", "ZRepo"]


def test_list_repos_skips_dirs_without_meta(vault: Path) -> None:
    _make_repo(vault, "Good")
    (vault / "codes" / "BareDir").mkdir()
    repos = list_repos(vault)
    assert [r["name"] for r in repos] == ["Good"]


# ---------------------------------------------------------------------------
# delete_repo
# ---------------------------------------------------------------------------


def test_delete_repo_removes_tree(vault: Path) -> None:
    repo_dir = _make_repo(vault, "ToDelete")
    (repo_dir / "extra.txt").write_text("hi")
    assert repo_dir.exists()
    delete_repo(vault, "ToDelete")
    assert not repo_dir.exists()


def test_delete_repo_missing_raises(vault: Path) -> None:
    with pytest.raises(CodeError, match="No repo"):
        delete_repo(vault, "DoesNotExist")


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
    assert "list" in result.output
    assert "link" in result.output
    assert "update" in result.output
    assert "rm" in result.output


# ---------------------------------------------------------------------------
# CLI: lit code list
# ---------------------------------------------------------------------------


def test_cli_code_list_empty(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["code", "list", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "No code repos" in result.output


def test_cli_code_list_shows_all_repos(vault: Path) -> None:
    _make_repo(vault, "RepoA", upstream="https://github.com/a/repo")
    _make_repo(vault, "RepoB", upstream="https://github.com/b/repo")
    runner = CliRunner()
    result = runner.invoke(cli, ["code", "list", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "RepoA" in result.output
    assert "RepoB" in result.output
    assert "2 repo(s)" in result.output


def test_cli_code_list_filter_by_paper(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    _make_repo(vault, "Bound", papers=["2024_Smith_X"])
    _make_repo(vault, "Orphan", papers=[])
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "list",
            "--paper", "2024_Smith_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Bound" in result.output
    assert "Orphan" not in result.output


def test_cli_code_list_orphan_only(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    _make_repo(vault, "Bound", papers=["2024_Smith_X"])
    _make_repo(vault, "Orphan", papers=[])
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "list", "--orphan", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Orphan" in result.output
    assert "Bound" not in result.output


def test_cli_code_list_mutually_exclusive_filters(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "list",
            "--paper", "2024_X_y",
            "--orphan",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)
    assert "mutually exclusive" in str(result.exception)


def test_cli_code_list_paper_filter_missing_paper(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "list",
            "--paper", "2024_Nope_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


# ---------------------------------------------------------------------------
# CLI: lit code link
# ---------------------------------------------------------------------------


def test_cli_code_link_binds(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    _make_repo(vault, "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "link", "MyRepo",
            "--paper", "2024_Smith_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Linked" in result.output

    paper_meta = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    assert paper_meta["code-clones"] == ["MyRepo"]
    repo_meta = _yaml_safe.load(
        (vault / "codes" / "MyRepo" / "repo-meta.yaml").read_text()
    )
    assert repo_meta["papers"] == ["2024_Smith_X"]


def test_cli_code_link_idempotent(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X", **{"code-clones": ["MyRepo"]})
    _make_repo(vault, "MyRepo", papers=["2024_Smith_X"])
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "link", "MyRepo",
            "--paper", "2024_Smith_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output


def test_cli_code_link_missing_paper(vault: Path) -> None:
    _make_repo(vault, "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "link", "MyRepo",
            "--paper", "2024_Nope_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_cli_code_link_missing_repo(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "link", "NoSuchRepo",
            "--paper", "2024_Smith_X",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)


# ---------------------------------------------------------------------------
# CLI: lit code update
# ---------------------------------------------------------------------------


def test_cli_code_update_already_uptodate(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "TestRepo",
            "--library", str(vault),
        ],
    )
    result = runner.invoke(
        cli,
        ["code", "update", "TestRepo", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_cli_code_update_pulls_new_commit(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "TestRepo",
            "--library", str(vault),
        ],
    )

    # Push a new commit to the upstream.
    (upstream_repo / "second.txt").write_text("more\n")
    subprocess.run(
        ["git", "-C", str(upstream_repo), "add", "."], check=True
    )
    subprocess.run(
        ["git", "-C", str(upstream_repo),
         "-c", "user.email=test@example.com",
         "-c", "user.name=test",
         "commit", "-q", "-m", "second"],
        check=True,
    )

    result = runner.invoke(
        cli,
        ["code", "update", "TestRepo", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated" in result.output
    assert (vault / "codes" / "TestRepo" / "repo" / "second.txt").is_file()


def test_cli_code_update_unshallow_tolerates_full_clone(
    vault: Path, upstream_repo: Path
) -> None:
    """--unshallow against an already-full clone is a no-op (not an error).

    Note: ``git clone --depth 1 file://...`` against a local upstream does
    NOT actually produce a shallow clone (git's local-clone optimization
    bypasses the depth flag), so we exercise the "already full → tolerated"
    branch here rather than the true shallow→full promotion.
    """
    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "TestRepo",
            "--library", str(vault),
        ],
    )
    result = runner.invoke(
        cli,
        [
            "code", "update", "TestRepo",
            "--unshallow",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output


def test_cli_code_update_missing_repo(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "update", "NoSuchRepo", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)


def test_cli_code_update_non_git_dir(vault: Path) -> None:
    # Make a repo entry with no .git/ checkout — should fail.
    _make_repo(vault, "FakeRepo")
    (vault / "codes" / "FakeRepo" / "repo").mkdir()  # plain dir, no .git/
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "update", "FakeRepo", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)
    assert "not a git checkout" in str(result.exception)


# ---------------------------------------------------------------------------
# CLI: lit code rm
# ---------------------------------------------------------------------------


def test_cli_code_rm_no_bindings_with_yes(vault: Path) -> None:
    _make_repo(vault, "ToDelete")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "rm", "ToDelete", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Removed" in result.output
    assert not (vault / "codes" / "ToDelete").exists()


def test_cli_code_rm_refuses_with_bindings(vault: Path) -> None:
    _make_paper(vault, "2024_Smith_X", **{"code-clones": ["Bound"]})
    _make_repo(vault, "Bound", papers=["2024_Smith_X"])

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "rm", "Bound", "--yes", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)
    assert "still bound" in str(result.exception)
    # Repo intact, paper bindings untouched.
    assert (vault / "codes" / "Bound").exists()
    paper_meta = _yaml_safe.load(
        (vault / "papers" / "2024_Smith_X" / "metadata.yaml").read_text()
    )
    assert paper_meta["code-clones"] == ["Bound"]


def test_cli_code_rm_cascade_strips_papers(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["X", "Y"]})
    _make_paper(vault, "p2", **{"code-clones": ["X"]})
    _make_repo(vault, "X", papers=["p1", "p2"])

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "rm", "X", "--cascade", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Removed" in result.output
    assert "Unbound from 2 paper(s)" in result.output

    # Repo gone.
    assert not (vault / "codes" / "X").exists()
    # Paper code-clones stripped of X but not Y.
    after_p1 = _yaml_safe.load(
        (vault / "papers" / "p1" / "metadata.yaml").read_text()
    )
    after_p2 = _yaml_safe.load(
        (vault / "papers" / "p2" / "metadata.yaml").read_text()
    )
    assert after_p1["code-clones"] == ["Y"]
    assert after_p2["code-clones"] == []


def test_cli_code_rm_aborts_on_n_prompt(vault: Path) -> None:
    """Without --yes, type 'n' at the prompt → no deletion."""
    _make_repo(vault, "Keep")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "rm", "Keep", "--library", str(vault)],
        input="n\n",
    )
    assert result.exit_code != 0  # click.confirm(abort=True) exits non-zero
    assert (vault / "codes" / "Keep").exists()


def test_cli_code_rm_missing_repo(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "rm", "NoSuchRepo", "--yes", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError)


# ---------------------------------------------------------------------------
# bump_repo_updated_at
# ---------------------------------------------------------------------------


def test_bump_updated_at_changes_timestamp(vault: Path) -> None:
    _make_repo(vault, "X")
    before = _yaml_safe.load(
        (vault / "codes" / "X" / "repo-meta.yaml").read_text()
    )["updated-at"]
    bump_repo_updated_at(vault, "X")
    after = _yaml_safe.load(
        (vault / "codes" / "X" / "repo-meta.yaml").read_text()
    )["updated-at"]
    assert after != before


# ---------------------------------------------------------------------------
# git_pull helper unit
# ---------------------------------------------------------------------------


def test_git_pull_returns_status_dict(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "code", "add", str(upstream_repo),
            "--name", "TestRepo",
            "--library", str(vault),
        ],
    )
    status = git_pull(vault / "codes" / "TestRepo" / "repo")
    assert status["changed"] is False
    assert status["before_sha"] == status["after_sha"]
    assert len(status["before_sha"]) == 40  # full SHA-1


def test_git_pull_rejects_non_git_dir(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(CodeError, match="not a git checkout"):
        git_pull(plain)
