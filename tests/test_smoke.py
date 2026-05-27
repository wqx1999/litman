"""Smoke tests: package importable, CLI dispatches correctly."""

from __future__ import annotations

from click.testing import CliRunner

import litman
from litman.cli import cli


def test_version_is_set() -> None:
    # Read the package version dynamically so this test does not need to
    # be edited every time pyproject.toml bumps. We just assert the
    # shape (non-empty semver-ish string).
    assert isinstance(litman.__version__, str)
    assert litman.__version__.count(".") >= 1


def test_cli_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "litman" in result.output.lower()


def test_cli_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert litman.__version__ in result.output


def test_hello_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["hello"])
    assert result.exit_code == 0
    assert litman.__version__ in result.output
    assert "installed" in result.output


def test_help_command_no_args_lists_commands() -> None:
    # `lit help` is an alias for `lit --help`: prints the top-level command
    # list. M27 replaced the flat "Commands:" block with workflow sections,
    # so assert against a section title rather than the old generic header.
    runner = CliRunner()
    result = runner.invoke(cli, ["help"])
    assert result.exit_code == 0
    assert "Setup & vaults" in result.output
    assert "taxonomy" in result.output


def test_help_command_for_leaf() -> None:
    # prog_name mirrors the real `lit` console script (CliRunner would
    # otherwise default it to the group's function name).
    runner = CliRunner()
    result = runner.invoke(cli, ["help", "init"], prog_name="lit")
    assert result.exit_code == 0
    # Usage line is rendered with the resolved command path, not "lit help".
    assert "Usage: lit init" in result.output


def test_help_command_for_nested_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["help", "code", "add"], prog_name="lit")
    assert result.exit_code == 0
    assert "Usage: lit code add" in result.output


def test_help_command_unknown_errors() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["help", "nonexistent"])
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_help_command_leaf_rejects_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["help", "read", "read"])
    assert result.exit_code != 0
    assert "no subcommands" in result.output.lower()
