"""Tests for ``lit init`` and the underlying ``create_vault()``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import VAULT_SUBDIRS, create_vault
from litman.exceptions import ParentNotFoundError, VaultExistsError

# ---------------------------------------------------------------------------
# core/library.py — direct function tests
# ---------------------------------------------------------------------------


def test_create_vault_default_name(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    assert vault == (tmp_path / "literature_vault").resolve()
    assert vault.is_dir()
    for sub in VAULT_SUBDIRS:
        assert (vault / sub).is_dir(), f"missing subdirectory: {sub}"
    assert (vault / "TAXONOMY.md").is_file()
    assert (vault / "INDEX.json").is_file()
    assert (vault / "lit-config.yaml").is_file()
    # vault is deliberately NOT a git repo: cloud sync (M5) handles version
    # history and multi-file atomicity uses filesystem staging instead.
    assert not (vault / ".git").exists()


def test_create_vault_custom_name(tmp_path: Path) -> None:
    vault = create_vault(tmp_path, name="my_papers")
    assert vault.name == "my_papers"
    assert vault.is_dir()
    assert not (vault / ".git").exists()


def test_create_vault_creates_codes_and_staging(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    assert (vault / "codes").is_dir(), "codes/ not created"
    assert (vault / ".litman-staging").is_dir(), ".litman-staging/ not created"


def test_create_vault_has_no_top_level_notes_dir(tmp_path: Path) -> None:
    """ADR-008 / M16: the top-level notes/ dir is no longer part of the
    skeleton, but every other expected dir still is."""
    vault = create_vault(tmp_path)
    assert not (vault / "notes").exists(), "notes/ should not be created"
    for expected in (
        "papers",
        "codes",
        "inbox",
        "views/by-project",
        "views/by-topic",
        "views/by-method",
        "views/by-status",
        ".litman-staging",
    ):
        assert (vault / expected).is_dir(), f"missing skeleton dir: {expected}"


def test_create_vault_missing_parent(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent"
    with pytest.raises(ParentNotFoundError):
        create_vault(missing)


def test_create_vault_parent_is_file(tmp_path: Path) -> None:
    a_file = tmp_path / "afile"
    a_file.write_text("hi")
    with pytest.raises(ParentNotFoundError):
        create_vault(a_file)


def test_create_vault_existing_nonempty_target(tmp_path: Path) -> None:
    target = tmp_path / "literature_vault"
    target.mkdir()
    (target / "preexisting.txt").write_text("don't clobber me")
    with pytest.raises(VaultExistsError):
        create_vault(tmp_path)
    # Pre-existing file still intact.
    assert (target / "preexisting.txt").read_text() == "don't clobber me"


def test_create_vault_existing_empty_target_succeeds(tmp_path: Path) -> None:
    target = tmp_path / "literature_vault"
    target.mkdir()
    vault = create_vault(tmp_path)
    assert vault == target.resolve()
    assert (vault / "TAXONOMY.md").is_file()


def test_index_seed_is_valid_empty_json(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    content = (vault / "INDEX.json").read_text()
    payload = json.loads(content)
    assert "AUTO-GENERATED" in payload["_comment"]
    assert payload["n_papers"] == 0
    assert payload["papers"] == []
    assert "generated_at" in payload


def test_taxonomy_seed_has_fixed_enums(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    taxonomy = (vault / "TAXONOMY.md").read_text()
    # fixed enums are pre-populated
    assert "research" in taxonomy
    assert "deep-read" in taxonomy
    # user dictionaries start empty
    assert "## projects\n\n(empty)" in taxonomy


def test_lit_config_seed_is_valid_yaml(tmp_path: Path) -> None:
    vault = create_vault(tmp_path, name="custom_lib")
    yaml = YAML(typ="safe")
    config = yaml.load((vault / "lit-config.yaml").read_text())
    assert config["library_name"] == "custom_lib"
    # git_auto_commit was removed when the vault stopped being a git repo.
    assert "git_auto_commit" not in config
    assert "by-topic" in config["view_definitions"]
    assert "doi" in config["unique_keys"]


# ---------------------------------------------------------------------------
# CLI tests via click.testing.CliRunner
# ---------------------------------------------------------------------------


def test_lit_init_default_arg(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "literature_vault").is_dir()
    assert "Vault initialized" in result.output


def test_lit_init_custom_name_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["init", str(tmp_path), "--name", "my_papers"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "my_papers").is_dir()
    assert (tmp_path / "literature_vault").exists() is False


def test_lit_init_missing_parent_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    missing = tmp_path / "nope"
    result = runner.invoke(cli, ["init", str(missing)])
    assert result.exit_code != 0
    assert isinstance(result.exception, ParentNotFoundError)


def test_lit_init_existing_vault_refused(tmp_path: Path) -> None:
    runner = CliRunner()
    first = runner.invoke(cli, ["init", str(tmp_path)])
    assert first.exit_code == 0, first.output
    second = runner.invoke(cli, ["init", str(tmp_path)])
    assert second.exit_code != 0
    assert isinstance(second.exception, VaultExistsError)


def test_lit_init_help_lists_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output
