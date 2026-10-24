"""Tests for ``litman.core.fs_identity`` — same file or folder, or not.

The "no inode numbers" volume (some Windows network / virtual disks report
``st_ino == 0`` for every file) is played by the ``no_inodes`` fixture
(tests/conftest.py), which replaces the module's ``_stat`` seam: every stat
answers with the real device and inode 0.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from litman.core import fs_identity
from litman.core.fs_identity import Anchor, identity, same_entry


def _two_files(tmp_path: Path) -> tuple[Path, Path]:
    one = tmp_path / "one.txt"
    two = tmp_path / "two.txt"
    one.write_text("1", encoding="utf-8")
    two.write_text("2", encoding="utf-8")
    return one, two


def test_identity_is_none_without_an_inode() -> None:
    assert identity(SimpleNamespace(st_dev=7, st_ino=0)) is None
    assert identity(SimpleNamespace(st_dev=7, st_ino=42)) == (7, 42)


def test_same_entry_by_real_identity(tmp_path: Path) -> None:
    one, two = _two_files(tmp_path)
    (tmp_path / "sub").mkdir()
    assert same_entry(one, tmp_path / "sub" / ".." / "one.txt")
    assert not same_entry(one, two)
    assert same_entry(tmp_path, tmp_path / "sub" / "..")


def test_same_entry_through_a_link(tmp_path: Path) -> None:
    one, _two = _two_files(tmp_path)
    link = tmp_path / "link.txt"
    try:
        os.symlink(one, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this system cannot make a symlink here: {exc}")
    assert same_entry(one, link)


def test_without_inodes_different_files_stay_different(
    tmp_path: Path, no_inodes: None
) -> None:
    """Identity says nothing there; comparing it would make every file on the
    disk "the same" one."""
    one, two = _two_files(tmp_path)
    assert not same_entry(one, two)
    assert not same_entry(tmp_path, one)


def test_without_inodes_one_path_spelled_twice_is_the_same(
    tmp_path: Path, no_inodes: None
) -> None:
    one, _two = _two_files(tmp_path)
    (tmp_path / "sub").mkdir()
    assert same_entry(one, tmp_path / "sub" / ".." / "one.txt")
    assert same_entry(tmp_path, tmp_path / "sub" / "..")


def test_anchor_matches_only_its_own_folder(tmp_path: Path) -> None:
    one, _two = _two_files(tmp_path)
    anchor = Anchor.of(tmp_path)
    assert anchor.ident is not None
    assert anchor.matches(tmp_path)
    assert not anchor.matches(one)
    assert not anchor.matches(tmp_path / "missing")
    assert not anchor.matches(tmp_path.parent)


def test_anchor_without_inodes_falls_back_to_the_path(
    tmp_path: Path, no_inodes: None
) -> None:
    one, _two = _two_files(tmp_path)
    (tmp_path / "sub").mkdir()
    anchor = Anchor.of(tmp_path)
    assert anchor.ident is None
    assert anchor.matches(tmp_path / "sub" / "..")
    assert not anchor.matches(one)
    assert not anchor.matches(tmp_path / "sub")


def test_anchors_compare_without_touching_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = Anchor.of(tmp_path / "a")
    a_again = Anchor.of(tmp_path / "b" / ".." / "a")
    b = Anchor.of(tmp_path / "b")

    def no_io(path: object) -> None:
        raise AssertionError("same_as must not stat")

    monkeypatch.setattr(fs_identity, "_stat", no_io)
    assert a.same_as(a_again)
    assert not a.same_as(b)


def test_anchors_without_inodes_compare_by_path(
    tmp_path: Path, no_inodes: None
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = Anchor.of(tmp_path / "a")
    assert a.ident is None
    assert a.same_as(Anchor.of(tmp_path / "b" / ".." / "a"))
    assert not a.same_as(Anchor.of(tmp_path / "b"))
