"""`lit sync status --local`: how this library syncs, from this machine's records alone.

An agent asks it at the start of every session, so it has to answer at once
and offline: no rclone call at all, where plain `lit sync status` lists the
cloud and counts every file in it. Every test here runs with rclone made to
fail; the last one shows the stub bites on plain `lit sync status`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from litman.cli import cli
from litman.core import sync as core_sync
from litman.core.library import create_vault
from litman.core.sync import (
    LastRun,
    SetupPayload,
    SyncState,
    conflict_backups_root,
    record_run,
    sync_workdir,
    write_sync_state,
    write_sync_to_config,
)

_TARGET = "cloud:lib"
_ISO = "2026-11-01T10:00:00+01:00"


@pytest.fixture(autouse=True)
def no_rclone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any rclone call fails the test: --local must not make one."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"rclone was called: {args!r}")

    monkeypatch.setattr(core_sync, "run_rclone", refuse)
    monkeypatch.setattr(core_sync, "run_bisync", refuse)


def _vault(tmp_path: Path, *, sync: bool = True) -> Path:
    parent = tmp_path / "machine"
    parent.mkdir()
    vault = create_vault(parent)
    if sync:
        write_sync_to_config(vault / "lit-config.yaml", SetupPayload(remote="cloud", path="lib"))
    return vault


def _local(vault: Path) -> Result:
    return CliRunner().invoke(cli, ["sync", "status", "--local", "--library", str(vault)])


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _mode(result: Result) -> str:
    assert result.exit_code == 0, result.output
    rows = [line for line in result.output.splitlines() if " Mode " in line]
    assert len(rows) == 1, result.output
    return rows[0].split(" Mode ", 1)[1].strip(" │|")


def test_a_library_without_sync_says_it_is_not_set_up(tmp_path: Path) -> None:
    vault = _vault(tmp_path, sync=False)

    result = _local(vault)

    assert result.exit_code == 0, result.output
    assert "Mode: not set up. `lit sync setup` sets up cloud sync" in _one_line(result.output)
    # The reverse: once set up, the same library has a mode.
    write_sync_to_config(vault / "lit-config.yaml", SetupPayload(remote="cloud", path="lib"))
    assert _mode(_local(vault)) == "not synced from this machine yet"


@pytest.mark.parametrize(
    ("state", "mode", "rows", "not_rows"),
    [
        (SyncState(), "not synced from this machine yet", ("Last push",), ("Last sync",)),
        (SyncState(last_push=_ISO), "one-way (push / pull)", ("Last push",), ("Last sync",)),
        (SyncState(last_pull=_ISO), "one-way (push / pull)", ("Last pull",), ("Last sync",)),
        (SyncState(last_sync=_ISO), "two-way", ("Last sync",), ("Last push",)),
        # A first `lit sync` that stopped for a choice: push now refuses it.
        (SyncState(last_sync_result="needs-choice"), "two-way", ("Last sync",), ("Last push",)),
        # Pushed once, then synced two-way: two-way.
        (SyncState(last_push=_ISO, last_sync=_ISO), "two-way", ("Last sync",), ("Last push",)),
    ],
)
def test_each_mode_comes_from_this_machines_records(
    tmp_path: Path,
    state: SyncState,
    mode: str,
    rows: tuple[str, ...],
    not_rows: tuple[str, ...],
) -> None:
    vault = _vault(tmp_path)
    write_sync_state(vault, state)

    result = _local(vault)

    assert _mode(result) == mode
    for row in rows:
        assert row in result.output
    for row in not_rows:
        assert row not in result.output


def test_rclones_own_records_alone_make_it_two_way(tmp_path: Path) -> None:
    """The state file is gone, rclone's two-way records are not."""
    vault = _vault(tmp_path)
    work = sync_workdir(vault, _TARGET)
    work.mkdir(parents=True)
    (work / "x.path1.lst").write_text("", encoding="utf-8")

    assert _mode(_local(vault)) == "two-way"
    # The reverse: without them, nothing has synced from here.
    (work / "x.path1.lst").unlink()
    assert _mode(_local(vault)) == "not synced from this machine yet"


def test_it_shows_the_last_run_and_the_conflict_backups(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    write_sync_state(vault, SyncState(last_sync=_ISO))
    record_run(
        vault,
        LastRun(
            at=_ISO,
            trigger="auto",
            result="failed",
            reason="auth",
            error="cloud refused the password, so nothing was synced.",
            sign_in="password",
        ),
    )
    folder = conflict_backups_root(vault, _TARGET) / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    copy = folder / "papers" / "p1" / "notes.md.sync-conflict2"
    copy.parent.mkdir(parents=True)
    copy.write_text("the cloud's notes\n", encoding="utf-8")

    said = _one_line(_local(vault).output)

    assert "the GUI's auto sync: failed (the cloud refused the sign-in)." in said
    # The error does not name the way back, so "What to do:" does.
    assert (
        "What to do: Enter the password again with `rclone config` (edit remote cloud), "
        "then run `lit sync`." in said
    )
    assert "Conflict backups from the last 7 days:" in said
    assert "papers/p1/notes.md.sync-conflict2" in said


@pytest.mark.parametrize(
    ("error", "sign_in", "way_back"),
    [
        (
            "cloud refused the password, so nothing was synced. Enter the password again "
            "with `rclone config` (edit remote cloud), then run `lit sync`.\n"
            "CRITICAL: read metadata failed: 401 Unauthorized",
            "password",
            "Enter the password again with `rclone config` (edit remote cloud), "
            "then run `lit sync`.",
        ),
        (
            "The sign-in to cloud has expired, so nothing was synced. Sign in again with "
            "`rclone config reconnect cloud:`, then run `lit sync`.\n"
            "ERROR : couldn't fetch token: invalid_grant",
            "browser",
            "Sign in again with `rclone config reconnect cloud:`, then run `lit sync`.",
        ),
    ],
)
def test_an_auth_error_that_names_the_way_back_gets_no_what_to_do_line(
    tmp_path: Path, error: str, sign_in: str, way_back: str
) -> None:
    """As for rclone's safety stop: the error says it, so it is not said twice."""
    vault = _vault(tmp_path)
    write_sync_state(vault, SyncState(last_sync=_ISO))
    record_run(
        vault,
        LastRun(
            at=_ISO,
            trigger="auto",
            result="failed",
            reason="auth",
            error=error,
            sign_in=sign_in,
        ),
    )

    said = _one_line(_local(vault).output)

    assert said.count(way_back) == 1
    assert "What to do:" not in said
    # The reverse: an error without it, written before litman named it there
    # (rclone's OAuth words only), still gets the line.
    record_run(
        vault,
        LastRun(
            at=_ISO,
            trigger="auto",
            result="failed",
            reason="auth",
            error="ERROR : couldn't fetch token: invalid_grant",
        ),
    )
    said = _one_line(_local(vault).output)
    assert (
        "What to do: Sign in again with `rclone config reconnect cloud:`, then run `lit sync`."
        in said
    )


def test_plain_status_does_ask_rclone(tmp_path: Path) -> None:
    """The control for the stub: without --local, status calls rclone at once."""
    vault = _vault(tmp_path)

    result = CliRunner().invoke(cli, ["sync", "status", "--library", str(vault)])

    assert isinstance(result.exception, AssertionError), result.output
    assert "rclone was called" in str(result.exception)
