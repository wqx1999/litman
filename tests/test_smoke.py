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
