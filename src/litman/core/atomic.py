"""Atomic multi-file write helper for vault mutations.

Replaces the git-auto-commit "rollback" mechanism we dropped in M2.0.
Multi-file ops (e.g. ``lit modify`` writing both ``metadata.yaml`` and
``INDEX.json``, or ``lit taxonomy rename`` rippling across many metadata
files) stage all writes under ``<vault>/.litman-staging/<op-id>/`` and
promote them to their final paths via ``os.replace()`` (POSIX atomic rename)
only after a write-ahead commit record is durably on disk. An exception
during the body skips promotion and the staging directory is dropped — no
target file is touched.

Crash-safety boundary: the commit phase uses a manifest + sentinel
write-ahead protocol (the same shape as SQLite's journal or git's
index-lock). All staged files are fsync'd, a ``MANIFEST.json`` listing the
promotion order is written and fsync'd, then an empty ``COMMITTED``
sentinel is created and its directory fsync'd. The instant that final
directory fsync returns is the atomic decision point: a crash before it
rolls the whole op back; a crash after it is rolled *forward* — recovery
replays the manifest and finishes the half-done promotion. This makes the
helper crash-safe up to the filesystem's own atomicity guarantee on POSIX.

A write can also move whole folders (:meth:`StagedWrite.move_dir`, used by
``lit rename``). The moves are listed in the manifest and done between the
manifest and the sentinel, so a folder that cannot be moved (on Windows, a
file in it open in another program) stops the op before anything is
promoted. Recovery reads the same list: before the decision point a moved
folder is moved back, after it a folder not yet moved is moved forward.

On Windows the directory fsync is unavailable (no ``O_DIRECTORY``), so
``_fsync_dir`` degrades to a no-op (ADR-005 informational compatibility).
Without durable directory entries the protocol cannot guarantee
roll-forward across a power loss, so crash-safety on Windows degrades to
the previous "safe-on-clean-failure" level. This is declared honestly
rather than silently pretending the stronger guarantee holds.

Recovery runs automatically at every command's vault-open
(:func:`recover_staging` via the ``ensure_vault_recovered`` hook) and is
also reachable through ``lit health-check`` (M2.8).

Example::

    with staged_write(vault, op_id="modify-2024_Foo_Bar") as stage:
        stage.write_text("papers/2024_Foo_Bar/metadata.yaml", new_meta)
        stage.write_text("INDEX.json", new_index)
    # On clean exit, both targets atomically promoted; on exception, both
    # rolled back together; on a crash mid-promote, recovery rolls forward.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import TracebackType
from typing import Literal

from rich.console import Console

from litman.core.locking import (
    is_truth_lockable,
    lock_truth_file,
    unlock_truth_file,
)

_STAGING_DIRNAME = ".litman-staging"
_MANIFEST_FILENAME = "MANIFEST.json"
_SENTINEL_FILENAME = "COMMITTED"
# Folder-move markers in a leftover op dir. DIRS_DONE: a committed op's
# folders were all put in place once, so no later pass moves them again.
# DIRS_UNCLEAR: the folders were found in a state the op cannot settle; it
# holds the line reported then, and no later pass moves any folder for it.
_DIRS_DONE_FILENAME = "DIRS_DONE"
_DIRS_UNCLEAR_FILENAME = "DIRS_UNCLEAR"

# Stderr console so recovery / degrade notices don't contaminate stdout
# (which CLI consumers may pipe / parse). Module-level singleton.
_console = Console(stderr=True)

# One-shot latch for the Windows dir-fsync graceful-degrade warning. The
# notice is informative, not actionable per-call, so the first occurrence
# carries the full hint and subsequent calls stay silent for the rest of
# the process. Mirrors core/portable_link.py's stance.
_WARNED_THIS_PROCESS: bool = False


def reset_warning_state() -> None:
    """Reset the once-per-process dir-fsync degrade warning latch.

    Test-support helper: a test exercising the Windows degrade path can
    call this in setup so the warning fires deterministically.
    """
    global _WARNED_THIS_PROCESS
    _WARNED_THIS_PROCESS = False


def _make_op_id(prefix: str = "op") -> str:
    """Generate a unique-enough op id: timestamp + short random suffix.

    Format ``<prefix>-<UTC-timestamp>-<6-hex>``, e.g.
    ``op-20260428T112545-abc123``. Two calls in the same second still get
    distinct ids via the uuid4 suffix.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}-{ts}-{suffix}"


def _fsync_file(path: Path) -> None:
    """Flush ``path``'s contents to stable storage.

    Open the file, ``os.fsync`` the descriptor, close it. The caller has
    already written the bytes; this only forces them durable.

    The fd is opened ``O_WRONLY``, not the intuitive ``O_RDONLY``: on
    Windows ``os.fsync`` calls MSVCRT ``_commit()`` which requires the fd
    be opened for writing (otherwise it returns ``EBADF`` — bad file
    descriptor). POSIX ``fsync(2)`` accepts both modes, so ``O_WRONLY``
    is the minimal portable flag. Mirrors ``_fsync_dir``'s ADR-005
    cross-platform-compatibility stance.
    """
    fd = os.open(path, os.O_WRONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Flush ``path``'s directory entries to stable storage.

    Required after creating / renaming an entry inside ``path``: without
    it a crash can lose the new directory entry even though the file's
    own data is durable, leaving recovery with neither the staging file
    nor the target.

    On platforms without ``O_DIRECTORY`` (Windows) this degrades to a
    no-op and crash-safety drops to "safe-on-clean-failure" (ADR-005).
    Silently: it is a constant of the platform, true on every Windows box
    on every write, so a per-command stderr warning would nag the user
    (and pollute every agent run) about weather they cannot change — an
    environment limitation dressed up as a defect. The guarantee level is
    documented (ADR-005, docs caveats), and the staged-write recovery
    machinery already reports the moment a half-finished commit is
    actually found. A POSIX host where the directory OPEN fails is the
    opposite case — unusual and worth one line — so that arm still warns.
    """
    if sys.platform == "win32":
        return
    try:
        fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    except OSError as err:
        _warn_dir_fsync_unsupported(err)
        return
    try:
        os.fsync(fd)
    except OSError as err:
        _warn_dir_fsync_unsupported(err)
    finally:
        os.close(fd)


def _warn_dir_fsync_unsupported(err: OSError | None = None) -> None:
    """Emit a once-per-process warning when directory fsync is unavailable."""
    global _WARNED_THIS_PROCESS
    if _WARNED_THIS_PROCESS:
        return
    _WARNED_THIS_PROCESS = True
    hint = (
        "Directory fsync is unavailable on this platform, so litman's "
        "staged-write commit protocol cannot guarantee crash recovery "
        "across a power loss. It degrades to 'safe-on-clean-failure': a "
        "clean process exit is still atomic, but a hard crash mid-commit "
        "may leave the vault half-written. Run `lit health-check` after "
        "any unclean shutdown. (POSIX hosts get full crash-safety.)"
    )
    detail = f"\n[dim]    {err}[/]" if err is not None else ""
    _console.print(f"[yellow]warning:[/] {hint}{detail}")


def _rename_dir(src: Path, dst: Path) -> None:
    """Move the folder ``src`` to ``dst``.

    The one place a staged write moves a folder, forward or back, so a test
    can make the move fail without patching ``os`` for the whole process.
    """
    os.rename(src, dst)


class DirMoveError(OSError):
    """A folder move inside a staged write failed; nothing was committed.

    ``reason`` is the OS's own wording (``Access is denied``, ``No such file
    or directory``), short enough to quote inside a one-line error.
    """

    def __init__(self, src_rel: str, dst_rel: str, reason: str) -> None:
        super().__init__(f"could not move {src_rel} to {dst_rel}: {reason}")
        self.src_rel = src_rel
        self.dst_rel = dst_rel
        self.reason = reason


def _os_reason(err: OSError) -> str:
    """The OS's wording for ``err`` without the paths, which can run long."""
    return err.strerror or str(err)


def _exists(path: Path) -> bool:
    """``os.path.lexists`` that tells "not there" from "could not look".

    Only a missing entry is ``False``; any other error is raised. A folder a
    passing fault hid (a network mount hiccup, a permission flicker) must not
    be read as gone: that verdict is acted on, and once marked unclear an op
    never moves a folder again.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _remove_tree(path: Path) -> None:
    """Delete a finished op's dir, best effort.

    The one place teardown deletes, so a test can stop it partway — which is
    what a file held open by another program does to ``rmtree``.
    """
    shutil.rmtree(path, ignore_errors=True)


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of recovering one leftover staging op directory.

    Attributes:
        op_id: The op directory name under ``.litman-staging/``.
        kind: ``rolled_back`` (no sentinel — clean abort, normal),
            ``rolled_forward`` (sentinel present — torn commit finished),
            ``unrecoverable`` (sentinel present but a manifested file is
            missing from both staging and target — data lost, evidence
            preserved; also a folder move that cannot be settled this pass).
        n_files: Number of files acted on (promoted for roll-forward).
        message: Human-readable line for the recovery hook + health-check,
            or ``None`` for the silent ``rolled_back`` case. A rollback that
            had to move a folder back carries one: the command the user ran
            did not take effect, and saying nothing would hide that.
    """

    op_id: str
    kind: Literal["rolled_back", "rolled_forward", "unrecoverable"]
    n_files: int
    message: str | None = None


def _manifest_unreadable_message(op_id: str) -> str:
    """User-facing line for a torn op whose MANIFEST.json cannot be read.

    Single source of truth shared by the mutating recoverer
    (:func:`_recover_one_op`) and the read-only classifier
    (``checks.maintenance._classify_torn_op``) so the data-loss-path wording cannot
    drift between the two call sites.
    """
    return (
        f"torn commit {op_id}: its MANIFEST.json is unreadable, so it "
        f"cannot be recovered automatically; kept .litman-staging/{op_id}/ "
        "as evidence — please inspect it by hand"
    )


def _unrecoverable_message(
    op_id: str,
    lost: list[str],
    recovered: int,
    mode: Literal["done", "pending"] = "done",
) -> str:
    """User-facing line for a torn op with files missing from both sides.

    Single source of truth shared by the mutating recoverer
    (:func:`_recover_one_op`) and the read-only classifier
    (``checks.maintenance._classify_torn_op``). One parameterized template, two
    voices, so the data-loss-path wording cannot drift between the call
    sites:

    * ``mode="done"`` (mutating recoverer) — completed past-tense phrasing
      with the real promoted count: it already rolled the other files
      forward.
    * ``mode="pending"`` (read-only classifier) — conditional/future
      phrasing with the count of files that *would* be recovered; the
      probe never promotes, so claiming a completed action would be a
      lie. ``recovered`` here is the recoverable count, not 0.
    """
    if mode == "done":
        roll_forward_clause = (
            f"rolled the other {recovered} file(s) in the same op forward, "
            f"and kept .litman-staging/{op_id}/ as evidence — please inspect "
            "it by hand"
        )
    else:
        roll_forward_clause = (
            f"the other {recovered} file(s) in the same op can be rolled "
            f"forward (after running lit health-check --fix); kept "
            f".litman-staging/{op_id}/ as evidence — please inspect it by hand"
        )
    return (
        f"torn commit {op_id} is unrecoverable: "
        f"{len(lost)} file(s) missing from both staging and target "
        f"({', '.join(lost)}); {roll_forward_clause}"
    )


def _promote_failed_message(
    op_id: str, failed: list[str], recovered: int
) -> str:
    """User-facing line for a torn op whose promotion failed *this pass*.

    Distinct from :func:`_unrecoverable_message`: the staged copies are still
    present (no data lost), the underlying ``os.replace`` simply failed —
    typically the storage that tore the original commit is still unwritable
    (network-mount EIO/ESTALE, mount gone read-only). recover_staging swallows
    the error instead of letting it escape the vault-open hook (F4), preserves
    the evidence, and the next ``lit`` command retries.
    """
    recovered_clause = (
        f" ({recovered} rolled forward successfully this pass)"
        if recovered
        else ""
    )
    return (
        f"torn commit {op_id} could not be completed this pass: "
        f"{len(failed)} file(s) failed to promote "
        f"({', '.join(failed)}), likely because the underlying storage is "
        f"temporarily unwritable; kept .litman-staging/{op_id}/ and the "
        f"staged copies — the next lit command will retry automatically"
        f"{recovered_clause}"
    )


_DirMoveState = Literal["moved", "not_moved", "both", "neither"]


def _is_plain_relpath(relpath: str) -> bool:
    """True for a path that names something inside the vault.

    Judged under BOTH path flavours, never just this host's. Windows calls
    "/etc/passwd" relative — there is no drive letter — yet still re-anchors
    it to the staging root's drive when joined, so that shape sailed past
    ``is_absolute()`` there and landed on C:\\etc\\passwd, outside the vault.
    ``anchor`` is drive + root together, so it covers the driveless-rooted
    form, the bare-drive form ("C:x") and UNC alike; ".." needs its own test
    because a traversal has no anchor at all, and an empty path names the
    vault itself. A NUL byte is refused too: the OS rejects it with a
    ValueError, not an OSError, which nothing downstream expects. Asking both
    flavours costs nothing — no legitimate relpath here is anything but
    "papers/<id>/..." or a bare filename — and buys the property worth having
    in a containment check: the same verdict on every platform, by
    construction rather than by test coverage.
    """
    if "\x00" in relpath:
        return False
    posix, win = PurePosixPath(relpath), PureWindowsPath(relpath)
    return bool(posix.parts) and not (
        posix.anchor or win.anchor or ".." in posix.parts or ".." in win.parts
    )


def _dir_move_state(vault: Path, src_rel: str, dst_rel: str) -> _DirMoveState:
    """Where one manifested folder move stands on disk. Pure: stats only.

    ``moved`` (only the destination is there) and ``not_moved`` (only the
    source) are the two states the protocol can leave; ``both`` / ``neither``
    mean something else touched the folders, and only a person can say which
    one is right. Raises ``OSError`` when a folder cannot be looked at.
    """
    src_there = _exists(vault / src_rel)
    dst_there = _exists(vault / dst_rel)
    if dst_there and not src_there:
        return "moved"
    if src_there and not dst_there:
        return "not_moved"
    return "both" if src_there else "neither"


def _sort_dir_moves(
    vault: Path, moves: list[tuple[str, str]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Split manifested moves into ``(moved, not_moved, unclear)``.

    ``unclear`` holds ``(src, dst, state)`` for ``both`` / ``neither``.
    """
    moved: list[tuple[str, str]] = []
    not_moved: list[tuple[str, str]] = []
    unclear: list[tuple[str, str, str]] = []
    for src, dst in moves:
        state = _dir_move_state(vault, src, dst)
        if state == "moved":
            moved.append((src, dst))
        elif state == "not_moved":
            not_moved.append((src, dst))
        else:
            unclear.append((src, dst, state))
    return moved, not_moved, unclear


def _dir_move_plan(
    op_dir: Path,
    vault: Path,
    moves: list[tuple[str, str]],
    *,
    committed: bool,
) -> tuple[list[tuple[str, str]], str | None]:
    """What recovery still has to do with a leftover op's folder moves. Pure.

    Returns ``(to_do, blocked)``. ``to_do`` lists ``(from, to)`` moves:
    forward for a committed op, back (last first) for one that never reached
    its decision point. ``blocked`` is the line to report instead when the
    folders are in a state the op cannot settle — then nothing is moved.

    The one source of truth for the recoverer (:func:`_recover_one_op`) and
    the read-only classifier (``checks.check_stale_staging``), so the two
    cannot disagree. An op marked ``DIRS_UNCLEAR`` stays blocked. A committed
    op marked ``DIRS_DONE`` already put its folders in place once; a folder
    that is not where it left it was moved by someone since, and moving it
    again (or promoting a file that recreates it) would undo that. Raises
    ``OSError`` when a folder cannot be looked at; the recoverer turns that
    into a retry, never into a verdict.
    """
    moved, not_moved, unclear = _sort_dir_moves(vault, moves)
    marker = op_dir / _DIRS_UNCLEAR_FILENAME
    if os.path.lexists(marker):
        try:
            noted = marker.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            noted = ""
        # Empty when writing it failed halfway: say what the folders show now.
        return [], noted or _dir_moves_unclear_message(unclear)
    if committed and os.path.lexists(op_dir / _DIRS_DONE_FILENAME):
        unclear += [(src, dst, "gone") for src, dst in not_moved]
        not_moved = []
    if unclear:
        return [], _dir_moves_unclear_message(unclear)
    if committed:
        return not_moved, None
    return [(dst, src) for src, dst in reversed(moved)], None


def _moved_back_message(
    moves: list[tuple[str, str]],
    mode: Literal["done", "pending"] = "done",
) -> str:
    """User-facing line for an uncommitted op whose folder had been moved.

    ``moves`` are the ``(from, to)`` moves back. The command that started
    the op stopped before its decision point, so nothing it meant to change
    took effect. ``done`` for the recoverer, ``pending`` for the read-only
    classifier.

    Each folder is named once and the op id not at all: a rename's op id
    carries both paper ids, which put the line past 300 characters. Nobody
    has to act on the op dir — recovery removes it.
    """
    back = ", ".join(f"{to}/" for _, to in moves)
    if mode == "done":
        verb = "is" if len(moves) == 1 else "are"
        return f"Undid an unfinished command: {back} {verb} back. Rerun it if still needed."
    folders = "its folder" if len(moves) == 1 else "its folders"
    return f"An unfinished command will be undone, moving {folders} back to {back}."


# What a command prints, every time, while an op's folders stay unsettled:
# the folders and the op dir to remove are health-check's to name, so the
# line stays short however long the paper ids are.
_UNSETTLED_LINE = (
    "An unfinished command left folders it cannot settle. "
    "Run `lit health-check` to see which."
)


def _dir_moves_unclear_message(unclear: list[tuple[str, str, str]]) -> str:
    """What the folders of an op whose move nobody can settle show.

    Both folders there, or neither, or (after the op had put it in place) the
    moved folder gone again: the op cannot tell which is right, so it touches
    nothing and keeps its record for a person to read. Kept in the op's
    ``DIRS_UNCLEAR`` marker and shown by health-check, whose hint names the
    op dir to remove; a command prints :data:`_UNSETTLED_LINE` instead.
    """
    parts = []
    for src, dst, state in unclear:
        if state == "both":
            parts.append(f"{src}/ and {dst}/ both exist")
        elif state == "neither":
            parts.append(f"{src}/ and {dst}/ are both missing")
        else:
            parts.append(f"{dst}/ is gone and {src}/ is back")
    if not parts:
        return "An unfinished command left folders it cannot settle."
    return f"{'; '.join(parts)} after an unfinished command."


def _dir_move_failed_message(src_rel: str, reason: str) -> str:
    """User-facing line for a recovery folder move that failed this pass.

    Like :func:`_promote_failed_message`: nothing is lost, the move is
    retried by the next ``lit`` command. On Windows the usual cause is a file
    in the folder open in another program, so the line says to close it.
    Names only the folder that would not move, and no op id, to stay one line.
    """
    return (
        f"Could not move {src_rel}/ ({reason}). "
        "Close any file open from it, then run lit again."
    )


def _folders_unreadable_message(vault: Path, err: OSError) -> str:
    """User-facing line when recovery cannot look at an op's folders.

    Names the folder that could not be looked at, when the error says which,
    so a fault that persists can be traced to it; never the op id.
    """
    reason = _os_reason(err)
    try:
        folder = Path(err.filename).relative_to(vault).as_posix()
    except (TypeError, ValueError):
        return (
            f"Could not check the folders of an unfinished command ({reason}). "
            "The next lit command retries."
        )
    return f"Could not check {folder}/ ({reason}). The next lit command retries."


def _marker_failed_message(reason: str) -> str:
    """User-facing line when recovery cannot record that a folder moved."""
    return (
        f"Could not record an unfinished command's folder move ({reason}). "
        "The next lit command retries."
    )


def _write_marker(op_dir: Path, name: str, text: str = "") -> None:
    """Durably write a folder-move marker into ``op_dir``."""
    path = op_dir / name
    path.write_text(text, encoding="utf-8")
    _fsync_file(path)
    _fsync_dir(op_dir)


def _drop_dir_moves(op_dir: Path) -> None:
    """Rewrite the op's MANIFEST.json to move and promote nothing, atomically.

    Temp file, fsync, ``os.replace``, fsync of the dir: at every instant the
    manifest on disk is either the old one or an inert one. Its files go too:
    teardown runs only once every file is promoted, or on a rollback where
    none will be, and a leftover that still listed them would report each
    one lost on every command after the paper moved on.
    """
    manifest_path = op_dir / _MANIFEST_FILENAME
    if not os.path.lexists(manifest_path):
        return
    record = json.loads(manifest_path.read_text(encoding="utf-8"))
    record.pop("dirs", None)
    record["files"] = []
    tmp = op_dir / (_MANIFEST_FILENAME + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    _fsync_file(tmp)
    os.replace(tmp, manifest_path)
    _fsync_dir(op_dir)


def _tear_down(op_dir: Path, moved_folders: bool) -> None:
    """Remove a finished op's dir, rolled back or forward.

    An op that moved folders first loses them from its manifest. Teardown can
    stop partway — a file in the dir held open, a crash — and what is left
    must never read as a move still to make or undo: an uncommitted manifest
    listing a folder already back would move it again, and a committed one
    whose DIRS_DONE was deleted first would move it again after a hand undo.
    If the rewrite fails, the whole dir stays and the next pass tears it down.
    """
    if moved_folders:
        try:
            _drop_dir_moves(op_dir)
        except (OSError, ValueError, AttributeError):
            return
    _remove_tree(op_dir)


def op_moving_folders(vault: Path, rels: set[str]) -> str | None:
    """The leftover op in ``.litman-staging/`` that may move one of ``rels``.

    ``rels`` are vault-relative folder paths (``papers/<id>``). An op whose
    manifest is there but cannot be read counts too: it may move either. Used
    by ``lit rename`` and the ``--fix`` folder rename, which must leave such a
    folder to that op's recovery.
    """
    staging = vault / _STAGING_DIRNAME
    if not staging.is_dir():
        return None
    for child in sorted(staging.iterdir()):
        if not child.is_dir():
            continue
        unreadable = (
            os.path.lexists(child / _MANIFEST_FILENAME)
            and _read_manifest_relpaths(child) is None
        )
        moves = _read_manifest_dir_moves(child)
        if (
            unreadable
            or moves is None
            or any(src in rels or dst in rels for src, dst in moves)
        ):
            return child.name
    return None


def _read_manifest_relpaths(op_dir: Path) -> list[str] | None:
    """Read the promotion-order relpath list from an op's MANIFEST.json.

    Returns ``None`` if the manifest is absent / unreadable / malformed —
    the caller turns that into the shared unrecoverable message. Pure: no
    filesystem mutation.
    """
    manifest_path = op_dir / _MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return list(manifest["files"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _read_manifest_dir_moves(op_dir: Path) -> list[tuple[str, str]] | None:
    """Read the folder moves from an op's MANIFEST.json, in order.

    A manifest without ``"dirs"`` (every write that moves no folder, and
    every op left by an older litman) has none: ``[]``. So does one that
    cannot be read at all: it was torn while being written, before any
    folder moved (a committed op with such a manifest is caught by
    :func:`_read_manifest_relpaths`). ``None`` when ``"dirs"`` is there but
    malformed, or names a path outside the vault: the op cannot be trusted
    and is kept as evidence. Pure: no mutation.
    """
    manifest_path = op_dir / _MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    dirs = manifest.get("dirs", []) if isinstance(manifest, dict) else None
    if not isinstance(dirs, list) or not all(
        isinstance(pair, list)
        and len(pair) == 2
        and all(isinstance(rel, str) and _is_plain_relpath(rel) for rel in pair)
        for pair in dirs
    ):
        return None
    return [(src, dst) for src, dst in dirs]


def _staging_is_empty(staging_dir: Path) -> bool:
    """True if ``staging_dir`` has no entries.

    One cheap ``os.scandir`` probe that stops at the first entry — does
    NOT build a sorted list or walk the whole directory. ``library.py``
    creates ``.litman-staging/`` at vault init, so every production vault
    has an empty-but-present staging dir; treating that identically to an
    absent dir keeps the vault-open hook at ≤1 stat + 1 cheap probe.
    """
    with os.scandir(staging_dir) as it:
        return next(it, None) is None


class StagedWrite:
    """Context manager for atomic multi-file writes within a vault.

    Files are staged under ``<vault>/.litman-staging/<op_id>/`` and
    promoted to their final paths via ``os.replace()`` only after a
    durable manifest + sentinel commit record exists. An exception during
    the body skips promotion (rollback). The staging directory is removed on
    exit, EXCEPT when the commit passed its atomic decision point (the
    COMMITTED sentinel is durable) but ``_promote`` failed partway: then the
    staging dir is preserved as the only roll-forward evidence for the next
    :func:`recover_staging` pass (F3).
    """

    def __init__(self, vault: Path, op_id: str | None = None) -> None:
        self.vault = vault.resolve()
        self.op_id = op_id or _make_op_id()
        self.staging_root = self.vault / _STAGING_DIRNAME / self.op_id
        # Map relpath → (staging_path, target_path), insertion-ordered so
        # promotion happens in the order the caller staged things. Some
        # callers care: e.g. `lit refresh-views` should promote INDEX.json
        # last because views/ symlinks pointing into papers/ rely on the
        # papers/<id>/ trees already being in place.
        self._staged: dict[str, tuple[Path, Path]] = {}
        # Commit-phase progress flags consulted by _cleanup so it never
        # destroys roll-forward evidence (F3). _committed flips once the
        # COMMITTED sentinel is durable (the op is decided); _promoted flips
        # once every file is promoted. If _promote fails between the two, the
        # staging dir is the sole recovery record and must survive cleanup.
        self._committed = False
        self._promoted = False
        # Folder moves, in the order the caller asked for them, and the ones
        # actually done this commit (so _cleanup can undo exactly those).
        self._moves: list[tuple[str, str]] = []
        self._moved: list[tuple[str, str]] = []

    def __enter__(self) -> StagedWrite:
        # exist_ok=False: a collision means another op already grabbed this
        # id. With the auto-generated uuid suffix this is effectively
        # impossible; with a caller-supplied op_id it surfaces a real bug.
        self.staging_root.mkdir(parents=True, exist_ok=False)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                self._commit()
        finally:
            self._cleanup()

    # ----- staging API -------------------------------------------------

    def write_text(self, relpath: str, content: str) -> Path:
        """Stage a text-mode write to ``<vault>/<relpath>``.

        Returns the staging path (rarely needed by callers).
        """
        return self._stage(relpath, content.encode("utf-8"))

    def write_bytes(self, relpath: str, content: bytes) -> Path:
        """Stage a binary write to ``<vault>/<relpath>``."""
        return self._stage(relpath, content)

    def copy_file(self, relpath: str, src: Path) -> Path:
        """Stage a copy of the file at ``src`` as ``<vault>/<relpath>``.

        Streams the bytes (``shutil.copyfile``) instead of holding them in
        memory, for payloads such as SI archives that can run to gigabytes.
        Content only, never the mode: the staged copy must stay writable,
        because ``_fsync_file`` opens it ``O_WRONLY`` and a source that is
        itself a locked TRUTH file would otherwise pass its read-only bit on.
        Returns the staging path, so a caller can check the copy before the
        commit.
        """
        staging_path = self._staged_path(relpath)
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, staging_path)
        self._staged[relpath] = (staging_path, self.vault / relpath)
        return staging_path

    def move_dir(self, src_rel: str, dst_rel: str) -> None:
        """Move the folder ``<vault>/<src_rel>`` to ``<vault>/<dst_rel>`` on commit.

        The move happens after the manifest is durable and before the
        sentinel, so if it fails nothing is promoted and the op rolls back.
        Files that belong in the moved folder are staged at their *new*
        path; staging one under ``src_rel`` is refused at commit.
        """
        self._staged_path(src_rel)
        self._staged_path(dst_rel)
        if PurePosixPath(src_rel) == PurePosixPath(dst_rel):
            raise ValueError(f"a folder cannot be moved onto itself: {src_rel!r}")
        self._moves.append((src_rel, dst_rel))

    # ----- internals ---------------------------------------------------

    def _staged_path(self, relpath: str) -> Path:
        # The same check recovery applies to the folder moves a manifest
        # lists (_read_manifest_dir_moves).
        if not _is_plain_relpath(relpath):
            raise ValueError(
                f"relpath must be a relative path within the vault: {relpath!r}"
            )
        return self.staging_root / relpath

    def _stage(self, relpath: str, payload: bytes) -> Path:
        staging_path = self._staged_path(relpath)
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_bytes(payload)
        target_path = self.vault / relpath
        self._staged[relpath] = (staging_path, target_path)
        return staging_path

    def _commit(self) -> None:
        """Durably commit then promote all staged files.

        Five steps, each its own method so a test can monkeypatch a
        precise crash point:

        1. fsync every staged file (recovery replays them).
        2. write + fsync ``MANIFEST.json`` (promotion order and folder
           moves), fsync dir.
        3. move each folder, fsync its parent. A failure here raises
           :class:`DirMoveError` before anything is decided.
        4. write + fsync the ``COMMITTED`` sentinel, fsync dir. The
           instant this returns is the atomic decision point.
        5. promote each file in manifest order, fsync each target dir.
        """
        for src_rel, _ in self._moves:
            src = self.vault / src_rel
            for relpath, (_, target) in self._staged.items():
                if target.is_relative_to(src):
                    raise ValueError(
                        f"{relpath!r} is staged inside {src_rel!r}, which this "
                        "write moves; stage it at its new path"
                    )
        self._fsync_staged_files()
        self._write_manifest()
        self._move_dirs()
        self._write_sentinel()
        self._promote()

    def _fsync_staged_files(self) -> None:
        """fsync every staged file *and* the staging dirs that hold them.

        ``_fsync_file`` makes a staged file's data durable, but on POSIX a
        newly-created file ``d/f`` is only crash-durable once ``d`` is fsync'd
        too. Staged relpaths are frequently nested (``papers/<id>/metadata.yaml``)
        and ``_stage`` freshly mkdirs those intermediate dirs, so without also
        fsyncing each ancestor up to ``staging_root`` a power loss after the
        COMMITTED sentinel could drop a nested directory entry — leaving
        recovery unable to find the staged copy. ``staging_root`` itself is also
        fsync'd by ``_write_manifest`` / ``_write_sentinel``; re-fsyncing it here
        is harmless and idempotent.
        """
        root = self.staging_root
        dirs_to_sync: set[Path] = set()
        for staging_path, _ in self._staged.values():
            _fsync_file(staging_path)
            parent = staging_path.parent
            # Walk parent → … → staging_root (inclusive); the membership guard
            # bounds the loop even if a path somehow sat outside the root.
            while parent == root or root in parent.parents:
                dirs_to_sync.add(parent)
                if parent == root:
                    break
                parent = parent.parent
        # Deepest first: flush a child's dirent before its parent's.
        for directory in sorted(
            dirs_to_sync, key=lambda p: len(p.parts), reverse=True
        ):
            _fsync_dir(directory)

    def _write_manifest(self) -> None:
        """Write + fsync the promotion-order manifest, then fsync the dir.

        ``files`` preserves ``self._staged`` insertion order — callers
        depend on it (e.g. INDEX.json must promote last). ``dirs`` is written
        only when the op moves a folder, so an older litman still reads the
        manifest of every write that moves none.
        """
        manifest_path = self.staging_root / _MANIFEST_FILENAME
        record: dict[str, object] = {
            "op_id": self.op_id,
            "files": list(self._staged.keys()),
        }
        if self._moves:
            record["dirs"] = [[src, dst] for src, dst in self._moves]
        payload = json.dumps(record, ensure_ascii=False)
        manifest_path.write_text(payload, encoding="utf-8")
        _fsync_file(manifest_path)
        _fsync_dir(self.staging_root)

    def _move_dirs(self) -> None:
        """Move each recorded folder, in order, and fsync its parent.

        Runs after the manifest is durable, so a crash from here on leaves
        recovery the list of moves to check. An existing destination is
        refused rather than handed to ``os.rename``, which on POSIX would
        quietly replace an empty folder. Each move is noted before it is
        made: an interrupt landing right after the rename must still leave
        _cleanup a folder to move back.
        """
        for src_rel, dst_rel in self._moves:
            src, dst = self.vault / src_rel, self.vault / dst_rel
            if os.path.lexists(dst):
                raise DirMoveError(src_rel, dst_rel, "the destination exists")
            self._moved.append((src_rel, dst_rel))
            try:
                _rename_dir(src, dst)
            except OSError as err:
                self._moved.pop()
                raise DirMoveError(src_rel, dst_rel, _os_reason(err)) from err
            for parent in {src.parent, dst.parent}:
                _fsync_dir(parent)

    def _undo_moves(self) -> bool:
        """Move back every folder this commit moved, last first.

        Decided by what is on disk, not by whether the rename returned: a
        noted move may not have happened. Returns ``False`` if a folder cannot
        be moved back, or is in neither place; the staging dir then stays and
        the next :func:`recover_staging` pass settles it.
        """
        while self._moved:
            src_rel, dst_rel = self._moved[-1]
            try:
                state = _dir_move_state(self.vault, src_rel, dst_rel)
            except OSError:
                return False
            if state == "moved":
                src, dst = self.vault / src_rel, self.vault / dst_rel
                try:
                    _rename_dir(dst, src)
                except OSError:
                    return False
                for parent in {src.parent, dst.parent}:
                    _fsync_dir(parent)
            elif state != "not_moved":
                return False
            self._moved.pop()
        return True

    def _drop_sentinel(self) -> bool:
        """Remove a ``COMMITTED`` that reached disk although its write failed.

        Done before any folder is moved back: a sentinel left beside a folder
        already back in place would roll the op forward later, over a
        rollback. ``False`` if it cannot be removed; then nothing is undone and
        the op rolls forward as a whole.
        """
        sentinel = self.staging_root / _SENTINEL_FILENAME
        if not os.path.lexists(sentinel):
            return True
        try:
            sentinel.unlink()
        except OSError:
            return False
        _fsync_dir(self.staging_root)
        return True

    def _write_sentinel(self) -> None:
        """Create + fsync the empty ``COMMITTED`` sentinel, then fsync dir.

        The directory fsync returning here is the atomic decision point:
        before it the op rolls back, after it the op rolls forward.
        """
        sentinel_path = self.staging_root / _SENTINEL_FILENAME
        sentinel_path.write_bytes(b"")
        _fsync_file(sentinel_path)
        _fsync_dir(self.staging_root)
        # Decision point passed: from here a failure must roll FORWARD, so
        # _cleanup must not destroy the staging dir until _promote finishes.
        self._committed = True

    def _promote(self) -> None:
        """Promote staged files to targets in manifest (insertion) order.

        Each locked TRUTH file is unlocked immediately before its rename and
        re-locked immediately after (M32). On POSIX ``os.replace`` ignores the
        read-only bit on the *overwritten* target, but on Windows it refuses a
        read-only destination (``PermissionError`` / ``WinError 5``), so the
        unlock is mandatory there for every `lit` write command that funnels
        through staged_write (modify / taxonomy / rm / rename / project /
        trash-restore). ``unlock_truth_file`` no-ops on a first-time create.
        """
        for staging_path, target_path in self._staged.values():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            lockable = is_truth_lockable(self.vault, target_path)
            if lockable:
                unlock_truth_file(target_path)
            os.replace(staging_path, target_path)
            _fsync_dir(target_path.parent)
            if lockable:
                lock_truth_file(target_path)
        # All targets in place: the staging dir is now a spent shell that
        # _cleanup may safely remove.
        self._promoted = True

    def _cleanup(self) -> None:
        """Remove this op's staging directory, unless it is recovery evidence.

        A torn promotion (COMMITTED sentinel durable but ``_promote`` did not
        finish) leaves the staging dir as the ONLY roll-forward record;
        destroying it would strand the vault in a silent, unrecoverable torn
        state (F3). So rmtree only on a clean rollback (sentinel never
        written) or a fully completed promotion; otherwise preserve the dir
        for the next ``recover_staging`` pass (vault-open hook / health-check).

        A rollback after a folder was already moved (the sentinel write
        failed, or an interrupt) first removes a sentinel that reached disk,
        then moves the folder back. If either fails, the manifest is the only
        record of where the folder belongs, so the dir is kept for the next
        recovery pass: rolled back if the sentinel went, forward if it stayed.

        A write that moved folders is torn down through :func:`_tear_down`, so
        a removal cut short cannot leave a move for recovery to replay.

        Errors are swallowed: a leftover staging dir is harmless and the next
        recovery pass idempotently re-processes and removes it.
        """
        if self._committed and not self._promoted:
            return
        if not self._committed and self._moved:  # noqa: SIM102 — the inner if runs the rollback
            if not self._drop_sentinel() or not self._undo_moves():
                return
        if self.staging_root.exists():
            _tear_down(self.staging_root, moved_folders=bool(self._moves))


def staged_write(vault: Path, op_id: str | None = None) -> StagedWrite:
    """Create a :class:`StagedWrite` context manager for the given vault.

    See the module docstring for usage.
    """
    return StagedWrite(vault, op_id=op_id)


def _recover_one_op(op_dir: Path, vault: Path) -> RecoveryResult | None:
    """Recover a single leftover op directory.

    No sentinel → clean abort before the decision point: rmtree the op
    dir and report nothing (``rolled_back``, no message — not an anomaly).
    If the manifest lists folder moves, every folder already moved is moved
    back first (last first), and that rollback is reported.

    Sentinel present → the commit was decided and durable. Every listed
    folder not yet moved is moved first; until they all are, no file is
    promoted (it would land in a new folder beside the old one). Then
    replay the manifest in order:

    * staging file present → finish the promotion (mkdir parent,
      ``os.replace``, fsync parent dir).
    * staging missing but target present → already promoted, skip.
    * staging *and* target both missing → unrecoverable data loss for
      that relpath: preserve the op dir as evidence, do not auto-clean,
      but still roll forward this op's other recoverable files.

    All relpaths resolved with no unrecoverable entry → rmtree the op
    dir and report ``rolled_forward``.

    A folder move that fails, either way, keeps the op dir and reports an
    error for the next command to retry; both folders present, or neither,
    keeps it as evidence for a person and marks it so that no later pass
    moves a folder for it. Once a committed op's folders are in place it is
    marked so too: a later pass only replays files. Neither ever raises (F4).
    """
    op_id = op_dir.name
    sentinel = op_dir / _SENTINEL_FILENAME

    if not sentinel.is_file():
        # Before the atomic decision point.
        moves = _read_manifest_dir_moves(op_dir)
        if moves is None:
            return RecoveryResult(
                op_id=op_id,
                kind="unrecoverable",
                n_files=0,
                message=_manifest_unreadable_message(op_id),
            )
        try:
            back, blocked = _dir_move_plan(op_dir, vault, moves, committed=False)
        except OSError as err:
            return _folders_unreadable_result(op_id, vault, err)
        if blocked is not None:
            return _blocked_result(op_dir, blocked)
        for frm, to in back:
            failure = _settle_dir_move(vault, op_id, frm, to)
            if failure is not None:
                return failure
        # A clean abort is normal and silent; a folder moved back is not.
        _tear_down(op_dir, moved_folders=bool(moves))
        return RecoveryResult(
            op_id=op_id,
            kind="rolled_back",
            n_files=0,
            message=_moved_back_message(back) if back else None,
        )

    relpaths = _read_manifest_relpaths(op_dir)
    moves = _read_manifest_dir_moves(op_dir)
    if relpaths is None or moves is None:
        # Sentinel present but manifest unreadable: cannot know the
        # promotion set. Preserve evidence, do not delete.
        return RecoveryResult(
            op_id=op_id,
            kind="unrecoverable",
            n_files=0,
            message=_manifest_unreadable_message(op_id),
        )

    try:
        forward, blocked = _dir_move_plan(op_dir, vault, moves, committed=True)
    except OSError as err:
        return _folders_unreadable_result(op_id, vault, err)
    if blocked is not None:
        return _blocked_result(op_dir, blocked)
    for frm, to in forward:
        failure = _settle_dir_move(vault, op_id, frm, to)
        if failure is not None:
            return failure
    if moves and not os.path.lexists(op_dir / _DIRS_DONE_FILENAME):
        try:
            _write_marker(op_dir, _DIRS_DONE_FILENAME)
        except OSError as err:
            return RecoveryResult(
                op_id=op_id,
                kind="unrecoverable",
                n_files=0,
                message=_marker_failed_message(_os_reason(err)),
            )

    promoted = 0
    unrecoverable: list[str] = []
    promote_failed: list[str] = []
    for relpath in relpaths:
        staging_path = op_dir / relpath
        target_path = vault / relpath
        if staging_path.exists():
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                lockable = is_truth_lockable(vault, target_path)
                if lockable:
                    # See _promote: Windows os.replace refuses a read-only dest.
                    unlock_truth_file(target_path)
                os.replace(staging_path, target_path)
                _fsync_dir(target_path.parent)
                if lockable:
                    lock_truth_file(target_path)
            except OSError:
                # The promotion itself failed — typically the storage that
                # tore the original commit is still unwritable. NEVER let this
                # escape: recover_staging runs at the vault-open chokepoint, so
                # an exception here would crash every subsequent lit command,
                # `lit health-check` included, turning a recoverable tear into
                # a whole-vault DoS (F4). Keep the staged copy + evidence and
                # let the next vault-open retry.
                promote_failed.append(relpath)
                continue
            promoted += 1
        elif target_path.exists():
            # Already promoted before the crash — idempotent skip.
            continue
        else:
            # Neither side present: this file is lost.
            unrecoverable.append(relpath)

    if unrecoverable:
        # Genuine data loss (both sides gone) is the headline even if other
        # files also failed to promote this pass. Evidence preserved.
        return RecoveryResult(
            op_id=op_id,
            kind="unrecoverable",
            n_files=promoted,
            message=_unrecoverable_message(op_id, unrecoverable, promoted),
        )

    if promote_failed:
        # No data lost, but this pass could not finish promoting. Report and
        # preserve (kind="unrecoverable" so it surfaces as an error and the op
        # dir is NOT deleted); the next vault-open retries automatically.
        return RecoveryResult(
            op_id=op_id,
            kind="unrecoverable",
            n_files=promoted,
            message=_promote_failed_message(op_id, promote_failed, promoted),
        )

    _tear_down(op_dir, moved_folders=bool(moves))
    return RecoveryResult(
        op_id=op_id,
        kind="rolled_forward",
        n_files=promoted,
        message=(
            f"auto-completed torn commit {op_id} "
            f"({promoted} file(s) rolled forward)"
        ),
    )


def _folders_unreadable_result(
    op_id: str, vault: Path, err: OSError
) -> RecoveryResult:
    """Keep an op whose folders could not be looked at; retried next pass."""
    return RecoveryResult(
        op_id=op_id,
        kind="unrecoverable",
        n_files=0,
        message=_folders_unreadable_message(vault, err),
    )


def _blocked_result(op_dir: Path, blocked: str) -> RecoveryResult:
    """Keep an op whose folders cannot be settled, marked so it stays that way.

    The marker keeps ``blocked`` — what the folders showed — for
    health-check to name; the command prints :data:`_UNSETTLED_LINE`.
    Writing the marker is best effort: without it the next pass finds the
    same folders and reports the same line.
    """
    if not os.path.lexists(op_dir / _DIRS_UNCLEAR_FILENAME):
        try:
            _write_marker(op_dir, _DIRS_UNCLEAR_FILENAME, blocked)
        except OSError:
            pass
    return RecoveryResult(
        op_id=op_dir.name, kind="unrecoverable", n_files=0, message=_UNSETTLED_LINE
    )


def _settle_dir_move(
    vault: Path, op_id: str, src_rel: str, dst_rel: str
) -> RecoveryResult | None:
    """Move ``src_rel`` to ``dst_rel`` for recovery; a result only on failure.

    The failure is returned, never raised: recovery runs at the vault-open
    chokepoint, so an exception here would stop every ``lit`` command (F4).
    """
    src, dst = vault / src_rel, vault / dst_rel
    try:
        _rename_dir(src, dst)
    except (OSError, ValueError) as err:
        return RecoveryResult(
            op_id=op_id,
            kind="unrecoverable",
            n_files=0,
            message=_dir_move_failed_message(
                src_rel,
                _os_reason(err) if isinstance(err, OSError) else str(err),
            ),
        )
    for parent in {src.parent, dst.parent}:
        _fsync_dir(parent)
    return None


def recover_staging(vault: Path) -> list[RecoveryResult]:
    """Recover any leftover op directories under ``<vault>/.litman-staging/``.

    Fast path: a single ``is_dir()`` stat plus, when the dir exists, one
    cheap ``os.scandir`` emptiness probe. ``library.py`` creates
    ``.litman-staging/`` at vault init, so the clean-vault case in
    production is empty-but-*present*, not absent — both short-circuit to
    ``[]`` before any ``sorted(iterdir())`` walk. That ≤1 stat + ≤1 probe
    is the entire cost the vault-open hook pays on a clean vault.

    For each op directory, see :func:`_recover_one_op`. Silent clean-abort
    rollbacks are dropped from the returned list so the caller only sees
    genuine anomalies (``rolled_forward`` / ``unrecoverable``, and a
    ``rolled_back`` that had to move a folder back). Idempotent: a second
    call over an already-healed vault returns ``[]``.
    """
    staging_dir = vault / _STAGING_DIRNAME
    if not staging_dir.is_dir() or _staging_is_empty(staging_dir):
        return []

    results: list[RecoveryResult] = []
    for child in sorted(staging_dir.iterdir()):
        if child.is_dir():
            result = _recover_one_op(child, vault)
        else:
            # A stray file directly under .litman-staging/ (not an op
            # dir): leftover from the old cleanup semantics or manual
            # tampering. Drop it; not an anomaly worth reporting.
            try:
                child.unlink()
            except OSError:
                # Best-effort removal. recover_staging runs at the
                # vault-open chokepoint, so an exception escaping here
                # would crash every subsequent lit command (the same
                # whole-vault DoS the promote path guards against above).
                # A stray file we cannot delete is not worth that.
                pass
            result = None
        if result is not None and (
            result.kind != "rolled_back" or result.message
        ):
            results.append(result)
    return results


def ensure_vault_recovered(vault: Path) -> list[RecoveryResult]:
    """Vault-open self-heal hook: recover then surface anomalies.

    Called once per command at the vault-resolution chokepoint. Runs
    :func:`recover_staging` and prints every message it returns to
    **stderr** (never stdout — must not pollute pipes). Idempotent and
    ≤1 stat on a clean vault.
    """
    results = recover_staging(vault)
    for result in results:
        if result.message:
            if result.kind == "unrecoverable":
                _console.print(f"[red]error:[/] {result.message}")
            else:
                _console.print(f"[yellow]recovered:[/] {result.message}")
    return results


def cleanup_stale_staging(vault: Path) -> int:
    """Recovery-aware sweep of ``<vault>/.litman-staging/``.

    Delegates to :func:`recover_staging` — it no longer blind-``rmtree``s
    leftover op directories. Clean-abort dirs are rolled back, torn
    commits are rolled forward, and unrecoverable tears are preserved as
    evidence (not deleted). Used by ``lit health-check --fix`` (M2.8);
    the autofix routing in :mod:`litman.core.checks` is unchanged because
    the fixer simply calls through here.

    Returns the number of op directories acted on (rolled back or rolled
    forward). Unrecoverable ops are not counted — they were intentionally
    left in place for the user to inspect.
    """
    staging_dir = vault / _STAGING_DIRNAME
    if not staging_dir.is_dir() or _staging_is_empty(staging_dir):
        return 0
    before = {c.name for c in staging_dir.iterdir()}
    recover_staging(vault)
    after = (
        {c.name for c in staging_dir.iterdir()}
        if staging_dir.is_dir()
        else set()
    )
    return len(before - after)
