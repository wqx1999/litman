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
import os
import re
import time
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


def test_upload_rejects_oversize_without_content_length(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap must hold for a chunked upload too.

    Passing an iterator makes httpx stream the body with no ``content-length``,
    so the declared-size check cannot fire — only the streaming byte count
    stops it. Without that the whole body would be buffered into memory before
    any check ran, which is precisely what the cap exists to prevent.
    """
    monkeypatch.setattr(ri, "_MAX_UPLOAD_BYTES", 16)
    resp = client.post(
        "/api/ingest/pdf",
        content=iter([b"%PDF-1.4", b"x" * 64, b"y" * 64]),
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 413
    tmp_dir = vault / _UPLOAD_DIRNAME
    assert not tmp_dir.is_dir() or not any(tmp_dir.iterdir())


def test_sweep_removes_only_stale_uploads(client: TestClient, vault: Path) -> None:
    """TTL sweep: an abandoned stash goes, a fresh one stays."""
    fresh = _upload(client, _pdf_with_doi()).json()["handle"]
    tmp_dir = vault / _UPLOAD_DIRNAME
    stale = tmp_dir / f"{'a' * 32}.pdf"
    stale.write_bytes(_pdf_with_doi())
    old = time.time() - ri._UPLOAD_TTL_SECONDS - 60
    os.utime(stale, (old, old))

    assert ri.sweep_uploads(vault) == 1
    assert not stale.exists()
    assert (tmp_dir / f"{fresh}.pdf").is_file()


def test_startup_sweep_clears_orphans(vault: Path) -> None:
    """Entering the app's lifespan clears stashes left by a previous session.

    At startup no page is loaded, so anything in the stash directory is
    orphaned by definition — it must not survive into the new session (the
    24 h TTL would keep a 100 MB file around for a day otherwise).
    """
    tmp_dir = vault / _UPLOAD_DIRNAME
    tmp_dir.mkdir()
    orphan = tmp_dir / f"{'b' * 32}.pdf"
    orphan.write_bytes(_pdf_with_doi())
    old = time.time() - ri._STARTUP_TTL_SECONDS - 30
    os.utime(orphan, (old, old))

    # `with TestClient(...)` is what actually runs the lifespan.
    with TestClient(create_app(vault)):
        pass
    assert not orphan.exists()


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
# POST /api/ingest/confirm with hand-entered metadata — the papers CrossRef
# does not have (a patent, a CNKI-registered Chinese journal article)
# ---------------------------------------------------------------------------

_PATENT_META: dict[str, Any] = {
    "title": "Method for continuous macrocyclisation of peptides",
    "authors": ["Zhang, Wei", "Li, Hua"],
    "year": 2021,
    "journal": "China National Intellectual Property Administration",
    "venue-type": "patent",
}


def _manual_confirm(
    client: TestClient, meta: dict[str, Any]
) -> Any:
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    return client.post(
        "/api/ingest/confirm", json={"handle": handle, "metadata": meta}
    )


def test_manual_metadata_ingests_through_the_same_backend(
    client: TestClient, vault: Path
) -> None:
    # No mock_crossref fixture on purpose: this path must not touch CrossRef
    # at all, so a stray fetch would blow up on the real network call rather
    # than pass quietly against a stub.
    resp = _manual_confirm(client, _PATENT_META)
    assert resp.status_code == 200, resp.text
    paper_id = resp.json()["id"]
    assert paper_id == "2021_Zhang_Method-continuous-macrocyclisation"

    paper_dir = vault / "papers" / paper_id
    assert (paper_dir / "paper.pdf").is_file()
    meta = _yaml.load((paper_dir / "metadata.yaml").read_text(encoding="utf-8"))
    assert meta["authors"] == ["Zhang, Wei", "Li, Hua"]
    assert meta["venue-type"] == "patent"
    assert not meta["doi"]

    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert paper_id in {p["id"] for p in index["papers"]}
    assert not any((vault / _UPLOAD_DIRNAME).iterdir())


def test_manual_metadata_keeps_a_doi_crossref_never_resolved(
    client: TestClient, vault: Path
) -> None:
    # The Chinese-journal case: the DOI is real, registered with CNKI rather
    # than CrossRef. "Not found upstream" must not mean "thrown away" — the
    # record keeps it, and the dedup precheck honours it like any other.
    cnki_doi = "10.11949/j.issn.0438-1157.20171279"
    resp = _manual_confirm(
        client,
        {
            "title": "Bubbling-breath reactor for peptide synthesis",
            "authors": ["Wang, Xiaoming"],
            "year": 2017,
            "journal": "CIESC Journal",
            "doi": cnki_doi,
        },
    )
    assert resp.status_code == 200, resp.text
    paper_dir = vault / "papers" / resp.json()["id"]
    meta = _yaml.load((paper_dir / "metadata.yaml").read_text(encoding="utf-8"))
    assert meta["doi"] == cnki_doi

    # …and a second drop of the same DOI is refused as a duplicate, which is
    # only possible because the first one was actually written.
    dup = _manual_confirm(
        client,
        {"title": "Same paper again", "authors": ["Wang, Xiaoming"], "year": 2017, "doi": cnki_doi},
    )
    assert dup.status_code == 409


def test_manual_metadata_refuses_a_placeholder_first_author(
    client: TestClient, vault: Path
) -> None:
    # The whole reason this path reuses `lit add --from-llm-json`'s schema:
    # the first author's family name becomes the folder name, the citation
    # key and every wikilink, so a filler there is permanent.
    resp = _manual_confirm(
        client, {**_PATENT_META, "authors": ["Unknown", "Li, Hua"]}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "'authors'" in detail and "placeholder" in detail
    # Refused means nothing was written — not a folder, not an INDEX entry.
    assert not any((vault / "papers").iterdir())


def test_manual_metadata_rejects_unknown_fields(client: TestClient) -> None:
    # `extra="forbid"` on the schema: a field name the GUI invented (or a
    # typo) must fail loudly rather than be dropped on the way to disk.
    resp = _manual_confirm(client, {**_PATENT_META, "patentNumber": "CN123456"})
    assert resp.status_code == 400
    assert "patentNumber" in resp.json()["detail"]


def test_confirm_refuses_both_doi_and_metadata(client: TestClient) -> None:
    # Resolving this by precedence would silently pick one of two things the
    # user asked for; naming it is the only honest answer.
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm",
        json={"handle": handle, "doi": _DOI, "metadata": _PATENT_META},
    )
    assert resp.status_code == 400
    assert "not both" in resp.json()["detail"]


def test_confirm_refuses_neither_doi_nor_metadata(client: TestClient) -> None:
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post("/api/ingest/confirm", json={"handle": handle})
    assert resp.status_code == 400
    assert "required" in resp.json()["detail"]


def test_manual_metadata_must_be_an_object(client: TestClient) -> None:
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm",
        json={"handle": handle, "metadata": ["title", "authors"]},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/ingest/{handle} — dismissing the dialog
# ---------------------------------------------------------------------------


def test_discard_removes_the_stash(client: TestClient, vault: Path) -> None:
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    assert (vault / _UPLOAD_DIRNAME / f"{handle}.pdf").is_file()

    resp = client.delete(f"/api/ingest/{handle}")
    assert resp.status_code == 200
    assert resp.json() == {"discarded": True}
    assert not any((vault / _UPLOAD_DIRNAME).iterdir())


def test_discard_is_idempotent(client: TestClient) -> None:
    """A handle already consumed / swept is not an error — this is cleanup."""
    resp = client.delete(f"/api/ingest/{'c' * 32}")
    assert resp.status_code == 200
    assert resp.json() == {"discarded": False}


def test_discard_rejects_malformed_handle(client: TestClient) -> None:
    """Only server-minted uuid4 hex reaches the filesystem.

    (A dot-segment traversal never even gets this far — the URL normalizes to
    a different path and the router declines it — so what is worth asserting
    here is the shape gate on everything that DOES route.)
    """
    assert client.delete("/api/ingest/nope").status_code == 400
    assert client.delete("/api/ingest/paper.pdf").status_code == 400
    assert client.delete(f"/api/ingest/{'z' * 32}").status_code == 400
    assert client.delete(f"/api/ingest/{'a' * 31}").status_code == 400


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
    assert client.delete(f"/api/ingest/{'0' * 32}").status_code == 409


# ---------------------------------------------------------------------------
# POST /api/ingest/derive-id — the Paper ID line the add form shows live
#
# It exists so the frontend never has to know how ids are made. A TypeScript
# copy of derive_id would drift from the Python one, and the drift would only
# show up as a preview that disagrees with what got written.
# ---------------------------------------------------------------------------

_CN_TITLE = "关于化合物A的合成方法"


def _derive(client: TestClient, **fields: Any) -> Any:
    return client.post("/api/ingest/derive-id", json=fields)


def test_derive_id_returns_what_the_write_would_produce(client: TestClient) -> None:
    resp = _derive(
        client,
        title="Method for continuous macrocyclisation of peptides",
        authors=["Zhang, Wei"],
        year=2021,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "2021_Zhang_Method-continuous-macrocyclisation"
    assert body["error"] is None
    assert body["suggestion"] is None
    # Nothing left for the person to write: the form shows the quiet one-line
    # id, not the three boxes.
    assert body["segments"] == {
        "year": "2021",
        "family": "Zhang",
        "keyword": "Method-continuous-macrocyclisation",
        "needs": [],
    }


def test_derive_id_refuses_a_chinese_title_and_offers_nothing_bogus(
    client: TestClient,
) -> None:
    """The `2018_Zhang_A` case: no id, and no suggestion either.

    Offering `2018_Zhang_A` here would hand back precisely the id the gate in
    core/id.py just refused to create.
    """
    body = _derive(client, title=_CN_TITLE, authors=["Zhang, Wei"], year=2018).json()
    assert body["id"] is None
    assert body["suggestion"] is None
    # One line, and one that reads under a form field. The CLI's follow-up
    # paragraphs quote the title back, which is noise beside the input the
    # title is already sitting in.
    assert body["error"] == "This title cannot produce a paper id."


def test_derive_id_suggests_the_latin_fragment_when_there_is_one(
    client: TestClient,
) -> None:
    body = _derive(
        client,
        title="一种新型 PROTAC 分子的设计与合成",
        authors=["Zhang, Wei"],
        year=2018,
    ).json()
    assert body["id"] is None
    assert body["suggestion"] == "2018_Zhang_PROTAC"


def test_derive_id_stays_quiet_while_the_form_is_still_being_filled(
    client: TestClient,
) -> None:
    """Half-typed fields are not an error — the line renders empty, not red."""
    for fields in (
        {"title": "", "authors": ["Zhang, Wei"], "year": 2018},
        {"title": "A title", "authors": [], "year": 2018},
        {"title": "A title", "authors": ["Zhang, Wei"], "year": None},
        {},
    ):
        body = _derive(client, **fields).json()
        assert {k: body[k] for k in ("id", "error", "suggestion")} == {
            "id": None,
            "error": None,
            "suggestion": None,
        }, fields


def test_derive_id_survives_junk_field_types(client: TestClient) -> None:
    body = _derive(client, title=7, authors="Zhang, Wei", year="2018").json()
    assert {k: body[k] for k in ("id", "error", "suggestion")} == {
        "id": None,
        "error": None,
        "suggestion": None,
    }
    # Junk in, no boxes pre-filled — and no crash reaching for `.strip()` on
    # an int on the way there.
    assert body["segments"] == {
        "year": None,
        "family": None,
        "keyword": None,
        "needs": ["year", "family", "keyword"],
    }


# ---------------------------------------------------------------------------
# The three-part Paper ID field: which third of the id is actually the user's
# ---------------------------------------------------------------------------


def test_derive_id_leaves_only_the_keyword_to_the_user(client: TestClient) -> None:
    """A Chinese title costs the keyword and nothing else.

    The year and the family name are sitting right there and are correct, so
    the form fills them in and asks for one box, not three. Handing back a
    single empty field here is what made a first-time reader think the whole
    id was theirs to invent.
    """
    body = _derive(client, title=_CN_TITLE, authors=["Zhang, Wei"], year=2018).json()
    assert body["id"] is None
    assert body["segments"] == {
        "year": "2018",
        "family": "Zhang",
        "keyword": None,
        "needs": ["keyword"],
    }


def test_derive_id_asks_for_two_parts_when_the_author_is_chinese_too(
    client: TestClient,
) -> None:
    """`小王` slugs to nothing, so the family name is the user's as well."""
    body = _derive(client, title="气泡呼吸", authors=["小王"], year=2024).json()
    assert body["segments"] == {
        "year": "2024",
        "family": None,
        "keyword": None,
        "needs": ["family", "keyword"],
    }


def test_derive_id_prefills_a_keyword_it_still_wants_looked_at(
    client: TestClient,
) -> None:
    """The Latin fragment arrives in the box — but as a starting point.

    It is in `needs`, so the box stays editable and the id is written only
    because someone saw `PROTAC` and let it stand. That is the ASCII gate's
    judgement, not a formality: the same machinery would otherwise offer
    `2018_Zhang_A` for a title whose only Latin character is a compound label.
    """
    body = _derive(
        client, title="一种新型 PROTAC 分子的设计与合成", authors=["Zhang, Wei"], year=2018
    ).json()
    assert body["segments"]["keyword"] == "PROTAC"
    assert body["segments"]["needs"] == ["keyword"]


def test_derive_id_never_prefills_a_keyword_that_names_nothing(
    client: TestClient,
) -> None:
    """`2018_Zhang_A` is the id the gate exists to prevent — not a default."""
    body = _derive(client, title=_CN_TITLE, authors=["Zhang, Wei"], year=2018).json()
    assert body["segments"]["keyword"] is None


def test_preview_splits_the_id_for_a_record_crossref_cannot_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DOI path gets the same boxes — a CNKI-registered DOI lands here."""
    cn = {**SAMPLE_MESSAGE, "title": [_CN_TITLE]}
    monkeypatch.setattr(ri, "fetch_crossref", lambda doi, client=None: cn)
    payload = client.get("/api/ingest/preview", params={"doi": _DOI}).json()
    assert payload["proposedId"] is None
    assert payload["idSegments"]["needs"] == ["keyword"]
    assert payload["idSegments"]["year"] and payload["idSegments"]["family"]


def test_derive_id_rejects_a_non_object_body(client: TestClient) -> None:
    assert client.post("/api/ingest/derive-id", json=["title"]).status_code == 400


# ---------------------------------------------------------------------------
# An explicit id on confirm — the only way a Chinese-titled paper gets in
# ---------------------------------------------------------------------------


def test_an_explicit_id_lets_a_chinese_titled_paper_in(
    client: TestClient, vault: Path
) -> None:
    """The whole point of the field: without it this paper has no way past.

    Asserted against TRUTH and DERIVED both, so it is proof the id rode the
    shared ``_apply_add`` and not some second write path (invariant #16).
    """
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm",
        json={
            "handle": handle,
            "id": "2018_Zhang_Huahewu-A",
            "metadata": {
                "title": _CN_TITLE,
                "authors": ["Zhang, Wei"],
                "year": 2018,
                "journal": "化工学报",
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "2018_Zhang_Huahewu-A"

    paper = vault / "papers" / "2018_Zhang_Huahewu-A"
    assert (paper / "paper.pdf").is_file()
    meta = _yaml.load((paper / "metadata.yaml").read_text(encoding="utf-8"))
    assert meta["title"] == _CN_TITLE
    assert meta["journal"] == "化工学报"

    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert any(p["id"] == "2018_Zhang_Huahewu-A" for p in index["papers"])


def test_the_same_paper_without_an_id_is_refused(
    client: TestClient, vault: Path
) -> None:
    """The reverse half: proof the id is what unblocked the test above.

    Without it this is a 422 and the vault stays empty — so the previous test
    cannot be passing for some unrelated reason.
    """
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm",
        json={
            "handle": handle,
            "metadata": {
                "title": _CN_TITLE,
                "authors": ["Zhang, Wei"],
                "year": 2018,
            },
        },
    )
    assert resp.status_code == 422
    assert not any((vault / "papers").iterdir())


def test_an_explicit_id_works_on_the_doi_path_too(
    client: TestClient, vault: Path, mock_crossref: dict[str, Any]
) -> None:
    """A CrossRef record can fail id derivation just as a typed one can."""
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm",
        json={"handle": handle, "doi": _DOI, "id": "2024_Chen_Chosen-by-hand"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "2024_Chen_Chosen-by-hand"
    assert (vault / "papers" / "2024_Chen_Chosen-by-hand" / "paper.pdf").is_file()


def test_a_typed_id_that_collides_is_an_error_not_a_silent_suffix(
    client: TestClient, vault: Path, mock_crossref: dict[str, Any]
) -> None:
    """A derived id auto-suffixes; a typed one must not.

    Typing an id is a claim about which paper this is. Saving it as `…-2`
    would answer that claim by ignoring it.
    """
    first = _upload(client, _pdf_with_doi()).json()["handle"]
    client.post(
        "/api/ingest/confirm",
        json={"handle": first, "doi": _DOI, "id": "2024_Chen_Taken"},
    )
    second = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm",
        json={"handle": second, "doi": _DOI, "id": "2024_Chen_Taken"},
    )
    assert resp.status_code == 409
    assert not (vault / "papers" / "2024_Chen_Taken-2").exists()


@pytest.mark.parametrize(
    "bad",
    ["../escape", "a/b", "a\\b", "..", ".hidden", "", "   ", 7, ["x"]],
)
def test_a_malformed_id_never_reaches_the_filesystem(
    client: TestClient, vault: Path, bad: Any
) -> None:
    """The id becomes a directory name, so this gate is path traversal defence."""
    handle = _upload(client, _pdf_with_doi()).json()["handle"]
    resp = client.post(
        "/api/ingest/confirm",
        json={
            "handle": handle,
            "id": bad,
            "metadata": {
                "title": "A perfectly good title",
                "authors": ["Zhang, Wei"],
                "year": 2018,
            },
        },
    )
    assert resp.status_code == 400, bad
    assert not any((vault / "papers").iterdir()), bad
