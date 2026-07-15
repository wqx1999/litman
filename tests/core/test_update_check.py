"""Unit tests for the PyPI update-check core (task-self-update D1).

Network is ALWAYS mocked (red line: no test may depend on pypi.org). Two shapes
are exercised: a successful GET returning a higher version, and a failing GET
(timeout / URLError) that must resolve to ``None`` silently. The cache is
isolated by the autouse ``_isolate_registry`` fixture in ``tests/conftest.py``
(``$LITMAN_REGISTRY_DIR`` → a per-test temp dir), so ``cache_path()`` — derived
from ``registry_path().parent`` — lands there.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from litman.core import update_check


@pytest.fixture(autouse=True)
def _pin_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the current version so these version-comparison tests stay hermetic
    across release bumps. The product reads ``litman.__version__`` live (via
    ``update_check._current_version``), so the forged-cache ``latest`` values
    and the ``1.1.0`` literals below are all relative to this pin, not to
    whatever version happens to be shipping."""
    monkeypatch.setattr("litman.__version__", "1.1.0")


@pytest.fixture(autouse=True)
def _clear_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the opt-out env is off unless a test sets it explicitly."""
    monkeypatch.delenv(update_check.OPT_OUT_ENV, raising=False)


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _mock_urlopen_ok(monkeypatch: pytest.MonkeyPatch, version: str) -> list[float]:
    """Patch urlopen to return ``{"info": {"version": version}}``.

    Returns a list the fake appends each call's ``timeout`` to, so a test can
    assert the ≤2s ceiling and the call count.
    """
    body = json.dumps({"info": {"version": version}}).encode("utf-8")
    calls: list[float] = []

    def _open(url: str, timeout: float | None = None) -> _FakeResp:
        calls.append(timeout if timeout is not None else -1.0)
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", _open)
    return calls


def _mock_urlopen_fail(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch urlopen to raise (network down). Returns a hit-counter list."""
    calls: list[int] = []

    def _open(url: str, timeout: float | None = None) -> _FakeResp:
        calls.append(1)
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", _open)
    return calls


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# version comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "latest, current, expected",
    [
        ("1.1.1", "1.1.0", True),
        ("1.2.0", "1.1.0", True),
        ("2.0.0", "1.9.9", True),
        ("1.1.0", "1.1.0", False),
        ("1.0.0", "1.1.0", False),
        ("1.1.10", "1.1.9", True),  # numeric, not lexical
        ("1.10.0", "1.9.0", True),
        ("1.1", "1.1.0", False),  # zero-pad shorter → equal, not newer
        ("1.1.0.1", "1.1.0", True),
    ],
)
def test_is_newer(latest: str, current: str, expected: bool) -> None:
    assert update_check.is_newer(latest, current) is expected


# ---------------------------------------------------------------------------
# network fetch (mocked)
# ---------------------------------------------------------------------------


def test_fetch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_urlopen_ok(monkeypatch, "9.9.9")
    assert update_check._fetch_latest_version() == "9.9.9"
    # timeout ceiling honoured (≤2s).
    assert calls and calls[0] <= 2.0


def test_fetch_network_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: a failing GET resolves to None, never raises."""
    _mock_urlopen_fail(monkeypatch)
    assert update_check._fetch_latest_version() is None


def test_fetch_malformed_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _open(url: str, timeout: float | None = None) -> _FakeResp:
        return _FakeResp(b"not json")

    monkeypatch.setattr("urllib.request.urlopen", _open)
    assert update_check._fetch_latest_version() is None


# ---------------------------------------------------------------------------
# refresh_cache_if_stale
# ---------------------------------------------------------------------------


def test_refresh_writes_cache_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_urlopen_ok(monkeypatch, "9.9.9")
    update_check.refresh_cache_if_stale()

    cache = update_check.read_cache()
    assert cache is not None
    assert cache["latest"] == "9.9.9"
    assert "checked_at" in cache


def test_refresh_skips_network_when_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: a fresh cache (checked_at within TTL) fires no request."""
    update_check._write_cache(
        {"checked_at": _iso_days_ago(0.1), "latest": "9.9.9"}
    )
    calls = _mock_urlopen_ok(monkeypatch, "9.9.9")
    update_check.refresh_cache_if_stale()
    assert calls == []  # no network


def test_refresh_fetches_when_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    update_check._write_cache(
        {"checked_at": _iso_days_ago(2), "latest": "1.1.0"}
    )
    calls = _mock_urlopen_ok(monkeypatch, "9.9.9")
    update_check.refresh_cache_if_stale()
    assert len(calls) == 1
    assert update_check.read_cache()["latest"] == "9.9.9"


def test_refresh_records_attempt_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed fetch still stamps checked_at, so the TTL throttles retries."""
    _mock_urlopen_fail(monkeypatch)
    update_check.refresh_cache_if_stale()
    cache = update_check.read_cache()
    assert cache is not None
    assert "checked_at" in cache
    assert "latest" not in cache  # nothing to record

    # A second refresh within the TTL must not re-hit the network.
    calls = _mock_urlopen_fail(monkeypatch)
    update_check.refresh_cache_if_stale()
    assert calls == []


def test_refresh_opt_out_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3: opt-out → zero network, zero cache write."""
    monkeypatch.setenv(update_check.OPT_OUT_ENV, "1")
    calls = _mock_urlopen_ok(monkeypatch, "9.9.9")
    update_check.refresh_cache_if_stale()
    assert calls == []
    assert not update_check.cache_path().exists()


# ---------------------------------------------------------------------------
# available_update (GUI read)
# ---------------------------------------------------------------------------


def test_available_update_when_newer() -> None:
    update_check._write_cache({"checked_at": _iso_days_ago(0), "latest": "9.9.9"})
    assert update_check.available_update() == ("1.1.0", "9.9.9")


def test_available_update_when_current() -> None:
    update_check._write_cache({"checked_at": _iso_days_ago(0), "latest": "1.1.0"})
    assert update_check.available_update() is None


def test_available_update_no_cache() -> None:
    assert update_check.available_update() is None


def test_available_update_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    update_check._write_cache({"checked_at": _iso_days_ago(0), "latest": "9.9.9"})
    monkeypatch.setenv(update_check.OPT_OUT_ENV, "1")
    assert update_check.available_update() is None


# ---------------------------------------------------------------------------
# consume_nudge (CLI, frequency-capped)
# ---------------------------------------------------------------------------


def test_consume_nudge_fires_then_caps() -> None:
    """AC2: due once, then capped for 24h via last_nudged_at."""
    update_check._write_cache({"checked_at": _iso_days_ago(0), "latest": "9.9.9"})

    first = update_check.consume_nudge()
    assert first == ("1.1.0", "9.9.9")
    assert "last_nudged_at" in update_check.read_cache()

    second = update_check.consume_nudge()
    assert second is None


def test_consume_nudge_none_when_current() -> None:
    update_check._write_cache({"checked_at": _iso_days_ago(0), "latest": "1.0.0"})
    assert update_check.consume_nudge() is None


def test_consume_nudge_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    update_check._write_cache({"checked_at": _iso_days_ago(0), "latest": "9.9.9"})
    monkeypatch.setenv(update_check.OPT_OUT_ENV, "1")
    assert update_check.consume_nudge() is None


def test_consume_nudge_refires_after_ttl() -> None:
    """A last_nudged_at older than 24h re-arms the nudge."""
    update_check._write_cache(
        {
            "checked_at": _iso_days_ago(0),
            "latest": "9.9.9",
            "last_nudged_at": _iso_days_ago(2),
        }
    )
    assert update_check.consume_nudge() == ("1.1.0", "9.9.9")


# ---------------------------------------------------------------------------
# cache location honours $LITMAN_REGISTRY_DIR
# ---------------------------------------------------------------------------


def test_cache_path_under_registry_dir(tmp_path: Path) -> None:
    from litman.core.vault_registry import registry_path

    assert update_check.cache_path() == registry_path().parent / "update-check.json"
