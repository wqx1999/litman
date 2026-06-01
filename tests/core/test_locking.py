"""Unit tests for the read-only TRUTH lock helper (core/locking.py, M32)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from litman.core.library import create_vault
from litman.core.locking import (
    ensure_truth_locked,
    is_truth_lockable,
    lock_truth_file,
)

# The Windows read-only attribute does not produce the same POSIX
# os.access(W_OK) / PermissionError semantics, and CI runs on Linux; gate the
# mode-assertion tests on POSIX so they stay meaningful.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)


def _make_paper(vault: Path, paper_id: str, *, with_pdf: bool = True) -> Path:
    """Create a minimal papers/<id>/ dir with metadata.yaml (+ optional pdf)."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text("id: x\n", encoding="utf-8")
    (paper_dir / "notes.md").write_text("# notes\n", encoding="utf-8")
    (paper_dir / "discussion.md").write_text("# disc\n", encoding="utf-8")
    if with_pdf:
        (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return paper_dir


# ---------------------------------------------------------------------------
# lock_truth_file
# ---------------------------------------------------------------------------


@_posix_only
def test_lock_truth_file_makes_readonly(tmp_path: Path) -> None:
    target = tmp_path / "metadata.yaml"
    target.write_text("id: x\n", encoding="utf-8")
    assert os.access(target, os.W_OK)

    lock_truth_file(target)

    assert not os.access(target, os.W_OK)
    assert (target.stat().st_mode & 0o777) == 0o444


@_posix_only
def test_locked_file_open_w_raises_permissionerror(tmp_path: Path) -> None:
    target = tmp_path / "metadata.yaml"
    target.write_text("id: x\n", encoding="utf-8")
    lock_truth_file(target)

    with pytest.raises(PermissionError):
        target.open("w", encoding="utf-8")


@_posix_only
def test_unlocked_file_open_w_succeeds(tmp_path: Path) -> None:
    """A non-locked file (e.g. notes.md) stays freely writable."""
    notes = tmp_path / "notes.md"
    notes.write_text("# notes\n", encoding="utf-8")
    # Never locked.
    with notes.open("w", encoding="utf-8") as f:
        f.write("edited\n")
    assert notes.read_text() == "edited\n"


def test_lock_truth_file_missing_is_noop(tmp_path: Path) -> None:
    """Locking a non-existent file does not raise (paper with no pdf)."""
    lock_truth_file(tmp_path / "papers" / "x" / "paper.pdf")  # no error


def test_lock_truth_file_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "metadata.yaml"
    target.write_text("id: x\n", encoding="utf-8")
    lock_truth_file(target)
    lock_truth_file(target)  # second call on an already-locked file is fine
    if sys.platform != "win32":
        assert (target.stat().st_mode & 0o777) == 0o444


# ---------------------------------------------------------------------------
# is_truth_lockable — true / false matrix
# ---------------------------------------------------------------------------


def test_is_truth_lockable_true_cases(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    vault.mkdir()
    assert is_truth_lockable(vault, vault / "papers" / "2024_x" / "metadata.yaml")
    assert is_truth_lockable(vault, vault / "papers" / "2024_x" / "paper.pdf")
    assert is_truth_lockable(vault, vault / "TAXONOMY.md")


def test_is_truth_lockable_false_cases(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    vault.mkdir()
    not_locked = [
        vault / "papers" / "2024_x" / "notes.md",
        vault / "papers" / "2024_x" / "discussion.md",
        vault / "lit-config.yaml",
        vault / "INDEX.json",
        vault / "views" / "by-topic" / "t" / "metadata.yaml",
        vault / "codes" / "repo" / "repo-meta.yaml",
        vault / ".trash" / "2024_x" / "metadata.yaml",
        vault / ".litman-staging" / "op" / "metadata.yaml",
        # nested too deep / wrong shape under papers/
        vault / "papers" / "2024_x" / "sub" / "metadata.yaml",
        vault / "metadata.yaml",  # at root, not under papers/<id>/
    ]
    for p in not_locked:
        assert not is_truth_lockable(vault, p), p


def test_is_truth_lockable_relative_target(tmp_path: Path) -> None:
    """A relative target (as appears in a staging manifest) resolves correctly."""
    vault = tmp_path / "v"
    vault.mkdir()
    assert is_truth_lockable(vault, Path("papers/2024_x/metadata.yaml"))
    assert is_truth_lockable(vault, Path("TAXONOMY.md"))
    assert not is_truth_lockable(vault, Path("papers/2024_x/notes.md"))
    assert not is_truth_lockable(vault, Path("INDEX.json"))


def test_is_truth_lockable_outside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    vault.mkdir()
    assert not is_truth_lockable(vault, tmp_path / "elsewhere" / "metadata.yaml")


# ---------------------------------------------------------------------------
# ensure_truth_locked — idempotent sweep
# ---------------------------------------------------------------------------


@_posix_only
def test_ensure_truth_locked_locks_all_truth(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    # create_vault already locks TAXONOMY.md; unlock it to count it again.
    os.chmod(vault / "TAXONOMY.md", 0o644)
    _make_paper(vault, "2024_a")
    _make_paper(vault, "2024_b", with_pdf=False)

    n = ensure_truth_locked(vault)
    # TAXONOMY.md + a/metadata + a/paper.pdf + b/metadata == 4
    assert n == 4

    assert not os.access(vault / "TAXONOMY.md", os.W_OK)
    assert not os.access(vault / "papers" / "2024_a" / "metadata.yaml", os.W_OK)
    assert not os.access(vault / "papers" / "2024_a" / "paper.pdf", os.W_OK)
    assert not os.access(vault / "papers" / "2024_b" / "metadata.yaml", os.W_OK)


@_posix_only
def test_ensure_truth_locked_idempotent(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    _make_paper(vault, "2024_a")
    ensure_truth_locked(vault)
    # Second pass: everything is already locked, so nothing is re-locked.
    assert ensure_truth_locked(vault) == 0


@_posix_only
def test_ensure_truth_locked_only_relocks_writable(tmp_path: Path) -> None:
    """Only a TRUTH file that was made writable is re-locked and counted."""
    vault = create_vault(tmp_path)
    _make_paper(vault, "2024_a")
    ensure_truth_locked(vault)  # lock everything

    meta = vault / "papers" / "2024_a" / "metadata.yaml"
    os.chmod(meta, 0o644)  # simulate a writable file (post-pull / hand edit)

    assert ensure_truth_locked(vault) == 1
    assert not os.access(meta, os.W_OK)


@_posix_only
def test_ensure_truth_locked_leaves_non_truth_writable(tmp_path: Path) -> None:
    """notes.md / discussion.md / INDEX.json stay writable after the sweep."""
    vault = create_vault(tmp_path)
    paper_dir = _make_paper(vault, "2024_a")
    ensure_truth_locked(vault)

    assert os.access(paper_dir / "notes.md", os.W_OK)
    assert os.access(paper_dir / "discussion.md", os.W_OK)
    assert os.access(vault / "INDEX.json", os.W_OK)
    assert os.access(vault / "lit-config.yaml", os.W_OK)


def test_ensure_truth_locked_empty_papers_dir(tmp_path: Path) -> None:
    """A vault with no papers does not crash (only TAXONOMY may re-lock)."""
    vault = create_vault(tmp_path)
    os.chmod(vault / "TAXONOMY.md", 0o644)
    assert ensure_truth_locked(vault) == 1
