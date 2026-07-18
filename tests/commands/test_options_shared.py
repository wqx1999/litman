"""The --library / --vault pair is defined once and says nothing platform-specific.

Both flags were hand-copied into ~30 command modules, which is how the
--vault help came to name ``~/.config/litman/vaults.yaml`` — a path that is
only real on Linux. Windows is a supported platform now, so the help must
name no config path at all. These tests walk the whole command tree so a
future command cannot reintroduce either problem by copy-paste.
"""

from __future__ import annotations

import click

from litman.cli import cli
from litman.commands._options import LIBRARY_HELP, VAULT_HELP

# Substrings that would put a user on the wrong machine's filesystem.
_PLATFORM_SPECIFIC = (
    "~/.config",
    "%APPDATA%",
    "Library/Application Support",
    "vaults.yaml",
)


def _all_commands() -> list[tuple[str, click.Command]]:
    """Every leaf command in the tree, as (dotted path, command).

    Walks via ``list_commands`` / ``get_command`` (not the ``.commands`` dict):
    the root group loads its subcommands lazily, so the dict is sparse until a
    command is resolved — the public API resolves each on demand.
    """
    found: list[tuple[str, click.Command]] = []

    def walk(name: str, cmd: click.Command) -> None:
        if isinstance(cmd, click.Group):
            ctx = click.Context(cmd, info_name=name)
            for sub_name in cmd.list_commands(ctx):
                sub = cmd.get_command(ctx, sub_name)
                if sub is not None:
                    walk(f"{name} {sub_name}", sub)
        else:
            found.append((name, cmd))

    walk("lit", cli)
    return found


def _help_of(cmd: click.Command, flag: str) -> str | None:
    for param in cmd.params:
        if isinstance(param, click.Option) and flag in param.opts:
            return param.help
    return None


def test_every_vault_flag_uses_the_shared_help() -> None:
    seen = 0
    for path, cmd in _all_commands():
        help_text = _help_of(cmd, "--vault")
        if help_text is None:
            continue
        seen += 1
        assert help_text == VAULT_HELP, (
            f"{path} defines its own --vault help; use "
            f"@vault_option from litman.commands._options"
        )
    assert seen > 20, f"expected the whole command tree, only walked {seen}"


def test_every_library_flag_uses_the_shared_help() -> None:
    seen = 0
    for path, cmd in _all_commands():
        help_text = _help_of(cmd, "--library")
        if help_text is None:
            continue
        seen += 1
        assert help_text == LIBRARY_HELP, (
            f"{path} defines its own --library help; use "
            f"@library_option from litman.commands._options"
        )
    assert seen > 20, f"expected the whole command tree, only walked {seen}"


def test_no_flag_help_names_a_platform_specific_path() -> None:
    """A registry path is right on one OS and wrong on the other two."""
    for path, cmd in _all_commands():
        for param in cmd.params:
            help_text = getattr(param, "help", None) or ""
            for bad in _PLATFORM_SPECIFIC:
                assert bad not in help_text, (
                    f"{path} --{param.name} help names {bad!r}; the registry "
                    f"lives in a different place on each platform, so help "
                    f"text must point at a command, not a path"
                )
