"""Windows launcher-stub management for litman's self-update paths.

uv and pipx expose litman as small launcher executables ("stubs") in a bin
directory on PATH — ``lit.exe`` plus its console-less gui-scripts twin
``litw.exe``. Each stub embeds the absolute path of the tool venv's python, so
a stub is version-agnostic: any copy of it launches whatever the venv holds.

Two Windows facts drive everything here:

* A running executable cannot be deleted or overwritten — but it CAN be
  renamed. ``uv tool upgrade`` run from ``lit`` itself therefore always died
  copying the new ``lit.exe`` over the very trampoline that was executing it
  (os error 32). Moving the stubs aside first (``rename_aside``) clears the
  destination; ``restore_or_clean`` afterwards either drops the ``.old`` copy
  (the upgrade re-created the stub) or renames it back (it did not — e.g. uv's
  "Nothing to upgrade" fast path never reinstalls entrypoints, and a bare
  delete there would leave NO stub at all).
* A half-failed upgrade can leave the venv current but a bin stub missing
  (uv does not re-lay entrypoints once its receipt says up-to-date). The venv
  ``Scripts`` dir still holds a good copy, so ``repair_missing_stubs`` heals
  offline with a plain file copy — no network, no version change.

Everything is best-effort: callers sit on user-facing command paths, so no
function here raises on OSError; a stub that cannot be moved simply keeps
today's behavior.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: The launcher names litman installs (project.scripts + project.gui-scripts).
STUB_NAMES = ("lit.exe", "litw.exe")

#: Suffix for a stub moved aside during an upgrade.
OLD_SUFFIX = ".old"


def default_bin_dir() -> Path | None:
    """The directory holding the ``lit`` launcher on PATH, if resolvable."""
    lit = shutil.which("lit")
    if lit is None:
        return None
    return Path(lit).resolve().parent


def default_source_dir() -> Path:
    """The tool venv's own scripts dir — the running interpreter's home."""
    return Path(sys.executable).resolve().parent


def installed_stubs() -> list[Path]:
    """The litman launcher stubs present in the PATH bin dir (win32 only)."""
    if sys.platform != "win32":
        return []
    bin_dir = default_bin_dir()
    if bin_dir is None:
        return []
    return [bin_dir / name for name in STUB_NAMES if (bin_dir / name).exists()]


def rename_aside(stubs: list[Path]) -> list[tuple[Path, Path]]:
    """Move each stub to ``<stub>.old`` so an upgrade can lay a fresh copy.

    Returns the ``(original, old)`` pairs actually moved; a stub that cannot
    be moved is skipped (the upgrade then behaves as it does today).
    """
    renamed: list[tuple[Path, Path]] = []
    for stub in stubs:
        old = stub.with_name(stub.name + OLD_SUFFIX)
        try:
            if old.exists():
                old.unlink()
        except OSError:
            pass  # a locked leftover; os.replace below may still overwrite it
        try:
            os.replace(stub, old)
        except OSError:
            continue
        renamed.append((stub, old))
    return renamed


def restore_or_clean(renamed: list[tuple[Path, Path]]) -> None:
    """After the upgrade ran — success OR failure — settle each moved stub.

    The upgrade re-created the stub → drop the ``.old`` copy. It did not
    (failed upgrade, or uv's "Nothing to upgrade" fast path, which never
    reinstalls entrypoints) → rename the ``.old`` back so the launcher never
    disappears. Deleting unconditionally on success would brick the install
    in that fast-path case, so this MUST run on both outcomes.
    """
    for stub, old in renamed:
        try:
            if stub.exists():
                old.unlink(missing_ok=True)
            else:
                os.replace(old, stub)
        except OSError:
            continue


def repair_missing_stubs(bin_dir: Path, source_dir: Path) -> list[str]:
    """Copy stubs missing from ``bin_dir`` back from ``source_dir``.

    Returns the names restored. No-op when the dirs coincide (conda/editable
    installs run straight from their env's scripts dir).
    """
    if bin_dir == source_dir:
        return []
    repaired: list[str] = []
    for name in STUB_NAMES:
        dst = bin_dir / name
        src = source_dir / name
        if dst.exists() or not src.is_file():
            continue
        try:
            shutil.copy2(src, dst)
        except OSError:
            continue
        repaired.append(name)
    return repaired


def cleanup_leftover_old(bin_dir: Path) -> None:
    """Delete ``*.exe.old`` leftovers a previous upgrade could not remove
    (its own running image is deletable only once that process is gone)."""
    for name in STUB_NAMES:
        try:
            (bin_dir / (name + OLD_SUFFIX)).unlink(missing_ok=True)
        except OSError:
            continue


def repair_default() -> list[str]:
    """Win32 wrapper: clean leftovers, then heal missing stubs in the PATH bin
    dir from the running venv's scripts dir. Silent no-op elsewhere."""
    if sys.platform != "win32":
        return []
    bin_dir = default_bin_dir()
    if bin_dir is None:
        return []
    cleanup_leftover_old(bin_dir)
    return repair_missing_stubs(bin_dir, default_source_dir())
