"""Smoke tests: package importable, CLI dispatches correctly."""

from __future__ import annotations

from click.testing import CliRunner

import litman
from litman.cli import cli


def test_version_is_set() -> None:
    assert litman.__version__ == "0.1.0"


def test_cli_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "litman" in result.output.lower()


def test_cli_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_hello_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["hello"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output
    assert "installed" in result.output
