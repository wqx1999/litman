"""A promotion the disk refuses while litman runs: the lock comes back, and no
recovery later lands a write its caller reported as failed.

A crash between ``COMMITTED`` and the promotion still rolls forward: the next
recovery finishes the write. A refusal is no crash; the process is there to
tidy up. Refused at the first file, nothing changed, so the write is called
off. Refused after a file landed, the old version of that file is gone, so the
write stays for recovery to finish, and the error says so.

The refusal is injected into ``os.replace``, so these run on every platform;
one more makes a paper folder read-only for real, where that stops a rename.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from litman.core.atomic import (
    StagedWrite,
    left_for_recovery,
    recover_staging,
    refused_file,
    staged_write,
)
from litman.core.library import create_vault
from litman.core.locking import lock_truth_file

META = "papers/2024_A_b/metadata.yaml"
OTHER = "papers/2024_C_d/metadata.yaml"
INDEX = '{"rev": 1}\n'


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    vault = create_vault(tmp_path)
    for rel in (META, OTHER):
        path = vault / rel
        path.parent.mkdir(parents=True)
        path.write_text(f"{rel}: rev 1\n", encoding="utf-8")
        lock_truth_file(path)
    (vault / "INDEX.json").write_text(INDEX, encoding="utf-8")
    return vault


def _locked(path: Path) -> bool:
    return not path.stat().st_mode & stat.S_IWRITE


def _ops(vault: Path) -> list[str]:
    staging = vault / ".litman-staging"
    return sorted(p.name for p in staging.iterdir()) if staging.is_dir() else []


def _refuse(monkeypatch: Any, vault: Path, rel: str) -> None:
    """The disk refuses to put ``rel`` in place, as a read-only folder does."""
    target = (vault / rel).resolve()
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if ".litman-staging" in str(src) and Path(dst).resolve() == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(dst))
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)


def _write(vault: Path, *rels: str) -> None:
    with staged_write(vault, op_id="modify-x") as stage:
        for rel in rels:
            stage.write_text(rel, f"{rel}: rev 2\n")


def _crash_before_promote(vault: Path) -> None:
    """Stage META and pass the decision point; the process dies before promoting."""
    sw = StagedWrite(vault, op_id="modify-crash")
    sw.__enter__()
    sw.write_text(META, f"{META}: rev 2\n")
    sw._fsync_staged_files()
    sw._write_manifest()
    sw._write_sentinel()


def test_a_refused_first_file_keeps_its_lock_and_is_never_replayed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as held:
        _refuse(held, vault, META)
        with pytest.raises(PermissionError) as raised:
            _write(vault, META, "INDEX.json")

    assert refused_file(raised.value) == META
    assert not left_for_recovery(raised.value)
    assert _locked(vault / META)
    assert _ops(vault) == []
    # What a restart (or the next command) does: nothing is left to replay.
    assert recover_staging(vault) == []
    assert (vault / META).read_text(encoding="utf-8") == f"{META}: rev 1\n"
    assert (vault / "INDEX.json").read_text(encoding="utf-8") == INDEX


@pytest.mark.skipif(
    sys.platform == "win32", reason="a read-only folder does not stop a rename on Windows"
)
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes to a read-only folder"
)
def test_a_read_only_paper_folder_is_refused_for_real_the_same_way(vault: Path) -> None:
    folder = (vault / META).parent
    folder.chmod(0o555)
    try:
        with pytest.raises(PermissionError) as raised:
            _write(vault, META, "INDEX.json")

        assert refused_file(raised.value) == META
        assert _locked(vault / META)
        assert _ops(vault) == []
        assert recover_staging(vault) == []
        assert (vault / META).read_text(encoding="utf-8") == f"{META}: rev 1\n"
    finally:
        folder.chmod(0o755)


def test_a_crash_between_commit_and_promote_still_rolls_forward_locked(vault: Path) -> None:
    _crash_before_promote(vault)

    [result] = recover_staging(vault)

    assert result.kind == "rolled_forward"
    assert (vault / META).read_text(encoding="utf-8") == f"{META}: rev 2\n"
    assert _locked(vault / META)
    assert _ops(vault) == []


def test_a_refusal_after_a_file_landed_is_left_for_recovery_with_its_lock_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as held:
        _refuse(held, vault, OTHER)
        with pytest.raises(PermissionError) as raised:
            _write(vault, META, OTHER, "INDEX.json")

    assert refused_file(raised.value) == OTHER
    assert left_for_recovery(raised.value)
    # META landed and was locked again; OTHER is as it was, lock included.
    assert (vault / META).read_text(encoding="utf-8") == f"{META}: rev 2\n"
    assert _locked(vault / META)
    assert (vault / OTHER).read_text(encoding="utf-8") == f"{OTHER}: rev 1\n"
    assert _locked(vault / OTHER)
    assert _ops(vault) == ["modify-x"]

    [result] = recover_staging(vault)

    assert result.kind == "rolled_forward"
    assert (vault / OTHER).read_text(encoding="utf-8") == f"{OTHER}: rev 2\n"
    assert _locked(vault / OTHER)
    assert (vault / "INDEX.json").read_text(encoding="utf-8") == "INDEX.json: rev 2\n"
    assert _ops(vault) == []


def test_a_recovery_whose_replace_is_refused_puts_the_lock_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _crash_before_promote(vault)

    with monkeypatch.context() as held:
        _refuse(held, vault, META)
        [refused] = recover_staging(vault)

    # Kept for the next pass, the file as it was and still read-only.
    assert refused.kind == "unrecoverable"
    assert _ops(vault) == ["modify-crash"]
    assert (vault / META).read_text(encoding="utf-8") == f"{META}: rev 1\n"
    assert _locked(vault / META)

    [result] = recover_staging(vault)

    assert result.kind == "rolled_forward"
    assert (vault / META).read_text(encoding="utf-8") == f"{META}: rev 2\n"
    assert _locked(vault / META)


def test_a_called_off_write_moves_its_folder_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = "papers/2024_A_c"
    with monkeypatch.context() as held:
        _refuse(held, vault, f"{moved}/metadata.yaml")
        with pytest.raises(PermissionError) as raised:
            with staged_write(vault, op_id="rename-x") as stage:
                stage.move_dir("papers/2024_A_b", moved)
                stage.write_text(f"{moved}/metadata.yaml", "id: 2024_A_c\n")
                stage.write_text("INDEX.json", "{}\n")

    assert not left_for_recovery(raised.value)
    assert not (vault / moved).exists()
    assert (vault / META).read_text(encoding="utf-8") == f"{META}: rev 1\n"
    assert _locked(vault / META)
    assert _ops(vault) == []
    assert recover_staging(vault) == []


def test_a_decision_that_cannot_be_taken_back_is_left_for_recovery(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as held:
        _refuse(held, vault, META)
        held.setattr(StagedWrite, "_drop_sentinel", lambda _self: False)
        with pytest.raises(PermissionError) as raised:
            _write(vault, META, "INDEX.json")

    assert left_for_recovery(raised.value)
    assert _locked(vault / META)
    assert _ops(vault) == ["modify-x"]

    [result] = recover_staging(vault)

    assert result.kind == "rolled_forward"
    assert (vault / META).read_text(encoding="utf-8") == f"{META}: rev 2\n"
    assert _locked(vault / META)
