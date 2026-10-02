"""The test client the webUI server tests talk through.

Starlette's ``TestClient`` addresses the app as ``http://testserver``, so every
request carries ``Host: testserver``. The server answers only under a loopback
name, which a client left on that default would never be. This one addresses
the server the way the page does, at ``http://127.0.0.1:8765``; everything
else is Starlette's client unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient as _StarletteTestClient

GUI_URL = "http://127.0.0.1:8765"


class TestClient(_StarletteTestClient):
    __test__ = False

    def __init__(self, app: Any, base_url: str = GUI_URL, **kwargs: Any) -> None:
        super().__init__(app, base_url=base_url, **kwargs)
