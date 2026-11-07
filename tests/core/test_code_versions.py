"""Code checkouts at a recorded commit: restore, move, read, record.

Two-way ``lit sync`` brings every machine's ``codes/<name>/repo/`` to the
commit ``repo-meta.yaml`` records; these are the git-level pieces it uses,
and ``lit code add`` / ``update`` / ``restore-all``, which record or follow
the commit too. Commits are compared by id, never file bytes, so git's line
ending conversion on Windows changes nothing here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import locking
from litman.core.code import (
    CheckoutState,
    GitStepError,
    checkout_state,
    clone_repo,
    code_folder_kind,
    git_pull,
    make_repo_meta,
    move_to_commit,
    read_head,
    record_repo_heads,
    repo_changes,
    restore_at_commit,
    valid_branch,
    valid_commit,
    write_repo_meta,
)
from litman.core.library import create_vault
from litman.exceptions import CodeError
from tests._git_clone import git
from tests._git_upstream import Upstream, make_bare_upstream

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed; CI has it."
)


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return make_bare_upstream(tmp_path / "upstream")


def _shallow(repo: Path) -> bool:
    return git("rev-parse", "--is-shallow-repository", cwd=repo).strip() == "true"


def _vault(tmp_path: Path) -> Path:
    parent = tmp_path / "lib"
    parent.mkdir()
    return create_vault(parent)


def _lit(vault: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, [*args, "--library", str(vault)])


def _meta(vault: Path, name: str) -> dict[str, object]:
    text = (vault / "codes" / name / "repo-meta.yaml").read_text(encoding="utf-8")
    return dict(YAML(typ="safe").load(text))


# --- restore at a commit -------------------------------------------------------


@needs_git
def test_a_checkout_is_restored_at_the_recorded_commit_and_still_pulls(
    tmp_path: Path, upstream: Upstream
) -> None:
    """Only the recorded commit is fetched, the branch follows origin's, and a
    later pull reaches the tip: git can connect the two."""
    c1, _c2, c3 = upstream.commits
    repo = tmp_path / "repo"

    restore_at_commit(upstream.url, repo, c1, "main", depth=1)

    assert read_head(repo) == (c1, "main")
    assert checkout_state(repo) == CheckoutState(changed=False, untracked=False, unpushed=0)
    assert git("status", "-sb", cwd=repo).splitlines()[0] == "## main...origin/main"
    assert _shallow(repo)
    with pytest.raises(subprocess.CalledProcessError):
        git("cat-file", "-e", f"{c3}^{{commit}}", cwd=repo)
    assert git_pull(repo, depth=1)["after_sha"] == c3
    assert read_head(repo) == (c3, "main")


@needs_git
def test_a_detached_record_is_restored_detached_and_counts_as_clean(
    tmp_path: Path, upstream: Upstream
) -> None:
    """No remote branch names a commit fetched by its id: litman's own ref
    does, so neither the sync nor `lit rm` takes it for a commit of the user's.
    With the whole history fetched there is no shallow edge to tell by."""
    c2 = upstream.commits[1]
    repo = tmp_path / "codes" / "tool" / "repo"

    restore_at_commit(upstream.url, repo, c2, None, depth=0)

    assert read_head(repo) == (c2, None)
    assert checkout_state(repo) == CheckoutState(changed=False, untracked=False, unpushed=0)
    assert repo_changes(repo.parent) == ("clean", "")


@needs_git
def test_a_commit_the_upstream_lacks_is_not_restored_and_leaves_nothing(
    tmp_path: Path, upstream: Upstream
) -> None:
    repo = tmp_path / "repo"

    with pytest.raises(GitStepError, match=r"git could not fetch commit 0000000: fatal:"):
        restore_at_commit(upstream.url, repo, "0" * 40, "main", depth=1)

    assert not repo.exists()


@needs_git
def test_a_clone_at_a_terminal_shows_git_progress(
    tmp_path: Path, upstream: Upstream, capsys: pytest.CaptureFixture[str]
) -> None:
    clone_repo(upstream.url, tmp_path / "repo", depth=1, progress=True)

    assert "Cloning into" in capsys.readouterr().err
    assert read_head(tmp_path / "repo") == (upstream.commits[2], "main")


# --- move to a commit ----------------------------------------------------------


@needs_git
def test_a_shallow_checkout_moves_back_and_forth_and_stays_shallow(
    tmp_path: Path, upstream: Upstream
) -> None:
    c1, c2, c3 = upstream.commits
    repo = tmp_path / "repo"
    clone_repo(upstream.url, repo, depth=1)

    move_to_commit(repo, c1, "main", depth=1)
    assert read_head(repo) == (c1, "main")
    move_to_commit(repo, c2, "main", depth=1)

    assert read_head(repo) == (c2, "main")
    assert _shallow(repo)
    assert checkout_state(repo) == CheckoutState(changed=False, untracked=False, unpushed=0)
    # c2 and origin/main's c3 are two pieces of history git cannot connect:
    # the pull follows the tip instead of fast-forwarding.
    assert git_pull(repo, depth=1)["after_sha"] == c3


@needs_git
def test_a_full_checkout_stays_full_when_it_moves(tmp_path: Path, upstream: Upstream) -> None:
    repo = tmp_path / "repo"
    clone_repo(upstream.url, repo, depth=0)

    move_to_commit(repo, upstream.commits[0], "main", depth=1)

    assert read_head(repo) == (upstream.commits[0], "main")
    assert not _shallow(repo)


@needs_git
def test_a_pull_leaves_a_moved_shallow_checkout_with_changes_where_it_is(
    tmp_path: Path, upstream: Upstream
) -> None:
    c1 = upstream.commits[0]
    repo = tmp_path / "repo"
    clone_repo(upstream.url, repo, depth=1)
    move_to_commit(repo, c1, "main", depth=1)
    (repo / "code.py").write_bytes(b"print('mine')\n")

    with pytest.raises(CodeError, match="git pull --ff-only failed"):
        git_pull(repo, depth=1)

    assert read_head(repo) == (c1, "main")
    assert (repo / "code.py").read_bytes() == b"print('mine')\n"


# --- what a checkout holds -------------------------------------------------------


@needs_git
def test_what_a_checkout_holds_of_the_users_own(tmp_path: Path, upstream: Upstream) -> None:
    repo = tmp_path / "repo"
    clone_repo(upstream.url, repo, depth=1)
    original = (repo / "code.py").read_bytes()

    (repo / "code.py").write_bytes(b"print('edited')\n")
    edited = checkout_state(repo)
    (repo / "code.py").write_bytes(original)
    (repo / ".DS_Store").write_bytes(b"\0")
    dropping_only = checkout_state(repo)
    (repo / "results.csv").write_bytes(b"1,2\n")
    untracked = checkout_state(repo)
    git("add", "results.csv", cwd=repo)
    git("commit", "-q", "-m", "mine", cwd=repo)
    committed = checkout_state(repo)
    (repo / "more.csv").write_bytes(b"3,4\n")
    git("add", "more.csv", cwd=repo)
    git("commit", "-q", "-m", "more", cwd=repo)
    two_commits = checkout_state(repo)

    assert edited == CheckoutState(changed=True, untracked=False, unpushed=0)
    assert edited.has_work
    assert dropping_only == CheckoutState(changed=False, untracked=False, unpushed=0)
    assert untracked == CheckoutState(changed=False, untracked=True, unpushed=0)
    assert not untracked.has_work
    assert committed == CheckoutState(changed=False, untracked=False, unpushed=1)
    assert committed.has_work
    assert two_commits == CheckoutState(changed=False, untracked=False, unpushed=2)


@needs_git
def test_a_commit_fetched_by_hand_into_a_shallow_clone_is_not_the_users(
    tmp_path: Path, upstream: Upstream
) -> None:
    """No ref names a commit fetched by its id, but it sits at the edge of the
    shallow history, where only a fetch puts one. A commit made on top is the
    user's."""
    c1 = upstream.commits[0]
    repo = tmp_path / "repo"
    clone_repo(upstream.url, repo, depth=1)
    git("fetch", "-q", "--depth", "1", "origin", c1, cwd=repo)
    git("checkout", "-q", "--detach", c1, cwd=repo)

    fetched = checkout_state(repo)
    (repo / "mine.py").write_bytes(b"print(2)\n")
    git("add", "mine.py", cwd=repo)
    git("commit", "-q", "-m", "mine", cwd=repo)

    assert fetched == CheckoutState(changed=False, untracked=False, unpushed=0)
    assert checkout_state(repo) == CheckoutState(changed=False, untracked=False, unpushed=1)


# --- recording the commit ---------------------------------------------------------


@needs_git
def test_the_commit_and_branch_go_right_after_the_upstream_and_only_when_new(
    tmp_path: Path,
) -> None:
    vault = create_vault(tmp_path)
    folder = vault / "codes" / "tool"
    folder.mkdir(parents=True)
    write_repo_meta(folder, make_repo_meta("tool", "https://x/y", now="2024-01-01T00:00:00Z"))

    first = record_repo_heads(vault, {"tool": ("a" * 40, "main")}, op_id="t1")
    written = (folder / "repo-meta.yaml").read_bytes()
    again = record_repo_heads(vault, {"tool": ("a" * 40, "main")}, op_id="t2")

    assert first == ["tool"] and again == []
    assert (folder / "repo-meta.yaml").read_bytes() == written
    meta = _meta(vault, "tool")
    assert list(meta)[:4] == ["name", "upstream", "commit", "branch"]
    assert (meta["commit"], meta["branch"]) == ("a" * 40, "main")
    assert meta["updated-at"] != "2024-01-01T00:00:00Z"


@needs_git
def test_lit_code_add_and_update_record_the_commit_and_branch(
    tmp_path: Path, upstream: Upstream
) -> None:
    vault = _vault(tmp_path)

    added = _lit(vault, "code", "add", upstream.url, "--name", "tool")
    after_add = _meta(vault, "tool")
    c4 = upstream.commit("c4")
    updated = _lit(vault, "code", "update", "tool")

    assert added.exit_code == 0, added.output
    assert (after_add["commit"], after_add["branch"]) == (upstream.commits[2], "main")
    assert updated.exit_code == 0, updated.output
    assert (_meta(vault, "tool")["commit"], _meta(vault, "tool")["branch"]) == (c4, "main")


@needs_git
def test_restore_all_fetches_the_recorded_commit(tmp_path: Path, upstream: Upstream) -> None:
    vault = _vault(tmp_path)
    assert _lit(vault, "code", "add", upstream.url, "--name", "tool").exit_code == 0
    c1 = upstream.commits[0]
    record_repo_heads(vault, {"tool": (c1, "main")}, op_id="t")
    locking.rmtree(vault / "codes" / "tool" / "repo")

    restored = _lit(vault, "code", "restore-all")

    assert restored.exit_code == 0, restored.output
    assert read_head(vault / "codes" / "tool" / "repo") == (c1, "main")


# --- code folders -------------------------------------------------------------------


@needs_git
def test_lit_code_add_takes_an_empty_code_folder_but_not_a_left_behind_checkout(
    tmp_path: Path, upstream: Upstream
) -> None:
    vault = _vault(tmp_path)
    empty = vault / "codes" / "tool"
    (empty / "repo").mkdir(parents=True)
    (empty / ".DS_Store").write_bytes(b"\0")
    kept = vault / "codes" / "other" / "repo"
    kept.mkdir(parents=True)
    (kept / "train.py").write_bytes(b"print(1)\n")

    taken = _lit(vault, "code", "add", upstream.url, "--name", "tool")
    refused = _lit(vault, "code", "add", upstream.url, "--name", "other")

    assert taken.exit_code == 0, taken.output
    assert read_head(empty / "repo") == (upstream.commits[2], "main")
    assert refused.exit_code != 0
    assert "Repo folder already exists" in str(refused.exception)
    assert (kept / "train.py").read_bytes() == b"print(1)\n"


def test_what_a_code_folder_holds(tmp_path: Path) -> None:
    def folder(name: str, files: dict[str, bytes], dirs: tuple[str, ...] = ()) -> Path:
        root = tmp_path / name
        root.mkdir()
        for rel in dirs:
            (root / rel).mkdir(parents=True)
        for rel, data in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
        return root

    assert code_folder_kind(folder("repo", {"repo-meta.yaml": b"name: x\n"})) == "repo"
    assert code_folder_kind(folder("left", {"repo/train.py": b"1\n"})) == "left-behind"
    assert code_folder_kind(folder("left-git", {"repo/.git/HEAD": b"x\n"})) == "left-behind"
    assert code_folder_kind(folder("empty", {".DS_Store": b"\0"}, ("repo/sub",))) == "empty"
    assert code_folder_kind(folder("bare", {})) == "empty"
    assert code_folder_kind(folder("notes", {"notes.md": b"# x\n"})) == "broken"
    assert code_folder_kind(folder("both", {"notes.md": b"#\n", "repo/a": b"1\n"})) == "broken"
    assert code_folder_kind(folder("file", {"repo": b"not a folder\n"})) == "broken"


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        ("main", True),
        ("feature/x-1.2", True),
        ("-upload-pack=evil", False),
        ("a..b", False),
        ("HEAD", False),
        ("x.lock", False),
        (".hidden", False),
        ("with space", False),
        ("a~1", False),
        ("ends/", False),
        (None, False),
        (7, False),
    ],
)
def test_a_branch_name_from_another_machine_is_checked(value: object, kept: bool) -> None:
    assert valid_branch(value) == (value if kept else None)


def test_a_commit_id_is_checked() -> None:
    assert valid_commit("a" * 40) == "a" * 40
    assert valid_commit("b" * 64) == "b" * 64
    assert valid_commit("A" * 40) is None
    assert valid_commit("a" * 39) is None
    assert valid_commit("-" + "a" * 39) is None
    assert valid_commit(None) is None
