"""Tests for the M8.3 ``--vault`` transparent layer on existing commands.

M8.3 adds ``--vault NAME`` to every command that already accepted
``--library``. This file verifies the plumbing on a representative sample
of commands (we don't re-test each command's whole surface — those tests
live in their own file — only that the ``--vault`` translation reaches
``find_vault`` correctly and that ``--library`` / ``--vault`` mutual
exclusion fires).

Covered commands here:
- ``lit list``       — read-only, no state mutation
- ``lit show``       — single-paper read
- ``lit health-check`` — runs core check pipeline against the vault
- ``lit config show``  — pure config read
- ``lit refresh-views`` — write side-effects (INDEX.json, views/)

We rely on the registry being in a temp ``$HOME`` so registering test
vaults never touches the user's real registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    save_registry,
)
from litman.core.vault_registry import VaultRegistry
from litman.exceptions import VaultRegistryError

_yaml = YAML()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Drop LIT_LIBRARY so click doesn't pick up an inherited value during tests.
    monkeypatch.delenv("LIT_LIBRARY", raising=False)
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


@pytest.fixture
def registry_with_two_vaults(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> tuple[Path, Path]:
    """Register both vaults; ``main`` is active by default (first added)."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    save_registry(reg)
    return vault_a, vault_b


def _seed_paper(vault: Path, paper_id: str, title: str = "Test") -> None:
    """Materialize a minimal paper folder so list / show / health-check have data."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": title,
        "year": 2024,
        "status": "inbox",
        "priority": "B",
        "type": "research",
        "doi": f"10.fake/{paper_id}",
        "projects": [],
        "topics": [],
        "methods": [],
        "data": [],
        "authors": ["Test, A."],
        "created-at": "2026-05-12T10:00:00+02:00",
        "updated-at": "2026-05-12T10:00:00+02:00",
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    (paper_dir / "notes.md").write_text("# notes\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# lit list --vault
# ---------------------------------------------------------------------------


def test_cli_list_with_vault_resolves_correct_vault(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    vault_a, vault_b = registry_with_two_vaults
    _seed_paper(vault_a, "PAPER_IN_MAIN", title="Main vault paper")
    _seed_paper(vault_b, "PAPER_IN_SECOND", title="Second vault paper")

    runner = CliRunner()
    res_main = runner.invoke(cli, ["list", "--vault", "main"])
    res_second = runner.invoke(cli, ["list", "--vault", "second"])

    assert res_main.exit_code == 0, res_main.output
    assert "PAPER_IN_MAIN" in res_main.output
    assert "PAPER_IN_SECOND" not in res_main.output

    assert res_second.exit_code == 0, res_second.output
    assert "PAPER_IN_SECOND" in res_second.output
    assert "PAPER_IN_MAIN" not in res_second.output


def test_cli_list_unknown_vault_errors(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--vault", "ghost"])
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)
    assert "No vault named" in str(result.exception)


def test_cli_list_vault_and_library_mutually_exclusive(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    vault_a, _ = registry_with_two_vaults
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["list", "--vault", "main", "--library", str(vault_a)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)
    assert "mutually exclusive" in str(result.exception)


def test_cli_list_without_vault_uses_active_default(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    """No --vault and no --library → discovery chain falls back to active vault."""
    vault_a, vault_b = registry_with_two_vaults
    _seed_paper(vault_a, "ACTIVE_DEFAULT")
    _seed_paper(vault_b, "NOT_ACTIVE")

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])  # no flags at all
    assert result.exit_code == 0, result.output
    assert "ACTIVE_DEFAULT" in result.output
    assert "NOT_ACTIVE" not in result.output


# ---------------------------------------------------------------------------
# lit show --vault
# ---------------------------------------------------------------------------


def test_cli_show_with_vault_targets_the_right_paper(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    vault_a, vault_b = registry_with_two_vaults
    _seed_paper(vault_a, "MAIN_PAPER", title="Main")
    _seed_paper(vault_b, "SECOND_PAPER", title="Second")

    runner = CliRunner()
    res = runner.invoke(cli, ["show", "SECOND_PAPER", "--vault", "second"])
    assert res.exit_code == 0, res.output
    assert "SECOND_PAPER" in res.output


def test_cli_show_paper_missing_in_chosen_vault(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    """show with --vault main but paper is only in 'second' → not found."""
    vault_a, vault_b = registry_with_two_vaults
    _seed_paper(vault_b, "ONLY_IN_SECOND")
    runner = CliRunner()
    res = runner.invoke(cli, ["show", "ONLY_IN_SECOND", "--vault", "main"])
    assert res.exit_code != 0  # PaperNotFoundError


# ---------------------------------------------------------------------------
# lit health-check --vault
# ---------------------------------------------------------------------------


def test_cli_health_check_with_vault_runs_against_chosen(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    vault_a, _ = registry_with_two_vaults
    _seed_paper(vault_a, "PAPER1")
    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--vault", "main"])
    # Empty / freshly-seeded vault should be clean.
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# lit config show --vault
# ---------------------------------------------------------------------------


def test_cli_config_show_with_vault(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["config", "show", "--vault", "second"])
    assert result.exit_code == 0, result.output
    # Each vault's lit-config.yaml carries its own library_name (the vault dir
    # name passed to `create_vault`). Confirm we read the SECOND vault, not main.
    assert "vault_b" in result.output


# ---------------------------------------------------------------------------
# lit refresh-views --vault
# ---------------------------------------------------------------------------


def test_cli_refresh_views_with_vault(
    registry_with_two_vaults: tuple[Path, Path],
) -> None:
    vault_a, vault_b = registry_with_two_vaults
    _seed_paper(vault_a, "MAIN_PAPER")
    _seed_paper(vault_b, "SECOND_PAPER")

    runner = CliRunner()
    result = runner.invoke(cli, ["refresh-views", "--vault", "second"])
    assert result.exit_code == 0, result.output
    # The view symlink should exist in vault_b, NOT in vault_a, because we
    # explicitly pointed refresh-views at "second".
    assert any((vault_b / "views").rglob("SECOND_PAPER"))


# ---------------------------------------------------------------------------
# Mutual exclusion fires on more than just `list`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["show", "ANY_ID"],
        ["health-check"],
        ["config", "show"],
        ["refresh-views"],
        ["taxonomy", "list"],
        ["code", "list"],
        ["trash", "list"],
    ],
)
def test_cli_vault_library_mutual_exclusion_on_various_commands(
    registry_with_two_vaults: tuple[Path, Path],
    argv: list[str],
) -> None:
    vault_a, _ = registry_with_two_vaults
    runner = CliRunner()
    result = runner.invoke(
        cli, [*argv, "--vault", "main", "--library", str(vault_a)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, VaultRegistryError)
    assert "mutually exclusive" in str(result.exception)


# ---------------------------------------------------------------------------
# --vault appears in --help for every patched command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "--help"],
        ["show", "--help"],
        ["add", "--help"],
        ["modify", "--help"],
        ["rename", "--help"],
        ["rm", "--help"],
        ["health-check", "--help"],
        ["refresh-views", "--help"],
        ["link", "--help"],
        ["unlink", "--help"],
        ["taxonomy", "list", "--help"],
        ["taxonomy", "add", "--help"],
        ["taxonomy", "rename", "--help"],
        ["taxonomy", "merge", "--help"],
        ["taxonomy", "rm", "--help"],
        ["code", "add", "--help"],
        ["code", "list", "--help"],
        ["code", "link", "--help"],
        ["code", "update", "--help"],
        ["code", "rm", "--help"],
        ["code", "restore-all", "--help"],
        ["trash", "list", "--help"],
        ["trash", "restore", "--help"],
        ["trash", "empty", "--help"],
        ["config", "show", "--help"],
        ["sync", "setup", "--help"],
        ["sync", "push", "--help"],
        ["sync", "pull", "--help"],
        ["sync", "status", "--help"],
    ],
)
def test_cli_help_lists_vault_option(argv: list[str]) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, argv)
    assert result.exit_code == 0
    assert "--vault" in result.output, (
        f"--vault missing from help for: {' '.join(argv)}"
    )
