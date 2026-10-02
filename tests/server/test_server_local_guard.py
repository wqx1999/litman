"""``lit gui`` answers only the litman page itself (task-gui-localhost-guard).

Any page open in the user's browser can send requests to the localhost server.
Two checks keep them out: a request must arrive under a loopback name (Host),
which stops DNS rebinding, and a write or WebSocket handshake that carries an
Origin must carry this server's own, which stops cross-site requests. Every
refusal here is paired with the same request from the page passing, so a
refusal can't be an artefact of the request itself.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from starlette.websockets import WebSocketDisconnect

from litman.core import agents, terminal
from litman.core.library import create_vault
from litman.server import create_app
from litman.server.local_guard import host_name, refusal
from tests.server._client import GUI_URL, TestClient

FOREIGN = "https://evil.example"
# What a hostile page sends to dodge the preflight: JSON, labelled as text.
TEXT = "text/plain;charset=UTF-8"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path, name="lib")


def _wait_for(predicate, *, timeout: float = 5.0) -> bool:
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


def _mkdir(client: TestClient, parent: Path, name: str, **headers: str):
    return client.post(
        "/api/fs/mkdir",
        content=json.dumps({"parent": str(parent), "name": name}),
        headers={"Content-Type": TEXT, **headers},
    )


# ---------------------------------------------------------------------------
# Host: the name the request arrives under
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "name"),
    [
        ("127.0.0.1:8765", "127.0.0.1"),
        ("LOCALHOST:9000", "localhost"),
        ("localhost", "localhost"),
        ("[::1]:8765", "::1"),
        ("[::1]", "::1"),
        ("evil.example:8765", "evil.example"),
        ("127.0.0.1:abc", None),
        ("127.0.0.1:\u00b2", None),  # a digit to str.isdigit, not a port
        ("[::1]:\u0663", None),
        ("localhost:", None),
        ("[::1", None),
        ("[::1]x", None),
    ],
)
def test_host_name_takes_the_name_and_rejects_a_malformed_port(
    host: str, name: str | None
) -> None:
    assert host_name(host) == name


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:8765", "localhost:8765", "LOCALHOST:8765", "[::1]:8765", "127.0.0.1:9123"],
)
def test_loopback_names_are_served_on_any_port(vault: Path, host: str) -> None:
    resp = TestClient(create_app(vault)).get("/api/papers", headers={"Host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "rebind.evil.example:8765",
        "localhost.evil.example:8765",
        "127.0.0.1.evil.example:8765",
        "127.0.0.1:abc",
        "[::1",
    ],
)
@pytest.mark.parametrize("path", ["/api/papers", "/api/fs/list", "/"])
def test_a_foreign_host_is_refused_pages_and_api_alike(
    vault: Path, host: str, path: str
) -> None:
    client = TestClient(create_app(vault))
    assert client.get(path).status_code == 200  # the same request, from the page
    resp = client.get(path, headers={"Host": host})
    assert resp.status_code == 403
    assert resp.headers["cache-control"] == "no-store"
    detail = resp.json()["detail"]
    assert "http://127.0.0.1:8765" in detail
    assert len(detail) <= 120


def test_the_refusal_names_the_port_this_server_listens_on(vault: Path) -> None:
    """Not the port the foreign Host claimed, and not the default one."""
    client = TestClient(create_app(vault), base_url="http://127.0.0.1:9123")
    assert client.get("/api/papers").status_code == 200
    resp = client.get("/api/papers", headers={"Host": "rebind.evil.example:8765"})
    assert resp.status_code == 403
    assert "http://127.0.0.1:9123" in resp.json()["detail"]
    assert "8765" not in resp.json()["detail"]


@pytest.mark.parametrize(
    ("method", "headers", "expected"),
    [
        ("GET", [], None),
        ("POST", [], None),
        ("POST", [(b"origin", FOREIGN.encode())], "origin"),
    ],
)
def test_a_request_with_no_host_header(
    method: str, headers: list[tuple[bytes, bytes]], expected: str | None
) -> None:
    """HTTP/1.0 clients and scripts may leave Host out; a browser never does.

    Built by hand: the test client always adds a Host.
    """
    assert refusal({"type": "http", "method": method, "headers": headers}) == expected


def test_a_foreign_host_is_refused_before_the_vault_is_consulted() -> None:
    """With no vault the API answers 409 — but a rebound page learns nothing."""
    client = TestClient(create_app(None))
    assert client.get("/api/papers").status_code == 409
    resp = client.get("/api/papers", headers={"Host": "rebind.evil.example:8765"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Origin: who sent a write
# ---------------------------------------------------------------------------


def test_a_cross_site_mkdir_is_refused_and_creates_nothing(tmp_path: Path) -> None:
    client = TestClient(create_app(None))

    resp = _mkdir(client, tmp_path, "from-another-site", Origin=FOREIGN)
    assert resp.status_code == 403
    assert not (tmp_path / "from-another-site").exists()
    detail = resp.json()["detail"]
    assert "another website" in detail
    assert len(detail) <= 120

    assert _mkdir(client, tmp_path, "from-the-page", Origin=GUI_URL).status_code == 200
    assert (tmp_path / "from-the-page").is_dir()


def test_a_cross_site_agent_launch_is_refused_and_launches_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bodyless POST that needs no preflight at all."""
    spawned: list[list[str]] = []

    def fake_spawn(argv: list[str], cwd: Path) -> bool:
        spawned.append(argv)
        return True

    monkeypatch.setattr(agents, "detect", lambda _spec: True)
    monkeypatch.setattr(terminal, "spawn_terminal", fake_spawn)
    client = TestClient(create_app(vault))

    resp = client.post("/api/agent/launch", headers={"Origin": FOREIGN})
    assert resp.status_code == 403
    assert spawned == []

    assert client.post("/api/agent/launch", headers={"Origin": GUI_URL}).status_code == 200
    assert spawned == [["claude"]]


@pytest.mark.parametrize(
    "origin",
    [
        FOREIGN,
        "http://127.0.0.1:9000",  # a page on another local port
        "https://127.0.0.1:8765",
        "http://localhost:8765",  # not the name this request arrived under
        "null",  # sandboxed iframe, data: URL
    ],
)
def test_any_origin_but_this_server_is_refused(tmp_path: Path, origin: str) -> None:
    client = TestClient(create_app(None))
    assert _mkdir(client, tmp_path, "x", Origin=origin).status_code == 403
    assert not (tmp_path / "x").exists()
    assert _mkdir(client, tmp_path, "x", Origin=GUI_URL).status_code == 200


def test_a_rebound_page_cannot_write_either(tmp_path: Path) -> None:
    """Its Origin equals its Host, so it is same-origin — under a foreign name."""
    client = TestClient(create_app(None))
    rebound = "rebind.evil.example:8765"
    resp = _mkdir(client, tmp_path, "rebound", Host=rebound, Origin=f"http://{rebound}")
    assert resp.status_code == 403
    assert not (tmp_path / "rebound").exists()
    assert _mkdir(client, tmp_path, "rebound", Origin=GUI_URL).status_code == 200


def test_a_cross_site_notes_overwrite_is_refused_and_changes_nothing(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    notes = vault / "papers" / paper_id / "notes.md"
    notes.write_text("my own notes\n", encoding="utf-8")
    client = TestClient(create_app(vault))
    url = f"/api/paper/{paper_id}/notes"

    resp = client.put(url, json={"text": "overwritten"}, headers={"Origin": FOREIGN})
    assert resp.status_code == 403
    assert notes.read_text(encoding="utf-8") == "my own notes\n"

    resp = client.put(url, json={"text": "edited on the page"}, headers={"Origin": GUI_URL})
    assert resp.status_code == 200
    assert "edited on the page" in notes.read_text(encoding="utf-8")


def test_a_cross_site_delete_is_refused_and_the_paper_stays(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    client = TestClient(create_app(vault))

    resp = client.delete(f"/api/paper/{paper_id}", headers={"Origin": FOREIGN})
    assert resp.status_code == 403
    assert (vault / "papers" / paper_id / "metadata.yaml").is_file()

    resp = client.delete(f"/api/paper/{paper_id}", headers={"Origin": GUI_URL})
    assert resp.status_code == 200
    assert not (vault / "papers" / paper_id).exists()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/paper/p1/notes"),
        ("OPTIONS", "/api/paper/p1/notes"),
    ],
)
def test_methods_no_route_answers_are_checked_too(
    vault: Path, method: str, path: str
) -> None:
    """The rule is every method but GET and HEAD, not a list of routes."""
    client = TestClient(create_app(vault))
    resp = client.request(method, path, headers={"Origin": FOREIGN})
    assert resp.status_code == 403
    assert client.request(method, path, headers={"Origin": GUI_URL}).status_code != 403


def test_a_write_with_no_origin_passes(tmp_path: Path) -> None:
    """curl, scripts: not a browser, so not what this check is for."""
    client = TestClient(create_app(None))
    assert _mkdir(client, tmp_path, "from-curl").status_code == 200
    assert (tmp_path / "from-curl").is_dir()


def test_origin_is_matched_against_the_requests_own_host(tmp_path: Path) -> None:
    """A walked-up port, or an ``ssh -L`` tunnel on another local port."""
    client = TestClient(create_app(None))
    resp = _mkdir(
        client, tmp_path, "tunnelled", Host="localhost:9000", Origin="http://localhost:9000"
    )
    assert resp.status_code == 200
    assert (tmp_path / "tunnelled").is_dir()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_cross_site_get_or_head_is_not_checked(vault: Path, method: str) -> None:
    """Their responses are unreadable from another site, and no GET route writes.

    (FastAPI answers HEAD on a GET route with 405 — not this guard's 403.)
    """
    client = TestClient(create_app(vault))
    resp = client.request(method, "/api/papers", headers={"Origin": FOREIGN})
    assert resp.status_code != 403
    assert resp.status_code == client.request(method, "/api/papers").status_code


# ---------------------------------------------------------------------------
# WebSocket: the presence socket that keeps a --window server alive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": GUI_URL},
        {"Host": "localhost:8765", "Origin": "http://localhost:8765"},
        {},
    ],
)
def test_the_pages_own_socket_is_counted_in(vault: Path, headers: dict[str, str]) -> None:
    app = create_app(vault)
    with TestClient(app).websocket_connect("/api/presence", headers=headers):
        assert _wait_for(lambda: app.state.presence.count == 1)


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": FOREIGN},
        {"Origin": "null"},
        {"Host": "rebind.evil.example:8765", "Origin": "http://rebind.evil.example:8765"},
    ],
)
def test_a_foreign_socket_is_refused_and_never_counted(
    vault: Path, headers: dict[str, str]
) -> None:
    app = create_app(vault)
    client = TestClient(app)
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/api/presence", headers=headers),
    ):
        pass
    assert refused.value.code == 1008
    assert app.state.presence.snapshot() == (0, False, None)

    with client.websocket_connect("/api/presence", headers={"Origin": GUI_URL}):
        assert _wait_for(lambda: app.state.presence.count == 1)


def test_a_real_server_refuses_with_403() -> None:
    """Real uvicorn, real clients: what a browser actually gets back.

    The guard closes a refused socket before accepting it; uvicorn has to turn
    that into an HTTP 403 on the handshake, or a refused page would hang.
    """
    import uvicorn
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect as ws_connect

    app = create_app(None)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        assert _wait_for(lambda: server.started, timeout=10), "server never started"
        url = f"ws://127.0.0.1:{port}/api/presence"

        with ws_connect(url, origin=f"http://127.0.0.1:{port}"):
            assert _wait_for(lambda: app.state.presence.count == 1)
        assert _wait_for(lambda: app.state.presence.count == 0)

        with pytest.raises(InvalidStatus) as refused, ws_connect(url, origin=FOREIGN):
            pass
        assert refused.value.response.status_code == 403
        assert app.state.presence.count == 0

        for host, status in [(f"127.0.0.1:{port}", 409), ("rebind.evil.example", 403)]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("GET", "/api/papers", headers={"Host": host})
                assert conn.getresponse().status == status
            finally:
                conn.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
    assert not server_thread.is_alive()
