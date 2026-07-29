"""Tests for the Windows launcher-stub primitives (task-win-update-hardening).

The functions are pure path operations, so they run identically on POSIX with
plain files standing in for the exes. What is left here only ever runs on a
*quiet* install — nothing in this module can move a launcher out from under a
live litman, which is why both self-update paths upgrade from a detached
helper (see :mod:`litman.core.self_update_helper`). The invariant under test:

* ``repair_missing_stubs`` heals a stub the installer lost (uv#11930 leaves
  the venv current but never re-lays entrypoints) by a plain offline copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

from litman.core import launcher_stubs as stubs


def _mk(p: Path, content: str = "exe") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ----------------------------------------------------------- no in-place trick


def test_no_in_process_rename_helpers_survive() -> None:
    """The 'move the running stub aside' guard is gone for good.

    Measured on Windows 11 / uv 0.11: the idle ``litw.exe`` moved, the running
    ``lit.exe`` raised ERROR_SHARING_VIOLATION, the failure was swallowed, and
    ``uv tool upgrade`` died with os error 32 exactly as it had before.
    Re-introducing either name would resurrect a guard that cannot work — the
    upgrade must run from the detached helper.
    """
    assert not hasattr(stubs, "rename_aside")
    assert not hasattr(stubs, "restore_or_clean")


# --------------------------------------------------------------------- repair


def test_repair_copies_only_missing_stubs(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    src_dir = tmp_path / "venv" / "Scripts"
    _mk(bin_dir / "lit.exe", "bin lit")
    _mk(src_dir / "lit.exe", "venv lit")
    _mk(src_dir / "litw.exe", "venv litw")
    assert stubs.repair_missing_stubs(bin_dir, src_dir) == ["litw.exe"]
    assert (bin_dir / "litw.exe").read_text(encoding="utf-8") == "venv litw"
    # The present stub was not touched.
    assert (bin_dir / "lit.exe").read_text(encoding="utf-8") == "bin lit"


def test_repair_skips_when_source_lacks_the_stub(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    src_dir = tmp_path / "src"
    bin_dir.mkdir()
    src_dir.mkdir()
    assert stubs.repair_missing_stubs(bin_dir, src_dir) == []


def test_repair_noops_when_dirs_coincide(tmp_path: Path) -> None:
    """conda / editable installs run straight from their env's scripts dir."""
    _mk(tmp_path / "lit.exe")
    assert stubs.repair_missing_stubs(tmp_path, tmp_path) == []


# -------------------------------------------------------------------- cleanup


def test_cleanup_removes_only_old_leftovers(tmp_path: Path) -> None:
    _mk(tmp_path / "lit.exe.old")
    _mk(tmp_path / "litw.exe.old")
    keep = _mk(tmp_path / "lit.exe")
    stubs.cleanup_leftover_old(tmp_path)
    assert keep.exists()
    assert not (tmp_path / "lit.exe.old").exists()
    assert not (tmp_path / "litw.exe.old").exists()


# ------------------------------------------------------------- platform gates


def test_installed_stubs_is_empty_off_windows() -> None:
    if sys.platform != "win32":
        assert stubs.installed_stubs() == []


def test_repair_default_is_noop_off_windows() -> None:
    if sys.platform != "win32":
        assert stubs.repair_default() == []
