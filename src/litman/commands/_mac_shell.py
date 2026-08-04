"""macOS native shell for the ``--window`` app window (WKWebView via pywebview).

The Dock badges a window with the bundle of the process that *owns* it. The
Chromium ``--app=`` window is owned by the browser, so a litman launch wore
the browser's face no matter what the launcher did; a WKWebView created in
this very process inherits litman.app's identity through the launcher stub's
``exec`` — and, WebKit being system-supplied, needs no browser installed at
all. Same FastAPI server, same frontend bundle: only the window glass changes.

Two runtime facts shape the orchestration:

- Cocoa insists on owning the process's main thread, and uvicorn skips its
  signal-handler installation when started off the main thread — so the two
  swap places relative to the browser route: the server runs on a background
  thread and ``webview.start()`` blocks the main one.
- WKWebView shows plain white for an unreachable server — there is no
  browser error page here (measured on macOS 26.5). The window therefore
  opens on an inline loading page, swaps to the served app the moment
  uvicorn reports ready, and to an inline error page when it never does.
  Both pages are self-contained by construction: they exist for the moments
  the server cannot serve anything.

pywebview/pyobjc imports stay inside functions, so this module imports (and
the orchestration can be driven with a fake webview) anywhere; ``gui_cmd``
reaches it through the single :func:`litman.commands.gui._load_mac_shell`
seam, and anything short of a working pywebview means the Chromium route
runs exactly as it always has.
"""

from __future__ import annotations

import base64
import contextlib
import threading
from typing import Any

from litman.commands import gui

_WINDOW_TITLE = "litman"
_WINDOW_WIDTH = 1280
_WINDOW_HEIGHT = 860

# Shared shell for the loading and error pages: system font, centered column,
# both color schemes (WKWebView follows the OS appearance).
_STYLE = """\
  html, body { height: 100%; margin: 0; }
  body {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 14px;
    font: 15px/1.45 -apple-system, 'Helvetica Neue', sans-serif;
    color: #6e6e73; background: #ffffff; text-align: center;
  }
  @media (prefers-color-scheme: dark) {
    body { color: #98989d; background: #1e1e1e; }
  }
  img { width: 88px; height: 88px; }
  .spinner {
    width: 20px; height: 20px;
    border: 2.5px solid rgba(128, 128, 128, 0.25);
    border-top-color: currentColor; border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
"""


def _icon_img() -> str:
    """The litman book mark as a self-contained ``<img>``, or nothing.

    A data URI, because these pages are shown exactly when the server cannot
    serve assets. Best effort: a missing icon costs the picture, not the page.
    """
    try:
        raw = gui._icon_path("litman.png").read_bytes()
    except OSError:
        return ""
    encoded = base64.b64encode(raw).decode("ascii")
    return f'<img src="data:image/png;base64,{encoded}" alt="">'


def _page(body: str) -> str:
    icon = _icon_img()
    return (
        "<!doctype html>\n<html>\n<head>\n"
        '<meta charset="utf-8">\n<title>litman</title>\n'
        "<style>\n" + _STYLE + "</style>\n</head>\n<body>\n"
        + (icon + "\n" if icon else "")
        + body
        + "\n</body>\n</html>\n"
    )


def _loading_html() -> str:
    return _page('<div class="spinner"></div>\n<p>Starting litman…</p>')


def _error_html() -> str:
    # One verdict, one way out — relaunching re-runs the whole launcher, and
    # the log the stub keeps is where the diagnosis lives.
    return _page(
        "<p><strong>litman's server didn't start.</strong><br>\n"
        "Close this window and try again.</p>"
    )


def load_webview() -> Any | None:
    """Import pywebview, or None when this install cannot host a window."""
    try:
        import webview
    except Exception:
        return None
    return webview


def _set_dock_icon() -> None:
    """Best-effort Dock artwork for a terminal-launched ``lit gui --window``.

    Outside the bundle there is no identity to inherit, so the Dock shows
    python's white-sheet tile; painting litman's mark on it is the whole
    ambition — the name is not chased, because the product path is the
    double-clicked .app, which needs none of this. Harmless from a bundle
    launch (it re-sets the icon the tile already wears).
    """
    try:
        from AppKit import NSApplication, NSImage

        image = NSImage.alloc().initWithContentsOfFile_(
            str(gui._icon_path("litman.icns"))
        )
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass


def run_shell(
    webview: Any,
    server: Any,
    url: str,
    presence: Any,
    *,
    ready_timeout: float | None = None,
    ready_poll: float | None = None,
) -> bool:
    """Own the window: uvicorn on a background thread, WKWebView on this one.

    ``webview`` is whatever :func:`load_webview` (or a test fake) handed the
    caller — every pywebview call flows through it. ``server`` is the
    constructed-but-not-running ``uvicorn.Server``; ``presence`` the app's
    ``PresenceTracker``.

    Returns False when the window layer failed before the server ever ran —
    the server is untouched and the caller still owns the launch (the
    browser route can make good on it). True once the server has run under
    the shell, however the session ended: even a GUI loop that blew up
    mid-flight ends with the server stopped and its thread collected, and
    there is nothing left for the caller to fall back onto.

    Closing the window is the main exit signal; the page-presence gate from
    the browser route stays armed as the backup (windows can die without
    their closed event — a crashed WebContent process). Both set the same
    idempotent ``should_exit``. The reverse direction is covered too: a
    server that stops on its own (self-update, the presence gate) takes the
    window down with it rather than leaving a dead SPA on screen.
    ``ready_timeout``/``ready_poll`` default to the shipped readiness
    constants; tests inject shorter ones.
    """
    server_thread = threading.Thread(
        target=server.run, name="litman-server", daemon=True
    )
    stop_event = threading.Event()

    def _stop_server(*_args: Any) -> None:
        # Also stands the readiness poller down, so a window closed during
        # startup never gets the URL loaded into a corpse.
        stop_event.set()
        server.should_exit = True

    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
        window = webview.create_window(
            _WINDOW_TITLE,
            html=_loading_html(),
            width=_WINDOW_WIDTH,
            height=_WINDOW_HEIGHT,
        )
        window.events.closed += _stop_server
    except Exception:
        # The window layer failed with the server not yet running: report
        # the launch unconsumed. "pywebview cannot come up" includes this —
        # the quiet-fallback contract is the caller's to honour.
        return False

    def _swap_in_the_app() -> None:
        # _open_when_ready returns on readiness, on its give_up probe or on
        # its timeout backstop; `started` says which side this landed on.
        if getattr(server, "started", False):
            window.load_url(url)
        else:
            window.load_html(_error_html())

    ready_kwargs: dict[str, float] = {}
    if ready_timeout is not None:
        ready_kwargs["ready_timeout"] = ready_timeout
    if ready_poll is not None:
        ready_kwargs["ready_poll"] = ready_poll

    def _drive() -> None:
        # Runs on the thread webview.start() spawns once the GUI loop is up,
        # so load_url/load_html always land on a live window. give_up: a
        # server thread that died before listening has no readiness coming,
        # and the error page must not wait out the full timeout on its
        # account.
        gui._open_when_ready(
            server,
            _swap_in_the_app,
            stop_event,
            give_up=lambda: not server_thread.is_alive(),
            **ready_kwargs,
        )

    def _close_window_when_server_stops() -> None:
        # The server can stop without the window: self-update sets
        # should_exit from inside a request, the presence gate from its
        # backup poll. A dead SPA in a live window — whose process would
        # then double on relaunch — is what the browser route's `finally`
        # terminate prevents; this is the shell's version. A server that
        # never listened is different: its window shows the error page, the
        # only place that verdict lives, so it stays up.
        server_thread.join()
        if not getattr(server, "started", False):
            return
        with contextlib.suppress(Exception):
            window.destroy()

    # The backup exit signal: the same page gate --window has always had,
    # with no process to poll (the window is ours, not a child's).
    threading.Thread(
        target=gui._stop_server_when_window_closes,
        args=(None, server, presence),
        daemon=True,
        name="litman-presence-gate",
    ).start()

    _set_dock_icon()
    server_thread.start()
    threading.Thread(
        target=_close_window_when_server_stops,
        daemon=True,
        name="litman-window-reaper",
    ).start()
    try:
        webview.start(_drive)
    except Exception:
        # The GUI loop itself refused to run, with the server already up:
        # no clean fallback exists, so the cleanup below is the whole
        # remedy and the launch reports consumed. The stub's log is where
        # a bundle launch's diagnosis lives — leave the one breadcrumb.
        gui.console.print("[dim]Native window failed; server stopped.[/]")
    finally:
        # The window is gone (or never came): stop the server and collect
        # its thread. Ask, then insist — and returning is the "leave"
        # stage, because the thread is a daemon and every vault write is
        # atomic and per-request.
        _stop_server()
        server_thread.join(timeout=gui.FORCE_EXIT_AFTER)
        if server_thread.is_alive():
            server.force_exit = True
            server_thread.join(
                timeout=max(0.0, gui.HARD_EXIT_AFTER - gui.FORCE_EXIT_AFTER)
            )
    return True
