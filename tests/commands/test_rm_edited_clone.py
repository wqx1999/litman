"""``lit rm`` keeps a repo holding your own changes.

A repo only the deleted paper used goes with it when it can be cloned again —
but a clone does not bring back what you changed in the checkout. Each case
makes one such change to a real shallow clone and checks that the repo is
kept, untouched and named, and that a restore binds it again; the controls
check that a clone git reads as clean still goes. Then the restore that used
to delete a checkout it found back in place.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.commands.trash import _reclone_missing_repo
from litman.core import code as code_mod
from litman.core.code import import_local_repo, repo_changes
from litman.core.library import create_vault
from litman.core.locking import rmtree
from litman.core.trash import list_trash
from litman.exceptions import CodeError
from tests._git_clone import clone, git, make_upstream
from tests._project_folder import deny

TARGET = "2024_Target"
WAY_OUT = "`lit code rm` deletes one"

_yaml = YAML(typ="safe")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _write_target(vault: Path, repos: list[str]) -> None:
    paper_dir = vault / "papers" / TARGET
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {TARGET}\ntitle: Target\nauthors: [Doe, Jane]\nyear: 2024\n"
        f"doi: 10.0/{TARGET}\nstatus: inbox\ntype: research\nprojects: []\n"
        f"topics: []\nmethods: []\ncode-clones: {repos}\n"
        "created-at: '2026-05-11T10:00:00+02:00'\n"
        "updated-at: '2026-05-11T10:00:00+02:00'\n",
        encoding="utf-8",
    )


def _write_repo_meta(root: Path, upstream: str) -> None:
    (root / "repo-meta.yaml").write_text(
        f"name: {root.name}\nupstream: '{upstream}'\npapers: [{TARGET}]\n",
        encoding="utf-8",
    )
    (root / "notes.md").write_text(f"# {root.name}\n", encoding="utf-8")


def _add_repo(vault: Path, tmp_path: Path, name: str = "Solo") -> Path:
    """``codes/<name>/`` as ``lit code add`` leaves it: a shallow clone, its
    repo-meta.yaml (bound to the target) and notes.md."""
    upstream = tmp_path / "upstream"
    if not upstream.exists():
        make_upstream(upstream)
    root = vault / "codes" / name
    clone(upstream, root / "repo")
    _write_repo_meta(root, upstream.as_uri())
    return root


def _paper_with_repo(vault: Path, tmp_path: Path) -> Path:
    _write_target(vault, ["Solo"])
    return _add_repo(vault, tmp_path)


def _lit(vault: Path, *args: str) -> str:
    """Run ``lit``; its output with one space between words (a line Rich
    wrapped still matches)."""
    result = CliRunner().invoke(cli, [*args, "--library", str(vault)])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


def _files(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` (``.git/`` too) but repo-meta.yaml, which the
    delete rewrites."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "repo-meta.yaml"
    }


def _papers_of(vault: Path, name: str = "Solo") -> list[str]:
    meta = _yaml.load(
        (vault / "codes" / name / "repo-meta.yaml").read_text(encoding="utf-8")
    )
    return list(meta["papers"])


def _edit(root: Path) -> None:
    (root / "repo" / "README.md").write_text("# mine\n", encoding="utf-8")


def _commit(root: Path) -> None:
    _edit(root)
    git("commit", "-q", "-am", "mine", cwd=root / "repo")


def _commit_on_a_branch(root: Path) -> None:
    git("checkout", "-q", "-b", "side", cwd=root / "repo")
    _commit(root)
    git("checkout", "-q", "-", cwd=root / "repo")


def _stash(root: Path) -> None:
    _edit(root)
    git("stash", "-q", cwd=root / "repo")


def _new_file(root: Path) -> None:
    (root / "repo" / "results.csv").write_text("x\n", encoding="utf-8")


def _staged(root: Path) -> None:
    (root / "repo" / "new.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "new.py", cwd=root / "repo")


def _hidden_new_file(root: Path) -> None:
    git("config", "status.showUntrackedFiles", "no", cwd=root / "repo")
    _new_file(root)


def _staged_like_a_file_managers(root: Path) -> None:
    # Named like what a file manager leaves, but the user's to commit.
    (root / "repo" / "desktop.ini").write_text("[x]\n", encoding="utf-8")
    git("add", "desktop.ini", cwd=root / "repo")


def _next_to_repo(root: Path) -> None:
    (root / "run.sh").write_text("python train.py\n", encoding="utf-8")


CHANGES: dict[str, Callable[[Path], None]] = {
    "an edited file": _edit,
    "a new file": _new_file,
    "a new file the repo hides from git status": _hidden_new_file,
    "a staged file": _staged,
    "a staged file named like a file manager's": _staged_like_a_file_managers,
    "a commit": _commit,
    "a commit on another branch": _commit_on_a_branch,
    "a stash": _stash,
    "a file next to repo/": _next_to_repo,
}


@pytest.mark.parametrize("change", list(CHANGES))
def test_rm_keeps_a_repo_with_your_own_changes(
    vault: Path, tmp_path: Path, change: str
) -> None:
    root = _paper_with_repo(vault, tmp_path)
    CHANGES[change](root)
    before = _files(root)

    preview = _lit(vault, "rm", TARGET, "--dry-run")
    result = _lit(vault, "rm", TARGET, "-y")

    assert "Would keep repos with your own changes: Solo" in preview
    assert "orphan" not in preview
    assert "still bound" not in preview
    assert f"Kept 1 repo with your own changes (Solo; {WAY_OUT})" in result
    assert "Removed" not in result
    assert "Unbound" not in result
    assert _files(root) == before  # the check changed nothing either
    assert _papers_of(vault) == []
    (entry,) = list_trash(vault)
    assert entry.orphan_repos == {}


def _ignored_file(root: Path) -> None:
    (root / "repo" / "__pycache__").mkdir()
    (root / "repo" / "__pycache__" / "m.pyc").write_bytes(b"\0")


def _file_manager_files(root: Path) -> None:
    (root / ".DS_Store").write_bytes(b"\0")
    (root / "repo" / "Thumbs.db").write_bytes(b"\0")
    (root / "repo" / "._README.md").write_bytes(b"\0")
    # A new folder Finder was shown, holding nothing else.
    (root / "repo" / "figures").mkdir()
    (root / "repo" / "figures" / ".DS_Store").write_bytes(b"\0")


def _no_checkout(root: Path) -> None:
    # What a sync that leaves checkouts out brings over.
    rmtree(root / "repo")


GONE: dict[str, Callable[[Path], None]] = {
    "nothing": lambda root: None,
    "an ignored file": _ignored_file,
    "a file manager's files": _file_manager_files,
    "no checkout": _no_checkout,
}


@pytest.mark.parametrize("state", list(GONE))
def test_rm_still_deletes_a_repo_a_clone_brings_back(
    vault: Path, tmp_path: Path, state: str
) -> None:
    root = _paper_with_repo(vault, tmp_path)
    GONE[state](root)

    preview = _lit(vault, "rm", TARGET, "--dry-run")
    result = _lit(vault, "rm", TARGET, "-y")

    assert "Would remove orphan repos: Solo" in preview
    assert "Removed 1 orphan repo (Solo)" in result
    assert "Kept" not in result
    assert not root.exists()


def _broken_git(root: Path) -> None:
    rmtree(root / "repo" / ".git")
    (root / "repo" / ".git").write_text("gitdir: nowhere\n", encoding="utf-8")


def _no_commit_yet(root: Path) -> None:
    rmtree(root / "repo" / ".git")
    git("init", "-q", cwd=root / "repo")


def _corrupt_index(root: Path) -> None:
    # git log still reads it; git status cannot.
    (root / "repo" / ".git" / "index").write_bytes(b"garbage")


UNCHECKED: dict[str, tuple[Callable[[Path], None], str]] = {
    "a plain folder": (lambda root: rmtree(root / "repo" / ".git"), "not a git checkout"),
    "a broken .git": (_broken_git, "git failed"),
    "a checkout with no commit yet": (_no_commit_yet, "git failed"),
    "a corrupt index": (_corrupt_index, "git failed"),
}


@pytest.mark.parametrize("state", list(UNCHECKED))
def test_rm_keeps_a_repo_it_cannot_check(
    vault: Path, tmp_path: Path, state: str
) -> None:
    root = _paper_with_repo(vault, tmp_path)
    make, why = UNCHECKED[state]
    make(root)

    preview = _lit(vault, "rm", TARGET, "--dry-run")
    result = _lit(vault, "rm", TARGET, "-y")

    assert f"Would keep repos it could not check for changes: Solo ({why})" in preview
    assert (
        f"Kept 1 repo it could not check for changes (Solo: {why}; {WAY_OUT})"
        in result
    )
    assert (root / "repo" / "README.md").is_file()
    assert _papers_of(vault) == []


def test_a_repo_folder_it_cannot_list_is_one_it_cannot_check(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _paper_with_repo(vault, tmp_path)
    with monkeypatch.context() as patch:
        deny(patch, root)
        assert repo_changes(root) == ("unchecked", "cannot read the folder")


def test_rm_keeps_a_repo_when_there_is_no_git(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _paper_with_repo(vault, tmp_path)
    nothing = tmp_path / "no-programs"
    nothing.mkdir()
    monkeypatch.setenv("PATH", str(nothing))

    result = _lit(vault, "rm", TARGET, "-y")

    assert f"(Solo: git not found; {WAY_OUT})" in result
    assert (root / "repo" / "README.md").is_file()


def test_rm_keeps_a_repo_git_takes_too_long_to_read(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _paper_with_repo(vault, tmp_path)
    monkeypatch.setattr(code_mod, "_CHANGES_TIMEOUT_S", 1e-6)

    result = _lit(vault, "rm", TARGET, "-y")

    assert f"(Solo: git took too long; {WAY_OUT})" in result
    assert (root / "repo" / "README.md").is_file()


def test_a_git_variable_set_for_another_repo_changes_nothing(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lit rm`` run where git's location variables are set (inside a git
    hook, say) still reads the repo's own checkout."""
    root = _paper_with_repo(vault, tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))

    result = _lit(vault, "rm", TARGET, "-y")

    assert "Removed 1 orphan repo (Solo)" in result
    assert not root.exists()


def test_rm_purge_keeps_a_repo_with_your_own_changes(
    vault: Path, tmp_path: Path
) -> None:
    root = _paper_with_repo(vault, tmp_path)
    _edit(root)

    result = _lit(vault, "rm", TARGET, "--purge", "-y")

    assert f"Kept 1 repo with your own changes (Solo; {WAY_OUT})" in result
    assert (root / "repo" / "README.md").read_text(encoding="utf-8") == "# mine\n"


@pytest.mark.parametrize("after", ["nothing", "a commit", "the import amended"])
def test_a_folder_imported_without_git(
    vault: Path, tmp_path: Path, after: str
) -> None:
    """litman makes the one commit of a folder it imports without git; a
    re-import makes it again, so it is not the user's — one on top of it is,
    and so is that commit once the user amended it."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "run.py").write_text("print(1)\n", encoding="utf-8")
    _write_target(vault, ["Local"])
    root = vault / "codes" / "Local"
    import_local_repo(source, root / "repo")
    _write_repo_meta(root, f"local:{source}")
    if after == "a commit":
        (root / "repo" / "run.py").write_text("print(2)\n", encoding="utf-8")
        git("commit", "-q", "-am", "mine", cwd=root / "repo")
    elif after == "the import amended":
        (root / "repo" / "run.py").write_text("print(2)\n", encoding="utf-8")
        git("commit", "-q", "-a", "--amend", "--no-edit", cwd=root / "repo")

    result = _lit(vault, "rm", TARGET, "-y")

    if after == "nothing":
        assert "Removed 1 orphan repo (Local)" in result
        assert not root.exists()
    else:
        assert f"Kept 1 repo with your own changes (Local; {WAY_OUT})" in result
        assert (root / "repo" / "run.py").read_text(encoding="utf-8") == "print(2)\n"


def test_the_web_ui_names_the_repos_it_keeps_and_why(
    vault: Path, tmp_path: Path
) -> None:
    """The delete dialog's preview and the delete itself list each kept repo
    under its reason, apart from the one that goes."""
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    _write_target(vault, ["Clean", "Edited", "Plain"])
    _add_repo(vault, tmp_path, "Clean")
    _edit(_add_repo(vault, tmp_path, "Edited"))
    rmtree(_add_repo(vault, tmp_path, "Plain") / "repo" / ".git")
    client = TestClient(create_app(vault))

    preview = client.get(f"/api/paper/{TARGET}/rm-preview").json()
    deleted = client.delete(f"/api/paper/{TARGET}").json()

    for answer in (preview, deleted):
        assert answer["reposRemoved"] == ["Clean"]
        assert answer["reposEdited"] == ["Edited"]
        assert answer["reposUnchecked"] == [
            {"name": "Plain", "reason": "not a git checkout"}
        ]
        assert answer["reposKept"] == []
    assert (preview["reposUnbound"], deleted["reposUnbound"]) == ([], 0)
    assert not (vault / "codes" / "Clean").exists()
    assert (vault / "codes" / "Edited" / "repo" / "README.md").read_text(
        encoding="utf-8"
    ) == "# mine\n"


def test_restore_binds_a_kept_repo_again(vault: Path, tmp_path: Path) -> None:
    root = _paper_with_repo(vault, tmp_path)
    _edit(root)
    _lit(vault, "rm", TARGET, "-y")

    restored = _lit(vault, "trash", "restore", TARGET, "-y")

    assert "Re-bound to 1 repo (Solo)" in restored
    assert "clone" not in restored.lower()
    assert _papers_of(vault) == [TARGET]
    assert (root / "repo" / "README.md").read_text(encoding="utf-8") == "# mine\n"


def _deleted_then_back_in_place(vault: Path, tmp_path: Path) -> Path:
    """rm deleted the clean repo; then its checkout came back without its
    repo-meta.yaml — a sync from a computer where the paper was deleted keeps
    the checkout it leaves out, or a clone by hand — and was changed."""
    root = _paper_with_repo(vault, tmp_path)
    _lit(vault, "rm", TARGET, "-y")
    assert not root.exists()
    clone(tmp_path / "upstream", root / "repo")
    _edit(root)
    return root


def test_restore_binds_a_checkout_that_is_back_in_place(
    vault: Path, tmp_path: Path
) -> None:
    root = _deleted_then_back_in_place(vault, tmp_path)

    restored = _lit(vault, "trash", "restore", TARGET, "-y")

    assert "Re-bound to 1 repo (Solo)" in restored
    assert "clone" not in restored.lower()
    assert (root / "repo" / "README.md").read_text(encoding="utf-8") == "# mine\n"
    assert _papers_of(vault) == [TARGET]


def test_a_web_ui_restore_binds_a_checkout_that_is_back_in_place(
    vault: Path, tmp_path: Path
) -> None:
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    root = _deleted_then_back_in_place(vault, tmp_path)
    (entry,) = list_trash(vault)

    resp = TestClient(create_app(vault)).post(
        f"/api/trash/{entry.entry_name}/restore"
    )

    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert (body["repos_rebound"], body["missing_repos"]) == (["Solo"], {})
    assert _papers_of(vault) == [TARGET]
    assert (root / "repo" / "README.md").read_text(encoding="utf-8") == "# mine\n"


def test_a_reclone_never_takes_a_checkout_that_is_there(
    vault: Path, tmp_path: Path
) -> None:
    root = _paper_with_repo(vault, tmp_path)
    _edit(root)

    with pytest.raises(CodeError, match="already there"):
        _reclone_missing_repo(
            vault, TARGET, "Solo", (tmp_path / "upstream").as_uri(), link_errors=[]
        )

    assert (root / "repo" / "README.md").read_text(encoding="utf-8") == "# mine\n"


def test_a_failed_reclone_leaves_what_the_repo_folder_held(
    vault: Path, tmp_path: Path
) -> None:
    root = vault / "codes" / "Solo"
    root.mkdir(parents=True)
    (root / "mine.txt").write_text("mine\n", encoding="utf-8")

    with pytest.raises(CodeError):
        _reclone_missing_repo(
            vault, TARGET, "Solo", (tmp_path / "nowhere").as_uri(), link_errors=[]
        )

    assert (root / "mine.txt").read_text(encoding="utf-8") == "mine\n"
    assert not (root / "repo").exists()
