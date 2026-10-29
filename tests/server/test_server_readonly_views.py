"""The web UI with a views/ folder litman cannot change.

The server half of tests/commands/test_readonly_views.py: the line a command
prints there reaches only the terminal `lit gui` runs in, so a write answers
200 and the health panel is where the page learns the view is out of date.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from litman.core.portable_link import is_portable_link
from litman.server import create_app
from tests._project_folder import (
    Library,
    deny_writes,
    make_library,
    paper_meta,
    run_lit,
)
from tests.server._client import TestClient


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    lib = make_library(tmp_path)
    code, output, exc = run_lit(lib, ["refresh-views"])
    assert code == 0, f"{output} {exc!r}"
    return lib


@pytest.mark.parametrize("folder", ["by-status", "by-status/inbox"])
def test_a_status_change_finishes_and_the_health_panel_reports_the_view(
    lib: Library, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    """``by-status`` takes no new bucket (``skim``); ``by-status/inbox`` will
    not give up the paper's old link, which stays behind."""
    inbox = lib.vault / "views" / "by-status" / "inbox"
    client = TestClient(create_app(lib.vault), raise_server_exceptions=False)
    with monkeypatch.context() as patch:
        deny_writes(patch, lib.vault / "views" / folder)
        resp = client.put(
            "/api/paper/p2/metadata", json={"set": {"status": "skim"}}
        )

    assert resp.status_code == 200, resp.text
    assert paper_meta(lib, "p2")["status"] == "skim"
    assert is_portable_link(inbox / "p2")  # left behind
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    found = [i for i in health.json() if i["category"] == "views_vs_metadata"]
    assert any("views/by-status/inbox/p2" in i["message"] for i in found), found
