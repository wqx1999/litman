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
from datetime import datetime, timezone
from pathlib import Path
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
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of recovering one leftover staging op directory.

    Attributes:
        op_id: The op directory name under ``.litman-staging/``.
        kind: ``rolled_back`` (no sentinel — clean abort, normal),
            ``rolled_forward`` (sentinel present — torn commit finished),
            ``unrecoverable`` (sentinel present but a manifested file is
            missing from both staging and target — data lost, evidence
            preserved).
        n_files: Number of files acted on (promoted for roll-forward).
        message: Human-readable line for the recovery hook + health-check,
            or ``None`` for the silent ``rolled_back`` case.
    """

    op_id: str
    kind: Literal["rolled_back", "rolled_forward", "unrecoverable"]
    n_files: int
    message: str | None = None


def _manifest_unreadable_message(op_id: str) -> str:
    """User-facing line for a torn op whose MANIFEST.json cannot be read.

    Single source of truth shared by the mutating recoverer
    (:func:`_recover_one_op`) and the read-only classifier
    (``checks._classify_torn_op``) so the data-loss-path wording cannot
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
    (``checks._classify_torn_op``). One parameterized template, two
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

    def __enter__(self) -> "StagedWrite":
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

    # ----- internals ---------------------------------------------------

    def _staged_path(self, relpath: str) -> Path:
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(
                f"relpath must be a relative path within the vault: {relpath!r}"
            )
        return self.staging_root / rel

    def _stage(self, relpath: str, payload: bytes) -> Path:
        staging_path = self._staged_path(relpath)
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_bytes(payload)
        target_path = self.vault / relpath
        self._staged[relpath] = (staging_path, target_path)
        return staging_path

    def _commit(self) -> None:
        """Durably commit then promote all staged files.

        Four steps, each its own method so a test can monkeypatch a
        precise crash point:

        1. fsync every staged file (recovery replays them).
        2. write + fsync ``MANIFEST.json`` (promotion order), fsync dir.
        3. write + fsync the ``COMMITTED`` sentinel, fsync dir. The
           instant this returns is the atomic decision point.
        4. promote each file in manifest order, fsync each target dir.
        """
        self._fsync_staged_files()
        self._write_manifest()
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
        depend on it (e.g. INDEX.json must promote last).
        """
        manifest_path = self.staging_root / _MANIFEST_FILENAME
        payload = json.dumps(
            {"op_id": self.op_id, "files": list(self._staged.keys())},
            ensure_ascii=False,
        )
        manifest_path.write_text(payload, encoding="utf-8")
        _fsync_file(manifest_path)
        _fsync_dir(self.staging_root)

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

        Errors are swallowed: a leftover staging dir is harmless and the next
        recovery pass idempotently re-processes and removes it.
        """
        if self._committed and not self._promoted:
            return
        if self.staging_root.exists():
            shutil.rmtree(self.staging_root, ignore_errors=True)


def staged_write(vault: Path, op_id: str | None = None) -> StagedWrite:
    """Create a :class:`StagedWrite` context manager for the given vault.

    See the module docstring for usage.
    """
    return StagedWrite(vault, op_id=op_id)


def _recover_one_op(op_dir: Path, vault: Path) -> RecoveryResult | None:
    """Recover a single leftover op directory.

    No sentinel → clean abort before the decision point: rmtree the op
    dir and report nothing (``rolled_back``, no message — not an anomaly).

    Sentinel present → the commit was decided and durable. Replay the
    manifest in order:

    * staging file present → finish the promotion (mkdir parent,
      ``os.replace``, fsync parent dir).
    * staging missing but target present → already promoted, skip.
    * staging *and* target both missing → unrecoverable data loss for
      that relpath: preserve the op dir as evidence, do not auto-clean,
      but still roll forward this op's other recoverable files.

    All relpaths resolved with no unrecoverable entry → rmtree the op
    dir and report ``rolled_forward``.
    """
    op_id = op_dir.name
    sentinel = op_dir / _SENTINEL_FILENAME

    if not sentinel.is_file():
        # Clean abort before the atomic decision point. Normal, silent.
        shutil.rmtree(op_dir, ignore_errors=True)
        return RecoveryResult(
            op_id=op_id, kind="rolled_back", n_files=0, message=None
        )

    relpaths = _read_manifest_relpaths(op_dir)
    if relpaths is None:
        # Sentinel present but manifest unreadable: cannot know the
        # promotion set. Preserve evidence, do not delete.
        return RecoveryResult(
            op_id=op_id,
            kind="unrecoverable",
            n_files=0,
            message=_manifest_unreadable_message(op_id),
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

    shutil.rmtree(op_dir, ignore_errors=True)
    return RecoveryResult(
        op_id=op_id,
        kind="rolled_forward",
        n_files=promoted,
        message=(
            f"auto-completed torn commit {op_id} "
            f"({promoted} file(s) rolled forward)"
        ),
    )


def recover_staging(vault: Path) -> list[RecoveryResult]:
    """Recover any leftover op directories under ``<vault>/.litman-staging/``.

    Fast path: a single ``is_dir()`` stat plus, when the dir exists, one
    cheap ``os.scandir`` emptiness probe. ``library.py`` creates
    ``.litman-staging/`` at vault init, so the clean-vault case in
    production is empty-but-*present*, not absent — both short-circuit to
    ``[]`` before any ``sorted(iterdir())`` walk. That ≤1 stat + ≤1 probe
    is the entire cost the vault-open hook pays on a clean vault.

    For each op directory, see :func:`_recover_one_op`. Clean-abort
    rollbacks (``rolled_back``) are dropped from the returned list so the
    caller only sees genuine anomalies (``rolled_forward`` /
    ``unrecoverable``). Idempotent: a second call over an already-healed
    vault returns ``[]``.
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
        if result is not None and result.kind != "rolled_back":
            results.append(result)
    return results


def ensure_vault_recovered(vault: Path) -> list[RecoveryResult]:
    """Vault-open self-heal hook: recover then surface anomalies.

    Called once per command at the vault-resolution chokepoint. Runs
    :func:`recover_staging` and prints any ``rolled_forward`` /
    ``unrecoverable`` message to **stderr** (never stdout — must not
    pollute pipes). Idempotent and ≤1 stat on a clean vault.
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
