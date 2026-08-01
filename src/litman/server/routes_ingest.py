"""webUI drag-in ingest endpoints (task-gui-doi-add).

Three steps, one write path:

1. ``POST /api/ingest/pdf`` — raw PDF bytes in, temp file + sniffed DOI
   candidates out. Nothing touches the vault's papers/ yet.
2. ``GET /api/ingest/preview`` — a DOI in, CrossRef metadata + duplicate
   check + proposed paper id out. Pure read (plus one explicit CrossRef
   call — this is a user-initiated action, unlike the never-blocking
   update-check probe, which stays untouched).
3. ``POST /api/ingest/confirm`` — temp handle + confirmed DOI in; the ingest
   runs through :func:`litman.commands.add._apply_add`, the very function
   ``lit add`` calls, so a GUI drop and a terminal add produce byte-identical
   vault state (invariant #16: the GUI opens no second write path).

The browser upload is inherently a COPY of the user's file, so ``lit add``'s
mv semantics consume only our temp file — the user's original PDF is never
touched, let alone removed.

None of these routes is in ``_VAULTLESS_ALLOWED``: ingesting needs a vault,
and the welcome page doesn't mount the drop zone anyway.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from litman.commands.add import (
    _PDF_MAGIC,
    _PDF_SNIFF_BYTES,
    _apply_add,
    _first_author_family,
    _looks_like_pdf,
)
from litman.core.dedup import auto_suffix_id, canonicalize_doi, find_paper_by_doi
from litman.core.doi_sniff import sniff_dois
from litman.core.id import derive_id
from litman.exceptions import (
    AddError,
    DuplicateDOIError,
    IDError,
    ImporterError,
)
from litman.importers.crossref import fetch_crossref, parse_crossref

router = APIRouter(prefix="/api")

# Uploads land in a dot-directory at the vault root (sibling of `.trash`):
# same filesystem as papers/, so the final ingest copy stays cheap and the
# eventual unlink is a plain same-device operation.
_UPLOAD_DIRNAME = ".upload-tmp"

# Generous for article PDFs (a heavy scanned book chapter is ~50 MB); mainly
# a guard against buffering something absurd into memory on a mis-drop.
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Abandoned uploads (dialog dismissed, tab closed) are swept on the next
# upload rather than by any background job.
_UPLOAD_TTL_SECONDS = 24 * 3600

# Handles are server-minted uuid4 hex — anything else is refused before it
# can reach the filesystem (no path traversal via a crafted handle).
_HANDLE_RE = re.compile(r"^[0-9a-f]{32}$")


def _upload_dir(vault: Path) -> Path:
    d = vault / _UPLOAD_DIRNAME
    d.mkdir(exist_ok=True)
    return d


def _sweep_stale(tmp_dir: Path) -> None:
    """Best-effort removal of uploads older than the TTL. Never raises."""
    now = time.time()
    try:
        entries = list(tmp_dir.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if now - p.stat().st_mtime > _UPLOAD_TTL_SECONDS:
                p.unlink()
        except OSError:
            continue


@router.post("/ingest/pdf")
async def post_ingest_pdf(request: Request) -> dict[str, Any]:
    """Stash a dropped PDF and sniff its DOI. Body = raw PDF bytes.

    Raw bytes rather than multipart on purpose: FastAPI's multipart parsing
    needs the extra ``python-multipart`` dependency, and the browser can send
    a File object as a fetch body directly — zero new dependencies.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="PDF is larger than 100 MB — add it via the CLI instead.",
        )
    body = await request.body()
    if len(body) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="PDF is larger than 100 MB — add it via the CLI instead.",
        )
    # Same magic-number rule as `lit add` (Content-Type is not trustworthy):
    # a renamed .txt or an HTML error page saved as .pdf is refused before
    # anything lands on disk.
    if _PDF_MAGIC not in body[:_PDF_SNIFF_BYTES]:
        raise HTTPException(
            status_code=400,
            detail=(
                "That file does not look like a PDF (missing the %PDF- "
                "header). Drop the paper's PDF file."
            ),
        )

    vault: Path = request.app.state.vault
    tmp_dir = _upload_dir(vault)
    _sweep_stale(tmp_dir)

    handle = uuid.uuid4().hex
    tmp_path = tmp_dir / f"{handle}.pdf"
    tmp_path.write_bytes(body)

    candidates = sniff_dois(tmp_path)
    return {
        "handle": handle,
        "doi": candidates[0] if candidates else None,
        "candidates": candidates,
    }


@router.get("/ingest/preview")
def get_ingest_preview(
    request: Request, doi: str = Query(...)
) -> dict[str, Any]:
    """Resolve a DOI to CrossRef metadata + dedup verdict + proposed id.

    Read-only against the vault. ``inVault`` non-null means the Add button
    must stay disabled — replacing an existing paper is a CLI affair.
    ``proposedId`` may be null with ``idError`` explaining why (CrossRef
    record lacks a year or a first author); those rare papers go in via the
    CLI/agent path with an explicit ``--id``.
    """
    doi = canonicalize_doi(doi.strip())
    if not doi:
        raise HTTPException(status_code=400, detail="DOI is required.")

    vault: Path = request.app.state.vault
    existing = find_paper_by_doi(vault, doi)
    in_vault = (
        {"id": existing[0], "title": existing[1].get("title") or ""}
        if existing is not None
        else None
    )

    try:
        parsed = parse_crossref(fetch_crossref(doi))
    except ImporterError as exc:
        # The importer's messages already distinguish "DOI not found in
        # CrossRef" from a network failure; surface them verbatim.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    proposed_id: str | None = None
    id_error: str | None = None
    try:
        family = _first_author_family(parsed.get("authors") or [])
        if parsed.get("year") is None:
            raise IDError(
                "The CrossRef record has no publication year, which the "
                "paper id needs. Add this one via the CLI/agent with an "
                "explicit --id."
            )
        if not family:
            raise IDError(
                "The CrossRef record has no first-author name, which the "
                "paper id needs. Add this one via the CLI/agent with an "
                "explicit --id."
            )
        primary = derive_id(parsed["year"], family, parsed["title"])
        proposed_id = (
            auto_suffix_id(vault, primary)
            if (vault / "papers" / primary).exists()
            else primary
        )
    except IDError as exc:
        id_error = str(exc)

    return {
        "doi": parsed.get("doi") or doi,
        "title": parsed.get("title") or "",
        "authors": parsed.get("authors") or [],
        "year": parsed.get("year"),
        "journal": parsed.get("journal") or "",
        "proposedId": proposed_id,
        "idError": id_error,
        "inVault": in_vault,
    }


@router.post("/ingest/confirm")
async def post_ingest_confirm(request: Request) -> dict[str, Any]:
    """Ingest a stashed upload through the shared ``lit add`` backend.

    Body: ``{"handle": <from /ingest/pdf>, "doi": <confirmed DOI>}``.
    CrossRef is fetched again here (not trusted from the preview response) so
    the written metadata can never drift from what the confirmed DOI resolves
    to; the dedup precheck reruns inside ``_apply_add`` for the same reason.
    Id collisions auto-suffix — the GUI shows the final id in the response.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    handle = payload.get("handle")
    doi_raw = payload.get("doi")
    if not isinstance(handle, str) or not _HANDLE_RE.match(handle):
        raise HTTPException(status_code=400, detail="Unknown upload handle.")
    if not isinstance(doi_raw, str) or not doi_raw.strip():
        raise HTTPException(status_code=400, detail="DOI is required.")
    doi = canonicalize_doi(doi_raw.strip())

    vault: Path = request.app.state.vault
    tmp_path = vault / _UPLOAD_DIRNAME / f"{handle}.pdf"
    if not tmp_path.is_file():
        raise HTTPException(
            status_code=400,
            detail="Upload expired or unknown — drop the PDF again.",
        )
    # Re-check the magic: the upload endpoint verified the bytes it received,
    # but the file has sat on disk since (defense in depth, same helper the
    # CLI uses).
    if not _looks_like_pdf(tmp_path):
        raise HTTPException(
            status_code=400,
            detail="Stored upload is not a valid PDF — drop the PDF again.",
        )

    try:
        parsed = parse_crossref(fetch_crossref(doi))
    except ImporterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = _apply_add(
            vault,
            tmp_path,
            parsed,
            doi_for_dedup=parsed.get("doi") or doi,
            source_label=f"DOI {doi!r}",
            id_override=None,
            resolve_collision=lambda pid, yr, fam: auto_suffix_id(vault, pid),
        )
    except DuplicateDOIError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IDError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AddError as exc:
        # Case-fold clash and friends: a conflict with existing vault state.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"id": result["paper_id"], "warnings": result["warnings"]}
