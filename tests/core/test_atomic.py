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
    assert "已 roll-forward 同 op 内其余 2 个文件" in (r.message or "")
    assert "可 roll-forward" not in (r.message or "")
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
