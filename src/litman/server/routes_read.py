"""Read-only API endpoints for the litman webUI (Phase 0).

Every handler delegates to the existing core read layer — it never
re-implements vault scanning or parses derived files by hand (invariant #16 /
ADR-017). The vault is read from ``request.app.state.vault`` (set by
:func:`litman.server.create_app`), so nothing here assumes a path.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from litman.core.checks import all_fixed_enums, fixed_enum_allows_none, run_all_checks
from litman.core.cite import format_acs
from litman.core.config import CONFIG_FILENAME
from litman.core.code import missing_code_clones
from litman.core.document import find_paper, list_papers
from litman.core.id import is_valid_id
from litman.core.query import recency_key
from litman.core.search import search_notes
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
    if not isinstance(papers, list):
        return None
    # Schema-staleness guard: a pre-1.1.1 INDEX.json predates `authors` in the
    # thin projection. Treat it as "not available" so the caller re-projects
    # live off metadata.yaml — the API always serves the current schema; the
    # on-disk INDEX catches up on the vault's next write / refresh-views.
    if papers and isinstance(papers[0], dict) and "authors" not in papers[0]:
        return None
    return papers


# The three left-nav smart-lists. Their membership/ordering depends on
# recency_key (paper.pdf mtime + updated-at), which the INDEX thin projection
# does NOT carry, so a smart-list view is always computed server-side over the
# full metadata returned by ``list_papers`` — reusing the SAME recency_key the
# CLI's ``lit list --sort recent`` uses (invariant #16: one ranking, no second
# sort path).
_SMART_LIST_VIEWS = frozenset({"reading", "recent-read", "backlog"})


def _smart_list(vault: Path, view: str) -> list[dict[str, Any]]:
    """Order ``list_papers`` for one smart-list view.

    - ``reading``     = unread, recency DESC.
    - ``recent-read`` = read (read-date set), read-date DESC.
    - ``backlog``     = unread, recency ASC (same membership as ``reading`` in
      reverse — the tail of the unread list).

    Dropped papers are NOT filtered out: a dropped paper stays in ``reading``
    (if unread) or ``recent-read`` (if read), rendered muted in the GUI, so
    setting a paper aside never makes it vanish from the list (anti-drift).
    Membership is purely read-date-based; ``status`` only changes row styling.
    """
    papers = list_papers(vault)
    if view == "recent-read":
        read = [p for p in papers if p.get("read-date")]
        # str ISO sort is fine; list.sort is stable so equal read-dates keep
        # the incoming id-ascending order from list_papers as a tiebreak.
        read.sort(key=lambda p: str(p.get("read-date") or ""), reverse=True)
        return read

    unread = [p for p in papers if not p.get("read-date")]
    unread.sort(
        key=lambda p: recency_key(vault, p),
        reverse=(view == "reading"),
    )
    return unread


@router.get("/papers")
def get_papers(
    request: Request,
    view: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List papers as the INDEX.json thin projection.

    Without ``view`` (Phase 0 behavior): reads INDEX.json directly when present
    (the agent-facing thin projection, invariant #10); falls back to projecting
    ``list_papers`` through the same ``project_paper`` so the schema is
    byte-identical when INDEX is missing.

    With ``view`` ∈ {reading, recent-read, backlog}: a recency-ordered
    smart-list computed server-side (the recency signal is absent from INDEX),
    returned through the same thin ``project_paper`` schema. Any other ``view``
    value is a 400.
    """
    vault = _vault(request)
    if view is None:
        indexed = _index_papers(vault)
        if indexed is not None:
            return indexed
        return [project_paper(p) for p in list_papers(vault)]

    if view not in _SMART_LIST_VIEWS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown view {view!r}; expected one of "
                f"{sorted(_SMART_LIST_VIEWS)}."
            ),
        )
    return [project_paper(p) for p in _smart_list(vault, view)]


@router.get("/version")
def get_version() -> dict[str, str | None]:
    """Current litman version + the latest available release, if any.

    PURE READ (invariant #16): reads ONLY the local update-check cache — it never
    fetches PyPI in the request path. ``latest`` is the newer version when the
    cache shows one strictly greater than ``current``, else ``null`` (no cache,
    stale/empty, already current, or opted out). The server's startup task
    populates the cache; the TopBar shows a badge only when ``latest`` is set.
    """
    from litman import __version__
    from litman.core.update_check import available_update

    upd = available_update()
    return {"current": __version__, "latest": upd[1] if upd else None}


@router.get("/capabilities")
def get_capabilities(request: Request) -> dict[str, Any]:
    """What this host can do — currently just: which folder-link mechanism works.

    ``links`` is ``"symlink"`` (POSIX), ``"junction"`` (Windows) or ``"none"``
    (a drive that cannot hold links at all — FAT32 / exFAT, network shares).
    The SPA raises its advisory only on ``"none"``; both working mechanisms are
    silent, fully-functional states.

    Cheap enough for the frontend to call on page load, which is the whole
    reason it is not folded into ``GET /health``: that endpoint is Tier-2 (it
    reads every ``metadata.yaml``) and is deliberately fetched only when the
    user opens the health panel. But a GUI-only user needs to be TOLD, at boot,
    why ``views/`` is empty and why the shortcuts never appeared in their
    project folders — the CLI's stderr warning goes nowhere, because the desktop
    shortcut launches the console-less ``litw`` entry point.

    ``link_mechanism`` probes once per directory per process, so the
    long-lived server pays exactly one probe for the life of the process and
    every later boot of the SPA is answered from cache.

    Not a TRUTH write (invariant #16): the probe creates a dot-prefixed,
    pid-suffixed scratch entry and removes it in a ``finally``. It never
    touches metadata, INDEX, or anything under ``papers/``.
    """
    import sys

    from litman.core.portable_link import link_mechanism

    return {
        "links": link_mechanism(_vault(request)),
        "platform": sys.platform,
    }


@router.get("/health")
def get_health(request: Request) -> list[dict[str, Any]]:
    """Run every health-check probe and return the flat ``Issue[]`` list.

    Mirrors ``lit health-check`` (``run_all_checks``: registry drift / schema /
    dangling refs / code-clone integrity / index-vs-disk reconciliation, etc.),
    so a pure-GUI user can self-audit library consistency (ADR-017). Each issue
    serializes to the five Issue fields (``category`` / ``severity`` /
    ``paper_id`` / ``message`` / ``hint``); the panel groups by category and
    colors by severity.

    RED LINE — this endpoint is *only* read (invariant #16): it never re-locks
    TRUTH, never auto-fixes, and never stamps ``last_health_check_at`` on the
    registry. Those are the write side effects of the CLI ``health_check_cmd``;
    the GET surfaces findings only. Repair still goes through
    ``lit health-check --fix``. Tier-2 cost (reads every ``metadata.yaml`` via
    ``list_papers``), so the frontend runs it on demand, never on page load.
    """
    vault = _vault(request)
    papers = list_papers(vault)
    issues = run_all_checks(vault, papers)
    return [dataclasses.asdict(i) for i in issues]


def _stat_mtime(path: Path) -> float | None:
    """st_mtime (epoch seconds) or None when the file is absent — one stat, no read."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


@router.get("/doc-mtimes")
def get_doc_mtimes(request: Request) -> dict[str, dict[str, float | None]]:
    """Per-paper notes.md / discussion.md mtimes (epoch seconds), for change detection.

    PURE READ (invariant #16): stat only — never reads file contents, never writes.
    One entry per paper currently in the vault (same enumeration get_health uses,
    list_papers); each value is Path.stat().st_mtime, or null when absent. The webUI
    resync diff compares this against its previous in-memory snapshot to surface
    notes/discussion edits made OUTSIDE the GUI (agents write these files directly —
    there is no lit command to hook). O(papers) stats per call; fine for single-user.
    """
    vault = _vault(request)
    out: dict[str, dict[str, float | None]] = {}
    for paper in list_papers(vault):
        paper_dir = vault / "papers" / paper["id"]
        out[paper["id"]] = {
            "notes": _stat_mtime(paper_dir / "notes.md"),
            "discussion": _stat_mtime(paper_dir / "discussion.md"),
        }
    return out


def _snippet_window(line: str, query: str, *, width: int = 90, lead: int = 24) -> str:
    """A short, match-centered slice of a matched markdown line for a dropdown row.

    Keeps the matched substring visible: starts ~``lead`` chars before the
    (case-insensitive) match, spans ``width`` chars, and marks any trimmed end
    with an ellipsis. A whole line that already fits is returned untouched
    (only stripped). Trimming is presentation-only — ``core.search`` returns
    full lines for agent retrieval; the GUI is the one place that wants a
    bounded preview.
    """
    line = line.strip()
    if len(line) <= width:
        return line
    idx = line.lower().find(query.lower())
    if idx < 0:  # query spanned across normalization — fall back to a head slice
        return line[:width].rstrip() + "…"
    start = max(0, idx - lead)
    end = min(len(line), start + width)
    snippet = line[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(line):
        snippet = snippet + "…"
    return snippet


@router.get("/search")
def get_search(
    request: Request,
    q: str = Query(default=""),
) -> dict[str, Any]:
    """Substring search over authored ``notes.md`` / ``discussion.md`` (vault-wide).

    Powers the typeahead dropdown's notes / discussion scopes only — id / title
    matching is instant client-side off the already-loaded INDEX projection, so
    this endpoint never re-scans metadata. Same corpus and matcher as
    ``lit search`` (``core.search.search_notes``): case-insensitive substring,
    with ``paper.pdf`` full text, ``.trash/`` and ``views/`` excluded
    (invariant #16: one search path, not a second one reinvented here).

    Collapsed to at most one hit per paper (notes preferred over discussion),
    each carrying a match-centered snippet. The caller caps how many rows it
    renders; the full id set drives the middle list's filter.
    """
    vault = _vault(request)
    query = q.strip()
    if not query:
        return {"query": q, "hits": []}

    # search_notes yields line-level hits ordered by (id, file, line). Collapse
    # to one per paper: the first hit wins, but a later notes hit upgrades a
    # discussion-only entry so notes always outranks discussion per paper.
    best: dict[str, dict[str, Any]] = {}
    for hit in search_notes(vault, query):
        pid = hit["id"]
        cur = best.get(pid)
        # First hit for a paper wins; a later notes hit upgrades a
        # discussion-only entry so notes always outranks discussion per paper.
        if cur is None or (cur["file"] != "notes" and hit["file"] == "notes"):
            best[pid] = hit

    hits = [
        {
            "id": h["id"],
            "scope": h["file"],  # "notes" | "discussion"
            "line": h["line"],
            "snippet": _snippet_window(h["snippet"], query),
        }
        for h in best.values()
    ]
    return {"query": q, "hits": hits}


@router.get("/paper/{paper_id}")
def get_paper(request: Request, paper_id: str) -> dict[str, Any]:
    """Full metadata for one paper (same loader ``lit show`` uses)."""
    vault = _vault(request)
    try:
        meta = find_paper(vault, paper_id)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CorruptMetadataError as exc:
        # The paper exists; its metadata is broken. That is a server-side
        # data problem (500), not a missing resource (404).
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not meta:
        # find_paper returns {} for an empty / comment-only metadata.yaml; treat
        # that as a missing resource (mirrors get_trash_metadata) rather than
        # serving 200 with an empty body.
        raise HTTPException(
            status_code=404,
            detail=f"No usable metadata for paper {paper_id!r} (file is empty).",
        )
    # Derived display hint (not persisted): which code-clones links are dangling
    # — codes/<name>/ gone — so the cockpit marks them instead of showing a
    # deleted codebase as live. Same criterion as lit health-check (invariant #12).
    meta["code-clones-missing"] = missing_code_clones(
        vault, meta.get("code-clones") or []
    )
    return meta


@router.get("/paper/{paper_id}/cite")
def get_paper_cite(request: Request, paper_id: str) -> dict[str, Any]:
    """Compact ACS-style citation for one paper, plus any caveats.

    Same formatting path as ``lit cite`` (``core.cite.format_acs``); the webUI
    copies ``text`` to the clipboard and shows ``warnings`` (unverified journal
    abbreviation, missing fields, preprint venue) next to the button.
    """
    vault = _vault(request)
    try:
        meta = find_paper(vault, paper_id)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CorruptMetadataError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    citation = format_acs(meta)
    return {"text": citation.text, "warnings": citation.warnings}


@router.get("/paper/{paper_id}/pdf")
def get_paper_pdf(request: Request, paper_id: str) -> FileResponse:
    """Serve ``papers/{id}/paper.pdf`` with HTTP range support.

    Starlette's ``FileResponse`` emits ``Accept-Ranges: bytes`` and honors a
    ``Range:`` request header with a ``206 Partial Content`` response, which
    pdf.js relies on to fetch the document in segments.

    ``Cache-Control: no-store`` is mandatory: paper.pdf is mutable (the
    annotation write-back overwrites it in place via :func:`staged_write`), yet
    it is served at a stable URL. Without it the browser caches the bytes (and
    pdf.js's range requests reuse them), so reopening a paper after a save shows
    the *old* file. ``no-store`` forces a fresh fetch every time; on a localhost
    server the re-read is cheap.
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
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        headers={"Cache-Control": "no-store"},
    )


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
    try:
        return {"text": md_path.read_text(encoding="utf-8")}
    except (OSError, UnicodeDecodeError) as exc:
        # Present but unservable (external editor saved it non-UTF-8, cloud
        # client left a husk): damaged data, not a missing resource — the
        # same clean-500 contract as get_paper's CorruptMetadataError arm,
        # never an unhandled traceback.
        raise HTTPException(
            status_code=500,
            detail=(
                f"papers/{paper_id}/{filename} cannot be read "
                f"(not UTF-8, or unreadable): {exc}"
            ),
        ) from exc


def _taxonomy_text(vault: Path) -> str:
    """TAXONOMY.md's text, or a described 500 when it cannot be served.

    A missing / non-UTF-8 TAXONOMY.md is damaged TRUTH (health-check's beat
    on the CLI side). Left unguarded it took down /api/taxonomy AND
    /api/projects with raw tracebacks, which the frontend swallows into a
    silently empty panel.
    """
    path = vault / "TAXONOMY.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "TAXONOMY.md is missing from this library — run "
                "`lit health-check` to diagnose."
            ),
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"TAXONOMY.md cannot be read (not UTF-8, or unreadable): {exc}",
        ) from exc


@router.get("/taxonomy")
def get_taxonomy(request: Request) -> dict[str, list[str]]:
    """The TAXONOMY controlled vocabulary, one list per dict key."""
    vault = _vault(request)
    return parse_taxonomy(_taxonomy_text(vault))


@router.get("/fixed-enums")
def get_fixed_enums() -> dict[str, dict[str, Any]]:
    """Whitelists for the status / priority / type cockpit dropdowns.

    Sourced from ``core.checks`` (the same table ``check_schema`` / ``lit modify
    --set`` validate against), never hard-coded in the frontend. Each field
    carries its allowed ``values`` in display order plus ``allowsNone`` — whether
    the dropdown offers an "— (unset)" option (priority/type, M29; status's
    unevaluated state is the explicit value ``inbox``, so it has none). Vault-
    independent, so it takes no request state.
    """
    enums = all_fixed_enums()
    return {
        field: {"values": values, "allowsNone": fixed_enum_allows_none(field)}
        for field, values in enums.items()
    }


@router.get("/projects")
def get_projects(request: Request) -> list[dict[str, str]]:
    """Registered projects (name / path / status).

    Same JOIN of TAXONOMY.md's projects section and the lit-config.yaml
    projects map that backs ``lit project list``.
    """
    from litman.core.config import load_config

    vault = _vault(request)
    taxonomy_names = set(parse_taxonomy(_taxonomy_text(vault))["projects"])
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
    """Registry entries plus which one is active (for the switch-vault dropdown).

    ``served`` is the vault this running server is actually bound to, or ``None``
    when it started with no vault (welcome-page mode). It is distinct from
    ``active`` (the registry's active *name*): a server can start in no-vault
    mode even while the registry names an active entry whose path has moved, so
    the frontend keys the welcome page off ``served``, not ``active``.

    Each entry carries ``exists``: whether its registered path still holds a
    vault, probed on every call (the registry stores paths, and a folder can be
    moved or deleted behind litman's back at any moment). The sentinel is the
    ``lit-config.yaml`` — the same one the 410 middleware guard stats and the
    same test ``apply_vault_use(require_path=True)`` applies before it will
    switch, so a vault reported ``exists: false`` is exactly a vault ``PUT
    /vaults/active`` would reject — the frontend marks it in the selector rather
    than letting the user pick it and collect a 400. A bare directory (an
    unrelated same-name folder at the old path) reads as missing, not as the
    vault having come back.
    """
    reg = load_registry()
    active = find_active(reg)
    served = request.app.state.vault
    return {
        "active": active.name if active else None,
        "served": str(served) if served is not None else None,
        "vaults": [
            {
                "name": v.name,
                "path": v.path,
                "active": v.is_active,
                "exists": (Path(v.path).expanduser() / CONFIG_FILENAME).is_file(),
            }
            for v in reg.vaults
        ],
    }
