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

``--window`` owns its browser: the app window is the application, so closing
it stops the server, and Ctrl+C closes the window. The forward direction is
an AND gate, not a process wait: Chromium hands windows across processes, so
the spawned process exiting does not mean the window closed — the server
stops only once that process is gone AND no page holds the ``/api/presence``
WebSocket open (see :func:`_stop_server_when_window_closes`). Two more
things are load-bearing — the dedicated ``--user-data-dir`` (see
:func:`browser_profile_dir`) and the desktop shortcut running the console-less
``litw`` twin (see :func:`_shortcut_executable`). A terminal-launched
``lit gui`` (tab mode) keeps the plain Ctrl+C contract: what the terminal
started, the terminal stops.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from importlib.resources import files
from pathlib import Path
from typing import Any

import click
from platformdirs import user_cache_dir
from rich.console import Console

from litman.commands._options import library_option, vault_option
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.locking import rmtree as _rmtree
from litman.core.presence import PresenceTracker
from litman.core.vault_registry import REGISTRY_APP_NAME, REGISTRY_ENV_VAR
from litman.exceptions import LibraryNotFoundError, LitmanError

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


_BROWSER_PROFILE_DIRNAME = "browser-profile"


def browser_profile_dir() -> Path:
    """Chromium ``--user-data-dir`` for the ``--window`` app window.

    A dedicated profile gives us a browser instance of our own. Launched
    against the user's normal profile, a Chromium hands the URL to the
    already-running browser and exits at once — leaving no process for the
    Ctrl+C path to terminate (a dead window shell would outlive the server on
    screen), and dropping litman's app window into the middle of the user's
    everyday browsing session.

    The cache dir, not the config dir the registry lives in: this holds tens of
    MB of Chromium's own state and must never ride along on a cloud-synced
    config dir. ``$LITMAN_REGISTRY_DIR`` still overrides it, so the test
    suite's ``_isolate_registry`` fixture keeps it out of a developer's real
    home for free.
    """
    override = os.environ.get(REGISTRY_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser() / _BROWSER_PROFILE_DIRNAME
    return Path(user_cache_dir(REGISTRY_APP_NAME)) / _BROWSER_PROFILE_DIRNAME


def remove_browser_profile() -> Path | None:
    """Delete the app-window browser profile. Returns the path, or None.

    Counterpart to :func:`browser_profile_dir`, used by ``lit uninstall`` so
    the profile does not outlive the install. Returns None when there was
    nothing to remove, or when something still holds it open (a running
    browser on Windows) and the directory survived.
    """
    target = browser_profile_dir()
    if not target.is_dir():
        return None
    _rmtree(target, ignore_errors=True)
    return None if target.is_dir() else target


# Chromium treats the presence of this file in the user-data-dir as proof that
# first run already happened.
_FIRST_RUN_SENTINEL = "First Run"

def _quiet_browser_profile(profile: Path) -> None:
    """Mark a fresh app-window profile as one the browser has already run.

    Two things follow from the sentinel. It keeps Edge from signing the
    profile into the Windows account on sight, and it suppresses Edge's
    first-run self-restart — the spawned process handing the real window to
    a process we never see. The presence gate in
    :func:`_stop_server_when_window_closes` survives that handoff on its own
    now, so the suppression is defense in depth, not the fix.

    The sentinel is all we write. Seeding Chromium's ``Preferences`` from
    outside makes the browser announce that its settings were changed
    unexpectedly — louder than the prompts it was meant to silence. The flags
    in :func:`_app_window_argv` and the page's own ``translate="no"`` cover
    what those preferences did. Best-effort: a profile we cannot seed still
    opens a window, just a chattier one.
    """
    with contextlib.suppress(OSError):
        (profile / _FIRST_RUN_SENTINEL).touch(exist_ok=True)


def _app_window_argv(url: str) -> list[str] | None:
    """argv for a Chromium-family ``--app=`` window, or None if none found.

    ``--app=`` gives a standalone window without address/tab bars — the
    closest thing to a native app with zero new dependencies (ADR-019).

    ``--user-data-dir`` is not a preference: it forces a browser instance of
    our own — the process the Ctrl+C path can terminate without touching the
    user's everyday browser session (see :func:`browser_profile_dir`). The
    suppression flags exist because a never-before-used profile otherwise
    greets the user with a first-run tab, a make-me-default prompt and a
    translate bubble on top of their library. The profile's sentinel file
    quiets the rest (see :func:`_quiet_browser_profile`).
    """
    flags = [
        f"--app={url}",
        f"--user-data-dir={browser_profile_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--disable-sync",
    ]
    for name in _CHROMIUM_CANDIDATES:
        exe = shutil.which(name)
        if exe:
            return [exe, *flags]
    if sys.platform == "darwin":
        # Chrome/Edge on macOS are .app bundles, not on PATH. Run the binary
        # inside the bundle rather than `open -na`: `open` asks Launch Services
        # to start the app and returns immediately, so it never owns the window.
        for app in ("Google Chrome", "Microsoft Edge"):
            for root in (Path("/Applications"), Path.home() / "Applications"):
                binary = root / f"{app}.app" / "Contents" / "MacOS" / app
                if binary.exists():
                    return [str(binary), *flags]
    if sys.platform == "win32":
        # Edge ships with Win10+ but is not always on PATH.
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                exe_path = (
                    Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
                )
                if exe_path.exists():
                    return [str(exe_path), *flags]
    return None


def _stop_server_when_window_closes(
    proc: subprocess.Popen[bytes],
    server: Any,
    presence: PresenceTracker,
    *,
    first_connect_grace: float = 15.0,
    linger: float = 5.0,
    poll: float = 0.25,
) -> None:
    """Ask uvicorn to shut down once the window is gone — an AND gate.

    The spawned process exiting is necessary but not sufficient: Chromium
    hands windows across processes (Edge restarts itself partway through a
    fresh profile's first run), so ``proc.wait()`` returning can mean a
    handoff, not a closed window. The page is the other witness — the SPA
    holds a WebSocket open to ``/api/presence`` for as long as it is loaded,
    and ``presence`` counts those sockets. After the process exits, the
    server stops only once the count is zero and has stayed zero for
    ``linger`` seconds (an F5 reload drops and re-opens the socket inside
    that window). If no page ever connected — the browser never came up —
    ``first_connect_grace`` bounds the wait so a failed launch still leaves
    no orphan. Consequence: the server now follows the *last live page*, not
    the window process; an extra tab on the same server keeps it alive after
    the window closes.

    The keyword defaults are the shipped values; tests inject shorter ones.
    Each round reads the tracker through a single ``snapshot()`` call — read
    as separate properties, a connect landing between reads can show
    ``ever_connected=True`` with ``last_zero=None`` torn across rounds
    instead of confined to one (the None guard below absorbs it).

    ``server`` is a ``uvicorn.Server`` (untyped here to keep uvicorn out of the
    CLI import path, invariant #5). Its main loop polls ``should_exit`` every
    100 ms, so setting the flag from this thread is the supported way to stop
    it from outside the event loop.
    """
    proc.wait()
    exited_at = time.monotonic()
    while True:
        count, ever_connected, last_zero = presence.snapshot()
        if count == 0:
            if ever_connected:
                # last_zero is None ⇒ a connection is being established right
                # now (torn snapshot) — treat it as not idle and keep polling.
                # Without this guard, None in the subtraction is a TypeError,
                # this daemon thread dies silently, and the server becomes an
                # orphan that never exits.
                if last_zero is not None and time.monotonic() - last_zero >= linger:
                    break
            elif time.monotonic() - exited_at >= first_connect_grace:
                break
        time.sleep(poll)
    server.should_exit = True


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


def _shortcut_executable() -> str:
    """The executable a desktop shortcut should run.

    Windows decides whether a process gets a console window from the subsystem
    field in the exe's own PE header, and no ``.lnk`` field overrides it —
    ``lit.exe`` is a console app, so double-clicking the shortcut pops a black
    box that outlives the window. ``litw.exe`` is the gui-scripts twin (same
    entry point, windows subsystem), which is why the shortcut targets it.

    Linux and macOS decide in the launcher instead (``Terminal=false``, an
    ``.app`` stub), so they keep plain ``lit``.

    Falling back to ``lit`` when the twin is missing — an install predating it,
    or a launcher that skipped gui-scripts — is deliberate: a console window is
    ugly, not fatal, and a shortcut that fails to exist is worse.
    """
    lit = _resolve_lit_executable()
    if sys.platform != "win32":
        return lit
    on_path = shutil.which("litw")
    if on_path:
        return str(Path(on_path).resolve())
    sibling = Path(lit).with_name("litw.exe")
    if sibling.is_file():
        return str(sibling)
    return lit


def _windows_desktop_dir() -> Path:
    """The folder the shell actually shows as Desktop.

    Not the literal ``%USERPROFILE%\\Desktop``: with OneDrive folder backup
    on (the default once Windows 11 signs into a Microsoft account) the shell
    moves Desktop to ``%USERPROFILE%\\OneDrive\\Desktop``, and a shortcut
    written to the literal path lands in a folder Explorer no longer
    displays — the installer then says "double-click the Desktop icon" about
    an icon the user cannot see. ``SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)``
    asks the shell where Desktop currently is, redirects included. Best
    effort: any failure falls back to the literal path, which is correct on
    every machine without folder redirection.
    """
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        # 0x10 = CSIDL_DESKTOPDIRECTORY, the physical folder (0x00 is the
        # virtual desktop namespace); final 0 = SHGFP_TYPE_CURRENT.
        ok = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
        if ok == 0 and buf.value:
            return Path(buf.value)
    except (OSError, AttributeError):
        pass
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(userprofile) / "Desktop"


def shortcut_path() -> Path:
    """Where the desktop shortcut lives on this platform.

    Windows lands on the actual Desktop (``%USERPROFILE%\\Desktop``) so the
    icon is visible the moment the installer finishes — the install script
    creates it, and a fresh install is meant to be started by double-clicking
    it, not by running ``lit setup``. macOS uses ``~/Applications`` and Linux
    the applications menu, each platform's own launcher home (a ``.desktop``
    file on the Linux Desktop would need a manual "trust" step).
    """
    if sys.platform == "win32":
        return _windows_desktop_dir() / "litman.lnk"
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
    lit = _shortcut_executable()
    if sys.platform == "win32":
        _write_shortcut_win32(target, lit)
    elif sys.platform == "darwin":
        _write_shortcut_darwin(target, lit)
    else:
        _write_shortcut_linux(target, lit)
    return target, existed


def remove_shortcut() -> Path | None:
    """Delete the desktop shortcut if present. Counterpart to
    :func:`create_shortcut`, used by ``lit uninstall``.

    Returns the path removed, or ``None`` when there was nothing there. The
    macOS artifact is a ``.app`` bundle (a directory) so it is removed
    recursively; the Linux ``.desktop`` and Windows ``.lnk`` are single files.
    """
    target = shortcut_path()
    if not target.exists():
        return None
    if target.is_dir():  # macOS .app bundle
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink()
    return target


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


# Explorer caches shortcut icons by the icon FILE'S PATH, and litman.ico always
# sits at the same path inside the install. Upgrading rewrites the bytes there,
# so a user who had the old artwork keeps seeing it — deleting the .lnk does not
# help, because the stale entry is keyed on the .ico, not the shortcut.
# SHCNE_ASSOCCHANGED is the notification installers send to make the shell drop
# those bitmaps. Best-effort: a shell that refuses to refresh must not fail the
# shortcut we just wrote successfully.
_SHELL_ICON_REFRESH = (
    "; try { "
    "Add-Type -Namespace Litman -Name Shell -MemberDefinition "
    "'[DllImport(\"shell32.dll\")] public static extern void "
    "SHChangeNotify(int eventId, uint flags, IntPtr item1, IntPtr item2);' "
    "-ErrorAction Stop; "
    # SHCNE_ASSOCCHANGED = 0x08000000, SHCNF_IDLIST = 0x0000
    "[Litman.Shell]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero) "
    "} catch { }"
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
        + _SHELL_ICON_REFRESH
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
            f"Could not create the desktop shortcut: {detail.strip()}"
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
@library_option
@vault_option
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

    # No vault to serve → start in welcome-page mode (vault=None) so a fresh
    # install can create a library from the browser (task-gui-welcome). But an
    # explicit --library / --vault that fails to resolve is a real mistake the
    # user should see, so re-raise it rather than silently dropping to welcome.
    explicit = resolve_library_or_vault(library, vault_name)
    try:
        vault: Path | None = find_vault(explicit)
    except LibraryNotFoundError:
        if explicit is not None:
            raise
        vault = None

    actual_port = _find_free_port(port if port is not None else _DEFAULT_PORT)
    user = getpass.getuser()
    host = socket.gethostname()
    url = f"http://127.0.0.1:{actual_port}"

    if vault is not None:
        console.print(
            f"[green]litman webUI[/] serving vault [bold]{vault}[/] "
            f"on [bold]{url}[/]"
        )
    else:
        console.print(
            f"[green]litman webUI[/] on [bold]{url}[/]\n"
            "[dim]No vault yet — open the URL to create your library.[/]"
        )
    console.print(
        "[dim]SSH tunnel (run on your local machine):[/]\n"
        f"  ssh -L {actual_port}:localhost:{actual_port} {user}@{host}"
    )

    # Keep the app reference: the window watcher reads the presence tracker
    # off app.state (created unconditionally by create_app).
    app = create_app(vault)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=actual_port)
    )

    browser_timer: threading.Timer | None = None
    # The app window we spawned, if we spawned one. Appended from the timer
    # thread, read in `finally` — a list because a plain name cannot be rebound
    # across that boundary.
    owned: list[subprocess.Popen[bytes]] = []

    if not no_browser and display_available():
        app_argv = _app_window_argv(url) if window else None
        if window and app_argv is None:
            console.print(
                "[dim]No Chrome/Edge/Chromium found for --window; opening a "
                "normal browser tab instead.[/]"
            )
        elif app_argv is not None:
            console.print("[dim]Close the window to stop the server (or Ctrl+C).[/]")

        def _open() -> None:
            if app_argv is None:
                webbrowser.open(url)
                return
            try:
                profile = browser_profile_dir()
                profile.mkdir(parents=True, exist_ok=True)
                _quiet_browser_profile(profile)
                proc = subprocess.Popen(
                    app_argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except OSError:
                # The browser vanished between the `which` probe and now. A tab
                # is a worse window, but no window at all is worse still — and
                # without a process to watch, the server keeps the Ctrl+C
                # contract rather than exiting immediately.
                webbrowser.open(url)
                return
            owned.append(proc)
            threading.Thread(
                target=_stop_server_when_window_closes,
                args=(proc, server, app.state.presence),
                daemon=True,
            ).start()

        # The server comes up sub-second; 1s keeps the browser from racing it.
        browser_timer = threading.Timer(1.0, _open)
        browser_timer.daemon = True
        browser_timer.start()

    try:
        server.run()
    finally:
        # No-op once fired; stops the open if server startup raised first.
        if browser_timer is not None:
            browser_timer.cancel()
        # The other direction: the server stopped first (Ctrl+C, or a crash),
        # so close the window it was serving rather than leave a dead shell on
        # screen. Safe because the profile is ours alone — there are no other
        # tabs to take down with it. A no-op when the window is already gone.
        for proc in owned:
            with contextlib.suppress(OSError):
                proc.terminate()
