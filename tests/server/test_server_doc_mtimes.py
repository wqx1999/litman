"""Tests for GET /api/doc-mtimes (the notes/discussion mtime change-detection feed).

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.server import create_app


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def test_doc_mtimes_keyed_by_paper_id(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get("/api/doc-mtimes")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert paper_id in body
    assert set(body[paper_id]) == {"notes", "discussion"}


def test_present_notes_float_absent_discussion_null(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    (vault / "papers" / paper_id / "notes.md").write_text(
        "# notes\n", encoding="utf-8"
    )
    body = _client(vault).get("/api/doc-mtimes").json()
    # Both AC-B2 branches on one paper (the fixture has a single paper):
    # the written notes.md → float, the never-created discussion.md → null.
    # _stat_mtime is field-agnostic, so this also covers a notes-less paper.
    assert isinstance(body[paper_id]["notes"], float)
    assert body[paper_id]["discussion"] is None


def test_rewriting_notes_bumps_mtime(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    notes = vault / "papers" / paper_id / "notes.md"
    notes.write_text("first\n", encoding="utf-8")
    client = _client(vault)

    m1 = client.get("/api/doc-mtimes").json()[paper_id]["notes"]
    # Bump st_mtime deterministically (no sleep): the endpoint reports st_mtime
    # verbatim, so a later mtime must produce a strictly-greater value.
    os.utime(notes, (m1 + 10, m1 + 10))
    m2 = client.get("/api/doc-mtimes").json()[paper_id]["notes"]
    assert m2 > m1


def test_endpoint_performs_no_writes(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    paper_dir = vault / "papers" / paper_id
    meta = paper_dir / "metadata.yaml"
    notes = paper_dir / "notes.md"
    meta_mtime_before = meta.stat().st_mtime
    assert not notes.exists()

    resp = _client(vault).get("/api/doc-mtimes")
    assert resp.status_code == 200

    # Pure read (invariant #16): stat-only must not rewrite metadata.yaml, nor
    # materialize an absent notes.md.
    assert meta.stat().st_mtime == meta_mtime_before
    assert not notes.exists()
