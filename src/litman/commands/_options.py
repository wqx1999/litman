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

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from litman.core.confirm import is_interactive

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


#: Appended to every read command's ``--format`` help.
FORMAT_DEFAULT_HELP = (
    "Default: 'table' on a terminal, 'json' anywhere else (an agent's shell, "
    "a pipe, a file)."
)


def table_or_json(
    _ctx: click.Context | None, _param: click.Parameter | None, value: str | None
) -> str:
    """The ``--format`` a read command uses: the user's, else by where stdout goes.

    The CLI is the agents' API and people read the GUI (ADR-021), so output
    that is not going to a terminal is JSON. An agent's shell is not a
    terminal, and a table squeezed into its 80 columns had every column but
    the id cut to an ellipsis. A person typing at a terminal still gets the
    table. A ``click`` option callback.
    """
    if value is not None:
        return value
    return "table" if is_interactive(sys.stdout) else "json"


def format_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Add ``--format table|json`` (parameter ``output_format``).

    For the enumerate commands (``taxonomy`` / ``vault`` / ``project`` /
    ``code`` / ``trash`` ``list``), whose tables are for people and whose
    JSON is for agents.  ``lit list`` / ``show`` / ``search`` / ``related``
    each declare their own ``--format`` because they document a specific
    payload (the INDEX projection, match spans) rather than rows. With no
    ``--format``, :func:`table_or_json` picks by where stdout goes.

    The json branch must be taken *before* a command prints its "nothing
    here" message: an empty result is ``[]``, never prose on stdout.
    """
    return click.option(
        "--format",
        "output_format",
        type=click.Choice(["table", "json"]),
        default=None,
        callback=table_or_json,
        help=(
            "Output format. 'json' emits one object per row, keyed the way "
            f"the underlying file is (for agents). {FORMAT_DEFAULT_HELP}"
        ),
    )(fn)
