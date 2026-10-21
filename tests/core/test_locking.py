"""Unit tests for the read-only TRUTH lock helper (core/locking.py, M32)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from litman.core import locking
from litman.core.library import create_vault
from litman.core.locking import (
    ensure_truth_locked,
    is_truth_lockable,
    lock_truth_file,
    rmtree,
    unlock_truth_file,
)
from litman.core.portable_link import make_portable_link

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
# unlock_truth_file
# ---------------------------------------------------------------------------


@_posix_only
def test_unlock_truth_file_clears_readonly(tmp_path: Path) -> None:
    """unlock reverses lock: a 0o444 TRUTH file becomes writable again."""
    target = tmp_path / "metadata.yaml"
    target.write_text("id: x\n", encoding="utf-8")
    lock_truth_file(target)
    assert not os.access(target, os.W_OK)

    unlock_truth_file(target)

    assert os.access(target, os.W_OK)


def test_unlock_truth_file_missing_is_noop(tmp_path: Path) -> None:
    """Unlocking a non-existent file does not raise (first-time create)."""
    unlock_truth_file(tmp_path / "papers" / "x" / "metadata.yaml")  # no error


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


def test_is_truth_lockable_covers_si_files(tmp_path: Path) -> None:
    """Every file directly in papers/<id>/supplementary/ is locked like
    paper.pdf — but not OS clutter, not deeper nesting, and only that
    folder name."""
    vault = tmp_path / "v"
    vault.mkdir()
    si = vault / "papers" / "2024_x" / "supplementary"
    assert is_truth_lockable(vault, si / "si-01.pdf")
    assert is_truth_lockable(vault, si / "si-02.tar.gz")
    # A stray dropped in by hand is locked too: it is a file in the SI folder.
    assert is_truth_lockable(vault, si / "Table S2.xlsx")
    assert is_truth_lockable(vault, Path("papers/2024_x/supplementary/si-03.csv"))
    for p in (
        si / ".DS_Store",
        si / "Thumbs.db",
        si / "desktop.ini",
        si / "sub" / "si-01.pdf",
        vault / "papers" / "2024_x" / "supplements" / "si-01.pdf",
        # A deleted paper's SI rides into the trash with it, unlocked there.
        vault / ".trash" / "2024_x-20261020T083000Z" / "supplementary" / "si-02.pdf",
    ):
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


@_posix_only
def test_ensure_truth_locked_covers_si_files(tmp_path: Path) -> None:
    """The re-lock sweep (health-check, post-pull) reaches SI files; OS
    clutter stays writable, and a second pass finds nothing to do."""
    vault = create_vault(tmp_path)
    paper_dir = _make_paper(vault, "2024_a")
    si = paper_dir / "supplementary"
    si.mkdir()
    (si / "si-01.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (si / "si-02.xlsx").write_bytes(b"PK")
    (si / ".DS_Store").write_bytes(b"\0")
    (si / ".my notes.txt").write_bytes(b"hidden, not SI")
    ensure_truth_locked(vault)

    os.chmod(si / "si-01.pdf", 0o644)  # a pulled copy arrives writable
    os.chmod(si / "si-02.xlsx", 0o644)
    assert ensure_truth_locked(vault) == 2
    assert not os.access(si / "si-01.pdf", os.W_OK)
    assert not os.access(si / "si-02.xlsx", os.W_OK)
    assert os.access(si / ".DS_Store", os.W_OK)
    assert os.access(si / ".my notes.txt", os.W_OK)
    assert ensure_truth_locked(vault) == 0


@_posix_only
def test_ensure_truth_locked_does_not_follow_si_symlinks(tmp_path: Path) -> None:
    """chmod acts on a link's target, which could be any file on the machine."""
    vault = create_vault(tmp_path)
    paper_dir = _make_paper(vault, "2024_a")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    si = paper_dir / "supplementary"
    si.mkdir()
    (si / "si-01.pdf").symlink_to(outside)

    ensure_truth_locked(vault)

    assert os.access(outside, os.W_OK)


def test_ensure_truth_locked_never_reaches_through_a_linked_si_folder(
    tmp_path: Path,
) -> None:
    """A supplementary/ that is a folder link (symlink, or junction on
    Windows) leads to some other folder on the machine — say the user's
    Downloads. The sweep must not lock anything in there."""
    vault = create_vault(tmp_path)
    paper_dir = _make_paper(vault, "2024_a")
    outside = tmp_path / "Downloads"
    outside.mkdir()
    personal = outside / "personal.docx"
    personal.write_bytes(b"mine")
    if not make_portable_link(paper_dir / "supplementary", outside):
        pytest.skip("this filesystem cannot hold folder links")

    ensure_truth_locked(vault)

    assert os.access(personal, os.W_OK)


@_posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the permission bits this takes away",
)
def test_ensure_truth_locked_survives_an_unreadable_si_folder(
    tmp_path: Path,
) -> None:
    """The sweep runs first in `lit health-check` and after `lit sync pull`:
    a folder it cannot list must not abort either (the SI check reports it)."""
    vault = create_vault(tmp_path)
    paper_dir = _make_paper(vault, "2024_a")
    si = paper_dir / "supplementary"
    si.mkdir()
    (si / "si-01.pdf").write_bytes(b"%PDF-1.4\n")
    si.chmod(0o000)
    try:
        ensure_truth_locked(vault)
    finally:
        si.chmod(0o755)


def test_ensure_truth_locked_tolerates_a_file_named_supplementary(
    tmp_path: Path,
) -> None:
    vault = create_vault(tmp_path)
    paper_dir = _make_paper(vault, "2024_a")
    (paper_dir / "supplementary").write_text("not a folder", encoding="utf-8")
    ensure_truth_locked(vault)  # no error


def test_ensure_truth_locked_empty_papers_dir(tmp_path: Path) -> None:
    """A vault with no papers does not crash (only TAXONOMY may re-lock)."""
    vault = create_vault(tmp_path)
    os.chmod(vault / "TAXONOMY.md", 0o644)
    assert ensure_truth_locked(vault) == 1


# ---------------------------------------------------------------------------
# rmtree — read-only-tolerant tree deletion (Windows os.unlink refuses a
# read-only child; the onexc handler clears the bit and retries).
# ---------------------------------------------------------------------------


def test_clear_readonly_onexc_removes_readonly_file(tmp_path: Path) -> None:
    """The onexc handler mirrors what rmtree's failed unlink hands off on
    Windows: clear the read-only bit, then re-run the op (here os.unlink)."""
    target = tmp_path / "metadata.yaml"
    target.write_text("id: x\n", encoding="utf-8")
    lock_truth_file(target)

    locking._clear_readonly_onexc(os.unlink, str(target), None)

    assert not target.exists()


@_posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the permission bits this asserts on",
)
def test_failed_rmtree_leaves_the_folder_readable_by_its_owner(
    tmp_path: Path,
) -> None:
    """A tree the handler touched but could not finish deleting stays usable.

    ``stat.S_IWRITE`` on POSIX is 0o200 — write-only. Assigning it left the
    folder un-listable and un-enterable by its own owner, and unreadable to the
    next comparison run. Survivable for vault-internal state; not for a folder
    in the user's project directory, which is where settle_hub_entry aims this.
    """
    hub = tmp_path / "hub"
    hub.mkdir()
    victim = hub / "p1"
    victim.mkdir()
    (victim / "notes.md").write_text("mine\n", encoding="utf-8")
    before = stat.S_IMODE(victim.stat().st_mode)

    hub.chmod(stat.S_IRUSR | stat.S_IXUSR)  # entries cannot be removed from it
    try:
        with pytest.raises(OSError):
            rmtree(victim)
        after = stat.S_IMODE(victim.stat().st_mode)
    finally:
        hub.chmod(stat.S_IRWXU)
        # If this assertion ever fails again, tmp_path cleanup must still be
        # able to remove what the handler left behind.
        victim.chmod(stat.S_IRWXU)

    assert after & stat.S_IRUSR, "owner can no longer list the folder"
    assert after & stat.S_IXUSR, "owner can no longer enter the folder"
    assert after & stat.S_IWUSR
    assert after == before | stat.S_IWRITE


def test_rmtree_removes_tree_with_locked_truth_files(tmp_path: Path) -> None:
    """A paper folder whose metadata.yaml / paper.pdf are locked read-only is
    fully removed. (On POSIX a bare shutil.rmtree already succeeds; this pins
    the intended API and the happy path for both platforms.)"""
    paper_dir = tmp_path / "papers" / "2024_a"
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text("id: 2024_a\n", encoding="utf-8")
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    lock_truth_file(paper_dir / "metadata.yaml")
    lock_truth_file(paper_dir / "paper.pdf")

    rmtree(paper_dir)

    assert not paper_dir.exists()


def test_rmtree_wires_the_readonly_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the wiring: rmtree must pass the read-only onexc handler through
    to shutil.rmtree so a read-only child is retried on Windows."""
    captured: dict[str, object] = {}

    def fake_rmtree(path: object, *, onexc: object = None) -> None:
        captured["onexc"] = onexc

    monkeypatch.setattr(locking.shutil, "rmtree", fake_rmtree)
    rmtree(tmp_path)

    assert captured["onexc"] is locking._clear_readonly_onexc


def test_rmtree_ignore_errors_swallows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ignore_errors=True swallows a still-failing removal; default re-raises."""

    def boom(path: object, *, onexc: object = None) -> None:
        raise OSError("still locked")

    monkeypatch.setattr(locking.shutil, "rmtree", boom)

    rmtree(tmp_path, ignore_errors=True)  # no raise
    with pytest.raises(OSError):
        rmtree(tmp_path)
