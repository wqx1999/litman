"""Cross-platform relative-symlink helper (ADR-005).

litman creates relative symlinks in two places:

1. ``<vault>/views/by-{project,topic,method,status}/<tag>/<paper-id>`` —
   convenience browsing views (``core/views.py``).
2. ``<project_dir>/litman_reflib/<paper-id>`` and
   ``<project_dir>/litman_code/<repo>`` — bridges from external project working
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


_SYMLINK_SUPPORT: dict[str, bool] = {}


def symlink_supported(directory: Path) -> bool:
    """Can this filesystem create symlinks? Probed once per directory, per process.

    Probes by creating a **dangling** symlink inside ``directory`` (the target
    does not exist, so nothing can be followed through it) and unlinking it in a
    ``finally``. The probe must run in the directory being asked about, not in a
    tempdir: on Windows a vault on NTFS and a project folder on an exFAT drive
    give different answers, and Developer Mode is a per-machine — but the
    filesystem is a per-path — property.

    The result is cached per directory for the life of the process. A health
    check therefore pays one probe per distinct directory, and the long-lived
    server probes once. Callers must be Tier-2 (``health-check``) or slower:
    this writes to the filesystem and must never run on the Tier-1 per-command
    hot path (invariant #15).

    A directory that does not exist, or any ``OSError``, reads as ``False``.
    """
    key = str(directory)
    cached = _SYMLINK_SUPPORT.get(key)
    if cached is not None:
        return cached

    probe = directory / f".litman-symlink-probe-{os.getpid()}"
    ok = False
    try:
        # Dangling on purpose: a probe that resolved somewhere real could be
        # followed by a tree-walker (or an rmtree) before we remove it.
        probe.symlink_to(".litman-symlink-probe-target-does-not-exist")
        ok = True
    except OSError:
        ok = False
    finally:
        try:
            if probe.is_symlink():
                probe.unlink()
        except OSError:
            pass

    _SYMLINK_SUPPORT[key] = ok
    return ok


def symlink_hint() -> str:
    """One-line remediation hint for a filesystem that refuses symlinks.

    Shared by the creation-time warning and by ``lit health-check``'s
    ``symlink_unsupported`` finding so the two never drift apart.
    """
    if sys.platform == "win32":
        return (
            "enable Developer Mode (Settings → System → For developers, or run "
            "`start ms-settings:developers`), then `lit health-check --fix` to "
            "backfill — do NOT run litman as administrator to work around this"
        )
    return (
        "common causes: FAT32 / exFAT, some SMB or WebDAV mounts — move the "
        "vault to a filesystem that supports symlinks, then `lit health-check "
        "--fix` to backfill"
    )


def reset_symlink_support_cache() -> None:
    """Clear the per-directory probe cache.

    Test-support helper: a test that flips symlink availability mid-process
    (monkeypatching ``Path.symlink_to``) must clear the cache or it will read a
    stale verdict for the same directory.
    """
    _SYMLINK_SUPPORT.clear()


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
    obstruction: OSError | None = None
    if link_path.is_symlink() or link_path.exists():
        try:
            link_path.unlink()
        except OSError as err:
            # Pre-existing entry is a non-empty directory or otherwise
            # un-removable: remember why. If symlink_to fails below, the real
            # cause is this stale entry, NOT a platform lacking symlink
            # support — don't misdirect the user to WSL / admin shell.
            obstruction = err
    try:
        link_path.symlink_to(rel)
        return True
    except OSError as err:
        if obstruction is not None:
            _warn_link_obstructed(link_path, obstruction)
        else:
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


def _warn_link_obstructed(link_path: Path, err: OSError) -> None:
    """Warn that a stale entry blocked the symlink upsert.

    Distinct from :func:`_warn_symlink_unsupported`: here the platform DOES
    support symlinks, but the existing entry at ``link_path`` could not be
    removed first (locked file, non-empty directory), so the fix is to clear
    that entry — not to switch to WSL / enable Developer Mode. Always printed
    (no once-per-process latch): it is a specific, actionable, per-link
    condition the user needs to see.
    """
    _console.print(
        f"[yellow]warning:[/] could not replace existing entry at "
        f"{link_path}: {err}.\n"
        "[dim]    The symlink was not created. Remove that entry manually "
        "and re-run; this is NOT a symlink-support problem.[/]"
    )


def _warn_symlink_unsupported(link_path: Path, err: OSError) -> None:
    """Emit a once-per-process warning when symlinks are not supported."""
    global _WARNED_THIS_PROCESS
    if _WARNED_THIS_PROCESS:
        return
    _WARNED_THIS_PROCESS = True
    if sys.platform == "win32":
        # Developer Mode FIRST. It grants SeCreateSymbolicLinkPrivilege to
        # ordinary users, so a normal non-elevated litman can create symlinks —
        # it is the only fix that costs nothing. "Run as administrator" is NOT
        # offered as a remedy: the server spawns agent processes (ADR-020), so
        # elevating litman elevates that whole surface, and files an elevated
        # process creates end up owned by Administrators, after which the user's
        # ordinary `lit add` / `lit modify` cannot write its own library.
        hint = (
            "Symlink creation was refused. On Windows this is almost always "
            "because Developer Mode is off — turning it on lets an ordinary "
            "(non-elevated) process create symlinks:\n"
            "        Settings → System → For developers → Developer Mode\n"
            "        (or run:  start ms-settings:developers )\n"
            "    Then run `lit health-check --fix` to backfill what was "
            "skipped. Do NOT run litman as administrator to work around this.\n"
            "    Meanwhile nothing is lost: only views/by-*/ and the "
            "litman_reflib / litman_code project shortcuts are skipped. "
            "metadata.yaml and INDEX.json remain authoritative and every "
            "command (lit add / list / show / modify / taxonomy), the web UI "
            "and the agent workflow all work normally."
        )
    else:
        hint = (
            "Filesystem refused symlink creation. litman's views/by-*/ "
            "and project litman_reflib/litman_code bridges will be skipped on this "
            "run; metadata.yaml and INDEX.json remain authoritative and every "
            "command still works. Common causes: FAT32 / exFAT filesystems, "
            "some SMB or WebDAV mounts. Move the vault to a filesystem that "
            "supports symlinks, then run `lit health-check --fix` to backfill."
        )
    _console.print(
        f"[yellow]warning:[/] {hint}\n"
        f"[dim]    first failure at {link_path}: {err}[/]"
    )
