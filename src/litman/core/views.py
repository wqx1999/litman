"""Derive INDEX.json and views/by-*/ from papers/*/metadata.yaml.

The metadata files are the single source of truth; INDEX.json and the
``views/by-{project,topic,method,status}/`` link hubs are regenerated
wholesale by ``lit refresh-views``. Links route through
``core.portable_link``: relative symlinks on POSIX (so ``cp -r`` to a new
machine still resolves them), junctions on Windows.

On filesystems that cannot hold links (FAT32/exFAT, network shares), the
link hubs are silently skipped via ``core.portable_link``'s
graceful-degrade contract (ADR-005). INDEX.json and metadata.yaml stay
authoritative regardless.

INDEX.json is consumed primarily by AI assistants and programmatic tooling.
Humans should browse via ``lit list`` (filterable, paginated) instead of
opening the JSON directly. JSON has no native comment syntax, so the
``_comment`` field carries the auto-generated banner.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

from litman.core.dates import now_iso
from litman.core.portable_link import make_portable_link, remove_link_if_present

# Views whose tag values come from a list-typed metadata field.
LIST_VIEW_FIELDS: dict[str, str] = {
    "by-project": "projects",
    "by-topic": "topics",
    "by-method": "methods",
}

# Views whose tag values come from a single scalar metadata field.
SCALAR_VIEW_FIELDS: dict[str, str] = {
    "by-status": "status",
}

# Per-paper fields included in INDEX.json. A summary projection of
# metadata.yaml — AI consumers can `cat` the source file for fields not
# listed here. `authors` joined in v1.1.1 (it was deliberately out of the
# original thin projection) so both the GUI quick-search and `lit list
# --format json` consumers can match/read authors without a per-paper load.
INDEX_PAPER_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "authors",
    "year",
    "type",
    "priority",
    "status",
    "topics",
    "projects",
    "methods",
    "data",
    "doi",
    "read-date",
)

# Fields stored as lists in metadata.yaml — emitted as `[]` when absent so
# downstream consumers don't have to special-case None.
_LIST_FIELDS = {"authors", "topics", "projects", "methods", "data"}

# Date-typed scalar fields. The YAML safe-loader parses "2026-05-26" into a
# datetime.date (not a string), which json.dumps cannot serialize — coerce
# to the canonical YYYY-MM-DD string so INDEX.json / `lit list --format json`
# stay JSON-clean and string-comparable.
_DATE_FIELDS = {"read-date"}


def _date_to_iso(value: Any) -> Any:
    """Normalize a date/datetime field to a YYYY-MM-DD string.

    A datetime.date (datetime is a date subclass) becomes its ISO calendar
    date. Everything else (already a string, or None) passes through.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _safe_name(value: str) -> str:
    name = value.replace("/", "_").replace("\\", "_").strip()
    # Neutralize path-traversal / current-dir names (review A3): after slashes
    # are gone, the only way a value can still escape its bucket is by being a
    # bare "." or "..", which would make views/by-X/<name> resolve to
    # views/by-X/ or views/ itself. Prefix them (and the empty string) so the
    # result is always a single, non-traversing path segment.
    if name in ("", ".", ".."):
        return "_" + name
    return name


def project_paper(p: dict[str, Any]) -> dict[str, Any]:
    """Pick the INDEX.json subset of fields from a metadata dict.

    Public so ``lit list --format json`` can reuse the exact same
    projection, keeping its per-paper schema byte-identical to
    INDEX.json (no second projection definition can drift).
    """
    out: dict[str, Any] = {}
    for field in INDEX_PAPER_FIELDS:
        value = p.get(field)
        if field in _LIST_FIELDS:
            out[field] = list(value) if value else []
        elif field in _DATE_FIELDS:
            out[field] = _date_to_iso(value)
        else:
            out[field] = value
    return out


# Backward-compat alias: existing internal callers / tests reference the
# private name. Keep it pointing at the public function so neither has to
# change.
_project_paper = project_paper


def _build_by_doi(papers: list[dict[str, Any]]) -> dict[str, str]:
    """Build the ``by_doi`` reverse map: normalized DOI → paper id.

    DOIs are case-insensitive per the DOI Handbook; normalized to lowercase
    + stripped so future ``lit show --doi`` / AI lookups can hit without
    normalizing again. Last-write wins on collisions, but a vault with two
    papers sharing one DOI is already a health-check violation surfaced by
    M2.8 — this map is best-effort, not the dedup source of truth.
    """
    # Local import avoids a hard import cycle when views.py is imported during
    # library.create_vault() before dedup's heavier ruamel-typ machinery loads.
    from litman.core.dedup import normalize_doi

    out: dict[str, str] = {}
    for p in papers:
        doi = p.get("doi")
        paper_id = p.get("id")
        if not doi or not paper_id:
            continue
        key = normalize_doi(str(doi))
        if not key:
            continue
        out[key] = str(paper_id)
    return out


def render_index(papers: list[dict[str, Any]], timestamp: str) -> str:
    """Render the INDEX.json content for a given paper list.

    Returns a JSON string with stable key order, two-space indent, and
    UTF-8 characters preserved (no ``\\uXXXX`` escapes) so the file stays
    readable for AI consumers.
    """
    sorted_papers = sorted(papers, key=lambda x: str(x.get("id", "")))
    payload = {
        "_comment": "AUTO-GENERATED by `lit refresh-views` — DO NOT EDIT BY HAND",
        "generated_at": timestamp,
        "n_papers": len(sorted_papers),
        "papers": [_project_paper(p) for p in sorted_papers],
        "by_doi": _build_by_doi(sorted_papers),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_index(vault: Path, papers: list[dict[str, Any]]) -> Path:
    """Write the rendered INDEX.json to ``<vault>/INDEX.json`` and return its path.

    Uses a tmp-file + ``os.replace`` rename (same pattern as
    ``sync.write_sync_state`` / ``vault_registry``) so a crash mid-write never
    leaves a half-written INDEX.json behind — a corrupt INDEX would break
    ``lit list`` / ``lit show`` / ``lit search`` and every agent workflow.
    """
    target = vault / "INDEX.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(render_index(papers, now_iso()), encoding="utf-8")
    tmp.replace(target)
    return target


def load_index_ids(vault: Path) -> set[str] | None:
    """Return the set of paper ids recorded in ``<vault>/INDEX.json``.

    Reads ONLY ``INDEX.json`` — never per-paper ``metadata.yaml`` (invariant
    #15), so it is safe in the Tier-1 hot path. Returns ``None`` when the file
    is absent or unparseable (the caller decides how to surface that — the
    Tier-1 ``index_vs_disk`` check treats a missing INDEX as "nothing to
    reconcile yet", while a corrupt one would be surfaced by the full-tier
    structural check).
    """
    target = vault / "INDEX.json"
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    papers = payload.get("papers") or []
    return {
        str(p.get("id"))
        for p in papers
        if isinstance(p, dict) and p.get("id")
    }


def disk_paper_ids(vault: Path) -> set[str]:
    """Directory names under ``<vault>/papers/`` — one readdir, no file reads.

    The cheap freshness probe for :func:`load_index_papers`: on Linux
    ``os.scandir`` answers ``is_dir`` from the directory entry itself, so a
    300-paper vault costs one syscall-ish listing instead of 300 YAML parses.
    """
    papers_dir = vault / "papers"
    try:
        with os.scandir(papers_dir) as it:
            return {entry.name for entry in it if entry.is_dir()}
    except OSError:
        return set()


def load_index_papers(
    vault: Path, *, pending_ids: set[str] | None = None
) -> list[dict[str, Any]] | None:
    """Load INDEX.json's per-paper projections as a verified paper list.

    The read-side fast path (the documented agent contract: one JSON read
    instead of a per-paper YAML scan). Returns the ``papers`` entries only
    when every freshness probe passes; on ANY doubt it returns ``None`` and
    the caller falls back to ``list_papers`` — INDEX stays a derived
    artifact, never a second source of truth. Specifically ``None`` when:

    * INDEX.json is missing, unreadable, or not valid JSON;
    * an entry is not a dict, lacks an id, or its key set differs from
      ``INDEX_PAPER_FIELDS`` (an INDEX written by an older/newer litman —
      the next write command regenerates it wholesale);
    * the entry id set does not exactly match the ``papers/`` directory
      listing (stale after a manual copy/delete; also any vault holding a
      broken paper dir, which ``list_papers`` would drop — conservative:
      those vaults keep today's full-scan behaviour until repaired).

    Args:
        vault: Vault root.
        pending_ids: Paper ids whose directory is ALREADY on disk but which
            the caller knows are not in INDEX yet — ``lit add``'s just-written
            paper, whose metadata the caller holds in memory. They are
            excluded from the id-set probe (and only from it), so a
            mid-ingest vault still takes the fast path; every other staleness
            signal is checked exactly as before.

    Deliberately silent on fallback: surfacing INDEX↔disk drift is owned by
    the Tier-1 hook (``index_vs_disk``) and ``lit health-check``, and the
    JSON output modes downstream must stay clean for parsers.

    The returned dicts carry ONLY the projection fields — enough for
    ``lit list`` filters, INDEX re-rendering and the views/ buckets, but NOT
    for REFERENCES.md (``relevance-<project>`` lives only in metadata.yaml).
    Callers needing full metadata must scan.
    """
    target = vault / "INDEX.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    entries = payload.get("papers")
    if not isinstance(entries, list):
        return None
    expected_keys = set(INDEX_PAPER_FIELDS)
    papers: list[dict[str, Any]] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != expected_keys
            or not entry.get("id")
        ):
            return None
        papers.append(entry)
    ids = {str(p["id"]) for p in papers}
    if len(ids) != len(papers):
        return None
    if ids | (pending_ids or set()) != disk_paper_ids(vault):
        return None
    return papers


def rewrite_index_dropping_ids(vault: Path, dead_ids: set[str]) -> int:
    """Remove ``dead_ids`` from the existing ``INDEX.json`` without reading metadata.

    Operates purely on the on-disk ``INDEX.json`` (the ``papers`` projection
    list + the ``by_doi`` reverse map), filtering out every entry whose ``id``
    is in ``dead_ids`` and re-deriving ``by_doi`` from the surviving entries.
    This is the metadata-free klass-A INDEX repair the Tier-1 hook needs
    (invariant #15: the hook MUST NOT call ``list_papers`` / open any
    ``metadata.yaml``).

    Returns the number of entries actually dropped (0 when INDEX is absent /
    unparseable or none of ``dead_ids`` were present).
    """
    target = vault / "INDEX.json"
    if not target.is_file():
        return 0
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    papers = payload.get("papers") or []
    if not isinstance(papers, list):
        return 0

    kept = [
        p
        for p in papers
        if not (isinstance(p, dict) and str(p.get("id")) in dead_ids)
    ]
    n_dropped = len(papers) - len(kept)
    if n_dropped == 0:
        return 0

    # Re-derive by_doi from the surviving entries so a dropped paper's DOI does
    # not linger in the reverse map. Cheap (operates on the already-thin
    # projection, no metadata read).
    kept_ids = {str(p.get("id")) for p in kept if isinstance(p, dict)}
    by_doi = payload.get("by_doi") or {}
    if isinstance(by_doi, dict):
        by_doi = {k: v for k, v in by_doi.items() if str(v) in kept_ids}
    else:
        by_doi = {}

    payload["papers"] = kept
    payload["n_papers"] = len(kept)
    payload["by_doi"] = by_doi
    payload["generated_at"] = now_iso()
    # tmp-file + rename: same crash-safety as write_index (a half-written
    # INDEX would break every read path + the Tier-1 drift hook).
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    return n_dropped


def _clear_view_subdir(view_dir: Path) -> None:
    """Empty (or create) a view bucket directory.

    Deletes both symlinks and tag-bucket subdirectories under ``view_dir``
    so that paper IDs / tag values no longer present in the metadata
    don't leave stale entries on disk.
    """
    if view_dir.exists():
        for child in view_dir.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
    else:
        view_dir.mkdir(parents=True, exist_ok=True)


def rebuild_views(
    vault: Path, papers: list[dict[str, Any]]
) -> dict[str, int]:
    """Rebuild ``views/by-*/`` symlink hubs from the given paper list.

    Returns a mapping ``{view_name: n_symlinks_created}`` for caller-side
    reporting. On platforms where symlink creation is refused (Windows
    without dev mode, FAT32, etc.), individual entries are silently
    skipped — the count reflects actual symlinks created, not requested.
    A one-shot warning is emitted by ``portable_link`` on first failure.
    """
    views_dir = vault / "views"
    counts: dict[str, int] = {}

    for view_name, field_name in LIST_VIEW_FIELDS.items():
        view_dir = views_dir / view_name
        _clear_view_subdir(view_dir)
        n = 0
        for p in papers:
            paper_id = p.get("id")
            if not paper_id:
                continue
            for value in p.get(field_name) or []:
                bucket = view_dir / _safe_name(str(value))
                bucket.mkdir(parents=True, exist_ok=True)
                if make_portable_link(
                    bucket / paper_id, vault / "papers" / paper_id
                ):
                    n += 1
        counts[view_name] = n

    for view_name, field_name in SCALAR_VIEW_FIELDS.items():
        view_dir = views_dir / view_name
        _clear_view_subdir(view_dir)
        n = 0
        for p in papers:
            paper_id = p.get("id")
            value = p.get(field_name)
            if not paper_id or not value:
                continue
            bucket = view_dir / _safe_name(str(value))
            bucket.mkdir(parents=True, exist_ok=True)
            if make_portable_link(
                bucket / paper_id, vault / "papers" / paper_id
            ):
                n += 1
        counts[view_name] = n

    return counts


def view_fields_snapshot(metadata: dict[str, Any]) -> dict[str, Any]:
    """Copy the view-driving fields out of a metadata dict.

    Taken BEFORE edit ops run — the tag ops mutate the list values in
    place, so :func:`update_views_for_paper` needs a defensive copy of the
    before-state to diff bucket membership against.
    """
    snap: dict[str, Any] = {}
    for field_name in LIST_VIEW_FIELDS.values():
        snap[field_name] = list(metadata.get(field_name) or [])
    for field_name in SCALAR_VIEW_FIELDS.values():
        snap[field_name] = metadata.get(field_name)
    return snap


def _bucket_names(
    fields: dict[str, Any], field_name: str, is_list: bool
) -> set[str]:
    """Bucket directory names a paper occupies for one view dimension."""
    if is_list:
        return {_safe_name(str(v)) for v in (fields.get(field_name) or [])}
    value = fields.get(field_name)
    return {_safe_name(str(value))} if value else set()


def update_views_for_paper(
    vault: Path,
    paper_id: str,
    old_fields: dict[str, Any],
    new_fields: dict[str, Any],
) -> dict[str, int]:
    """Incrementally update ``views/by-*/`` for one paper's field edit.

    The single-paper-edit counterpart of :func:`rebuild_views`, and
    equivalent by construction on a consistent tree: only buckets whose
    membership changed between ``old_fields`` and ``new_fields`` (both from
    :func:`view_fields_snapshot`) are touched. The paper's link is removed
    from every bucket it left — and an emptied bucket directory is removed,
    since a full rebuild would never have created it — and upserted into
    every bucket it joined. Buckets it stays in, and every other paper's
    links, are not visited at all, which is the point: a 300-paper vault
    pays two link operations for a status change instead of a full wipe and
    ~1200 re-links.

    Maintains consistency, never establishes it: a views/ tree damaged by
    hand keeps its damage outside the edited buckets until a full rebuild
    (``lit health-check --fix`` / ``lit refresh-views`` / any write command
    still on the wholesale path) repairs it — the same trust boundary as
    INDEX.json's fast read path.

    Same graceful-degrade contract as ``rebuild_views``: on a filesystem
    without links, creations no-op with the one-shot warning and removals
    find nothing to remove. Returns ``{view_name: n_links_created}``
    (creations only, mirroring ``rebuild_views``' counting).
    """
    views_dir = vault / "views"
    target = vault / "papers" / paper_id
    view_specs = [
        (view_name, field_name, True)
        for view_name, field_name in LIST_VIEW_FIELDS.items()
    ] + [
        (view_name, field_name, False)
        for view_name, field_name in SCALAR_VIEW_FIELDS.items()
    ]

    counts: dict[str, int] = {}
    for view_name, field_name, is_list in view_specs:
        old_buckets = _bucket_names(old_fields, field_name, is_list)
        new_buckets = _bucket_names(new_fields, field_name, is_list)
        n = 0
        for bucket_name in sorted(old_buckets - new_buckets):
            bucket = views_dir / view_name / bucket_name
            remove_link_if_present(bucket / paper_id)
            try:
                # Only succeeds when the bucket is now empty — matching the
                # full rebuild, which never materializes an empty bucket.
                bucket.rmdir()
            except OSError:
                pass
        for bucket_name in sorted(new_buckets - old_buckets):
            bucket = views_dir / view_name / bucket_name
            bucket.mkdir(parents=True, exist_ok=True)
            if make_portable_link(bucket / paper_id, target):
                n += 1
        counts[view_name] = n
    return counts
