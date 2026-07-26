"""Read-only API endpoints for the litman webUI (Phase 0).

Every handler delegates to the existing core read layer — it never
re-implements vault scanning or parses derived files by hand (invariant #16 /
ADR-017). The vault is read from ``request.app.state.vault`` (set by
:func:`litman.server.create_app`), so nothing here assumes a path.
"""

from __future__ import annotations

import dataclasses
import json
import os
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
from litman.core.views import (
    INDEX_PAPER_FIELDS,
    load_index_papers,
    project_paper,
)
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
    # Schema-staleness guard: an INDEX.json written by an older litman carries
    # an older projection (no `authors`, no `updated-at`). Compare the whole
    # field set rather than probing one name, so adding a field to the
    # projection never again means remembering to edit this line. Treat a
    # mismatch as "not available" so the caller re-projects live off
    # metadata.yaml — the API always serves the current schema; the on-disk
    # INDEX catches up on the vault's next write / refresh-views.
    if papers and (
        not isinstance(papers[0], dict)
        or set(papers[0]) != set(INDEX_PAPER_FIELDS)
    ):
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
    """Order the vault's papers for one smart-list view.

    - ``reading``     = unread, recency DESC.
    - ``recent-read`` = read (read-date set), read-date DESC.
    - ``backlog``     = unread, recency ASC (same membership as ``reading`` in
      reverse — the tail of the unread list).

    Dropped papers are NOT filtered out: a dropped paper stays in ``reading``
    (if unread) or ``recent-read`` (if read), rendered muted in the GUI, so
    setting a paper aside never makes it vanish from the list (anti-drift).
    Membership is purely read-date-based; ``status`` only changes row styling.
    """
    if view == "recent-read":
        # Both the membership test and the sort key are read-date, which the
        # INDEX projection carries — so a verified INDEX serves this view
        # without opening a single metadata.yaml. Same order either way: INDEX
        # entries are id-sorted, exactly like list_papers' output.
        indexed = load_index_papers(vault)
        papers = indexed if indexed is not None else list_papers(vault)
        read = [p for p in papers if p.get("read-date")]
        # str ISO sort is fine; list.sort is stable so equal read-dates keep
        # the incoming id-ascending order as a tiebreak.
        read.sort(key=lambda p: str(p.get("read-date") or ""), reverse=True)
        return read

    # reading / backlog rank by recency_key, which reads `updated-at` and the
    # PDF's mtime. `updated-at` is in the projection, so a verified INDEX
    # serves the default view too: 300 stat() calls instead of 300 YAML
    # parses. The projection serializes `updated-at` back to a full ISO string
    # (views._timestamp_to_iso), which recency_key parses to the same instant
    # it computes from the typed object a scan hands it — so the order is
    # identical on both paths, not merely similar.
    indexed = load_index_papers(vault)
    papers = indexed if indexed is not None else list_papers(vault)
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
    One entry per paper currently in the vault (same enumeration get_health uses);
    each value is Path.stat().st_mtime, or null when absent. The webUI resync diff
    compares this against its previous in-memory snapshot to surface notes/discussion
    edits made OUTSIDE the GUI (agents write these files directly — there is no lit
    command to hook). O(papers) stats per call; fine for single-user.

    The endpoint needs ids and nothing else, so a verified INDEX supplies them —
    the resync sweep fires on every window focus, and parsing every metadata.yaml
    for a field it does not read made the GUI pay a full vault scan per focus. A
    stale / older-schema INDEX falls back to ``list_papers``, whose id set (which
    drops broken paper dirs) the probe already matches, so the enumeration is the
    same either way.
    """
    vault = _vault(request)
    indexed = load_index_papers(vault)
    papers = indexed if indexed is not None else list_papers(vault)
    out: dict[str, dict[str, float | None]] = {}
    for paper in papers:
        paper_id = str(paper["id"])
        paper_dir = vault / "papers" / paper_id
        out[paper_id] = {
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
    """Full metadata for one paper (same loader ``lit show`` uses).

    The projection fields come through ``project_paper``, exactly as
    ``GET /api/papers`` serves them, and every other key in metadata.yaml —
    ``relevance-<project>``, ``created-at``, the relation lists, any custom
    field a user invented — is passed through untouched.

    Why the projection is applied at all, when this endpoint's job is to
    return everything: metadata.yaml is schemaless (invariant #7), so a
    missing field simply is not there. Served raw, one paper could arrive
    with ``topics`` and the next without it, while the frontend's PaperMeta
    extends IndexPaper and declares all of them present. Consumers happen to
    defend themselves today; the projection makes the two endpoints agree on
    the fields they share, so they no longer have to.
    """
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

    out = project_paper(meta)
    out.update(
        {k: v for k, v in meta.items() if k not in INDEX_PAPER_FIELDS}
    )
    # Derived display hint (not persisted): which code-clones links are dangling
    # — codes/<name>/ gone — so the cockpit marks them instead of showing a
    # deleted codebase as live. Same criterion as lit health-check (invariant #12).
    out["code-clones-missing"] = missing_code_clones(
        vault, meta.get("code-clones") or []
    )
    return out


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


# ===========================================================================
# Filesystem directory picker (vault-independent) — backs the GUI's "Browse…".
# ===========================================================================
#
# Pure, read-only enumeration of server-side directories so the frontend path
# fields can offer "Browse…" instead of forcing users to type an absolute path
# (task-path-browser). It lists ONLY subdirectories — never files, never file
# contents — and never writes. It does not read the vault or re-implement any
# vault logic (invariant #16): ``is_vault`` is a single stat probing whether a
# child holds a ``lit-config.yaml``, nothing more. The server is bound to
# 127.0.0.1 and runs as the user who already owns these files, so no path
# sandbox is imposed — a single-user local tool must be able to browse its own
# disk (spec §2 security note).


def _fs_anchors() -> list[dict[str, str]]:
    """Standard one-click locations for the directory picker.

    Home is always present; Desktop / Documents / Downloads are listed only
    when they exist as directories (a headless HPC account often has none of
    the three — the picker then shows just Home).
    """
    home = Path.home()
    anchors = [{"label": "Home", "path": str(home)}]
    for label in ("Desktop", "Documents", "Downloads"):
        candidate = home / label
        if candidate.is_dir():
            anchors.append({"label": label, "path": str(candidate)})
    return anchors


def _suggested_start() -> Path:
    """Where the picker lands with no ``path``: first existing of
    Desktop → Documents → Home.

    Shared by ``GET /api/fs/list`` (empty ``path``) and the create-vault
    default parent dir (spec §5, consumed by the frontend's empty ``listDir()``
    call): put new libraries somewhere the user can actually see them. Home
    always exists, so this always returns a real directory.
    """
    home = Path.home()
    for name in ("Desktop", "Documents"):
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home


def _directory_listing(target: Path, show_hidden: bool = False) -> dict[str, Any]:
    """Assemble the picker payload for an ALREADY-resolved directory.

    The ``{path, is_vault, parent, entries, anchors, denied}`` half of the picker
    response, factored out of :func:`get_fs_list` so ``POST /api/fs/mkdir`` can
    return the freshly created (or entered) folder in the exact same shape — the
    frontend then treats that response as one more navigation (invariant #16
    spirit: the two fs endpoints share one listing path, not two copies).
    ``target`` must already be a resolved, existing directory; each caller does
    the path resolution / validation appropriate to its own input.
    """
    entries: list[dict[str, Any]] = []
    denied = False
    try:
        with os.scandir(target) as scan:
            for entry in scan:
                try:
                    name = entry.name
                    if not show_hidden and name.startswith("."):
                        continue
                    if not entry.is_dir():  # follows symlinks; files skipped
                        continue
                    child = Path(entry.path)
                    entries.append(
                        {
                            "name": name,
                            "path": str(child),
                            "is_vault": (child / CONFIG_FILENAME).is_file(),
                        }
                    )
                except OSError:
                    # One unstattable entry (dead symlink, races) is skipped,
                    # never fatal to the whole listing.
                    continue
    except PermissionError:
        # The directory can be stat'd but not read: degrade to an empty,
        # flagged listing so the picker says "can't open this" instead of 500.
        denied = True

    entries.sort(key=lambda e: e["name"].lower())

    # Whether the folder being listed is *itself* a litman library. Gates the
    # picker's ``vault-dir`` mode: the frontend reads this rather than
    # remembering the clicked entry's flag, so it stays correct when the user
    # lands here via an anchor chip or an address-bar paste instead. Guarded:
    # on an unreadable folder (the ``denied`` case) the sentinel stat itself
    # raises PermissionError — that must degrade to ``false``, not a 500.
    try:
        is_vault = (target / CONFIG_FILENAME).is_file()
    except OSError:
        is_vault = False

    parent = target.parent
    return {
        "path": str(target),
        "is_vault": is_vault,
        "parent": str(parent) if parent != target else None,
        "entries": entries,
        "anchors": _fs_anchors(),
        "denied": denied,
    }


@router.get("/fs/list")
def get_fs_list(
    path: str | None = Query(default=None),
    show_hidden: int = Query(default=0),
) -> dict[str, Any]:
    """List the subdirectories of one server-side folder, for the path picker.

    ``path`` — absolute directory to list; ``~`` is expanded. Omitted / empty
    lands on the suggested start (Desktop → Documents → Home). A bad ``path``
    (does not exist, or is not a directory) is a 400 whose human-readable
    ``detail`` the picker shows inline — never a silent empty 200.

    ``show_hidden`` — ``1`` also lists dot-prefixed directories; the default
    ``0`` hides them.

    Returns the listed ``path`` (expanded + resolved), whether that folder is
    itself a library (``is_vault``), its ``parent`` (``null`` at the filesystem
    root), the subdirectory ``entries`` (name-sorted, case-insensitive; each
    flagged ``is_vault`` when it holds a ``lit-config.yaml``), the existing
    ``anchors``, and ``denied`` — ``true`` when the folder can be reached but
    its children cannot be read (``PermissionError``), which degrades to an
    empty listing instead of a 500.
    """
    candidate = Path(path).expanduser() if path else _suggested_start()
    try:
        target = candidate.resolve()
    except OSError as exc:
        # resolve() can raise on a broken symlink chain / lookup loop — a path
        # that cannot even be resolved is bad input, not a server crash.
        raise HTTPException(
            status_code=400, detail=f"Cannot open folder: {path}"
        ) from exc
    if not target.exists():
        raise HTTPException(status_code=400, detail=f"No such folder: {path}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a folder: {path}")
    return _directory_listing(target, show_hidden=bool(show_hidden))


@router.post("/fs/mkdir")
async def post_fs_mkdir(request: Request) -> dict[str, Any]:
    """Create ONE subfolder under an existing directory, for the path picker.

    Body JSON ``{"parent": "<absolute dir>", "name": "<single folder name>"}``.
    The GUI's first write-to-disk endpoint: the picker's "＋ New folder" button
    (existing-dir / parent-dir modes only) posts here, and the welcome-page
    library flow uses it to create a parent folder before ``lit init`` — so it is
    whitelisted in the no-vault middleware guard.

    RED LINE — only ONE level, and only here: ``parent`` must ALREADY exist and be
    a directory (``mkdir()`` without ``parents=True``), and ``name`` is rejected
    if it contains ``/`` / ``\\`` / ``..`` or is ``.`` / ``..`` (a single component
    only — no traversal, no multi-level create). Nothing else on the server writes
    to disk on a browse.

    Behaviour:

    * ``parent`` missing / not a directory → 400.
    * bad ``name`` → 400, and nothing is created.
    * ``target`` already a directory → 200, its listing (idempotent: equivalent to
      navigating into an existing folder — wangq 2026-07-23).
    * ``target`` already a *file* → 400.
    * otherwise ``target.mkdir()``; a ``PermissionError`` degrades to 403 and any
      other ``OSError`` to 400 (mirrors ``fs/list``'s ``denied`` style — never 500).

    Returns the SAME ``{path, is_vault, parent, entries, anchors, denied}`` shape
    as ``GET /api/fs/list`` (computed on ``target``), so the frontend consumes it
    as one navigation and lands inside the new folder.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    parent_raw = payload.get("parent")
    if not isinstance(parent_raw, str) or not parent_raw.strip():
        raise HTTPException(status_code=400, detail="parent must be a non-empty string.")
    name_raw = payload.get("name")
    if not isinstance(name_raw, str):
        raise HTTPException(status_code=400, detail="name must be a string.")
    name = name_raw.strip()

    # 1. Parent must already exist and be a directory — never a multi-level mkdir.
    #    Every probe (resolve/exists/is_dir) can raise inside a locked ANCESTOR
    #    (PermissionError → 403) or on a malformed path with an embedded NUL
    #    (ValueError → 400); a broken symlink chain / lookup loop is OSError → 400.
    #    None of them may escape as a 500.
    parent = Path(parent_raw).expanduser()
    try:
        parent = parent.resolve()
        parent_exists = parent.exists()
        parent_is_dir = parent.is_dir()
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail=f"Permission denied opening: {parent_raw}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Cannot open folder: {parent_raw}"
        ) from exc
    if not parent_exists:
        raise HTTPException(
            status_code=400, detail=f"Parent folder does not exist: {parent}"
        )
    if not parent_is_dir:
        raise HTTPException(status_code=400, detail=f"Parent is not a folder: {parent}")

    # 2. Name: exactly one path component (RED LINE — no separators, no traversal,
    #    no control chars / NUL — a NUL would make mkdir() raise ValueError → 500).
    if not name:
        raise HTTPException(status_code=400, detail="Folder name can't be empty.")
    if name in (".", ".."):
        raise HTTPException(status_code=400, detail="Folder name can't be '.' or '..'.")
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(
            status_code=400, detail="Folder name can't contain path separators."
        )
    if any(ord(ch) < 32 or ch == "\x7f" for ch in name):
        raise HTTPException(
            status_code=400, detail="Folder name can't contain control characters."
        )

    # 3-6. Create (or idempotently enter) exactly one level under parent. Every
    # filesystem probe here can raise PermissionError inside a locked parent
    # (M6) — that degrades to 403, and any other OSError (e.g. a mid-request race
    # that removes parent) to 400. Never a 500.
    target = parent / name
    try:
        already_dir = target.is_dir()
        if not already_dir and target.exists():
            raise HTTPException(
                status_code=400, detail="A file with that name already exists."
            )
        if not already_dir:
            target.mkdir()  # single level; parent verified above (no parents=True)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied creating a folder in {parent}.",
        ) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Couldn't create the folder: {getattr(exc, 'strerror', None) or exc}.",
        ) from exc

    return _directory_listing(target)
