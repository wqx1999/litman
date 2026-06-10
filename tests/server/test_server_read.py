"""Read-endpoint tests for the litman webUI server (A2).

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.document import list_papers
from litman.core.views import project_paper
from litman.server import create_app


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def test_get_papers_matches_core_projection(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get("/api/papers")
    assert resp.status_code == 200

    expected = [project_paper(p) for p in list_papers(vault)]
    body = resp.json()
    assert [p["id"] for p in body] == [paper_id]
    assert body == expected


def test_get_paper_returns_full_metadata(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get(f"/api/paper/{paper_id}")
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["id"] == paper_id
    assert meta["title"] == "Foo Bar"
    # full metadata carries fields the thin INDEX projection drops
    assert "authors" in meta
    assert "journal" in meta


def test_get_paper_unknown_id_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/paper/does_not_exist")
    assert resp.status_code == 404


def test_get_pdf_supports_range(
    vault_with_paper: tuple[Path, str],
    make_text_pdf: Callable[..., Path],
) -> None:
    vault, paper_id = vault_with_paper
    pdf_src = make_text_pdf([["hello pdf"]])
    (vault / "papers" / paper_id / "paper.pdf").write_bytes(pdf_src.read_bytes())

    client = _client(vault)

    full = client.get(f"/api/paper/{paper_id}/pdf")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"

    partial = client.get(
        f"/api/paper/{paper_id}/pdf", headers={"Range": "bytes=0-99"}
    )
    assert partial.status_code == 206
    assert partial.headers["accept-ranges"] == "bytes"
    assert len(partial.content) == 100


def test_get_pdf_missing_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get(f"/api/paper/{paper_id}/pdf")
    assert resp.status_code == 404


@pytest.mark.parametrize("suffix", ["pdf", "notes"])
def test_traversal_id_rejected(
    vault_with_paper: tuple[Path, str],
    tmp_path: Path,
    suffix: str,
) -> None:
    """A traversal-style id must never reach outside the vault.

    Two id shapes are exercised on each file-serving endpoint:

    1. A percent-encoded ``../../`` form — the httpx test client normalizes the
       escaped slashes, so Starlette's router rejects it before our handler.
       We only assert it 404s and serves nothing outside the vault.
    2. A single-segment id containing ``..`` (e.g. ``foo..bar``) that DOES
       reach the handler as one path param. ``is_valid_id`` rejects it, so the
       defense-in-depth guard fires and returns 404 with our own detail — this
       proves the guard code path runs, not just a routing accident.
    """
    vault, _ = vault_with_paper

    # Plant a sentinel file outside the vault that a successful traversal could
    # leak; assert it is never served.
    secret = tmp_path / "outside_secret"
    secret.write_text("TOP SECRET\n", encoding="utf-8")

    client = _client(vault)

    encoded = client.get(f"/api/paper/..%2f..%2foutside_secret/{suffix}")
    assert encoded.status_code == 404
    assert "TOP SECRET" not in encoded.text

    guarded = client.get(f"/api/paper/foo..bar/{suffix}")
    assert guarded.status_code == 404
    assert guarded.json()["detail"] == "Invalid paper id: 'foo..bar'."


def test_get_paper_corrupt_metadata_500(
    vault_with_paper: tuple[Path, str],
) -> None:
    """An existing paper with unparseable YAML is a 500, not a 404.

    The paper folder is present, so the resource exists; its metadata being
    broken is a server-side data problem. ``find_paper`` raises
    ``CorruptMetadataError`` (carrying the offending path), which the handler
    surfaces as 500 rather than masking it as 'not found'.
    """
    vault, paper_id = vault_with_paper
    # Overwrite the fixture's valid YAML with a syntactically broken document.
    (vault / "papers" / paper_id / "metadata.yaml").write_text(
        "title: [unbalanced\n  : : :\n", encoding="utf-8"
    )

    resp = _client(vault).get(f"/api/paper/{paper_id}")
    assert resp.status_code == 500
    # The path-bearing message from CorruptMetadataError is carried through.
    assert "metadata.yaml" in resp.json()["detail"]


def test_get_paper_missing_is_404_not_500(
    vault_with_paper: tuple[Path, str],
) -> None:
    """A genuinely-absent paper stays a 404 (regression guard for Fix 2)."""
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/paper/2024_Nope_Missing")
    assert resp.status_code == 404


def test_get_taxonomy_returns_dict_keys(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/taxonomy")
    assert resp.status_code == 200
    tax = resp.json()
    for key in ("projects", "topics", "methods", "data"):
        assert key in tax
        assert isinstance(tax[key], list)


def test_get_projects_empty_vault(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_vaults_reports_active(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/vaults")
    assert resp.status_code == 200
    payload = resp.json()
    assert "active" in payload
    assert "vaults" in payload
    assert isinstance(payload["vaults"], list)


def test_get_notes_404_when_absent(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get(f"/api/paper/{paper_id}/notes")
    assert resp.status_code == 404


def test_get_notes_returns_text(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    (vault / "papers" / paper_id / "notes.md").write_text(
        "# notes\n", encoding="utf-8"
    )
    resp = _client(vault).get(f"/api/paper/{paper_id}/notes")
    assert resp.status_code == 200
    assert resp.json()["text"] == "# notes\n"


def test_root_route_when_frontend_not_built(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/")
    # assets/webui does not exist in Phase 0 → plain "not built" placeholder
    assert resp.status_code == 200
    assert "not built" in resp.text
