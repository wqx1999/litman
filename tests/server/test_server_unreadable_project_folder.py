"""The web UI with a project folder litman cannot read, or cannot change.

The server half of tests/commands/test_unreadable_project_folder.py and
test_readonly_project_folder.py: the health panel lists the folder it could not
read (200, the finding in the list) instead of failing as a whole, and a write
finishes instead of answering 500.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from litman.server import create_app
from tests._project_folder import (
    ACCESS_DENIED,
    Library,
    deny,
    deny_writes,
    make_library,
    registered_projects,
)
from tests.server._client import TestClient

HUBS = ["litman_reflib", "litman_code"]


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    return make_library(tmp_path)


@pytest.mark.parametrize("hub", HUBS)
def test_the_health_panel_lists_a_project_folder_it_cannot_read(
    lib: Library, monkeypatch: pytest.MonkeyPatch, hub: str
) -> None:
    client = TestClient(create_app(lib.vault), raise_server_exceptions=False)
    with monkeypatch.context() as patch:
        deny(patch, lib.hub(hub))
        resp = client.get("/api/health")

    assert resp.status_code == 200, resp.text
    found = [
        i for i in resp.json()
        if i["category"] == "project_references"
        and i["message"].startswith(f"could not read {lib.hub(hub)}")
    ]
    assert len(found) == 1
    assert found[0]["message"].endswith(
        f"({ACCESS_DENIED}) — project 'pp' not checked"
    )
    assert found[0]["hint"] == "fix that, then run `lit health-check --fix`"


ROUTES = {
    "DELETE paper": ("delete", "/api/paper/p1", None),
    "POST paper project": ("post", "/api/paper/p3/project", {"project": "pp"}),
    "DELETE paper project": ("delete", "/api/paper/p2/project/pp", None),
    "PUT metadata title": ("put", "/api/paper/p2/metadata", {"set": {"title": "New"}}),
    "PUT project (rename)": ("put", "/api/projects/pp", {"new": "pp2"}),
    "DELETE project": ("delete", "/api/projects/pp", None),
}


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("route", list(ROUTES))
def test_a_write_finishes_with_a_project_folder_it_cannot_read(
    lib: Library, monkeypatch: pytest.MonkeyPatch, route: str, hub: str
) -> None:
    """The write lands and answers 200; the health panel (above) is where the
    folder shows up — no new message of its own (decided for the web UI)."""
    method, url, body = ROUTES[route]
    client = TestClient(create_app(lib.vault), raise_server_exceptions=False)
    with monkeypatch.context() as patch:
        deny(patch, lib.hub(hub))
        resp = getattr(client, method)(url, **({"json": body} if body else {}))
        health = client.get("/api/health")

    assert resp.status_code == 200, resp.text
    assert health.status_code == 200, health.text


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("fault", ["cannot read", "cannot change"])
def test_deleting_a_project_names_the_folder_it_could_not_clean_up(
    lib: Library, monkeypatch: pytest.MonkeyPatch, fault: str, hub: str
) -> None:
    """The one write the health panel cannot stand in for: the project is out
    of the config, so nothing looks at its folder again. The delete itself
    names what it left there (the page turns it into the notice)."""
    client = TestClient(create_app(lib.vault), raise_server_exceptions=False)
    with monkeypatch.context() as patch:
        (deny if fault == "cannot read" else deny_writes)(patch, lib.hub(hub))
        resp = client.delete("/api/projects/pp")

    assert resp.status_code == 200, resp.text
    # Compared resolved: the registered path may be spelled another way
    # (macOS /private/var, Windows short names).
    left = resp.json()["foldersLeft"]
    assert [Path(f["path"]).resolve() for f in left] == [lib.hub(hub).resolve()]
    assert [f["reason"] for f in left] == [ACCESS_DENIED]
    assert "pp" not in registered_projects(lib)
    assert any(lib.hub(hub).iterdir())  # left there, as the notice says


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("route", list(ROUTES))
def test_a_write_finishes_with_a_project_folder_it_cannot_change(
    lib: Library, monkeypatch: pytest.MonkeyPatch, route: str, hub: str
) -> None:
    method, url, body = ROUTES[route]
    client = TestClient(create_app(lib.vault), raise_server_exceptions=False)
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.hub(hub))
        resp = getattr(client, method)(url, **({"json": body} if body else {}))
        health = client.get("/api/health")

    assert resp.status_code == 200, resp.text
    assert health.status_code == 200, health.text
