"""``lit config`` command group (M2.2).

A thin window onto ``lit-config.yaml``: ``lit config show`` prints the
parsed + validated config (so the user can confirm what the CLI actually
sees, vs. what they think they wrote). ``set`` is intentionally NOT
provided yet — yaml comments would be clobbered by a naive rewrite, and
hand-editing the file is fine for the small number of fields involved.
"""

from __future__ import annotations

import io
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from litman.core.config import (
    CONFIG_FILENAME,
    LitConfig,
    config_to_yaml_dict,
    load_config,
)
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import ConfigError

console = Console()

# ruamel writer configured to match the seed style.
_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    default_flow_style=False,
)


@click.group("config")
def config_group() -> None:
    """Inspect the vault's lit-config.yaml.

    The config file controls library-level preferences (default PDF
    viewer, view set, default clone depth, etc.). It is read by every
    command that needs a config-driven default — pass --<flag> on the
    command line to override per-invocation.
    """


@config_group.command("show")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "yaml"]),
    default="table",
    show_default=True,
    help="Render as a Rich table (default) or the canonical YAML form.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def config_show_cmd(
    fmt: str, library: Path | None, vault_name: str | None
) -> None:
    """Print the parsed, validated config for the active vault.

    Reflects the *effective* values after schema defaults fill in any
    fields the on-disk yaml omits. Useful for confirming a hand edit took
    effect, or for inspecting an inherited vault on a new machine.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    config = load_config(vault)

    if fmt == "yaml":
        buf = io.StringIO()
        _yaml.dump(config_to_yaml_dict(config), buf)
        console.print(buf.getvalue().rstrip())
        return

    table = Table(
        title=f"{CONFIG_FILENAME} ({vault})",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Field", style="bold")
    table.add_column("Value", overflow="fold")
    for name, value in config_to_yaml_dict(config).items():
        if isinstance(value, list):
            rendered = (
                "[dim](empty)[/]" if not value
                else "\n".join(f"- {escape(str(v))}" for v in value)
            )
        else:
            rendered = escape(str(value))
        table.add_row(escape(name), rendered)
    console.print(table)
