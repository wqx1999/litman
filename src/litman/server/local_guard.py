"""Answer only the litman page itself (task-gui-localhost-guard, ADR-026).

Binding ``127.0.0.1`` keeps other machines out, not other web pages: any page
open in the user's browser can send requests to ``http://127.0.0.1:8765``, and
the server would take them for the user's own clicks. There are two ways in,
and one check for each:

* **Cross-site requests.** A foreign page cannot read our responses (the
  server sends no CORS headers), but a "simple" request — a POST with no body,
  or with a ``text/plain`` one — reaches the route without a preflight, and the
  route acts on it. ``request.json()`` never looks at Content-Type, so JSON
  wrapped as ``text/plain`` gets through, and ``POST /api/agent/launch`` needs
  no body at all. A browser puts ``Origin`` on every request whose method is
  not GET or HEAD, and on every WebSocket handshake, so those are refused when
  their Origin is not this server. GET and HEAD are not checked: a foreign
  page cannot read what they return, and no GET route writes anything — keep
  it that way, or this check stops covering it.
* **DNS rebinding.** A hostile domain that re-resolves to ``127.0.0.1`` turns
  its page into a same-origin one, which can read every response and use every
  method. What gives it away is the name it arrives under: ``Host`` carries
  that domain. The server is only ever reached as ``127.0.0.1``, ``localhost``
  or ``[::1]``, so any other name is refused, for pages and API alike.

The Origin is compared with the request's own Host, not with a fixed address:
Host has already passed as a loopback name, and the two agreeing is exactly
"same origin" — whichever port the server walked up to, and whichever local
port an ``ssh -L`` tunnel forwards. A page served on another local port is
refused like any other site. A request with no Origin (curl, a script) or no
Host is not from a browser, which is the only thing this guards against, and
passes.

A pure ASGI middleware rather than ``@app.middleware("http")``: that kind never
sees WebSocket traffic, and pages reach ``/api/presence`` too.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# Not Origin-checked: another site cannot read what they return, and no route
# writes on them.
_UNCHECKED_METHODS = frozenset({"GET", "HEAD"})

# Policy violation: the close code a refused WebSocket handshake carries.
_WS_POLICY_VIOLATION = 1008


def _header(scope: Scope, name: bytes) -> str | None:
    """The first value of header ``name`` (lowercase bytes), or ``None``."""
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _is_port(text: str) -> bool:
    """ASCII digits only — ``str.isdigit`` alone also takes ``²`` and the like."""
    return text.isascii() and text.isdigit()


def host_name(host: str) -> str | None:
    """The name part of a ``Host`` value, lowercased; ``None`` when malformed.

    ``127.0.0.1:8765`` gives ``127.0.0.1`` and ``[::1]:8765`` gives ``::1``. The
    port is dropped, never judged — but a port that is not a number makes the
    whole value malformed rather than letting the text before it pass.
    """
    host = host.strip().lower()
    if host.startswith("["):
        name, bracket, rest = host[1:].partition("]")
        if not bracket or (rest and not (rest[:1] == ":" and _is_port(rest[1:]))):
            return None
        return name
    name, colon, port = host.partition(":")
    if colon and not _is_port(port):
        return None
    return name


def refusal(scope: Scope) -> str | None:
    """``"host"`` or ``"origin"`` — which check this request fails — or ``None``."""
    host = _header(scope, b"host")
    if host is not None and host_name(host) not in _LOOPBACK_NAMES:
        return "host"
    if scope["type"] == "http" and scope["method"] in _UNCHECKED_METHODS:
        return None
    origin = _header(scope, b"origin")
    if origin is None:
        return None
    if origin.strip().lower() != "http://" + (host or "").strip().lower():
        return "origin"
    return None


class LocalPageGuard:
    """Refuse requests that did not come from the litman page (module docstring)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        reason = refusal(scope)
        if reason is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # Closing before accepting refuses the handshake (HTTP 403), so the
            # presence route never runs and never counts the page in.
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})
            return
        if reason == "host":
            server = scope.get("server")
            port = server[1] if server else None
            address = f"http://127.0.0.1:{port}" if port else "http://127.0.0.1"
            detail = f"litman answers only at 127.0.0.1 or localhost. Open {address}."
        else:
            detail = "Only the litman page itself can send this request, not another website."
        response = JSONResponse(
            status_code=403,
            content={"detail": detail},
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
