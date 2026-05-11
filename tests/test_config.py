"""Tests for ``lit-config.yaml`` schema, loader, and the ``lit config`` group (M2.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.config import (
    CONFIG_FILENAME,
    DEFAULT_CLONE_DEPTH,
    DEFAULT_CODES_IGNORE_PATTERNS,
    DEFAULT_PDF_VIEWER,
    DEFAULT_UNIQUE_KEYS,
    DEFAULT_VIEW_DEFINITIONS,
    LitConfig,
    config_to_yaml_dict,
    load_config,
)
from litman.core.library import create_vault
from litman.exceptions import ConfigError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault built by ``create_vault`` — the seed config is on disk."""
    return create_vault(tmp_path)


def _overwrite_config(vault: Path, body: str) -> None:
    (vault / CONFIG_FILENAME).write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# LitConfig schema
# ---------------------------------------------------------------------------


def test_litconfig_requires_library_name() -> None:
    with pytest.raises(Exception):
        LitConfig.model_validate({})


def test_litconfig_minimal_uses_defaults() -> None:
    cfg = LitConfig.model_validate({"library_name": "bare"})
    assert cfg.library_name == "bare"
    assert cfg.default_pdf_viewer == DEFAULT_PDF_VIEWER
    assert cfg.view_definitions == list(DEFAULT_VIEW_DEFINITIONS)
    assert cfg.unique_keys == list(DEFAULT_UNIQUE_KEYS)
    assert cfg.default_clone_depth == DEFAULT_CLONE_DEPTH
    assert cfg.codes_ignore_patterns == list(DEFAULT_CODES_IGNORE_PATTERNS)


def test_litconfig_full_override() -> None:
    cfg = LitConfig.model_validate({
        "library_name": "custom",
        "default_pdf_viewer": "okular",
        "view_definitions": ["by-status"],
        "unique_keys": ["doi"],
        "default_clone_depth": 0,
        "codes_ignore_patterns": ["repo/", "data/"],
    })
    assert cfg.library_name == "custom"
    assert cfg.default_pdf_viewer == "okular"
    assert cfg.view_definitions == ["by-status"]
    assert cfg.unique_keys == ["doi"]
    assert cfg.default_clone_depth == 0
    assert cfg.codes_ignore_patterns == ["repo/", "data/"]


def test_litconfig_rejects_unknown_key() -> None:
    """`extra='forbid'` catches typos so they don't silently fall through."""
    with pytest.raises(Exception):
        LitConfig.model_validate({
            "library_name": "x",
            "default_pdf_viewr": "code",  # typo of default_pdf_viewer
        })


def test_litconfig_rejects_bad_type() -> None:
    with pytest.raises(Exception):
        LitConfig.model_validate({
            "library_name": "x",
            "default_clone_depth": "not-an-int",
        })


def test_litconfig_rejects_negative_clone_depth() -> None:
    """Depth must be >= 0 — negative makes no sense for git clone."""
    with pytest.raises(Exception):
        LitConfig.model_validate({
            "library_name": "x",
            "default_clone_depth": -1,
        })


def test_litconfig_is_frozen() -> None:
    """`frozen=True` prevents accidental mutation of a loaded config."""
    cfg = LitConfig.model_validate({"library_name": "x"})
    with pytest.raises(Exception):
        cfg.default_clone_depth = 5  # type: ignore[misc]


def test_config_to_yaml_dict_round_trip() -> None:
    """model_dump → yaml-dict → model_validate gives the same object."""
    cfg = LitConfig.model_validate({"library_name": "x", "default_clone_depth": 7})
    dumped = config_to_yaml_dict(cfg)
    restored = LitConfig.model_validate(dumped)
    assert restored == cfg


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_from_fresh_vault(vault: Path) -> None:
    """`create_vault` writes a seed; the loader must parse it cleanly."""
    cfg = load_config(vault)
    assert cfg.library_name == vault.name
    assert cfg.default_clone_depth == DEFAULT_CLONE_DEPTH
    assert cfg.codes_ignore_patterns == list(DEFAULT_CODES_IGNORE_PATTERNS)


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    """A directory with no lit-config.yaml is not a vault."""
    with pytest.raises(ConfigError, match="No lit-config.yaml"):
        load_config(tmp_path)


def test_load_config_malformed_yaml_raises(vault: Path) -> None:
    _overwrite_config(vault, "not: : valid: yaml: at all: [")
    with pytest.raises(ConfigError, match="Failed to parse"):
        load_config(vault)


def test_load_config_non_mapping_raises(vault: Path) -> None:
    _overwrite_config(vault, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="YAML mapping"):
        load_config(vault)


def test_load_config_unknown_key_raises(vault: Path) -> None:
    _overwrite_config(vault, "library_name: x\ndefault_pdf_viewr: code\n")
    with pytest.raises(ConfigError, match="default_pdf_viewr"):
        load_config(vault)


def test_load_config_bad_type_raises(vault: Path) -> None:
    _overwrite_config(vault, "library_name: x\ndefault_clone_depth: hello\n")
    with pytest.raises(ConfigError, match="default_clone_depth"):
        load_config(vault)


def test_load_config_missing_required_raises(vault: Path) -> None:
    _overwrite_config(vault, "default_pdf_viewer: code\n")
    with pytest.raises(ConfigError, match="library_name"):
        load_config(vault)


def test_load_config_empty_file_surfaces_required(vault: Path) -> None:
    """Empty file → empty mapping → library_name validation fires."""
    _overwrite_config(vault, "")
    with pytest.raises(ConfigError, match="library_name"):
        load_config(vault)


def test_load_config_omitted_fields_take_defaults(vault: Path) -> None:
    """A vault whose yaml predates the M3 fields still loads."""
    _overwrite_config(vault, "library_name: legacy_vault\n")
    cfg = load_config(vault)
    assert cfg.library_name == "legacy_vault"
    assert cfg.default_clone_depth == DEFAULT_CLONE_DEPTH
    assert cfg.codes_ignore_patterns == list(DEFAULT_CODES_IGNORE_PATTERNS)
    assert cfg.view_definitions == list(DEFAULT_VIEW_DEFINITIONS)


# ---------------------------------------------------------------------------
# CLI: lit config show
# ---------------------------------------------------------------------------


def test_cli_config_show_table_default(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["config", "show", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Field names appear as table rows.
    assert "library_name" in result.output
    assert "default_clone_depth" in result.output
    assert vault.name in result.output


def test_cli_config_show_yaml_format(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["config", "show", "--format", "yaml", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "library_name:" in result.output
    assert "default_clone_depth:" in result.output


def test_cli_config_show_reflects_edits(vault: Path) -> None:
    _overwrite_config(vault, "library_name: edited\ndefault_clone_depth: 5\n")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["config", "show", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "edited" in result.output
    assert "5" in result.output


def test_cli_config_show_validation_error_surfaces(vault: Path) -> None:
    """ConfigError propagates out of `cli` — the top-level `main()` wrapper
    is what formats it as a friendly message, but `CliRunner` invokes the
    Click group directly, so we assert the exception type instead of stderr.
    """
    _overwrite_config(vault, "library_name: x\ndefault_clone_depth: -3\n")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["config", "show", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
    assert "default_clone_depth" in str(result.exception)


def test_cli_config_show_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["config", "show", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.output
    assert "yaml" in result.output


def test_cli_config_group_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["config", "--help"])
    assert result.exit_code == 0
    assert "show" in result.output


# ---------------------------------------------------------------------------
# Integration: config drives `lit code add` / `lit code restore-all` --depth
# ---------------------------------------------------------------------------


def test_code_add_uses_config_default_clone_depth(
    vault: Path, tmp_path: Path
) -> None:
    """Setting `default_clone_depth: 0` in config -> code add does a full clone."""
    import subprocess

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", str(upstream)], check=True)
    (upstream / "README.md").write_text("# u\n")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(upstream),
         "-c", "user.email=t@x.com", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        check=True,
    )

    # Override config to full-clone default. Depth via CLI is NOT passed.
    _overwrite_config(
        vault,
        f"library_name: {vault.name}\ndefault_clone_depth: 0\n",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "add", str(upstream), "--name", "Full",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    # depth=0 -> the human-readable banner says "full history"
    assert "full history" in result.output


def test_code_add_cli_depth_overrides_config(
    vault: Path, tmp_path: Path
) -> None:
    """Explicit --depth on the command line wins over config default."""
    import subprocess

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", str(upstream)], check=True)
    (upstream / "README.md").write_text("# u\n")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(upstream),
         "-c", "user.email=t@x.com", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        check=True,
    )

    _overwrite_config(
        vault,
        f"library_name: {vault.name}\ndefault_clone_depth: 0\n",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["code", "add", str(upstream), "--name", "Shallow",
         "--depth", "1", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "depth 1" in result.output
