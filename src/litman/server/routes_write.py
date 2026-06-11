"""Whitelist direct-write API endpoints for the litman webUI (Phase 2+).

Per invariant #16, the webUI's direct-write surface is a CLOSED whitelist:
``{papers/<id>/paper.pdf annotation increments, notes.md, discussion.md}``.
These are free-form / orthogonal layers with zero structural value to agent
retrieval, so they may be written directly — but ALWAYS through
``core/atomic.py`` ``staged_write`` (atomic ``os.replace``), never a naive open.

Phase 2 wires only the PDF-annotation overwrite. The handler does NOT scan the
vault, touch metadata / INDEX / TAXONOMY, or open any second write path. The
vault is read from ``request.app.state.vault`` (set by
:func:`litman.server.create_app`), so nothing here assumes a path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from litman.core.atomic import staged_write
from litman.core.id import is_valid_id

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
