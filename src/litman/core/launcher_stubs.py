"""Windows launcher-stub management for litman's self-update paths.

uv and pipx expose litman as small launcher executables ("stubs") in a bin
directory on PATH — ``lit.exe`` plus its console-less gui-scripts twin
``litw.exe``. Each stub embeds the absolute path of the tool venv's python, so
a stub is version-agnostic: any copy of it launches whatever the venv holds.

The rule that shapes both self-update paths: **while litman runs, its own stub
cannot be replaced, overwritten, or even renamed.** Windows refusing to
overwrite a running image is the familiar half; the other half is that a uv
trampoline keeps its own image open without share-delete, so renaming it fails
with ERROR_SHARING_VIOLATION too. Measured, not assumed: an in-process
"move the stubs aside first" guard renamed the idle ``litw.exe`` and silently
skipped the running ``lit.exe``, and ``uv tool upgrade`` then died copying the
new stub over the trampoline executing it (os error 32) exactly as before.

Nothing here can therefore work around a live process. Both ``lit self-update``
and the webUI's one-click update hand the upgrade to the detached helper (see
:mod:`litman.core.self_update_helper`), which moves the stubs aside only once
litman is gone. What is left in this module runs on a *quiet* install:

* A half-failed upgrade can leave the venv current but a bin stub missing
  (uv does not re-lay entrypoints once its receipt says up-to-date). The venv
  ``Scripts`` dir still holds a good copy, so ``repair_missing_stubs`` heals
  offline with a plain file copy — no network, no version change.
* ``cleanup_leftover_old`` drops a ``.old`` the helper could not remove
  (its own running image is deletable only once that process is gone).

Everything is best-effort: callers sit on user-facing command paths, so no
function here raises on OSError; a stub that cannot be healed simply keeps
today's behavior.
"""

from __future__ import annotations

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
