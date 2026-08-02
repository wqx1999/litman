"""Tests for ``GET /api/whatsnew`` + ``PUT /api/whatsnew/seen``.

The GET is PURE READ (invariant #16): the digest ships inside the installed
package, so the request path never touches the network and the route stays
reachable with no vault served (it is on the vaultless allowlist, like
``/api/version`` — the two report on the *installation*, not a library).

The PUT (task-dogfood-fixes §2) records the dismissal machine-side instead of
in ``localStorage``, which the GUI partitions away from itself every time it
opens under the other browser profile or the next port up. It takes no body:
the server records ITS OWN version, so no client can name a version it did
not actually show.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import litman
from litman.core import ui_state
from litman.core.library import create_vault
from litman.core.whatsnew import CHANGELOG_URL
from litman.server import create_app


def test_whatsnew_reports_running_version_digest(tmp_path: Path) -> None:
    resp = TestClient(create_app(create_vault(tmp_path))).get("/api/whatsnew")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == litman.__version__
    assert isinstance(body["bullets"], list) and body["bullets"]
    assert all(isinstance(b, str) for b in body["bullets"])
    assert body["changelogUrl"] == CHANGELOG_URL
    assert body["seen"] is None  # fresh isolated state dir


def test_whatsnew_unknown_version_gives_empty_bullets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version with no recorded section answers cleanly with [] — the SPA
    then simply keeps the card closed."""
    monkeypatch.setattr("litman.__version__", "0.0.0")
    resp = TestClient(create_app(create_vault(tmp_path))).get("/api/whatsnew")
    assert resp.status_code == 200
    assert resp.json() == {
        "version": "0.0.0",
        "bullets": [],
        "changelogUrl": CHANGELOG_URL,
        "seen": None,
    }


def test_whatsnew_reachable_without_vault() -> None:
    """Welcome-page / vault-gone states must not 409 this route: it reads the
    installed package, not the library."""
    resp = TestClient(create_app(None)).get("/api/whatsnew")
    assert resp.status_code == 200
    assert resp.json()["version"] == litman.__version__


def test_put_seen_records_and_get_reports_it(tmp_path: Path) -> None:
    """AC-4 (server half): dismissing writes the marker; the next GET — which
    is what a freshly opened window makes — reads it back, whatever browser
    profile or port that window happens to be."""
    client = TestClient(create_app(create_vault(tmp_path)))
    resp = client.put("/api/whatsnew/seen")
    assert resp.status_code == 200
    assert resp.json() == {"seen": litman.__version__}
    assert ui_state.load_whatsnew_seen() == litman.__version__
    assert client.get("/api/whatsnew").json()["seen"] == litman.__version__


def test_put_seen_records_the_servers_own_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded value comes from the running package, never from the
    request — the endpoint accepts no body, and a client that sends one is
    ignored rather than believed."""
    monkeypatch.setattr("litman.__version__", "9.9.9")
    client = TestClient(create_app(create_vault(tmp_path)))
    resp = client.put("/api/whatsnew/seen", json={"version": "0.0.1"})
    assert resp.status_code == 200
    assert resp.json() == {"seen": "9.9.9"}
    assert ui_state.load_whatsnew_seen() == "9.9.9"


def test_put_seen_is_idempotent(tmp_path: Path) -> None:
    """Repeat dismissals (two windows open, both closed) must not error."""
    client = TestClient(create_app(create_vault(tmp_path)))
    first = client.put("/api/whatsnew/seen")
    second = client.put("/api/whatsnew/seen")
    assert first.json() == second.json() == {"seen": litman.__version__}


def test_put_seen_works_without_vault() -> None:
    """AC-4: the welcome page pops the card too — a user with no library yet
    must be able to dismiss it for good, so the route is on the vaultless
    allowlist (without it the middleware answers 409 and the card returns
    every launch)."""
    resp = TestClient(create_app(None)).put("/api/whatsnew/seen")
    assert resp.status_code == 200
    assert resp.json() == {"seen": litman.__version__}


def test_put_seen_keeps_pins(tmp_path: Path) -> None:
    """The two tenants of ui-state.json share a file; dismissing the card must
    not cost the user their pins."""
    vault = create_vault(tmp_path)
    ui_state.save_pins(vault, ["some_paper"])
    TestClient(create_app(vault)).put("/api/whatsnew/seen")
    assert ui_state.load_pins(vault) == ["some_paper"]
