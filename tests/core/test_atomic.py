"""Tests for `litman.core.atomic.staged_write`."""

from __future__ import annotations

from pathlib import Path

import pytest

from litman.core.atomic import (
    StagedWrite,
    cleanup_stale_staging,
    staged_write,
)
from litman.core.library import create_vault


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ---------------------------------------------------------------------------
# Happy path: promotion on clean exit
# ---------------------------------------------------------------------------


def test_staged_write_promotes_on_success(vault: Path) -> None:
    target = vault / "INDEX.json"
    original = target.read_text()
    assert "n_papers" in original  # sanity: seed exists

    with staged_write(vault) as stage:
        stage.write_text("INDEX.json", '{"replaced": true}\n')

    assert target.read_text() == '{"replaced": true}\n'
    # Staging dir cleaned up.
    assert list((vault / ".litman-staging").iterdir()) == []


def test_staged_write_multiple_files_all_promoted(vault: Path) -> None:
    with staged_write(vault) as stage:
        stage.write_text("INDEX.json", '{"a": 1}')
        stage.write_text("notes/methods/foo.md", "# foo\n")
        stage.write_text("papers/2024_X_y/metadata.yaml", "id: 2024_X_y\n")

    assert (vault / "INDEX.json").read_text() == '{"a": 1}'
    assert (vault / "notes/methods/foo.md").read_text() == "# foo\n"
    assert (vault / "papers/2024_X_y/metadata.yaml").read_text() == "id: 2024_X_y\n"


def test_staged_write_creates_target_parent_dirs(vault: Path) -> None:
    # Deep new path the vault's seed doesn't include.
    with staged_write(vault) as stage:
        stage.write_text("papers/new_paper/sub/notes.md", "hi")

    assert (vault / "papers/new_paper/sub/notes.md").read_text() == "hi"


def test_staged_write_write_bytes(vault: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\n"
    with staged_write(vault) as stage:
        stage.write_bytes("inbox/blob.bin", payload)

    assert (vault / "inbox/blob.bin").read_bytes() == payload


# ---------------------------------------------------------------------------
# Rollback path: exception during body
# ---------------------------------------------------------------------------


def test_staged_write_rollback_on_exception(vault: Path) -> None:
    target = vault / "INDEX.json"
    before = target.read_text()

    with pytest.raises(RuntimeError, match="boom"):
        with staged_write(vault) as stage:
            stage.write_text("INDEX.json", '{"never": "promoted"}')
            raise RuntimeError("boom")

    # Target file untouched.
    assert target.read_text() == before
    # Staging dir cleaned up despite the exception.
    assert list((vault / ".litman-staging").iterdir()) == []


def test_staged_write_rollback_does_not_create_new_targets(vault: Path) -> None:
    new_target = vault / "papers/never_created/metadata.yaml"
    assert not new_target.exists()

    with pytest.raises(RuntimeError):
        with staged_write(vault) as stage:
            stage.write_text("papers/never_created/metadata.yaml", "x")
            raise RuntimeError("boom")

    assert not new_target.exists()
    assert not new_target.parent.exists()


# ---------------------------------------------------------------------------
# Path-safety checks
# ---------------------------------------------------------------------------


def test_staged_write_rejects_absolute_path(vault: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        with staged_write(vault) as stage:
            stage.write_text("/etc/passwd", "evil")


def test_staged_write_rejects_parent_traversal(vault: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        with staged_write(vault) as stage:
            stage.write_text("../escape.txt", "evil")


def test_staged_write_rejects_traversal_in_middle(vault: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        with staged_write(vault) as stage:
            stage.write_text("papers/../../escape.txt", "evil")


# ---------------------------------------------------------------------------
# op_id behavior
# ---------------------------------------------------------------------------


def test_op_id_default_is_unique(vault: Path) -> None:
    s1 = StagedWrite(vault)
    s2 = StagedWrite(vault)
    assert s1.op_id != s2.op_id


def test_op_id_custom_used_verbatim(vault: Path) -> None:
    with staged_write(vault, op_id="my-custom-op") as stage:
        assert stage.op_id == "my-custom-op"
        assert stage.staging_root.name == "my-custom-op"
        # Staging dir lives directly under .litman-staging/.
        assert stage.staging_root.parent.name == ".litman-staging"


def test_collision_on_duplicate_op_id_raises(vault: Path) -> None:
    """Two simultaneous ops with the same custom id surface as FileExistsError.

    Auto-generated ids never collide in practice; an explicit clash signals
    a caller bug.
    """
    with staged_write(vault, op_id="dup") as _outer:
        with pytest.raises(FileExistsError):
            with staged_write(vault, op_id="dup"):
                pass


# ---------------------------------------------------------------------------
# cleanup_stale_staging
# ---------------------------------------------------------------------------


def test_cleanup_stale_staging_removes_leftover_dirs(vault: Path) -> None:
    staging_root = vault / ".litman-staging"
    (staging_root / "stale-1").mkdir()
    (staging_root / "stale-1" / "f.txt").write_text("x")
    (staging_root / "stale-2").mkdir()
    (staging_root / "stray-file.txt").write_text("y")

    n = cleanup_stale_staging(vault)

    assert n == 3
    assert list(staging_root.iterdir()) == []


def test_cleanup_stale_staging_no_staging_dir(tmp_path: Path) -> None:
    # No vault structure at all → no-op.
    assert cleanup_stale_staging(tmp_path) == 0


def test_cleanup_stale_staging_empty_dir(vault: Path) -> None:
    # Fresh vault has the staging dir but nothing inside.
    assert cleanup_stale_staging(vault) == 0
