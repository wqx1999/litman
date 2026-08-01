"""Ingest endpoint tests (task-gui-doi-add): drag-in add via the webUI.

The invariant #16 assertion mirrors test_server_structured: after a confirm
we check TRUTH (papers/<id>/ with metadata.yaml + paper.pdf) AND DERIVED
(INDEX.json contains the paper) — proof the ingest ran through the shared
``_apply_add`` backend, not some second write path. CrossRef is mocked at the
routes_ingest import site; no test touches the network.

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.library import create_vault
from litman.exceptions import ImporterError
from litman.server import create_app
from litman.server import routes_ingest as ri
from litman.server.routes_ingest import _UPLOAD_DIRNAME

from tests.core.test_doi_sniff import _minimal_pdf_with_text

_yaml = YAML(typ="safe")

_DOI = "10.1093/bioinformatics/btae364"
_PAPER_ID = "2024_Chen_HELM-GPT-Macrocyclic"

SAMPLE_MESSAGE: dict[str, Any] = {
    "title": ["HELM-GPT: De novo macrocyclic peptide design"],
    "author": [
        {"family": "Chen", "given": "Yi"},
        {"family": "Wang", "given": "Lin"},
    ],
    "published-print": {"date-parts": [[2024]]},
    "container-title": ["Bioinformatics"],
    "DOI": _DOI,
}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


@pytest.fixture
def client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


@pytest.fixture
def mock_crossref(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace routes_ingest's fetch_crossref with a SAMPLE_MESSAGE stub.

    Patched on the imported MODULE OBJECT, never by dotted string: an earlier
    test in the suite (test_cli_import_does_not_load_fastapi) purges
    ``litman.server*`` from sys.modules, so a string target would re-import a
    fresh module and patch a copy the running app never calls — the request
    would then hit the real CrossRef API.
    """
    captured: dict[str, Any] = {}

    def _fake(doi: str, client=None) -> dict[str, Any]:
        captured["doi"] = doi
        return SAMPLE_MESSAGE

    monkeypatch.setattr(ri, "fetch_crossref", _fake)
    return captured


def _upload(client: TestClient, body: bytes) -> Any:
    return client.post(
        "/api/ingest/pdf",
        content=body,
        headers={"Content-Type": "application/pdf"},
    )


def _pdf_with_doi() -> bytes:
    return _minimal_pdf_with_text(f"doi:{_DOI}")


# ---------------------------------------------------------------------------
# POST /api/ingest/pdf — upload + sniff
# ---------------------------------------------------------------------------


def test_upload_stashes_pdf_and_sniffs_doi(
    client: TestClient, vault: Path
) -> None:
    resp = _upload(client, _pdf_with_doi())
    assert resp.status_code == 200
    payload = resp.json()
    assert re.fullmatch(r"[0-9a-f]{32}", payload["handle"])
    assert payload["doi"] == _DOI
    assert payload["candidates"] == [_DOI]
    stored = vault / _UPLOAD_DIRNAME / f"{payload['handle']}.pdf"
    assert stored.is_file()
    assert stored.read_bytes() == _pdf_with_doi()


def test_upload_without_text_layer_sniffs_nothing(client: TestClient) -> None:
    # Magic header present but no parseable text layer → stored, doi null.
    resp = _upload(client, b"%PDF-1.4\nno text layer here\n%%EOF\n")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["doi"] is None
    assert payload["candidates"] == []


def test_upload_rejects_non_pdf(client: TestClient, vault: Path) -> None:
    resp = _upload(client, b"<html>definitely not a pdf</html>")
    assert resp.status_code == 400
    assert "%PDF-" in resp.json()["detail"]
    # Nothing may land on disk for a refused upload.
    tmp_dir = vault / _UPLOAD_DIRNAME
    assert not tmp_dir.is_dir() or not any(tmp_dir.iterdir())


def test_upload_rejects_oversize(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ri, "_MAX_UPLOAD_BYTES", 16)
    resp = _upload(client, b"%PDF-1.4" + b"x" * 64)
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# GET /api/ingest/preview — CrossRef + dedup + proposed id
# ---------------------------------------------------------------------------


def test_preview_resolves_metadata_and_proposes_id(
    client: TestClient, mock_crossref: dict[str, Any]
) -> None:
    resp = client.get("/api/ingest/preview", params={"doi": _DOI})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["title"] == "HELM-GPT: De novo macrocyclic peptide design"
    assert payload["authors"] == ["Chen, Yi", "Wang, Lin"]
    assert payload["year"] == 2024
    assert payload["journal"] == "Bioinformatics"
    assert payload["proposedId"] == _PAPER_ID
    assert payload["idError"] is None
    assert payload["inVault"] is None


def test_preview_canonicalizes_resolver_url(
    client: TestClient, mock_crossref: dict[str, Any]
) -> None:
    resp = client.get(
        "/api/ingest/preview", params={"doi": f"https://doi.org/{_DOI}"}
    )
    assert resp.status_code == 200
    assert mock_crossref["doi"] == _DOI


def test_preview_importer_error_maps_to_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(doi: str, client=None) -> dict[str, Any]:
        raise ImporterError(f"DOI not found in CrossRef: {doi!r}")

    monkeypatch.setattr(ri, "fetch_crossref", _boom)
    resp = client.get("/api/ingest/preview", params={"doi": "10.9999/nope"})
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]


def test_preview_missing_year_reports_id_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    yearless = {k: v for k, v in SAMPLE_MESSAGE.items() if k != "published-print"}

    monkeypatch.setattr(ri, "fetch_crossref", lambda doi, client=None: yearless)
    resp = client.get("/api/ingest/preview", params={"doi": _DOI})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["proposedId"] is None
    assert "year" in payload["idError"]


# ---------------------------------------------------------------------------
# POST /api/ingest/confirm — the actual write, through _apply_add
# ---------------------------------------------------------------------------


def _ingest_once(client: TestClient) -> str:
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm", json={"handle": handle, "doi": _DOI}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_confirm_ingests_through_shared_backend(
    client: TestClient, vault: Path, mock_crossref: dict[str, Any]
) -> None:
    paper_id = _ingest_once(client)
    assert paper_id == _PAPER_ID

    # TRUTH: the paper folder exists with the full `lit add` file set …
    paper_dir = vault / "papers" / paper_id
    assert (paper_dir / "paper.pdf").is_file()
    assert (paper_dir / "notes.md").is_file()
    assert (paper_dir / "discussion.md").is_file()
    meta = _yaml.load((paper_dir / "metadata.yaml").read_text(encoding="utf-8"))
    assert meta["doi"] == _DOI
    assert meta["status"] == "inbox"

    # … DERIVED: the backend reconciled INDEX.json in the same call …
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert paper_id in {p["id"] for p in index["papers"]}

    # … and mv semantics consumed the temp upload.
    assert not any((vault / _UPLOAD_DIRNAME).iterdir())


def test_confirm_duplicate_doi_409(
    client: TestClient, mock_crossref: dict[str, Any]
) -> None:
    _ingest_once(client)
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm", json={"handle": handle, "doi": _DOI}
    )
    assert resp.status_code == 409
    assert "already registered" in resp.json()["detail"]


def test_confirm_id_collision_auto_suffixes(
    client: TestClient, vault: Path, mock_crossref: dict[str, Any]
) -> None:
    # A pre-existing folder with the derived id but no matching DOI (so the
    # dedup precheck passes) forces the collision path — the GUI resolver
    # must auto-suffix, never prompt.
    (vault / "papers" / _PAPER_ID).mkdir(parents=True)
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm", json={"handle": handle, "doi": _DOI}
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == f"{_PAPER_ID}_b"


def test_confirm_rejects_malformed_handle(client: TestClient) -> None:
    resp = client.post(
        "/api/ingest/confirm",
        json={"handle": "../../escape", "doi": _DOI},
    )
    assert resp.status_code == 400


def test_confirm_unknown_handle_400(client: TestClient) -> None:
    resp = client.post(
        "/api/ingest/confirm", json={"handle": "0" * 32, "doi": _DOI}
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Vaultless gate
# ---------------------------------------------------------------------------


def test_ingest_routes_blocked_without_vault() -> None:
    client = TestClient(create_app(None))
    assert _upload(client, _pdf_with_doi()).status_code == 409
    assert (
        client.get("/api/ingest/preview", params={"doi": _DOI}).status_code
        == 409
    )
    assert (
        client.post(
            "/api/ingest/confirm", json={"handle": "0" * 32, "doi": _DOI}
        ).status_code
        == 409
    )
