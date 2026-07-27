"""Tests for ``POST /api/self-update`` (task-one-click-update).

The endpoint mirrors ``lit self-update``'s refusals (editable install / no
uv-pipx owner → 409 with the human hint as ``detail``), and on the happy path
spawns the detached helper and schedules the server's own exit. The helper
itself is exercised in ``tests/core/test_self_update_helper.py``; here its
spawn is intercepted at the module seam to keep the server tests hermetic.

The failure-flag surfacing (helper wrote ``self-update-failed`` → next server
run reports it once via ``GET /api/version``) runs through the REAL lifespan
(``with TestClient(...)``), network-off via the update-check opt-out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core import self_update_helper
from litman.core.library import create_vault
from litman.core.update_check import OPT_OUT_ENV
from litman.server import create_app


@pytest.fixture(autouse=True)
def _not_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev tree IS an editable install — neutralize the probe by default
    so each test states its own installer situation explicitly."""
    monkeypatch.setattr(
        "litman.commands.self_update._is_editable_install", lambda: False
    )


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(create_vault(tmp_path)))


def _arm_session(client: TestClient, relaunch: list[str] | None) -> None:
    """Stand in for what ``lit gui`` stashes on app.state."""
    app = client.app
    app.state.self_update_port = 8765  # type: ignore[attr-defined]
    if relaunch is not None:
        app.state.self_update_relaunch = relaunch  # type: ignore[attr-defined]


def test_editable_install_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "litman.commands.self_update._is_editable_install", lambda: True
    )
    client = _client(tmp_path)
    _arm_session(client, ["lit", "gui"])
    resp = client.post("/api/self-update")
    assert resp.status_code == 409
    assert "development" in resp.json()["detail"]


def test_unknown_installer_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("litman.commands.self_update._detect_installer", lambda: None)
    client = _client(tmp_path)
    _arm_session(client, ["lit", "gui"])
    resp = client.post("/api/self-update")
    assert resp.status_code == 409
    assert "pip install --upgrade litman" in resp.json()["detail"]


def test_no_gui_session_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server not started by `lit gui` has no relaunch recipe → 409."""
    monkeypatch.setattr("litman.commands.self_update._detect_installer", lambda: "uv")
    client = _client(tmp_path)  # nothing armed
    resp = client.post("/api/self-update")
    assert resp.status_code == 409
    assert "lit self-update" in resp.json()["detail"]


def test_happy_path_spawns_helper_and_schedules_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("litman.commands.self_update._detect_installer", lambda: "uv")
    spawns: list[dict[str, Any]] = []

    def fake_spawn(**kwargs: Any) -> Path:
        spawns.append(kwargs)
        return tmp_path / "helper.sh"

    monkeypatch.setattr(self_update_helper, "write_and_spawn_helper", fake_spawn)
    monkeypatch.setattr("litman.server.routes_update.EXIT_DELAY_S", 0.01)

    client = _client(tmp_path)
    _arm_session(client, ["lit", "gui", "--window"])

    class _FakeServer:
        should_exit = False

    fake_server = _FakeServer()
    client.app.state.uvicorn_server = fake_server  # type: ignore[attr-defined]

    resp = client.post("/api/self-update")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "updating"
    assert body["installer"] == "uv"

    [spawn] = spawns
    assert spawn["upgrade_cmd"] == ["uv", "tool", "upgrade", "litman"]
    assert spawn["relaunch_cmd"] == ["lit", "gui", "--window"]
    assert spawn["port"] == 8765
    assert spawn["pid"]  # the server's own pid

    _wait_until(lambda: fake_server.should_exit)


def test_failed_flag_surfaces_once_via_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Helper left self-update-failed → the NEXT server run reports it through
    /api/version and consumes the flag (real lifespan, network off)."""
    monkeypatch.setenv(OPT_OUT_ENV, "1")  # lifespan refresh: no network
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "reg"))
    flag = self_update_helper.fail_flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("upgrade command failed; see self-update.log", encoding="utf-8")

    with TestClient(create_app(create_vault(tmp_path))) as client:
        body = client.get("/api/version").json()
        assert body["selfUpdateFailed"].startswith("upgrade command failed")
    assert not flag.exists()  # consumed: surfaced once

    # A fresh server run with no flag reports null.
    (tmp_path / "v2").mkdir()
    with TestClient(create_app(create_vault(tmp_path / "v2"))) as client:
        assert client.get("/api/version").json()["selfUpdateFailed"] is None


def _wait_until(cond, timeout: float = 5.0):  # type: ignore[no-untyped-def]
    import time

    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        time.sleep(0.02)
