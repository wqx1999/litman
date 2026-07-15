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
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from litman.core.atomic import staged_write
from litman.core.id import is_valid_id
from litman.core.notes import ensure_discussion_scaffold, ensure_wikilink_reminder

router = APIRouter(prefix="/api")


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

    with staged_write(vault) as sw:
        sw.write_bytes(f"papers/{paper_id}/paper.pdf", body)

    return {"ok": True, "bytes": len(body)}


async def _md_text_body(request: Request) -> str:
    """Parse the ``{"text": "..."}`` JSON body shared by the md write endpoints.

    Symmetric with the GET notes/discussion read endpoints, which return
    ``{"text": ...}``. An absent/non-object body, a missing ``text`` key, a
    non-string ``text``, or a blank string are all client bugs → 400 (a save
    with nothing to write is never a legitimate overwrite of an authored file).
    "Blank" is checked after ``strip()`` so a whitespace-only ``"  \n"`` is
    rejected too — otherwise an accidental select-all + delete + save would
    silently blank an authored note/discussion.
    """
    try:
        payload = await request.json()
    except Exception as exc:  # malformed / non-JSON body
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise HTTPException(status_code=400, detail="Body must be {\"text\": <str>}.")
    text = payload["text"]
    if not text.strip():
        raise HTTPException(status_code=400, detail="Empty text.")
    return text


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

    text = await _md_text_body(request)
    healed = ensure_wikilink_reminder(text)

    with staged_write(vault) as sw:
        sw.write_text(f"papers/{paper_id}/notes.md", healed)

    return {"ok": True, "bytes": len(healed.encode("utf-8"))}


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

    text = await _md_text_body(request)
    healed = ensure_discussion_scaffold(text, paper_id)

    with staged_write(vault) as sw:
        sw.write_text(f"papers/{paper_id}/discussion.md", healed)

    return {"ok": True, "bytes": len(healed.encode("utf-8"))}
