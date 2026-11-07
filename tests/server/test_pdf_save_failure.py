"""A PDF save the disk refuses answers in words, with a code, and lands in the GUI log.

Saving annotations overwrites ``paper.pdf``. When the write fails (Windows
keeps a PDF that another viewer has open locked; a read-only file; a full
disk) the route used to raise the OSError unhandled: a plain-text 500 the
page dropped into the console, and nothing on disk. Now the answer says what
failed and what to do, ``pdf-not-saved`` picks the page's line, and
``lit gui-log`` has it.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner

from litman.cli import cli
from litman.server import create_app, routes_write
from tests.server._client import TestClient

PDF = b"%PDF-1.4\n% annotated\n"


def _log() -> list[dict[str, Any]]:
    result = CliRunner().invoke(cli, ["gui-log", "--format", "json"])
    assert result.exit_code == 0, result.output
    entries: list[dict[str, Any]] = json.loads(result.stdout)
    return entries


@pytest.fixture
def paper(vault_with_paper: tuple[Path, str]) -> tuple[Path, str]:
    vault, paper_id = vault_with_paper
    (vault / "papers" / paper_id / "paper.pdf").write_bytes(b"%PDF-1.4\n% original\n")
    return vault, paper_id


def _refuse(error: OSError) -> Any:
    def save(*_args: object) -> dict[str, object]:
        raise error

    return save


def test_a_pdf_the_disk_refuses_answers_in_words_and_is_logged(
    paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, paper_id = paper
    monkeypatch.setattr(
        routes_write, "_save", _refuse(PermissionError(errno.EACCES, "Permission denied"))
    )

    resp = TestClient(create_app(vault)).put(
        f"/api/paper/{paper_id}/pdf-annotations", content=PDF
    )

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "pdf-not-saved"
    assert body["detail"] == (
        f"papers/{paper_id}/paper.pdf could not be saved (Permission denied); nothing was "
        "changed. Close any program that has it open, or make it writable, then save again."
    )
    [entry] = _log()
    assert (entry["route"], entry["code"], entry["text"]) == (
        f"PUT /api/paper/{paper_id}/pdf-annotations", "pdf-not-saved", body["detail"],
    )
    assert (vault / "papers" / paper_id / "paper.pdf").read_bytes() == b"%PDF-1.4\n% original\n"


def test_a_full_disk_says_so(paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch) -> None:
    vault, paper_id = paper
    monkeypatch.setattr(routes_write, "_save", _refuse(OSError(errno.ENOSPC, "No space left")))

    resp = TestClient(create_app(vault)).put(
        f"/api/paper/{paper_id}/pdf-annotations", content=PDF
    )

    assert (resp.status_code, resp.json()["code"]) == (507, "pdf-not-saved")
    assert "Not enough free disk space" in resp.json()["detail"]


def test_a_pdf_gone_from_this_machine_is_a_failed_save_too(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper  # no paper.pdf

    resp = TestClient(create_app(vault)).put(
        f"/api/paper/{paper_id}/pdf-annotations", content=PDF
    )

    assert (resp.status_code, resp.json()["code"]) == (404, "pdf-not-saved")


def test_a_save_that_works_is_unchanged(paper: tuple[Path, str]) -> None:
    vault, paper_id = paper

    resp = TestClient(create_app(vault)).put(
        f"/api/paper/{paper_id}/pdf-annotations", content=PDF
    )

    assert resp.status_code == 200, resp.text
    assert (vault / "papers" / paper_id / "paper.pdf").read_bytes() == PDF
    assert _log() == []


def test_a_notes_save_the_disk_refuses_gets_the_tabs_own_line(
    paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, paper_id = paper
    monkeypatch.setattr(
        routes_write, "_save", _refuse(PermissionError(errno.EACCES, "Permission denied"))
    )

    resp = TestClient(create_app(vault)).put(
        f"/api/paper/{paper_id}/notes", json={"text": "# Foo Bar\n\nmy notes\n"}
    )

    assert resp.status_code == 500
    assert resp.json()["code"] is None
    assert "notes.md could not be saved" in resp.json()["detail"]
    assert [e["route"] for e in _log()] == [f"PUT /api/paper/{paper_id}/notes"]
