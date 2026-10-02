"""The test client the webUI server tests talk through.

Starlette's ``TestClient`` addresses the app as ``http://testserver``, so every
request carries ``Host: testserver``. The server answers only under a loopback
name, which a client left on that default would never be. This one addresses
the server the way the page does, at ``http://127.0.0.1:8765``; everything
else is Starlette's client unchanged.

``websocket_connect`` needs its own fix: Starlette joins a WebSocket path onto
a hard-coded ``ws://testserver`` and ignores ``base_url`` there.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit

from fastapi.testclient import TestClient as _StarletteTestClient
from starlette.testclient import WebSocketTestSession

GUI_URL = "http://127.0.0.1:8765"


class TestClient(_StarletteTestClient):
    __test__ = False

    def __init__(self, app: Any, base_url: str = GUI_URL, **kwargs: Any) -> None:
        super().__init__(app, base_url=base_url, **kwargs)

    def websocket_connect(self, url: str, *args: Any, **kwargs: Any) -> WebSocketTestSession:
        base = urlsplit(str(self.base_url))
        ws_base = base._replace(scheme="wss" if base.scheme == "https" else "ws").geturl()
        return super().websocket_connect(urljoin(ws_base, url), *args, **kwargs)
