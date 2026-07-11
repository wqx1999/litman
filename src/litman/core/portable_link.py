"""Cross-platform folder links (ADR-005).

litman places folder links in two places:

1. ``<vault>/views/by-{project,topic,method,status}/<tag>/<paper-id>`` —
   convenience browsing views (``core/views.py``).
2. ``<project_dir>/litman_reflib/<paper-id>`` and
   ``<project_dir>/litman_code/<repo>`` — bridges from external project working
   directories into the vault (``core/project_link.py``).

Every one of them links to a DIRECTORY, which allows one mechanism per
platform:

- POSIX: a **relative symlink**. The stored target survives copying or moving
  the vault as a unit.
- Windows: a **directory junction** — the native directory link that any
  standard user can create, with no Developer Mode, no elevation, no prompt.
  Junctions store absolute targets, so a moved vault leaves them stale; that
  is the same detected-and-repaired failure class as the project bridges
  (``lit health-check --fix``, the Tier-1 rebuild prompt, the GUI rebuild on
  vault switch). To Python's detection calls a junction reads as a symlink
  (Windows lstat marks name-surrogate reparse points ``S_IFLNK``), so the
  checks, regen and removal code paths need no platform branches.

Windows never falls back to symlinks: a filesystem where junctions are
impossible (no reparse points — FAT32, exFAT, network shares) refuses
symlinks too, and probing the privileged mechanism would split Windows users
into two behaviors. On such filesystems litman degrades gracefully instead:
links are skipped, a one-shot stderr warning names the cause,
``lit health-check`` reports it once as info, and the web UI shows a
dismissible notice. The links are pure **convenience** — the authoritative
data lives in ``papers/<id>/metadata.yaml`` and ``INDEX.json``, and every
command keeps working without them.
"""

from __future__ import annotations

import os
import subprocess
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


_LINK_MECHANISM: dict[str, str] = {}


def _probe_symlink(directory: Path) -> bool:
    """POSIX probe: will this filesystem hold a symlink?"""
    probe = directory / f".litman-link-probe-{os.getpid()}"
    ok = False
    try:
        # Dangling on purpose: a probe that resolved somewhere real could be
        # followed by a tree-walker (or an rmtree) before we remove it.
        probe.symlink_to(".litman-link-probe-target-does-not-exist")
        ok = True
    except OSError:
        ok = False
    finally:
        try:
            if probe.is_symlink():
                probe.unlink()
        except OSError:
            pass
    return ok


def _probe_junction(directory: Path) -> bool:
    """Windows probe: will this filesystem hold a directory junction?

    Unlike the symlink probe this one needs a real target —
    ``_winapi.CreateJunction`` refuses a nonexistent source — so it creates a
    scratch directory next to the probe link and removes both in ``finally``.
    """
    target = directory / f".litman-link-probe-target-{os.getpid()}"
    link = directory / f".litman-link-probe-{os.getpid()}"
    try:
        target.mkdir()
    except OSError:
        return False
    ok = False
    try:
        _create_junction(link, target)
        ok = True
    except OSError:
        ok = False
    finally:
        try:
            if link.is_symlink() or link.is_junction():
                try:
                    link.unlink()
                except OSError:
                    # A junction is a directory entry to some delete paths;
                    # rmdir removes the reparse point itself and can never
                    # descend into the target.
                    link.rmdir()
        except OSError:
            pass
        try:
            target.rmdir()
        except OSError:
            pass
    return ok


def _create_junction(link_path: Path, target_path: Path) -> None:
    """Create a directory junction ``link_path`` → ``target_path`` (win32 only).

    ``_winapi.CreateJunction`` is CPython's own junction constructor. It
    requires the target to exist (always true at litman's call sites), and its
    privilege adjustment is opportunistic — standard users succeed, exactly as
    with ``mklink /J``. Running elevated is neither needed nor wanted
    (ADR-020: the server spawns agent processes, and files created elevated
    lock the user's ordinary commands out of their own library).

    ``mklink /J`` is the fallback only for a Python build whose ``_winapi``
    lacks the function. A plain ``OSError`` (e.g. a filesystem that cannot
    hold reparse points) propagates so the caller degrades to the advisory
    tier.
    """
    try:
        import _winapi
    except ImportError:
        _winapi = None
    if _winapi is not None and hasattr(_winapi, "CreateJunction"):
        _winapi.CreateJunction(str(target_path), str(link_path))
        return
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise OSError(f"mklink /J failed for {link_path}: {detail}")


def link_mechanism(directory: Path) -> str:
    """Which folder-link mechanism works in ``directory``.

    Returns ``"symlink"`` (POSIX), ``"junction"`` (Windows) or ``"none"``.
    One mechanism per platform — Windows probes junctions and never symlinks,
    so every Windows install behaves the same whatever its privilege state.

    Probed once per directory, per process, and probed in the directory being
    asked about rather than a tempdir: a vault on an internal drive and a
    project folder on an exFAT stick give different answers, because link
    support is a per-filesystem property. A health check therefore pays one
    probe per distinct directory, and the long-lived server probes once.
    Callers must be Tier-2 (``health-check``) or slower: this writes to the
    filesystem and must never run on the Tier-1 per-command hot path
    (invariant #15).

    A directory that does not exist, or any ``OSError``, reads as ``"none"``.
    """
    key = str(directory)
    cached = _LINK_MECHANISM.get(key)
    if cached is not None:
        return cached
    if sys.platform == "win32":
        mechanism = "junction" if _probe_junction(directory) else "none"
    else:
        mechanism = "symlink" if _probe_symlink(directory) else "none"
    _LINK_MECHANISM[key] = mechanism
    return mechanism


def links_supported(directory: Path) -> bool:
    """Can litman materialize folder links in ``directory`` at all?"""
    return link_mechanism(directory) != "none"


def links_unsupported_hint() -> str:
    """One-line remediation hint for a filesystem that cannot hold folder links.

    Shared by the creation-time warning and ``lit health-check``'s
    ``links_unsupported`` finding so the two never drift apart.

    Deliberately free of system-settings instructions. By the time this fires
    the cause is the filesystem itself — Windows uses junctions, which any
    standard user can create on a normal internal drive — so the only remedy
    is a different drive. Developer Mode, elevation or WSL would change
    nothing here, and sending users into system dialogs reads as malware.
    """
    return (
        "common causes: FAT32 / exFAT drives (USB sticks, SD cards) and "
        "network shares — keep the library on an internal drive, then "
        "`lit health-check --fix` backfills the skipped links"
    )


def reset_link_probe_cache() -> None:
    """Clear the per-directory probe cache.

    Test-support helper: a test that flips link availability mid-process
    (monkeypatching ``Path.symlink_to`` or ``_create_junction``) must clear
    the cache or it will read a stale verdict for the same directory.
    """
    _LINK_MECHANISM.clear()


def make_portable_link(link_path: Path, target_path: Path) -> bool:
    """Create the folder link ``link_path`` → ``target_path``.

    Both inputs are absolute paths. On POSIX the stored target is the path of
    ``target_path`` relative to ``link_path.parent``, so that copying the
    entire vault to a new machine preserves resolution. On Windows the link is
    a directory junction (absolute target by nature; staleness after a move is
    repaired by the same machinery that repairs moved project bridges).

    The parent of ``link_path`` is created if missing. Any pre-existing
    entry at ``link_path`` (link or regular file) is removed first so
    the operation is a true upsert.

    On filesystems that refuse the link (FAT32/exFAT, network shares), the
    ``OSError`` is caught, a one-shot warning is emitted, and the function
    returns ``False``. The caller should treat ``False`` as "convenience link
    absent" — the source-of-truth metadata is untouched and the command can
    proceed.

    Returns:
        ``True`` on success, ``False`` on graceful degrade.
    """
    link_path.parent.mkdir(parents=True, exist_ok=True)
    obstruction: OSError | None = None
    if link_path.is_symlink() or link_path.is_junction() or link_path.exists():
        try:
            _remove_existing_entry(link_path)
        except OSError as err:
            # Pre-existing entry is a non-empty directory or otherwise
            # un-removable: remember why. If link creation fails below, the
            # real cause is this stale entry, NOT a filesystem lacking link
            # support — don't misdirect the user to a different drive.
            obstruction = err
    try:
        if sys.platform == "win32":
            _create_junction(link_path, target_path)
        else:
            link_path.symlink_to(os.path.relpath(target_path, link_path.parent))
        return True
    except OSError as err:
        if obstruction is not None:
            _warn_link_obstructed(link_path, obstruction)
        else:
            _warn_links_unsupported(link_path, err)
        return False


def _remove_existing_entry(path: Path) -> None:
    """Remove whatever sits at ``path`` so the link upsert can proceed.

    Links (symlinks and junctions) go through ``unlink`` with an ``rmdir``
    fallback. A real file unlinks. A real directory raises — refusing to
    flatten user data into a link is the point; the caller reports that as an
    obstruction, not as missing link support.
    """
    if path.is_symlink() or path.is_junction():
        try:
            path.unlink()
        except OSError:
            path.rmdir()
        return
    path.unlink()


def remove_link_if_present(link_path: Path) -> bool:
    """Remove the folder link at ``link_path`` if it is in fact a link.

    Refuses to delete real files or directories — only links (symlinks and,
    on Windows, junctions; both read as symlinks to ``is_symlink()`` there,
    the ``is_junction()`` check is belt-and-braces). On filesystems where the
    link was never created (graceful degrade above), this is a silent no-op
    returning ``False``.

    Returns:
        ``True`` if a link was removed, ``False`` otherwise.
    """
    if not (link_path.is_symlink() or link_path.is_junction()):
        return False
    try:
        link_path.unlink()
        return True
    except OSError:
        try:
            link_path.rmdir()
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
    """Warn that a stale entry blocked the link upsert.

    Distinct from :func:`_warn_links_unsupported`: here the filesystem DOES
    support links, but the existing entry at ``link_path`` could not be
    removed first (locked file, non-empty directory), so the fix is to clear
    that entry — not to move the library. Always printed (no once-per-process
    latch): it is a specific, actionable, per-link condition the user needs
    to see.
    """
    _console.print(
        f"[yellow]warning:[/] could not replace existing entry at "
        f"{link_path}: {err}.\n"
        "[dim]    The link was not created. Remove that entry manually "
        "and re-run; this is NOT a link-support problem.[/]"
    )


def _warn_links_unsupported(link_path: Path, err: OSError) -> None:
    """Emit a once-per-process warning when folder links cannot be created.

    One message for every platform, with no system-settings instructions.
    Windows creates junctions, which need no privilege on any normal internal
    drive, so by the time this fires the cause is the filesystem itself
    (FAT32 / exFAT, network shares) — a fact about the drive that Developer
    Mode, elevation or WSL would not change. And litman must never run
    elevated regardless (ADR-020: the server spawns agent processes, and
    files created by an elevated process lock the user's ordinary commands
    out of their own library).
    """
    global _WARNED_THIS_PROCESS
    if _WARNED_THIS_PROCESS:
        return
    _WARNED_THIS_PROCESS = True
    _console.print(
        "[yellow]warning:[/] this filesystem cannot hold folder links, so the "
        "views/by-*/ browsing folders and the litman_reflib / litman_code "
        "project shortcuts are skipped ("
        + links_unsupported_hint()
        + ").\n"
        "[dim]    Nothing is lost: metadata.yaml and INDEX.json remain "
        "authoritative, and every command, the web UI and the agent "
        "workflow work normally.\n"
        f"    first failure at {link_path}: {err}[/]"
    )
