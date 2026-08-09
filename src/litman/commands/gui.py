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
window instead of a tab). On macOS ``--window`` prefers a WKWebView window
owned by this very process (:mod:`litman.commands._mac_shell`) — the Dock
then badges it as litman, and no browser needs to exist — quietly falling
back to the Chromium window when pywebview cannot come up. Headless sessions
never attempt a browser launch —
``webbrowser`` on a display-less Linux box can drag up a text-mode browser,
which is worse than the printed URL. ``--make-shortcut`` writes a desktop
entry that runs ``lit gui --window`` and exits without starting the server
(shared with ``lit setup`` step 5, ADR-019).

``--window`` owns its browser: the app window is the application, so closing
it stops the server, and Ctrl+C closes the window. The shutdown signal is the
last live *page*, not the browser process: the SPA holds a ``/api/presence``
WebSocket while it is loaded, and the server stops a short linger after that
count reaches zero (see :func:`_stop_server_when_window_closes`). Asking is
not the same as stopping, so a window launch also escalates a request uvicorn
does not honour (see :func:`_escalate_shutdown`) — a GUI process has no
console to Ctrl+C twice from, and on Windows one that outlives its window
blocks its own next upgrade. The spawned
process is only a secondary hint — it can outlive the window (on Windows Edge
keeps the browser resident after the window closes) or die before it (Chromium
hands a fresh profile's first window to another process), so *waiting* on it
would either wedge the server open forever or shut it down early. Two more
things are load-bearing — the dedicated ``--user-data-dir`` (see
:func:`browser_profile_dir`) and the desktop shortcut running the console-less
``litw`` twin (see :func:`_shortcut_executable`). A terminal-launched
``lit gui`` (tab mode) keeps the plain Ctrl+C contract: what the terminal
started, the terminal stops.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

import click
from platformdirs import user_cache_dir
from rich.console import Console

from litman.cli import _launched_without_console
from litman.commands._options import library_option, vault_option
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.locking import rmtree as _rmtree
from litman.core.presence import PresenceTracker
from litman.core.vault_registry import REGISTRY_APP_NAME, REGISTRY_ENV_VAR
from litman.exceptions import LibraryNotFoundError, LitmanError

console = Console()

_DEFAULT_PORT = 8765
_MAX_PORT = 65535

# Readiness poll (Part A) — replaces the old flat 1s browser timer with "open
# the instant the server is listening". Injectable via _open_when_ready's
# keyword args; these module constants are the shipped defaults.
READY_TIMEOUT = 10.0
READY_POLL = 0.02

# Splash close backstop (Part C, gui.py side, seconds). The _splash.py
# subprocess carries its own independent millisecond self-destruct
# (SPLASH_TIMEOUT_MS) — two constants across two modules by design: the splash
# process must stay import-light and cannot import this module.
SPLASH_TIMEOUT = 25.0


# The live-refresh change token, polled by every visible page every few seconds.
# It is the one route whose access-log line carries no information: hundreds of
# identical 200s per hour, in a log file that exists to explain a *failure*.
_POLLED_PATHS = ("/api/vault-version",)


class _DropPolledAccessLines(logging.Filter):
    """Keep the uvicorn access log readable once the GUI polls on a timer.

    Launched without a console (the .app / .exe shortcuts) litman redirects
    stdout+stderr — uvicorn's access log included — into a per-launch log file.
    A 4s poll writes ~900 lines an hour there, which would bury the handful of
    lines someone opens that file to find. Dropping them costs nothing: the
    route is a pure read whose only failure mode (the vault went away) the page
    reports through the banner, not the log.

    Matches on the log record's *arguments*, not the formatted string: uvicorn
    passes ``(client_addr, method, full_path, http_version, status)``, so this
    cannot be fooled by a path that merely mentions the route inside a query
    string. Any record that does not look like an access record is kept —
    filters must never be the reason a diagnostic went missing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        path = args[2]
        if not isinstance(path, str):
            return True
        return path.split("?", 1)[0] not in _POLLED_PATHS


def _quiet_polled_access_lines() -> None:
    """Install :class:`_DropPolledAccessLines` on uvicorn's access logger.

    Call AFTER ``uvicorn.Config(...)`` — its constructor runs ``dictConfig``,
    which re-points the logger's handlers; installing before would work today
    (dictConfig leaves logger-level filters alone) but only by luck.
    Idempotent, so a second ``lit gui`` in-process cannot stack filters.
    """
    logger = logging.getLogger("uvicorn.access")
    if any(isinstance(f, _DropPolledAccessLines) for f in logger.filters):
        return
    logger.addFilter(_DropPolledAccessLines())


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
    # Edge's Linux package installs microsoft-edge-stable and only sometimes
    # leaves a microsoft-edge symlink beside it, so probe both.
    "microsoft-edge",
    "microsoft-edge-stable",
    "msedge",
    "brave-browser",
)

# macOS ships browsers as .app bundles and puts nothing on PATH, so the tuple
# above almost never fires there — these bundle names are the real lookup, and
# a name missing from them makes an installed browser invisible. Kept in step
# with _CHROMIUM_CANDIDATES on purpose: a browser Linux finds has no reason to
# be a plain tab on macOS.
#
# Every entry must honour --app and --user-data-dir. A browser that ignores
# them is worse than the tab fallback, because it would cost both the
# standalone window and the shutdown gate that waits on our own instance —
# which is why the heavily reskinned Chromium derivatives are not here.
#
# Each name is both the bundle and the executable inside it
# (Chromium.app/Contents/MacOS/Chromium); a browser that broke that symmetry
# would need its own entry shape.
_DARWIN_APP_CANDIDATES = (
    "Google Chrome",
    "Microsoft Edge",
    "Chromium",
    "Brave Browser",
)

# Every reach into /Applications goes through this name, never the literal:
# a test pretending to be darwin must not probe — or, on a Mac host, write
# into — the real folder. A literal reads as green on Linux and then answers
# with the host's own browsers on a Mac, which is a test that proves nothing.
_DARWIN_SYSTEM_APPS = Path("/Applications")


def display_available() -> bool:
    """True when this session can show a browser window.

    Windows and macOS sessions always can. On Linux, require ``DISPLAY`` or
    ``WAYLAND_DISPLAY`` — on a headless box ``webbrowser`` may hand the URL
    to a text-mode browser (lynx/w3m), which is worse than not opening.
    """
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# The identity an X11 desktop matches against StartupWMClass in
# litman.desktop. Passed via --class AND written into the .desktop file —
# the two must stay one constant: --class alone regroups the window under a
# name no .desktop file claims (falling back to the 16px tab favicon, worse
# than today), StartupWMClass alone never matches "Chromium-browser".
_LINUX_WM_CLASS = "litman"

_BROWSER_PROFILE_DIRNAME = "browser-profile"
# Under ~/snap/<snap>/common/ the profile sits among that snap's own data, so
# the name has to say whose it is — and it keeps the uninstall sweep from
# matching anything but ours.
_SNAP_PROFILE_DIRNAME = "litman-browser-profile"
_SNAP_BIN = Path("/snap/bin")
_SNAP_ROOT = Path("/snap")


def _snap_name(browser_exe: str | os.PathLike[str]) -> str | None:
    """The snap a browser executable belongs to, or None if it is not one.

    ``/snap/bin/chromium`` is snapd's wrapper — it resolves to ``snap``
    itself, so the name is all there is to go on. Snap commands are named
    ``<snap>`` or ``<snap>.<app>`` and every installed snap owns a real
    ``/snap/<snap>`` directory, which is enough to derive the name and
    confirm it in one step rather than guess.
    """
    path = Path(browser_exe)
    if path.parent != _SNAP_BIN:
        return None
    snap = path.name.split(".", 1)[0]
    return snap if (_SNAP_ROOT / snap).is_dir() else None


def _snap_user_root() -> Path:
    """Where snapd grants each snap its writable per-user area.

    Follows ``$LITMAN_REGISTRY_DIR`` when set. That override is the project's
    one "keep every litman-owned path inside this sandbox" seam — the test
    suite's autouse isolation leans on it — and a sweep that *deletes*
    directories must not be the single place that reaches past it into a real
    home. The cost is that a real machine which sets the override hands a
    confined browser an unwritable profile again, and gets the tab fallback
    in :func:`_stop_server_when_window_closes` instead of a window.
    """
    override = os.environ.get(REGISTRY_ENV_VAR, "").strip()
    return (Path(override).expanduser() if override else Path.home()) / "snap"


def browser_profile_dir(browser_exe: str | os.PathLike[str] | None = None) -> Path:
    """Chromium ``--user-data-dir`` for the ``--window`` app window.

    ``browser_exe`` is the browser this profile is for. It matters for
    exactly one case: a snap-packaged browser is confined out of hidden
    directories under ``$HOME``, and the cache dir below is one
    (``~/.cache/...``). Handed it, Ubuntu's snap Chromium cannot create its
    ``SingletonLock`` and *aborts* rather than run a profile it cannot lock —
    so a machine that installed a browser ended up with no window at all,
    where a machine with none at least got a tab. ``~/snap/<snap>/common`` is
    the writable area snapd grants that snap, so that is where its profile
    goes. Omitting the argument gives the default location, which is what
    ``lit uninstall`` and the tests want.

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
    snap = _snap_name(browser_exe) if browser_exe is not None else None
    if snap is not None:
        return _snap_user_root() / snap / "common" / _SNAP_PROFILE_DIRNAME
    override = os.environ.get(REGISTRY_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser() / _BROWSER_PROFILE_DIRNAME
    return Path(user_cache_dir(REGISTRY_APP_NAME)) / _BROWSER_PROFILE_DIRNAME


def browser_profile_dirs() -> list[Path]:
    """Every app-window browser profile that exists on this machine.

    The default location, plus a per-snap copy for each confined browser
    :func:`browser_profile_dir` had to route elsewhere. A machine that has
    run both an ordinary and a snap Chromium holds two, and ``lit uninstall``
    owes the user both — so the sweep is by directory name, not by asking
    which browsers are installed now (by uninstall time one may not be).
    """
    found = [browser_profile_dir()]
    found += sorted(_snap_user_root().glob(f"*/common/{_SNAP_PROFILE_DIRNAME}"))
    return [p for p in found if p.is_dir()]


def remove_browser_profile() -> list[Path]:
    """Delete every app-window browser profile. Returns the ones now gone.

    Counterpart to :func:`browser_profile_dirs`, used by ``lit uninstall`` so
    the profile does not outlive the install. A directory missing from the
    result is one something still holds open (a running browser on Windows)
    and which survived the attempt.
    """
    removed = []
    for target in browser_profile_dirs():
        _rmtree(target, ignore_errors=True)
        if not target.is_dir():
            removed.append(target)
    return removed


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


# Chromium's session-restore state inside the profile's default sub-profile:
# the modern SNSS directory plus the legacy file quartet older builds keep.
_SESSION_RESTORE_DIR = "Sessions"
_SESSION_RESTORE_FILES = ("Current Session", "Current Tabs", "Last Session", "Last Tabs")

# One-time migration for app profiles that saw a cacheable API 410 before API
# responses acquired ``Cache-Control: no-store``.  The marker belongs at the
# profile root (not inside Default/Cache, which the migration removes).
_HTTP_CACHE_MIGRATION_MARKER = ".api-no-store-v1"


def _purge_stale_browser_session(profile: Path) -> None:
    """Drop the profile's session-restore state before opening the window.

    Every launch hands the browser its own fresh URL, so there is never a
    previous session worth restoring — but Chromium doesn't know that. A
    force-killed browser (Task Manager sweeps while hunting a stuck server)
    is recorded as a crash, and the next launch resurrects the dead
    session's app window alongside the one we asked for: a days-old page,
    served from cache against a server that no longer exists, wearing
    whatever banner was true back then. Deleting the SNSS state leaves the
    browser nothing to resurrect.

    Files only, not ``Preferences``: session state carries no settings, so
    removing it stays inside the same line :func:`_quiet_browser_profile`
    draws — seeding preferences from outside makes the browser announce
    tampering. Best-effort: a profile we cannot clean still opens a window.
    """
    default = profile / "Default"
    _rmtree(default / _SESSION_RESTORE_DIR, ignore_errors=True)
    for name in _SESSION_RESTORE_FILES:
        with contextlib.suppress(OSError):
            (default / name).unlink(missing_ok=True)


def _migrate_legacy_http_cache(profile: Path) -> None:
    """Drop the app profile's HTTP cache once after the no-store upgrade.

    A response header cannot retroactively evict a 410 that an older release
    already put in Chromium's disk cache.  Because ``--window`` deliberately
    reuses both its profile and (normally) localhost:8765, that old response
    remains an exact cache-key match after upgrading and can hide the healthy
    server even after relocate rebound it successfully.

    Only ``Default/Cache`` is removed: cookies, preferences, Local Storage and
    every other profile facility survive.  The client now also sends fetches
    with ``cache: no-store``; this migration is the bridge that ensures the new
    SPA shell itself is not held behind a legacy cached response.  Best-effort
    like the neighbouring session cleanup.  Write the marker only when the
    cache is absent, so a Windows file lock gets retried next launch.
    """
    marker = profile / _HTTP_CACHE_MIGRATION_MARKER
    if marker.is_file():
        return
    cache = profile / "Default" / "Cache"
    _rmtree(cache, ignore_errors=True)
    if cache.exists():
        return
    with contextlib.suppress(OSError):
        marker.touch(exist_ok=True)


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

    The browser is found before the flags are built, because which browser it
    is decides where its profile can live (:func:`browser_profile_dir`).
    """
    exe = _find_chromium()
    return None if exe is None else [exe, *_app_window_flags(url, exe)]


def _app_window_flags(url: str, browser_exe: str) -> list[str]:
    """The ``--app=`` flag set for ``browser_exe``. See :func:`_app_window_argv`."""
    flags = [
        f"--app={url}",
        f"--user-data-dir={browser_profile_dir(browser_exe)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--disable-sync",
        # A force-killed browser leaves the profile marked as crashed; without
        # this the next window opens under a "restore pages?" bubble for a
        # session _purge_stale_browser_session has already emptied.
        "--hide-crash-restore-bubble",
    ]
    if sys.platform not in ("win32", "darwin"):
        # Without this the window's WM_CLASS is the browser's own
        # ("Chromium-browser"), so the taskbar files it as a browser window.
        # Windows groups by AppUserModelID and macOS by owning bundle, so the
        # flag has nothing to do there.
        flags.append(f"--class={_LINUX_WM_CLASS}")
        if os.environ.get("DISPLAY"):
            # Native-Wayland Chromium ignores --class (its app_id stays the
            # browser's own — measured on Ubuntu 24.04 / chromium snap 150),
            # so WM_CLASS only works through XWayland. $DISPLAY guards the
            # rare XWayland-less compositor, where forcing x11 would mean no
            # window at all instead of a wrongly-badged one.
            flags.append("--ozone-platform=x11")
    return flags


def _find_chromium() -> str | None:
    """Path to a Chromium-family browser, or None if this box has none."""
    # A shortcut-launched litman inherits the desktop session's PATH, which
    # omits the per-user and Homebrew bin dirs (see refresh_path) — without
    # this a browser installed there is as invisible as an agent CLI was.
    # Imported here, not at module scope, to keep `lit gui`'s startup import
    # graph unchanged.
    from litman.core.agents import refresh_path

    refresh_path()
    for name in _CHROMIUM_CANDIDATES:
        exe = shutil.which(name)
        if exe:
            return exe
    if sys.platform == "darwin":
        # Run the binary inside the bundle rather than `open -na`: `open` asks
        # Launch Services to start the app and returns immediately, so it never
        # owns the window. See _DARWIN_APP_CANDIDATES for the name list.
        for app in _DARWIN_APP_CANDIDATES:
            for root in (_DARWIN_SYSTEM_APPS, Path.home() / "Applications"):
                binary = root / f"{app}.app" / "Contents" / "MacOS" / app
                if binary.exists():
                    return str(binary)
    if sys.platform == "win32":
        # Edge ships with Win10+ but is not always on PATH.
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                exe_path = (
                    Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
                )
                if exe_path.exists():
                    return str(exe_path)
    return None


def _stop_server_when_window_closes(
    proc: subprocess.Popen[bytes] | None,
    server: Any,
    presence: PresenceTracker,
    *,
    on_launch_failed: Callable[[], bool] | None = None,
    first_connect_grace: float = 15.0,
    launch_failure_grace: float = 1.5,
    never_connected_timeout: float = 180.0,
    linger: float = 5.0,
    poll: float = 0.25,
) -> None:
    """Ask uvicorn to shut down once the last live page is gone.

    The authoritative signal is the page, not the browser process: the SPA
    holds a WebSocket open to ``/api/presence`` for as long as it is loaded,
    and ``presence`` counts those sockets. Two shutdown paths:

    * **A page connected and then every page went away.** The window was
      closed or navigated off; stop once the count has stayed zero for
      ``linger`` seconds (an F5 reload drops and re-opens the socket inside
      that window). This fires *regardless of the spawned process* — on
      Windows, Edge (Startup boost, single-instance-per-profile) keeps our
      ``msedge`` resident long after its window is gone, so waiting on the
      process would wedge the gate open forever and leave the server running.

    * **No page ever connected** — a launch that never produced a window.
      ``first_connect_grace`` bounds the wait so a failed launch leaves no
      orphan, but the clock only starts once the spawned process is *gone*:
      a window merely slow to paint (process still alive) must not be shot
      before its first page loads. ``never_connected_timeout`` is the outer
      bound on that patience, because on Windows the process may never exit
      at all (Edge stays resident) — without it, a window that comes up and
      then fails to load a single page keeps the server alive forever, which
      is the same orphan by a different route.

    ``proc`` is *polled*, never waited on. An earlier version blocked on
    ``proc.wait()`` before it ever read presence — which is exactly what let a
    resident Windows browser keep the server alive after the window closed
    (the wait never returned, so the presence loop never ran). ``None`` means
    there is no process to poll (the fallback that opens a tab in the user's
    everyday browser); the page is then the only signal there is.

    ``on_launch_failed`` is that fallback, offered once, at the only moment
    this loop can tell a launch failed: the process is gone with a non-zero
    status and no page ever connected. ``Popen`` succeeding does not mean the
    window came up — the browser can exit on its own a moment later — and
    that gap is not hypothetical: an Ubuntu snap Chromium is confined out of
    the hidden cache dir :func:`browser_profile_dir` hands it and aborts
    rather than corrupt a profile it cannot lock. The status is what
    separates that from the clean exits this gate was built for (a Chromium
    that handed the URL to an instance we never see), so only a crash buys
    the second chance. Returning True means a tab was opened and the watch
    continues against the page alone; False (or no callback) keeps the
    original verdict.

    A crash is also judged on its own, much shorter clock. The 15 seconds of
    ``first_connect_grace`` are patience for a hand-off — an invisible window
    that still has a page to load — and a process that aborted has no page
    coming. Spending them anyway is 15 seconds of a desktop shortcut that
    looks like it did nothing, which is how this bug was reported in the
    first place.

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
    started = time.monotonic()
    exited_at: float | None = None
    while True:
        if exited_at is None and proc is not None and proc.poll() is not None:
            exited_at = time.monotonic()
        # A non-zero exit is a failed launch rather than a hand-off, and gets
        # neither the patience nor the second-chance rules of one.
        crashed = proc is not None and proc.returncode not in (0, None)
        count, ever_connected, last_zero = presence.snapshot()
        if count == 0:
            if ever_connected:
                # The window lived and now no page does — stop a linger later,
                # even if our spawned browser is still resident (Windows keeps
                # it around). last_zero is None only in a torn snapshot mid-
                # connect; treat that as not-idle. Without the guard, None in
                # the subtraction is a TypeError that kills this daemon thread
                # silently and orphans the server.
                if last_zero is not None and time.monotonic() - last_zero >= linger:
                    break
            elif exited_at is not None and (
                time.monotonic() - exited_at
                >= (launch_failure_grace if crashed else first_connect_grace)
            ):
                # No page ever connected and the window process is gone: a
                # launch that never came up. One fallback first, and only on
                # a *non-zero* exit — a browser that aborts is a launch
                # failure Popen reported as success (a confined snap
                # Chromium refusing its --user-data-dir), and the user who
                # installed that browser must not end up worse off than the
                # user who owns none. A clean exit is not that: it either
                # handed the URL to an instance we cannot see or the user
                # shut the window before its page painted, and a tab opening
                # on top of those is the surprise, not the rescue.
                took_over = (
                    crashed and on_launch_failed is not None and on_launch_failed()
                )
                if not took_over:
                    break  # give up, so a failed launch leaves no orphan
                # A tab took over: nothing left to poll, and its page gets
                # the same patience the window got.
                on_launch_failed = None
                proc = None
                exited_at = None
                started = time.monotonic()
            elif time.monotonic() - started >= never_connected_timeout:
                # Same verdict, reached without the process ever exiting: a
                # browser that has been up for minutes without loading one
                # page is not a window that is merely slow to paint.
                break
        time.sleep(poll)
    server.should_exit = True


# Shutdown escalation for a window launch, in seconds from the moment the
# watcher asks the server to stop. Generous, because the normal path returns
# in well under a second and these only ever fire on a wedge.
FORCE_EXIT_AFTER = 10.0
HARD_EXIT_AFTER = 25.0


def _escalate_shutdown(
    server: Any,
    stopped: threading.Event,
    *,
    force_after: float = FORCE_EXIT_AFTER,
    hard_after: float = HARD_EXIT_AFTER,
    hard_exit: Callable[[], None] | None = None,
) -> None:
    """Make sure a requested shutdown actually ends this process.

    ``should_exit`` is a *request*: uvicorn stops accepting, then waits — with
    no timeout of its own — for every open connection and task to finish. Its
    escape hatch, ``force_exit``, is normally reached only by a second Ctrl+C,
    and the launch this whole file is built around (a desktop shortcut running
    the console-less ``litw.exe``) has no console to send one from. One
    connection that never closes therefore leaves a GUI process running with
    no window, no console and nothing the user can reach it by — and because
    Windows will not overwrite a running executable, the next
    ``uv tool install --force`` then fails with a permission error on
    ``litw.exe`` rather than upgrading.

    So: ask, then insist, then leave. ``stopped`` is set by ``gui_cmd``'s
    ``finally`` the instant ``server.run()`` returns, which is how every stage
    normally ends.

    The last stage is ``os._exit`` — deliberately the one exit no event loop
    can hold up. It is safe here because every vault write has already
    completed by the time it could fire: writes are atomic and per-request,
    and this process buffers no state that a graceful teardown would flush.
    Only a ``--window`` launch arms this. A terminal ``lit gui`` keeps the
    plain Ctrl+C contract, where the second Ctrl+C is the user's own force.
    """
    if stopped.wait(force_after):
        return
    server.force_exit = True
    if stopped.wait(max(0.0, hard_after - force_after)):
        return
    if hard_exit is not None:
        hard_exit()
    else:  # pragma: no cover - the shipped default kills the interpreter
        os._exit(0)


# ---------------------------------------------------------------------------
# readiness poll (Part A) + splash hand-off (Part C)
# ---------------------------------------------------------------------------


def _open_when_ready(
    server: Any,
    open_browser: Callable[[], None],
    stop_event: threading.Event,
    *,
    ready_timeout: float = READY_TIMEOUT,
    ready_poll: float = READY_POLL,
    after_open: Callable[[], None] | None = None,
    give_up: Callable[[], bool] | None = None,
) -> None:
    """Open the browser the instant the server is listening — no fixed guess.

    Polls ``server.started`` (uvicorn sets it True once startup finished and
    the socket is listening) every ``ready_poll`` seconds and opens the moment
    it flips — typically 0.3-0.7s, versus the old flat 1s. ``ready_timeout`` is
    the backstop: if startup wedges, open anyway (best effort — the shipped
    "always try to open" behaviour) rather than hang forever.

    ``stop_event`` is the abort. ``gui_cmd``'s ``finally`` sets it, so if
    ``server.run()`` raised before it ever listened, this returns without
    opening a browser onto a dead server. The re-check after the loop covers
    the one-``ready_poll``-wide window where the event lands between the break
    and the open. ``after_open`` runs once, right after the open, for the
    splash hand-off (wait for the page to paint, then close the splash).

    ``give_up`` is polled alongside ``started``: True means readiness is
    never coming — the mac shell hands in "the server thread died" — so stop
    waiting now rather than sit out the timeout. The open still runs, same
    as the timeout backstop; the caller's callback is what decides what
    opening means against a server that never came up (the shell swaps in
    its error page instead of a URL).
    """
    deadline = time.monotonic() + ready_timeout
    while True:
        if stop_event.is_set():
            return
        if getattr(server, "started", False):
            break
        if give_up is not None and give_up():
            break
        if time.monotonic() >= deadline:
            break
        stop_event.wait(ready_poll)
    if stop_event.is_set():
        return
    open_browser()
    if after_open is not None:
        after_open()


def _spawn_ready_watcher(
    server: Any,
    open_browser: Callable[[], None],
    stop_event: threading.Event,
    after_open: Callable[[], None] | None = None,
) -> threading.Thread:
    """Start the readiness poller on its own daemon thread and return it.

    A named seam so ``gui_cmd`` stays readable and the poll behaviour
    (``_open_when_ready``) can be driven directly in tests.
    """
    thread = threading.Thread(
        target=_open_when_ready,
        args=(server, open_browser, stop_event),
        kwargs={"after_open": after_open},
        daemon=True,
    )
    thread.start()
    return thread


def _terminate_splash_when_visible(
    splash_proc: subprocess.Popen[bytes],
    presence: PresenceTracker,
    stop_event: threading.Event,
    *,
    splash_timeout: float = SPLASH_TIMEOUT,
    poll: float = READY_POLL,
) -> None:
    """Close the splash once a page has actually painted.

    The smoothest hand-off holds the splash until the browser's first frame is
    really up — signalled by the SPA opening its ``/api/presence`` socket
    (``ever_connected``) — so the splash never blinks out before the window
    blinks in. Backstops: ``splash_timeout`` (presence never arrives) and
    ``stop_event`` (the server is shutting down). ``terminate`` is idempotent
    and suppressed: other paths may have closed the splash already.
    """
    deadline = time.monotonic() + splash_timeout
    while True:
        if stop_event.is_set():
            break
        if presence.ever_connected:
            break
        if time.monotonic() >= deadline:
            break
        stop_event.wait(poll)
    with contextlib.suppress(OSError):
        splash_proc.terminate()


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


def _repair_launcher_stubs() -> None:
    """Best-effort launcher self-heal at GUI start (win32 only).

    Cleans ``*.exe.old`` leftovers a previous upgrade could not delete and
    copies any missing ``lit.exe``/``litw.exe`` back from the tool venv's own
    scripts dir. Never raises: a repair failure just keeps today's behavior.
    """
    if sys.platform != "win32":
        return
    try:
        from litman.core import launcher_stubs

        for name in launcher_stubs.repair_default():
            console.print(f"[dim]restored missing launcher {name}[/]")
    except Exception:
        pass


def _warn_console_shortcut() -> None:
    """Say so out loud when the shortcut had to target the console ``lit.exe``.

    The silent fallback in :func:`_shortcut_executable` is fine for installs
    that never had ``litw.exe``, but after an upgrade accident it hides real
    breakage: the shortcut pops a console window whose closing kills the GUI.
    """
    if sys.platform != "win32":
        return
    try:
        exe = _shortcut_executable()
    except LitmanError:
        return
    if exe.lower().endswith("litw.exe"):
        return
    console.print(
        "[yellow]warning:[/] litw.exe (the console-less launcher) is missing, "
        "so this shortcut opens a console window — closing that window closes "
        "litman too.\n"
        "Repair: [bold]uv tool install --force litman[/] (or "
        "[bold]pipx reinstall litman[/]), then re-run "
        "[bold]lit gui --make-shortcut[/]."
    )


def _brand_windows_taskbar(stop_event: threading.Event) -> threading.Thread | None:
    """Give the app window litman's taskbar face (win32 only), off-thread.

    The --app window already sits in a taskbar group of its own (Chromium
    derives its AppUserModelID from our URL), but the group wears the
    browser's icon and name — those come from relaunch properties Chromium
    writes onto the window, and rewriting them after the window appears is
    the fix. Mechanism and measurements: :mod:`litman.commands._win_taskbar`.

    Best effort in both directions: daemon thread, and any failure leaves
    the browser's face in place rather than touching the launch.
    """
    if sys.platform != "win32":
        return None

    def worker() -> None:
        try:
            from litman.commands import _win_taskbar

            relaunch = subprocess.list2cmdline(
                [_shortcut_executable(), "gui", "--window"]
            )
            _win_taskbar.adopt_window(
                str(_icon_path("litman.ico")),
                relaunch,
                give_up=stop_event.is_set,
            )
        except Exception:
            pass

    # The worker swallows its own failures, but running out of threads fails
    # the start() itself — same best-effort verdict: no icon, never no window.
    try:
        thread = threading.Thread(target=worker, daemon=True, name="litman-taskbar")
        thread.start()
    except Exception:
        return None
    return thread


def _load_mac_shell() -> Any | None:
    """The pywebview module for the macOS native shell, or None.

    The one seam ``gui_cmd`` reaches the shell through, and the surface the
    tests fake a webview (or its absence) behind. None — a missing pywebview,
    or anything else going wrong in the probe — sends the launch down the
    Chromium route exactly as it always ran, so falling back is the one path
    that needs no new trust.
    """
    try:
        from litman.commands import _mac_shell

        return _mac_shell.load_webview()
    except Exception:
        return None


# Set (to "1") by both exec lines of _DARWIN_STUB, so a process can tell a
# bundle launch — one wearing litman.app's Launch Services identity — from a
# terminal `lit gui --window`, which has no identity to speak of.
_DARWIN_APP_LAUNCH_ENV = "LITMAN_DARWIN_APP_LAUNCH"


def _shed_darwin_app_identity() -> bool:
    """Hand a bundle launch that lost its window to an identity-less child.

    Only reached when the native shell could not put up a window. A bundle
    launch that then serves a *browser* window keeps litman.app's Launch
    Services identity on a process with no window of its own — the exact
    configuration in which a second double-click became an activation
    request nothing could answer, bouncing the Dock icon until macOS called
    litman unresponsive. So the fallback re-launches itself as a detached
    child with the marker stripped and lets this process exit: Launch
    Services sees an app that started and finished (every double-click runs
    the stub afresh), while the child serves the browser window as a plain
    process. The stripped marker is also the recursion gate — a child that
    falls back again finds no marker and serves in place.

    Returns True when the launch was handed off (the caller just returns).
    False keeps the launch here: a terminal launch has no identity to shed,
    and a child that cannot be spawned is no reason to serve nothing —
    running with the residual identity risk beats not running.
    """
    if not os.environ.get(_DARWIN_APP_LAUNCH_ENV):
        return False
    env = dict(os.environ)
    env.pop(_DARWIN_APP_LAUNCH_ENV, None)
    try:
        # Fixed argv, because the marker only ever comes from the stub and
        # this is the stub's exact launch shape (no --library/--port to lose).
        subprocess.Popen(
            [_resolve_lit_executable(), "gui", "--window"],
            env=env,
            start_new_session=True,
        )
    except (OSError, LitmanError):
        return False
    return True


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


def _darwin_bundle_locations() -> tuple[Path, Path]:
    """The (system, user) homes for litman.app, in preference order.

    ``/Applications`` first because Finder's sidebar "Applications" is
    hardwired to it — a bundle in ``~/Applications`` is invisible from the
    one place users look. On a stock Mac the folder is admin-group writable,
    so preferring it costs no escalation.
    """
    return (
        _DARWIN_SYSTEM_APPS / "litman.app",
        Path.home() / "Applications" / "litman.app",
    )


def _is_litman_bundle(bundle: Path) -> bool:
    """True when the bundle is ours — the guard every delete goes through.

    ``/Applications`` is shared territory: nothing there is removed,
    rewritten or migrated away from unless its Info.plist says litman.
    Every bundle this code ever wrote carries the identifier as literal
    ASCII text, so probing bytes is enough — and a foreign bundle's plist
    is usually binary (Xcode's default), which a text read would die on.
    """
    try:
        plist = (bundle / "Contents" / "Info.plist").read_bytes()
    except OSError:
        return False
    return b"io.github.litman" in plist


def _dir_accepts_writes(directory: Path) -> bool:
    """Probe by writing, not by ``os.access``: a non-admin account, MDM
    management and TCC each veto in their own way, and only a real write
    trips them all."""
    if not directory.is_dir():
        return False
    probe = directory / f".litman-write-probe-{os.getpid()}"
    try:
        probe.touch()
    except OSError:
        return False
    with contextlib.suppress(OSError):
        probe.unlink()
    return True


def shortcut_path() -> Path:
    """Where the desktop shortcut lives on this platform.

    Windows lands on the actual Desktop (``%USERPROFILE%\\Desktop``) so the
    icon is visible the moment the installer finishes — the install script
    creates it, and a fresh install is meant to be started by double-clicking
    it, not by running ``lit setup``. macOS prefers ``/Applications`` (see
    :func:`_darwin_bundle_locations`), falling back to ``~/Applications``
    when it cannot write there or when a foreign litman.app holds the slot —
    but an existing install of ours in either home wins over preference,
    because uninstall's removal preview and setup's
    skip-this-step probe both ask this function where the shortcut *is*, and
    answering with the preferred home would silently overlook the other.
    Linux uses the applications menu (a ``.desktop`` file on the Desktop
    would need a manual "trust" step).
    """
    if sys.platform == "win32":
        return _windows_desktop_dir() / "litman.lnk"
    if sys.platform == "darwin":
        system, user = _darwin_bundle_locations()
        for bundle in (system, user):
            if bundle.exists() and _is_litman_bundle(bundle):
                return bundle
        # The system slot held by a foreign litman.app is not ours to answer
        # with: uninstall would preview a stranger's app for deletion, and
        # setup would call the step done and never create anything. The user
        # home takes over — the same verdict _create_shortcut_darwin reaches.
        if system.exists():
            return user
        return system if _dir_accepts_writes(_DARWIN_SYSTEM_APPS) else user
    data_home = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local" / "share"
    )
    return Path(data_home) / "applications" / "litman.desktop"


def create_shortcut() -> tuple[Path, bool]:
    """Create or refresh the desktop shortcut. Returns ``(path, existed)``.

    Idempotent: an existing shortcut is overwritten, never an error.
    """
    lit = _shortcut_executable()
    if sys.platform == "darwin":
        return _create_shortcut_darwin(lit)
    target = shortcut_path()
    existed = target.exists()
    if sys.platform == "win32":
        _write_shortcut_win32(target, lit)
    else:
        _write_shortcut_linux(target, lit)
    return target, existed


def _create_shortcut_darwin(lit: str) -> tuple[Path, bool]:
    """Write litman.app into the preferred home and leave exactly one copy.

    One copy, because a bundle in each home is how a stale stub outlives an
    upgrade: ``--make-shortcut`` rewrites wherever :func:`shortcut_path`
    points, and a second bundle elsewhere keeps launching yesterday's code
    (it happened — a hand-moved bundle in /Applications survived a whole
    day). Whichever home gets written, a litman bundle in the other is
    removed.

    Never escalates. A POSIX write into /Applications without permission is
    a flat EACCES — no password prompt exists on this path; only Finder's
    drag has one. So the fallback names the drag as the way over and leaves
    escalating to the program entitled to it.
    """
    system, user = _darwin_bundle_locations()
    system_preexisted = system.exists()
    existed = system_preexisted or user.exists()
    if system_preexisted:
        # In-place refresh of our own install; a foreign litman.app is not
        # ours to touch, so the user home takes over (without the drag hint —
        # dragging onto a stranger's bundle is no way out).
        target = system if _is_litman_bundle(system) else user
        say_drag = False
    elif _dir_accepts_writes(_DARWIN_SYSTEM_APPS):
        target, say_drag = system, False
    else:
        target, say_drag = user, _DARWIN_SYSTEM_APPS.is_dir()
    try:
        _write_shortcut_darwin(target, lit)
    except OSError:
        if target == user:
            raise
        # The probe passed but the bundle write failed (a raced permission
        # change, a per-bundle veto): clean up the partial copy and fall
        # back rather than die half-installed.
        if not system_preexisted:
            shutil.rmtree(system, ignore_errors=True)
        target, say_drag = user, _DARWIN_SYSTEM_APPS.is_dir()
        _write_shortcut_darwin(target, lit)
    other = user if target == system else system
    if other.exists() and _is_litman_bundle(other):
        shutil.rmtree(other, ignore_errors=True)
    if say_drag:
        console.print(
            "Installed to ~/Applications (no write access to /Applications). "
            "To move it: drag litman.app there in Finder."
        )
    return target, existed


def remove_shortcut() -> Path | None:
    """Delete the desktop shortcut if present. Counterpart to
    :func:`create_shortcut`, used by ``lit uninstall``.

    Returns the path removed, or ``None`` when there was nothing there.
    macOS sweeps both bundle homes — uninstall is a one-shot exit,
    completeness wins — though only bundles :func:`_is_litman_bundle`
    vouches for; the first one actually removed is the return value. The
    Linux ``.desktop`` and Windows ``.lnk`` are single files.
    """
    if sys.platform == "darwin":
        removed: Path | None = None
        for bundle in _darwin_bundle_locations():
            if bundle.exists() and _is_litman_bundle(bundle):
                shutil.rmtree(bundle, ignore_errors=True)
                # Re-check rather than trust the call (the same verdict
                # remove_browser_profile reaches): reporting a bundle gone
                # while it still launches is worse than admitting the miss.
                if removed is None and not bundle.exists():
                    removed = bundle
        return removed
    target = shortcut_path()
    if not target.exists():
        return None
    if target.is_dir():  # a bundle handed in by a patched shortcut_path
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
        f"StartupWMClass={_LINUX_WM_CLASS}\n"
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


# platformdirs' user_log_dir on macOS, spelled in shell because the stub below
# runs before any Python does.
_DARWIN_LOG_DIR = "$HOME/Library/Logs/litman"

# Launched from Finder / Launchpad / the Dock, the bundle has no console, so
# without a log a launch that failed and a launch that is merely slow look
# exactly alike — nothing on screen either way until the browser window shows
# up. The console-less Windows launcher keeps litw.log for the same reason.
# Truncated per launch rather than appended, so it never grows.
#
# The bare `exec` on the last line is the fallback, and it is load-bearing: a
# redirection onto a path the shell cannot open aborts the script, which would
# turn an unwritable log directory into an app that does not start at all. The
# log is a diagnostic; it never gets a vote on whether litman runs.
#
# `exec` itself is load-bearing too: Launch Services identifies the running
# app by the process it started from the bundle, so exec hands litman.app's
# identity to the server process — which is what lets the WKWebView window
# that process opens wear litman's Dock tile (see _mac_shell). Identity
# without a window once deadlocked second launches (an activation request
# arrived at a process that could answer nothing); now the process really
# owns a window and an event loop, so a reopen has somewhere to land.
#
# Both exec lines carry _DARWIN_APP_LAUNCH_ENV (an assignment prefix rides
# into the exec'd program's environment), so the launched process knows it
# wears the bundle's identity. That matters exactly once: a browser-route
# fallback must not keep that identity on a windowless server (see
# _shed_darwin_app_identity) — and a terminal launch, which never comes
# through here, must not shed one it never had.
_DARWIN_STUB = f"""\
#!/bin/sh
LOG_DIR="{{log_dir}}"
if mkdir -p "$LOG_DIR" 2>/dev/null && : >"$LOG_DIR/litman.log" 2>/dev/null; then
    {_DARWIN_APP_LAUNCH_ENV}=1 exec "{{lit}}" gui --window >"$LOG_DIR/litman.log" 2>&1
fi
{_DARWIN_APP_LAUNCH_ENV}=1 exec "{{lit}}" gui --window
"""


def _install_darwin_icon(target: Path) -> str | None:
    """Copy the bundled ``.icns`` into ``target``. Returns the name, or None.

    Without ``CFBundleIconFile`` and this file beside it, the Dock and
    Launchpad draw the generic executable tile — which is what earlier
    installs got. The artwork is the same mark the Windows
    ``.ico`` carries, inset to Apple's icon grid (the rounded body is 824 of
    1024) so it does not sit visibly larger than its neighbours in the Dock.

    Best effort by design: an install missing the asset still deserves a
    working launcher, so a failed copy drops the plist key rather than the
    shortcut.
    """
    source = _icon_path("litman.icns")
    dest = target / "Contents" / "Resources" / source.name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    except OSError:
        return None
    return source.name


def _write_shortcut_darwin(target: Path, lit: str) -> None:
    """Write the ``~/Applications/litman.app`` launcher bundle.

    A minimal bundle — Info.plist, an icon, and an executable shell stub that
    runs ``lit gui --window``. See :data:`_DARWIN_STUB` for why the stub logs
    and :func:`_install_darwin_icon` for the icon.
    """
    macos_dir = target / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    icon = _install_darwin_icon(target)
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
        + (
            f"  <key>CFBundleIconFile</key><string>{icon}</string>\n"
            if icon
            else ""
        )
        + "</dict>\n"
        "</plist>\n",
        encoding="utf-8",
    )
    stub = macos_dir / "litman"
    stub.write_text(
        _DARWIN_STUB.format(lit=lit, log_dir=_DARWIN_LOG_DIR), encoding="utf-8"
    )
    stub.chmod(0o755)
    # Finder and the Dock cache a bundle's icon; the cache is keyed on the
    # bundle and dropped when its modification date moves. Rewriting the files
    # *inside* Contents/ does not move the .app's own date, so an install that
    # already showed the generic tile would keep showing it. Best effort — a
    # refused utime costs an icon, not the shortcut.
    with contextlib.suppress(OSError):
        os.utime(target)


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
        "Open in a standalone app window (no address bar) instead of a "
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

    # A half-failed `uv tool upgrade` can leave a launcher stub missing from
    # the bin dir while the venv still holds a good copy (uv never re-lays
    # entrypoints once its receipt says up to date) — heal that before doing
    # anything that resolves or embeds those launchers.
    _repair_launcher_stubs()

    if make_shortcut:
        target, existed = create_shortcut()
        console.print(
            f"[green]{'updated' if existed else 'created'}[/] desktop "
            f"shortcut: [bold]{target}[/]"
        )
        _warn_console_shortcut()
        return

    # Part C: launch the splash as early as possible — before importing uvicorn
    # and building the app — so it paints while that heavy work runs. Only for a
    # console-less --window launch that has a display: a terminal already gives
    # feedback (its prints are visible), and a headless / ssh -L session must
    # never spawn a window. Its own subprocess, never waited on; a missing
    # tkinter or a failed Popen degrades to no splash and never blocks startup.
    splash_proc: subprocess.Popen[bytes] | None = None
    want_splash = (
        window
        and not no_browser
        and display_available()
        and _launched_without_console()
        # Not macOS. Aqua's Tk ignores overrideredirect, so instead of a
        # floating mark the splash comes up as an ordinary titled window —
        # traffic lights, Tk's default "tk" in the title bar, its own Dock tile
        # and the menu bar to itself. It reads as litman's main window right up
        # until it vanishes and the real one appears somewhere else, which is
        # worse than no splash at all. The feedback it exists to give is already
        # there anyway: Launch Services bounces the Dock icon while the bundle
        # starts, which is exactly what a Windows .lnk does not do.
        and sys.platform != "darwin"
    )
    if want_splash:
        with contextlib.suppress(Exception):
            splash_proc = subprocess.Popen(
                [sys.executable, "-m", "litman.commands._splash"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )

    def _terminate_splash() -> None:
        if splash_proc is not None:
            with contextlib.suppress(OSError):
                splash_proc.terminate()

    # fastapi + uvicorn are core dependencies, so this import normally always
    # succeeds; the guard only fires on a corrupted install, and points at a
    # reinstall rather than a (no-longer-existing) optional extra.
    try:
        import uvicorn
    except ImportError:
        _terminate_splash()
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
    # Config() just (re)configured logging; silence the live-refresh poll's
    # access lines now that the loggers are in their final shape.
    _quiet_polled_access_lines()
    # One-click update (POST /api/self-update) needs three things only this
    # command knows: the uvicorn server (to schedule its own exit), the port
    # (the helper's relaunch race guard probes it), and how to bring THIS kind
    # of session back — the app window again for --window, otherwise the same
    # port so an open tab / SSH tunnel finds the new server where the old one
    # was.
    app.state.uvicorn_server = server
    app.state.self_update_port = actual_port
    try:
        if window:
            relaunch = [_shortcut_executable(), "gui", "--window"]
        else:
            relaunch = [_resolve_lit_executable(), "gui", "--port", str(actual_port)]
            if no_browser:
                relaunch.append("--no-browser")
        app.state.self_update_relaunch = relaunch
    except LitmanError:
        # No resolvable `lit` executable (stripped PATH, unusual embedding):
        # leave the relaunch recipe unset — one-click update then refuses with
        # its manual hint instead of arming a restart that cannot work.
        pass

    # macOS --window: prefer a window this process owns. The Dock badges a
    # window with the owning process's bundle, so a WKWebView here wears
    # litman's own face (the .app stub exec'd us) where the Chromium --app
    # window wore the browser's — and no browser needs to be installed at
    # all. Anything short of a working pywebview falls back to the Chromium
    # route below, byte-for-byte the launch it always was.
    if window and sys.platform == "darwin":
        try:
            shell = _load_mac_shell()
        except Exception:
            # Belt and braces: the seam already swallows its own failures —
            # this catch only survives a broken (or test-patched) seam
            # object itself.
            shell = None
        if shell is not None:
            console.print("[dim]Close the window to stop the server.[/]")
            from litman.commands import _mac_shell

            if _mac_shell.run_shell(shell, server, url, app.state.presence):
                return
            # False: the window layer failed before the server ever ran, so
            # the launch is still ours to make good on below.
        if _shed_darwin_app_identity():
            console.print(
                "[dim]Native window unavailable; relaunching in the "
                "browser.[/]"
            )
            return
        console.print(
            "[dim]Native window unavailable; falling back to the browser.[/]"
        )

    # Signals the readiness poller to stand down: set in `finally` so a server
    # that raised before it ever listened never gets a browser opened onto it.
    stop_event = threading.Event()
    ready_thread: threading.Thread | None = None
    # The app window we spawned, if we spawned one. Appended from the readiness
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

        def _hard_exit() -> None:
            # `finally` never runs after os._exit, so take the window down
            # here rather than leave a shell on screen pointing at a server
            # that is about to stop existing.
            for spawned in owned:
                with contextlib.suppress(OSError):
                    spawned.terminate()
            os._exit(0)

        def _fall_back_to_tab() -> bool:
            """Open a tab for a window that died before it loaded a page.

            The last-resort twin of the `except OSError` arm in `_open`: that
            one catches a browser that would not start, this one a browser
            that started and then quit. Same remedy, because the splash is
            still up and the server is still serving a UI nobody can see.

            Unless the run is already ending: `_hard_exit` and the `finally`
            below terminate the window themselves, and a signalled process
            exits non-zero exactly like a crashed one. Ctrl+C would otherwise
            throw a tab up on its way out — the parting gift nobody asked for.
            """
            if stop_event.is_set():
                return False
            _terminate_splash()
            return webbrowser.open(url)

        def _watch_window(proc: subprocess.Popen[bytes] | None) -> None:
            """Stop the server when the last page goes, and see it through."""
            _stop_server_when_window_closes(
                proc,
                server,
                app.state.presence,
                # Only a launch that spawned something can have failed this
                # way; a tab that never connects has nothing left to fall
                # back to.
                on_launch_failed=_fall_back_to_tab if proc is not None else None,
            )
            # The server may already be down (Ctrl+C raced the window close),
            # in which case there is nothing to escalate against — and arming
            # the kill stage against a finished run is exactly the mistake
            # worth being structural about.
            if stop_event.is_set():
                return
            _escalate_shutdown(server, stop_event, hard_exit=_hard_exit)

        def _start_watcher(proc: subprocess.Popen[bytes] | None) -> None:
            threading.Thread(
                target=_watch_window, args=(proc,), daemon=True
            ).start()

        def _open() -> None:
            if app_argv is None:
                # A plain tab (no Chromium found, or tab mode): no window
                # process to watch, and no page paint will close the splash —
                # so close it now (SF-5).
                _terminate_splash()
                webbrowser.open(url)
                # `--window` still owes the user a way to stop the server, and
                # the tab is the window here. Its launch may well have come
                # from the desktop shortcut, where the console-less litw.exe
                # leaves no Ctrl+C to fall back on — so watch the page even
                # though there is no process to poll alongside it.
                if window:
                    _start_watcher(None)
                return
            try:
                # The same directory _app_window_flags put on the command
                # line: argv[0] is the browser, and for a confined one that
                # is what decides where its profile is allowed to live.
                profile = browser_profile_dir(app_argv[0])
                profile.mkdir(parents=True, exist_ok=True)
                _quiet_browser_profile(profile)
                _purge_stale_browser_session(profile)
                _migrate_legacy_http_cache(profile)
                proc = subprocess.Popen(
                    app_argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except OSError:
                # The browser vanished between the `which` probe and now. A tab
                # is a worse window, but no window at all is worse still. No
                # window to paint, so close the splash now (SF-5); the page
                # gate still applies, for the same reason as above.
                _terminate_splash()
                webbrowser.open(url)
                _start_watcher(None)
                return
            owned.append(proc)
            # Watcher first: closing the window must always stop the server,
            # and the brander is cosmetics — it never gets to stand in front
            # of the lifeline.
            _start_watcher(proc)
            _brand_windows_taskbar(stop_event)

        def _after_open() -> None:
            # Splash hand-off: only a real app window has a page that will hold
            # the presence socket, so wait for it to paint before closing the
            # splash. The tab / default-browser fallbacks in _open already
            # closed it (owned stays empty there); nothing to hold for.
            if splash_proc is None:
                return
            if owned:
                _terminate_splash_when_visible(
                    splash_proc, app.state.presence, stop_event
                )
            else:
                _terminate_splash()

        ready_thread = _spawn_ready_watcher(
            server, _open, stop_event, after_open=_after_open
        )

    try:
        server.run()
    finally:
        # Tell the readiness poller to stand down and wait for it, so a server
        # that raised before listening leaves no thread behind (and never opens
        # a browser onto a dead server).
        stop_event.set()
        if ready_thread is not None:
            ready_thread.join(timeout=READY_TIMEOUT + 1.0)
        # Backstop: close the splash no matter which path brought us here.
        _terminate_splash()
        # The other direction: the server stopped first (Ctrl+C, or a crash),
        # so close the window it was serving rather than leave a dead shell on
        # screen. Safe because the profile is ours alone — there are no other
        # tabs to take down with it. A no-op when the window is already gone.
        for proc in owned:
            with contextlib.suppress(OSError):
                proc.terminate()
