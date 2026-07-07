"""Tests for ``lit init`` and the underlying ``create_vault()``."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import VAULT_SUBDIRS, create_vault
from litman.core.vault_registry import add_vault, load_registry, save_registry
from litman.exceptions import ParentNotFoundError, VaultExistsError


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME + clear cross-cutting env vars so every test in this
    module reads/writes a tmp-path-rooted registry instead of the real
    ``~/.config/litman/vaults.yaml``.

    Autouse because ``lit init`` now registers vaults — without isolation,
    even the plain CLI tests would scribble over wangq's real registry.
    Mirrors the ``fake_home`` fixture in test_vault.py / test_vault_registry.py.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


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


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_create_vault_locks_taxonomy_readonly(tmp_path: Path) -> None:
    """The seeded TAXONOMY.md is read-only; lit-config.yaml / INDEX.json are not (AC#1)."""
    vault = create_vault(tmp_path)
    assert not os.access(vault / "TAXONOMY.md", os.W_OK)
    assert os.access(vault / "lit-config.yaml", os.W_OK)
    assert os.access(vault / "INDEX.json", os.W_OK)


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_init_cli_locks_taxonomy_readonly(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    vault = tmp_path / "literature_vault"
    assert not os.access(vault / "TAXONOMY.md", os.W_OK)


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
        "views/by-project",
        "views/by-topic",
        "views/by-method",
        "views/by-status",
        ".litman-staging",
    ):
        assert (vault / expected).is_dir(), f"missing skeleton dir: {expected}"
    # inbox/ used to be a vault skeleton dir but was never used by any command;
    # the "inbox" concept is now the default value of metadata.yaml's status
    # field. The folder is retired.
    assert not (vault / "inbox").exists(), "inbox/ should not be created"


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
    # --no-register on both runs skips the registry name pre-flight, so the
    # second invocation reaches create_vault and raises VaultExistsError (the
    # behavior under test). With registration on, a duplicate registry name
    # would short-circuit earlier; that path is covered by
    # test_init_duplicate_name_aborts_without_creating_vault below.
    runner = CliRunner()
    first = runner.invoke(cli, ["init", str(tmp_path), "--no-register"])
    assert first.exit_code == 0, first.output
    second = runner.invoke(cli, ["init", str(tmp_path), "--no-register"])
    assert second.exit_code != 0
    assert isinstance(second.exception, VaultExistsError)


def test_lit_init_help_lists_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output


# ---------------------------------------------------------------------------
# M26 — registry-first auto-register
# ---------------------------------------------------------------------------


def test_init_registers_and_activates_first_vault(tmp_path: Path) -> None:
    """First `lit init` into an empty registry registers + auto-activates."""
    parent = tmp_path / "parent"
    parent.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(parent)])
    assert result.exit_code == 0, result.output

    reg = load_registry()
    assert len(reg.vaults) == 1
    entry = reg.vaults[0]
    assert entry.name == "literature_vault"
    assert entry.is_active is True
    assert "registered" in result.output.lower()
    assert "active" in result.output.lower()


def test_init_second_vault_not_active(tmp_path: Path) -> None:
    """A second `lit init` registers but does not preempt the active vault."""
    parent1 = tmp_path / "p1"
    parent1.mkdir()
    parent2 = tmp_path / "p2"
    parent2.mkdir()
    runner = CliRunner()

    first = runner.invoke(cli, ["init", str(parent1)])
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        cli, ["init", str(parent2), "--register-as", "fork1"]
    )
    assert second.exit_code == 0, second.output

    reg = load_registry()
    by_name = {v.name: v for v in reg.vaults}
    assert by_name["fork1"].is_active is False
    assert by_name["literature_vault"].is_active is True
    assert "lit vault use fork1" in second.output


def test_init_duplicate_name_aborts_without_creating_vault(
    tmp_path: Path,
) -> None:
    """A duplicate registry name fails fast, leaving no orphan vault dir."""
    parent1 = tmp_path / "p1"
    parent1.mkdir()
    parent2 = tmp_path / "p2"
    parent2.mkdir()
    runner = CliRunner()

    first = runner.invoke(cli, ["init", str(parent1)])
    assert first.exit_code == 0, first.output

    # Second init under a different parent, but default name collides.
    second = runner.invoke(cli, ["init", str(parent2)])
    assert second.exit_code != 0
    assert not (parent2 / "literature_vault").exists()
    assert "--register-as" in second.output


def test_init_register_as_overrides_name(tmp_path: Path) -> None:
    """--register-as sets the registry name; the dir name stays default."""
    parent = tmp_path / "parent"
    parent.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        cli, ["init", str(parent), "--register-as", "main"]
    )
    assert result.exit_code == 0, result.output

    reg = load_registry()
    assert len(reg.vaults) == 1
    assert reg.vaults[0].name == "main"
    assert (parent / "literature_vault").is_dir()


def test_init_no_register_skips_registry(tmp_path: Path) -> None:
    """--no-register creates the vault but writes nothing to the registry."""
    parent = tmp_path / "parent"
    parent.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(parent), "--no-register"])
    assert result.exit_code == 0, result.output
    assert (parent / "literature_vault").is_dir()
    assert load_registry().vaults == []
    assert "Not registered" in result.output


def test_init_case_fold_collision_aborts(tmp_path: Path) -> None:
    """A name differing only in case from an existing entry fails fast."""
    parent1 = tmp_path / "p1"
    parent1.mkdir()
    parent2 = tmp_path / "p2"
    parent2.mkdir()

    # Seed the registry with `Main` directly via the data layer.
    existing = create_vault(parent1, name="Main")
    save_registry(add_vault(load_registry(), "Main", existing))

    runner = CliRunner()
    result = runner.invoke(
        cli, ["init", str(parent2), "--register-as", "main"]
    )
    assert result.exit_code != 0
    assert "case" in result.output.lower()
    assert not (parent2 / "literature_vault").exists()


def test_init_output_has_no_export_lit_library(tmp_path: Path) -> None:
    """Regression: the default init panel must not teach `export LIT_LIBRARY`."""
    parent = tmp_path / "parent"
    parent.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["init", str(parent)])
    assert result.exit_code == 0, result.output
    assert "export LIT_LIBRARY" not in result.output


# ---------------------------------------------------------------------------
# apply_init — the create+register core shared with POST /api/vaults/create
# ---------------------------------------------------------------------------


def test_apply_init_creates_registers_and_marks_health(tmp_path: Path) -> None:
    """First vault → created on disk, registered active, health-checked (not stale)."""
    from litman.commands.init import apply_init
    from litman.core.vault_registry import find_by_name

    parent = tmp_path / "p"
    parent.mkdir()
    vault, entry = apply_init(parent, "mylib")

    assert vault == parent / "mylib"
    assert (vault / "lit-config.yaml").is_file()
    assert entry.is_active
    assert entry.last_health_check_at is not None
    persisted = find_by_name(load_registry(), "mylib")
    assert persisted is not None
    assert Path(persisted.path).resolve() == vault.resolve()


def test_apply_init_name_clash_raises_before_creating(tmp_path: Path) -> None:
    """A registry-name clash raises before anything lands on disk."""
    from litman.commands.init import apply_init
    from litman.exceptions import VaultRegistryError

    parent = tmp_path / "p"
    parent.mkdir()
    existing = create_vault(parent, name="taken")
    save_registry(add_vault(load_registry(), "taken", existing))

    with pytest.raises(VaultRegistryError):
        apply_init(parent, "taken")
    # No half-built second vault under a different subdir name was attempted,
    # and the clashing name was never re-created.
    assert not (parent / "taken2").exists()
