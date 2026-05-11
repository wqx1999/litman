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

import click
from rich.console import Console

from litman import __version__
from litman.commands.add import add_cmd
from litman.commands.code import code_group
from litman.commands.config import config_group
from litman.commands.health import health_check_cmd
from litman.commands.init import init_cmd
from litman.commands.list import list_cmd
from litman.commands.modify import modify_cmd
from litman.commands.refresh import refresh_views_cmd
from litman.commands.rename import rename_cmd
from litman.commands.rm import rm_cmd
from litman.commands.show import show_cmd
from litman.commands.taxonomy import taxonomy_group
from litman.commands.trash import trash_group
from litman.exceptions import LitmanError

console = Console()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="lit")
def cli() -> None:
    """litman — local-first, AI-augmented literature management CLI.

    Run ``lit COMMAND --help`` for command-specific help.
    """


cli.add_command(init_cmd)
cli.add_command(add_cmd)
cli.add_command(list_cmd)
cli.add_command(show_cmd)
cli.add_command(refresh_views_cmd)
cli.add_command(modify_cmd)
cli.add_command(taxonomy_group)
cli.add_command(rename_cmd)
cli.add_command(rm_cmd)
cli.add_command(trash_group)
cli.add_command(health_check_cmd)
cli.add_command(code_group)
cli.add_command(config_group)


@cli.command()
def hello() -> None:
    """Sanity-check command. Confirms `lit` is installed and importable."""
    console.print(
        f"[bold green]litman[/] v{__version__} is installed and importable."
    )


def main() -> None:
    """Entry point invoked by the ``lit`` console script."""
    try:
        cli()
    except LitmanError as e:
        console.print(f"[bold red]error:[/] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
