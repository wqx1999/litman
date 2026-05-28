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
