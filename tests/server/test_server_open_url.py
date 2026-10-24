"""``POST /api/open-url``: a link clicked in the ``--window`` app window, handed
to the default browser.

No test reaches a real opener: ``viewer._spawn`` / ``_startfile`` fail the
test unless one puts its own in place, and every patch lands on the module
object (a dotted-string patch misses once ``test_gui`` has purged the server
modules from ``sys.modules``). The platform is faked through ``viewer._platform``,
never ``sys.platform``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from litman.core import viewer as viewer_mod
from litman.core.gui_registry import host_id
from litman.core.library import create_vault
from litman.server import create_app
from tests.server._client import GUI_URL, TestClient

_PAGE = "https://pubs.acs.org/doi/10.1021/acs.jmedchem.4c00001#si"
_INSTEAD = " — opening it in this window instead"


@pytest.fixture(autouse=True)
def _no_real_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a test reached a real opener: {args!r}")

    monkeypatch.setattr(viewer_mod, "_spawn", refuse)
    monkeypatch.setattr(viewer_mod, "_startfile", refuse)


class _Launch:
    """Stands in for ``viewer.launch_url``: records each page, and fails
    the way it is told to."""

    def __init__(self, fail: viewer_mod.OpenFailed | None = None) -> None:
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        return "xdg-open"


@pytest.fixture
def launch(monkeypatch: pytest.MonkeyPatch) -> _Launch:
    fake = _Launch()
    monkeypatch.setattr(viewer_mod, "open_files_mode", lambda: "desktop")
    monkeypatch.setattr(viewer_mod, "launch_url", fake)
    return fake


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(create_vault(tmp_path)))


def _open(client: TestClient, body: Any, **headers: str):
    return client.post("/api/open-url", json=body, headers=headers)


@pytest.mark.parametrize("url", [_PAGE, "http://example.org/a?b=c"])
def test_a_web_page_goes_to_the_default_browser(
    client: TestClient, launch: _Launch, url: str
) -> None:
    resp = _open(client, {"url": url})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"opened": True, "host": host_id()}
    assert launch.urls == [url]


def test_the_real_opener_gets_the_page_as_sent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(viewer_mod, "_platform", lambda: "linux")
    monkeypatch.setattr(
        viewer_mod, "_which", lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None
    )
    argvs: list[list[str]] = []

    class _Running:
        def wait(self, timeout: float | None = None) -> int:
            return 0

    def spawn(argv: list[str]) -> _Running:
        argvs.append(list(argv))
        return _Running()

    monkeypatch.setattr(viewer_mod, "_spawn", spawn)

    assert _open(client, {"url": _PAGE}).status_code == 200
    assert argvs == [["xdg-open", _PAGE]]


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("javascript:alert(1)", "Only an http or https link can go to your browser"),
        ("file:///C:/Windows/notepad.exe", "Only an http or https link can go to your browser"),
        ("data:text/html,x", "Only an http or https link can go to your browser"),
        ("zotero://select/items/1", "Only an http or https link can go to your browser"),
        ("https://exa mple.org/", "The link is not a single web address"),
        ("http://127.1:8765/", "The link is not a single web address"),
        ("http://127.0.0.1:8765\\@example.org/", "The link is not a single web address"),
        ("http://127.0.0.%31:8765/", "The link is not a single web address"),
        ("http://%6c%6fcalhost:8765/", "The link is not a single web address"),
        ("http://localhost%2e/", "The link is not a single web address"),
        (f"{GUI_URL}/api/vaults", "The link points at this computer, not the web"),
        ("http://localhost:8765/", "The link points at this computer, not the web"),
        ("http://[::1]:8888/tree", "The link points at this computer, not the web"),
    ],
)
def test_anything_but_a_page_on_another_machine_is_refused(
    client: TestClient, launch: _Launch, url: str, problem: str
) -> None:
    resp = _open(client, {"url": url})
    assert (resp.status_code, resp.json()["detail"]) == (400, problem + _INSTEAD)
    assert launch.urls == []


@pytest.mark.parametrize("body", [[_PAGE], {}, {"url": 7}, {"url": None}, {"href": _PAGE}])
def test_a_body_without_a_url_string_is_refused(
    client: TestClient, launch: _Launch, body: Any
) -> None:
    resp = _open(client, body)
    assert resp.status_code == 400
    assert resp.json()["detail"] == 'Body must be a JSON object with a "url" string.'
    assert launch.urls == []


def test_a_body_that_is_not_json_is_refused(client: TestClient, launch: _Launch) -> None:
    resp = client.post(
        "/api/open-url", content=b"url=x", headers={"Content-Type": "text/plain"}
    )
    assert (resp.status_code, resp.json()["detail"]) == (400, "Body must be JSON.")
    assert launch.urls == []


def test_another_site_cannot_open_pages(client: TestClient, launch: _Launch) -> None:
    resp = _open(client, {"url": _PAGE}, Origin="https://evil.example")
    assert resp.status_code == 403
    assert launch.urls == []
    # The page's own Origin passes, as every litman POST does.
    assert _open(client, {"url": _PAGE}, Origin=GUI_URL).status_code == 200
    assert launch.urls == [_PAGE]


def test_no_desktop_opens_nothing(
    client: TestClient, launch: _Launch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewer_mod, "open_files_mode", lambda: "none")
    resp = _open(client, {"url": _PAGE})
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        f"{host_id()} has no desktop to show your browser on" + _INSTEAD,
    )
    assert launch.urls == []


@pytest.mark.parametrize(
    ("reason", "detail"),
    [
        ("no-app", "No browser on {host} took the link"),
        ("not-started", "{host} could not start xdg-open for the link"),
    ],
)
def test_an_opener_that_fails_is_a_conflict(
    client: TestClient, launch: _Launch, reason: Any, detail: str
) -> None:
    launch.fail = viewer_mod.OpenFailed("x", command="xdg-open", reason=reason)
    resp = _open(client, {"url": _PAGE})
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        detail.format(host=host_id()) + _INSTEAD,
    )
    assert launch.urls == [_PAGE]


def test_it_works_once_the_library_is_gone(tmp_path: Path, launch: _Launch) -> None:
    # The main view stays up with its DOI and what's-new links while every
    # other route answers 410.
    vault = create_vault(tmp_path, name="lib")
    client = TestClient(create_app(vault))
    shutil.move(str(vault), str(tmp_path / "moved"))
    assert client.get("/api/papers").status_code == 410

    resp = _open(client, {"url": _PAGE})
    assert resp.status_code == 200, resp.text
    assert launch.urls == [_PAGE]


def test_it_works_without_a_library(launch: _Launch) -> None:
    resp = _open(TestClient(create_app(None)), {"url": _PAGE})
    assert resp.status_code == 200, resp.text
    assert launch.urls == [_PAGE]
