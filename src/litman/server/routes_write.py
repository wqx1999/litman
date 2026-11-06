"""Whitelist direct-write API endpoints for the litman webUI (Phase 2+).

Per invariant #16, the webUI's direct-write surface is a CLOSED whitelist:
``{papers/<id>/paper.pdf annotation increments, notes.md, discussion.md}``.
These are free-form / orthogonal layers with zero structural value to agent
retrieval, so they may be written directly — but ALWAYS through
``core/atomic.py`` ``staged_write`` (atomic ``os.replace``), never a naive open.

Phase 2 wired the PDF-annotation overwrite; phase 3a adds the notes.md and
discussion.md writes (create-or-overwrite; the other two whitelist members).
None of these
handlers scan the vault, touch metadata / INDEX / TAXONOMY, or open any second
write path. The vault is read from ``request.app.state.vault`` (set by
:func:`litman.server.create_app`), so nothing here assumes a path.

Each save may carry ``base``, the version of the file the page started from
(``GET`` notes / discussion give it; ``GET /pdf-version`` for the PDF). When
the file on disk is no longer that version (a sync brought another machine's
copy, an agent rewrote it), the save still wins, and the copy it replaces is
kept in the sync's conflicts backup first; the answer's ``replaced.backup``
names it, so the page can say so. Every answer carries the new ``version``,
so consecutive saves chain. ``base`` is ``"absent"`` for a notes / discussion
file that did not exist when the edit began: one there now is kept too. A save
of exactly what the file already holds keeps nothing (a repeated save).
The copy and the write run in a worker thread, off the server's event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from litman.core.atomic import staged_write
from litman.core.id import is_valid_id
from litman.core.notes import ensure_discussion_scaffold, ensure_wikilink_reminder
from litman.core.sync import keep_replaced_copy
from litman.core.vault_token import fingerprint, md_version

router = APIRouter(prefix="/api")


#: The ``base`` of an edit that began on a notes / discussion file that did
#: not exist yet. No version reads like it.
ABSENT_BASE = "absent"


def _read_md_version(path: Path) -> str:
    return md_version(path.read_bytes())


def _keep_if_changed(
    vault: Path, rel: str, base: str | None, version_of: Callable[[Path], str], data: bytes
) -> dict[str, Any]:
    """Keep ``rel`` in the backup when it is no longer at ``base``; the answer's part.

    Nothing is kept when the file already holds ``data``, the bytes about to
    be written: a save made twice replaces nothing.

    Raises:
        HTTPException: 500 when the copy could not be kept (then nothing is saved).
    """
    path = vault / rel
    if base is None or not path.is_file():
        return {}
    try:
        if base != ABSENT_BASE and version_of(path) == base:
            return {}
        if path.read_bytes() == data:
            return {}
        backup = keep_replaced_copy(vault, rel)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"{rel} changed on disk and a copy of it could not be kept "
                f"({exc.strerror or exc}), so nothing was saved. Try again."
            ),
        ) from exc
    return {"replaced": {"backup": str(backup)}}


def _save(
    vault: Path, rel: str, data: bytes, base: str | None, version_of: Callable[[Path], str]
) -> dict[str, Any]:
    """Keep the copy a save replaces, then write ``data`` to ``rel``: blocking, run in a thread."""
    kept = _keep_if_changed(vault, rel, base, version_of, data)
    with staged_write(vault) as sw:
        sw.write_bytes(rel, data)
    return kept


@router.put("/paper/{paper_id}/pdf-annotations")
async def put_pdf_annotations(request: Request, paper_id: str) -> dict[str, object]:
    """Atomically overwrite ``papers/{id}/paper.pdf`` with annotated bytes.

    The body is the new PDF produced by pdf.js ``saveDocument()`` (the original
    document plus the embedded annotation editor layers). This is the
    invariant #16 first-class direct-write item: it overwrites paper.pdf and
    NOTHING else, and goes through ``staged_write`` so a crash mid-write can
    never destroy the only PDF copy (atomic ``os.replace``).

    - Reject a traversal-style id (mirrors ``get_paper_pdf``) → 404.
    - The paper.pdf must already exist: this is an OVERWRITE-only whitelist
      item, never a create → 404 if absent.
    - An empty body is a client bug (nothing to embed) → 400.
    """
    vault = request.app.state.vault

    if not is_valid_id(paper_id):
        raise HTTPException(status_code=404, detail=f"Invalid paper id: {paper_id!r}.")

    pdf_path = vault / "papers" / paper_id / "paper.pdf"
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail=f"No paper.pdf for {paper_id!r}.")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body.")

    rel = f"papers/{paper_id}/paper.pdf"
    kept = await run_in_threadpool(
        _save, vault, rel, body, request.query_params.get("base"), fingerprint
    )

    return {"ok": True, "bytes": len(body), "version": fingerprint(pdf_path), **kept}


async def _md_text_body(request: Request) -> tuple[str, str | None]:
    """Parse the ``{"text": "...", "base": "..."}`` body shared by the md write endpoints.

    Symmetric with the GET notes/discussion read endpoints, which return
    ``{"text": ..., "version": ...}``. An absent/non-object body, a missing
    ``text`` key, a non-string ``text``, or a blank string are all client
    bugs → 400 (a save with nothing to write is never a legitimate overwrite
    of an authored file). "Blank" is checked after ``strip()`` so a
    whitespace-only ``"  \n"`` is rejected too — otherwise an accidental
    select-all + delete + save would silently blank an authored
    note/discussion. ``base`` is optional; a non-string one is a client bug.
    """
    try:
        payload = await request.json()
    except Exception as exc:  # malformed / non-JSON body
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise HTTPException(status_code=400, detail="Body must be {\"text\": <str>}.")
    text: str = payload["text"]
    if not text.strip():
        raise HTTPException(status_code=400, detail="Empty text.")
    base = payload.get("base")
    if base is not None and not isinstance(base, str):
        raise HTTPException(status_code=400, detail="base must be a string.")
    return text, base


@router.put("/paper/{paper_id}/notes")
async def put_notes(request: Request, paper_id: str) -> dict[str, object]:
    """Atomically overwrite ``papers/{id}/notes.md`` with the posted text.

    The GUI's md tab edits the WHOLE file (a render↔edit toggle, not a patch),
    so this is a full overwrite, not an append. Before writing,
    :func:`ensure_wikilink_reminder` re-inserts the ``[[id]]`` wikilink nudge if
    the edit stripped it (idempotent: present → returned unchanged), matching the
    heal-on-read-close behaviour so the convention survives a human rewrite.

    Reject a traversal-style id → 404, and require the PAPER to exist (its
    ``papers/{id}/`` dir) → 404 if absent. The notes.md file itself is
    CREATE-or-overwrite: unlike paper.pdf (which only ``lit add`` produces),
    notes.md / discussion.md are whitelist members the GUI may create — which it
    still may need to, for a paper predating either scaffold.
    Goes through ``staged_write`` so a crash mid-write can never tear the note.
    """
    vault = request.app.state.vault

    if not is_valid_id(paper_id):
        raise HTTPException(status_code=404, detail=f"Invalid paper id: {paper_id!r}.")

    if not (vault / "papers" / paper_id).is_dir():
        raise HTTPException(status_code=404, detail=f"No such paper: {paper_id!r}.")

    text, base = await _md_text_body(request)
    healed = ensure_wikilink_reminder(text)

    rel = f"papers/{paper_id}/notes.md"
    data = healed.encode("utf-8")
    kept = await run_in_threadpool(_save, vault, rel, data, base, _read_md_version)

    return {"ok": True, "bytes": len(data), "version": md_version(data), **kept}


@router.put("/paper/{paper_id}/discussion")
async def put_discussion(request: Request, paper_id: str) -> dict[str, object]:
    """Atomically write ``papers/{id}/discussion.md`` with the posted text.

    Same whitelist path as :func:`put_notes` (full overwrite, never an append:
    per-line append is the agent SOP and has no GUI caller), with its own
    scaffold heal: :func:`ensure_discussion_scaffold` puts the append-format
    header back if the edit stripped it, the way notes gets its wikilink nudge
    back. Also CREATE-or-overwrite — a paper predating the scaffold has no
    discussion.md until ``lit health-check --fix`` backfills it, so the first
    GUI save can still be the create.
    """
    vault = request.app.state.vault

    if not is_valid_id(paper_id):
        raise HTTPException(status_code=404, detail=f"Invalid paper id: {paper_id!r}.")

    if not (vault / "papers" / paper_id).is_dir():
        raise HTTPException(status_code=404, detail=f"No such paper: {paper_id!r}.")

    text, base = await _md_text_body(request)
    healed = ensure_discussion_scaffold(text, paper_id)

    rel = f"papers/{paper_id}/discussion.md"
    data = healed.encode("utf-8")
    kept = await run_in_threadpool(_save, vault, rel, data, base, _read_md_version)

    return {"ok": True, "bytes": len(data), "version": md_version(data), **kept}
