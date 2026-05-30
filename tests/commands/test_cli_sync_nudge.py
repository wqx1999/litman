"""Integration tests for the M30 Phase 5 *sync-push* staleness nudge.

Dual to the health-check nudge (``test_cli_nudge.py``): post-dispatch in
``LitGroup.invoke``, the CLI reminds the user to ``lit sync push`` when the
active registered vault has a configured remote but ``.litman-sync-state.yaml``
``last_push`` is > :data:`SYNC_STALE_DAYS` days old (or None — never pushed).
Crucially it ONLY fires when ``lit-config.yaml`` ``sync`` is set: a vault
without a configured remote is never nagged. Same emit semantics as the
health-check arm (always emits; TTY → stdout, non-TTY → stderr).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands import _drift
from litman.core.library import create_vault
from litman.core.sync import SyncState, write_sync_state
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
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


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


def test_sync_nudge_fires_when_never_pushed(tmp_path: Path) -> None:
    """Remote configured but never pushed (no sync-state → last_push None) →
    nudge fires."""
    _seed_active_vault(tmp_path, configure_remote=True, last_push=None)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _SYNC_NUDGE in result.stderr


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
