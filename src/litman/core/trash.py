"""Recoverable-delete bin under ``<vault>/.trash/``.

`lit rm` defaults to moving the paper folder here instead of permanently
deleting it. `lit trash {list, restore, empty}` operates on this directory.

Layout::

    <vault>/.trash/
    ├── 2024_Foo-20260510T184612Z/                  # the moved paper folder
    │   ├── metadata.yaml
    │   ├── paper.pdf
    │   └── notes.md
    └── 2024_Foo-20260510T184612Z.meta.yaml         # sidecar (deleted_at, cascade, title)

The sidecar carries human-readable trash metadata so ``lit trash list`` can
render a useful summary without re-reading the moved paper's metadata.yaml.
A missing sidecar is tolerated: enumeration falls back to parsing the entry
name for the paper id and timestamp.

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

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

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

# Entry name format: <paper_id>-<UTC-timestamp>
# Timestamp is YYYYMMDDTHHMMSSZ (compact ISO 8601 basic).
_ENTRY_NAME_RE = re.compile(r"^(.+?)-(\d{8}T\d{6}Z)$")


@dataclass
class TrashEntry:
    """One row of `lit trash list` output."""

    paper_id: str
    deleted_at: str          # ISO 8601 with timezone, or "(unknown)" if no sidecar
    cascade_was_used: bool
    title: str | None
    entry_name: str          # e.g. "2024_Foo-20260510T184612Z"
    entry_path: Path         # absolute path to the trashed dir


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


def move_to_trash(
    vault: Path, paper_id: str, *, cascade_was_used: bool
) -> Path:
    """Move ``papers/<paper_id>/`` into ``.trash/<paper_id>-<timestamp>/``.

    Writes the sidecar first; if the move fails, the sidecar is removed so
    no orphaned metadata is left behind. Returns the absolute path of the
    new trash entry.

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
        "cascade_was_used": bool(cascade_was_used),
        "title": title,
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
        if sidecar.is_file():
            try:
                data = _yaml.load(sidecar.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
            deleted_at = str(data.get("deleted_at") or deleted_at)
            cascade = bool(data.get("cascade_was_used"))
            title = data.get("title")

        entries.append(
            TrashEntry(
                paper_id=parsed_id,
                deleted_at=deleted_at,
                cascade_was_used=cascade,
                title=title,
                entry_name=child.name,
                entry_path=child,
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


def restore_from_trash(vault: Path, entry: TrashEntry) -> Path:
    """Move a trash entry back to ``papers/<id>/`` and drop the sidecar.

    Raises:
        TrashError: ``papers/<id>/`` already exists (would clobber active state).
    """
    dst = vault / "papers" / entry.paper_id
    if dst.exists():
        raise TrashError(
            f"Cannot restore {entry.paper_id!r}: papers/{entry.paper_id}/ "
            "already exists. Rename or remove the active paper first."
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(entry.entry_path), str(dst))

    # Drop sidecar; if this fails the entry has been restored successfully
    # and a stray sidecar in .trash/ is harmless (M2.8 will surface it).
    _sidecar_path(entry.entry_path).unlink(missing_ok=True)
    return dst


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
