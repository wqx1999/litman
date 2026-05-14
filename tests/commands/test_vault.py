"""Tests for the M8.2 ``lit vault`` CLI command group.

Companion to ``test_vault_registry.py`` (which covers the data layer
and find_vault discovery chain integration). This file exercises the
Click entry points end-to-end: argument parsing, error messages, Rich
output, exit codes, and registry-file side effects.

Every test redirects ``$HOME`` through monkeypatch so the registry file
lands in a tmp-path-rooted ``~/.config/litman/vaults.yaml`` and never
touches the real one.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    find_active,
    find_by_name,
    load_registry,
    registry_path,
    save_registry,
)
from litman.core.vault_registry import VaultRegistry
from litman.exceptions import VaultRegistryError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Clear cross-cutting env vars so registry_path() resolution is
    # deterministic regardless of the user's real shell environment.
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.fixture
def vault_a(tmp_path: Path) -> Path:
    parent = tmp_path / "parent_a"
    parent.mkdir()
    return create_vault(parent, name="vault_a")


@pytest.fixture
def vault_b(tmp_path: Path) -> Path:
    parent = tmp_path / "parent_b"
    parent.mkdir()
    return create_vault(parent, name="vault_b")


# ---------------------------------------------------------------------------
# lit vault add
# ---------------------------------------------------------------------------


def test_cli_vault_add_first_registers_and_activates(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    assert result.exit_code == 0, result.output
    assert "Registered" in result.output
    assert "active" in result.output  # first vault auto-active

    reg = load_registry()
    assert len(reg.vaults) == 1
    assert reg.vaults[0].name == "main"
    assert reg.vaults[0].is_active is True


def test_cli_vault_add_second_not_active_by_default(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    result = runner.invoke(cli, ["vault", "add", "second", str(vault_b)])
    assert result.exit_code == 0, result.output
    assert "not active" in result.output

    reg = load_registry()
    by_name = {v.name: v for v in reg.vaults}
    assert by_name["main"].is_active is True
    assert by_name["second"].is_active is False


def test_cli_vault_add_use_flag_transfers_active(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    result = runner.invoke(
        cli, ["vault", "add", "second", str(vault_b), "--use"]
    )
    assert result.exit_code == 0, result.output

    reg = load_registry()
    by_name = {v.name: v for v in reg.vaults}
    assert by_name["main"].is_active is False
    assert by_name["second"].is_active is True


def test_cli_vault_add_import_from_auto_fills_today(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "vault", "add", "zhang", str(vault_a),
            "--import-from", "Zhang via USB drop",
        ],
    )
    assert result.exit_code == 0, result.output

    reg = load_registry()
    entry = find_by_name(reg, "zhang")
    assert entry is not None
    assert entry.imported_from == "Zhang via USB drop"
    assert entry.imported_at == date.today().isoformat()


def test_cli_vault_add_explicit_import_at_overrides_default(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "vault", "add", "zhang", str(vault_a),
            "--import-from", "Zhang",
            "--import-at", "2026-01-15",
        ],
    )
    assert result.exit_code == 0, result.output
    entry = find_by_name(load_registry(), "zhang")
    assert entry is not None
    assert entry.imported_at == "2026-01-15"


def test_cli_vault_add_duplicate_name_errors(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    result = runner.invoke(cli, ["vault", "add", "main", str(vault_b)])
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)
    assert "already registered" in str(result.exception)


def test_cli_vault_add_bad_name_errors(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["vault", "add", "bad:name", str(vault_a)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)
    assert "Invalid vault name" in str(result.exception)


def test_cli_vault_add_nonexistent_path_errors(
    fake_home: Path, tmp_path: Path
) -> None:
    nowhere = tmp_path / "does-not-exist"
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "add", "main", str(nowhere)])
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)


def test_cli_vault_add_path_without_lit_config_errors(
    fake_home: Path, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "add", "main", str(plain)])
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)
    assert "lit-config.yaml" in str(result.exception)


def test_cli_vault_add_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "add", "--help"])
    assert result.exit_code == 0
    assert "--import-from" in result.output
    assert "--import-at" in result.output
    assert "--use" in result.output


# ---------------------------------------------------------------------------
# lit vault use
# ---------------------------------------------------------------------------


def test_cli_vault_use_switches_active(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    runner.invoke(cli, ["vault", "add", "second", str(vault_b)])
    result = runner.invoke(cli, ["vault", "use", "second"])
    assert result.exit_code == 0, result.output
    assert "Active vault" in result.output

    reg = load_registry()
    active = find_active(reg)
    assert active is not None
    assert active.name == "second"


def test_cli_vault_use_missing_name_errors(fake_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "use", "ghost"])
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)
    assert "No vault named" in str(result.exception)


def test_cli_vault_use_idempotent_on_already_active(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    result = runner.invoke(cli, ["vault", "use", "main"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# lit vault list
# ---------------------------------------------------------------------------


def test_cli_vault_list_empty_registry(fake_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "list"])
    assert result.exit_code == 0, result.output
    assert "No vaults registered" in result.output


def test_cli_vault_list_shows_all_vaults_and_active_marker(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    runner.invoke(cli, ["vault", "add", "second", str(vault_b)])
    result = runner.invoke(cli, ["vault", "list"])
    assert result.exit_code == 0, result.output
    assert "main" in result.output
    assert "second" in result.output
    assert "Registered vaults (2)" in result.output


def test_cli_vault_list_shows_provenance(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "vault", "add", "zhang", str(vault_a),
            "--import-from", "Zhang via USB drop",
        ],
    )
    result = runner.invoke(cli, ["vault", "list"])
    assert result.exit_code == 0, result.output
    assert "Zhang via USB drop" in result.output


def test_cli_vault_list_warns_when_no_active(
    fake_home: Path, vault_a: Path
) -> None:
    """A registry with entries but no active emits the warning hint."""
    # Manually craft an inactive registry.
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = VaultRegistry(
        vaults=[v.model_copy(update={"is_active": False}) for v in reg.vaults]
    )
    save_registry(reg)

    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "list"])
    assert result.exit_code == 0, result.output
    assert "No active vault" in result.output


# ---------------------------------------------------------------------------
# lit vault info
# ---------------------------------------------------------------------------


def test_cli_vault_info_active_vault(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    result = runner.invoke(cli, ["vault", "info", "main"])
    assert result.exit_code == 0, result.output
    assert "Path:" in result.output
    assert "Active:" in result.output
    assert "yes" in result.output  # is_active=True
    assert "Papers:" in result.output
    assert "Total size:" in result.output


def test_cli_vault_info_inactive_vault(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    runner.invoke(cli, ["vault", "add", "second", str(vault_b)])
    result = runner.invoke(cli, ["vault", "info", "second"])
    assert result.exit_code == 0, result.output
    assert "Active:" in result.output
    assert "no" in result.output  # not active


def test_cli_vault_info_with_provenance(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "vault", "add", "zhang", str(vault_a),
            "--import-from", "Zhang via USB drop",
            "--import-at", "2026-01-15",
        ],
    )
    result = runner.invoke(cli, ["vault", "info", "zhang"])
    assert result.exit_code == 0, result.output
    assert "Zhang via USB drop" in result.output
    assert "2026-01-15" in result.output
    assert "Imported at:" in result.output


def test_cli_vault_info_missing_name_errors(fake_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "info", "ghost"])
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)


def test_cli_vault_info_stale_path_warns(
    fake_home: Path, vault_a: Path
) -> None:
    """Registered vault whose directory has disappeared shows a warning
    panel rather than crashing."""
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    import shutil
    shutil.rmtree(vault_a)

    result = runner.invoke(cli, ["vault", "info", "main"])
    # info should still succeed (exit 0), just emit a warning Panel.
    assert result.exit_code == 0, result.output
    assert "missing" in result.output.lower() or "directory" in result.output.lower()


# ---------------------------------------------------------------------------
# lit vault remove
# ---------------------------------------------------------------------------


def test_cli_vault_remove_with_yes_unregisters(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    result = runner.invoke(cli, ["vault", "remove", "main", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Unregistered" in result.output

    reg = load_registry()
    assert reg.vaults == []
    # Directory itself untouched.
    assert vault_a.is_dir()
    assert (vault_a / "lit-config.yaml").is_file()


def test_cli_vault_remove_aborts_on_n_prompt(
    fake_home: Path, vault_a: Path
) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    result = runner.invoke(cli, ["vault", "remove", "main"], input="n\n")
    assert result.exit_code != 0  # click.confirm(abort=True) on 'n'

    reg = load_registry()
    assert len(reg.vaults) == 1  # not removed


def test_cli_vault_remove_active_warns_in_prompt(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """Removing the active vault should emit a hint in the prompt."""
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    runner.invoke(cli, ["vault", "add", "second", str(vault_b)])
    # main is active; remove it.
    result = runner.invoke(
        cli, ["vault", "remove", "main", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "active vault was removed" in result.output.lower()

    reg = load_registry()
    assert find_active(reg) is None  # no auto-promote
    assert [v.name for v in reg.vaults] == ["second"]


def test_cli_vault_remove_missing_name_errors(fake_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "remove", "ghost", "--yes"])
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)


def test_cli_vault_remove_does_not_delete_directory(
    fake_home: Path, vault_a: Path
) -> None:
    """Re-emphasized: `remove` is registry-only, never deletes the directory."""
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])

    # Add a marker file we'd expect to survive.
    (vault_a / "MARKER").write_text("don't delete me", encoding="utf-8")
    runner.invoke(cli, ["vault", "remove", "main", "--yes"])
    assert (vault_a / "MARKER").is_file()
    assert (vault_a / "MARKER").read_text(encoding="utf-8") == "don't delete me"


# ---------------------------------------------------------------------------
# Group / help
# ---------------------------------------------------------------------------


def test_cli_vault_group_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "use" in result.output
    assert "list" in result.output
    assert "info" in result.output
    assert "remove" in result.output


# ---------------------------------------------------------------------------
# First-time registry prompt (ADR-005)
# ---------------------------------------------------------------------------


def test_first_time_prompt_silent_when_non_tty(
    fake_home: Path, vault_a: Path
) -> None:
    """CliRunner runs in non-TTY mode by default — prompt must be skipped
    so CI / scripts / docker init don't hang."""
    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    assert result.exit_code == 0
    # The hint panel should not have appeared.
    assert "First-time registry setup" not in result.output
    # The vault was registered successfully.
    assert find_by_name(load_registry(), "main") is not None


def test_first_time_prompt_silent_when_env_var_set(
    fake_home: Path,
    vault_a: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User who already set LITMAN_REGISTRY_DIR doesn't need the hint."""
    custom = tmp_path / "custom-registry"
    custom.mkdir()
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(custom))

    runner = CliRunner()
    result = runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    assert result.exit_code == 0
    assert "First-time registry setup" not in result.output
    # Registry was written under the custom dir, not under HOME.
    assert (custom / "vaults.yaml").is_file()


def test_first_time_prompt_skipped_after_registry_exists(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """Second `lit vault add` does NOT re-prompt — registry already exists."""
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault_a)])
    result = runner.invoke(cli, ["vault", "add", "second", str(vault_b)])
    assert result.exit_code == 0
    assert "First-time registry setup" not in result.output
