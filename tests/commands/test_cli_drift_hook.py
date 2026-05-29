"""Integration tests for the M28 drift hook on the root CLI group.

These tests exercise ``LitGroup.invoke`` end-to-end (via ``CliRunner``) so
the skip list, prompt path, and CliRunner's non-TTY default are all wired
correctly. The unit-level behavior of ``check_and_prompt_registry_drift``
itself is covered in ``test_drift.py``.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands import _drift
from litman.core.library import create_vault
from litman.core.vault_registry import (
    VaultEntry,
    VaultRegistry,
    load_registry,
    save_registry,
)


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME + clear cross-cutting env vars so the drift hook
    reads/writes a tmp registry rather than the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _seed_dangling_plus_active(tmp_path: Path) -> Path:
    """Persist a registry with one real (active) vault + one dangling entry.

    Returns the path of the real vault.
    """
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    real_vault = create_vault(real_parent)  # active vault
    ghost = tmp_path / "ghost"  # never created on disk

    reg = VaultRegistry(
        vaults=[
            VaultEntry(name="real", path=str(real_vault), is_active=True),
            VaultEntry(name="ghost", path=str(ghost), is_active=False),
        ]
    )
    save_registry(reg)
    return real_vault


def _add_missing_project(vault: Path, tmp_path: Path) -> None:
    """Configure the active vault with one project whose dir does not exist.

    Makes the unified ``project_path_exists`` cheap check fire so the hook's
    project-drift corrector is dispatched (M30 Phase 2: correctors run only
    when their category is detected).
    """
    missing = tmp_path / "gone_project"  # never created
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  ghostproj: {missing}\n",
        encoding="utf-8",
    )


def test_lit_list_triggers_drift_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running ``lit list`` with a TTY-forced drift hook prunes the dangling
    entry before list executes — proves the root-group hook is wired into
    every non-skipped subcommand."""
    _seed_dangling_plus_active(tmp_path)

    # CliRunner is non-TTY by default. Force the drift probe to True so we
    # exercise the prompt branch, then auto-answer Y.
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: True)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    # ``lit list`` itself runs cleanly on the real (active) vault.
    assert result.exit_code == 0, result.output
    # And the dangling entry has been pruned by the hook.
    remaining = load_registry().vaults
    assert [v.name for v in remaining] == ["real"]
    # Positive output assertion: the TTY-yes branch's "Removed N dangling"
    # rendering must have reached the user. Without this we would only know
    # the registry mutated, not that the user saw why.
    assert "dangling" in result.output.lower()
    assert "Removed" in result.output


def test_lit_help_skips_drift_prompt(tmp_path: Path) -> None:
    """``lit help`` is in the skip list — running it must not touch the
    registry even when a dangling entry exists. CliRunner is non-TTY by
    default; if the hook fired we'd see the stderr warning, but we'd never
    see a prompt regardless. The contract is "skip means no-op": the
    registry stays exactly as-is."""
    _seed_dangling_plus_active(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["help"])

    assert result.exit_code == 0, result.output
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]
    # Skip-list AC: drift output must NOT appear at all (not even the
    # non-TTY stderr warning). ``result.output`` is the mixed stdout+stderr
    # stream in Click 8.2+, so this catches a silently broken _DRIFT_SKIP
    # that lets the hook fire and emit the non-TTY warning.
    assert "lit vault remove" not in result.output
    assert "dangling" not in result.output.lower()


def test_hook_calls_registry_then_project_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-skipped subcommand fires BOTH drift correctors, registry first
    then project, in that order (registry-drift owns the missing-vault case,
    so project-drift must run after it).

    M30 Phase 2: the hook runs the cheap detection subset, then dispatches a
    corrector only for a category that fired. We seed a dangling registry entry
    (fires ``vault_registry_drift``) AND a missing project dir (fires
    ``project_path_exists``) so both correctors are dispatched.
    """
    real_vault = _seed_dangling_plus_active(tmp_path)
    _add_missing_project(real_vault, tmp_path)

    order: list[str] = []
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_registry_drift",
        lambda *a, **kw: order.append("registry"),
    )
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_project_drift",
        lambda *a, **kw: order.append("project"),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    assert order == ["registry", "project"]


def test_hook_dispatches_only_fired_correctors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook gates each corrector on its unified check firing (Phase 2).

    With a dangling registry entry but NO project drift, only the registry
    corrector is dispatched; the project corrector is not called."""
    _seed_dangling_plus_active(tmp_path)  # registry drift only, no project map

    fired: list[str] = []
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_registry_drift",
        lambda *a, **kw: fired.append("registry"),
    )
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_project_drift",
        lambda *a, **kw: fired.append("project"),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    assert fired == ["registry"]


def test_hook_project_drift_exception_does_not_crash_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise inside project-drift heal must not crash the user's command —
    the hook wraps it defensively and the actual subcommand still runs."""
    real_vault = _seed_dangling_plus_active(tmp_path)
    _add_missing_project(real_vault, tmp_path)  # makes project_path_exists fire

    monkeypatch.setattr(
        _drift, "check_and_prompt_registry_drift", lambda *a, **kw: None
    )

    def _boom(*a: object, **kw: object) -> None:
        raise RuntimeError("heal blew up")

    monkeypatch.setattr(_drift, "check_and_prompt_project_drift", _boom)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output


def test_hook_help_skips_both_drift_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: ``lit help`` skips BOTH drift segments (neither fires)."""
    _seed_dangling_plus_active(tmp_path)

    fired: list[str] = []
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_registry_drift",
        lambda *a, **kw: fired.append("registry"),
    )
    monkeypatch.setattr(
        _drift,
        "check_and_prompt_project_drift",
        lambda *a, **kw: fired.append("project"),
    )

    runner = CliRunner()
    for argv in (["help"], ["hello"], []):
        fired.clear()
        result = runner.invoke(cli, argv)
        assert result.exit_code in (0, 2), result.output
        assert fired == [], f"{argv!r} should skip the drift hook"


def test_lit_no_args_skips_drift_prompt(tmp_path: Path) -> None:
    """``lit`` with no subcommand has ``invoked_subcommand is None`` — also
    in the skip list (the user is about to see the help message; don't
    ambush them with a registry prompt)."""
    _seed_dangling_plus_active(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, [])

    # Click renders help and exits 0 (or 2 depending on the version) — either
    # way the contract is "registry unchanged".
    assert result.exit_code in (0, 2), result.output
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]
    # Skip-list AC: drift output must NOT appear at all (not even the
    # non-TTY stderr warning). ``result.output`` is the mixed stdout+stderr
    # stream in Click 8.2+, so this catches a silently broken _DRIFT_SKIP
    # that lets the hook fire and emit the non-TTY warning.
    assert "lit vault remove" not in result.output
    assert "dangling" not in result.output.lower()


def test_hook_non_tty_reports_registry_drift_without_mutating(
    tmp_path: Path,
) -> None:
    """Non-TTY (CliRunner default): the hook surfaces registry drift as a
    stderr warning and does NOT prune (spec §6: agent / non-TTY = report-only,
    no auto-mutate). Preserves the M28 behavior through the Phase-2 rewire."""
    _seed_dangling_plus_active(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    # The registry is untouched — non-TTY never mutates without consent.
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]
    # ...but the drift IS surfaced (one warning naming the entry + the fix).
    assert "ghost" in result.output
    assert "lit vault remove" in result.output


def test_hook_project_drift_non_tty_no_mutation(
    tmp_path: Path,
) -> None:
    """Non-TTY project drift via the hook: warn, never rewrite lit-config.yaml.

    Preserves the non-destructive, no-auto-mutate default for project-path
    drift through the unified-detection rewire (spec §6)."""
    real_vault = _seed_dangling_plus_active(tmp_path)
    _add_missing_project(real_vault, tmp_path)
    config_before = (real_vault / "lit-config.yaml").read_bytes()

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    # Config untouched (no heal in non-TTY) ...
    assert (real_vault / "lit-config.yaml").read_bytes() == config_before
    # ... but the project drift is surfaced.
    assert "ghostproj" in result.output
    assert "lit project set-path" in result.output
