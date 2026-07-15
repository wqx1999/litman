"""Page-presence WebSocket endpoint (``/api/presence``).

The SPA opens this socket on load and holds it for the page's lifetime; the
server side does nothing but count it on ``app.state.presence`` (see
:class:`litman.core.presence.PresenceTracker`). The ``--window`` watcher in
``commands/gui.py`` reads that count to decide when the last live page has
gone — the browser *process* exiting is not that signal, because Chromium
hands windows across processes.

A WebSocket over a JS timer heartbeat, deliberately: Chrome throttles timers
in hidden/minimized windows down to about once a minute, so a POST heartbeat
from a minimized window goes quiet and the server would kill a live page.
Socket disconnect is a network event and protocol ping/pong lives in the
browser's network stack — neither runs on the throttled timer queue.

This route must NOT be gated on a usable vault, and is not: ``_guard_vault``
in ``litman.server`` is an ``http`` middleware, which WebSocket connections
bypass. That is load-bearing — the welcome page (no vault yet) and the
vault-gone banner both need to keep the server alive exactly like any other
page.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(prefix="/api")


@router.websocket("/presence")
async def presence(websocket: WebSocket) -> None:
    """Count the page in while its socket is open, out when it closes.

    No messages are exchanged in either direction: the connection itself is
    the signal. The receive loop exists only to park the handler until the
    peer goes away; whatever a client might send is drained and ignored.
    """
    tracker = websocket.app.state.presence
    await websocket.accept()
    tracker.connect()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        tracker.disconnect()
