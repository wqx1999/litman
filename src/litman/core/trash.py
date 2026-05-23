"""Recoverable-delete bin under ``<vault>/.trash/``.

`lit rm` defaults to moving the paper folder here instead of permanently
deleting it. `lit trash {list, restore, empty}` operates on this directory.

Layout::

    <vault>/.trash/
    ├── 2024_Foo-20260510T184612Z/                  # the moved paper folder
    │   ├── metadata.yaml
    │   ├── paper.pdf
    │   └── notes.md
    └── 2024_Foo-20260510T184612Z.meta.yaml         # sidecar (deleted_at, title, orphan_repos)

The sidecar carries human-readable trash metadata so ``lit trash list`` can
render a useful summary without re-reading the moved paper's metadata.yaml.
It also records the ``orphan_repos`` map — repos hard-deleted because the
removed paper was their last binder (1:1 case) — so M23.2 restore knows
which upstream URLs to re-clone from. A missing sidecar is tolerated:
enumeration falls back to parsing the entry name for the paper id and
timestamp.

Older trash entries (pre-M23.1) carry a ``cascade_was_used`` key that the
post-M23.1 writer no longer emits; readers tolerate its absence (default
``False``).

Atomicity: the sidecar is written first, then the paper folder is moved
via ``shutil.move`` (POSIX ``os.rename`` if same FS — guaranteed atomic).
On a failure between the two writes, the sidecar is dropped to avoid
orphaned metadata. Restore is the inverse: move first, then drop the
sidecar.

The trash directory is at the vault root (alongside ``papers/`` and
``.litman-staging/``), so ``list_papers(vault / 'papers')`` never sees
trashed entries — they are absent from INDEX.json and views/.
"""

from __future__ import annotations

import io
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.code import CODES_DIRNAME, REPO_DIRNAME, REPO_META_FILENAME
from litman.core.document import list_papers
from litman.core.notes import (
    deannotate_deleted_wikilinks,
    enumerate_markdown_files,
)
from litman.core.portable_link import make_relative_symlink
from litman.core.project_link import CODE_SUBDIR
from litman.core.project_refs import LITERATURE_SUBDIR, write_references_md
from litman.core.relations import ALL_REF_FIELDS, RELATION_PAIRS
from litman.core.views import render_index
from litman.exceptions import TrashError

TRASH_DIRNAME = ".trash"

# Count-based ring eviction: keep at most the N most-recently-deleted
# entries. When `move_to_trash` pushes past N, the oldest are permanently
# removed (see `enforce_cap`). Hardcoded by design — no flag, no tunable
# (ADR-011). N=100 gives a generous undo window under single-paper curation
# (invariant #13: deletion is rare).
TRASH_MAX_ENTRIES = 100

_yaml = YAML(typ="safe")
_yaml_dump = YAML()
_yaml_dump.indent(mapping=2, sequence=4, offset=2)
_yaml_dump.default_flow_style = False

# Round-trip writer for restore's symmetric reverse-edge rebuild: it edits
# opposite papers' / repos' existing metadata, so comments and quoting must
# survive (mirrors the writer rm.py uses for the inverse teardown).
_yaml_rt = YAML()
_yaml_rt.indent(mapping=2, sequence=4, offset=2)
_yaml_rt.preserve_quotes = True
_yaml_rt.default_flow_style = False


def _dump_rt_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml_rt.dump(data, buf)
    return buf.getvalue()

# Entry name format: <paper_id>-<UTC-timestamp>
# Timestamp is YYYYMMDDTHHMMSSZ (compact ISO 8601 basic).
_ENTRY_NAME_RE = re.compile(r"^(.+?)-(\d{8}T\d{6}Z)$")


@dataclass
class TrashEntry:
    """One row of `lit trash list` output."""

    paper_id: str
    deleted_at: str          # ISO 8601 with timezone, or "(unknown)" if no sidecar
    cascade_was_used: bool    # legacy pre-M23.1 field; absent ⇒ False
    title: str | None
    entry_name: str          # e.g. "2024_Foo-20260510T184612Z"
    entry_path: Path         # absolute path to the trashed dir
    orphan_repos: dict[str, str]  # {repo_name: upstream_url} hard-deleted on rm


@dataclass
class RestoreResult:
    """Outcome of ``restore_from_trash``'s in-transaction rebuild (M23.2).

    The folder move + symmetric reverse-edge rebuild (steps 1-2) have all
    committed by the time this is returned. The re-clone of any
    hard-deleted 1:1 repo (step 3) is the caller's POST-transaction
    responsibility — ``missing_repos`` hands it the ``{name: upstream}``
    map to act on, sourced from the trash sidecar's ``orphan_repos``.
    """

    paper_id: str
    title: str | None
    restored_path: Path
    # repos A still binds whose codes/<name>/ is gone (1:1 hard-deleted at
    # rm time): {repo_name: upstream_url}. Caller attempts re-clone post-tx.
    missing_repos: dict[str, str] = field(default_factory=dict)
    # opposite paper ids whose paired field got A written back in.
    reverse_edges_rebuilt: set[str] = field(default_factory=set)
    # opposite ids no longer in the library: their edge was silently dropped
    # from A's own field (decision #1 — no warning, self-heals on their own
    # later restore).
    dead_edges_dropped: set[str] = field(default_factory=set)
    # repos still present whose repo-meta.papers got A re-bound (1:N survivor
    # or a 1:1 repo that happened to survive).
    repos_rebound: set[str] = field(default_factory=set)
    # projects whose symlink + REFERENCES were re-created.
    projects_rebuilt: set[str] = field(default_factory=set)


def _utc_compact_now() -> str:
    """Compact ISO 8601 basic format used in entry names."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    """Local-timezone ISO 8601 with seconds precision (sidecar's deleted_at)."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _trash_dir(vault: Path) -> Path:
    return vault / TRASH_DIRNAME


def _sidecar_path(entry_path: Path) -> Path:
    """Sidecar lives next to the entry, with `.meta.yaml` suffix."""
    return entry_path.parent / f"{entry_path.name}.meta.yaml"


def _read_paper_title(paper_dir: Path) -> str | None:
    """Best-effort title extraction; returns None on any failure."""
    meta_file = paper_dir / "metadata.yaml"
    if not meta_file.is_file():
        return None
    try:
        data = _yaml.load(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    return str(title) if title else None


def _read_paper_meta(paper_dir: Path) -> dict[str, Any]:
    """Load a (round-trip) metadata.yaml dict from a paper / trash-entry dir.

    Used by restore to read A's sealed fields out of the trash entry before
    moving it back. Returns ``{}`` for a missing / empty / malformed file so
    restore degrades to a plain folder move (no edges to rebuild).
    """
    meta_file = paper_dir / "metadata.yaml"
    if not meta_file.is_file():
        return {}
    try:
        data = _yaml_rt.load(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def move_to_trash(
    vault: Path,
    paper_id: str,
    *,
    orphan_repos: dict[str, str] | None = None,
) -> Path:
    """Move ``papers/<paper_id>/`` into ``.trash/<paper_id>-<timestamp>/``.

    Writes the sidecar first; if the move fails, the sidecar is removed so
    no orphaned metadata is left behind. Returns the absolute path of the
    new trash entry.

    ``orphan_repos`` maps ``repo_name → upstream_url`` for repos that were
    hard-deleted during the cascade because this paper was their last binder
    (1:1 case). M23.2 restore reads this map to re-clone. Empty / ``None``
    when no repo was orphaned.

    The post-M23.1 sidecar no longer carries ``cascade_was_used`` — the
    ``--cascade`` flag is gone, so the field is meaningless. Readers default
    it to ``False`` when absent (see ``list_trash``).

    Raises:
        TrashError: source paper folder is missing or destination collides.
    """
    src = vault / "papers" / paper_id
    if not src.is_dir():
        raise TrashError(
            f"Cannot trash {paper_id!r}: papers/{paper_id}/ is missing."
        )

    trash_root = _trash_dir(vault)
    trash_root.mkdir(parents=True, exist_ok=True)

    entry_name = f"{paper_id}-{_utc_compact_now()}"
    entry_path = trash_root / entry_name
    if entry_path.exists():
        # Sub-second collision after rapid double-rm. Tack on a hex suffix.
        import uuid
        entry_name = f"{entry_name}-{uuid.uuid4().hex[:4]}"
        entry_path = trash_root / entry_name
    sidecar_path = _sidecar_path(entry_path)

    title = _read_paper_title(src)
    sidecar_payload: dict[str, Any] = {
        "paper_id": paper_id,
        "deleted_at": _now_iso(),
        "title": title,
        "orphan_repos": dict(orphan_repos or {}),
    }

    # Write sidecar first; if the move fails we'll drop it.
    import io
    buf = io.StringIO()
    _yaml_dump.dump(sidecar_payload, buf)
    sidecar_path.write_text(buf.getvalue(), encoding="utf-8")

    try:
        shutil.move(str(src), str(entry_path))
    except Exception:
        sidecar_path.unlink(missing_ok=True)
        raise

    return entry_path


def list_trash(vault: Path) -> list[TrashEntry]:
    """Enumerate trash entries, sorted by deletion time (newest first).

    Tolerates missing or malformed sidecars: the paper id and timestamp
    fall back to parsing the entry directory name.
    """
    trash_root = _trash_dir(vault)
    if not trash_root.is_dir():
        return []

    entries: list[TrashEntry] = []
    for child in trash_root.iterdir():
        if not child.is_dir():
            continue
        # Skip sidecar files (they end in .meta.yaml; iterdir yields them
        # only because we want to find dirs — sidecars are .yaml files).
        m = _ENTRY_NAME_RE.match(child.name)
        if not m:
            continue
        parsed_id, parsed_ts = m.group(1), m.group(2)

        sidecar = _sidecar_path(child)
        deleted_at = "(unknown)"
        cascade = False
        title: str | None = None
        orphan_repos: dict[str, str] = {}
        if sidecar.is_file():
            try:
                data = _yaml.load(sidecar.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
            deleted_at = str(data.get("deleted_at") or deleted_at)
            # Legacy key: pre-M23.1 entries set it, the new writer omits it.
            cascade = bool(data.get("cascade_was_used"))
            title = data.get("title")
            raw_orphans = data.get("orphan_repos")
            if isinstance(raw_orphans, dict):
                orphan_repos = {str(k): str(v) for k, v in raw_orphans.items()}

        entries.append(
            TrashEntry(
                paper_id=parsed_id,
                deleted_at=deleted_at,
                cascade_was_used=cascade,
                title=title,
                entry_name=child.name,
                entry_path=child,
                orphan_repos=orphan_repos,
            )
        )

    # Sort by entry name desc → newest deletion first (timestamp is part
    # of the name and lexicographically sortable).
    entries.sort(key=lambda e: e.entry_name, reverse=True)
    return entries


def resolve_trash_entry(vault: Path, paper_id_or_entry: str) -> TrashEntry:
    """Find the trash entry matching the given id or entry-name.

    Resolution order:
        1. Exact entry-name match (e.g. ``2024_Foo-20260510T184612Z``).
        2. Paper-id match — unique → return; ambiguous → raise.
    """
    entries = list_trash(vault)
    if not entries:
        raise TrashError("Trash is empty.")

    # Try exact entry-name match first.
    for e in entries:
        if e.entry_name == paper_id_or_entry:
            return e

    # Fall back to paper-id match.
    by_id = [e for e in entries if e.paper_id == paper_id_or_entry]
    if not by_id:
        raise TrashError(
            f"No trash entry matches {paper_id_or_entry!r}. "
            "Run `lit trash list` to see available entries."
        )
    if len(by_id) > 1:
        names = "\n  - ".join(e.entry_name for e in by_id)
        raise TrashError(
            f"Multiple trash entries for paper id {paper_id_or_entry!r}:\n"
            f"  - {names}\n"
            "Pass the full entry name to disambiguate."
        )
    return by_id[0]


def _opposite_ref_targets(restored_meta: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``[(opposite_id, paired_field), ...]`` to rebuild external→A edges.

    The exact inverse of ``commands/rm.py::_opposite_ref_targets``: for each
    opposite A names in its own (sealed) relation fields, the edge to write
    BACK is on the opposite's RELATION_PAIRS-paired field (A.extends:[B] ⇒ add
    A to B.extended-by; A.extended-by:[E] ⇒ add A to E.extends). After M23.0
    symmetry this enumerates every inbound edge A had, so restore is lossless.
    """
    out: list[tuple[str, str]] = []
    for field_name in ALL_REF_FIELDS:
        for opposite_id in restored_meta.get(field_name) or []:
            paired = RELATION_PAIRS[field_name]
            out.append((str(opposite_id), paired))
    return out


def _build_restore_ref_updates(
    vault: Path,
    restored_meta: dict[str, Any],
    paper_id: str,
    now: str,
) -> tuple[dict[str, str], set[str], set[str]]:
    """Build staged metadata writes that re-add ``paper_id`` into opposites.

    Inverse of rm's cascade: for each opposite A names, ADD A back to that
    opposite's paired field. Opposites no longer in the library have their
    edge SILENTLY dropped from A's own fields (decision #1) — this mutates
    ``restored_meta`` in place so the pruned copy is what gets re-staged.

    Returns ``(staged_opposite_writes, reverse_edges_rebuilt, dead_edges)``:
        * ``staged_opposite_writes`` maps opposite-id → new yaml text
        * ``reverse_edges_rebuilt`` is the set of opposite ids written back
        * ``dead_edges`` is the set of opposite ids dropped from A (gone)
    """
    # opposite_id → set of its paired fields that must regain paper_id.
    by_opposite: dict[str, set[str]] = {}
    for opposite_id, paired_field in _opposite_ref_targets(restored_meta):
        if opposite_id == paper_id:
            continue  # self-reference: no separate opposite to write
        by_opposite.setdefault(opposite_id, set()).add(paired_field)

    staged: dict[str, str] = {}
    rebuilt: set[str] = set()
    dead: set[str] = set()
    for opposite_id, fields in by_opposite.items():
        meta_path = vault / "papers" / opposite_id / "metadata.yaml"
        if not meta_path.is_file():
            dead.add(opposite_id)
            continue
        rt = _yaml_rt.load(meta_path.read_text(encoding="utf-8"))
        if rt is None:
            # Opposite present but empty/corrupt: treat as a dead edge so the
            # restored paper does not keep a ref into an unreadable target.
            dead.add(opposite_id)
            continue
        changed = False
        for field_name in fields:
            cur = list(rt.get(field_name) or [])
            if paper_id not in cur:
                cur.append(paper_id)
                rt[field_name] = cur
                changed = True
        if changed:
            rt["updated-at"] = now
            staged[opposite_id] = _dump_rt_to_string(rt)
            # Count only opposites we actually rewrote, so the "rebuilt
            # reverse edges in N papers" message is not inflated by edges
            # that were already symmetric.
            rebuilt.add(opposite_id)

    # Decision #1: prune dead edges out of A's own relation fields, silently.
    if dead:
        for field_name in ALL_REF_FIELDS:
            cur = restored_meta.get(field_name)
            if cur:
                restored_meta[field_name] = [v for v in cur if v not in dead]

    return staged, rebuilt, dead


def _build_restore_code_updates(
    vault: Path,
    restored_meta: dict[str, Any],
    paper_id: str,
    now: str,
    orphan_repos: dict[str, str],
) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Build staged repo-meta.yaml writes that re-bind A into surviving repos.

    For each repo in A's sealed ``code-clones``:
        * ``codes/<repo>/repo-meta.yaml`` present → re-add A to its ``papers``
          (idempotent), staged for the transaction.
        * absent and listed in the sidecar's ``orphan_repos`` → a 1:1
          hard-deleted repo; defer to the caller's POST-transaction re-clone.
          The binding on A's side is KEPT (decision #2/#3).
        * absent and NOT in ``orphan_repos`` → already-orphan ref; health-check
          backstops. Binding kept; nothing staged.

    Returns ``(staged_repo_writes, repos_rebound, missing_repos)``.
    """
    staged: dict[str, str] = {}
    rebound: set[str] = set()
    missing: dict[str, str] = {}
    for repo_name in restored_meta.get("code-clones") or []:
        repo_name = str(repo_name)
        repo_meta_path = (
            vault / CODES_DIRNAME / repo_name / REPO_META_FILENAME
        )
        if not repo_meta_path.is_file():
            if repo_name in orphan_repos:
                missing[repo_name] = orphan_repos[repo_name]
            continue
        rt = _yaml_rt.load(repo_meta_path.read_text(encoding="utf-8"))
        if rt is None:
            continue
        papers = list(rt.get("papers") or [])
        if paper_id not in papers:
            papers.append(paper_id)
            rt["papers"] = papers
            rt["updated-at"] = now
            staged[f"{CODES_DIRNAME}/{repo_name}/{REPO_META_FILENAME}"] = (
                _dump_rt_to_string(rt)
            )
        rebound.add(repo_name)
    return staged, rebound, missing


def _rebuild_project_links(
    vault: Path,
    restored_meta: dict[str, Any],
    paper_id: str,
    registry: dict[str, str],
) -> set[str]:
    """Re-create A's project symlinks + re-render REFERENCES.md (post-stage).

    Inverse of rm's ``_teardown_project_links``. For each project A names:
        * (re)create ``<project>/literature/A`` symlink into papers/A/;
        * (re)create ``<project>/code/<repo>`` symlink for each surviving repo;
        * re-render REFERENCES.md (A is back in papers/ so it reappears).
    A project not registered or whose dir is missing on disk is skipped
    (decision: P missing → skip) — A's own ``projects`` field is untouched.

    Returns the set of project names actually rebuilt.
    """
    code_clones = [str(r) for r in (restored_meta.get("code-clones") or [])]
    rebuilt: set[str] = set()
    for project in restored_meta.get("projects") or []:
        project = str(project)
        project_dir_str = registry.get(project)
        if not project_dir_str:
            continue
        project_dir = Path(project_dir_str).expanduser()
        if not project_dir.is_dir():
            continue

        make_relative_symlink(
            project_dir / LITERATURE_SUBDIR / paper_id,
            (vault / "papers" / paper_id).resolve(),
        )
        for repo_name in code_clones:
            repo_target = (
                vault / CODES_DIRNAME / repo_name / REPO_DIRNAME
            ).resolve()
            if not repo_target.exists():
                continue
            make_relative_symlink(
                project_dir / CODE_SUBDIR / repo_name, repo_target
            )
        try:
            write_references_md(vault, project, project_dir)
        except FileNotFoundError:
            continue
        rebuilt.add(project)
    return rebuilt


def restore_from_trash(
    vault: Path,
    entry: TrashEntry,
    *,
    registry: dict[str, str] | None = None,
) -> RestoreResult:
    """Restore a trashed paper and rebuild its symmetric relationship network.

    M23.2 best-effort symmetric rebuild (NOT a byte-level undo). Flow:

        0. Refuse if ``papers/<id>/`` already holds a LIVE paper — never
           clobber (decision #4).
        1. Move ``papers/<id>/`` back.
        2. (atomic, one ``staged_write``) From A's OWN sealed fields:
           rebuild every opposite paper's paired reverse field, re-bind
           surviving repos' ``repo-meta.papers``, prune dead edges out of A,
           de-annotate any ``[[A]] (deleted)`` tag back to ``[[A]]`` (M24,
           best-effort: missed stale tags are left to health-check), and
           re-render INDEX.json. The folder move + these writes are the
           transaction (steps 1-2); a failure rolls A back into trash.
        3. (caller, POST-transaction) re-clone any 1:1 hard-deleted repo —
           surfaced via ``RestoreResult.missing_repos``; never a precondition
           for restore success (decision #2).

    Project symlink + REFERENCES re-render run after the staged commit
    (filesystem-only, recoverable). TAXONOMY drift is left for health-check
    (decision #5; invariant #2 — restore never edits TAXONOMY).

    Raises:
        TrashError: ``papers/<id>/`` already exists (would clobber active state).
    """
    paper_id = entry.paper_id
    dst = vault / "papers" / paper_id
    if dst.exists():
        # Step 0: a live paper occupies the id slot — refuse, never overwrite.
        raise TrashError(
            f"Cannot restore {paper_id!r}: papers/{paper_id}/ "
            "already exists. Rename or remove the active paper first."
        )

    if registry is None:
        registry = {}

    # Read A's sealed metadata from the trash entry BEFORE moving, then prune
    # dead edges on this in-memory copy and re-stage it to the live path.
    sealed_meta = _read_paper_meta(entry.entry_path)
    now = _now_iso()

    # Build the in-transaction writes while A is still in trash.
    ref_writes, reverse_rebuilt, dead_edges = _build_restore_ref_updates(
        vault, sealed_meta, paper_id, now
    )
    code_writes, repos_rebound, missing_repos = _build_restore_code_updates(
        vault, sealed_meta, paper_id, now, entry.orphan_repos
    )

    # Bump A's own updated-at when restore pruned dead edges (best-effort
    # rebuild leaves a fingerprint); identity fields are otherwise untouched.
    pruned_meta_text: str | None = None
    if dead_edges:
        sealed_meta["updated-at"] = now
        pruned_meta_text = _dump_rt_to_string(sealed_meta)

    # Step 1: move the folder back (filesystem). If the staged write below
    # fails, roll the folder back into trash so the op is atomic end-to-end.
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(entry.entry_path), str(dst))

    # INDEX.json reflects the post-restore (pruned) paper list, rendered in
    # memory so it does not depend on the just-moved files being re-read.
    surviving = [p for p in list_papers(vault) if p.get("id") != paper_id]
    surviving.append(dict(sealed_meta))
    new_index = render_index(surviving, now)

    # De-annotate `[[A]] (deleted)` → `[[A]]` across all tracked notes now
    # that A is back on disk (M24). A's own restored notes are scanned too
    # (the folder is already at papers/A/). Stage only files whose text
    # changed; best-effort — a missed stale tag is left to health-check.
    note_updates: dict[str, str] = {}
    for md_path in enumerate_markdown_files(vault):
        text = md_path.read_text(encoding="utf-8")
        cleaned = deannotate_deleted_wikilinks(text, paper_id)
        if cleaned != text:
            note_updates[str(md_path.relative_to(vault))] = cleaned

    try:
        with staged_write(vault, op_id=f"restore-{paper_id}") as stage:
            if pruned_meta_text is not None:
                stage.write_text(
                    f"papers/{paper_id}/metadata.yaml", pruned_meta_text
                )
            for oid, content in ref_writes.items():
                stage.write_text(f"papers/{oid}/metadata.yaml", content)
            for relpath, content in code_writes.items():
                stage.write_text(relpath, content)
            for relpath, content in note_updates.items():
                stage.write_text(relpath, content)
            stage.write_text("INDEX.json", new_index)
    except Exception:
        # Roll the folder back into trash so a failed transaction leaves no
        # half-restored paper (steps 1-2 are one logical transaction).
        if dst.exists() and not entry.entry_path.exists():
            shutil.move(str(dst), str(entry.entry_path))
        raise

    # Drop sidecar now that the restore is committed; a stray sidecar is
    # harmless if this fails (health-check surfaces it).
    _sidecar_path(entry.entry_path).unlink(missing_ok=True)

    # Post-stage filesystem rebuild: project symlinks + REFERENCES.md.
    projects_rebuilt = _rebuild_project_links(
        vault, sealed_meta, paper_id, registry
    )

    return RestoreResult(
        paper_id=paper_id,
        title=entry.title,
        restored_path=dst,
        missing_repos=missing_repos,
        reverse_edges_rebuilt=reverse_rebuilt,
        dead_edges_dropped=dead_edges,
        repos_rebound=repos_rebound,
        projects_rebuilt=projects_rebuilt,
    )


def empty_trash(vault: Path) -> int:
    """Permanently delete every trash entry and sidecar. Returns count."""
    trash_root = _trash_dir(vault)
    if not trash_root.is_dir():
        return 0
    n = 0
    for child in trash_root.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
            n += 1
        elif child.is_dir():
            shutil.rmtree(child)
            n += 1
    return n


def enforce_cap(vault: Path, cap: int = TRASH_MAX_ENTRIES) -> list[str]:
    """Permanently evict the oldest entries beyond ``cap`` (ring eviction).

    Reuses ``list_trash`` for enumeration and newest-first ordering (which
    already tolerates missing sidecars and orders by the in-name UTC
    timestamp), so the tail ``entries[cap:]`` is the oldest surplus. Each
    surplus entry's folder and sidecar are deleted, mirroring ``empty_trash``.

    Returns the paper ids of the evicted entries (empty if nothing exceeded
    the cap).

    Best-effort: a single failed deletion is swallowed and that entry is
    skipped — this function never raises. Eviction is post-hoc housekeeping
    triggered after a paper is already safely in trash, so it must not abort
    the caller (``lit rm`` exits 0 regardless).
    """
    entries = list_trash(vault)
    if len(entries) <= cap:
        return []

    evicted: list[str] = []
    for entry in entries[cap:]:
        try:
            shutil.rmtree(entry.entry_path)
            _sidecar_path(entry.entry_path).unlink(missing_ok=True)
        except Exception:
            continue
        evicted.append(entry.paper_id)
    return evicted
