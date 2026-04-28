"""Atomic multi-file write helper for vault mutations.

Replaces the git-auto-commit "rollback" mechanism we dropped in M2.0.
Multi-file ops (e.g. ``lit modify`` writing both ``metadata.yaml`` and
``INDEX.json``, or ``lit taxonomy rename`` rippling across many metadata
files) stage all writes under ``<vault>/.litman-staging/<op-id>/`` and
promote them to their final paths via ``os.replace()`` (POSIX atomic rename)
only on a clean exit. An exception during the body skips promotion and the
staging directory is dropped — no target file is touched.

Crash-safety boundary: a power loss during the promotion phase can leave
the vault half-committed. Recovery surfaces via ``lit health-check`` (M2.8),
which uses :func:`cleanup_stale_staging` to drop leftover op directories.
The current helper is "safe-on-clean-failure" not "crash-safe atomic" — a
manifest+sentinel protocol can be layered on later if needed.

Example::

    with staged_write(vault, op_id="modify-2024_Foo_Bar") as stage:
        stage.write_text("papers/2024_Foo_Bar/metadata.yaml", new_meta)
        stage.write_text("INDEX.json", new_index)
    # On clean exit, both targets atomically promoted; on exception, both
    # rolled back together.
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

_STAGING_DIRNAME = ".litman-staging"


def _make_op_id(prefix: str = "op") -> str:
    """Generate a unique-enough op id: timestamp + short random suffix.

    Format ``<prefix>-<UTC-timestamp>-<6-hex>``, e.g.
    ``op-20260428T112545-abc123``. Two calls in the same second still get
    distinct ids via the uuid4 suffix.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}-{ts}-{suffix}"


class StagedWrite:
    """Context manager for atomic multi-file writes within a vault.

    Files are staged under ``<vault>/.litman-staging/<op_id>/`` and
    promoted to their final paths via ``os.replace()`` only when the
    context exits cleanly. An exception during the body skips promotion
    (rollback). Either way the staging directory is removed on exit.
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
        """Promote all staged files to their target paths."""
        for staging_path, target_path in self._staged.values():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_path, target_path)

    def _cleanup(self) -> None:
        """Remove this op's staging directory.

        Errors are swallowed: a leftover empty staging dir is harmless and
        ``cleanup_stale_staging`` will sweep it up on the next health-check.
        """
        if self.staging_root.exists():
            shutil.rmtree(self.staging_root, ignore_errors=True)


def staged_write(vault: Path, op_id: str | None = None) -> StagedWrite:
    """Create a :class:`StagedWrite` context manager for the given vault.

    See the module docstring for usage.
    """
    return StagedWrite(vault, op_id=op_id)


def cleanup_stale_staging(vault: Path) -> int:
    """Drop any leftover op directories under ``<vault>/.litman-staging/``.

    Used by ``lit health-check`` (M2.8) to clean up after crashed runs.
    Returns the number of entries removed.
    """
    staging_dir = vault / _STAGING_DIRNAME
    if not staging_dir.is_dir():
        return 0
    n = 0
    for child in staging_dir.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
            n += 1
        elif child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            n += 1
    return n
