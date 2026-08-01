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

Step 2 is skippable. CrossRef does not have every paper — a patent has no DOI
at all, and a Chinese journal article usually has a real one registered with
CNKI rather than CrossRef, so "not found" is not "does not exist". Those go in
through the same confirm endpoint carrying hand-entered ``metadata`` instead
of a DOI, validated by the same schema ``lit add --from-llm-json`` uses
(:func:`litman.importers.llm.validate_candidate_metadata`) and written by the
same ``_apply_add``. A third input channel, still not a second write path.

``DELETE /api/ingest/{handle}`` discards a stash the user walked away from.

The browser upload is inherently a COPY of the user's file, so ``lit add``'s
mv semantics consume only our temp file — the user's original PDF is never
touched, let alone removed.

Every blocking step (writing the upload, parsing the PDF, the CrossRef call,
the vault write) runs in a threadpool, never on the event loop: a slow or
unreachable CrossRef would otherwise freeze *every* other request — the paper
list, the PDF viewer — for the full 10 s HTTP timeout. Only the body read and
the JSON parse stay async, because that is where the request actually is.

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
from starlette.concurrency import run_in_threadpool

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
from litman.importers.llm import validate_candidate_metadata

router = APIRouter(prefix="/api")

# Uploads land in a dot-directory at the vault root, named for the same
# machine-local-transient family as ``.litman-staging`` (core/atomic.py): same
# filesystem as papers/, so the final ingest copy stays cheap and the eventual
# unlink is a plain same-device operation. Like ``.litman-staging`` it is on
# the sync hard-exclude list (core/sync.py) — an abandoned upload is scratch,
# never something to push to someone's cloud.
_UPLOAD_DIRNAME = ".litman-upload"

# Generous for article PDFs (a heavy scanned book chapter is ~50 MB); mainly
# a guard against buffering something absurd into memory on a mis-drop.
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# A stash the user walked away from (tab closed, browser crashed) is swept on
# the next upload and at server startup. The dialog's own Cancel deletes its
# stash outright, so reaching this TTL means the page never got to say goodbye.
_UPLOAD_TTL_SECONDS = 24 * 3600

# At startup no page is loaded yet, so every stash on disk is orphaned by
# definition and the 24 h wait is pointless — but a second `lit gui` on the
# same vault could be mid-drag, so leave a minute's grace rather than none.
_STARTUP_TTL_SECONDS = 60

# Handles are server-minted uuid4 hex — anything else is refused before it
# can reach the filesystem (no path traversal via a crafted handle).
_HANDLE_RE = re.compile(r"^[0-9a-f]{32}$")

_TOO_BIG = "PDF is larger than 100 MB — add it via the CLI instead."


def _upload_dir(vault: Path) -> Path:
    d = vault / _UPLOAD_DIRNAME
    try:
        d.mkdir(exist_ok=True)
    except OSError as exc:
        # A read-only vault, a full disk, or a stray *file* sitting on the
        # name: report it as a server-side problem in words, never as an
        # unhandled traceback behind the dialog.
        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not create the upload staging folder "
                f"{_UPLOAD_DIRNAME!r} in the vault ({exc.__class__.__name__}). "
                "Add this paper via the CLI instead."
            ),
        ) from exc
    return d


def sweep_uploads(
    vault: Path, ttl_seconds: float = _UPLOAD_TTL_SECONDS
) -> int:
    """Delete stashed uploads older than ``ttl_seconds``. Never raises.

    Called on every upload and once at server startup (with the much shorter
    startup TTL), so a stash orphaned by a closed tab or a crash cannot
    outlive the next ``lit gui`` — there is nothing here for the user to
    notice, let alone clean up by hand.

    Returns the number of files removed (for tests / callers that log).
    """
    tmp_dir = vault / _UPLOAD_DIRNAME
    now = time.time()
    removed = 0
    try:
        entries = list(tmp_dir.iterdir())
    except OSError:
        return 0
    for p in entries:
        try:
            if now - p.stat().st_mtime > ttl_seconds:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


async def _read_capped_body(request: Request) -> bytes:
    """Read the request body, refusing anything over the cap.

    Streamed rather than ``await request.body()``: a request without a
    ``content-length`` (chunked) would otherwise be buffered whole in memory
    *before* the size check could run, which is the one thing the cap exists
    to prevent. The declared length is still honoured first — that refuses an
    oversized upload without transferring it at all.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_TOO_BIG)

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=_TOO_BIG)
        chunks.append(chunk)
    return b"".join(chunks)


def _stash_and_sniff(vault: Path, body: bytes) -> dict[str, Any]:
    """Write the upload into its stash and sniff DOI candidates (blocking)."""
    tmp_dir = _upload_dir(vault)
    sweep_uploads(vault)

    handle = uuid.uuid4().hex
    tmp_path = tmp_dir / f"{handle}.pdf"
    tmp_path.write_bytes(body)

    candidates = sniff_dois(tmp_path)
    return {
        "handle": handle,
        "doi": candidates[0] if candidates else None,
        "candidates": candidates,
    }


@router.post("/ingest/pdf")
async def post_ingest_pdf(request: Request) -> dict[str, Any]:
    """Stash a dropped PDF and sniff its DOI. Body = raw PDF bytes.

    Raw bytes rather than multipart on purpose: FastAPI's multipart parsing
    needs the extra ``python-multipart`` dependency, and the browser can send
    a File object as a fetch body directly — zero new dependencies.
    """
    body = await _read_capped_body(request)
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
    # Disk write + a full pypdf parse: 0.25 s median measured on real article
    # PDFs, but a fat scanned one runs into seconds — off the event loop.
    return await run_in_threadpool(_stash_and_sniff, vault, body)


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

    Declared ``def``, not ``async def``: FastAPI runs sync handlers in a
    threadpool, so the CrossRef call here never blocks the event loop.
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


def _ingest_confirmed(
    vault: Path,
    handle: str,
    doi: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the metadata and run the stash through the shared backend.

    Blocking. Exactly one of ``doi`` (fetch CrossRef) or ``metadata`` (the
    hand-entry form) arrives filled — the caller enforces that.
    """
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
        if metadata is not None:
            # Same schema, same validators, same normalized shape as
            # `lit add --from-llm-json`: hand-entered metadata is refused for
            # a placeholder title or first author exactly where an agent's
            # would be. The GUI opens no second set of rules, as it opens no
            # second write path.
            parsed = validate_candidate_metadata(
                metadata, context="Cannot add this paper"
            )
        else:
            parsed = parse_crossref(fetch_crossref(doi or ""))
    except ImporterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # A hand-entered DOI is kept and deduped on exactly like a fetched one.
    # It is often perfectly real — Chinese journals register with CNKI rather
    # than CrossRef, so "CrossRef does not know it" is not "it does not
    # exist" — and a paper with no DOI at all (a patent) simply dedups on
    # nothing, which is what `lit add --from-llm-json` already does.
    effective_doi = parsed.get("doi") or (doi or "")
    source_label = (
        f"DOI {doi!r}" if metadata is None else "the details you entered"
    )
    try:
        result = _apply_add(
            vault,
            tmp_path,
            parsed,
            doi_for_dedup=effective_doi,
            source_label=source_label,
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


@router.post("/ingest/confirm")
async def post_ingest_confirm(request: Request) -> dict[str, Any]:
    """Ingest a stashed upload through the shared ``lit add`` backend.

    Body: ``{"handle": <from /ingest/pdf>, "doi": <confirmed DOI>}`` — or,
    when CrossRef cannot supply the record, ``{"handle": …, "metadata":
    {title, authors, year, …}}`` in the schema ``lit add --from-llm-json``
    takes. Exactly one of the two; sending both is a mistake worth naming
    rather than resolving by precedence.

    On the DOI path CrossRef is fetched again here (not trusted from the
    preview response) so the written metadata can never drift from what the
    confirmed DOI resolves to; the dedup precheck reruns inside ``_apply_add``
    for the same reason. Id collisions auto-suffix — the GUI shows the final
    id in the response.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    handle = payload.get("handle")
    doi_raw = payload.get("doi")
    metadata = payload.get("metadata")
    if not isinstance(handle, str) or not _HANDLE_RE.match(handle):
        raise HTTPException(status_code=400, detail="Unknown upload handle.")

    has_doi = isinstance(doi_raw, str) and bool(doi_raw.strip())
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise HTTPException(
                status_code=400, detail="'metadata' must be a JSON object."
            )
        if has_doi:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Send either 'doi' (look it up) or 'metadata' (enter it "
                    "by hand), not both. A hand-entered DOI belongs inside "
                    "'metadata'."
                ),
            )
        vault: Path = request.app.state.vault
        return await run_in_threadpool(
            _ingest_confirmed, vault, handle, None, metadata
        )

    if not has_doi:
        raise HTTPException(
            status_code=400, detail="Either 'doi' or 'metadata' is required."
        )
    doi = canonicalize_doi(str(doi_raw).strip())

    vault = request.app.state.vault
    # CrossRef (10 s timeout) + the vault write + the INDEX/views reconcile:
    # all blocking, none of it allowed to stall the rest of the GUI.
    return await run_in_threadpool(_ingest_confirmed, vault, handle, doi)


@router.delete("/ingest/{handle}")
def delete_ingest(request: Request, handle: str) -> dict[str, bool]:
    """Discard a stashed upload the user dismissed.

    Fired by the confirm dialog's Cancel / Esc / backdrop close, so a PDF the
    user changed their mind about leaves nothing behind; the TTL sweep is only
    for stashes whose page never got to say goodbye.

    Idempotent: an already-consumed (successful add) or already-swept handle
    answers ``discarded: false``, not an error — this is cleanup, and the
    dialog must never show the user a failure for it.
    """
    if not _HANDLE_RE.match(handle):
        raise HTTPException(status_code=400, detail="Unknown upload handle.")
    vault: Path = request.app.state.vault
    tmp_path = vault / _UPLOAD_DIRNAME / f"{handle}.pdf"
    try:
        tmp_path.unlink()
    except OSError:
        return {"discarded": False}
    return {"discarded": True}
