"""``/api/presence`` WebSocket tests (task-window-presence-gate AC2 + AC6).

The socket is the page's liveness signal for the ``--window`` shutdown gate:
open while the page is loaded, counted on ``app.state.presence``. Two things
are load-bearing and tested here:

* The socket must work in BOTH degraded server states — welcome mode
  (``create_app(None)``, every vault route 409s) and vault-gone mode (the
  bound vault vanished, 410). ``_guard_vault`` is an ``http`` middleware so
  WebSocket connections bypass it, and that is required, not incidental: the
  welcome window has to keep its server alive like any other page.

* The final test is a REAL end-to-end (inject-seam lesson: an injectable
  default needs one test driving the shipped defaults): real uvicorn, a real
  websockets client, a real already-exited subprocess standing in for Edge's
  first-run self-restart, and the watcher on its factory constants. It holds
  ~1s to prove the server survives the bug scenario, then ~5s of linger after
  the page closes — the runtime is the point, so it is never skipped.

Guarded with ``importorskip`` so the suite still collects without fastapi
(invariant #5).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.commands.gui import _stop_server_when_window_closes
from litman.core.config import CONFIG_FILENAME
from litman.core.library import create_vault
from litman.core.presence import PresenceTracker
from litman.server import create_app


def _wait_for(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ===========================================================================
# the endpoint drives the tracker (AC2)
# ===========================================================================


def test_presence_socket_counts_the_page_in_and_out(tmp_path: Path) -> None:
    app = create_app(create_vault(tmp_path, name="lib"))
    tracker = app.state.presence
    assert isinstance(tracker, PresenceTracker)
    assert tracker.snapshot() == (0, False, None)

    with TestClient(app).websocket_connect("/api/presence"):
        # The handler runs on the test client's portal thread, so both edges
        # are polled rather than asserted instantly.
        assert _wait_for(lambda: tracker.count == 1)
        assert tracker.ever_connected is True

    assert _wait_for(lambda: tracker.count == 0)
    assert tracker.last_zero is not None


def test_two_pages_count_as_two(tmp_path: Path) -> None:
    # The gate follows the LAST live page: an app window plus a hand-opened
    # tab must count as two, so closing the window alone cannot idle it.
    app = create_app(create_vault(tmp_path, name="lib"))
    tracker = app.state.presence
    client = TestClient(app)

    with client.websocket_connect("/api/presence"):
        assert _wait_for(lambda: tracker.count == 1)
        with client.websocket_connect("/api/presence"):
            assert _wait_for(lambda: tracker.count == 2)
        assert _wait_for(lambda: tracker.count == 1)
        assert tracker.last_zero is None
    assert _wait_for(lambda: tracker.count == 0)


def test_presence_works_in_welcome_mode(tmp_path: Path) -> None:
    # No vault was ever served: HTTP routes 409, but the welcome page still
    # needs to keep its server alive — the socket must connect regardless.
    app = create_app(None)
    tracker = app.state.presence
    client = TestClient(app)
    assert client.get("/api/papers").status_code == 409

    with client.websocket_connect("/api/presence"):
        assert _wait_for(lambda: tracker.count == 1)
    assert _wait_for(lambda: tracker.count == 0)


def test_presence_works_when_the_vault_is_gone(tmp_path: Path) -> None:
    # The bound vault vanished mid-session: HTTP routes 410, but the page
    # showing the gone-banner is still a live page.
    vault = create_vault(tmp_path, name="lib")
    app = create_app(vault)
    tracker = app.state.presence
    client = TestClient(app)
    (vault / CONFIG_FILENAME).unlink()
    assert client.get("/api/papers").status_code == 410

    with client.websocket_connect("/api/presence"):
        assert _wait_for(lambda: tracker.count == 1)
    assert _wait_for(lambda: tracker.count == 0)


# ===========================================================================
# the whole gate on shipped defaults (AC6)
# ===========================================================================


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_window_gate_survives_the_browser_handoff_end_to_end() -> None:
    """Everything real, every constant the shipped default.

    The bug scenario replayed against a live server: the spawned process
    exits immediately (Edge's first-run self-restart) while a real WebSocket
    client — the page — stays connected. Before the gate, the server died
    right here; now it must hold for the page and stop only a linger after
    the page goes away.
    """
    import uvicorn
    from websockets.sync.client import connect as ws_connect

    app = create_app(None)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        # The sync websockets client has no retry: connect only once uvicorn
        # actually listens, or the handshake dies on ConnectionRefused.
        assert _wait_for(lambda: server.started, timeout=10), "server never started"

        # Edge's first-run handoff: the process we spawned is already gone.
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()

        watcher = threading.Thread(
            target=_stop_server_when_window_closes,
            args=(proc, server, app.state.presence),  # shipped defaults only
            daemon=True,
        )
        with ws_connect(f"ws://127.0.0.1:{port}/api/presence"):
            watcher.start()
            time.sleep(1.0)
            # Old behavior killed the server the moment proc.wait() returned.
            assert not server.should_exit

        # The page is gone: the shipped 5s linger, then shutdown.
        assert _wait_for(
            lambda: server.should_exit, timeout=5.0 + 3.0
        ), "last page closed but the server was never stopped"
        watcher.join(timeout=5)
        assert not watcher.is_alive()
    finally:
        # Whatever happened above, do not leak a live server into the suite.
        server.should_exit = True
        server_thread.join(timeout=10)
    assert not server_thread.is_alive()
