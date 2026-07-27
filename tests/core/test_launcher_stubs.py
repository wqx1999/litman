"""Tests for the Windows launcher-stub primitives (task-win-update-hardening).

The functions are pure path operations, so they run identically on POSIX with
plain files standing in for the exes. The invariants under test are the two
that killed a real install:

* ``restore_or_clean`` must settle stubs on BOTH outcomes — an upgrade that
  never re-created the stub (failure, or uv's "Nothing to upgrade" fast path)
  gets its original renamed back; deleting the ``.old`` unconditionally would
  leave no launcher at all.
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


# ------------------------------------------------------------ rename / settle


def test_rename_aside_moves_and_reports(tmp_path: Path) -> None:
    lit = _mk(tmp_path / "lit.exe")
    litw = _mk(tmp_path / "litw.exe")
    renamed = stubs.rename_aside([lit, litw])
    assert [(o.name, old.name) for o, old in renamed] == [
        ("lit.exe", "lit.exe.old"),
        ("litw.exe", "litw.exe.old"),
    ]
    assert not lit.exists() and not litw.exists()
    assert lit.with_name("lit.exe.old").exists()


def test_rename_aside_overwrites_a_stale_old(tmp_path: Path) -> None:
    lit = _mk(tmp_path / "lit.exe", "new")
    _mk(tmp_path / "lit.exe.old", "stale leftover")
    renamed = stubs.rename_aside([lit])
    assert len(renamed) == 1
    assert (tmp_path / "lit.exe.old").read_text(encoding="utf-8") == "new"


def test_settle_drops_old_when_upgrade_recreated_the_stub(tmp_path: Path) -> None:
    lit = _mk(tmp_path / "lit.exe", "v1")
    renamed = stubs.rename_aside([lit])
    _mk(lit, "v2")  # the upgrade laid a fresh stub
    stubs.restore_or_clean(renamed)
    assert lit.read_text(encoding="utf-8") == "v2"
    assert not lit.with_name("lit.exe.old").exists()


def test_settle_restores_old_when_upgrade_did_not_recreate(tmp_path: Path) -> None:
    """The brick guard: 'Nothing to upgrade' skips entrypoints — the moved
    stub MUST come back or the install loses its only launcher."""
    lit = _mk(tmp_path / "lit.exe", "v1")
    renamed = stubs.rename_aside([lit])
    stubs.restore_or_clean(renamed)  # upgrade wrote nothing
    assert lit.read_text(encoding="utf-8") == "v1"
    assert not lit.with_name("lit.exe.old").exists()


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
