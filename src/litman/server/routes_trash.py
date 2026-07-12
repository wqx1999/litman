"""Trash (recoverable-delete bin) API endpoints for the litman webUI (Phase 4.9).

These are invariant #16 SECOND-class reads/writes: every handler is a thin
wrapper over ``core.trash`` (the same layer ``lit trash {list, restore}`` uses).
The server NEVER re-implements trash enumeration or restore logic — restore
mirrors ``commands/trash.py:trash_restore_cmd`` exactly (resolve →
``restore_from_trash`` → ``reconcile_derived``), minus the interactive re-clone
step (decision (b): the endpoint never re-clones; it only hands ``missing_repos``
back for the CLI / health-check to act on).

Path-traversal defense (red line ⑤): ``entry_name`` arrives from the URL, so a
file-serving handler must NEVER synthesize ``vault/'.trash'/entry_name`` from
the raw param. It is always resolved through
``core.trash.resolve_trash_entry(vault, entry_name)``, and files are read only
relative to the returned ``TrashEntry.entry_path``. A ``TrashError`` from
resolve (entry not found) → 404.

The vault is read from ``request.app.state.vault`` (set by
:func:`litman.server.create_app`), so nothing here assumes a path.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from litman.core.config import load_config
from litman.core.correctors import reconcile_derived
from litman.core.document import list_papers, read_metadata_or_raise
from litman.core.trash import (
    TrashEntry,
    list_trash,
    resolve_trash_entry,
    restore_from_trash,
)
from litman.exceptions import CorruptMetadataError, TrashError

router = APIRouter(prefix="/api")


def _vault(request: Request) -> Path:
    return request.app.state.vault


def _resolve(vault: Path, entry_name: str) -> TrashEntry:
    """Resolve an entry-name to its on-disk ``TrashEntry`` or 404.

    The single choke point that turns a URL param into a filesystem path. All
    file-serving handlers go through here so the path is never built from the
    raw param (traversal defense). The GUI always passes the unique
    ``entry_name``, so ``resolve_trash_entry`` hits the exact-match branch or
    raises ``TrashError`` (empty trash / no match) — both map to 404.
    """
    try:
        return resolve_trash_entry(vault, entry_name)
    except TrashError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _read_trash_md(entry: TrashEntry, filename: str) -> dict[str, str]:
    """Read a markdown file from inside a resolved trash entry. 404 if absent."""
    md_path = entry.entry_path / filename
    if not md_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"No {filename} in trash entry {entry.entry_name!r}.",
        )
    try:
        return {"text": md_path.read_text(encoding="utf-8")}
    except (OSError, UnicodeDecodeError) as exc:
        # Same clean-500 contract as the live-paper markdown routes: damaged
        # data is described, never an unhandled traceback.
        raise HTTPException(
            status_code=500,
            detail=(
                f"{filename} in trash entry {entry.entry_name!r} cannot be "
                f"read (not UTF-8, or unreadable): {exc}"
            ),
        ) from exc


@router.get("/trash")
def get_trash(request: Request) -> list[dict[str, object]]:
    """List trash entries (newest-first) as a thin projection for the trash view.

    Delegates to ``core.trash.list_trash`` (already sorted newest-first) — the
    same enumeration ``lit trash list`` uses. Projects each entry to the fields
    the GUI renders; ``orphan_repo_count`` lets the list hint that a restore
    will surface repos needing a CLI re-clone.
    """
    vault = _vault(request)
    return [
        {
            "paper_id": e.paper_id,
            "title": e.title,
            "deleted_at": e.deleted_at,
            "entry_name": e.entry_name,
            "orphan_repo_count": len(e.orphan_repos),
        }
        for e in list_trash(vault)
    ]


@router.get("/trash/{entry_name}")
def get_trash_metadata(request: Request, entry_name: str) -> dict[str, object]:
    """The trashed paper's ``metadata.yaml`` (read from the resolved entry path).

    Mirrors ``routes_read.get_paper``: a missing or empty/comment-only
    metadata.yaml is a 404 (no usable paper to show — same as ``find_paper``
    treats an absent file), an unparseable / non-UTF-8 file is a 500
    (``CorruptMetadataError``: the paper exists, its data is broken).
    """
    vault = _vault(request)
    entry = _resolve(vault, entry_name)
    meta_path = entry.entry_path / "metadata.yaml"
    if not meta_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"No metadata.yaml in trash entry {entry.entry_name!r}.",
        )
    try:
        meta = read_metadata_or_raise(meta_path)
    except CorruptMetadataError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not meta:
        # Empty / comment-only YAML → no metadata to render. read_metadata_or_raise
        # returns {} here (no raise); align with find_paper, which treats a paper
        # with no usable metadata as not-found rather than serving an empty {}.
        raise HTTPException(
            status_code=404,
            detail=f"Empty metadata.yaml in trash entry {entry.entry_name!r}.",
        )
    return meta


@router.get("/trash/{entry_name}/notes")
def get_trash_notes(request: Request, entry_name: str) -> dict[str, str]:
    """Raw ``notes.md`` from a trashed paper. 404 when absent."""
    vault = _vault(request)
    entry = _resolve(vault, entry_name)
    return _read_trash_md(entry, "notes.md")


@router.get("/trash/{entry_name}/discussion")
def get_trash_discussion(request: Request, entry_name: str) -> dict[str, str]:
    """Raw ``discussion.md`` from a trashed paper. 404 when absent."""
    vault = _vault(request)
    entry = _resolve(vault, entry_name)
    return _read_trash_md(entry, "discussion.md")


@router.get("/trash/{entry_name}/pdf")
def get_trash_pdf(request: Request, entry_name: str) -> FileResponse:
    """Serve a trashed paper's ``paper.pdf`` with HTTP range support.

    Mirrors ``routes_read.get_paper_pdf``: ``FileResponse`` emits
    ``Accept-Ranges: bytes`` and honors ``Range:`` with ``206 Partial Content``
    for pdf.js. A trashed PDF is immutable (nothing writes into ``.trash/``), so
    the ``no-store`` header the live endpoint needs is omitted.
    """
    vault = _vault(request)
    entry = _resolve(vault, entry_name)
    pdf_path = entry.entry_path / "paper.pdf"
    if not pdf_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"No paper.pdf in trash entry {entry.entry_name!r}.",
        )
    return FileResponse(pdf_path, media_type="application/pdf")


@router.post("/trash/{entry_name}/restore")
def post_trash_restore(request: Request, entry_name: str) -> dict[str, object]:
    """Restore a trashed paper to ``papers/<id>/`` and rebuild its relations.

    Mirrors ``commands/trash.py:trash_restore_cmd`` (the structured-write core
    path, invariant #16): resolve → ``restore_from_trash`` (id-slot check,
    folder move, symmetric reverse-edge rebuild — all atomic) →
    ``reconcile_derived`` (INDEX + views recomputed together; ``project_refs``
    False because ``restore_from_trash`` already rebuilt the paper's project
    symlinks). It deliberately does NOT call ``_handle_missing_repos`` /
    re-clone (decision (b)): a hard-deleted 1:1 repo stays bound and is reported
    via ``missing_repos`` for the CLI / ``lit health-check`` to handle.

    Errors:
        * entry not found → 404 (via ``_resolve``).
        * id slot already held by a live paper → 409 (``restore_from_trash``
          raises ``TrashError`` with "already exists"); message passed through
          verbatim.
        * any other ``TrashError`` (unreachable from the GUI's exact-entry-name
          calls — defensive) → 400.
    """
    vault = _vault(request)
    entry = _resolve(vault, entry_name)
    registry = load_config(vault).projects

    try:
        result = restore_from_trash(vault, entry, registry=registry)
    except TrashError as exc:
        # A live paper occupying the id slot is the one expected runtime
        # collision (decision (b)): 409. resolve already handled "not found",
        # so any remaining TrashError is unexpected from the GUI → 400.
        status = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    # Same shared funnel the CLI uses (M30 Phase 4): INDEX + views together.
    # project_refs=False — restore_from_trash already rebuilt the restored
    # paper's project symlinks + REFERENCES.md.
    reconcile_derived(vault, papers=list_papers(vault), project_refs=False)

    return {
        "paper_id": result.paper_id,
        "title": result.title,
        "reverse_edges_rebuilt": sorted(result.reverse_edges_rebuilt),
        "repos_rebound": sorted(result.repos_rebound),
        "projects_rebuilt": sorted(result.projects_rebuilt),
        "missing_repos": dict(result.missing_repos),
        "dead_edges_dropped": sorted(result.dead_edges_dropped),
    }
