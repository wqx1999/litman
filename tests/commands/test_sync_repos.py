"""Code repos follow the library by version across machines (two-way ``lit sync``).

Machines A and B sync one library through a local folder standing in for the
cloud, and a bare git repository is the upstream of every code repo. A
checkout never travels as files: each machine brings its own to the commit
``repo-meta.yaml`` records, and says what it could not do. Commits are
compared by id, never file bytes (git converts line endings on Windows), in
the short temporary folder of the bisync tests: rclone names its record
files after both synced paths.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.console import Console
from ruamel.yaml import YAML

from litman.commands import sync as sync_cmd
from litman.core import locking
from litman.core.checks.maintenance import check_code_clone_integrity
from litman.core.code import code_folder_kind, read_head
from litman.core.document import list_papers
from tests._git_clone import git
from tests._git_upstream import Upstream, make_bare_upstream
from tests.commands.test_sync_bisync import (
    _edit_metadata,
    _lit,
    _machine,
    _sync,
    _write_paper,
    needs_rclone,
)

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed; CI has it."
)
pytestmark = [needs_rclone, needs_git]


# The three fixtures of tests/commands/test_sync_bisync.py these tests share.


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A short temporary folder, in place of pytest's own (see the bisync tests)."""
    path = Path(tempfile.mkdtemp(prefix="lt-")).resolve()
    yield path
    locking.rmtree(path, ignore_errors=True)


@pytest.fixture
def cloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The cloud folder; ``fake-cloud:<it>/lib`` is the library on the remote."""
    conf = tmp_path / "rclone.conf"
    conf.write_text("[fake-cloud]\ntype = local\nnounc = false\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    storage = tmp_path / "cloud"
    storage.mkdir()
    return storage


@pytest.fixture(autouse=True)
def _wide_consoles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let panels and rclone lines print unwrapped, so phrases stay whole."""
    monkeypatch.setattr(sync_cmd, "console", Console(width=400))
    monkeypatch.setattr(sync_cmd, "_rclone_console", Console(stderr=True, width=400))

_LEFT_BEHIND = (
    "codes/tool/repo/ stays on this machine: tool was removed from the library. "
    "Move that folder out of the library, or delete it."
)


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return make_bare_upstream(tmp_path / "upstream")


def _head(vault: Path, name: str = "tool") -> tuple[str, str | None] | None:
    return read_head(vault / "codes" / name / "repo")


def _recorded(vault: Path, name: str = "tool") -> tuple[object, object]:
    text = (vault / "codes" / name / "repo-meta.yaml").read_text(encoding="utf-8")
    meta = YAML(typ="safe").load(text)
    return meta.get("commit"), meta.get("branch")


def _with_repo(tmp_path: Path, cloud: Path, upstream: Upstream) -> Path:
    """Machine A with a paper and the repo ``tool`` (at the upstream's tip), synced up."""
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    added = _lit("code", "add", upstream.url, "--name", "tool", vault=a)
    assert added.exit_code == 0, added.output
    assert _sync(a).exit_code == 0
    return a


def _two_machines_with_a_repo(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> tuple[Path, Path]:
    """A as in :func:`_with_repo`, and B synced down from the cloud, repo fetched."""
    a = _with_repo(tmp_path, cloud, upstream)
    b = _machine(tmp_path, cloud, "b")
    first = _sync(b)
    assert first.exit_code == 0, first.output
    assert _head(b) == (upstream.commits[-1], "main")
    return a, b


def test_a_missing_checkout_is_fetched_at_the_recorded_commit_and_still_updates(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    a = _with_repo(tmp_path, cloud, upstream)
    c3 = upstream.commits[2]
    c4 = upstream.commit("c4")
    b = _machine(tmp_path, cloud, "b")

    result = _sync(b)

    assert result.exit_code == 0, result.output
    assert f"tool: fetched at {c3[:7]} (on main)" in result.output
    assert _head(b) == (c3, "main")
    assert _recorded(b) == _recorded(a) == (c3, "main")
    updated = _lit("code", "update", "tool", vault=b)
    assert updated.exit_code == 0, updated.output
    assert _head(b) == (c4, "main")


def test_a_repo_meta_written_before_records_the_commit_at_the_next_sync(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    """A repo-meta.yaml with no commit (written before there was one) gets
    the checkout's: B fetches that commit, not the upstream's newer tip."""
    a = _machine(tmp_path, cloud, "a")
    assert _lit("code", "add", upstream.url, "--name", "tool", vault=a).exit_code == 0
    meta = a / "codes" / "tool" / "repo-meta.yaml"
    meta.write_bytes(b"name: tool\nupstream: " + upstream.url.encode() + b"\npapers: []\n")
    c3 = upstream.commits[2]
    upstream.commit("c4")

    assert _sync(a).exit_code == 0
    b = _machine(tmp_path, cloud, "b")
    result = _sync(b)

    assert _recorded(a) == (c3, "main")
    assert result.exit_code == 0, result.output
    assert _head(b) == (c3, "main")


def test_a_checkout_behind_is_moved_to_the_commit_another_machine_recorded(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    a, b = _two_machines_with_a_repo(tmp_path, cloud, upstream)
    c4 = upstream.commit("c4")
    assert _lit("code", "update", "tool", vault=a).exit_code == 0
    assert _sync(a).exit_code == 0

    result = _sync(b)

    assert result.exit_code == 0, result.output
    assert f"tool: moved to {c4[:7]} (on main)" in result.output
    assert _head(b) == (c4, "main")
    assert _recorded(b) == (c4, "main")


def test_a_checkout_with_changes_stays_and_one_with_only_new_files_moves(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    a, b = _two_machines_with_a_repo(tmp_path, cloud, upstream)
    c3 = upstream.commits[2]
    c4 = upstream.commit("c4")
    assert _lit("code", "update", "tool", vault=a).exit_code == 0
    assert _sync(a).exit_code == 0
    repo = b / "codes" / "tool" / "repo"
    (repo / "code.py").write_bytes(b"print('mine')\n")

    kept = _sync(b)

    assert kept.exit_code == sync_cmd.EXIT_SYNC_REPOS_DIFFER == 22, kept.output
    assert (
        f"tool: it has changes here, so it stays at {c3[:7]}; the library records "
        f"{c4[:7]}. Commit and push them, or undo them; the next `lit sync` moves it."
    ) in kept.output
    assert _head(b) == (c3, "main")
    assert (repo / "code.py").read_bytes() == b"print('mine')\n"
    assert _recorded(b) == (c4, "main")

    git("checkout", "--", "code.py", cwd=repo)
    (repo / "results.csv").write_bytes(b"1,2\n")
    moved = _sync(b)

    assert moved.exit_code == 0, moved.output
    assert _head(b) == (c4, "main")
    assert "Uncommitted changes in tool are not synced." in moved.output
    assert (repo / "results.csv").read_bytes() == b"1,2\n"


def test_a_repo_imported_from_a_folder_is_reported_not_fetched(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    source = tmp_path / "mycode"
    source.mkdir()
    (source / "run.py").write_bytes(b"print(1)\n")
    assert _lit("code", "add", str(source), "--name", "mine", vault=a).exit_code == 0
    assert _sync(a).exit_code == 0
    b = _machine(tmp_path, cloud, "b")

    result = _sync(b)

    assert result.exit_code == sync_cmd.EXIT_SYNC_REPOS_DIFFER, result.output
    assert (
        "mine: it was imported from a folder on another machine. "
        "Copy that folder to codes/mine/repo here."
    ) in result.output
    assert (b / "codes" / "mine" / "repo-meta.yaml").is_file()
    assert not (b / "codes" / "mine" / "repo").exists()


def test_an_unreachable_upstream_is_reported_and_the_library_still_syncs(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    _with_repo(tmp_path, cloud, upstream)
    c3 = upstream.commits[2]
    away = upstream.bare.rename(upstream.bare.with_name("away.git"))
    b = _machine(tmp_path, cloud, "b")

    offline = _sync(b)

    assert offline.exit_code == sync_cmd.EXIT_SYNC_REPOS_DIFFER, offline.output
    assert (b / "papers" / "p1" / "metadata.yaml").is_file()
    assert f"tool: not fetched: git could not fetch commit {c3[:7]}: fatal:" in offline.output
    assert "The next `lit sync` tries again." in offline.output
    assert not (b / "codes" / "tool" / "repo").exists()

    away.rename(upstream.bare)
    again = _sync(b)

    assert again.exit_code == 0, again.output
    assert _head(b) == (c3, "main")


def test_a_checkout_that_could_not_follow_does_not_take_the_others_back(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    """B is still at the old commit after a fetch that failed; its next sync
    must not take that for a move of the user's and record it."""
    a, b = _two_machines_with_a_repo(tmp_path, cloud, upstream)
    c3 = upstream.commits[2]
    c4 = upstream.commit("c4")
    assert _lit("code", "update", "tool", vault=a).exit_code == 0
    assert _sync(a).exit_code == 0
    away = upstream.bare.rename(upstream.bare.with_name("away.git"))
    assert _sync(b).exit_code == sync_cmd.EXIT_SYNC_REPOS_DIFFER
    assert _head(b) == (c3, "main")
    away.rename(upstream.bare)

    back = _sync(b)
    last = _sync(a)

    assert back.exit_code == 0, back.output
    assert last.exit_code == 0, last.output
    assert _head(a) == _head(b) == (c4, "main")
    assert _recorded(a) == _recorded(b) == (c4, "main")


def test_a_pull_made_by_hand_reaches_the_other_machine(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    a, b = _two_machines_with_a_repo(tmp_path, cloud, upstream)
    c4 = upstream.commit("c4")
    git("pull", "-q", "--ff-only", cwd=a / "codes" / "tool" / "repo")

    assert _sync(a).exit_code == 0
    result = _sync(b)

    assert _recorded(a) == (c4, "main")
    assert _head(a) == (c4, "main")
    assert result.exit_code == 0, result.output
    assert _head(b) == (c4, "main")


def test_a_commit_no_remote_has_is_named_on_both_machines(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    a, b = _two_machines_with_a_repo(tmp_path, cloud, upstream)
    c3 = upstream.commits[2]
    repo_a = a / "codes" / "tool" / "repo"
    (repo_a / "mine.py").write_bytes(b"print('mine')\n")
    git("add", "mine.py", cwd=repo_a)
    git("commit", "-q", "-m", "mine", cwd=repo_a)
    mine = git("rev-parse", "HEAD", cwd=repo_a).strip()

    from_a = _sync(a)
    on_b = _sync(b)

    assert from_a.exit_code == 0, from_a.output
    assert (
        "Commits in tool that no remote has are not synced: "
        "push them so your other machines can fetch them."
    ) in from_a.output
    assert _recorded(a) == (mine, "main")
    assert on_b.exit_code == sync_cmd.EXIT_SYNC_REPOS_DIFFER, on_b.output
    assert f"tool: not moved to {mine[:7]}: git could not fetch commit {mine[:7]}" in on_b.output
    assert (
        f"Push {mine[:7]} from the machine that made it; the next `lit sync` fetches it."
    ) in on_b.output
    assert _head(b) == (c3, "main")


def test_a_detached_checkout_is_followed_detached(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    a, b = _two_machines_with_a_repo(tmp_path, cloud, upstream)
    c2 = upstream.commits[1]
    repo_a = a / "codes" / "tool" / "repo"
    git("fetch", "-q", "--depth", "1", "origin", c2, cwd=repo_a)
    git("checkout", "-q", "--detach", c2, cwd=repo_a)

    from_a = _sync(a)
    result = _sync(b)

    assert from_a.exit_code == 0, from_a.output
    assert "not synced" not in from_a.output
    assert _recorded(a) == (c2, None)
    assert result.exit_code == 0, result.output
    assert f"tool: moved to {c2[:7]} (detached)" in result.output
    assert _head(b) == (c2, None)


def test_a_checkout_left_by_a_repo_removed_elsewhere_is_reported_on_every_sync(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    a, b = _two_machines_with_a_repo(tmp_path, cloud, upstream)
    removed = _lit("code", "rm", "tool", "-y", vault=a)
    assert removed.exit_code == 0, removed.output
    assert _sync(a).exit_code == 0

    first = _sync(b)
    again = _sync(b)

    for result in (first, again):
        assert result.exit_code == sync_cmd.EXIT_SYNC_REPOS_DIFFER, result.output
        assert _LEFT_BEHIND in result.output
    assert _head(b) == (upstream.commits[-1], "main")
    assert not (b / "codes" / "tool" / "repo-meta.yaml").exists()

    # B's checkout keeps an empty codes/tool/ in the cloud, which comes to A:
    # nothing to fix there, and a repo of that name can be added again.
    on_a = _sync(a)

    assert on_a.exit_code == 0, on_a.output
    assert code_folder_kind(a / "codes" / "tool") == "empty"
    assert check_code_clone_integrity(a, list_papers(a)) == []
    readded = _lit("code", "add", upstream.url, "--name", "tool", vault=a)
    assert readded.exit_code == 0, readded.output


def test_conflicts_and_a_repo_that_did_not_follow_exit_21_and_both_are_listed(
    tmp_path: Path, cloud: Path, upstream: Upstream
) -> None:
    a, b = _two_machines_with_a_repo(tmp_path, cloud, upstream)
    now = time.time()
    _edit_metadata(a, "p1", "edited-on-a", now - 200)
    _edit_metadata(b, "p1", "edited-on-b", now - 100)
    upstream.commit("c4")
    assert _lit("code", "update", "tool", vault=a).exit_code == 0
    assert _sync(a).exit_code == 0
    (b / "codes" / "tool" / "repo" / "code.py").write_bytes(b"print('mine')\n")

    result = _sync(b)

    assert result.exit_code == sync_cmd.EXIT_SYNC_CONFLICTS, result.output
    assert "1 conflict" in result.output
    assert "p1 · metadata.yaml" in result.output
    assert "tool: it has changes here, so it stays at" in result.output
