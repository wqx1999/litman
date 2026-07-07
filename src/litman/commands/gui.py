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

When the session has a display, the URL also opens in the user's browser
(``--no-browser`` suppresses it; ``--window`` opens a Chromium ``--app=``
window instead of a tab). Headless sessions never attempt a browser launch —
``webbrowser`` on a display-less Linux box can drag up a text-mode browser,
which is worse than the printed URL. ``--make-shortcut`` writes a desktop
entry that runs ``lit gui --window`` and exits without starting the server
(shared with ``lit setup`` step 5, ADR-019).
"""

from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from importlib.resources import files
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


# ---------------------------------------------------------------------------
# browser opening
# ---------------------------------------------------------------------------

_CHROMIUM_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "msedge",
    "brave-browser",
)


def display_available() -> bool:
    """True when this session can show a browser window.

    Windows and macOS sessions always can. On Linux, require ``DISPLAY`` or
    ``WAYLAND_DISPLAY`` — on a headless box ``webbrowser`` may hand the URL
    to a text-mode browser (lynx/w3m), which is worse than not opening.
    """
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _app_window_argv(url: str) -> list[str] | None:
    """argv for a Chromium-family ``--app=`` window, or None if none found.

    ``--app=`` gives a standalone window without address/tab bars — the
    closest thing to a native app with zero new dependencies (ADR-019).
    """
    for name in _CHROMIUM_CANDIDATES:
        exe = shutil.which(name)
        if exe:
            return [exe, f"--app={url}"]
    if sys.platform == "darwin":
        # Chrome/Edge on macOS are .app bundles, not on PATH.
        for app in ("Google Chrome", "Microsoft Edge"):
            for root in (Path("/Applications"), Path.home() / "Applications"):
                if (root / f"{app}.app").exists():
                    return ["open", "-na", app, "--args", f"--app={url}"]
    if sys.platform == "win32":
        # Edge ships with Win10+ but is not always on PATH.
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                exe_path = (
                    Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
                )
                if exe_path.exists():
                    return [str(exe_path), f"--app={url}"]
    return None


# ---------------------------------------------------------------------------
# desktop shortcut (shared with `lit setup` step 5)
# ---------------------------------------------------------------------------


def _icon_path(name: str) -> Path:
    # litman always installs unpacked (wheel/editable), so the bundled icon
    # has a stable filesystem path a shortcut can point at. as_file() would
    # hand out a temp copy that dies with this process.
    return Path(str(files("litman").joinpath("assets", "icons", name)))


def _resolve_lit_executable() -> str:
    exe = shutil.which("lit")
    if exe:
        return str(Path(exe).resolve())
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.stem == "lit" and argv0.is_file():
        return str(argv0)
    raise LitmanError(
        "Could not locate the `lit` executable to embed in the shortcut. "
        "Make sure `lit` is on PATH, then re-run: lit gui --make-shortcut"
    )


def shortcut_path() -> Path:
    """Where the desktop shortcut lives on this platform."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(
            Path.home() / "AppData" / "Roaming"
        )
        return (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "litman.lnk"
        )
    if sys.platform == "darwin":
        return Path.home() / "Applications" / "litman.app"
    data_home = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local" / "share"
    )
    return Path(data_home) / "applications" / "litman.desktop"


def create_shortcut() -> tuple[Path, bool]:
    """Create or refresh the desktop shortcut. Returns ``(path, existed)``.

    Idempotent: an existing shortcut is overwritten, never an error.
    """
    target = shortcut_path()
    existed = target.exists()
    lit = _resolve_lit_executable()
    if sys.platform == "win32":
        _write_shortcut_win32(target, lit)
    elif sys.platform == "darwin":
        _write_shortcut_darwin(target, lit)
    else:
        _write_shortcut_linux(target, lit)
    return target, existed


def _write_shortcut_linux(target: Path, lit: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=litman\n"
        "Comment=Personal literature vault\n"
        f'Exec="{lit}" gui --window\n'
        f"Icon={_icon_path('litman.png')}\n"
        "Terminal=false\n"
        "Categories=Office;Science;\n",
        encoding="utf-8",
    )


def _write_shortcut_win32(target: Path, lit: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    def q(s: object) -> str:
        # PowerShell single-quoted string escape: double any embedded quote.
        return str(s).replace("'", "''")

    # TargetPath/Arguments are discrete .lnk fields, so spaces in the lit
    # path are safe without shell quoting.
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{q(target)}'); "
        f"$s.TargetPath = '{q(lit)}'; "
        "$s.Arguments = 'gui --window'; "
        f"$s.IconLocation = '{q(_icon_path('litman.ico'))}'; "
        f"$s.WorkingDirectory = '{q(Path.home())}'; "
        "$s.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        raise LitmanError(
            f"Could not create the Start Menu shortcut: {detail.strip()}"
        ) from e


def _write_shortcut_darwin(target: Path, lit: str) -> None:
    # Minimal .app bundle: Info.plist + an executable shell stub. No .icns
    # pipeline in v1 — the bundle works without a custom icon.
    macos_dir = target / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    (target / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>CFBundleName</key><string>litman</string>\n"
        "  <key>CFBundleIdentifier</key><string>io.github.litman</string>\n"
        "  <key>CFBundleExecutable</key><string>litman</string>\n"
        "  <key>CFBundlePackageType</key><string>APPL</string>\n"
        "</dict>\n"
        "</plist>\n",
        encoding="utf-8",
    )
    stub = macos_dir / "litman"
    stub.write_text(f'#!/bin/sh\nexec "{lit}" gui --window\n', encoding="utf-8")
    stub.chmod(0o755)


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
@click.option(
    "--no-browser",
    is_flag=True,
    help="Do not open a browser automatically.",
)
@click.option(
    "--window",
    is_flag=True,
    help=(
        "Open in a Chrome/Edge app window (no address bar) instead of a "
        "browser tab."
    ),
)
@click.option(
    "--make-shortcut",
    is_flag=True,
    help=(
        "Create a desktop shortcut that runs `lit gui --window`, then exit "
        "(does not start the server)."
    ),
)
def gui_cmd(
    port: int | None,
    library: Path | None,
    vault_name: str | None,
    no_browser: bool,
    window: bool,
    make_shortcut: bool,
) -> None:
    """Launch the litman webUI (browse / read PDFs / annotate) on localhost.

    Opens your browser automatically when the session has a display
    (--no-browser to skip; --window for a standalone app window). On HPC,
    tunnel the printed port with ``ssh -L`` and open the URL in your local
    browser.
    """
    if no_browser and window:
        raise click.UsageError(
            "--no-browser and --window are mutually exclusive."
        )

    if make_shortcut:
        target, existed = create_shortcut()
        console.print(
            f"[green]{'updated' if existed else 'created'}[/] desktop "
            f"shortcut: [bold]{target}[/]"
        )
        return

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
    url = f"http://127.0.0.1:{actual_port}"

    console.print(
        f"[green]litman webUI[/] serving vault [bold]{vault}[/] "
        f"on [bold]{url}[/]"
    )
    console.print(
        "[dim]SSH tunnel (run on your local machine):[/]\n"
        f"  ssh -L {actual_port}:localhost:{actual_port} {user}@{host}"
    )

    browser_timer: threading.Timer | None = None
    if not no_browser and display_available():
        app_argv = _app_window_argv(url) if window else None
        if window and app_argv is None:
            console.print(
                "[dim]No Chrome/Edge/Chromium found for --window; opening a "
                "normal browser tab instead.[/]"
            )

        def _open() -> None:
            if app_argv is not None:
                subprocess.Popen(
                    app_argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            else:
                webbrowser.open(url)

        # The server comes up sub-second; 1s keeps the browser from racing it.
        browser_timer = threading.Timer(1.0, _open)
        browser_timer.daemon = True
        browser_timer.start()

    try:
        uvicorn.run(create_app(vault), host="127.0.0.1", port=actual_port)
    finally:
        # No-op once fired; stops the open if server startup raised first.
        if browser_timer is not None:
            browser_timer.cancel()
