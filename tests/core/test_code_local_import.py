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

import errno
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import code as code_mod
from litman.core.code import (
    import_local_repo,
    make_repo_meta,
    missing_code_clones,
    write_repo_meta,
)
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


def test_local_move_failure_preserves_source(
    vault: Path, local_non_git_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (bug-report 2026-06-01 #7): a failure AFTER the copy must
    not destroy the source under --move.

    Pre-fix, `import_local_repo(move=True)` destroyed the source up front and
    the except handler then rmtree'd the half-built vault copy — losing BOTH.
    Now the import copies first and the source is deleted only after the whole
    add commits, so a mid-import failure leaves the source fully intact.
    """
    import litman.commands.code as code_cmd

    def _boom(*_a: object, **_k: object) -> None:
        raise CodeError("simulated post-copy failure")

    # write_repo_meta runs after the copy but before the source would be
    # consumed — the exact window that used to lose data.
    monkeypatch.setattr(code_cmd, "write_repo_meta", _boom)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "code", "add", str(local_non_git_dir),
            "--name", "Moved",
            "--move",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0

    # Half-built vault dir is rolled back...
    assert not (vault / "codes" / "Moved").exists()
    # ...and the source survives intact — no data loss.
    assert local_non_git_dir.exists()
    assert (local_non_git_dir / "main.py").is_file()
    assert (local_non_git_dir / "README.md").is_file()


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
# A source written to while it is copied — git's background maintenance
# ---------------------------------------------------------------------------
#
# git takes and drops locks and temp files inside `.git/` on its own schedule,
# so a file `copytree` listed can be gone when it gets to it (CI, macOS,
# 2026-09-06: `.git/objects/maintenance.lock`). These tests reproduce that
# window exactly: `os.scandir` of the source's `.git/objects` is answered with
# a listing that still names a file which is deleted before it is returned.

_real_scandir = os.scandir


class _Listing:
    """Stands in for ``os.scandir``'s iterator: a fixed list of entries."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __enter__(self) -> _Listing:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[os.DirEntry[str]]:
        return iter(self._entries)

    def close(self) -> None:
        return None


def _vanish_after_listing(
    monkeypatch: pytest.MonkeyPatch,
    src_root: Path,
    victim_for: Callable[[int], str | None],
) -> list[int]:
    """Make ``<src_root>/.git/objects`` lose a file between listing and copy.

    ``victim_for(n)`` names the file planted in that directory on its n-th
    listing (or ``None`` for an honest listing). Returns the list the listings
    are counted into.
    """
    listings: list[int] = []

    def scandir(path: Any = ".") -> Any:
        if isinstance(path, int):
            return _real_scandir(path)
        here = Path(os.fspath(path))
        if not (
            here.name == "objects"
            and here.parent.name == ".git"
            and src_root.name in here.parts
        ):
            return _real_scandir(path)
        victim = victim_for(len(listings))
        listings.append(len(listings))
        if victim is None:
            return _real_scandir(path)
        planted = here / victim
        planted.write_text("transient\n", encoding="utf-8")
        with _real_scandir(path) as it:
            entries = list(it)
        planted.unlink()
        return _Listing(entries)

    monkeypatch.setattr(os, "scandir", scandir)
    return listings


@pytest.fixture
def racy_repo(tmp_path: Path) -> Path:
    """A git repo with an uncommitted file, named so the hook can find it."""
    repo = tmp_path / "racy-src"
    repo.mkdir()
    _git_init_with_commit(repo, file_contents="# racy\n")
    (repo / "dirty.txt").write_text("uncommitted change\n", encoding="utf-8")
    return repo


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_mod, "_COPY_RETRY_DELAY_S", 0)


def _add_local(vault: Path, src: Path, name: str) -> Result:
    return CliRunner().invoke(
        cli,
        ["code", "add", str(src), "--name", name, "--library", str(vault)],
    )


def _assert_healthy_copy(repo_dir: Path) -> None:
    """The copy is a git repo git itself finds whole, dirty file included."""
    assert (repo_dir / "dirty.txt").read_text(encoding="utf-8") == (
        "uncommitted change\n"
    )
    subprocess.run(["git", "-C", str(repo_dir), "fsck", "--full"], check=True,
                   capture_output=True)
    status = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    )
    assert "dirty.txt" in status.stdout


def test_a_git_lock_taken_and_dropped_mid_copy_is_not_copied(
    vault: Path, racy_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CI failure itself: one walk, the lock never looked at."""
    listings = _vanish_after_listing(
        monkeypatch, racy_repo, lambda n: "maintenance.lock"
    )

    result = _add_local(vault, racy_repo, "Racy")

    assert result.exit_code == 0, result.output
    assert listings == [0]  # skipped by name, no restart needed
    repo_dir = vault / "codes" / "Racy" / "repo"
    assert not (repo_dir / ".git" / "objects" / "maintenance.lock").exists()
    _assert_healthy_copy(repo_dir)


def test_any_other_file_vanishing_mid_copy_restarts_the_copy(
    vault: Path,
    racy_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_retry_delay: None,
) -> None:
    """A temp object gone after listing: the half copy is redone, not patched.

    Git keeps loose objects read-only, and the half copy holds them, so the
    restart has to delete read-only files first — the step that fails on
    Windows unless it goes through ``core.locking.rmtree``. Not every git
    build leaves all of them read-only (Homebrew git 2.55 on macOS did not),
    so the test sets that itself instead of depending on the git it finds.
    """
    objects = [
        f for f in (racy_repo / ".git" / "objects").rglob("*")
        if f.is_file() and f.parent.name not in ("info", "pack")
    ]
    assert objects
    for f in objects:
        f.chmod(0o444)
    listings = _vanish_after_listing(
        monkeypatch, racy_repo, lambda n: "tmp_obj_Ab12Cd" if n == 0 else None
    )

    result = _add_local(vault, racy_repo, "Racy")

    assert result.exit_code == 0, result.output
    assert listings == [0, 1]
    _assert_healthy_copy(vault / "codes" / "Racy" / "repo")
    assert racy_repo.is_dir()


def test_a_folder_gone_mid_copy_and_back_by_the_check_still_restarts(
    vault: Path,
    racy_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_retry_delay: None,
) -> None:
    """git deletes an emptied ``objects/<xx>/`` and recreates it on its next
    write, so "is it gone now?" cannot be what decides a restart.
    """
    blob = subprocess.run(
        ["git", "-C", str(racy_repo), "hash-object", "-w", "--stdin"],
        input="a fresh loose object\n", check=True, capture_output=True,
        text=True,
    ).stdout.strip()
    tail = (racy_repo.name, ".git", "objects", blob[:2])
    walks: list[int] = []

    def scandir(path: Any = ".") -> Any:
        if not isinstance(path, int) and Path(os.fspath(path)).parts[-4:] == tail:
            walks.append(len(walks))
            if len(walks) == 1:  # gone when walked; back before anyone looks
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(path)
                )
        return _real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)

    result = _add_local(vault, racy_repo, "Racy")

    assert result.exit_code == 0, result.output
    assert walks == [0, 1]
    repo_dir = vault / "codes" / "Racy" / "repo"
    assert (repo_dir / ".git" / "objects" / blob[:2] / blob[2:]).is_file()
    _assert_healthy_copy(repo_dir)


def test_a_source_that_never_settles_gives_up_with_a_message(
    vault: Path,
    racy_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_retry_delay: None,
) -> None:
    listings = _vanish_after_listing(
        monkeypatch, racy_repo, lambda n: f"tmp_obj_{n:06d}"
    )

    result = _add_local(vault, racy_repo, "Racy")

    assert result.exit_code != 0
    assert isinstance(result.exception, CodeError), result.output
    assert "kept disappearing while they were copied" in str(result.exception)
    assert len(listings) == code_mod._COPY_ATTEMPTS
    assert not (vault / "codes" / "Racy").exists()
    assert (racy_repo / "dirty.txt").is_file()  # the source is untouched


def test_a_lock_held_in_the_source_is_left_behind(
    vault: Path, racy_repo: Path
) -> None:
    """A git command running in the source must not leave the copy locked.

    The project's own lock files (``uv.lock``, ``Cargo.lock``) are ordinary
    files and still come across.
    """
    (racy_repo / ".git" / "index.lock").write_text("", encoding="utf-8")
    (racy_repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (racy_repo / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")

    result = _add_local(vault, racy_repo, "Locked")

    assert result.exit_code == 0, result.output
    repo_dir = vault / "codes" / "Locked" / "repo"
    assert not (repo_dir / ".git" / "index.lock").exists()
    assert (repo_dir / "uv.lock").read_text(encoding="utf-8") == "version = 1\n"
    assert (repo_dir / "Cargo.lock").is_file()
    # git can write in the copy (a copied index.lock makes this refuse).
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True,
                   capture_output=True)
    assert (racy_repo / ".git" / "index.lock").exists()  # source untouched


def test_a_copy_failure_that_is_not_a_vanished_file_is_not_retried(
    racy_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable file is a real error: raised at once, no restart."""
    calls: list[Path] = []
    still_there = racy_repo / "README.md"

    def copytree(src: Path, dst: Path, **_: Any) -> None:
        calls.append(dst)
        raise shutil.Error([(str(still_there), str(dst / "README.md"),
                             "[Errno 13] Permission denied")])

    monkeypatch.setattr(code_mod.shutil, "copytree", copytree)

    with pytest.raises(shutil.Error):
        import_local_repo(racy_repo, tmp_path / "codes" / "X" / "repo")
    assert len(calls) == 1


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


# ---------------------------------------------------------------------------
# missing_code_clones — shared dangling-link predicate (CLI show / cockpit /
# health-check all key off it; lock it so the three never drift)
# ---------------------------------------------------------------------------


def _make_clone(vault: Path, name: str, *, with_meta: bool = True) -> None:
    """Materialize codes/<name>/ (with or without repo-meta.yaml)."""
    (vault / "codes" / name).mkdir(parents=True, exist_ok=True)
    if with_meta:
        (vault / "codes" / name / "repo-meta.yaml").write_text("papers: []\n")


def test_missing_code_clones_flags_only_absent(vault: Path) -> None:
    _make_clone(vault, "present-repo")
    missing = missing_code_clones(vault, ["present-repo", "gone-repo"])
    assert missing == ["gone-repo"]


def test_missing_code_clones_dir_without_meta_is_missing(vault: Path) -> None:
    # A directory with no repo-meta.yaml is "missing" — same criterion as
    # check_code_clone_integrity (the clone is unrecoverable / unreferenceable).
    _make_clone(vault, "no-meta", with_meta=False)
    assert missing_code_clones(vault, ["no-meta"]) == ["no-meta"]


def test_missing_code_clones_is_order_preserving(vault: Path) -> None:
    _make_clone(vault, "b-present")
    assert missing_code_clones(vault, ["a-gone", "b-present", "c-gone"]) == [
        "a-gone",
        "c-gone",
    ]


def test_missing_code_clones_skips_empty_and_non_string(vault: Path) -> None:
    assert missing_code_clones(vault, ["", "ok-gone", None]) == ["ok-gone"]  # type: ignore[list-item]


def test_missing_code_clones_no_codes_dir(vault: Path) -> None:
    # No codes/ at all — every referenced name is missing.
    assert missing_code_clones(vault, ["x", "y"]) == ["x", "y"]
