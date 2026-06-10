"""Read-only API endpoints for the litman webUI (Phase 0).

Every handler delegates to the existing core read layer — it never
re-implements vault scanning or parses derived files by hand (invariant #16 /
ADR-017). The vault is read from ``request.app.state.vault`` (set by
:func:`litman.server.create_app`), so nothing here assumes a path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from litman.core.document import find_paper, list_papers
from litman.core.id import is_valid_id
from litman.core.taxonomy import parse_taxonomy
from litman.core.vault_registry import find_active, load_registry
from litman.core.views import project_paper
from litman.exceptions import CorruptMetadataError, PaperNotFoundError

router = APIRouter(prefix="/api")


def _vault(request: Request) -> Path:
    return request.app.state.vault


def _index_papers(vault: Path) -> list[dict[str, Any]] | None:
    """Return the INDEX.json ``papers`` projection, or ``None`` if absent.

    Mirrors the thin-projection read used elsewhere (e.g.
    ``checks._load_vault_paper_ids``): a missing or unparseable INDEX.json is
    "not available", and the caller falls back to ``list_papers``.
    """
    index_path = vault / "INDEX.json"
    if not index_path.is_file():
        return None
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    papers = payload.get("papers")
    return papers if isinstance(papers, list) else None


@router.get("/papers")
def get_papers(request: Request) -> list[dict[str, Any]]:
    """List papers as the INDEX.json thin projection.

    Reads INDEX.json directly when present (the agent-facing thin projection,
    invariant #10); falls back to projecting ``list_papers`` through the same
    ``project_paper`` so the schema is byte-identical when INDEX is missing.
    """
    vault = _vault(request)
    indexed = _index_papers(vault)
    if indexed is not None:
        return indexed
    return [project_paper(p) for p in list_papers(vault)]


@router.get("/paper/{paper_id}")
def get_paper(request: Request, paper_id: str) -> dict[str, Any]:
    """Full metadata for one paper (same loader ``lit show`` uses)."""
    vault = _vault(request)
    try:
        return find_paper(vault, paper_id)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CorruptMetadataError as exc:
        # The paper exists; its metadata is broken. That is a server-side
        # data problem (500), not a missing resource (404).
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/paper/{paper_id}/pdf")
def get_paper_pdf(request: Request, paper_id: str) -> FileResponse:
    """Serve ``papers/{id}/paper.pdf`` with HTTP range support.

    Starlette's ``FileResponse`` emits ``Accept-Ranges: bytes`` and honors a
    ``Range:`` request header with a ``206 Partial Content`` response, which
    pdf.js relies on to fetch the document in segments.
    """
    vault = _vault(request)
    # Defense-in-depth: build the path only from an id the core layer would
    # accept (rejects ``..`` / ``/`` / ``\``), matching get_paper's validation
    # instead of relying on Starlette not re-expanding percent-decoded slashes.
    if not is_valid_id(paper_id):
        raise HTTPException(status_code=404, detail=f"Invalid paper id: {paper_id!r}.")
    pdf_path = vault / "papers" / paper_id / "paper.pdf"
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail=f"No paper.pdf for {paper_id!r}.")
    return FileResponse(pdf_path, media_type="application/pdf")


@router.get("/paper/{paper_id}/notes")
def get_paper_notes(request: Request, paper_id: str) -> dict[str, str]:
    """Raw ``notes.md`` text. 404 when the file is absent."""
    return _read_paper_md(request, paper_id, "notes.md")


@router.get("/paper/{paper_id}/discussion")
def get_paper_discussion(request: Request, paper_id: str) -> dict[str, str]:
    """Raw ``discussion.md`` text. 404 when the file is absent."""
    return _read_paper_md(request, paper_id, "discussion.md")


def _read_paper_md(request: Request, paper_id: str, filename: str) -> dict[str, str]:
    vault = _vault(request)
    # Defense-in-depth: same id validation as get_paper / get_paper_pdf so the
    # filesystem path can never escape the vault via a traversal-style id.
    if not is_valid_id(paper_id):
        raise HTTPException(status_code=404, detail=f"Invalid paper id: {paper_id!r}.")
    md_path = vault / "papers" / paper_id / filename
    if not md_path.is_file():
        raise HTTPException(status_code=404, detail=f"No {filename} for {paper_id!r}.")
    return {"text": md_path.read_text(encoding="utf-8")}


@router.get("/taxonomy")
def get_taxonomy(request: Request) -> dict[str, list[str]]:
    """The TAXONOMY controlled vocabulary, one list per dict key."""
    vault = _vault(request)
    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    return parse_taxonomy(text)


@router.get("/projects")
def get_projects(request: Request) -> list[dict[str, str]]:
    """Registered projects (name / path / status).

    Same JOIN of TAXONOMY.md's projects section and the lit-config.yaml
    projects map that backs ``lit project list``.
    """
    from litman.core.config import load_config

    vault = _vault(request)
    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    taxonomy_names = set(parse_taxonomy(text)["projects"])
    config_map = load_config(vault).projects
    config_names = set(config_map)

    out: list[dict[str, str]] = []
    for name in sorted(taxonomy_names | config_names):
        in_tax = name in taxonomy_names
        in_cfg = name in config_names
        path_str = config_map.get(name, "")
        if in_tax and in_cfg:
            path_ok = bool(path_str) and Path(path_str).expanduser().is_dir()
            status = "ok" if path_ok else "path-missing"
        elif in_cfg:
            status = "config-only"
        else:
            status = "taxonomy-only"
        out.append({"name": name, "path": path_str, "status": status})
    return out


@router.get("/vaults")
def get_vaults(request: Request) -> dict[str, Any]:
    """Registry entries plus which one is active (for the switch-vault dropdown)."""
    reg = load_registry()
    active = find_active(reg)
    return {
        "active": active.name if active else None,
        "vaults": [
            {"name": v.name, "path": v.path, "active": v.is_active}
            for v in reg.vaults
        ],
    }
