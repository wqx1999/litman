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
import sys
import threading
import urllib.error
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from litman.core import update_check

# Taken before the autouse fixture below replaces it, for the one test of it.
_REAL_UV_RECEIPT_PATH = update_check.uv_receipt_path


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


@pytest.fixture(autouse=True)
def _no_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the uv receipt at a file that does not exist yet, so a test run
    from inside a `uv tool` environment still takes the PyPI path. Tests that
    want an install index write this file."""
    receipt = tmp_path / "uv-receipt.toml"
    monkeypatch.setattr(update_check, "uv_receipt_path", lambda: receipt)
    return receipt


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:  # type: ignore[exit-return]  # a fake; never swallows
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
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


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


# ---------------------------------------------------------------------------
# the index uv installed litman from
# ---------------------------------------------------------------------------

_TUNA = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _receipt(options: str) -> str:
    return (
        "[tool]\n"
        'requirements = [{ name = "litman" }]\n'
        "entrypoints = [\n"
        '    { name = "lit", install-path = "/h/.local/bin/lit", from = "litman" },\n'
        "]\n" + options
    )


@pytest.mark.parametrize(
    "options, expected",
    [
        # What the China installers leave behind (UV_DEFAULT_INDEX), verbatim.
        (
            "\n[tool.options]\n"
            f'index = [{{ url = "{_TUNA}", explicit = false, default = true, '
            'format = "simple", authenticate = "auto" }]\n',
            _TUNA,
        ),
        # Receipts written before uv had `index`.
        (f'\n[tool.options]\nindex-url = "{_TUNA}"\n', _TUNA),
        # A plain `uv tool install litman`: no options at all.
        ("", None),
        # An extra index is not where litman came from; PyPI stays the default.
        (
            "\n[tool.options]\n"
            'index = [{ url = "https://example.org/simple", explicit = false, '
            "default = false }]\n",
            None,
        ),
    ],
    ids=["default-index", "legacy-index-url", "no-options", "extra-index-only"],
)
def test_install_index_reads_the_receipt(
    _no_receipt: Path, options: str, expected: str | None
) -> None:
    _no_receipt.write_text(_receipt(options), encoding="utf-8")
    assert update_check.install_index() == expected


def test_install_index_none_without_a_readable_receipt(_no_receipt: Path) -> None:
    assert update_check.install_index() is None  # pipx, pip, a dev checkout
    _no_receipt.write_text("this is [not toml", encoding="utf-8")
    assert update_check.install_index() is None


def test_the_receipt_is_looked_for_beside_the_interpreter() -> None:
    """The real default behind the seam the other tests replace: uv writes the
    receipt into the tool environment's root, which is sys.prefix there (seen
    on Linux, and on Windows with a non-ASCII profile path)."""
    assert _REAL_UV_RECEIPT_PATH() == Path(sys.prefix) / "uv-receipt.toml"


_JSON_PAGE = json.dumps(
    {
        "name": "litman",
        "files": [
            {"filename": "litman-1.3.8-py3-none-any.whl", "yanked": False},
            {"filename": "litman-1.3.9.tar.gz"},
            {"filename": "litman-1.3.10-py3-none-any.whl"},
            {"filename": "litman-1.4.0rc1-py3-none-any.whl"},
            {"filename": "litman-1.5.0-py3-none-any.whl", "yanked": "broken"},
        ],
    }
).encode("utf-8")

_HTML_PAGE = (
    b"<!DOCTYPE html>\n<html><body><h1>Links for litman</h1>\n"
    b'<a href="../a/litman-1.3.8-py3-none-any.whl#sha256=00">'
    b"litman-1.3.8-py3-none-any.whl</a><br/>\n"
    b'<a href="../b/litman-1.3.10.tar.gz#sha256=11" data-requires-python="&gt;=3.12">'
    b"litman-1.3.10.tar.gz</a><br/>\n"
    b'<a href="../c/litman-1.4.0rc1-py3-none-any.whl#sha256=22">'
    b"litman-1.4.0rc1-py3-none-any.whl</a><br/>\n"
    b'<a href="../d/litman-1.5.0-py3-none-any.whl#sha256=33" data-yanked="">'
    b"litman-1.5.0-py3-none-any.whl</a><br/>\n"
    b"</body></html>\n"
)


@pytest.mark.parametrize("page", [_JSON_PAGE, _HTML_PAGE], ids=["json", "html"])
def test_simple_page_versions_skip_yanked_and_prereleases(page: bytes) -> None:
    versions = update_check._simple_page_versions(page)
    assert max(versions, key=update_check._parse_version) == "1.3.10"
    assert "1.5.0" not in versions
    assert all("rc" not in v for v in versions)


class _Index:
    """A package index on 127.0.0.1: answers ``/simple/litman/`` with JSON when
    asked for it (TUNA, PyPI) or HTML only (Aliyun), and records each request."""

    def __init__(self, *, json_ok: bool) -> None:
        self.requests: list[tuple[str, str]] = []
        index = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                accept = self.headers.get("Accept", "")
                index.requests.append((self.path, accept))
                if self.path != "/simple/litman/":
                    self.send_error(404)
                    return
                as_json = json_ok and "application/vnd.pypi.simple.v1+json" in accept
                body = _JSON_PAGE if as_json else _HTML_PAGE
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/vnd.pypi.simple.v1+json" if as_json else "text/html",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/simple"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reach 127.0.0.1 directly even where a proxy is configured."""
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")


@pytest.fixture(params=[True, False], ids=["json-index", "html-index"])
def index(request: pytest.FixtureRequest, no_proxy: None) -> Iterator[_Index]:
    served = _Index(json_ok=request.param)
    yield served
    served.close()


def test_the_install_index_is_asked_not_pypi(
    index: _Index, _no_receipt: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing faked below urllib: the receipt names a mirror, the newest
    release is read off its Simple API page, and PyPI is never asked."""
    _no_receipt.write_text(
        _receipt(f'\n[tool.options]\nindex = [{{ url = "{index.url}", default = true }}]\n'),
        encoding="utf-8",
    )
    pypi = _Index(json_ok=True)
    monkeypatch.setattr(update_check, "PYPI_JSON_URL", pypi.url + "/litman/json")
    try:
        update_check.refresh_cache_if_stale()
    finally:
        pypi.close()

    cache = update_check.read_cache()
    assert cache is not None and cache["latest"] == "1.3.10"
    assert "failed" not in cache
    assert [path for path, _ in index.requests] == ["/simple/litman/"]
    assert "application/vnd.pypi.simple.v1+json" in index.requests[0][1]
    assert pypi.requests == []
    assert update_check.available_update() == ("1.1.0", "1.3.10")


def test_an_unreachable_install_index_is_not_swapped_for_pypi(
    _no_receipt: Path, monkeypatch: pytest.MonkeyPatch, no_proxy: None
) -> None:
    """A release the mirror cannot serve is one `lit self-update` cannot
    install, so a mirror that does not answer means no answer, not PyPI's."""
    gone = _Index(json_ok=True)
    gone_url = gone.url
    gone.close()  # nothing listens there any more
    _no_receipt.write_text(
        _receipt(f'\n[tool.options]\nindex = [{{ url = "{gone_url}", default = true }}]\n'),
        encoding="utf-8",
    )
    pypi = _Index(json_ok=True)
    monkeypatch.setattr(update_check, "PYPI_JSON_URL", pypi.url + "/litman/json")
    try:
        assert update_check._fetch_latest_version() is None
    finally:
        pypi.close()
    assert pypi.requests == []


# ---------------------------------------------------------------------------
# a failed check is retried sooner when the caller asks
# ---------------------------------------------------------------------------


def test_a_failed_check_is_marked_and_a_success_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_urlopen_fail(monkeypatch)
    update_check.refresh_cache_if_stale()
    assert update_check.read_cache()["failed"] is True

    update_check._write_cache(
        {"checked_at": _iso_days_ago(2), "failed": True}
    )
    _mock_urlopen_ok(monkeypatch, "9.9.9")
    update_check.refresh_cache_if_stale()
    cache = update_check.read_cache()
    assert cache["latest"] == "9.9.9"
    assert "failed" not in cache


def test_a_failed_check_is_retried_after_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUI passes an hour; the CLI keeps the 24h TTL, so an offline machine
    is not slowed by a 2s attempt on every command."""
    update_check._write_cache({"checked_at": _iso_days_ago(2 / 24), "failed": True})

    calls = _mock_urlopen_ok(monkeypatch, "9.9.9")
    update_check.refresh_cache_if_stale()
    assert calls == []  # CLI default: still inside the 24h TTL

    update_check.refresh_cache_if_stale(retry_after=3600)
    assert len(calls) == 1
    assert update_check.read_cache()["latest"] == "9.9.9"


def test_a_successful_check_keeps_the_full_ttl_whatever_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update_check._write_cache({"checked_at": _iso_days_ago(2 / 24), "latest": "1.1.0"})
    calls = _mock_urlopen_ok(monkeypatch, "9.9.9")
    update_check.refresh_cache_if_stale(retry_after=3600)
    assert calls == []


def test_the_timeout_reaches_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_urlopen_ok(monkeypatch, "9.9.9")
    update_check.refresh_cache_if_stale(timeout=10.0)
    assert calls == [10.0]
