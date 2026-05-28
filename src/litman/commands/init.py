"""``lit init`` — create a new literature vault under a parent directory.

The vault subdirectory is always created **by** the CLI; users never
``mkdir literature_vault`` themselves. The default subdir name is
``literature_vault`` (overridable via ``--name``). After creation the vault
is registered in litman's user-level registry (and made active when it is
the first vault), so subsequent commands find it with no environment
variable to set. ``--no-register`` opts out.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from litman.commands._registry_first_time import maybe_first_time_registry_prompt
from litman.core.library import DEFAULT_VAULT_NAME, create_vault
from litman.core.vault_registry import (
    add_vault,
    ensure_name_registrable,
    find_by_name,
    load_registry,
    save_registry,
)
from litman.exceptions import VaultRegistryError

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
@click.option(
    "--register-as",
    "register_as",
    default=None,
    help=(
        "Registry name for the new vault (default: the --name value). Use "
        "when the default name is already registered, e.g. a second vault."
    ),
)
@click.option(
    "--no-register",
    "no_register",
    is_flag=True,
    default=False,
    help=(
        "Create the vault but do NOT add it to the registry (for CI / "
        "scripts / throwaway vaults). You then point lit at it via "
        "--library / $LIT_LIBRARY or `lit vault add`."
    ),
)
def init_cmd(
    parent_dir: Path, name: str, register_as: str | None, no_register: bool
) -> None:
    """Initialize a new literature vault and register it as active.

    Creates PARENT_DIR/<name>/ with the standard skeleton (papers/, codes/,
    the four views/by-* hubs, a seeded TAXONOMY.md, an empty INDEX.json, and
    lit-config.yaml), then registers it in litman's
    user-level vault registry. The first vault you create becomes the active
    vault automatically, so subsequent commands (lit add / list / ...) find
    it with no environment variable to set. Pass --no-register to skip
    registration.

    The vault is deliberately NOT a git repository: version history is the
    job of cloud sync (lit sync push/pull), and multi-file atomicity is
    provided by an internal staging directory plus os.replace, not git.

    PARENT_DIR defaults to the current working directory.
    """
    register_name = register_as or name

    # Pre-flight: validate the registry name BEFORE creating anything, so a
    # name clash aborts cleanly without leaving an unregistered vault on disk.
    if not no_register:
        reg = load_registry()
        try:
            ensure_name_registrable(reg, register_name)
        except VaultRegistryError as e:
            raise click.ClickException(
                f"{e}\nNo vault was created. Re-run with "
                f"`--register-as <distinct-name>`, or `--no-register` to "
                f"create it without registering."
            ) from e
        # Fires only on the very first registry creation (TTY-gated). May
        # abort here, in which case nothing has been created yet.
        maybe_first_time_registry_prompt()

    vault = create_vault(parent_dir, name=name)

    if no_register:
        body = (
            f"[bold green]Vault initialized:[/] {vault}\n"
            f"[yellow]Not registered[/] (--no-register).\n\n"
            "Point lit at it with one of:\n"
            f"  [dim]•[/] export LIT_LIBRARY={vault}\n"
            f"  [dim]•[/] lit <cmd> --library {vault}\n"
            f"  [dim]•[/] lit vault add <name> {vault}\n\n"
            "Then: lit add <path-to-pdf> --doi <doi>"
        )
    else:
        updated = add_vault(reg, register_name, vault)
        save_registry(updated)
        entry = find_by_name(updated, register_name)
        assert entry is not None  # just added
        if entry.is_active:
            body = (
                f"[bold green]Vault initialized & registered:[/] {vault}\n"
                f"[bold]Registry name:[/] {register_name}  "
                f"[green](active)[/]\n\n"
                "lit will use this vault automatically — no environment "
                "variable needed.\n\n"
                "Next: lit add <path-to-pdf> --doi <doi>"
            )
        else:
            body = (
                f"[bold green]Vault initialized & registered:[/] {vault}\n"
                f"[bold]Registry name:[/] {register_name}  "
                f"[yellow](not active)[/]\n\n"
                f"Another vault is currently active. To switch:\n"
                f"  [dim]•[/] lit vault use {register_name}\n\n"
                "Next: lit add <path-to-pdf> --doi <doi>"
            )

    console.print(Panel.fit(body, title="lit init", border_style="green"))
