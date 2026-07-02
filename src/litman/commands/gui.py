"""``lit gui`` — launch the litman webUI (M-web-gui).

Starts a localhost-only FastAPI + uvicorn server serving the vendored SPA and
the read/write API over the active vault. The server stack lives in the
``litman[web]`` optional extra; the CLI itself stays fastapi-free (invariant
#5), so fastapi/uvicorn/the server module are imported *inside* the command
body, behind the extra-installed guard. Importing this module must not pull
fastapi in.

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

console = Console()

_DEFAULT_PORT = 8765


def _find_free_port(start: int) -> int:
    """Return the first free TCP port at or above ``start`` on 127.0.0.1.

    Probes by binding a socket; a busy port raises ``OSError`` and we step to
    the next one. Never errors out on a busy port (Jupyter model) — the caller
    prints whatever port we land on.
    """
    port = start
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1


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

    Requires the ``litman[web]`` extra (fastapi + uvicorn). On HPC, tunnel the
    printed port with ``ssh -L`` and open the URL in your local browser.
    """
    try:
        import uvicorn
    except ImportError:
        console.print(
            "[bold red]error:[/] the webUI needs the optional [bold]web[/] extra "
            "(fastapi + uvicorn), which is not installed."
        )
        # Escape the brackets so Rich renders the literal "litman[web]" rather
        # than treating "[web]" as a (vanishing) markup tag.
        console.print(
            r"Install it with:  pipx install 'litman\[web]'  "
            r"(or pip install 'litman\[web]')"
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
