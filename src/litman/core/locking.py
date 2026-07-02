"""Read-only TRUTH lock — the prevention arm of the drift guardrail (ADR-015, M32).

Three schema/governance-critical TRUTH files are kept read-only via
``os.chmod`` so manual ``vim``-save / ``rm`` / Finder edits hit friction and
the "do not hand-touch this, go through `lit`" signal, while every ``lit``
command keeps working — they write through ``os.replace()`` (rename). On POSIX
the rename ignores the read-only bit on the *overwritten* target; on Windows
``os.replace`` (``MoveFileEx``) refuses a read-only destination, so the write
path unlocks the target immediately before the replace (:func:`unlock_truth_file`)
and re-locks after.

Locked: ``papers/<id>/metadata.yaml``, ``TAXONOMY.md`` (vault root),
``papers/<id>/paper.pdf``. NOT locked: ``notes.md`` / ``discussion.md`` /
``lit-config.yaml`` / ``INDEX.json`` / ``views/`` / ``codes/`` / ``.trash/`` /
``.litman-staging/`` (rationale in ADR-015).

This module depends only on ``pathlib`` / ``os`` / ``stat`` / ``sys`` — no
import of any other litman module — so ``core/atomic.py`` (the central write
chokepoint) can import it with zero circular-import risk.

Cross-platform (ADR-005 graceful-degrade): on POSIX the lock is ``0o444``; on
Windows ``os.chmod`` toggles only the read-only attribute (``stat.S_IREAD``).
The lock is a per-machine property — it does not round-trip Google Drive
(rclone drops Unix permissions), so ``lit sync pull`` re-asserts it locally.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

# Files locked read-only, keyed by where they live. metadata.yaml and
# paper.pdf live one level under <vault>/papers/<id>/; TAXONOMY.md lives at the
# vault root. Kept as module constants so the lockable predicate and the sweep
# enumerate exactly the same set.
_PAPER_TRUTH_NAMES: frozenset[str] = frozenset({"metadata.yaml", "paper.pdf"})
_ROOT_TRUTH_NAME = "TAXONOMY.md"


def _chmod_readonly(path: Path) -> None:
    """Set ``path`` read-only, cross-platform.

    POSIX: ``0o444`` (owner/group/other read, no write/execute). Windows:
    ``stat.S_IREAD`` toggles only the read-only attribute; the other mode bits
    are ignored by the OS (ADR-005 informational compatibility — no NTFS ACL).
    """
    if sys.platform == "win32":
        os.chmod(path, stat.S_IREAD)
    else:
        os.chmod(path, 0o444)


def _chmod_writable(path: Path) -> None:
    """Clear ``path``'s read-only lock, cross-platform (inverse of
    :func:`_chmod_readonly`).

    POSIX: ``0o644``. Windows: ``stat.S_IWRITE`` clears the read-only
    attribute so ``os.replace`` will overwrite the file.
    """
    if sys.platform == "win32":
        os.chmod(path, stat.S_IWRITE)
    else:
        os.chmod(path, 0o644)


def lock_truth_file(path: Path) -> None:
    """Make a single TRUTH file read-only.

    No-op if ``path`` does not exist (e.g. a paper with no ``paper.pdf``).
    Re-chmod on an already-``0o444`` file is a cheap no-op, so no stat-guard
    is taken before locking. Only a missing file is guarded — any other
    ``os.chmod`` error (e.g. EPERM on a foreign-owned file) is allowed to
    propagate rather than be silently swallowed.
    """
    if not path.exists():
        return
    _chmod_readonly(path)


def unlock_truth_file(path: Path) -> None:
    """Clear the read-only lock on a single TRUTH file (inverse of
    :func:`lock_truth_file`).

    Needed before ``os.replace`` overwrites a locked TRUTH file on Windows,
    where ``os.replace`` (``MoveFileEx``) refuses a read-only destination and
    raises ``PermissionError`` (``WinError 5``). On POSIX the rename ignores
    the target's mode, so this is a harmless no-op there. No-op if ``path``
    does not exist (e.g. a first-time create, or a paper with no ``paper.pdf``).
    """
    if not path.exists():
        return
    _chmod_writable(path)


def is_truth_lockable(vault: Path, target: Path) -> bool:
    """Whether ``target`` is one of the three lockable TRUTH files.

    True iff ``target`` is, relative to ``vault``:
        * ``papers/<id>/metadata.yaml``
        * ``papers/<id>/paper.pdf``
        * ``TAXONOMY.md`` (vault root)

    False for ``notes.md`` / ``discussion.md`` / ``lit-config.yaml`` /
    ``INDEX.json`` and anything under ``views/`` / ``codes/`` / ``.trash/`` /
    ``.litman-staging/``. Robust to ``target`` being absolute or relative: the
    relpath is computed against the resolved vault root.
    """
    vault_resolved = vault.resolve()
    target_abs = target if target.is_absolute() else vault_resolved / target
    try:
        rel = target_abs.resolve().relative_to(vault_resolved)
    except ValueError:
        # target is outside the vault entirely.
        return False

    parts = rel.parts
    if len(parts) == 1 and parts[0] == _ROOT_TRUTH_NAME:
        return True
    if (
        len(parts) == 3
        and parts[0] == "papers"
        and parts[2] in _PAPER_TRUTH_NAMES
    ):
        return True
    return False


def ensure_truth_locked(vault: Path) -> int:
    """Idempotent vault-wide re-lock sweep. Returns the number re-locked.

    Stats ``<vault>/TAXONOMY.md`` and, for each ``<vault>/papers/<id>/``, its
    ``metadata.yaml`` + ``paper.pdf``; any that exists and is currently
    writable is locked and counted. A second run over an already-locked vault
    returns 0.

    **Stat only — never opens or reads file contents.** Enumeration uses
    ``os.scandir`` (not ``list_papers``, which parses every metadata.yaml);
    membership is a writability probe (``os.access(p, os.W_OK)``). This is the
    Tier-2 invariant-#15-compliant sweep: it touches no per-paper *content*,
    so it never breaches the Tier-1 metadata-read ban (it is wired into
    ``lit health-check`` + post-``sync pull``, never the per-command hook).
    """
    vault_resolved = vault.resolve()
    n = 0

    taxonomy = vault_resolved / _ROOT_TRUTH_NAME
    if taxonomy.exists() and os.access(taxonomy, os.W_OK):
        lock_truth_file(taxonomy)
        n += 1

    papers_root = vault_resolved / "papers"
    try:
        entries = list(os.scandir(papers_root))
    except FileNotFoundError:
        entries = []

    for entry in entries:
        if not entry.is_dir():
            continue
        for name in _PAPER_TRUTH_NAMES:
            target = Path(entry.path) / name
            if target.exists() and os.access(target, os.W_OK):
                lock_truth_file(target)
                n += 1

    return n


# ---------------------------------------------------------------------------
# Read-only-tolerant tree deletion — the delete-through-the-lock counterpart.
# ---------------------------------------------------------------------------


def _clear_readonly_onexc(func, path, _exc):  # type: ignore[no-untyped-def]
    """``shutil.rmtree`` ``onexc`` handler: clear a read-only bit and retry.

    On Windows a read-only file makes ``os.unlink`` raise ``PermissionError``
    mid-walk — this covers both the locked TRUTH files (``metadata.yaml`` /
    ``paper.pdf`` / ``TAXONOMY.md``) and read-only ``.git`` objects. Clearing
    the write bit and retrying the failed op lets the removal proceed. A no-op
    on POSIX, where ``rmtree`` ignores a child file's mode (deletion is
    governed by the parent dir's write bit); harmless if the retry still fails,
    in which case the original error re-raises to the caller.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path: Path, *, ignore_errors: bool = False) -> None:
    """``shutil.rmtree`` that tolerates read-only children, cross-platform.

    Every litman deletion that can hit a locked TRUTH file (or a cloned repo's
    read-only ``.git``) must funnel through here, not bare ``shutil.rmtree``:
    on Windows ``os.unlink`` refuses a read-only file, so a tree containing any
    locked file would otherwise fail to delete. On POSIX this behaves exactly
    like ``shutil.rmtree``. ``ignore_errors`` swallows a still-failing removal
    (best-effort cleanup) rather than raising.
    """
    try:
        shutil.rmtree(path, onexc=_clear_readonly_onexc)
    except OSError:
        if not ignore_errors:
            raise
