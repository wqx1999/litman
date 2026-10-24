"""Tests for ``litman.core.gui_registry`` — how the CLI finds a running GUI.

HTTP goes through ``httpx.MockTransport`` handed in as ``client=``, routed by
port, so no test needs a network. Two tests talk to the real ping route: one
through the in-process test client, and one through the default client
(``client=None``) against a real uvicorn server on an ephemeral port — the
injectable default exercised for real once.

The registry dir is the per-test ``$LITMAN_REGISTRY_DIR`` (tests/conftest.py).
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from litman import __version__
from litman.core import gui_registry
from litman.core.gui_registry import (
    GuiServer,
    find_gui,
    gui_dir,
    host_id,
    other_host_guis,
    register_gui,
    unregister_gui,
)
from litman.core.library import create_vault
from litman.core.portable_link import make_portable_link

OLD = 3600.0  # seconds: long past the startup grace


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _plant(
    *,
    port: int,
    pid: int,
    host: str | None = None,
    started_ago: float = OLD,
    name: str | None = None,
    vault: Path | None = None,
) -> Path:
    """A record as another GUI process would have written it."""
    folder = gui_dir()
    folder.mkdir(parents=True, exist_ok=True)
    host = host_id() if host is None else host
    path = folder / (name or f"{host}-{pid}.json")
    path.write_text(
        json.dumps(
            {
                "app": "litman",
                "version": "1.3.8",
                "host": host,
                "pid": pid,
                "port": port,
                "vault": str(vault.resolve()) if vault is not None else None,
                "started": time.time() - started_ago,
            }
        ),
        encoding="utf-8",
    )
    return path


Handler = Callable[[httpx.Request], httpx.Response]


def _client(routes: dict[int, Handler], seen: list[int] | None = None) -> httpx.Client:
    """A client whose requests go to ``routes[port]``; an unknown port fails
    the test outright."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/api/si/ping"
        port = request.url.port
        assert port is not None
        if seen is not None:
            seen.append(port)
        assert port in routes, f"pinged an unexpected port {port}"
        return routes[port](request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _alive(
    pid: int, vault: Path | None, pages: int = 0, host: str | None = None
) -> Handler:
    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "app": "litman",
                "version": "1.3.8",
                "pid": pid,
                "host": host_id() if host is None else host,
                "vault": str(vault) if vault is not None else None,
                "pages": pages,
            },
        )

    return answer


def _raise(exc: Exception) -> Handler:
    def answer(_request: httpx.Request) -> httpx.Response:
        raise exc

    return answer


def _status(code: int, **kwargs: Any) -> Handler:
    return lambda _request: httpx.Response(code, **kwargs)


# ---------------------------------------------------------------------------
# register / unregister
# ---------------------------------------------------------------------------


def test_register_writes_a_record_named_for_host_and_pid(vault: Path) -> None:
    path = register_gui(8766, vault)

    assert path.parent == gui_dir()
    assert gui_dir().parent == Path(os.environ["LITMAN_REGISTRY_DIR"])
    assert path.name == f"{gui_registry._file_host(host_id())}-{os.getpid()}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert set(record) == {"app", "version", "host", "pid", "port", "vault", "started"}
    assert record["app"] == "litman"
    assert record["version"] == __version__
    assert record["host"] == host_id()
    assert record["pid"] == os.getpid()
    assert record["port"] == 8766
    assert record["vault"] == str(vault.resolve())
    assert abs(record["started"] - time.time()) < 60
    assert [p.name for p in gui_dir().iterdir()] == [path.name]  # no temp left


def test_register_without_a_vault(tmp_path: Path) -> None:
    path = register_gui(8765, None)
    assert json.loads(path.read_text(encoding="utf-8"))["vault"] is None


def test_host_in_the_file_name_is_made_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gui_registry, "host_id", lambda: "node 7/../x:" + "y" * 80)
    path = register_gui(8765, None)
    host_part = path.name[: -len(f"-{os.getpid()}.json")]
    assert len(host_part) == 64
    assert set(host_part) <= set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    assert path.parent == gui_dir()
    # The record keeps the name as it is: that is what the ping answers with.
    assert json.loads(path.read_text(encoding="utf-8"))["host"].startswith("node 7/")


def test_host_id_is_the_lowercased_hostname() -> None:
    assert host_id() == socket.gethostname().strip().lower()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_record_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    path = register_gui(8765, None)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unregister_removes_the_record_and_the_empty_folder(tmp_path: Path) -> None:
    path = register_gui(8765, None)
    unregister_gui(path)
    assert not path.exists()
    assert not gui_dir().exists()
    unregister_gui(path)  # idempotent
    unregister_gui(None)


def test_unregister_keeps_the_folder_while_others_are_in_it(tmp_path: Path) -> None:
    other = _plant(port=9000, pid=1)
    path = register_gui(8765, None)
    unregister_gui(path)
    assert not path.exists()
    assert other.exists()


# ---------------------------------------------------------------------------
# find_gui
# ---------------------------------------------------------------------------


def test_find_gui_returns_the_live_gui_for_this_library(vault: Path) -> None:
    _plant(port=9001, pid=11)
    client = _client({9001: _alive(11, vault, pages=2)})

    found = find_gui(vault, client=client)

    assert found == GuiServer(
        port=9001, pid=11, pages=2, vault=vault, url="http://127.0.0.1:9001"
    )


def test_find_gui_skips_a_gui_on_another_library(vault: Path, tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    elsewhere = create_vault(tmp_path / "other")
    record = _plant(port=9001, pid=11)
    client = _client({9001: _alive(11, elsewhere, pages=1)})
    assert find_gui(vault, client=client) is None
    assert record.exists()


def test_find_gui_skips_a_gui_serving_no_library(vault: Path) -> None:
    record = _plant(port=9001, pid=11)
    assert find_gui(vault, client=_client({9001: _alive(11, None, pages=1)})) is None
    assert record.exists()


def test_find_gui_matches_the_library_through_a_link(
    vault: Path, tmp_path: Path
) -> None:
    alias = tmp_path / "alias-of-the-library"
    if not make_portable_link(alias, vault):
        pytest.skip("this filesystem cannot hold folder links")
    _plant(port=9001, pid=11)
    client = _client({9001: _alive(11, vault.resolve(), pages=1)})
    found = find_gui(alias, client=client)
    assert found is not None and found.port == 9001


def test_refused_within_the_startup_grace_is_kept(vault: Path) -> None:
    """The record is written before the server listens: a CLI asking in that
    moment must not delete a GUI that is still coming up."""
    record = _plant(port=9001, pid=11, started_ago=5)
    client = _client({9001: _raise(httpx.ConnectError("refused"))})
    assert find_gui(vault, client=client) is None
    assert record.exists()


def test_refused_after_the_grace_is_deleted(vault: Path) -> None:
    record = _plant(port=9001, pid=11)
    client = _client({9001: _raise(httpx.ConnectError("refused"))})
    assert find_gui(vault, client=client) is None
    assert not record.exists()
    assert not gui_dir().exists()


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.RemoteProtocolError("odd"),
    ],
    ids=["read-timeout", "connect-timeout", "other-transport-error"],
)
def test_a_slow_or_odd_answer_never_deletes(vault: Path, error: Exception) -> None:
    """A busy GUI that answers late is alive; deleting its record would hide
    it from every later CLI call."""
    record = _plant(port=9001, pid=11)
    assert find_gui(vault, client=_client({9001: _raise(error)})) is None
    assert record.exists()


@pytest.mark.parametrize(
    "answer",
    [
        _status(404),
        _status(302, headers={"Location": "http://127.0.0.1:9001/login"}),
        _status(200, text="<html>some other web app</html>"),
        _status(200, json={"app": "jupyter"}),
        _status(200, json=["litman"]),
        _alive(99, None),  # another litman, another pid
        _alive(11, None, host="other-node"),
    ],
    ids=["404", "redirect", "html", "not-litman", "not-an-object", "pid", "host"],
)
def test_something_else_on_the_port_means_the_record_is_dead(
    vault: Path, answer: Handler
) -> None:
    record = _plant(port=9001, pid=11, started_ago=1)
    assert find_gui(vault, client=_client({9001: answer})) is None
    assert not record.exists()


@pytest.mark.parametrize("code", [409, 410, 500, 503])
def test_other_statuses_keep_the_record(vault: Path, code: int) -> None:
    record = _plant(port=9001, pid=11)
    assert find_gui(vault, client=_client({9001: _status(code)})) is None
    assert record.exists()


def test_another_hosts_record_is_never_pinged_nor_deleted(vault: Path) -> None:
    """127.0.0.1 on another node of a shared home is another machine."""
    theirs = _plant(port=9001, pid=11, host="some-other-node")
    mine = _plant(port=9002, pid=12)
    seen: list[int] = []
    client = _client({9002: _alive(12, vault, pages=1)}, seen)

    found = find_gui(vault, client=client)

    assert seen == [9002]
    assert found is not None and found.port == 9002
    assert theirs.exists() and mine.exists()


def test_an_unreadable_record_is_skipped_and_kept(vault: Path) -> None:
    folder = gui_dir()
    folder.mkdir(parents=True)
    junk = folder / "junk.json"
    junk.write_text("{not json", encoding="utf-8")
    partial = folder / "partial.json"
    partial.write_text(json.dumps({"app": "litman", "port": 9001}), encoding="utf-8")
    other_file = folder / "notes.txt"
    other_file.write_text("not a record", encoding="utf-8")

    assert find_gui(vault, client=_client({})) is None
    assert junk.exists() and partial.exists() and other_file.exists()


@pytest.mark.parametrize(
    "started",
    ["NaN", "Infinity", "-Infinity", "1" + "0" * 400, '"yesterday"', "true"],
    ids=["nan", "inf", "-inf", "too-big-for-a-float", "text", "bool"],
)
def test_a_record_with_an_unusable_start_time_is_skipped_and_kept(
    vault: Path, started: str
) -> None:
    """JSON lets NaN, Infinity and huge integers through: such a record is
    not pinged (the client refuses any port), not named, and not deleted."""
    folder = gui_dir()
    folder.mkdir(parents=True)
    records = []
    for host in (host_id(), "node-b"):
        path = folder / f"{host}-11.json"
        library = json.dumps(str(vault.resolve()))
        path.write_text(
            f'{{"app": "litman", "version": "1.3.8", "host": {json.dumps(host)}, '
            f'"pid": 11, "port": 9001, "vault": {library}, "started": {started}}}',
            encoding="utf-8",
        )
        records.append(path)

    assert find_gui(vault, client=_client({})) is None
    assert other_host_guis(vault) == []
    assert all(path.exists() for path in records)


def test_a_gui_with_a_page_wins_then_the_newest(vault: Path) -> None:
    _plant(port=9001, pid=11, started_ago=300)  # pages, oldest
    _plant(port=9002, pid=12, started_ago=10)  # no page, newest
    _plant(port=9003, pid=13, started_ago=100)  # pages, newer
    client = _client(
        {
            9001: _alive(11, vault, pages=1),
            9002: _alive(12, vault, pages=0),
            9003: _alive(13, vault, pages=3),
        }
    )
    found = find_gui(vault, client=client)
    assert found is not None and found.port == 9003

    _plant(port=9003, pid=13, started_ago=100)
    client = _client(
        {
            9001: _alive(11, vault, pages=0),
            9002: _alive(12, vault, pages=0),
            9003: _alive(13, vault, pages=0),
        }
    )
    found = find_gui(vault, client=client)
    assert found is not None and found.port == 9002 and found.pages == 0


def test_no_folder_means_no_gui(vault: Path) -> None:
    assert not gui_dir().exists()
    assert find_gui(vault, client=_client({})) is None


def test_the_default_client_ignores_proxies_and_dials_127_0_0_1(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy from the environment (HTTP_PROXY, the system's) would carry
    127.0.0.1 to the proxy host; localhost may resolve to ::1."""
    _plant(port=9001, pid=11)
    built: dict[str, Any] = {}
    hosts: list[str] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return _alive(11, vault, pages=1)(request)

    def client_factory(**kwargs: Any) -> httpx.Client:
        built.update(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")

    found = find_gui(vault)

    assert found is not None and found.port == 9001
    assert built["trust_env"] is False
    assert built["timeout"].connect == 3.0
    assert built["timeout"].read == 2.0
    assert hosts == ["127.0.0.1"]


# ---------------------------------------------------------------------------
# other_host_guis
# ---------------------------------------------------------------------------


def test_other_host_guis_names_this_librarys_guis_elsewhere_newest_first(
    vault: Path, tmp_path: Path
) -> None:
    (tmp_path / "other").mkdir()
    elsewhere = create_vault(tmp_path / "other")
    older = _plant(port=9001, pid=11, host="node-a", vault=vault, started_ago=600)
    newer = _plant(port=9002, pid=12, host="node-b", vault=vault, started_ago=60)
    planted = [
        older,
        newer,
        _plant(port=9003, pid=13, vault=vault),  # this host
        _plant(port=9004, pid=14, host="node-c", vault=elsewhere),
        _plant(port=9005, pid=15, host="node-d"),  # serves no library
    ]
    junk = gui_dir() / "junk.json"
    junk.write_text("{not json", encoding="utf-8")

    found = other_host_guis(vault)

    assert [host for host, _started in found] == ["node-b", "node-a"]
    assert abs(found[0][1] - (time.time() - 60)) < 60
    # Read only: nothing is deleted, however stale.
    assert all(path.exists() for path in planted)
    assert junk.exists()


def test_other_host_guis_matches_the_library_through_a_link(
    vault: Path, tmp_path: Path
) -> None:
    alias = tmp_path / "alias-of-the-library"
    if not make_portable_link(alias, vault):
        pytest.skip("this filesystem cannot hold folder links")
    _plant(port=9001, pid=11, host="node-a", vault=vault)
    assert [host for host, _ in other_host_guis(alias)] == ["node-a"]


def test_other_host_guis_without_records(vault: Path) -> None:
    assert not gui_dir().exists()
    assert other_host_guis(vault) == []


# ---------------------------------------------------------------------------
# Against the real ping route
# ---------------------------------------------------------------------------


def test_find_gui_through_the_real_ping_route(vault: Path) -> None:
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    client = TestClient(create_app(vault))
    record = register_gui(8765, vault)

    found = find_gui(vault, client=client)
    assert found is not None
    assert (found.port, found.pid, found.pages) == (8765, os.getpid(), 0)

    with client.websocket_connect("/api/presence"):
        found = find_gui(vault, client=client)
        assert found is not None and found.pages == 1
    assert record.exists()


def test_find_gui_with_the_default_client_against_a_real_server(
    vault: Path,
) -> None:
    """The injectable default, for real: httpx's own client, a real uvicorn
    socket on 127.0.0.1, and a closed port that refuses."""
    pytest.importorskip("uvicorn")
    import uvicorn

    from litman.server import create_app

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(vault),
            host="127.0.0.1",
            port=0,
            lifespan="off",
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            assert thread.is_alive(), "uvicorn stopped before it listened"
            assert time.monotonic() < deadline, "uvicorn never started"
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        dead = _plant(port=closed_port, pid=os.getpid() + 1)
        live = register_gui(port, vault)

        found = find_gui(vault)

        assert found is not None
        assert (found.port, found.pid, found.pages) == (port, os.getpid(), 0)
        assert found.url == f"http://127.0.0.1:{port}"
        assert live.exists()
        assert not dead.exists()
    finally:
        server.should_exit = True
        thread.join(timeout=20)
    assert not thread.is_alive()
