"""Tests for ``GET /api/whatsnew`` (task-whatsnew).

The route is PURE READ (invariant #16): the digest ships inside the installed
package, so the request path never touches the network and the route stays
reachable with no vault served (it is on the vaultless allowlist, like
``/api/version`` — the two report on the *installation*, not a library).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import litman
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
    }


def test_whatsnew_reachable_without_vault() -> None:
    """Welcome-page / vault-gone states must not 409 this route: it reads the
    installed package, not the library."""
    resp = TestClient(create_app(None)).get("/api/whatsnew")
    assert resp.status_code == 200
    assert resp.json()["version"] == litman.__version__
