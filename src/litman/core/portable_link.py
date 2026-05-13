"""Cross-platform relative-symlink helper (ADR-005).

litman creates relative symlinks in two places:

1. ``<vault>/views/by-{project,topic,method,status}/<tag>/<paper-id>`` —
   convenience browsing views (``core/views.py``).
2. ``<project_dir>/literature/<paper-id>`` and
   ``<project_dir>/code/<repo>`` — bridges from external project working
   directories into the vault (``core/project_link.py``).

These symlinks are pure **convenience** — the authoritative data lives in
``papers/<id>/metadata.yaml`` and ``INDEX.json``. So on filesystems that
refuse to create symlinks (Windows without Developer Mode / Administrator
privilege, FAT32, exFAT, some SMB / WebDAV mounts), litman degrades
gracefully: a one-shot Rich warning is printed to stderr and the call
returns ``False``. The dependent commands (``lit refresh-views``,
``lit link``, etc.) keep running; only the on-disk convenience artifacts
are absent. Every metadata-touching command (``lit add``, ``lit list``,
``lit show``, ``lit modify``, ``lit taxonomy``) remains fully functional.

The warning is emitted **once per process** to avoid spamming users who
rebuild many views in a batch. Tests can reset the latch via
``reset_warning_state()``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

# Stderr console so warnings don't contaminate stdout (which CLI consumers
# may pipe / parse). Module-level singleton because the warning is rare
# and we don't want to re-instantiate per call.
_console = Console(stderr=True)

# One-shot latch: the warning is informative, not actionable per-call, so
# the first occurrence carries the full hint and subsequent failures stay
# silent for the rest of the process. ``reset_warning_state()`` exists so
# pytest can verify the warning emission deterministically.
_WARNED_THIS_PROCESS: bool = False


def make_relative_symlink(link_path: Path, target_path: Path) -> bool:
    """Create a relative symlink ``link_path`` → ``target_path``.

    Both inputs are absolute paths. The stored target is the path of
    ``target_path`` relative to ``link_path.parent``, so that copying the
    entire vault to a new machine preserves resolution.

    The parent of ``link_path`` is created if missing. Any pre-existing
    entry at ``link_path`` (symlink or regular file) is removed first so
    the operation is a true upsert.

    On platforms where the filesystem refuses the symlink (Windows
    without Developer Mode, FAT32/exFAT, etc.), the ``OSError`` is caught,
    a one-shot warning is emitted, and the function returns ``False``.
    The caller should treat ``False`` as "convenience link absent" — the
    source-of-truth metadata is untouched and the command can proceed.

    Returns:
        ``True`` on success, ``False`` on graceful degrade.
    """
    rel = os.path.relpath(target_path, link_path.parent)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.is_symlink() or link_path.exists():
        try:
            link_path.unlink()
        except OSError:
            # Pre-existing entry is a non-empty directory or otherwise
            # un-removable: leave it alone and let symlink_to fail below.
            pass
    try:
        link_path.symlink_to(rel)
        return True
    except OSError as err:
        _warn_symlink_unsupported(link_path, err)
        return False


def remove_link_if_present(link_path: Path) -> bool:
    """Remove the symlink at ``link_path`` if it is in fact a symlink.

    Refuses to delete real files or directories — only symlinks. On
    platforms where the link was never created (graceful degrade above),
    this is a silent no-op returning ``False``.

    Returns:
        ``True`` if a symlink was removed, ``False`` otherwise.
    """
    if not link_path.is_symlink():
        return False
    try:
        link_path.unlink()
        return True
    except OSError:
        return False


def reset_warning_state() -> None:
    """Reset the once-per-process warning latch.

    Test-support helper: each test that exercises the degrade path can
    call this in setup so the warning fires deterministically.
    """
    global _WARNED_THIS_PROCESS
    _WARNED_THIS_PROCESS = False


def _warn_symlink_unsupported(link_path: Path, err: OSError) -> None:
    """Emit a once-per-process warning when symlinks are not supported."""
    global _WARNED_THIS_PROCESS
    if _WARNED_THIS_PROCESS:
        return
    _WARNED_THIS_PROCESS = True
    if sys.platform == "win32":
        hint = (
            "Symlink creation refused on this Windows installation. "
            "litman's views/by-*/ and project literature/code bridges "
            "will be skipped on this run; metadata.yaml and INDEX.json "
            "remain authoritative, and every metadata-touching command "
            "(lit add / list / show / modify / taxonomy) is unaffected. "
            "To enable symlinks, turn on Windows Developer Mode "
            "(Settings > Update & Security > For developers) and re-run "
            "the command, or run your terminal as Administrator."
        )
    else:
        hint = (
            "Filesystem refused symlink creation. litman's views/by-*/ "
            "and project literature/code bridges will be skipped on this "
            "run; metadata.yaml and INDEX.json remain authoritative. "
            "Common causes: FAT32 / exFAT filesystems, some SMB or "
            "WebDAV mounts. Move the vault to a filesystem that supports "
            "symlinks to enable these convenience features."
        )
    _console.print(
        f"[yellow]warning:[/] {hint}\n"
        f"[dim]    first failure at {link_path}: {err}[/]"
    )
