"""The ``--library`` / ``--vault`` option pair, defined once.

Every command that reads or writes a vault takes the same two flags, and
they must keep the same shape everywhere: ``--library`` is a path,
``--vault`` is a registered name, and the two are mutually exclusive
(``core.library.resolve_library_or_vault`` enforces that).  Both flags
were copied by hand into ~30 command modules, which is how the ``--vault``
help came to name a Linux-only path.

Applying both, in the order every command uses::

    @some_group.command("thing")
    @library_option
    @vault_option
    def thing_cmd(library: Path | None, vault_name: str | None) -> None:
        vault = find_vault(resolve_library_or_vault(library, vault_name))
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

# Deliberately names no config path. The registry lives in the platform
# config directory, which is ~/.config/litman on Linux but not on macOS or
# Windows, and a user reaches it through `lit vault` either way.
VAULT_HELP = (
    "A vault name registered with 'lit vault add'. "
    "Mutually exclusive with --library."
)

LIBRARY_HELP = (
    "Override the active vault. Discovery order: this flag / $LIT_LIBRARY, "
    "then the active registered vault, then cwd-walk."
)


def library_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Add ``--library <path>`` (parameter ``library``)."""
    return click.option(
        "--library",
        type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
        default=None,
        envvar="LIT_LIBRARY",
        help=LIBRARY_HELP,
    )(fn)


def vault_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Add ``--vault <name>`` (parameter ``vault_name``)."""
    return click.option(
        "--vault",
        "vault_name",
        default=None,
        help=VAULT_HELP,
    )(fn)
