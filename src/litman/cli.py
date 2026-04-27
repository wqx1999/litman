"""litman CLI entry point.

This module defines the root Click group `cli` and the entry-point function
`main` referenced by ``[project.scripts]`` in ``pyproject.toml``.

Subcommands will be registered onto ``cli`` as M1.2+ adds them.
"""

from __future__ import annotations

import click
from rich.console import Console

from litman import __version__

console = Console()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="lit")
def cli() -> None:
    """litman — local-first, AI-augmented literature management CLI.

    Run ``lit COMMAND --help`` for command-specific help.
    """


@cli.command()
def hello() -> None:
    """Sanity-check command. Confirms `lit` is installed and importable."""
    console.print(
        f"[bold green]litman[/] v{__version__} is installed and importable.\n"
        "[dim]Placeholder command — real commands land starting M1.2.[/]"
    )


def main() -> None:
    """Entry point invoked by the ``lit`` console script."""
    cli()


if __name__ == "__main__":
    main()
