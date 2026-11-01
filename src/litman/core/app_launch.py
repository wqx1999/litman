"""Open a desktop-app agent at the vault (task-zcode-agent).

Most catalog agents are terminal CLIs: :mod:`litman.core.terminal` opens a
window that runs their command. An agent whose row has an ``open_url`` is (also)
a desktop app — litman opens the app itself at the vault and returns at once,
without waiting for it.

Only the catalog's data fields are read here (``url_scheme``, ``app_args``,
``open_url``, ``mac_bundle``); no app is named in code (ADR-020). How each
platform finds the app:

* Windows, a row with ``app_args``: the app writes its link scheme's open
  command under ``HKEY_CLASSES_ROOT\\<scheme>\\shell\\open\\command`` every time
  it starts, and that command's first argument is the exe. An app installed but
  never opened has no key yet, which is why "not found" also says "not opened
  yet".
* Windows, a row without ``app_args``: that key's exe, or failing that the
  system's answer on whether anything handles the scheme. A packaged app has no
  such key and no exe to reach; a traditional install has the key, and can leave
  it behind when uninstalled, which is why the exe has to exist.
* macOS: ``<mac_bundle>`` under ``/Applications`` or ``~/Applications``.
* Linux: a ``mimeapps.list`` or ``mimeinfo.cache`` names a handler for the
  scheme. Those are the files ``xdg-mime query default`` reads first; it is not
  run because, finding nothing (most users have no such app), it greps every
  ``.desktop`` file — 8 s on a network filesystem, on each status check.

On Windows, an exe plus an ``app_args`` template starts the exe directly,
at the vault when the template names it. Everywhere else the app's link goes
to the system opener, on Windows from a short-lived child process; the app may
ask the user to confirm it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from litman.core import viewer
from litman.exceptions import LitmanError

if TYPE_CHECKING:
    from litman.core.agents import AgentSpec

# Win32 process-creation flags, by value: Linux's subprocess module has no
# names for them, and the Windows branch is tested on every platform.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

# AssocQueryStringW's ASSOCSTR value for the handler's display name.
_ASSOCSTR_FRIENDLYAPPNAME = 4


class AppNotFound(LitmanError):
    """The app is not installed here, has never been opened, or would not start."""


class AppNoDisplay(LitmanError):
    """Linux without a graphical session: there is nothing to open the app in."""


def not_installed_message(spec: AgentSpec) -> str:
    """One verdict and one way out (the error-length budget)."""
    return (
        f"{spec.display} isn't installed, or hasn't been opened yet. "
        "Open it once, then try again."
    )


# The seams below — the platform, the registry, the app folders, xdg-mime and
# the process starters — are looked up at call time, so a test replaces them
# on this module instead of faking ``sys.platform`` (the whole interpreter's).


def _platform() -> str:
    return sys.platform


def _read_scheme_command(scheme: str) -> str | None:
    """The default value of ``HKCR\\<scheme>\\shell\\open\\command``, or None.

    HKEY_CLASSES_ROOT merges the per-user and machine-wide keys, so both a
    per-user and an all-users install are found.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - defensive for non-CPython builds
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, rf"{scheme}\shell\open\command"
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _handler_name_result(scheme: str) -> int | None:
    """The HRESULT of asking Windows for the display name of ``<scheme>:``'s
    handler, or None when the call could not be made: off Windows, ctypes has
    no ``WinDLL``."""
    try:
        import ctypes
        from ctypes import wintypes

        shlwapi = ctypes.WinDLL("shlwapi")  # type: ignore[attr-defined]  # Windows only
        name = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(name))
        result = shlwapi.AssocQueryStringW(
            0,
            _ASSOCSTR_FRIENDLYAPPNAME,
            ctypes.c_wchar_p(scheme),
            None,
            name,
            ctypes.byref(size),
        )
    except (ImportError, AttributeError, OSError):
        return None
    return int(result)


def _scheme_has_handler(scheme: str) -> bool:
    """Does Windows know a program for ``<scheme>:`` links?

    The one probe that sees a packaged app. Measured on Win11 with Claude
    4.29: it has no ``HKCR`` open command, and asking for the executable fails,
    while asking for the display name returns 0 and ``Claude``. A scheme nothing
    handles returns ``ERROR_NO_ASSOCIATION``. Any failure reads as no handler.
    """
    return _handler_name_result(scheme) == 0


def _app_dirs() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def _xdg_mime_files() -> list[Path]:
    """The XDG files that map a link scheme to a ``.desktop`` handler, in
    lookup order: each ``mimeapps.list`` (where ``xdg-mime default`` writes),
    then each applications dir's ``mimeinfo.cache`` (what a package install
    refreshes)."""
    home = Path.home()

    def dirs(var: str, fallback: str) -> list[Path]:
        # The spec separates these with ":"; os.pathsep is ":" on Linux and
        # keeps a test's tmp paths intact on Windows.
        value = os.environ.get(var) or fallback
        return [Path(d) for d in value.split(os.pathsep) if d]

    config = dirs("XDG_CONFIG_HOME", str(home / ".config")) + dirs(
        "XDG_CONFIG_DIRS", "/etc/xdg"
    )
    data = dirs("XDG_DATA_HOME", str(home / ".local" / "share")) + dirs(
        "XDG_DATA_DIRS", os.pathsep.join(["/usr/local/share", "/usr/share"])
    )
    apps = [d / "applications" for d in data]
    return (
        [d / "mimeapps.list" for d in config]
        + [d / "mimeapps.list" for d in apps]
        + [d / "mimeinfo.cache" for d in apps]
    )


_XDG_HANDLER_SECTIONS = (
    "[Default Applications]",
    "[Added Associations]",
    "[MIME Cache]",
)


def _xdg_default_handler(scheme: str) -> str:
    """The ``.desktop`` file handling ``<scheme>:`` links, or "" if none."""
    key = f"x-scheme-handler/{scheme}="
    for path in _xdg_mime_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("["):
                section = line
            elif section in _XDG_HANDLER_SECTIONS and line.startswith(key):
                handler = line[len(key) :].split(";")[0].strip()
                if handler:
                    return handler
    return ""


def _popen_detached_windows(argv: list[str], env: dict[str, str]) -> None:
    """Start the app on Windows so that it never writes into our console.

    Left alone, an Electron app attaches to its parent's console: its
    background process logs there and retitles the window. Electron does
    that through Chromium's RouteStdioToConsole, which returns early when
    stdout / stderr are already valid handles — so the NUL handles of
    ``DEVNULL`` on all three streams are the real fix. The caller's
    ``ELECTRON_NO_ATTACH_CONSOLE=1`` (an Electron production variable, Windows
    only) is a second guard. ``DETACHED_PROCESS`` does little for a GUI exe
    and is harmless. No ``CREATE_BREAKAWAY_FROM_JOB``: the uv ``lit.exe``
    launcher's job allows silent breakaway, so the app outlives ``lit``.

    Measured on Win11 with ZCode: started straight from PowerShell, it logs
    into that window for as long as it runs and retitles it
    ``zcode-host-local-1``. Started from here, by ``lit agent`` or the GUI's
    launch route, the window keeps its title and gets no app output, and the
    app keeps running after ``lit gui`` stops.
    """
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def _spawn(argv: list[str]) -> None:
    """Start an opener sharing nothing with this process (as viewer._spawn)."""
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _open_link_detached_windows(url: str) -> None:
    """Open a link from a short-lived python that shares nothing with us.

    A packaged app has no exe to start with ``DEVNULL`` handles, so its link
    must go through ``os.startfile``, and that hands the app our console. An
    Electron app started that way logs into the window only while the process
    that opened it lives: a brief one is gone before that matters, ``lit gui``
    is not. Measured on Win11 with Claude 4.29, not running beforehand:
    ``os.startfile`` from a python alive for 40 s logged into its window, also
    with ``ELECTRON_NO_ATTACH_CONSOLE=1``, which this app ignores; through this
    child the window stayed clean and the app opened normally.

    ``CREATE_NO_WINDOW`` rather than ``DETACHED_PROCESS``: a venv's
    ``python.exe`` is a launcher that starts the real python, and with no console
    to inherit that one opens its own, visible. Measured on Win11 in a uv tool
    environment: with ``DETACHED_PROCESS`` a window flashed on every launch; with
    ``CREATE_NO_WINDOW`` the child's console has no window, and Claude, not running
    beforehand, opened normally with nothing logged into the parent's window.

    The child is not waited on, so a failed open goes unnoticed here, which is
    why :func:`open_app` checks :func:`app_installed` first.
    """
    subprocess.Popen(
        [sys.executable, "-c", "import os,sys; os.startfile(sys.argv[1])", url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


_UNQUOTED_EXE = re.compile(r"(.+?\.exe)(?:\s|$)", re.IGNORECASE)


def exe_from_open_command(command: str | None) -> str | None:
    """The exe of a registry open command such as ``"D:\\ZCode.exe" -- "%1"``.

    A quoted first argument runs to the closing quote; an unquoted one to the
    first ``.exe`` (its folder may contain spaces). Anything that is not an
    ``.exe`` is None.
    """
    text = (command or "").strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        exe = text[1:end] if end > 0 else ""
    else:
        match = _UNQUOTED_EXE.match(text)
        exe = match.group(1) if match else ""
    return exe if exe.casefold().endswith(".exe") else None


def windows_exe(spec: AgentSpec) -> Path | None:
    """The app's registered exe, only if that file exists."""
    if not spec.url_scheme:
        return None
    exe = exe_from_open_command(_read_scheme_command(spec.url_scheme))
    if exe is None:
        return None
    path = Path(exe)
    return path if path.is_file() else None


def workspace_url(spec: AgentSpec, vault: Path) -> str:
    """The app's link opening ``vault``; the path is fully percent-encoded."""
    assert spec.open_url is not None
    return spec.open_url.replace("{vault}", quote(str(vault), safe=""))


def app_argv(spec: AgentSpec, exe: Path, vault: Path) -> list[str]:
    """The exe plus its argument template, the vault as one argument."""
    return [str(exe), *(arg.replace("{vault}", str(vault)) for arg in spec.app_args)]


def opens_at_vault(spec: AgentSpec) -> bool:
    """Does opening the app pass it the vault? Only when its link or an exe
    argument has ``{vault}`` in it; an app taking no folder opens where it
    was last used."""
    return "{vault}" in (spec.open_url or "") or any(
        "{vault}" in arg for arg in spec.app_args
    )


def app_reaches_user() -> bool:
    """Would an app opened from this process show up in front of the user?

    Reads this process's environment, not where the browser is. Asked only
    to choose between a row's two ways of starting; a row that is only an app
    goes to :func:`open_app` regardless.
    """
    if viewer.is_ssh_session():
        return False
    return _platform() in ("win32", "darwin") or not viewer.is_headless()


def app_installed(spec: AgentSpec) -> bool:
    """Is the app installed (on Windows, a row with ``app_args``: and opened
    once)? A row missing the field a platform needs is never reported as
    installed."""
    if not spec.url_scheme or not spec.open_url:
        return False
    platform = _platform()
    if platform == "win32":
        if spec.app_args:
            return windows_exe(spec) is not None
        # Either probe alone misses an install: a packaged app has no exe on
        # record, and a scheme with one can still have no display name
        # (measured: ERROR_NOT_FOUND). That error is no yes in itself, as
        # `.txt` returns it too.
        return windows_exe(spec) is not None or _scheme_has_handler(spec.url_scheme)
    if platform == "darwin":
        bundle = spec.mac_bundle
        return bool(bundle) and any((d / cast(str, bundle)).is_dir() for d in _app_dirs())
    return bool(_xdg_default_handler(spec.url_scheme))


def open_app(spec: AgentSpec, vault: Path) -> None:
    """Open the app at ``vault`` and return without waiting for it.

    Raises:
        AppNotFound: not installed / never opened, or the start failed.
        AppNoDisplay: Linux without ``DISPLAY`` / ``WAYLAND_DISPLAY``.
    """
    if not app_installed(spec):
        raise AppNotFound(not_installed_message(spec))
    platform = _platform()
    try:
        if platform == "win32":
            exe = windows_exe(spec)
            if exe is not None and spec.app_args:
                env = {**os.environ, "ELECTRON_NO_ATTACH_CONSOLE": "1"}
                _popen_detached_windows(app_argv(spec, exe, vault), env)
            else:
                _open_link_detached_windows(workspace_url(spec, vault))
            return
        if platform == "darwin":
            _spawn(["open", workspace_url(spec, vault)])
            return
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise AppNoDisplay(
                f"No display here to open {spec.display} in. "
                f"Run `lit agent {spec.name}` on the machine you sit at."
            )
        _spawn(["xdg-open", workspace_url(spec, vault)])
    except OSError as exc:
        raise AppNotFound(f"Could not open {spec.display}: {exc}") from exc
