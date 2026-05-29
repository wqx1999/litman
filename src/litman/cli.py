"""litman CLI entry point.

Defines the root Click group ``cli`` and the entry-point function ``main``
referenced by ``[project.scripts]`` in ``pyproject.toml``.

Subcommands are registered onto ``cli`` via ``cli.add_command`` below.
``main`` wraps ``cli`` so that ``LitmanError`` subclasses become friendly
single-line error messages with exit code 1; other exceptions propagate as
normal Python tracebacks (they indicate bugs, not user errors).
"""

from __future__ import annotations

import sys
from typing import Any

import click
from rich.console import Console

from litman import __version__
from litman.commands.add import add_cmd
from litman.commands.code import code_group
from litman.commands.config import config_group
from litman.commands.drop import drop_cmd
from litman.commands.export import export_cmd
from litman.commands.health import health_check_cmd
from litman.commands.init import init_cmd
from litman.commands.install_completion import install_completion_cmd
from litman.commands.install_skill import install_skill_cmd
from litman.commands.link import link_cmd, unlink_cmd
from litman.commands.list import list_cmd
from litman.commands.modify import modify_cmd
from litman.commands.open import open_cmd
from litman.commands.project import project_group
from litman.commands.promote import promote_cmd
from litman.commands.read import read_cmd
from litman.commands.refresh import refresh_views_cmd
from litman.commands.rename import rename_cmd
from litman.commands.revisit import revisit_cmd
from litman.commands.rm import rm_cmd
from litman.commands.setup import setup_cmd
from litman.commands.show import show_cmd
from litman.commands.skim import skim_cmd
from litman.commands.sync import sync_group
from litman.commands.taxonomy import taxonomy_group
from litman.commands.trash import trash_group
from litman.commands.vault import vault_group
from litman.exceptions import LitmanError

console = Console()


# Workflow-ordered command groups for `lit --help` / `lit help`. Any command
# not listed here still appears under "Other" (so a newly added command is
# never silently hidden) — add it to the right section when you add it.
_COMMAND_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Setup & vaults",
     ("setup", "init", "vault", "install-completion", "install-skill", "help")),
    ("Papers",
     ("add", "list", "show", "open", "modify", "rename", "rm")),
    ("Reading status",
     ("read", "skim", "promote", "revisit", "drop")),
    ("Linking & organization",
     ("link", "unlink", "project", "code", "taxonomy")),
    ("Maintenance",
     ("health-check", "refresh-views", "trash", "sync", "export", "config")),
)


class LitGroup(click.Group):
    """Root group that (1) renders the command list in workflow sections
    instead of one flat alphabetical block (M27), and (2) surfaces vault
    registry drift before dispatching to most subcommands (M28). Falls back
    to an 'Other' section for any command missing from _COMMAND_SECTIONS so
    nothing is silently hidden."""

    # Subcommands that must NOT trigger the drift prompt:
    # - help / hello: trivial, registry-irrelevant; would feel out of place
    # - None: the user typed `lit` with no subcommand and is about to see
    #   the help message; don't ambush them with a registry prompt
    _DRIFT_SKIP: frozenset[str | None] = frozenset({"help", "hello", None})

    def invoke(self, ctx: click.Context) -> Any:
        # ``ctx.invoked_subcommand`` is only populated inside ``Group.invoke``
        # AFTER our override runs, so we peek at the raw protected args set
        # by ``Group.parse_args`` to know which subcommand is about to run.
        # An empty protected-args list means ``lit`` with no subcommand
        # (e.g. about to render help) — represented as ``None`` in the skip
        # set, matching the convention used by ``ctx.invoked_subcommand``.
        #
        # ``ctx._protected_args`` is a Click-8.x internal carrying the same
        # value, populated by ``parse_args``. Click 9 plans to remove this
        # attribute (deprecation flagged in 8.2), so ``pyproject.toml`` pins
        # ``click<9``. Bumping past 9 requires either moving this hook into
        # the group callback (after ``parse_args`` has populated
        # ``invoked_subcommand``) or switching to a public-API replacement.
        protected = getattr(ctx, "_protected_args", None) or []
        cmd_name: str | None = protected[0] if protected else None
        if cmd_name not in self._DRIFT_SKIP:
            # Local import keeps cli.py's import graph shallow at module load
            # (commands/_drift.py pulls in vault_registry + rich), and avoids
            # a circular import if _drift ever needs to reference cli.
            from litman.commands._drift import (
                check_and_prompt_project_drift,
                check_and_prompt_registry_drift,
            )

            check_and_prompt_registry_drift()
            # Project-path drift heals via staged_write + rebuild, which touch
            # the filesystem from inside the pre-dispatch hook. A failure there
            # must never crash the user's actual command — degrade to silent
            # skip and let the cold-path `lit health-check` catch it later.
            try:
                check_and_prompt_project_drift()
            except Exception:
                pass
        return super().invoke(ctx)

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        visible: dict[str, click.Command] = {}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            visible[name] = cmd
        if not visible:
            return
        limit = formatter.width - 6 - max(len(n) for n in visible)

        def rows(names: list[str]) -> list[tuple[str, str]]:
            return [(n, visible[n].get_short_help_str(limit)) for n in names]

        placed: set[str] = set()
        for title, names in _COMMAND_SECTIONS:
            present = [n for n in names if n in visible]
            if not present:
                continue
            with formatter.section(title):
                formatter.write_dl(rows(present))
            placed.update(present)

        leftover = [n for n in visible if n not in placed]
        if leftover:
            with formatter.section("Other"):
                formatter.write_dl(rows(leftover))


@click.group(
    cls=LitGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(version=__version__, prog_name="lit")
def cli() -> None:
    """litman — local-first, AI-augmented literature management CLI.

    Run lit help COMMAND (or lit COMMAND --help) for command-specific
    help, e.g. lit help code add.
    """


cli.add_command(setup_cmd)
cli.add_command(init_cmd)
cli.add_command(add_cmd)
cli.add_command(list_cmd)
cli.add_command(show_cmd)
cli.add_command(open_cmd)
cli.add_command(refresh_views_cmd)
cli.add_command(modify_cmd)
cli.add_command(read_cmd)
cli.add_command(revisit_cmd)
cli.add_command(drop_cmd)
cli.add_command(promote_cmd)
cli.add_command(skim_cmd)
cli.add_command(taxonomy_group)
cli.add_command(project_group)
cli.add_command(rename_cmd)
cli.add_command(rm_cmd)
cli.add_command(trash_group)
cli.add_command(health_check_cmd)
cli.add_command(code_group)
cli.add_command(config_group)
cli.add_command(install_skill_cmd)
cli.add_command(install_completion_cmd)
cli.add_command(link_cmd)
cli.add_command(unlink_cmd)
cli.add_command(sync_group)
cli.add_command(vault_group)
cli.add_command(export_cmd)


@cli.command(hidden=True)
def hello() -> None:
    """Sanity-check command. Confirms lit is installed and importable."""
    console.print(
        f"[bold green]litman[/] v{__version__} is installed and importable."
    )


@cli.command("help")
@click.argument("command_path", nargs=-1)
@click.pass_context
def help_cmd(ctx: click.Context, command_path: tuple[str, ...]) -> None:
    """Show help for lit or a specific command.

    "lit help" prints the top-level command list (same as "lit --help").
    "lit help COMMAND [SUBCOMMAND ...]" prints that command's help (same as
    "lit COMMAND ... --help"), e.g. "lit help code add".
    """
    # ctx.parent is the root `cli` group's context (info_name == "lit").
    root_ctx = ctx.parent
    if not command_path:
        click.echo(cli.get_help(root_ctx))
        return

    current_cmd: click.Command = cli
    current_ctx = root_ctx
    walked: list[str] = []
    for name in command_path:
        if not isinstance(current_cmd, click.Group):
            raise click.UsageError(
                f"'lit {' '.join(walked)}' takes no subcommands, "
                f"so '{name}' has no help."
            )
        sub = current_cmd.get_command(current_ctx, name)
        if sub is None:
            raise click.UsageError(f"No such command: {' '.join(command_path)!r}")
        current_ctx = click.Context(sub, info_name=name, parent=current_ctx)
        current_cmd = sub
        walked.append(name)
    click.echo(current_cmd.get_help(current_ctx))


def main() -> None:
    """Entry point invoked by the ``lit`` console script."""
    try:
        cli()
    except LitmanError as e:
        console.print(f"[bold red]error:[/] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
