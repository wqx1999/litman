"""Tests for ``lit code`` group — M3.1 (add) + M3.2 (list/link/update/rm)."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.code import (
    RestoreItem,
    RestoreReport,
    bind_paper_to_repo,
    bump_repo_updated_at,
    delete_repo,
    derive_repo_name,
    find_orphan_code_refs,
    git_pull,
    is_valid_repo_name,
    list_repos,
    make_repo_meta,
    read_repo_meta,
    restore_missing_repos,
    unbind_paper_from_repo,
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
    """A real local git repo every URL-branch test clones from a file:// URL.

    M3.4 changed ``lit code add`` to route bare local paths through the new
    local-import branch. URL-branch tests that need a "real git source" still
    use this fixture, but pass ``f"file://{upstream_repo}"`` to keep
    exercising the ``git clone`` path. The fixture itself is unchanged.
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
# unbind_paper_from_repo (single-edge unbind, inverse of bind)
# ---------------------------------------------------------------------------


def test_unbind_drops_both_sides_keeps_other_referrers(vault: Path) -> None:
    # Repo shared by A and B (1:N). Unbinding A clears A<->repo on BOTH sides
    # and leaves B's binding (and the clone directory) intact.
    _make_paper(vault, "2024_A", **{"code-clones": ["MyRepo"]})
    _make_paper(vault, "2024_B", **{"code-clones": ["MyRepo"]})
    _make_repo(vault, "MyRepo", papers=["2024_A", "2024_B"])

    changed = unbind_paper_from_repo(vault, "2024_A", "MyRepo")
    assert changed is True

    a_meta = _yaml_safe.load(
        (vault / "papers" / "2024_A" / "metadata.yaml").read_text()
    )
    assert a_meta["code-clones"] == []
    repo_meta = _yaml_safe.load(
        (vault / "codes" / "MyRepo" / "repo-meta.yaml").read_text()
    )
    assert repo_meta["papers"] == ["2024_B"]
    b_meta = _yaml_safe.load(
        (vault / "papers" / "2024_B" / "metadata.yaml").read_text()
    )
    assert b_meta["code-clones"] == ["MyRepo"]
    assert (vault / "codes" / "MyRepo").is_dir()  # clone kept


def test_unbind_idempotent_returns_false(vault: Path) -> None:
    _make_paper(vault, "2024_A")  # code-clones: []
    _make_repo(vault, "MyRepo")   # papers: []
    seed = _yaml_safe.load(
        (vault / "papers" / "2024_A" / "metadata.yaml").read_text()
    )["updated-at"]
    changed = unbind_paper_from_repo(vault, "2024_A", "MyRepo")
    assert changed is False
    after = _yaml_safe.load(
        (vault / "papers" / "2024_A" / "metadata.yaml").read_text()
    )["updated-at"]
    assert after == seed


def test_unbind_tolerates_missing_repo_cleans_paper_side(vault: Path) -> None:
    # Dangling forward edge: paper lists a repo whose codes/<name>/ is gone.
    # unbind still cleans the paper side, repairing the dangling reference.
    _make_paper(vault, "2024_A", **{"code-clones": ["GoneRepo"]})
    # No _make_repo → codes/GoneRepo/ does not exist.
    changed = unbind_paper_from_repo(vault, "2024_A", "GoneRepo")
    assert changed is True
    a_meta = _yaml_safe.load(
        (vault / "papers" / "2024_A" / "metadata.yaml").read_text()
    )
    assert a_meta["code-clones"] == []


def test_unbind_missing_paper_raises(vault: Path) -> None:
    _make_repo(vault, "MyRepo", papers=["2024_A"])
    with pytest.raises(PaperNotFoundError):
        unbind_paper_from_repo(vault, "2024_Nope_X", "MyRepo")


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


def test_delete_repo_wraps_rmtree_failure_in_codeerror(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A torn rmtree must surface as a CodeError pointing at health-check.

    The caller strips the paper-side code-clones bindings (atomically,
    committed) *before* delete_repo runs, so a silently half-deleted
    directory would leave a dangling clone (invariant #12). Assert the
    failure is reported with a health-check hint, not swallowed.
    """
    _make_repo(vault, "ToDelete")

    def boom_rmtree(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("file locked")

    monkeypatch.setattr("litman.core.code.shutil.rmtree", boom_rmtree)
    with pytest.raises(CodeError, match="health-check"):
        delete_repo(vault, "ToDelete")


def test_clear_readonly_clears_bit_and_retries(tmp_path: Path) -> None:
    """The rmtree onexc handler chmods the path writable, then retries.

    Locks the cross-platform contract: on Windows a read-only ``.git``
    object makes ``os.unlink`` raise, and the handler must clear the write
    bit and re-invoke the failed op rather than give up.
    """
    from litman.core.code import _clear_readonly

    target = tmp_path / "ro.txt"
    target.write_text("x")
    target.chmod(stat.S_IREAD)

    retried: list[Path] = []

    def retry(p: Path) -> None:
        retried.append(p)
        p.unlink()

    _clear_readonly(retry, target, None)

    assert retried == [target]
    assert not target.exists()


# ---------------------------------------------------------------------------
# CLI: lit code add
# ---------------------------------------------------------------------------


def test_cli_code_add_creates_clone(
    vault: Path, upstream_repo: Path
) -> None:
    upstream_url = f"file://{upstream_repo}"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", upstream_url,
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
    assert meta["upstream"] == upstream_url
    assert meta["papers"] == []
    assert meta["framework"] is None


def test_cli_code_add_auto_derives_name(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    # file:// URL — derive_repo_name takes the last segment ("upstream"),
    # stripping any trailing ".git" if present (none here).
    result = runner.invoke(
        cli,
        ["code", "add", f"file://{upstream_repo}", "--library", str(vault)],
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
            "code", "add", f"file://{upstream_repo}",
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
        "code", "add", f"file://{upstream_repo}",
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
            "code", "add", f"file://{upstream_repo}",
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
    bogus = "file:///nonexistent/path/that/is/not/a/repo"
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
            "code", "add", f"file://{upstream_repo}",
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
            "code", "add", f"file://{upstream_repo}",
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
# CLI: lit code unlink
# ---------------------------------------------------------------------------


def test_cli_code_unlink_drops_both_sides(vault: Path) -> None:
    _make_paper(vault, "2024_A", **{"code-clones": ["MyRepo"]})
    _make_paper(vault, "2024_B", **{"code-clones": ["MyRepo"]})
    _make_repo(vault, "MyRepo", papers=["2024_A", "2024_B"])
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "unlink", "MyRepo",
            "--paper", "2024_A",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Unlinked" in result.output

    a_meta = _yaml_safe.load(
        (vault / "papers" / "2024_A" / "metadata.yaml").read_text()
    )
    assert a_meta["code-clones"] == []
    repo_meta = _yaml_safe.load(
        (vault / "codes" / "MyRepo" / "repo-meta.yaml").read_text()
    )
    assert repo_meta["papers"] == ["2024_B"]
    assert (vault / "codes" / "MyRepo").is_dir()  # clone kept


def test_cli_code_unlink_idempotent(vault: Path) -> None:
    _make_paper(vault, "2024_A")
    _make_repo(vault, "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "unlink", "MyRepo",
            "--paper", "2024_A",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output


def test_cli_code_link_accepts_fuzzy_paper(vault: Path) -> None:
    """M11 smoke: --paper accepts a unique substring of the id."""
    _make_paper(vault, "2024_Pandi_Cellfree")
    _make_repo(vault, "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "link", "MyRepo",
            "--paper", "Pandi",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output


def test_cli_code_link_accepts_paper_doi(vault: Path) -> None:
    """M11 smoke: --paper-doi reverse-looks-up the paper."""
    _make_paper(vault, "2024_Foo_Bar", doi="10.5/foo")
    _make_repo(vault, "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "link", "MyRepo",
            "--paper-doi", "10.5/foo",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output


def test_cli_code_add_accepts_fuzzy_paper(
    vault: Path, upstream_repo: Path
) -> None:
    """M11 smoke: code add --paper accepts a unique substring of the id."""
    _make_paper(vault, "2024_Pandi_Cellfree")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", f"file://{upstream_repo}",
            "--name", "MyRepo",
            "--paper", "Pandi",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    paper_meta = _yaml_safe.load(
        (vault / "papers" / "2024_Pandi_Cellfree" / "metadata.yaml").read_text()
    )
    assert paper_meta["code-clones"] == ["MyRepo"]


def test_cli_code_list_accepts_fuzzy_paper_filter(vault: Path) -> None:
    """M11 smoke: code list --paper accepts a unique substring."""
    _make_paper(vault, "2024_Pandi_Cellfree")
    _make_repo(vault, "Bound", papers=["2024_Pandi_Cellfree"])
    _make_repo(vault, "Orphan")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "list",
            "--paper", "Pandi",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Bound" in result.output
    assert "Orphan" not in result.output


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
            "code", "add", f"file://{upstream_repo}",
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
            "code", "add", f"file://{upstream_repo}",
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
            "code", "add", f"file://{upstream_repo}",
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
            "code", "add", f"file://{upstream_repo}",
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


# ---------------------------------------------------------------------------
# M3.3 — find_orphan_code_refs
# ---------------------------------------------------------------------------


def test_find_orphan_refs_none_when_all_meta_present(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["RepoA"]})
    _make_repo(vault, "RepoA")
    assert find_orphan_code_refs(vault) == []


def test_find_orphan_refs_flags_missing_meta(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["GhostRepo"]})
    # No codes/GhostRepo/ at all.
    assert find_orphan_code_refs(vault) == [("p1", "GhostRepo")]


def test_find_orphan_refs_partial_some_meta_missing(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["RepoA", "GhostRepo"]})
    _make_repo(vault, "RepoA")
    # GhostRepo has no meta -> orphan; RepoA is fine.
    assert find_orphan_code_refs(vault) == [("p1", "GhostRepo")]


def test_find_orphan_refs_deduped_across_papers(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["GhostRepo"]})
    _make_paper(vault, "p2", **{"code-clones": ["GhostRepo"]})
    refs = find_orphan_code_refs(vault)
    assert refs == [("p1", "GhostRepo"), ("p2", "GhostRepo")]
    # Each (paper_id, repo_name) appears exactly once.
    assert len(refs) == len(set(refs))


def test_find_orphan_refs_ignores_non_string_entries(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["GhostRepo", 42, None, ""]})
    # Only the string "GhostRepo" counts as a ref; others are silently skipped.
    assert find_orphan_code_refs(vault) == [("p1", "GhostRepo")]


def test_find_orphan_refs_codes_dir_only_no_papers(vault: Path) -> None:
    _make_repo(vault, "RepoA")
    # No papers -> nothing to flag.
    assert find_orphan_code_refs(vault) == []


# ---------------------------------------------------------------------------
# M3.3 — RestoreReport
# ---------------------------------------------------------------------------


def test_restore_report_counts_and_is_clean() -> None:
    report = RestoreReport(
        items=[
            RestoreItem("a", "u1", "restored", "ok"),
            RestoreItem("b", "u2", "skipped", "already present"),
            RestoreItem("c", "u3", "failed", "boom"),
            RestoreItem("d", "u4", "restored", "ok"),
        ],
        orphan_refs=[],
    )
    assert report.restored == 2
    assert report.skipped == 1
    assert report.failed == 1
    assert not report.is_clean  # one failure


def test_restore_report_clean_when_no_failures_no_orphans() -> None:
    report = RestoreReport(
        items=[
            RestoreItem("a", "u1", "restored", "ok"),
            RestoreItem("b", "u2", "skipped", "already present"),
        ],
        orphan_refs=[],
    )
    assert report.is_clean


def test_restore_report_dirty_when_orphan_present() -> None:
    report = RestoreReport(
        items=[RestoreItem("a", "u1", "skipped", "ok")],
        orphan_refs=[("p1", "Ghost")],
    )
    assert report.failed == 0
    assert not report.is_clean


# ---------------------------------------------------------------------------
# M3.3 — restore_missing_repos
# ---------------------------------------------------------------------------


def test_restore_empty_vault(vault: Path) -> None:
    report = restore_missing_repos(vault)
    assert report.items == []
    assert report.orphan_refs == []
    assert report.is_clean


def test_restore_skips_when_repo_already_present(
    vault: Path, upstream_repo: Path
) -> None:
    """Repo with repo/ already cloned -> skipped, no re-clone attempt."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "add", f"file://{upstream_repo}", "--name", "Alive",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    repo_dir = vault / "codes" / "Alive" / "repo"
    assert repo_dir.is_dir()

    # Capture an inode so we can confirm restore did not delete + re-clone.
    before_stat = (repo_dir / ".git" / "HEAD").stat().st_ino

    report = restore_missing_repos(vault)
    assert len(report.items) == 1
    assert report.items[0].status == "skipped"
    assert report.items[0].name == "Alive"
    after_stat = (repo_dir / ".git" / "HEAD").stat().st_ino
    assert before_stat == after_stat  # untouched


def test_restore_reclones_missing_repo(
    vault: Path, upstream_repo: Path
) -> None:
    """codes/<name>/repo-meta.yaml present, repo/ gone -> git clone runs."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "add", f"file://{upstream_repo}", "--name", "Gone",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    # Simulate cross-machine state: repo-meta.yaml survives, repo/ does not.
    shutil.rmtree(vault / "codes" / "Gone" / "repo")
    assert not (vault / "codes" / "Gone" / "repo").exists()

    report = restore_missing_repos(vault)
    assert len(report.items) == 1
    assert report.items[0].status == "restored"
    assert report.items[0].name == "Gone"
    assert (vault / "codes" / "Gone" / "repo" / ".git").exists()
    assert (vault / "codes" / "Gone" / "repo" / "README.md").is_file()


def test_restore_mixed_present_and_missing(
    vault: Path, upstream_repo: Path
) -> None:
    """Mixed vault: one repo present, one missing -> both appear in report."""
    runner = CliRunner()
    for name in ("Keep", "Drop"):
        r = runner.invoke(
            cli,
            ["code", "add", f"file://{upstream_repo}", "--name", name,
             "--library", str(vault)],
        )
        assert r.exit_code == 0, r.output
    shutil.rmtree(vault / "codes" / "Drop" / "repo")

    report = restore_missing_repos(vault)
    by_name = {it.name: it for it in report.items}
    assert by_name["Keep"].status == "skipped"
    assert by_name["Drop"].status == "restored"
    assert (vault / "codes" / "Drop" / "repo" / ".git").exists()


def test_restore_failed_when_upstream_empty(vault: Path) -> None:
    """repo-meta.yaml.upstream empty -> failed, no abort, no clone."""
    repo_dir = vault / "codes" / "NoURL"
    repo_dir.mkdir(parents=True)
    meta = make_repo_meta(name="NoURL", upstream="")
    write_repo_meta(repo_dir, meta)

    report = restore_missing_repos(vault)
    assert len(report.items) == 1
    assert report.items[0].status == "failed"
    assert "empty" in report.items[0].detail
    assert not (repo_dir / "repo").exists()


def test_restore_failed_on_bad_url(vault: Path) -> None:
    """Non-existent upstream URL -> failed, loop survives."""
    _make_repo(vault, "BadURL", upstream="/nonexistent/path/to/repo")
    # No repo/ subdir yet (just bare meta), so restore will try to clone.

    report = restore_missing_repos(vault)
    assert len(report.items) == 1
    assert report.items[0].status == "failed"
    assert not (vault / "codes" / "BadURL" / "repo").exists()


def test_restore_isolates_failures(
    vault: Path, upstream_repo: Path
) -> None:
    """One bad URL does not block another repo from restoring."""
    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["code", "add", f"file://{upstream_repo}", "--name", "Good",
         "--library", str(vault)],
    )
    assert r.exit_code == 0, r.output
    shutil.rmtree(vault / "codes" / "Good" / "repo")

    _make_repo(vault, "Bad", upstream="/nonexistent/path")

    report = restore_missing_repos(vault)
    by_name = {it.name: it for it in report.items}
    assert by_name["Good"].status == "restored"
    assert by_name["Bad"].status == "failed"


def test_restore_dry_run_does_not_clone(vault: Path) -> None:
    _make_repo(vault, "DryRunTarget", upstream="/nonexistent/path")

    report = restore_missing_repos(vault, dry_run=True)
    assert len(report.items) == 1
    assert report.items[0].status == "restored"
    assert "dry-run" in report.items[0].detail
    # Crucially, no actual clone attempt happened, so even a bad URL is "OK".
    assert not (vault / "codes" / "DryRunTarget" / "repo").exists()


def test_restore_attaches_orphan_refs(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["GhostRepo"]})
    _make_repo(vault, "RealRepo")  # different repo; not orphan

    report = restore_missing_repos(vault)
    assert report.orphan_refs == [("p1", "GhostRepo")]
    # RealRepo's repo/ is also missing but has a meta with empty upstream
    # (the default in _make_repo). So expect 1 failed item.
    assert len(report.items) == 1
    assert report.items[0].name == "RealRepo"


def test_restore_respects_depth_param(
    vault: Path, upstream_repo: Path
) -> None:
    """--depth 0 forwards to clone_repo for full history."""
    _make_repo(vault, "FullCloneTarget", upstream=str(upstream_repo))

    report = restore_missing_repos(vault, depth=0)
    assert report.items[0].status == "restored"
    repo_dir = vault / "codes" / "FullCloneTarget" / "repo"
    assert (repo_dir / ".git").exists()
    # depth=0 = no --depth flag -> not a shallow clone. Cloning from a local
    # path bypasses shallow semantics anyway (git local-clone optimization),
    # so we only assert the clone succeeded; depth semantics over a real
    # network are covered indirectly by lit code update --unshallow tests.


# ---------------------------------------------------------------------------
# M3.3 — CLI lit code restore-all
# ---------------------------------------------------------------------------


def test_cli_code_restore_all_empty_vault(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["code", "restore-all", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "No code repos" in result.output


def test_cli_code_restore_all_happy_path(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["code", "add", f"file://{upstream_repo}", "--name", "BackMe",
         "--library", str(vault)],
    )
    assert r.exit_code == 0, r.output
    shutil.rmtree(vault / "codes" / "BackMe" / "repo")

    result = runner.invoke(
        cli, ["code", "restore-all", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Restored" in result.output
    assert "1 restored" in result.output
    assert (vault / "codes" / "BackMe" / "repo" / ".git").exists()


def test_cli_code_restore_all_dry_run_does_not_clone(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["code", "add", f"file://{upstream_repo}", "--name", "Preview",
         "--library", str(vault)],
    )
    assert r.exit_code == 0, r.output
    shutil.rmtree(vault / "codes" / "Preview" / "repo")

    result = runner.invoke(
        cli, ["code", "restore-all", "--dry-run", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    # Nothing was actually cloned.
    assert not (vault / "codes" / "Preview" / "repo").exists()


def test_cli_code_restore_all_failure_exits_one(vault: Path) -> None:
    _make_repo(vault, "BadURL", upstream="/nonexistent/path/to/repo")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["code", "restore-all", "--library", str(vault)]
    )
    assert result.exit_code == 1, result.output
    assert "Failed" in result.output
    assert "1 failed" in result.output


def test_cli_code_restore_all_orphan_ref_exits_one(vault: Path) -> None:
    _make_paper(vault, "p1", **{"code-clones": ["GhostRepo"]})
    runner = CliRunner()
    result = runner.invoke(
        cli, ["code", "restore-all", "--library", str(vault)]
    )
    assert result.exit_code == 1, result.output
    assert "Orphan references" in result.output
    assert "GhostRepo" in result.output
    assert "p1" in result.output


def test_cli_code_restore_all_skips_already_present(
    vault: Path, upstream_repo: Path
) -> None:
    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["code", "add", f"file://{upstream_repo}", "--name", "Untouched",
         "--library", str(vault)],
    )
    assert r.exit_code == 0, r.output

    result = runner.invoke(
        cli, ["code", "restore-all", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output
    assert "1 skipped" in result.output


def test_cli_code_restore_all_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["code", "restore-all", "--help"])
    assert result.exit_code == 0
    assert "Cross-machine recovery" in result.output
    assert "--depth" in result.output
    assert "--dry-run" in result.output


def test_clone_repo_inserts_double_dash_before_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review A5: a url beginning with '-' must be passed as a positional after
    # '--', never interpreted as an injected git flag. restore_missing_repos
    # clones from the (unwhitelisted) repo-meta upstream, so a cloud-sync
    # conflict copy could begin with '-'.
    from litman.core.code import clone_repo

    captured: dict[str, list[str]] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    clone_repo("--upload-pack=evil", tmp_path / "dest")

    cmd = captured["cmd"]
    assert "--" in cmd
    sep = cmd.index("--")
    # The url is the positional immediately after '--' (so git never parses it
    # as an option), and nothing option-like precedes it unterminated.
    assert cmd[sep + 1] == "--upload-pack=evil"
