"""Every failure the litman window shows has its full text on disk, and ``lit gui-log`` reads it.

The page shows one short line; the agent the person asks finds the rest
through the CLI. Each test drives a real route and reads the entry back
through ``lit gui-log --format json``: a coded failure, one that gets the
action's fixed line, and one no route foresaw (with its traceback). Then the
cases with nothing on disk before the log: project folders a delete could
not clean, the SI window's doubts, a failed automatic update, the page's own
failures. And the reverses: refusals that are not this user's, and answers
that are states rather than failures, stay out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner

from litman.cli import cli
from litman.core import agent_permissions, agents, self_update_helper
from litman.core.config import CONFIG_FILENAME
from litman.core.library import create_vault
from litman.core.project_link import add_project
from litman.core.update_check import OPT_OUT_ENV
from litman.server import create_app, routes_structured, routes_ui_state
from tests.server._client import GUI_URL, TestClient

FOREIGN = "https://evil.example"


def _log() -> list[dict[str, Any]]:
    """The entries, newest first, as an agent reads them."""
    result = CliRunner().invoke(cli, ["gui-log", "--format", "json", "--limit", "100"])
    assert result.exit_code == 0, result.output
    entries: list[dict[str, Any]] = json.loads(result.stdout)
    return entries


def _client(vault: Path | None, **kwargs: Any) -> TestClient:
    return TestClient(create_app(vault), **kwargs)


# ---------------------------------------------------------------------------
# One route of each kind, read back through the CLI
# ---------------------------------------------------------------------------


def test_a_coded_failure_is_logged_in_full_and_the_reader_prints_it(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, paper_id = vault_with_paper
    folder = tmp_path / "thesis"
    folder.mkdir()
    add_project(vault, "thesis", folder)
    folder.rmdir()  # the registered folder is gone from this machine

    resp = _client(vault).post(f"/api/paper/{paper_id}/project", json={"project": "thesis"})

    assert resp.status_code == 400
    body = resp.json()
    assert (body["code"], body["params"]) == ("project-path-missing", {"project": "thesis"})
    [entry] = _log()
    assert entry["kind"] == "error"
    assert entry["route"] == f"POST /api/paper/{paper_id}/project"
    assert entry["vault"] == str(vault)
    assert (entry["status"], entry["code"], entry["params"]) == (
        400, "project-path-missing", {"project": "thesis"},
    )
    # The full text, with the way out, exactly as the answer carried it.
    assert entry["text"] == body["detail"]
    assert "does not exist on this machine" in entry["text"]


def test_a_failure_with_only_a_fixed_line_is_logged_too(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper

    resp = _client(vault).put("/api/pins/2099_Gone_Paper")

    assert resp.status_code == 404
    assert resp.json()["code"] is None
    [entry] = _log()
    assert (entry["route"], entry["status"], entry["code"]) == (
        "PUT /api/pins/2099_Gone_Paper", 404, None,
    )
    assert entry["text"] == resp.json()["detail"]


def test_an_unforeseen_failure_answers_in_json_and_logs_its_traceback(
    vault_with_paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _ = vault_with_paper

    def boom(_vault: Path) -> list[str]:
        raise RuntimeError("the pins file went away mid-read")

    monkeypatch.setattr(routes_ui_state, "load_pins", boom)

    resp = _client(vault, raise_server_exceptions=False).get("/api/pins")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "unexpected"
    assert "`lit gui-log`" in body["detail"]
    [entry] = _log()
    assert (entry["kind"], entry["code"], entry["route"]) == (
        "unexpected", "unexpected", "GET /api/pins",
    )
    assert entry["text"] == "RuntimeError: the pins file went away mid-read"
    assert "Traceback" in entry["traceback"]
    assert entry["traceback"].rstrip().endswith(
        "RuntimeError: the pins file went away mid-read"
    )
    # The reader's table points at the JSON for it.
    table = CliRunner().invoke(cli, ["gui-log", "--format", "table"]).output
    assert "traceback: see --format json" in table


# ---------------------------------------------------------------------------
# What stays out
# ---------------------------------------------------------------------------


def test_a_request_the_guard_refuses_writes_nothing(tmp_path: Path) -> None:
    client = _client(None)

    resp = client.post(
        "/api/fs/mkdir",
        json={"parent": str(tmp_path), "name": "a/b"},
        headers={"Origin": FOREIGN},
    )

    assert resp.status_code == 403
    assert _log() == []
    # The reverse: the same request from the page itself is logged.
    resp = client.post(
        "/api/fs/mkdir", json={"parent": str(tmp_path), "name": "a/b"},
        headers={"Origin": GUI_URL},
    )
    assert (resp.status_code, resp.json()["code"]) == (400, "name-separator")
    assert [e["code"] for e in _log()] == ["name-separator"]


def test_a_file_the_browser_looks_for_on_its_own_is_not_logged(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    client = _client(vault)

    assert client.get("/no-such-asset.js.map").status_code == 404
    assert _log() == []
    # The reverse: an unknown API route is a request the page made.
    assert client.get("/api/no-such-route").status_code == 404
    assert [e["route"] for e in _log()] == ["GET /api/no-such-route"]


def test_a_notes_file_not_written_yet_is_a_state_not_a_failure(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    client = _client(vault)

    assert client.get(f"/api/paper/{paper_id}/notes").status_code == 404
    assert _log() == []
    # The reverse: a PDF that is not on this machine is shown, so logged.
    resp = client.get(f"/api/paper/{paper_id}/pdf")
    assert (resp.status_code, resp.json()["code"]) == (404, "pdf-missing")
    assert [(e["code"], e["params"]) for e in _log()] == [("pdf-missing", {"id": paper_id})]


def test_a_library_gone_mid_session_is_logged_once_not_per_poll(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    client = _client(vault)
    (vault / CONFIG_FILENAME).unlink()

    for route in ("/api/papers", "/api/taxonomy", "/api/papers"):
        resp = client.get(route)
        assert (resp.status_code, resp.json()["code"]) == (410, "vault-gone")

    [entry] = _log()
    assert entry["code"] == "vault-gone"
    assert str(vault) in entry["text"]


# ---------------------------------------------------------------------------
# What had nothing on disk before
# ---------------------------------------------------------------------------


def test_project_folders_a_delete_could_not_clean_are_each_logged(
    vault_with_paper: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _ = vault_with_paper
    reflib = tmp_path / "thesis" / "litman_reflib"
    code = tmp_path / "thesis" / "litman_code"

    def remove(
        _vault: Path, _name: str, *, hubs_left: dict[Path, OSError]
    ) -> tuple[int, list[str]]:
        hubs_left[reflib] = PermissionError(13, "Permission denied")
        hubs_left[code] = OSError(16, "Device or resource busy")
        return 0, []

    monkeypatch.setattr(routes_structured, "remove_project", remove)

    resp = _client(vault).delete("/api/projects/thesis")

    assert resp.status_code == 200
    assert len(resp.json()["foldersLeft"]) == 2
    entries = {e["text"]: e for e in _log()}
    assert len(entries) == 2
    for entry in entries.values():
        assert (entry["kind"], entry["code"], entry["params"]) == (
            "warning", "project-folder-left", {"name": "thesis"},
        )
    reflib_line = next(t for t in entries if str(reflib) in t)
    assert "(Permission denied)" in reflib_line
    assert "Delete its links and REFERENCES.md by hand" in reflib_line
    code_line = next(t for t in entries if str(code) in t)
    assert "Delete its links by hand" in code_line


def _paper_with_doi(vault: Path, paper_id: str, doi: str) -> None:
    folder = vault / "papers" / paper_id
    folder.mkdir(parents=True)
    (folder / "metadata.yaml").write_text(
        f"id: {paper_id}\ntitle: Concepts of AI\nauthors:\n  - Doe, Jane\nyear: 2019\n"
        f"doi: {doi}\nstatus: inbox\n",
        encoding="utf-8",
    )


def test_the_si_windows_doubts_are_logged_and_its_yes_is_not(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    _paper_with_doi(vault, "2019_Yang_Concepts", "10.1021/acs.jcim.9b00237")
    client = _client(vault)
    rid = client.post(
        "/api/si/requests", json={"paperId": "2019_Yang_Concepts", "source": "page"}
    ).json()["request"]["id"]

    def drop(name: str, body: bytes) -> dict[str, Any]:
        resp = client.post(
            f"/api/si/requests/{rid}/files",
            content=body,
            headers={"X-Litman-Filename": name},
        )
        assert resp.status_code == 200, resp.text
        row: dict[str, Any] = resp.json()["row"]
        return row

    empty = drop("table_s1.xlsx", b"")
    unsure = drop("data.xlsx", b"PK\x03\x04 sheet")
    sure = drop("ci9b00237_si_001.xlsx", b"PK\x03\x04 another sheet")

    assert (empty["verdict"], empty["reasonCode"]) == ("rejected", "si-empty")
    assert (unsure["verdict"], unsure["reasonCode"]) == ("unverified", "si-unconfirmed")
    assert (sure["verdict"], sure["reasonCode"]) == ("ok", "si-doi-match")
    entries = _log()
    assert sorted((e["kind"], e["code"], e["params"]["file"]) for e in entries) == [
        ("si-check", "si-empty", "table_s1.xlsx"),
        ("si-check", "si-unconfirmed", "data.xlsx"),
    ]
    assert all(e["params"]["paper"] == "2019_Yang_Concepts" for e in entries)
    assert "data.xlsx: can't confirm it belongs to this paper" in {e["text"] for e in entries}


def test_a_failed_automatic_update_leaves_its_reason_in_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OPT_OUT_ENV, "1")  # the lifespan's update check: no network
    flag = self_update_helper.fail_flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("upgrade command failed; see self-update.log", encoding="utf-8")

    with TestClient(create_app(create_vault(tmp_path))) as client:
        assert client.get("/api/version").json()["selfUpdateFailed"]

    assert not flag.exists()
    [entry] = _log()
    assert (entry["kind"], entry["code"]) == ("warning", "self-update-failed")
    assert "upgrade command failed" in entry["text"]
    assert str(self_update_helper.log_path()) in entry["text"]


def test_a_permission_the_agent_launch_could_not_write_is_logged(
    vault_with_paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _ = vault_with_paper
    warning = "Could not safely update /home/u/.claude/settings.json: top level is not an object."
    monkeypatch.setattr(agents, "detect", lambda _spec: True)
    monkeypatch.setattr(
        agent_permissions,
        "install_claude_lit_permission",
        lambda: {"mode": "unchanged", "rule": "", "warning": warning},
    )
    monkeypatch.setattr("litman.core.terminal.spawn_terminal", lambda *_args: True)

    resp = _client(vault).post("/api/agent/launch", json={"agent": "claude"})

    assert resp.status_code == 200
    assert (resp.json()["permission_warning"], resp.json()["permission_code"]) == (
        warning, "permissions-not-written",
    )
    [entry] = _log()
    assert (entry["kind"], entry["code"], entry["params"], entry["text"]) == (
        "warning", "permissions-not-written", {"agent": "Claude Code"}, warning,
    )


# ---------------------------------------------------------------------------
# The page's own failures
# ---------------------------------------------------------------------------


def test_the_page_can_log_a_failure_that_never_reached_the_server(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    client = _client(vault)

    resp = client.post(
        "/api/gui-log",
        json={
            "action": "save PDF annotations",
            "code": "pdf-not-saved",
            "text": "pdf.js saveDocument failed: Invalid PDF structure.",
        },
    )

    assert resp.status_code == 200
    [entry] = _log()
    assert (entry["kind"], entry["route"], entry["code"], entry["status"]) == (
        "page", "page: save PDF annotations", "pdf-not-saved", None,
    )
    assert entry["text"] == "pdf.js saveDocument failed: Invalid PDF structure."
    assert entry["vault"] == str(vault)


def test_the_page_log_works_without_a_library_and_refuses_bad_bodies_quietly() -> None:
    client = _client(None)

    assert client.post(
        "/api/gui-log", json={"action": "save the language", "text": "Failed to fetch"}
    ).status_code == 200
    for bad in (
        {"text": "no action"},
        {"action": "x", "text": ""},
        {"action": "x", "text": "y", "code": "Not A Code"},
    ):
        assert client.post("/api/gui-log", json=bad).status_code == 400

    [entry] = _log()
    assert (entry["vault"], entry["text"]) == (None, "Failed to fetch")


def test_another_website_cannot_write_into_the_log(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper

    resp = _client(vault).post(
        "/api/gui-log",
        json={"action": "x", "text": "planted"},
        headers={"Origin": FOREIGN},
    )

    assert resp.status_code == 403
    assert _log() == []
