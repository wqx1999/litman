"""Integration tests for the M30 Phase 5 *sync-push* staleness nudge.

Dual to the health-check nudge (``test_cli_nudge.py``): post-dispatch in
``LitGroup.invoke``, the CLI reminds the user to ``lit sync push`` when the
active registered vault has a configured remote but ``.litman-sync-state.yaml``
``last_push`` is > :data:`SYNC_STALE_DAYS` days old (or None — never pushed).
Crucially it ONLY fires when ``lit-config.yaml`` ``sync`` is set: a vault
without a configured remote is never nagged. Same emit semantics as the
health-check arm (always emits; TTY → stdout, non-TTY → stderr).

While this machine's last two-way sync has not gone through (its ``last-run``
record says ``failed`` or ``needs-choice``), a line saying so takes the place
of the 7-day one, after every command but ``lit sync``'s own.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import _SYNC_STOP_WORDS, _sync_stopped_line, cli
from litman.commands import _drift
from litman.commands import sync as sync_cmd
from litman.core.library import create_vault
from litman.core.sync import (
    FileChanges,
    LastRun,
    Size,
    StatusReport,
    SyncMode,
    SyncState,
    TwoWayResult,
    read_sync_state,
    record_run,
    write_sync_state,
)
from litman.core.vault_registry import (
    add_vault,
    load_registry,
    mark_health_checked,
    save_registry,
)

_SYNC_NUDGE = "no `lit sync push` in 7+ days"


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME + clear cross-cutting env vars so the registry lands in
    tmp rather than the real ~/.config/litman/."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _configure_remote(vault: Path) -> None:
    """Append a minimal ``sync:`` block to the vault's lit-config.yaml so
    ``load_config(vault).sync`` is non-None."""
    cfg = vault / "lit-config.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8")
        + "\nsync:\n  remote: testremote\n  path: ''\n",
        encoding="utf-8",
    )


def _seed_active_vault(
    tmp_path: Path,
    *,
    configure_remote: bool,
    last_push: str | None,
) -> Path:
    """Register one active vault. Health-check is stamped fresh so only the
    sync arm of the nudge is under test. ``last_push=None`` writes no sync-state
    file (never pushed). Set ``configure_remote=False`` for a vault with no
    remote configured.
    """
    parent = tmp_path / "real_parent"
    parent.mkdir()
    vault = create_vault(parent)
    if configure_remote:
        _configure_remote(vault)
    if last_push is not None:
        write_sync_state(vault, SyncState(last_push=last_push))
    reg = add_vault(load_registry(), "main", vault)
    # Keep health-check fresh so its nudge never confounds these assertions.
    reg = mark_health_checked(reg, "main", _iso_days_ago(0))
    save_registry(reg)
    return vault


def test_sync_nudge_fires_when_stale(tmp_path: Path) -> None:
    """Remote configured + last push 8 days ago (> 7) → nudge on stderr."""
    _seed_active_vault(tmp_path, configure_remote=True, last_push=_iso_days_ago(8))

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _SYNC_NUDGE in result.stderr
    assert _SYNC_NUDGE not in result.stdout


def test_sync_nudge_names_lit_sync_on_a_machine_set_up_that_never_synced(
    tmp_path: Path,
) -> None:
    """Remote configured, nothing synced from here yet: the tip names `lit sync`,
    the step `lit sync setup` names next, not the one-way push."""
    vault = _seed_active_vault(tmp_path, configure_remote=True, last_push=None)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert "no `lit sync` in 7+ days" in result.stderr
    assert _SYNC_NUDGE not in result.stderr
    # The reverse: a machine with only push or pull history keeps the push tip.
    for history in (SyncState(last_push=_iso_days_ago(8)), SyncState(last_pull=_iso_days_ago(8))):
        write_sync_state(vault, history)
        result = CliRunner().invoke(cli, ["list"])
        assert _SYNC_NUDGE in result.stderr, history
        assert "no `lit sync` in" not in result.stderr


def test_sync_nudge_silent_when_fresh(tmp_path: Path) -> None:
    """Remote configured + pushed 2 days ago (< 7) → no nudge."""
    _seed_active_vault(tmp_path, configure_remote=True, last_push=_iso_days_ago(2))

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _SYNC_NUDGE not in result.output


def test_sync_nudge_silent_when_remote_not_configured(tmp_path: Path) -> None:
    """No remote configured → never nagged, even though last_push is None
    (the user has not opted into sync at all)."""
    _seed_active_vault(tmp_path, configure_remote=False, last_push=None)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _SYNC_NUDGE not in result.output


def test_sync_nudge_routes_to_stdout_when_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTY → nudge on stdout tail (mirrors the health-check arm's routing)."""
    _seed_active_vault(tmp_path, configure_remote=True, last_push=_iso_days_ago(30))
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _SYNC_NUDGE in result.stdout


def test_sync_nudge_does_not_fire_for_help(tmp_path: Path) -> None:
    """`lit help` is in the skip set → no nudge even when stale."""
    _seed_active_vault(tmp_path, configure_remote=True, last_push=_iso_days_ago(99))

    result = CliRunner().invoke(cli, ["help"])
    assert result.exit_code == 0, result.output
    assert _SYNC_NUDGE not in result.output


def test_sync_nudge_silent_after_a_recent_two_way_sync(tmp_path: Path) -> None:
    """A two-way `lit sync` within the window counts as much as a push."""
    vault = _seed_active_vault(
        tmp_path, configure_remote=True, last_push=_iso_days_ago(30)
    )
    write_sync_state(
        vault, SyncState(last_push=_iso_days_ago(30), last_sync=_iso_days_ago(1))
    )

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert "no `lit sync" not in result.output


def test_sync_nudge_names_lit_sync_once_two_way_sync_is_in_use(
    tmp_path: Path,
) -> None:
    vault = _seed_active_vault(tmp_path, configure_remote=True, last_push=None)
    write_sync_state(vault, SyncState(last_sync=_iso_days_ago(9)))

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert "no `lit sync` in 7+ days" in result.stderr
    assert _SYNC_NUDGE not in result.stderr


def test_sync_nudge_names_lit_sync_after_a_two_way_try_that_did_not_go_through(
    tmp_path: Path,
) -> None:
    """A first `lit sync` that stopped for a --first-from choice: push now
    refuses this library, so the nudge must not offer it."""
    vault = _seed_active_vault(tmp_path, configure_remote=True, last_push=None)
    write_sync_state(vault, SyncState(last_sync_result="needs-choice"))

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert "no `lit sync` in 7+ days" in result.stderr
    assert _SYNC_NUDGE not in result.stderr


def test_sync_nudge_names_lit_sync_when_this_machine_has_two_way_records(
    tmp_path: Path,
) -> None:
    """The sync-state file is gone, rclone's two-way records are not."""
    from litman.core.config import load_config
    from litman.core.sync import bisync_initialised, sync_workdir

    vault = _seed_active_vault(tmp_path, configure_remote=True, last_push=_iso_days_ago(30))
    sync_config = load_config(vault).sync
    assert sync_config is not None
    work = sync_workdir(vault, sync_config.target_url())
    work.mkdir(parents=True)
    (work / "x.path1.lst").write_text("", encoding="utf-8")
    assert bisync_initialised(work)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert "no `lit sync` in 7+ days" in result.stderr
    assert _SYNC_NUDGE not in result.stderr
    # The reverse: without the records, its push history makes it a one-way
    # library again.
    (work / "x.path1.lst").unlink()
    result = CliRunner().invoke(cli, ["list"])
    assert _SYNC_NUDGE in result.stderr


# ---------------------------------------------------------------------------
# The last sync did not go through
# ---------------------------------------------------------------------------

_WAY_OUT = "Run `lit sync status` for why and what to do."


def _stop(vault: Path, result: str, reason: str | None = None) -> datetime:
    """Record a run that ended ``result`` just now, as a sync does; return its time."""
    at = datetime.now().astimezone().replace(microsecond=0)
    record_run(
        vault,
        LastRun(at=at.isoformat(), trigger="auto", result=result, reason=reason, error="x"),
    )
    return at


@pytest.mark.parametrize(
    ("result", "reason", "verdict"),
    [
        ("failed", "auth", "Cloud sync stopped at {t} (sign-in refused)."),
        ("failed", "safety-stop", "Cloud sync stopped at {t} (too many changes on one side)."),
        ("needs-choice", "choice", "Cloud sync stopped at {t} (a side must be picked to win)."),
        ("failed", None, "Cloud sync failed at {t}."),
    ],
)
def test_a_sync_that_did_not_go_through_is_named_after_a_command_instead_of_the_7_day_tip(
    tmp_path: Path, result: str, reason: str | None, verdict: str
) -> None:
    # Never synced: the 7-day tip is due too.
    vault = _seed_active_vault(tmp_path, configure_remote=True, last_push=None)
    at = _stop(vault, result, reason)

    shown = CliRunner().invoke(cli, ["list"])

    assert shown.exit_code == 0, shown.output
    # One whole line, whatever the width; off a terminal, on stderr.
    assert f"{verdict.format(t=f'{at:%H:%M}')} {_WAY_OUT}" in shown.stderr.splitlines()
    assert "Cloud sync" not in shown.stdout
    assert "no `lit sync" not in shown.output
    # The reverse: a run that goes through replaces the record, and the line
    # goes with it; the 7-day tip is back.
    record_run(vault, LastRun(at=at.isoformat(), trigger="terminal", result="ok"))
    cleared = CliRunner().invoke(cli, ["list"])
    assert cleared.exit_code == 0, cleared.output
    assert "Cloud sync" not in cleared.output
    assert "no `lit sync" in cleared.stderr


@pytest.mark.parametrize("result", ["ok", "conflicts"])
def test_a_sync_that_went_through_leaves_no_line_even_with_conflicts(
    tmp_path: Path, result: str
) -> None:
    vault = _seed_active_vault(tmp_path, configure_remote=True, last_push=_iso_days_ago(1))
    _stop(vault, result)

    quiet = CliRunner().invoke(cli, ["list"])

    assert quiet.exit_code == 0, quiet.output
    assert "Cloud sync" not in quiet.output
    # The reverse: the same library after a run that failed.
    _stop(vault, "failed")
    assert "Cloud sync failed at" in CliRunner().invoke(cli, ["list"]).stderr


def test_a_library_without_sync_set_up_gets_no_line(tmp_path: Path) -> None:
    vault = _seed_active_vault(tmp_path, configure_remote=False, last_push=None)
    _stop(vault, "failed", "auth")

    quiet = CliRunner().invoke(cli, ["list"])

    assert quiet.exit_code == 0, quiet.output
    assert "Cloud sync" not in quiet.output
    # The reverse: once it is set up, the same record is named.
    _configure_remote(vault)
    assert "(sign-in refused)" in CliRunner().invoke(cli, ["list"]).stderr


def test_lit_sync_and_lit_sync_status_print_no_line_they_report_the_sync_themselves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _seed_active_vault(tmp_path, configure_remote=True, last_push=_iso_days_ago(1))
    _stop(vault, "failed", "auth")
    # No rclone: the remote is there, both sides hold nothing, and a sync goes
    # through without touching the record, which still says it failed.
    monkeypatch.setattr(sync_cmd, "remote_exists", lambda name: True)
    monkeypatch.setattr(
        sync_cmd, "read_sync_mode", lambda vault, target: SyncMode(True, False, False)
    )
    monkeypatch.setattr(
        sync_cmd,
        "compute_status",
        lambda vault, target, **kwargs: StatusReport(
            target, Size(0, 0), Size(0, 0), read_sync_state(vault), two_way=True
        ),
    )
    monkeypatch.setattr(
        sync_cmd,
        "two_way_sync",
        lambda vault, target, **kwargs: TwoWayResult(
            target_url=target,
            mode="sync",
            chosen=False,
            came_down=FileChanges(),
            went_up=FileChanges(),
            conflicts=(),
            remote_changed=False,
            local_backups=(),
            projects_without_folder=(),
        ),
    )

    status = CliRunner().invoke(cli, ["sync", "status"])
    synced = CliRunner().invoke(cli, ["sync"])

    assert status.exit_code == 0, status.output
    assert "failed (the cloud refused the sign-in)." in status.output
    assert synced.exit_code == 0, synced.output
    assert "Synced." in synced.output
    for result in (status, synced):
        assert "Cloud sync" not in result.output
    # The reverse: any other command names it.
    assert "(sign-in refused)" in CliRunner().invoke(cli, ["list"]).stderr


def test_the_line_names_another_day_by_its_date_and_stays_short() -> None:
    now = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    at = now.astimezone() - timedelta(days=1)
    run = LastRun(at=at.isoformat(), trigger="auto", result="needs-choice", reason="choice")

    assert _sync_stopped_line(run, now=now) == (
        f"Cloud sync stopped on {at:%Y-%m-%d} at {at:%H:%M} (a side must be picked to win). "
        + _WAY_OUT
    )
    # One verdict and one way out in about 120 characters, at its longest.
    for reason in [*_SYNC_STOP_WORDS, None, "unknown"]:
        for result in ("failed", "needs-choice"):
            line = _sync_stopped_line(replace(run, result=result, reason=reason), now=now)
            assert line is not None
            assert len(line) <= 120, line
    # A record without a time it can read is still named.
    assert _sync_stopped_line(replace(run, at="not a time"), now=now) == (
        f"Cloud sync stopped (a side must be picked to win). {_WAY_OUT}"
    )
    # The reverse: the same run today is named by its time alone.
    today = replace(run, at=now.astimezone().isoformat())
    assert _sync_stopped_line(today, now=now) == (
        f"Cloud sync stopped at {now.astimezone():%H:%M} (a side must be picked to win). "
        + _WAY_OUT
    )
