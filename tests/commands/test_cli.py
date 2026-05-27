"""Tests for the root CLI group's help layout (M27, Subtask C).

``LitGroup.format_commands`` renders commands in fixed workflow sections
instead of one flat alphabetical block, hides ``hello`` from the listing,
and falls back to an "Other" section for any uncategorized command. ``lit
help`` (the help_cmd alias) routes through the same ``get_help`` path, so
both ``lit --help`` and ``lit help`` must show the same sections.
"""

from __future__ import annotations

from click.testing import CliRunner

from litman.cli import cli

_SECTION_TITLES = (
    "Setup & vaults",
    "Papers",
    "Reading status",
    "Linking & organization",
    "Maintenance",
)


def test_help_shows_sections() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    for title in _SECTION_TITLES:
        assert title in result.output, f"missing section: {title}"


def test_help_hides_hello() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    # `hello` is hidden from the listing (whole-word check so it does not
    # match a substring of another command).
    assert "hello" not in result.output.split()
    # ...but the command still runs.
    ran = runner.invoke(cli, ["hello"])
    assert ran.exit_code == 0, ran.output


def test_help_has_no_uncategorized() -> None:
    # Every visible command is placed in a section, so no "Other" fallback
    # is rendered. This guard goes red if someone adds a command but forgets
    # to slot it into _COMMAND_SECTIONS.
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Other" not in result.output


def test_help_lists_setup() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    # `setup` appears under the first section, before the next section title.
    setup_section = result.output.split("Setup & vaults", 1)[1]
    setup_section = setup_section.split("Papers", 1)[0]
    assert "setup" in setup_section


def test_lit_help_command_matches() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["help"])
    assert result.exit_code == 0, result.output
    for title in _SECTION_TITLES:
        assert title in result.output, f"missing section in lit help: {title}"
