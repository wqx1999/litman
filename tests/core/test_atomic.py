"""Tests for `litman.core.atomic.staged_write`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from litman.core.atomic import (
    DirMoveError,
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
        stage.write_text("subdir/foo.md", "# foo\n")
        stage.write_text("papers/2024_X_y/metadata.yaml", "id: 2024_X_y\n")

    assert (vault / "INDEX.json").read_text() == '{"a": 1}'
    assert (vault / "subdir/foo.md").read_text() == "# foo\n"
    assert (vault / "papers/2024_X_y/metadata.yaml").read_text() == "id: 2024_X_y\n"


def test_staged_write_creates_target_parent_dirs(vault: Path) -> None:
    # Deep new path the vault's seed doesn't include.
    with staged_write(vault) as stage:
        stage.write_text("papers/new_paper/sub/notes.md", "hi")

    assert (vault / "papers/new_paper/sub/notes.md").read_text() == "hi"


def test_staged_write_write_bytes(vault: Path) -> None:
    payload = b"\x89PNG\r\n\x1a\n"
    with staged_write(vault) as stage:
        stage.write_bytes("attachments/blob.bin", payload)

    assert (vault / "attachments/blob.bin").read_bytes() == payload


def test_staged_write_copy_file_copies_a_file_in(
    vault: Path, tmp_path: Path
) -> None:
    """copy_file stages a copy of a file on disk (SI can be gigabytes, so it
    is streamed rather than handed over as bytes); the source stays put."""
    source = tmp_path / "blob.bin"
    source.write_bytes(bytes(range(256)) * 4096)

    with staged_write(vault) as stage:
        staged = stage.copy_file("attachments/blob.bin", source)
        # In place before the commit, so a caller can check it.
        assert staged.read_bytes() == source.read_bytes()

    assert (vault / "attachments/blob.bin").read_bytes() == source.read_bytes()
    assert source.is_file()
    assert list((vault / ".litman-staging").iterdir()) == []


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_staged_write_copy_file_does_not_carry_a_read_only_bit(
    vault: Path, tmp_path: Path
) -> None:
    """A locked source (an SI file re-added from inside the vault) must not
    lock the staged copy: _fsync_file opens it O_WRONLY. The promoted target
    is a lockable SI file, so it ends up locked all the same."""
    from litman.core.locking import lock_truth_file

    source = tmp_path / "locked.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF\n")
    lock_truth_file(source)

    with staged_write(vault) as stage:
        staged = stage.copy_file(
            "papers/2024_A_b/supplementary/si-01.pdf", source
        )
        assert os.access(staged, os.W_OK)

    target = vault / "papers" / "2024_A_b" / "supplementary" / "si-01.pdf"
    assert target.read_bytes() == b"%PDF-1.4\n%%EOF\n"
    assert not os.access(target, os.W_OK)


def test_staged_write_copy_file_rolls_back_on_exception(
    vault: Path, tmp_path: Path
) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")

    with pytest.raises(RuntimeError, match="boom"):
        with staged_write(vault) as stage:
            stage.copy_file("papers/2024_A_b/supplementary/si-01.bin", source)
            raise RuntimeError("boom")

    assert not (vault / "papers" / "2024_A_b").exists()
    assert source.read_bytes() == b"x"


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


@pytest.mark.parametrize(
    "escape",
    [
        "/etc/passwd",          # rooted, no drive: Windows calls this RELATIVE
        "C:/evil.txt",          # drive + root
        "C:evil.txt",           # bare drive, no root
        "//server/share/evil",  # UNC
        "..",
        "../escape.txt",
        "papers/../../escape.txt",
        "papers\\..\\..\\escape.txt",  # backslash traversal
    ],
)
def test_staged_write_rejects_anything_that_is_not_plainly_relative(
    vault: Path, escape: str
) -> None:
    """Every shape, on every platform — the guard reads both path flavours.

    Split by host it would prove almost nothing: "/etc/passwd" is absolute to
    POSIX but relative to Windows (no drive letter), which is exactly how it
    used to reach C:\\etc\\passwd there while this suite stayed green on Linux.
    """
    with pytest.raises(ValueError, match="relative"):
        with staged_write(vault) as stage:
            stage.write_text(escape, "evil")


def test_staged_write_accepts_the_shapes_litman_actually_writes(
    vault: Path,
) -> None:
    """Reverse of the above: the guard must not have become a wall.

    Without this, tightening the check to reject everything would pass the
    rejection test set and break every write command.
    """
    with staged_write(vault) as stage:
        stage.write_text("INDEX.json", "{}")
        stage.write_text("papers/2024_Foo_Bar/metadata.yaml", "id: x")
    assert (vault / "INDEX.json").read_text(encoding="utf-8") == "{}"
    assert (
        vault / "papers" / "2024_Foo_Bar" / "metadata.yaml"
    ).read_text(encoding="utf-8") == "id: x"


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


# ===========================================================================
# M17 — crash-injection matrix T1–T12
#
# Strategy: each test stages the same payload, injects a crash at a precise
# point (monkeypatching one of the four _commit substeps or os.replace to
# raise on the Nth call), then independently calls recover_staging and
# asserts the vault is byte-identical to either the fully-committed state
# or the pre-commit state, per the spec table.
# ===========================================================================

import json as _json
import os as _os
import sys as _sys

from litman.core import atomic as _atomic
from litman.core.atomic import RecoveryResult, recover_staging

_PAYLOAD: dict[str, str] = {
    "INDEX.json": '{"committed": true}\n',
    "papers/2024_A_b/metadata.yaml": "id: 2024_A_b\n",
    "subdir/note.md": "# note\n",
}


def _snapshot(vault: Path) -> dict[str, bytes]:
    """Byte-exact map of every vault file, skipping the staging scratch."""
    snap: dict[str, bytes] = {}
    for path in sorted(vault.rglob("*")):
        if path.is_dir() or ".litman-staging" in path.parts:
            continue
        snap[str(path.relative_to(vault))] = path.read_bytes()
    return snap


def _committed_reference(tmp_path: Path) -> dict[str, bytes]:
    """Snapshot of a vault where _PAYLOAD was cleanly committed."""
    ref_parent = tmp_path / "ref_parent"
    ref_parent.mkdir()
    ref_vault = create_vault(ref_parent)
    with staged_write(ref_vault) as stage:
        for rel, content in _PAYLOAD.items():
            stage.write_text(rel, content)
    return _snapshot(ref_vault)


def _stage_payload(vault: Path, op_id: str = "op-crash") -> StagedWrite:
    """Open a StagedWrite and stage _PAYLOAD without committing yet."""
    sw = StagedWrite(vault, op_id=op_id)
    sw.__enter__()
    for rel, content in _PAYLOAD.items():
        sw.write_text(rel, content)
    return sw


# --- T1: exception in `with` body, before _commit -------------------------


def test_T1_crash_in_body_before_commit(vault: Path, tmp_path: Path) -> None:
    before = _snapshot(vault)
    with pytest.raises(RuntimeError, match="boom"):
        with staged_write(vault, op_id="op-T1") as stage:
            for rel, content in _PAYLOAD.items():
                stage.write_text(rel, content)
            raise RuntimeError("boom")
    # Staging removed by _cleanup, targets untouched.
    assert list((vault / ".litman-staging").iterdir()) == []
    assert _snapshot(vault) == before
    # Idempotent recovery is a no-op.
    assert recover_staging(vault) == []
    assert _snapshot(vault) == before


# --- T2: staged files written, crash before MANIFEST ----------------------


def test_T2_crash_before_manifest(vault: Path) -> None:
    before = _snapshot(vault)
    sw = _stage_payload(vault, "op-T2")
    # Crash inside _write_manifest before it writes anything we keep:
    # easiest is to fail _write_manifest entirely.
    try:
        sw._fsync_staged_files()
        raise OSError("crash before manifest")
    except OSError:
        pass
    op_dir = vault / ".litman-staging" / "op-T2"
    assert not (op_dir / "COMMITTED").exists()

    results = recover_staging(vault)
    # No COMMITTED → roll-back, op dir gone, not reported as anomaly.
    assert results == []
    assert not op_dir.exists()
    assert _snapshot(vault) == before


# --- T3: MANIFEST written, crash before COMMITTED -------------------------


def test_T3_crash_after_manifest_before_sentinel(vault: Path) -> None:
    before = _snapshot(vault)
    sw = _stage_payload(vault, "op-T3")
    sw._fsync_staged_files()
    sw._write_manifest()
    op_dir = vault / ".litman-staging" / "op-T3"
    assert (op_dir / "MANIFEST.json").exists()
    assert not (op_dir / "COMMITTED").exists()
    # crash here (before _write_sentinel)

    results = recover_staging(vault)
    # MANIFEST without sentinel ≠ decided → roll-back.
    assert results == []
    assert not op_dir.exists()
    assert _snapshot(vault) == before


# --- T4: COMMITTED durable, crash before first os.replace -----------------


def test_T4_crash_after_sentinel_before_promote(
    vault: Path, tmp_path: Path
) -> None:
    ref = _committed_reference(tmp_path)
    sw = _stage_payload(vault, "op-T4")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    op_dir = vault / ".litman-staging" / "op-T4"
    assert (op_dir / "COMMITTED").exists()
    # crash here (before _promote)

    results = recover_staging(vault)
    assert len(results) == 1
    r = results[0]
    assert r.kind == "rolled_forward"
    assert r.n_files == len(_PAYLOAD)
    assert "op-T4" in (r.message or "")
    assert not op_dir.exists()
    assert _snapshot(vault) == ref


# --- T5: crash after promoting file 1 of N -------------------------------


def test_T5_crash_mid_promote(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _committed_reference(tmp_path)
    # Simulate a hard crash mid-promote (process killed → no __exit__,
    # no _cleanup): drive the commit steps manually and let os.replace
    # raise on its 2nd call. File 1 is promoted; 2..N stay staged with
    # COMMITTED durable on disk.
    sw = _stage_payload(vault, "op-T5")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()

    real_replace = _atomic.os.replace
    state = {"n": 0}

    def flaky_replace(src, dst):
        state["n"] += 1
        if state["n"] == 2:
            raise OSError("injected crash on 2nd os.replace")
        return real_replace(src, dst)

    monkeypatch.setattr(_atomic.os, "replace", flaky_replace)
    with pytest.raises(OSError, match="2nd os.replace"):
        sw._promote()
    monkeypatch.undo()

    op_dir = vault / ".litman-staging" / "op-T5"
    assert (op_dir / "COMMITTED").exists()  # decided, must roll forward

    results = recover_staging(vault)
    assert len(results) == 1
    assert results[0].kind == "rolled_forward"
    assert not op_dir.exists()
    assert _snapshot(vault) == ref


# --- T5b (F3): torn promote through the REAL context manager ------------
# T5 above drives the commit steps by hand to mimic a *hard kill* (process
# killed → no __exit__, no _cleanup), so the staging dir survives naturally.
# But every real caller uses `with staged_write(...)`, so __exit__ → _commit
# → _promote always runs, and a mid-promote OSError propagates through
# __exit__'s `finally: self._cleanup()`. The F3 bug: that _cleanup
# unconditionally rmtree'd the staging dir, destroying the COMMITTED sentinel
# + MANIFEST + still-staged files, so a later recover_staging saw an empty
# dir → the tear became permanent and invisible. Once the sentinel is durable
# but promotion is unfinished, _cleanup must PRESERVE the evidence.


def test_T5b_torn_promote_via_context_manager_preserves_evidence(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _committed_reference(tmp_path)

    real_replace = _atomic.os.replace
    state = {"n": 0}

    def flaky_replace(src, dst):
        state["n"] += 1
        if state["n"] == 2:
            # e.g. transient EIO / ESTALE on a network mount mid-promote.
            raise OSError("injected crash on 2nd os.replace")
        return real_replace(src, dst)

    monkeypatch.setattr(_atomic.os, "replace", flaky_replace)

    # Real caller shape: body completes cleanly; __exit__ runs _commit;
    # _promote raises on the 2nd file; the OSError escapes __exit__.
    with pytest.raises(OSError, match="2nd os.replace"):
        with staged_write(vault, op_id="op-T5b") as stage:
            for rel, content in _PAYLOAD.items():
                stage.write_text(rel, content)
    monkeypatch.undo()

    op_dir = vault / ".litman-staging" / "op-T5b"
    # Decision point passed (sentinel durable) but promotion is torn: the
    # staging dir is the only roll-forward evidence and must survive.
    assert op_dir.exists(), "F3: _cleanup destroyed the torn-op evidence"
    assert (op_dir / "COMMITTED").exists()

    # The existing, tested recovery path finishes the half-done promotion.
    results = recover_staging(vault)
    assert len(results) == 1
    assert results[0].kind == "rolled_forward"
    assert not op_dir.exists()
    assert _snapshot(vault) == ref


# --- T13: roll-forward re-locks a promoted TRUTH file (M32) ---------------
# _recover_one_op must call lock_truth_file on a metadata.yaml / TAXONOMY.md /
# paper.pdf promoted *during crash recovery*, mirroring _promote's re-lock, so
# a target promoted by the roll-forward path ends up read-only just as it would
# on a clean commit.


@pytest.mark.skipif(
    _sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_T13_recovery_relocks_truth_file(
    vault: Path, tmp_path: Path
) -> None:
    ref = _committed_reference(tmp_path)
    truth_target = vault / "papers" / "2024_A_b" / "metadata.yaml"

    sw = _stage_payload(vault, "op-T13")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    op_dir = vault / ".litman-staging" / "op-T13"
    assert (op_dir / "COMMITTED").exists()
    # crash here (before _promote) → recovery rolls the op forward

    results = recover_staging(vault)
    assert len(results) == 1
    assert results[0].kind == "rolled_forward"
    assert not op_dir.exists()
    assert _snapshot(vault) == ref

    # The TRUTH file promoted *during recovery* must be read-only.
    assert truth_target.exists()
    assert truth_target.read_text() == _PAYLOAD["papers/2024_A_b/metadata.yaml"]
    assert not _os.access(truth_target, _os.W_OK)


# --- T14/T15: overwrite a read-only TRUTH file under Windows os.replace ----
# On Windows os.replace (MoveFileEx) refuses to overwrite a read-only
# destination and raises PermissionError (WinError 5), unlike POSIX rename
# which ignores the target's mode. Every `lit` write funnels through
# staged_write and overwrites the read-only metadata.yaml / TAXONOMY.md /
# paper.pdf, so both the clean-commit (_promote) and roll-forward (recovery)
# paths must unlock the target before the replace. Simulate the win32 refusal
# on POSIX CI by wrapping os.replace.


def _windows_like_replace(real_replace):
    """Wrap os.replace to mimic win32 MoveFileEx: refuse a read-only dst."""

    def _replace(src, dst):
        if _os.path.exists(dst) and not _os.access(dst, _os.W_OK):
            raise PermissionError(5, "Access is denied (simulated win32)")
        return real_replace(src, dst)

    return _replace


@pytest.mark.skipif(
    _sys.platform == "win32", reason="simulates win32 os.replace on POSIX CI"
)
def test_T14_promote_over_readonly_truth_windows_semantics(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean-commit path: _promote overwrites a locked read-only metadata.yaml
    even when os.replace refuses a read-only dst (unlock-before-replace)."""
    from litman.core.locking import lock_truth_file

    target = vault / "papers" / "2024_A_b" / "metadata.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("id: 2024_A_b\nrev: 1\n", encoding="utf-8")
    lock_truth_file(target)
    assert not _os.access(target, _os.W_OK)  # locked read-only

    monkeypatch.setattr(
        _atomic.os, "replace", _windows_like_replace(_os.replace)
    )

    with staged_write(vault) as stage:
        stage.write_text(
            "papers/2024_A_b/metadata.yaml", "id: 2024_A_b\nrev: 2\n"
        )

    assert target.read_text() == "id: 2024_A_b\nrev: 2\n"
    assert not _os.access(target, _os.W_OK)  # re-locked after promotion
    assert list((vault / ".litman-staging").iterdir()) == []


@pytest.mark.skipif(
    _sys.platform == "win32", reason="simulates win32 os.replace on POSIX CI"
)
def test_T15_recovery_over_readonly_truth_windows_semantics(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roll-forward path: recovery overwrites a locked read-only metadata.yaml
    even when os.replace refuses a read-only dst (unlock-before-replace)."""
    from litman.core.locking import lock_truth_file

    target = vault / "papers" / "2024_A_b" / "metadata.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("id: 2024_A_b\nrev: 1\n", encoding="utf-8")
    lock_truth_file(target)

    # Stage + write the COMMITTED sentinel, then crash before _promote (as T13).
    sw = StagedWrite(vault, op_id="op-T15")
    sw.__enter__()
    sw.write_text("papers/2024_A_b/metadata.yaml", "id: 2024_A_b\nrev: 2\n")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    op_dir = vault / ".litman-staging" / "op-T15"
    assert (op_dir / "COMMITTED").exists()

    monkeypatch.setattr(
        _atomic.os, "replace", _windows_like_replace(_os.replace)
    )

    results = recover_staging(vault)
    assert len(results) == 1
    assert results[0].kind == "rolled_forward"
    assert not op_dir.exists()
    assert target.read_text() == "id: 2024_A_b\nrev: 2\n"
    assert not _os.access(target, _os.W_OK)  # re-locked during recovery


# --- T6: all promoted, crash before op-dir rmtree ------------------------


def test_T6_crash_before_cleanup(vault: Path, tmp_path: Path) -> None:
    ref = _committed_reference(tmp_path)
    sw = _stage_payload(vault, "op-T6")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    sw._promote()
    # crash here, before _cleanup() rmtree
    op_dir = vault / ".litman-staging" / "op-T6"
    assert op_dir.exists()

    results = recover_staging(vault)
    # All staging files gone, targets in place → idempotent: still
    # reported as rolled_forward (0 promoted this pass) and op removed.
    assert len(results) == 1
    assert results[0].kind == "rolled_forward"
    assert results[0].n_files == 0
    assert not op_dir.exists()
    assert _snapshot(vault) == ref


# --- T7: re-run recovery over a T5 terminal state (idempotent) -----------


def test_T7_recovery_is_idempotent(vault: Path, tmp_path: Path) -> None:
    ref = _committed_reference(tmp_path)
    sw = _stage_payload(vault, "op-T7")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    # First pass rolls forward.
    first = recover_staging(vault)
    assert len(first) == 1 and first[0].kind == "rolled_forward"
    assert _snapshot(vault) == ref
    # Second pass: zero changes, zero errors.
    second = recover_staging(vault)
    assert second == []
    assert _snapshot(vault) == ref


# --- T8: COMMITTED + a relpath missing from both sides -------------------


def test_T8_unrecoverable_tear_preserves_evidence(
    vault: Path, tmp_path: Path
) -> None:
    sw = _stage_payload(vault, "op-T8")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    op_dir = vault / ".litman-staging" / "op-T8"
    # Simulate: one staged file vanished and was never promoted.
    lost_rel = "subdir/note.md"
    (op_dir / lost_rel).unlink()
    assert not (vault / lost_rel).exists()

    results = recover_staging(vault)
    assert len(results) == 1
    r = results[0]
    assert r.kind == "unrecoverable"
    assert lost_rel in (r.message or "")
    # Mutating recoverer DID roll the siblings forward → completed
    # past-tense voice with the real promoted count (2), not pending.
    assert "rolled the other 2 file(s) in the same op forward" in (
        r.message or ""
    )
    assert "can be rolled forward" not in (r.message or "")
    # Evidence preserved — op dir NOT deleted.
    assert op_dir.exists()
    # Sibling recoverable files still rolled forward.
    assert (vault / "INDEX.json").read_text() == _PAYLOAD["INDEX.json"]
    assert (
        vault / "papers/2024_A_b/metadata.yaml"
    ).read_text() == _PAYLOAD["papers/2024_A_b/metadata.yaml"]
    assert r.n_files == 2


# --- T9: recovery through the health-check path --------------------------


def test_T9_recover_via_cleanup_stale_staging(
    vault: Path, tmp_path: Path
) -> None:
    ref = _committed_reference(tmp_path)
    sw = _stage_payload(vault, "op-T9")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    op_dir = vault / ".litman-staging" / "op-T9"

    # health-check fixer delegates here; must roll forward, not blind-rm.
    n = cleanup_stale_staging(vault)
    assert n == 1
    assert not op_dir.exists()
    assert _snapshot(vault) == ref

    # And the read-only check classifies a fresh torn op as info/error
    # correctly (re-create one to verify the check output).
    from litman.core.checks import check_stale_staging

    sw2 = _stage_payload(vault, "op-T9b")
    sw2._fsync_staged_files()
    sw2._write_manifest()
    sw2._write_sentinel()
    issues = check_stale_staging(vault, [])
    assert len(issues) == 1
    assert issues[0].severity == "info"
    assert "roll-forward" in (issues[0].message or "").lower()


# --- T10: recovery through the vault-open hook (lit list) ---------------


def test_T10_recover_via_vault_open_hook(
    vault: Path, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from litman.cli import cli

    ref = _committed_reference(tmp_path)
    sw = _stage_payload(vault, "op-T10")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    op_dir = vault / ".litman-staging" / "op-T10"
    assert op_dir.exists()

    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    # Vault self-healed before the command logic ran.
    assert not op_dir.exists()
    assert _snapshot(vault) == ref
    # Recovery line on stderr, not stdout (pipe-safe).
    assert "op-T10" in result.stderr
    assert "op-T10" not in result.stdout


# --- AC4: vault-open recovery fires exactly once per command -----------


def test_vault_open_recovery_fires_exactly_once_per_command(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: one `lit list` triggers recover_staging exactly once.

    Spies on ``ensure_vault_recovered`` (the chokepoint hook in
    library.py resolves the vault then calls it) via a counting wrapper.
    A T5-shaped torn op is injected so the run does real recovery work,
    not just the fast path. Asserts a single invocation across the
    command — the orchestrator can verify "exactly once" by test, not
    only by end state.
    """
    from click.testing import CliRunner

    from litman.cli import cli
    from litman.core import atomic as atomic_mod

    ref = _committed_reference(tmp_path)
    sw = _stage_payload(vault, "op-AC4")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    op_dir = vault / ".litman-staging" / "op-AC4"
    assert op_dir.exists()

    calls: list[Path] = []
    real_hook = atomic_mod.ensure_vault_recovered

    def counting_hook(vault_arg: Path):
        calls.append(vault_arg)
        return real_hook(vault_arg)

    # Patch on the atomic module; library.py imports it lazily inside
    # find_vault, so the lookup resolves to this wrapper.
    monkeypatch.setattr(
        atomic_mod, "ensure_vault_recovered", counting_hook
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--library", str(vault)])
    assert result.exit_code == 0, result.output

    assert len(calls) == 1, f"recovery fired {len(calls)}× (want 1)"
    # And it actually healed the vault before command logic ran.
    assert not op_dir.exists()
    assert _snapshot(vault) == ref
    assert "op-AC4" in result.stderr


# --- T11: promotion follows manifest order, not dict/glob order ---------


def test_T11_roll_forward_follows_manifest_order(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stage in a deliberate order: a, b, INDEX.json (INDEX last).
    ordered = [
        ("papers/a/metadata.yaml", "id: a\n"),
        ("papers/b/metadata.yaml", "id: b\n"),
        ("INDEX.json", '{"last": true}\n'),
    ]
    sw = StagedWrite(vault, op_id="op-T11")
    sw.__enter__()
    for rel, content in ordered:
        sw.write_text(rel, content)
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    # Promote the first file, then crash.
    first_staging = sw.staging_root / "papers/a/metadata.yaml"
    (vault / "papers/a").mkdir(parents=True, exist_ok=True)
    import os as _os

    _os.replace(first_staging, vault / "papers/a/metadata.yaml")

    op_dir = vault / ".litman-staging" / "op-T11"
    manifest = _json.loads(
        (op_dir / "MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["files"] == [r for r, _ in ordered]

    replaced_order: list[str] = []
    real_replace = _atomic.os.replace

    def tracking_replace(src, dst):
        replaced_order.append(Path(dst).name)
        return real_replace(src, dst)

    monkeypatch.setattr(_atomic.os, "replace", tracking_replace)

    results = recover_staging(vault)
    assert len(results) == 1 and results[0].kind == "rolled_forward"
    # Remaining files promoted in manifest order: b then INDEX.json.
    assert replaced_order == ["metadata.yaml", "INDEX.json"]
    assert (vault / "papers/b/metadata.yaml").read_text() == "id: b\n"
    assert (vault / "INDEX.json").read_text() == '{"last": true}\n'


# --- T12: two leftover ops, one clean-abort, one torn -------------------


def test_T12_mixed_leftover_ops(vault: Path, tmp_path: Path) -> None:
    # Op A: no COMMITTED → roll-back, silent.
    a = StagedWrite(vault, op_id="op-T12a")
    a.__enter__()
    a.write_text("INDEX.json", '{"never": true}\n')
    a._fsync_staged_files()
    # (no manifest, no sentinel — clean abort)

    # Op B: COMMITTED → roll-forward.
    b = StagedWrite(vault, op_id="op-T12b")
    b.__enter__()
    b.write_text("papers/2024_B_x/metadata.yaml", "id: 2024_B_x\n")
    b._fsync_staged_files()
    b._write_manifest()
    b._write_sentinel()

    results = recover_staging(vault)
    # Only the torn op is reported; the clean abort is silent.
    assert len(results) == 1
    assert results[0].op_id == "op-T12b"
    assert results[0].kind == "rolled_forward"
    # Both op dirs cleaned, B's file promoted, A's never written.
    assert not (vault / ".litman-staging" / "op-T12a").exists()
    assert not (vault / ".litman-staging" / "op-T12b").exists()
    assert (
        vault / "papers/2024_B_x/metadata.yaml"
    ).read_text() == "id: 2024_B_x\n"
    # A's payload to INDEX.json was rolled back (seed INDEX preserved).
    assert '"never"' not in (vault / "INDEX.json").read_text()


# --- T13/T14 (F4): recovery's own os.replace failure must not escape -----
# recover_staging runs at the vault-open chokepoint (library.find_vault), so
# an exception escaping it crashes EVERY lit command — `lit health-check`
# included, since it resolves the vault the same way. A torn op whose promote
# keeps failing (storage still flapping) must degrade to "preserved, retry
# next time", never raise. Mirrors the try/except already guarding the drift
# hook in cli.py.


def test_T13_recovery_replace_failure_is_contained(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sw = _stage_payload(vault, "op-T13")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()
    op_dir = vault / ".litman-staging" / "op-T13"

    def always_fail_replace(src, dst):
        raise OSError("storage still down")

    monkeypatch.setattr(_atomic.os, "replace", always_fail_replace)
    # Must NOT raise — before the F4 fix this OSError escaped recover_staging.
    results = recover_staging(vault)
    monkeypatch.undo()

    assert len(results) == 1
    assert results[0].kind == "unrecoverable"
    assert results[0].n_files == 0
    # Evidence preserved for the next vault-open retry; nothing half-promoted.
    assert op_dir.exists()
    assert (op_dir / "COMMITTED").exists()
    assert not (vault / "papers/2024_A_b/metadata.yaml").exists()


def test_T14_recovery_failure_does_not_dos_other_commands(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from litman.cli import cli

    sw = _stage_payload(vault, "op-T14")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()

    real_replace = _atomic.os.replace

    def fail_staging_replace(src, dst):
        # Only the recovery promotion (src under .litman-staging) fails;
        # leave any unrelated replace alone.
        if ".litman-staging" in str(src):
            raise OSError("storage still down")
        return real_replace(src, dst)

    monkeypatch.setattr(_atomic.os, "replace", fail_staging_replace)
    runner = CliRunner()
    # Before the F4 fix: recovery escapes find_vault → command crashes (exit≠0).
    result = runner.invoke(cli, ["list", "--library", str(vault)])
    monkeypatch.undo()

    assert result.exit_code == 0, result.output


# --- Fast-path: empty .litman-staging never iterdirs --------------------


def test_recover_staging_empty_does_one_isdir_no_iterdir(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: empty-but-PRESENT staging → no full iterdir/sort walk.

    ``library.py`` creates ``.litman-staging/`` at vault init, so the
    real clean-vault state is empty-but-present, NOT absent. Keep the
    empty dir in place (do not rmtree it) and assert the full
    ``sorted(iterdir())`` walk is never invoked on it. The Critical-1
    fix probes emptiness with a single ``os.scandir``; that one cheap
    probe is allowed, but ``Path.iterdir`` (the slow path's call) must
    not fire — so a regression to unconditional iterdir fails this test.
    """
    staging = vault / ".litman-staging"
    assert staging.is_dir() and next(staging.iterdir(), None) is None

    def boom_iterdir(self):
        raise AssertionError(
            "Path.iterdir must not be called on the empty-but-present "
            "fast path (regressed to unconditional sorted(iterdir()))"
        )

    monkeypatch.setattr(Path, "iterdir", boom_iterdir)
    assert recover_staging(vault) == []


def test_recovery_result_is_frozen() -> None:
    r = RecoveryResult(op_id="x", kind="rolled_forward", n_files=1)
    with pytest.raises(Exception):
        r.op_id = "y"  # type: ignore[misc]


def test_recover_staging_swallows_stray_file_unlink_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray file we cannot delete must not crash recover_staging.

    recover_staging runs at the vault-open chokepoint (every lit command),
    so an OSError escaping the stray-file ``unlink`` would turn one locked
    / read-only-mount file into a whole-vault DoS. Simulate the failure and
    assert the call returns normally instead of propagating.
    """
    staging = vault / ".litman-staging"
    (staging / "stray.lock").write_text("x")

    def boom_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("locked by antivirus / read-only mount")

    monkeypatch.setattr(Path, "unlink", boom_unlink)
    assert recover_staging(vault) == []


# --- Nested staged dirs get their directory entries fsync'd (durability) --


def test_nested_staged_dirs_are_dir_fsynced(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested staged relpath dir-fsyncs its intermediate staging dirs.

    On POSIX a newly-created file ``d/f`` is only crash-durable once ``d`` is
    fsync'd too. Real callers stage ``papers/<id>/metadata.yaml``, so the
    intermediate ``staging_root/papers`` and ``staging_root/papers/<id>`` dirs
    must be dir-fsync'd before the COMMITTED sentinel — otherwise a power loss
    could drop the nested dirent and strand the staged copy. Record every
    ``_fsync_dir`` target and assert those two dirs are among them.
    """
    synced: list[Path] = []
    monkeypatch.setattr(
        "litman.core.atomic._fsync_dir", lambda p: synced.append(Path(p))
    )

    with staged_write(vault, op_id="op-nested-fsync") as stage:
        stage.write_text("papers/2024_Foo/metadata.yaml", "id: 2024_Foo\n")
        stage.write_text("INDEX.json", "{}\n")
        root = stage.staging_root

    assert root / "papers" / "2024_Foo" in synced
    assert root / "papers" in synced


def test_dir_fsync_on_windows_is_a_silent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing O_DIRECTORY is a constant of the platform — true on every
    Windows box on every write — so the degraded guarantee must NOT be
    re-announced per command (it nagged users and polluted every agent
    run's stderr). ADR-005 + the docs carry the guarantee level; the
    staged-write recovery path still reports actual half-finished commits."""
    from litman.core import atomic

    warn_calls: list[object] = []
    monkeypatch.setattr(
        atomic, "_warn_dir_fsync_unsupported", lambda *a: warn_calls.append(a)
    )
    monkeypatch.setattr(atomic.sys, "platform", "win32")

    atomic._fsync_dir(tmp_path)

    assert warn_calls == []


# ===========================================================================
# Folder moves inside a staged write (move_dir)
#
# `lit rename` moves papers/<old>/ to papers/<new>/ between the manifest and
# the COMMITTED sentinel. Same crash-matrix shape as T1–T15: drive the commit
# steps by hand to stop at a precise point (a hard kill runs no _cleanup),
# then recover and compare the vault byte for byte with the state before the
# op or the state after a clean one. A failing move is simulated by patching
# the module's own _rename_dir, never os.
# ===========================================================================

# What a command prints while an op's folders stay unsettled; health-check
# names the folders.
_UNSETTLED = (
    "An unfinished command left folders it cannot settle. "
    "Run `lit health-check` to see which."
)
_MOVE_SRC = "papers/2024_A_b"
_MOVE_DST = "papers/2024_A_c"
_MOVE_PAYLOAD: dict[str, str] = {
    "papers/2024_A_c/metadata.yaml": "id: 2024_A_c\n",
    "INDEX.json": '{"moved": true}\n',
}


def _seed_folder(vault: Path) -> None:
    folder = vault / _MOVE_SRC
    folder.mkdir(parents=True)
    (folder / "metadata.yaml").write_text("id: 2024_A_b\n", encoding="utf-8")
    (folder / "paper.pdf").write_bytes(b"%PDF-1.4 stub\n")


def _stage_move(vault: Path, op_id: str) -> StagedWrite:
    """Open a StagedWrite that moves the seeded folder, without committing."""
    sw = StagedWrite(vault, op_id=op_id)
    sw.__enter__()
    sw.move_dir(_MOVE_SRC, _MOVE_DST)
    for rel, content in _MOVE_PAYLOAD.items():
        sw.write_text(rel, content)
    return sw


def _moved_reference(tmp_path: Path) -> dict[str, bytes]:
    """Snapshot of a vault where the move + payload committed cleanly."""
    parent = tmp_path / "moved_ref"
    parent.mkdir()
    ref = create_vault(parent)
    _seed_folder(ref)
    with staged_write(ref) as stage:
        stage.move_dir(_MOVE_SRC, _MOVE_DST)
        for rel, content in _MOVE_PAYLOAD.items():
            stage.write_text(rel, content)
    return _snapshot(ref)


def _refuse(*_args: object) -> None:
    raise PermissionError(13, "Access is denied")


@pytest.fixture
def seeded(vault: Path) -> Path:
    _seed_folder(vault)
    return vault


def test_move_dir_happy_path_moves_then_promotes_into_the_new_folder(
    seeded: Path, tmp_path: Path
) -> None:
    with staged_write(seeded) as stage:
        stage.move_dir(_MOVE_SRC, _MOVE_DST)
        for rel, content in _MOVE_PAYLOAD.items():
            stage.write_text(rel, content)

    assert not (seeded / _MOVE_SRC).exists()
    assert (seeded / _MOVE_DST / "paper.pdf").read_bytes() == b"%PDF-1.4 stub\n"
    assert (seeded / _MOVE_DST / "metadata.yaml").read_text(
        encoding="utf-8"
    ) == "id: 2024_A_c\n"
    assert list((seeded / ".litman-staging").iterdir()) == []
    assert _snapshot(seeded) == _moved_reference(tmp_path)


def test_a_file_staged_inside_the_folder_being_moved_is_refused(
    seeded: Path,
) -> None:
    """It would be promoted into a folder that no longer exists by that name;
    the caller must stage it at the new path. Refused before anything moves."""
    before = _snapshot(seeded)
    with pytest.raises(ValueError, match="stage it at its new path"):
        with staged_write(seeded) as stage:
            stage.move_dir(_MOVE_SRC, _MOVE_DST)
            stage.write_text(f"{_MOVE_SRC}/notes.md", "x")
    assert _snapshot(seeded) == before
    assert list((seeded / ".litman-staging").iterdir()) == []


@pytest.mark.parametrize(
    ("src", "dst"),
    [("papers/A", "papers/A"), ("../out", "papers/A"), ("papers/A", "/etc/x")],
)
def test_move_dir_refuses_a_move_onto_itself_or_out_of_the_vault(
    vault: Path, src: str, dst: str
) -> None:
    with pytest.raises(ValueError):
        with staged_write(vault) as stage:
            stage.move_dir(src, dst)


# --- 1: a write that moves nothing keeps the old manifest ------------------


def test_manifest_without_moves_is_exactly_op_id_and_files(vault: Path) -> None:
    sw = _stage_payload(vault, "op-M1")
    sw._fsync_staged_files()
    sw._write_manifest()
    manifest = _json.loads(
        (sw.staging_root / "MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest == {"op_id": "op-M1", "files": list(_PAYLOAD)}


def test_manifest_lists_the_moves_when_there_are_some(seeded: Path) -> None:
    sw = _stage_move(seeded, "op-M1b")
    sw._fsync_staged_files()
    sw._write_manifest()
    manifest = _json.loads(
        (sw.staging_root / "MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["dirs"] == [[_MOVE_SRC, _MOVE_DST]]
    assert manifest["files"] == list(_MOVE_PAYLOAD)


# --- 2: an op left by an older litman (no "dirs") recovers as before -------


@pytest.mark.parametrize("committed", [True, False])
def test_old_format_manifest_recovers_as_before(
    vault: Path, committed: bool
) -> None:
    before = (vault / "INDEX.json").read_bytes()
    op_dir = vault / ".litman-staging" / "op-old"
    op_dir.mkdir()
    (op_dir / "INDEX.json").write_text('{"old": true}\n', encoding="utf-8")
    (op_dir / "MANIFEST.json").write_text(
        '{"op_id": "op-old", "files": ["INDEX.json"]}', encoding="utf-8"
    )
    if committed:
        (op_dir / "COMMITTED").write_bytes(b"")

    results = recover_staging(vault)

    assert not op_dir.exists()
    if committed:
        assert [r.kind for r in results] == ["rolled_forward"]
        assert (vault / "INDEX.json").read_text(
            encoding="utf-8"
        ) == '{"old": true}\n'
    else:
        assert results == []
        assert (vault / "INDEX.json").read_bytes() == before


# --- 3: the move fails → nothing changed ------------------------------------


def test_a_failing_move_raises_dir_move_error_and_changes_nothing(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(seeded)
    monkeypatch.setattr(_atomic, "_rename_dir", _refuse)

    with pytest.raises(DirMoveError) as caught:
        with staged_write(seeded, op_id="op-M3") as stage:
            stage.move_dir(_MOVE_SRC, _MOVE_DST)
            for rel, content in _MOVE_PAYLOAD.items():
                stage.write_text(rel, content)

    assert isinstance(caught.value, OSError)
    assert (caught.value.src_rel, caught.value.reason) == (
        _MOVE_SRC,
        "Access is denied",
    )
    assert _snapshot(seeded) == before
    assert list((seeded / ".litman-staging").iterdir()) == []


def test_an_existing_destination_is_refused_not_replaced(seeded: Path) -> None:
    """POSIX rename would quietly replace an empty destination folder."""
    (seeded / _MOVE_DST).mkdir()
    before = _snapshot(seeded)
    with pytest.raises(DirMoveError, match="destination exists"):
        with staged_write(seeded) as stage:
            stage.move_dir(_MOVE_SRC, _MOVE_DST)
    assert _snapshot(seeded) == before
    assert (seeded / _MOVE_SRC / "paper.pdf").is_file()


# --- 4: the sentinel write fails after the move → moved back ----------------


def test_sentinel_failure_after_the_move_moves_the_folder_back(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(seeded)

    def no_sentinel(self: StagedWrite) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_atomic.StagedWrite, "_write_sentinel", no_sentinel)

    with pytest.raises(OSError, match="disk full"):
        with staged_write(seeded, op_id="op-M4") as stage:
            stage.move_dir(_MOVE_SRC, _MOVE_DST)
            for rel, content in _MOVE_PAYLOAD.items():
                stage.write_text(rel, content)

    assert _snapshot(seeded) == before
    assert (seeded / _MOVE_SRC).is_dir()
    assert not (seeded / _MOVE_DST).exists()
    assert list((seeded / ".litman-staging").iterdir()) == []


# --- 5: ... and moving back fails too → the next recovery does it ----------


def test_when_moving_back_fails_the_record_stays_for_recovery(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(seeded)
    real_rename_dir = _atomic._rename_dir
    calls = {"n": 0}

    def forward_only(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            _refuse()
        real_rename_dir(src, dst)

    def no_sentinel(self: StagedWrite) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_atomic, "_rename_dir", forward_only)
    monkeypatch.setattr(_atomic.StagedWrite, "_write_sentinel", no_sentinel)
    with pytest.raises(OSError, match="disk full"):
        with staged_write(seeded, op_id="op-M5") as stage:
            stage.move_dir(_MOVE_SRC, _MOVE_DST)
            for rel, content in _MOVE_PAYLOAD.items():
                stage.write_text(rel, content)
    monkeypatch.undo()

    op_dir = seeded / ".litman-staging" / "op-M5"
    assert op_dir.is_dir()
    assert not (op_dir / "COMMITTED").exists()
    assert (seeded / _MOVE_DST).is_dir()

    results = recover_staging(seeded)

    assert len(results) == 1
    assert results[0].kind == "rolled_back"
    assert results[0].message == (
        "Undid an unfinished command: papers/2024_A_b/ is back. Rerun it if still needed."
    )
    assert not op_dir.exists()
    assert _snapshot(seeded) == before


# --- 6: crash after the manifest, before the move → silent rollback -------


def test_crash_after_manifest_before_move_rolls_back_silently(
    seeded: Path,
) -> None:
    before = _snapshot(seeded)
    sw = _stage_move(seeded, "op-M6")
    sw._fsync_staged_files()
    sw._write_manifest()
    # killed here: the folder never moved

    assert recover_staging(seeded) == []
    assert not (seeded / ".litman-staging" / "op-M6").exists()
    assert _snapshot(seeded) == before


# --- 7: crash after the move, before COMMITTED → moved back, reported -----


def test_crash_after_move_before_sentinel_is_moved_back_with_one_line(
    seeded: Path,
) -> None:
    from click.testing import CliRunner

    from litman.cli import cli

    before = _snapshot(seeded)
    sw = _stage_move(seeded, "op-M7")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    # killed here: the folder moved, nothing decided
    assert (seeded / _MOVE_DST).is_dir()

    result = CliRunner().invoke(cli, ["list", "--library", str(seeded)])

    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.stderr.splitlines() if ln.startswith("recovered:")]
    assert len(lines) == 1
    assert lines[0].startswith(
        "recovered: Undid an unfinished command: papers/2024_A_b/ is back."
    )
    assert "Undid" not in result.stdout
    assert not (seeded / ".litman-staging" / "op-M7").exists()
    assert _snapshot(seeded) == before


# --- 8: crash after COMMITTED (before / mid promote) → rolled forward -----


@pytest.mark.parametrize("promoted_first", [0, 1])
def test_crash_after_sentinel_rolls_the_move_forward(
    seeded: Path, tmp_path: Path, promoted_first: int
) -> None:
    ref = _moved_reference(tmp_path)
    sw = _stage_move(seeded, "op-M8")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    sw._write_sentinel()
    if promoted_first:
        # Mid-promote: the first file made it, the kill came before the next.
        sw._staged = dict(list(sw._staged.items())[:promoted_first])
        sw._promote()

    results = recover_staging(seeded)

    assert [r.kind for r in results] == ["rolled_forward"]
    assert not (seeded / ".litman-staging" / "op-M8").exists()
    assert _snapshot(seeded) == ref


# --- 9: COMMITTED, but the folder was put back by hand → moved forward ----


def test_committed_op_whose_folder_is_back_is_moved_forward_first(
    seeded: Path, tmp_path: Path
) -> None:
    ref = _moved_reference(tmp_path)
    sw = _stage_move(seeded, "op-M9")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    sw._write_sentinel()
    _os.rename(seeded / _MOVE_DST, seeded / _MOVE_SRC)

    results = recover_staging(seeded)

    assert [r.kind for r in results] == ["rolled_forward"]
    assert _snapshot(seeded) == ref


# --- 10: a recovery move that fails is kept and retried, never raised -----


@pytest.mark.parametrize("committed", [False, True])
def test_a_recovery_move_that_fails_keeps_the_op_and_never_raises(
    seeded: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    from click.testing import CliRunner

    from litman.cli import cli

    sw = _stage_move(seeded, "op-M10")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    if committed:
        sw._write_sentinel()
        # Needs moving forward again before any file may be promoted.
        _os.rename(seeded / _MOVE_DST, seeded / _MOVE_SRC)
    folder_now = _MOVE_SRC if committed else _MOVE_DST
    before = _snapshot(seeded)
    op_dir = seeded / ".litman-staging" / "op-M10"
    monkeypatch.setattr(_atomic, "_rename_dir", _refuse)

    results = recover_staging(seeded)

    assert len(results) == 1
    assert results[0].kind == "unrecoverable"
    assert results[0].message == (
        f"Could not move {folder_now}/ (Access is denied). "
        "Close any file open from it, then run lit again."
    )
    assert op_dir.is_dir()
    assert (seeded / folder_now).is_dir()
    # Nothing promoted into a second folder beside the first.
    assert _snapshot(seeded) == before

    result = CliRunner().invoke(cli, ["list", "--library", str(seeded)])
    assert result.exit_code == 0, result.output
    assert f"error: Could not move {folder_now}/" in result.stderr
    assert op_dir.is_dir()

    monkeypatch.undo()
    recover_staging(seeded)
    assert not op_dir.exists()
    assert (seeded / (_MOVE_DST if committed else _MOVE_SRC)).is_dir()


# --- 11: both folders there, or neither → a person decides ----------------


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("state", ["both", "neither"])
def test_both_or_neither_folder_is_unrecoverable_and_keeps_evidence(
    seeded: Path, committed: bool, state: str
) -> None:
    from litman.core.checks import check_stale_staging

    sw = _stage_move(seeded, "op-M11")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    if committed:
        sw._write_sentinel()
    if state == "both":
        (seeded / _MOVE_SRC).mkdir()
    else:
        import shutil

        shutil.rmtree(seeded / _MOVE_DST)
    before = _snapshot(seeded)

    results = recover_staging(seeded)

    assert len(results) == 1
    assert results[0].kind == "unrecoverable"
    # The command says only that; health-check names the folders.
    assert results[0].message == _UNSETTLED
    (issue,) = check_stale_staging(seeded, [])
    found = (
        "papers/2024_A_b/ and papers/2024_A_c/ both exist"
        if state == "both"
        else "papers/2024_A_b/ and papers/2024_A_c/ are both missing"
    )
    assert issue.message == f"{found} after an unfinished command."
    assert issue.hint == (
        "settle the folders it names by hand, then remove "
        ".litman-staging/op-M11/ — NOT auto-fixed"
    )
    assert (seeded / ".litman-staging" / "op-M11").is_dir()
    assert _snapshot(seeded) == before

    # Someone then puts one folder right. Whatever the folders look like now,
    # the op that could not tell never moves one, nor promotes into one.
    if state == "both":
        (seeded / _MOVE_SRC).rmdir()
    else:
        (seeded / (_MOVE_SRC if committed else _MOVE_DST)).mkdir()
    before = _snapshot(seeded)
    folders = sorted(p.name for p in (seeded / "papers").iterdir())

    again = recover_staging(seeded)

    assert [(r.kind, r.message) for r in again] == [
        ("unrecoverable", results[0].message)
    ]
    # health-check still names what the folders showed when it was judged.
    assert [i.message for i in check_stale_staging(seeded, [])] == [issue.message]
    assert sorted(p.name for p in (seeded / "papers").iterdir()) == folders
    assert _snapshot(seeded) == before


# --- 12: health-check reads the op dir the same way -----------------------


def test_check_stale_staging_says_the_folder_will_be_moved_back(
    seeded: Path,
) -> None:
    from litman.core.checks import (
        AUTO_FIXABLE_CATEGORIES,
        apply_autofix,
        check_stale_staging,
    )

    before = _snapshot(seeded)
    sw = _stage_move(seeded, "op-M12")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()

    issues = check_stale_staging(seeded, [])

    assert len(issues) == 1
    assert issues[0].category == "stale_staging"
    assert issues[0].category in AUTO_FIXABLE_CATEGORIES
    assert issues[0].message == (
        "An unfinished command will be undone, moving its folder back to papers/2024_A_b/."
    )
    # The read-only probe moved nothing.
    assert (seeded / _MOVE_DST).is_dir()

    counts = apply_autofix(seeded, issues)

    assert counts["stale_staging"] == 1
    assert _snapshot(seeded) == before
    assert list((seeded / ".litman-staging").iterdir()) == []


@pytest.mark.parametrize("committed", [False, True])
def test_check_stale_staging_reports_both_folders_as_unrecoverable(
    seeded: Path, committed: bool
) -> None:
    from litman.core.checks import AUTO_FIXABLE_CATEGORIES, check_stale_staging

    sw = _stage_move(seeded, "op-M12b")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    if committed:
        sw._write_sentinel()
    (seeded / _MOVE_SRC).mkdir()

    issues = check_stale_staging(seeded, [])

    assert len(issues) == 1
    assert issues[0].category == "stale_staging_unrecoverable"
    assert issues[0].category not in AUTO_FIXABLE_CATEGORIES
    assert issues[0].severity == "error"
    assert "both exist" in issues[0].message
    assert issues[0].hint == (
        "settle the folders it names by hand, then remove "
        ".litman-staging/op-M12b/ — NOT auto-fixed"
    )


# --- a committed op's folders are moved once -------------------------------


def test_a_settled_commit_never_moves_its_folder_again(seeded: Path) -> None:
    """The op is kept as evidence (a file was lost), and someone then moves
    the folder back by hand: no later pass moves it forward again."""
    from litman.core.checks import check_stale_staging

    sw = _stage_move(seeded, "op-R3")
    sw.write_text("subdir/lost.md", "# lost\n")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    sw._write_sentinel()
    (sw.staging_root / "subdir" / "lost.md").unlink()

    first = recover_staging(seeded)

    assert [r.kind for r in first] == ["unrecoverable"]
    assert "subdir/lost.md" in (first[0].message or "")
    assert (seeded / _MOVE_DST).is_dir()
    _os.rename(seeded / _MOVE_DST, seeded / _MOVE_SRC)
    before = _snapshot(seeded)

    again = recover_staging(seeded)

    assert [(r.kind, r.message) for r in again] == [("unrecoverable", _UNSETTLED)]
    assert (seeded / _MOVE_SRC).is_dir()
    assert not (seeded / _MOVE_DST).exists()
    assert _snapshot(seeded) == before
    (issue,) = check_stale_staging(seeded, [])
    assert issue.category == "stale_staging_unrecoverable"
    assert issue.message == (
        "papers/2024_A_c/ is gone and papers/2024_A_b/ is back after an unfinished command."
    )


def test_classifier_finds_a_promoted_file_in_a_folder_not_moved_yet(
    seeded: Path, tmp_path: Path
) -> None:
    """A file promoted into the moved folder, which was then put back by hand,
    is not lost: recovery moves the folder forward and finds it there."""
    from litman.core.checks import check_stale_staging

    ref = _moved_reference(tmp_path)
    sw = _stage_move(seeded, "op-R7")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    sw._write_sentinel()
    sw._staged = dict(list(sw._staged.items())[:1])
    sw._promote()
    _os.rename(seeded / _MOVE_DST, seeded / _MOVE_SRC)

    (issue,) = check_stale_staging(seeded, [])

    assert issue.category == "stale_staging"
    assert issue.message == (
        ".litman-staging/op-R7/ — torn commit, roll-forward pending "
        "(moves papers/2024_A_b/ to papers/2024_A_c/)"
    )
    assert [r.kind for r in recover_staging(seeded)] == ["rolled_forward"]
    assert _snapshot(seeded) == ref


def test_an_interrupt_right_after_the_move_still_moves_the_folder_back(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C landing after the rename returned, before the write noted it."""
    before = _snapshot(seeded)
    real_rename_dir = _atomic._rename_dir
    calls = {"n": 0}

    def rename_then_interrupt(src: Path, dst: Path) -> None:
        real_rename_dir(src, dst)
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(_atomic, "_rename_dir", rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        with staged_write(seeded, op_id="op-R8") as stage:
            stage.move_dir(_MOVE_SRC, _MOVE_DST)
            for rel, content in _MOVE_PAYLOAD.items():
                stage.write_text(rel, content)

    assert (seeded / _MOVE_SRC).is_dir()
    assert not (seeded / _MOVE_DST).exists()
    assert _snapshot(seeded) == before
    assert list((seeded / ".litman-staging").iterdir()) == []


@pytest.mark.parametrize(
    "bad", ["papers/a\x00", "/etc", "C:/x", "papers/../..", ""]
)
@pytest.mark.parametrize("committed", [False, True])
def test_a_manifest_moving_a_path_outside_the_vault_is_kept_as_evidence(
    seeded: Path, bad: str, committed: bool
) -> None:
    from click.testing import CliRunner

    from litman.cli import cli

    op_dir = seeded / ".litman-staging" / "op-R9"
    op_dir.mkdir()
    (op_dir / "MANIFEST.json").write_text(
        _json.dumps({"op_id": "op-R9", "files": [], "dirs": [[bad, _MOVE_SRC]]}),
        encoding="utf-8",
    )
    if committed:
        (op_dir / "COMMITTED").write_bytes(b"")
    before = _snapshot(seeded)

    results = recover_staging(seeded)

    assert [r.kind for r in results] == ["unrecoverable"]
    assert "MANIFEST.json is unreadable" in (results[0].message or "")
    assert op_dir.is_dir()
    assert _snapshot(seeded) == before
    result = CliRunner().invoke(cli, ["list", "--library", str(seeded)])
    assert result.exit_code == 0, result.output
    assert (seeded / _MOVE_SRC).is_dir()


def _sentinel_reaches_disk_then_fails(self: StagedWrite) -> None:
    (self.staging_root / "COMMITTED").write_bytes(b"")
    raise OSError("fsync failed")


def test_a_sentinel_that_reached_disk_is_removed_before_moving_back(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gone first, so a crash between the move back and the cleanup cannot
    leave it beside a folder already back in place."""
    before = _snapshot(seeded)
    monkeypatch.setattr(
        _atomic.StagedWrite, "_write_sentinel", _sentinel_reaches_disk_then_fails
    )
    real_undo = _atomic.StagedWrite._undo_moves
    sentinel_at_undo: list[bool] = []

    def undo(self: StagedWrite) -> bool:
        sentinel_at_undo.append((self.staging_root / "COMMITTED").exists())
        return real_undo(self)

    monkeypatch.setattr(_atomic.StagedWrite, "_undo_moves", undo)

    with pytest.raises(OSError, match="fsync failed"):
        with staged_write(seeded, op_id="op-R10") as stage:
            stage.move_dir(_MOVE_SRC, _MOVE_DST)
            for rel, content in _MOVE_PAYLOAD.items():
                stage.write_text(rel, content)

    assert sentinel_at_undo == [False]
    assert _snapshot(seeded) == before
    assert list((seeded / ".litman-staging").iterdir()) == []
    assert recover_staging(seeded) == []


def test_a_sentinel_that_cannot_be_removed_rolls_the_op_forward_whole(
    seeded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the folder back beside a COMMITTED that stays would let the next
    pass roll it forward again over half a rollback; nothing is undone."""
    ref = _moved_reference(tmp_path)
    monkeypatch.setattr(
        _atomic.StagedWrite, "_write_sentinel", _sentinel_reaches_disk_then_fails
    )
    monkeypatch.setattr(_atomic.StagedWrite, "_drop_sentinel", lambda self: False)

    with pytest.raises(OSError, match="fsync failed"):
        with staged_write(seeded, op_id="op-R10b") as stage:
            stage.move_dir(_MOVE_SRC, _MOVE_DST)
            for rel, content in _MOVE_PAYLOAD.items():
                stage.write_text(rel, content)
    monkeypatch.undo()

    assert (seeded / _MOVE_DST).is_dir()
    assert (seeded / ".litman-staging" / "op-R10b" / "COMMITTED").is_file()
    assert [r.kind for r in recover_staging(seeded)] == ["rolled_forward"]
    assert _snapshot(seeded) == ref



def test_an_empty_unclear_marker_still_reports_what_the_folders_show(
    seeded: Path,
) -> None:
    """Writing the marker can fail after creating it (disk full)."""
    from litman.core.checks import check_stale_staging

    sw = _stage_move(seeded, "op-R3b")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    (seeded / _MOVE_SRC).mkdir()
    (sw.staging_root / "DIRS_UNCLEAR").write_bytes(b"")

    (result,) = recover_staging(seeded)
    (issue,) = check_stale_staging(seeded, [])

    assert result.kind == "unrecoverable"
    assert result.message == _UNSETTLED
    assert issue.message == (
        "papers/2024_A_b/ and papers/2024_A_c/ both exist after an unfinished command."
    )


def test_a_folder_that_cannot_be_looked_at_is_retried_not_judged(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing stat error is not "neither folder exists": nothing is marked,
    and the next pass moves the folder back as it should."""
    from litman.core.checks import check_stale_staging

    before = _snapshot(seeded)
    sw = _stage_move(seeded, "op-R4")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._move_dirs()
    real_exists = _atomic._exists

    def flicker(path: Path) -> bool:
        if path == seeded / _MOVE_DST:
            raise PermissionError(13, "Permission denied", str(path))
        return real_exists(path)

    monkeypatch.setattr(_atomic, "_exists", flicker)
    (result,) = recover_staging(seeded)
    (issue,) = check_stale_staging(seeded, [])
    monkeypatch.undo()

    assert result.kind == "unrecoverable"
    # Names the folder it could not look at, never the op id.
    assert result.message == (
        "Could not check papers/2024_A_c/ (Permission denied). "
        "The next lit command retries."
    )
    assert issue.message == result.message
    assert not (sw.staging_root / "DIRS_UNCLEAR").exists()
    assert [r.kind for r in recover_staging(seeded)] == ["rolled_back"]
    assert _snapshot(seeded) == before


def test_a_teardown_that_cannot_drop_the_moves_keeps_the_whole_op(
    seeded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half a teardown is never left: either the manifest stops listing the
    move first, or every file stays and the next pass finishes the job."""
    ref = _moved_reference(tmp_path)

    def cannot_rewrite(op_dir: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_atomic, "_drop_dir_moves", cannot_rewrite)
    with staged_write(seeded, op_id="op-R1") as stage:
        stage.move_dir(_MOVE_SRC, _MOVE_DST)
        for rel, content in _MOVE_PAYLOAD.items():
            stage.write_text(rel, content)
    op_dir = seeded / ".litman-staging" / "op-R1"
    assert (op_dir / "COMMITTED").is_file()
    manifest = _json.loads((op_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["dirs"] == [[_MOVE_SRC, _MOVE_DST]]
    monkeypatch.undo()

    assert [r.kind for r in recover_staging(seeded)] == ["rolled_forward"]
    assert not op_dir.exists()
    assert _snapshot(seeded) == ref


@pytest.mark.skipif(
    (hasattr(_os, "geteuid") and _os.geteuid() == 0) or _sys.platform == "win32",
    reason=(
        "root bypasses directory permission bits; chmod 000 would not deny. "
        "On Windows chmod never applies to a directory, so nothing is denied."
    ),
)
def test_a_folder_behind_a_permission_error_is_not_read_as_missing(
    tmp_path: Path,
) -> None:
    locked = tmp_path / "locked"
    (locked / "inside").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        assert _atomic._exists(tmp_path / "nowhere") is False
        with pytest.raises(PermissionError):
            _atomic._exists(locked / "inside")
    finally:
        locked.chmod(0o700)


def test_recovery_lines_stay_short_with_ids_as_long_as_real_ones() -> None:
    """A rename's op id is ``rename-<old>-to-<new>``: repeating it beside both
    folders put these lines past 300 characters. A command's line names one
    folder at most and the op id never; health-check's finding names the two
    folders, and its hint the op dir a person has to remove."""
    old = "papers/2026_Oberkampf_" + "a" * 32
    new = "papers/2026_Oberkampf_" + "b" * 32
    assert len(old) - len("papers/") == 47  # the longest id in a real library
    op_id = f"rename-{old[7:]}-to-{new[7:]}"
    reason = "Permission denied"

    vault = Path("vault")
    looked_at = PermissionError(13, reason, str(vault / old))
    at_a_terminal = [
        _atomic._moved_back_message([(new, old)]),
        _atomic._moved_back_message([(new, old)], mode="pending"),
        _atomic._UNSETTLED_LINE,
        _atomic._dir_move_failed_message(old, reason),
        _atomic._folders_unreadable_message(vault, looked_at),
        _atomic._folders_unreadable_message(vault, PermissionError(13, reason)),
        _atomic._marker_failed_message(reason),
    ]
    for line in at_a_terminal:
        assert len(line) <= 140, line
        assert op_id not in line
        assert line.count(old) + line.count(new) <= 1, line

    unsettled = _atomic._dir_moves_unclear_message([(old, new, "both")])
    assert unsettled == f"{old}/ and {new}/ both exist after an unfinished command."
    assert op_id not in unsettled
