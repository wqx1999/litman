"""Other users of the same machine cannot use your ``lit gui`` (ADR-029).

On Linux the guard asks the kernel who owns the client's end of each
connection (``peer_owner``) and lets in only the server's own user and root.
A client that hung up before it could be identified is refused too: its
request would still run, so that is how another user would write blind.
Wherever nobody can tell — Windows, macOS, the test client — everything passes
as before.

The rule is pinned on a fake kernel table, the kernel's answer on hand-built
bytes, and the refusal on a guard handed a fake owner. The rest runs the real
lookup against real sockets: a real server lets its own user in on every
platform, and on Linux it refuses requests whose client hung up while it
serves the same request from a client that stays.
"""

from __future__ import annotations

import collections
import errno
import http.client
import json
import os
import socket
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.websockets import WebSocketDisconnect

from litman.core.library import create_vault
from litman.server import create_app, peer_owner
from litman.server.local_guard import LocalPageGuard
from litman.server.peer_owner import GONE, UNKNOWN, Owner, Socket, allowed, owner, parse_reply
from tests.server._client import TestClient

# Read once, at import: whether this platform can be asked at all.
HAS_NETLINK = hasattr(socket, "AF_NETLINK")
ONLY_LINUX = "only Linux says who owns a connection's other end; elsewhere every client passes"

ME = 1000  # the server's own user, in the fake tables
OTHER = 1001
NOBODY = 65534  # another user, seen from inside a user namespace
# Never anyone's real uid: what the real guard takes for another user.
STRANGER = 3_000_000_001
CLIENT = ("127.0.0.1", 41000)
SERVER = ("127.0.0.1", 8765)
ESTABLISHED, FIN_WAIT1, FIN_WAIT2, TIME_WAIT, CLOSE_WAIT, LISTEN = 1, 4, 5, 6, 8, 10
LINUX_AF_INET6 = 10  # the kernel's number, whatever this platform's is

USER_REFUSAL = (
    "This litman server belongs to another user on this machine. "
    "Run `lit gui` to start your own."
)


def _wait_for(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _serving(app: Any) -> Iterator[int]:
    """A real uvicorn serving ``app`` the way ``lit gui`` does; yields its port."""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, proxy_headers=False, log_level="critical"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: server.started), "server never started"
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()


# --- The rule, on a fake kernel table ---------------------------------------


# The server's end of the connection, owned by the server's user: a lookup of
# the wrong end answers with the wrong uid.
MINE = Socket(ESTABLISHED, ME, 78)


def _kernel(peer: Socket | None, own: Socket | None):
    """A lookup that knows the client's end of the connection and the
    server's own."""
    table = {(CLIENT, SERVER): peer, (SERVER, CLIENT): own}

    def lookup(local, remote):
        return table[(local, remote)]

    return lookup


@pytest.mark.parametrize(
    ("peer", "own", "who", "passes"),
    [
        pytest.param(Socket(ESTABLISHED, ME, 77), MINE, ME, True, id="own user"),
        pytest.param(Socket(ESTABLISHED, 0, 77), MINE, 0, True, id="root"),
        pytest.param(Socket(ESTABLISHED, OTHER, 77), MINE, OTHER, False, id="another user"),
        pytest.param(Socket(ESTABLISHED, NOBODY, 77), MINE, NOBODY, False, id="nobody"),
        pytest.param(Socket(FIN_WAIT1, ME, 77), MINE, ME, True, id="half-closed own user"),
        pytest.param(Socket(FIN_WAIT2, 0, 0), MINE, GONE, False, id="closed: orphan as root"),
        pytest.param(Socket(FIN_WAIT1, OTHER, 0), MINE, GONE, False, id="closed: orphan keeps uid"),
        pytest.param(Socket(TIME_WAIT, 0, 0), MINE, GONE, False, id="closed: time-wait"),
        pytest.param(None, MINE, UNKNOWN, True, id="client out of sight"),
        pytest.param(None, Socket(CLOSE_WAIT, ME, 78), GONE, False, id="client gone, end closing"),
        pytest.param(None, None, GONE, False, id="client gone, end gone"),
    ],
)
def test_who_sent_it_and_whether_it_passes(
    peer: Socket | None, own: Socket | None, who: Owner, passes: bool
) -> None:
    found = owner(CLIENT, SERVER, lookup=_kernel(peer, own))
    assert found == who
    assert allowed(found, me=ME) is passes


def test_a_client_reset_before_accept_is_gone() -> None:
    """uvicorn has no address for it, so there is nothing to look up."""

    def lookup(local, remote):
        raise AssertionError("nothing to look up")

    assert owner(None, SERVER, lookup=lookup) == GONE
    assert allowed(GONE, me=ME) is False


@pytest.mark.parametrize(
    ("client", "server"),
    [
        (("testclient", 50000), ["127.0.0.1", 8765]),  # starlette's TestClient
        (CLIENT, None),
    ],
)
def test_what_no_socket_stands_behind_is_unknown(client, server) -> None:
    def lookup(local, remote):
        raise AssertionError("not an address a socket has")

    assert owner(client, server, lookup=lookup) == UNKNOWN
    assert allowed(UNKNOWN, me=ME) is True


@pytest.mark.parametrize("fails_on", ["client end", "server end"])
def test_a_kernel_that_will_not_answer_lets_the_request_through(fails_on: str) -> None:
    def lookup(local, remote):
        if (local, remote) == ((CLIENT, SERVER) if fails_on == "client end" else (SERVER, CLIENT)):
            raise OSError(errno.EPERM, "no netlink for you")
        return None

    assert owner(CLIENT, SERVER, lookup=lookup) == UNKNOWN


def test_without_uids_only_root_and_unknown_pass() -> None:
    """Windows has no uid for the server: no client can be its own user."""
    assert allowed(UNKNOWN, me=None) is True
    assert allowed(0, me=None) is True
    assert allowed(OTHER, me=None) is False
    assert allowed(GONE, me=None) is False


# --- The kernel's answer, on hand-built bytes -------------------------------


def _reply(
    *,
    family: int = socket.AF_INET,
    state: int = ESTABLISHED,
    ports: tuple[int, int] = (CLIENT[1], SERVER[1]),
    uid: int = ME,
    inode: int = 77,
) -> bytes:
    """A ``SOCK_DIAG_BY_FAMILY`` answer: nlmsghdr + inet_diag_msg."""
    loopback = socket.inet_aton("127.0.0.1")
    if family == LINUX_AF_INET6:  # how a dual-stack client's addresses read
        address = b"\0" * 10 + b"\xff\xff" + loopback
    else:
        address = loopback + b"\0" * 12
    message = (
        struct.pack("=BBBB", family, state, 0, 0)
        + struct.pack("!HH", *ports)
        + address
        + address
        + struct.pack("=III", 0, 0x1234, 0)  # interface, cookie
        + struct.pack("=IIIII", 0, 0, 0, uid, inode)  # expires, queues, uid, inode
    )
    return struct.pack("=IHHII", 16 + len(message), 20, 0, 1, 0) + message


def _error(code: int) -> bytes:
    """An ``NLMSG_ERROR`` answer: the negated errno, then the request's header."""
    payload = struct.pack("=i", -code) + bytes(16)
    return struct.pack("=IHHII", 16 + len(payload), 2, 0, 1, 0) + payload


@pytest.mark.parametrize("family", [socket.AF_INET, LINUX_AF_INET6], ids=["ipv4", "dual-stack"])
def test_an_answer_gives_state_uid_and_inode(family: int) -> None:
    reply = _reply(family=family, state=FIN_WAIT1, uid=OTHER, inode=4242)
    assert parse_reply(reply, CLIENT, SERVER) == Socket(FIN_WAIT1, OTHER, 4242)


def test_no_such_socket_is_none() -> None:
    assert parse_reply(_error(errno.ENOENT), CLIENT, SERVER) is None


def test_a_listening_socket_on_the_same_port_is_not_the_one_asked_for() -> None:
    """With nothing connected on those addresses, the kernel answers with the
    socket listening on the local port — for the client's end, another user's
    listener could stand there."""
    as_client = _reply(state=LISTEN, ports=(CLIENT[1], 0), uid=0, inode=55)
    assert parse_reply(as_client, CLIENT, SERVER) is None
    as_server = _reply(state=LISTEN, ports=(SERVER[1], 0), uid=ME, inode=56)
    assert parse_reply(as_server, SERVER, CLIENT) is None


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(_error(errno.EPERM), id="refused"),
        pytest.param(_reply()[:40], id="truncated answer"),
        pytest.param(_error(errno.ENOENT)[:18], id="truncated error"),
        pytest.param(_reply()[:4] + struct.pack("=H", 3) + _reply()[6:], id="another kind"),
    ],
)
def test_an_answer_that_says_nothing_is_an_error(reply: bytes) -> None:
    with pytest.raises(OSError):
        parse_reply(reply, CLIENT, SERVER)

    def lookup(local, remote):
        return parse_reply(reply, local, remote)

    assert owner(CLIENT, SERVER, lookup=lookup) == UNKNOWN


# --- The refusal, from a guard handed a fake owner --------------------------


def _counting_app() -> tuple[Starlette, list[str]]:
    ran: list[str] = []

    async def hit(request: Request) -> PlainTextResponse:
        ran.append(request.method)
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/hit", hit, methods=["GET", "POST"])]), ran


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("who", [STRANGER, NOBODY, GONE])
def test_another_user_is_refused_and_nothing_runs(method: str, who: Owner) -> None:
    app, ran = _counting_app()
    client = TestClient(LocalPageGuard(app, owner=lambda client, server: who))

    response = client.request(method, "/hit")

    assert response.status_code == 403
    assert response.json() == {"detail": USER_REFUSAL}
    assert len(USER_REFUSAL) <= 120
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert ran == []


@pytest.mark.parametrize("who", [0, UNKNOWN])
def test_root_and_unknown_pass(who: Owner) -> None:
    app, ran = _counting_app()
    client = TestClient(LocalPageGuard(app, owner=lambda client, server: who))
    assert client.post("/hit").status_code == 200
    assert ran == ["POST"]


def test_the_test_client_passes_the_real_lookup() -> None:
    """What every TestClient-based server test relies on."""
    app, ran = _counting_app()
    assert TestClient(LocalPageGuard(app)).post("/hit").status_code == 200
    assert ran == ["POST"]


def test_a_foreign_page_hears_about_the_page_first() -> None:
    """Host and Origin are judged before the user: their way out is the one
    that applies to a browser."""
    app, ran = _counting_app()
    client = TestClient(LocalPageGuard(app, owner=lambda client, server: STRANGER))
    response = client.post("/hit", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "another website" in response.json()["detail"]
    assert ran == []


def test_another_users_socket_is_refused_and_never_counted(tmp_path: Path) -> None:
    app = create_app(create_vault(tmp_path, name="lib"))
    refusing = TestClient(LocalPageGuard(app, owner=lambda client, server: STRANGER))
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        refusing.websocket_connect("/api/presence"),
    ):
        pass
    assert refused.value.code == 1008
    assert app.state.presence.snapshot() == (0, False, None)

    passing = TestClient(LocalPageGuard(app, owner=lambda client, server: UNKNOWN))
    with passing.websocket_connect("/api/presence"):
        assert _wait_for(lambda: app.state.presence.count == 1)


# --- The real lookup, on real sockets ---------------------------------------


def test_a_real_server_lets_its_own_user_in(tmp_path: Path) -> None:
    """Every platform: on Linux the lookup finds this process's user, elsewhere
    nobody can tell — either way the page and its writes go through."""
    app = create_app(create_vault(tmp_path, name="lib"))
    with _serving(app) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/api/papers")
            response = conn.getresponse()
            assert response.status == 200, response.read()
            response.read()
            body = json.dumps({"parent": str(tmp_path), "name": "made-by-me"})
            conn.request("POST", "/api/fs/mkdir", body=body)
            response = conn.getresponse()
            assert response.status == 200, response.read()
        finally:
            conn.close()
    assert (tmp_path / "made-by-me").is_dir()


def test_the_real_lookup_finds_the_clients_own_socket() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        server = listener.getsockname()
        with socket.create_connection(server) as client_end:
            accepted, client = listener.accept()
            with accepted:
                if HAS_NETLINK:
                    # The inode is the client socket's own: the client's end
                    # was found, not the server's.
                    found = peer_owner.sock_diag(client, server)
                    inode = os.fstat(client_end.fileno()).st_ino
                    assert found == Socket(ESTABLISHED, os.geteuid(), inode)
                    assert owner(client, server) == os.geteuid()
                else:
                    assert owner(client, server) == UNKNOWN


def _post(port: int, path: str) -> bytes:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", path)
        return conn.getresponse().read()
    finally:
        conn.close()


@pytest.mark.skipif(not HAS_NETLINK, reason=ONLY_LINUX)
@pytest.mark.parametrize("checked", [True, False], ids=["guard", "no hang-up check"])
def test_a_real_server_runs_no_request_whose_client_hung_up(checked: bool) -> None:
    """The same write, from a client that stays for the answer and from ones
    that leave at once — by closing, or by resetting the connection.

    The server is kept busy while those clients send and leave, so it looks at
    each request only once its client is gone, and none of them runs. (A
    client still there when its request is looked at is identified instead —
    here that is this process's own user, rightly let in.) With an owner that
    cannot tell anyone, the same requests do run: that is what the check on
    hang-ups stops.
    """
    ran: list[str] = []
    handled: list[str] = []
    busy, free = threading.Event(), threading.Event()

    async def write(request: Request) -> PlainTextResponse:
        ran.append(request.url.path.split("/")[1])
        return PlainTextResponse("written")

    async def hold(request: Request) -> PlainTextResponse:
        busy.set()
        free.wait(10)  # blocks the event loop: nothing is accepted or read
        return PlainTextResponse("held")

    inner = Starlette(
        routes=[
            Route("/hold", hold, methods=["POST"]),
            Route("/{how}/{n}", write, methods=["POST"]),
        ]
    )
    guard = LocalPageGuard(inner) if checked else LocalPageGuard(inner, owner=lambda c, s: UNKNOWN)

    async def app(scope, receive, send) -> None:
        try:
            await guard(scope, receive, send)
        finally:
            if scope["type"] == "http":
                handled.append(scope["path"].split("/")[1])

    with _serving(app) as port:
        holder = threading.Thread(target=_post, args=(port, "/hold"), daemon=True)
        holder.start()
        assert busy.wait(10), "the hold request never arrived"
        for how in ("closes", "resets"):
            for n in range(20):
                with socket.create_connection(("127.0.0.1", port)) as sock:
                    sock.sendall(
                        f"POST /{how}/{n} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                        "Content-Length: 0\r\n\r\n".encode()
                    )
                    if how == "resets":  # linger 0: close sends RST
                        linger = struct.pack("ii", 1, 0)
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        free.set()
        holder.join(10)
        assert _post(port, "/stays/0") == b"written"
        # Every request reached the guard: nothing was lost on the way.
        assert _wait_for(lambda: len(handled) == 42), collections.Counter(handled)
    assert collections.Counter(handled) == {"hold": 1, "stays": 1, "closes": 20, "resets": 20}
    if checked:
        assert ran == ["stays"]
    else:
        assert collections.Counter(ran) == {"stays": 1, "closes": 20, "resets": 20}


@pytest.mark.skipif(not HAS_NETLINK, reason=ONLY_LINUX)
def test_a_dual_stack_client_is_found_too() -> None:
    """A browser may connect from an IPv6 socket to ``::ffff:127.0.0.1``; the
    server, bound to IPv4, sees it as ``127.0.0.1``."""
    try:
        client_end = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("IPv6 is off on this machine: no dual-stack client can exist")
    with client_end, socket.create_server(("127.0.0.1", 0)) as listener:
        server = listener.getsockname()
        client_end.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        client_end.connect(("::ffff:127.0.0.1", server[1]))
        accepted, client = listener.accept()
        with accepted:
            assert client[0] == "127.0.0.1"
            found = peer_owner.sock_diag(client, server)
            inode = os.fstat(client_end.fileno()).st_ino
            assert found == Socket(ESTABLISHED, os.geteuid(), inode)
            assert owner(client, server) == os.geteuid()
