"""Integration tests for the M30 Phase 5 staleness nudge on the root CLI group.

The nudge fires post-dispatch in ``LitGroup.invoke`` when the active registered
vault's ``last_health_check_at`` is > 14 days old (or None / unparseable). It
ALWAYS emits (invariant #5 / ADR-007): TTY → stdout tail, non-TTY → stderr,
stdout clean. These tests drive ``lit list`` (a non-skipped read command) via
``CliRunner`` so the skip gate, the active-vault-only rule, and the TTY routing
are all exercised end-to-end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands import _drift
from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    load_registry,
    mark_health_checked,
    save_registry,
)

_NUDGE = "no `lit health-check` in 14+ days"


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


def _seed_active_vault(tmp_path: Path, *, last_check: str | None) -> Path:
    """Register one active vault with ``last_health_check_at = last_check``.

    Pass ``last_check=None`` for "never checked". Returns the vault path.
    """
    parent = tmp_path / "real_parent"
    parent.mkdir()
    vault = create_vault(parent)
    reg = add_vault(load_registry(), "main", vault)
    if last_check is not None:
        reg = mark_health_checked(reg, "main", last_check)
    save_registry(reg)
    return vault


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_nudge_fires_when_stale_non_tty_stderr(tmp_path: Path) -> None:
    """15 days stale + non-TTY (CliRunner default) → nudge on stderr, stdout
    stays clean for pipes."""
    _seed_active_vault(tmp_path, last_check=_iso_days_ago(15))

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _NUDGE in result.stderr
    assert _NUDGE not in result.stdout


def test_nudge_fires_when_never_checked(tmp_path: Path) -> None:
    """A None timestamp counts as stale → nudge fires."""
    _seed_active_vault(tmp_path, last_check=None)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _NUDGE in result.stderr


def test_nudge_does_not_fire_when_fresh(tmp_path: Path) -> None:
    """A check 5 days ago (< 14) → no nudge."""
    _seed_active_vault(tmp_path, last_check=_iso_days_ago(5))

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _NUDGE not in result.output


def test_nudge_does_not_fire_for_help(tmp_path: Path) -> None:
    """`lit help` is in the skip set → no nudge even when stale."""
    _seed_active_vault(tmp_path, last_check=_iso_days_ago(99))

    result = CliRunner().invoke(cli, ["help"])
    assert result.exit_code == 0, result.output
    assert _NUDGE not in result.output


def test_nudge_does_not_fire_for_bare_lit(tmp_path: Path) -> None:
    """Bare `lit` (no subcommand → None in skip set) → no nudge."""
    _seed_active_vault(tmp_path, last_check=_iso_days_ago(99))

    result = CliRunner().invoke(cli, [])
    assert _NUDGE not in result.output


def test_nudge_does_not_fire_without_active_vault(tmp_path: Path) -> None:
    """No active registered vault (empty registry) → no nudge. The command
    still runs (against an explicit --library here)."""
    parent = tmp_path / "unregistered_parent"
    parent.mkdir()
    vault = create_vault(parent)  # NOT registered

    result = CliRunner().invoke(cli, ["list", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert _NUDGE not in result.output


def test_nudge_routes_to_stdout_when_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTY → nudge on stdout tail. Forcing the TTY probe True with a clean
    registry exercises stdout routing without triggering a drift prompt."""
    _seed_active_vault(tmp_path, last_check=_iso_days_ago(20))
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _NUDGE in result.stdout


def test_cheap_hook_uses_single_shared_bounded_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification task (3): the Tier-1 cheap hook collapses the registry +
    project bounded-stat into ONE _exists_bounded call per invocation."""
    vault = _seed_active_vault(tmp_path, last_check=_iso_days_ago(1))
    # Give the active vault one project so project_path_exists would otherwise
    # probe a second time — proving the collapse, not a degenerate one-path case.
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n"
        f"  proj: {tmp_path / 'some_project'}\n",
        encoding="utf-8",
    )

    calls: list[list[str]] = []
    real = _drift._exists_bounded

    def _spy(paths: list[str], *a: object, **kw: object) -> dict[str, bool | None]:
        calls.append(list(paths))
        return real(paths, *a, **kw)

    monkeypatch.setattr(_drift, "_exists_bounded", _spy)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output

    # The cheap hook's shared probe is exactly one call; it must include BOTH
    # the registry vault path and the project path (the two checks no longer
    # each probe). _cheap_active_vault probes the active path separately (that
    # is a different concern — resolving the vault, not the drift checks), so
    # we assert there is exactly one call carrying multiple gathered paths.
    multi_path_calls = [c for c in calls if len(c) >= 2]
    assert len(multi_path_calls) == 1, calls
    shared = multi_path_calls[0]
    assert str(vault) in shared
    assert str(tmp_path / "some_project") in shared
