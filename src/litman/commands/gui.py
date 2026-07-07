"""``lit gui`` — launch the litman webUI (M-web-gui).

Starts a localhost-only FastAPI + uvicorn server serving the vendored SPA and
the read/write API over the active vault. fastapi + uvicorn are core
dependencies (the web UI is a first-class interface, ADR-018), but the CLI's
startup path stays fastapi-free (invariant #5): fastapi/uvicorn/the server
module are imported *inside* the command body, so importing this module — or
any other ``lit`` command — must not pull fastapi in.

bind 127.0.0.1 only — HPC users tunnel via ``ssh -L`` (the command prints a
copy-pasteable tunnel line). A busy port is never fatal: the port finder walks
upward to the next free port (Jupyter model) and the actual port is printed.
"""

from __future__ import annotations

import getpass
import socket
from pathlib import Path

import click
from rich.console import Console

from litman.core.library import find_vault, resolve_library_or_vault
from litman.exceptions import LitmanError

console = Console()

_DEFAULT_PORT = 8765
_MAX_PORT = 65535


def _find_free_port(start: int) -> int:
    """Return the first free TCP port at or above ``start`` on 127.0.0.1.

    Probes by binding a socket; a busy port raises ``OSError`` and we step to
    the next one (Jupyter model) — the caller prints whatever port we land on.
    Raises ``LitmanError`` only if the whole ``[start, 65535]`` range is busy
    (rather than stepping past 65535, where ``bind`` would raise OverflowError).
    """
    port = start
    while port <= _MAX_PORT:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise LitmanError(
        f"No free TCP port available in [{start}, {_MAX_PORT}] on 127.0.0.1. "
        "Free a port or pass an explicit --port."
    )


@click.command("gui")
@click.option(
    "--port",
    type=int,
    default=None,
    help=f"Port to bind (default {_DEFAULT_PORT}; auto-increments if busy).",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help=(
        "Override the active vault. Discovery order: this flag / $LIT_LIBRARY, "
        "then the active registered vault, then cwd-walk."
    ),
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
def gui_cmd(
    port: int | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Launch the litman webUI (browse / read PDFs / annotate) on localhost.

    On HPC, tunnel the printed port with ``ssh -L`` and open the URL in your
    local browser.
    """
    # fastapi + uvicorn are core dependencies, so this import normally always
    # succeeds; the guard only fires on a corrupted install, and points at a
    # reinstall rather than a (no-longer-existing) optional extra.
    try:
        import uvicorn
    except ImportError:
        console.print(
            "[bold red]error:[/] the web UI needs fastapi + uvicorn, which are "
            "missing from this install."
        )
        console.print(
            "Reinstall litman:  uv tool install --force litman  "
            "(or pipx install --force litman)"
        )
        raise SystemExit(1) from None

    from litman.server import create_app

    vault = find_vault(resolve_library_or_vault(library, vault_name))

    actual_port = _find_free_port(port if port is not None else _DEFAULT_PORT)
    user = getpass.getuser()
    host = socket.gethostname()

    console.print(
        f"[green]litman webUI[/] serving vault [bold]{vault}[/] "
        f"on [bold]http://127.0.0.1:{actual_port}[/]"
    )
    console.print(
        "[dim]SSH tunnel (run on your local machine):[/]\n"
        f"  ssh -L {actual_port}:localhost:{actual_port} {user}@{host}"
    )

    uvicorn.run(create_app(vault), host="127.0.0.1", port=actual_port)
