"""``lit init`` — create a new literature vault under a parent directory.

The vault subdirectory is always created **by** the CLI; users never
``mkdir literature_vault`` themselves. The default subdir name is
``literature_vault`` (overridable via ``--name``).
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from litman.core.library import DEFAULT_VAULT_NAME, create_vault

console = Console()


@click.command("init")
@click.argument(
    "parent_dir",
    type=click.Path(path_type=Path),
    default=".",
    metavar="[PARENT_DIR]",
)
@click.option(
    "--name",
    default=DEFAULT_VAULT_NAME,
    show_default=True,
    help="Vault subdirectory name to create under PARENT_DIR.",
)
def init_cmd(parent_dir: Path, name: str) -> None:
    """Initialize a new literature vault.

    Creates PARENT_DIR/<name>/ and populates it with the standard skeleton:
    papers/, inbox/, codes/, the four views/by-* symlink hubs
    (project / topic / method / status), a seeded TAXONOMY.md (controlled
    vocabulary), an empty INDEX.json (the agent-facing summary index), and
    a lit-config.yaml.

    The vault is deliberately NOT a git repository: version history is the
    job of cloud sync (lit sync push/pull), and multi-file atomicity is
    provided by an internal staging directory plus os.replace, not git.

    PARENT_DIR defaults to the current working directory.
    """
    vault = create_vault(parent_dir, name=name)

    console.print(
        Panel.fit(
            f"[bold green]Vault initialized:[/] {vault}\n\n"
            "Next steps:\n"
            f"  [dim]1.[/] export LIT_LIBRARY={vault}\n"
            "  [dim]2.[/] lit add <path-to-pdf> --doi <doi>   [dim](M1.3)[/]",
            title="lit init",
            border_style="green",
        )
    )
