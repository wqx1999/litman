"""Tests for ``GET /api/version`` (task-self-update D4, AC6).

The route is PURE READ: it reports the current litman version plus the latest
available release strictly from the local update-check cache — never fetching
PyPI in the request path. A forged cache with ``latest`` > current makes the
route report an available update (the data path behind the TopBar badge). With
no cache (AC1: net-down / never-refreshed) the route still returns cleanly with
``latest`` null.

The cache is isolated by the conftest ``_isolate_registry`` fixture. Tests use a
bare ``TestClient(create_app(...))`` (no ``with`` block), so the app's lifespan
startup refresh never runs — nothing touches the network.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from litman.core import update_check
from litman.core.library import create_vault
from litman.server import create_app
from tests.server._client import TestClient


@pytest.fixture(autouse=True)
def _pin_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the current version so the ``/api/version`` assertions stay hermetic
    across release bumps. ``get_version`` reads ``litman.__version__`` live, so
    the ``1.1.0`` literals below are relative to this pin."""
    monkeypatch.setattr("litman.__version__", "1.1.0")


@pytest.fixture(autouse=True)
def _clear_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(update_check.OPT_OUT_ENV, raising=False)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(create_vault(tmp_path)))


def test_version_no_cache_returns_current_and_null_latest(tmp_path: Path) -> None:
    """AC1: no cache → 200 with current set and latest null (no crash)."""
    resp = _client(tmp_path).get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] == "1.1.0"
    assert body["latest"] is None


def test_version_reports_available_update(tmp_path: Path) -> None:
    """AC6: a forged cache with latest > current → route reports the update."""
    update_check._write_cache({"checked_at": "2999-01-01T00:00:00+00:00", "latest": "9.9.9"})
    resp = _client(tmp_path).get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] == "1.1.0"
    assert body["latest"] == "9.9.9"


def test_version_hides_when_current_is_latest(tmp_path: Path) -> None:
    """Cache latest == current → no update (latest null)."""
    update_check._write_cache({"checked_at": "2999-01-01T00:00:00+00:00", "latest": "1.1.0"})
    resp = _client(tmp_path).get("/api/version")
    assert resp.json()["latest"] is None


def test_version_route_does_not_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request path must never hit the network (pure cache read)."""
    def _boom(**kw: object) -> str:
        raise AssertionError("version route must not fetch PyPI")

    monkeypatch.setattr(update_check, "_fetch_latest_version", _boom)
    resp = _client(tmp_path).get("/api/version")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# the server's own background check
# ---------------------------------------------------------------------------


def _wait_for(cond: Callable[[], object], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def test_version_says_checking_until_the_startup_check_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page reads `checking` to know a null `latest` may still change:
    true while the background check is out, false once it has an answer."""
    release = threading.Event()
    seen: list[dict[str, object]] = []

    def _refresh(**kw: object) -> None:
        seen.append(kw)
        release.wait(5)
        update_check._write_cache(
            {"checked_at": "2999-01-01T00:00:00+00:00", "latest": "9.9.9"}
        )

    monkeypatch.setattr(update_check, "refresh_cache_if_stale", _refresh)
    with _client(tmp_path) as client:
        assert _wait_for(lambda: seen)
        during = client.get("/api/version").json()
        release.set()
        assert _wait_for(lambda: not client.get("/api/version").json()["checking"])
        after = client.get("/api/version").json()

    assert during["checking"] is True and during["latest"] is None
    assert after["checking"] is False and after["latest"] == "9.9.9"
    # Off-thread, so it may wait on a slow link, and retries a failure hourly.
    assert seen[0] == {
        "timeout": update_check.GUI_NETWORK_TIMEOUT_SECONDS,
        "retry_after": update_check.GUI_RETRY_SECONDS,
    }
    assert update_check.GUI_NETWORK_TIMEOUT_SECONDS > update_check.NETWORK_TIMEOUT_SECONDS
    assert update_check.GUI_RETRY_SECONDS == 3600


def test_the_server_asks_again_while_it_runs_and_stops_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GUI left open for days still learns of a release: the check repeats
    every GUI_RETRY_SECONDS (an hour; shortened here) until the server stops."""
    calls: list[int] = []
    monkeypatch.setattr(
        update_check, "refresh_cache_if_stale", lambda **kw: calls.append(1)
    )
    monkeypatch.setattr(update_check, "GUI_RETRY_SECONDS", 0.02)
    with _client(tmp_path):
        assert _wait_for(lambda: len(calls) >= 3)
    stopped_at = len(calls)
    time.sleep(0.2)
    assert len(calls) <= stopped_at + 1  # one already past the wait may land


def test_a_bare_client_reports_not_checking(tmp_path: Path) -> None:
    """No lifespan ran, so nothing is out on the network."""
    assert _client(tmp_path).get("/api/version").json()["checking"] is False
