"""Whitelist write-endpoint tests for the litman webUI server (A3).

Covers ``PUT /api/paper/{id}/pdf-annotations``: the invariant #16 first-class
direct write that atomically overwrites paper.pdf via ``staged_write``.

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.server import create_app


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _plant_pdf(
    vault: Path, paper_id: str, make_text_pdf: Callable[..., Path]
) -> Path:
    pdf_path = vault / "papers" / paper_id / "paper.pdf"
    pdf_path.write_bytes(make_text_pdf([["original"]]).read_bytes())
    return pdf_path


def test_put_overwrites_pdf_on_disk(
    vault_with_paper: tuple[Path, str],
    make_text_pdf: Callable[..., Path],
) -> None:
    vault, paper_id = vault_with_paper
    pdf_path = _plant_pdf(vault, paper_id, make_text_pdf)

    new_bytes = make_text_pdf([["annotated"], ["page two"]]).read_bytes()
    assert new_bytes != pdf_path.read_bytes()

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=new_bytes,
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "bytes": len(new_bytes)}

    # The overwrite actually happened on disk.
    assert pdf_path.read_bytes() == new_bytes


def test_put_leaves_no_staging_residue(
    vault_with_paper: tuple[Path, str],
    make_text_pdf: Callable[..., Path],
) -> None:
    """A clean staged_write commit removes its op dir; .litman-staging is empty.

    ``create_vault`` creates an empty ``.litman-staging/`` directory, so the dir
    itself persists; what must NOT remain is any per-op subdirectory (which
    would indicate a torn / uncommitted write).
    """
    vault, paper_id = vault_with_paper
    _plant_pdf(vault, paper_id, make_text_pdf)

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=make_text_pdf([["annotated"]]).read_bytes(),
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 200

    staging = vault / ".litman-staging"
    leftover = [p for p in staging.iterdir()] if staging.is_dir() else []
    assert leftover == []


def test_put_bad_id_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).put(
        "/api/paper/foo..bar/pdf-annotations",
        content=b"%PDF-1.4 fake",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invalid paper id: 'foo..bar'."


def test_put_missing_pdf_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Whitelist is OVERWRITE-only: no paper.pdf on disk → 404, never create."""
    vault, paper_id = vault_with_paper
    pdf_path = vault / "papers" / paper_id / "paper.pdf"
    assert not pdf_path.exists()

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=b"%PDF-1.4 fake",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 404
    # And nothing was created.
    assert not pdf_path.exists()


def test_put_empty_body_400(
    vault_with_paper: tuple[Path, str],
    make_text_pdf: Callable[..., Path],
) -> None:
    vault, paper_id = vault_with_paper
    pdf_path = _plant_pdf(vault, paper_id, make_text_pdf)
    before = pdf_path.read_bytes()

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=b"",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 400
    # The existing pdf is untouched.
    assert pdf_path.read_bytes() == before
