"""Live-page presence counter backing the ``--window`` shutdown gate.

The desktop app window (``lit gui --window``) must stop the server when the
last live page goes away — and "the process we spawned exited" is not that
signal: Chromium browsers restart themselves across process boundaries (a
fresh profile's first run hands the real window to a different process), so
the spawned process can die while the window lives on. The reliable signal is
the page itself: the SPA holds a WebSocket open to ``/api/presence`` for as
long as it is loaded, and this tracker counts those connections.

One tracker per app, created by ``litman.server.create_app`` and stashed on
``app.state.presence``. Two threads touch it: the WebSocket handler
(uvicorn's event-loop thread) calls :meth:`connect` / :meth:`disconnect`,
and the window watcher in ``commands/gui.py`` polls :meth:`snapshot` from
its own thread — hence the lock.

Lives in ``litman.core`` and imports nothing beyond the stdlib on purpose:
``commands/gui.py`` sits on the CLI's import path, which must stay
fastapi-free (invariant #5), and the server package imports fastapi at
module scope.
"""

from __future__ import annotations

import threading
import time


class PresenceTracker:
    """Thread-safe count of live pages, plus the history the watcher needs.

    Beyond the raw count, the shutdown gate needs two facts: whether any page
    *ever* connected (a browser that never came up gets a first-connect grace
    period instead of an idle-since clock), and *when* the count last hit
    zero (so an F5 reload — disconnect, then reconnect a moment later — does
    not read as "the last page closed").
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._ever_connected = False
        self._last_zero: float | None = None

    def connect(self) -> None:
        """A page came up: count it, and clear any idle-since mark."""
        with self._lock:
            self._count += 1
            self._ever_connected = True
            self._last_zero = None

    def disconnect(self) -> None:
        """A page went away; when it was the last one, start the idle clock.

        A duplicate disconnect clamps to zero rather than raising — the
        watcher must keep polling no matter how a connection's teardown got
        double-counted, and a negative count would wedge the gate open.
        """
        with self._lock:
            self._count = max(0, self._count - 1)
            if self._count == 0:
                self._last_zero = time.monotonic()

    def snapshot(self) -> tuple[int, bool, float | None]:
        """(count, ever_connected, last_zero) read under one lock hold.

        The watcher reads all three per poll, and must get them as one
        consistent view: read as separate properties, a connect landing
        between reads yields ``ever_connected=True`` with ``last_zero=None``
        torn *across* polls rather than within one — snapshot confines that
        transient to a single poll, which the watcher treats as not-idle.
        """
        with self._lock:
            return (self._count, self._ever_connected, self._last_zero)

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def ever_connected(self) -> bool:
        with self._lock:
            return self._ever_connected

    @property
    def last_zero(self) -> float | None:
        with self._lock:
            return self._last_zero
