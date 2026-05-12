"""Health-check engine for ``lit health-check`` (M2.8).

Each ``check_*`` function is a self-contained probe that returns a list of
:class:`Issue` records. The CLI command in ``commands/health.py`` calls
:func:`run_all_checks` to gather every issue and renders the result.

Design notes:

* Checks are pure functions of ``(vault, papers)`` plus whatever extra disk
  reads they need (TAXONOMY.md, notes/, ``.litman-staging/``, ``.trash/``).
  They never mutate the vault — auto-fix lives in :func:`apply_autofix`.
* Severity is one of ``error`` / ``warning`` / ``info``. Errors indicate
  active inconsistency (dangling refs, schema violations, half-finished
  rename). Warnings indicate hygiene concerns (inbox staleness, large trash).
* A single category is set on each issue so the CLI can group + auto-fix.
* Auto-fixable categories are listed in :data:`AUTO_FIXABLE_CATEGORIES`.
  ``--fix`` only operates on those — bidirectional sync, taxonomy drift,
  and schema gaps need user judgment and stay report-only.

Cross-references:

* M2.0 added ``created-at`` / ``updated-at``. M2.8 enforces them.
* M2.3 added ``.litman-staging/``. M2.8 surfaces leftovers.
* M2.6 ``lit rename`` is two-phase (file content via staged_write, then
  ``os.rename`` of the dir). A failure between phases leaves dir name and
  metadata id out of sync — caught by :func:`check_id_consistency`.
* M2.7 ``lit rm`` is also two-phase; orphan paper dirs (file commit
  succeeded but ``move_to_trash`` failed) surface as INDEX-vs-disk
  mismatch detected by :func:`check_invalid_paper_dirs`.
* M2.7+ added ``.trash/`` with sidecar ``<entry>.meta.yaml``. M2.8 surfaces
  orphan sidecars (sidecar without entry dir) and trash bloat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ruamel.yaml import YAML, YAMLError

from litman.core.atomic import cleanup_stale_staging
from litman.core.id import is_valid_id
from litman.core.notes import enumerate_markdown_files, parse_wikilink_target
from litman.core.taxonomy import USER_DICTS, parse_taxonomy
from litman.core.trash import TRASH_DIRNAME
from litman.exceptions import VaultRegistryError

_yaml = YAML(typ="safe")

# ---------------------------------------------------------------------------
# Issue type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One finding from a health-check probe.

    Attributes:
        category: Stable string id, used for grouping + autofix routing.
        severity: ``error`` / ``warning`` / ``info``.
        paper_id: Owning paper id (None for vault-level issues like
            stale staging or trash bloat).
        message: Human-readable description.
        hint: Optional one-line remediation tip (CLI shows in dim).
    """

    category: str
    severity: str
    paper_id: str | None
    message: str
    hint: str | None = None


# Issue categories that ``--fix`` will auto-clean. See :func:`apply_autofix`.
AUTO_FIXABLE_CATEGORIES: frozenset[str] = frozenset(
    {"stale_staging", "orphan_trash_sidecar"}
)

# Threshold (days) for ``status: inbox`` papers to be flagged as stale.
INBOX_STALE_DAYS = 14

# Trash-health warning thresholds.
TRASH_SIZE_WARN = 50
TRASH_AGE_WARN_DAYS = 30


# Schema rules: required fields and their allowed values.
_REQUIRED_NONEMPTY_FIELDS: tuple[str, ...] = (
    "id",
    "created-at",
    "updated-at",
)

_FIXED_ENUM_VALUES: dict[str, frozenset[str]] = {
    "type": frozenset(
        {
            "research",
            "review",
            "position",
            "benchmark",
            "dataset",
            "tutorial",
            "thesis",
            "book-chapter",
        }
    ),
    "status": frozenset({"deep-read", "skim", "inbox", "dropped"}),
    "priority": frozenset({"A", "B", "C"}),
}

_REF_FIELDS: tuple[str, ...] = ("related", "contradicts", "extends")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")


# ---------------------------------------------------------------------------
# Per-paper / cross-paper checks
# ---------------------------------------------------------------------------


def check_schema(vault: Path, papers: list[dict[str, Any]]) -> list[Issue]:
    """Required fields present + non-empty; fixed enums in range."""
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id") or "(unknown)"
        for field in _REQUIRED_NONEMPTY_FIELDS:
            value = p.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=f"missing or empty required field {field!r}",
                        hint=(
                            f"set with `lit modify {pid} --set {field}=<value>`"
                            if field != "id"
                            else "metadata.yaml `id` is the load-bearing identity field"
                        ),
                    )
                )
        for field, allowed in _FIXED_ENUM_VALUES.items():
            value = p.get(field)
            if value is None:
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=f"required fixed-enum field {field!r} is missing",
                        hint=f"set with `lit modify {pid} --set {field}=<value>`",
                    )
                )
            elif value not in allowed:
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"field {field!r} has value {value!r}, "
                            f"not in {sorted(allowed)}"
                        ),
                        hint=f"correct via `lit modify {pid} --set {field}=<value>`",
                    )
                )
    return out


def check_id_consistency(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Directory name must equal ``metadata.yaml`` ``id`` field."""
    out: list[Issue] = []
    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return out
    for child in sorted(papers_dir.iterdir()):
        if not child.is_dir():
            continue
        meta_file = child / "metadata.yaml"
        if not meta_file.is_file():
            continue
        try:
            data = _yaml.load(meta_file.read_text(encoding="utf-8"))
        except (OSError, YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        meta_id = data.get("id")
        if meta_id and meta_id != child.name:
            out.append(
                Issue(
                    category="id_consistency",
                    severity="error",
                    paper_id=child.name,
                    message=(
                        f"directory name {child.name!r} != metadata id "
                        f"{meta_id!r} (likely a half-finished rename)"
                    ),
                    hint=(
                        f"reconcile manually: rename dir or "
                        f"`lit modify {child.name} --set id={child.name}`"
                    ),
                )
            )
    return out


def check_invalid_paper_dirs(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Folders under ``papers/`` that aren't valid paper ids or lack metadata."""
    out: list[Issue] = []
    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return out
    for child in sorted(papers_dir.iterdir()):
        if not child.is_dir():
            out.append(
                Issue(
                    category="invalid_paper_dirs",
                    severity="warning",
                    paper_id=None,
                    message=f"non-directory entry under papers/: {child.name}",
                    hint="remove manually if unintended",
                )
            )
            continue
        if not is_valid_id(child.name):
            out.append(
                Issue(
                    category="invalid_paper_dirs",
                    severity="error",
                    paper_id=child.name,
                    message=(
                        f"papers/{child.name}/ name is not a valid paper id"
                    ),
                    hint="rename the directory or move it out of papers/",
                )
            )
            continue
        if not (child / "metadata.yaml").is_file():
            out.append(
                Issue(
                    category="invalid_paper_dirs",
                    severity="error",
                    paper_id=child.name,
                    message=(
                        f"papers/{child.name}/ has no metadata.yaml "
                        "(orphan from a failed rm or interrupted add)"
                    ),
                    hint=(
                        "inspect the folder; if unwanted, "
                        f"`lit rm {child.name} --purge` or move it manually"
                    ),
                )
            )
    return out


def check_dangling_refs(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``related`` / ``contradicts`` / ``extends`` referencing missing ids."""
    known_ids = {str(p.get("id")) for p in papers if p.get("id")}
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        for field in _REF_FIELDS:
            for ref in p.get(field) or []:
                if str(ref) not in known_ids:
                    out.append(
                        Issue(
                            category="dangling_refs",
                            severity="error",
                            paper_id=str(pid),
                            message=(
                                f"{field!r} references missing paper "
                                f"{ref!r}"
                            ),
                            hint=(
                                f"`lit modify {pid} --rm-tag {field}={ref}` "
                                "to drop the broken edge"
                            ),
                        )
                    )
    return out


def check_bidirectional_refs(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``related`` should be symmetric (A→B implies B→A).

    Only ``related`` is checked: ``extends`` is intentionally directional
    (A extends B != B extends A) and ``contradicts`` may be one-sided when
    only one paper raises the disagreement. Only flags edges where both
    endpoints exist (dangling refs are reported separately).
    """
    by_id = {str(p.get("id")): p for p in papers if p.get("id")}
    out: list[Issue] = []
    seen_pairs: set[tuple[str, str]] = set()
    for pid, paper in by_id.items():
        for ref in paper.get("related") or []:
            ref = str(ref)
            other = by_id.get(ref)
            if other is None:
                continue
            other_related = {str(x) for x in (other.get("related") or [])}
            if pid in other_related:
                continue
            pair = tuple(sorted((pid, ref)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            out.append(
                Issue(
                    category="bidirectional_refs",
                    severity="warning",
                    paper_id=pid,
                    message=(
                        f"{pid!r}.related contains {ref!r} but "
                        f"{ref!r}.related does not contain {pid!r}"
                    ),
                    hint=(
                        f"`lit modify {ref} --add-tag related={pid}` "
                        "to make symmetric"
                    ),
                )
            )
    return out


def _load_vault_paper_ids(vault_path: Path) -> set[str] | None:
    """Read ``INDEX.json`` from any vault and return its set of paper ids.

    Used by ``check_dangling_wikilinks`` to resolve cross-vault wikilinks
    (M8.4). Returns ``None`` when the vault path no longer exists, is not
    a directory, has no INDEX.json, or whose INDEX.json is unparseable —
    every one of those cases reads as "we can't verify what's in this
    fork" and the caller surfaces it as a dangling-link diagnostic.
    """
    import json

    if not vault_path.is_dir():
        return None
    index_path = vault_path / "INDEX.json"
    if not index_path.is_file():
        return None
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    papers = payload.get("papers") or []
    return {
        str(p.get("id"))
        for p in papers
        if isinstance(p, dict) and p.get("id")
    }


def check_dangling_wikilinks(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``[[id]]`` / ``[[vault:id]]`` in tracked notes pointing at missing papers.

    M8.4 extends the legacy check: the inner target of every ``[[...]]``
    is parsed via :func:`parse_wikilink_target`; if a vault prefix is
    present we cross-check against that vault's registry entry +
    INDEX.json instead of the current vault's paper set.

    Failure modes the check distinguishes for cross-vault links:

    * **Unregistered vault** — the prefix points at a name not in
      ``~/.config/litman/vaults.yaml``. Surfaced with a "register or
      correct the name" hint.
    * **Vault registered but unreadable** — the registry entry exists
      but the path no longer holds an INDEX.json (directory moved /
      vault never pushed). Surfaced with a "check vault info" hint.
    * **Paper id not found in target vault** — the vault loaded
      cleanly but the id is absent from its INDEX.json. Same shape as
      the legacy same-vault dangling case, just scoped to the fork.
    * **Empty vault prefix / empty paper id** — malformed link
      (``[[:id]]`` or ``[[vault:]]``); reported with a clear message.

    Local import of vault_registry helpers avoids a cycle: vault_registry
    depends only on stdlib + pydantic, but pulling it into checks at
    module load would tie health-check init time to registry-load time
    even for vaults that contain zero cross-vault wikilinks.
    """
    from litman.core.vault_registry import find_by_name, load_registry

    known_ids = {str(p.get("id")) for p in papers if p.get("id")}
    out: list[Issue] = []

    # Lazy-load the registry once: we don't want to pay the file IO when
    # no cross-vault link exists in the entire vault. None encodes both
    # "not loaded yet" and "loaded but registry corrupt"; the boolean
    # below disambiguates so we don't retry the load on every link.
    registry = None
    registry_loaded = False
    # Cache target vault → paper-id set so repeated cross-vault refs to
    # the same vault don't re-parse its INDEX.json.
    target_ids_cache: dict[str, set[str] | None] = {}

    for md_path in enumerate_markdown_files(vault):
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = md_path.relative_to(vault)
        seen: set[str] = set()
        for m in _WIKILINK_RE.finditer(text):
            raw = m.group(1).strip()
            if not raw or raw in seen:
                continue
            seen.add(raw)

            vault_prefix, paper_id = parse_wikilink_target(raw)

            if vault_prefix is None:
                # Legacy same-vault form: paper id must exist locally.
                if paper_id not in known_ids:
                    out.append(
                        Issue(
                            category="dangling_wikilinks",
                            severity="error",
                            paper_id=None,
                            message=f"{rel}: contains [[{raw}]] but no such paper",
                            hint="edit the file to remove the bracket-link or correct the id",
                        )
                    )
                continue

            # Cross-vault form (M8.4).
            if not vault_prefix or not paper_id:
                out.append(
                    Issue(
                        category="dangling_wikilinks",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"{rel}: malformed cross-vault wikilink [[{raw}]] — "
                            "both vault name and paper id must be non-empty"
                        ),
                        hint="rewrite as [[vault-name:paper-id]]",
                    )
                )
                continue

            if not registry_loaded:
                try:
                    registry = load_registry()
                except VaultRegistryError:
                    registry = None
                registry_loaded = True

            entry = (
                find_by_name(registry, vault_prefix)
                if registry is not None else None
            )
            if entry is None:
                out.append(
                    Issue(
                        category="dangling_wikilinks",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"{rel}: [[{raw}]] references unregistered "
                            f"vault {vault_prefix!r}"
                        ),
                        hint=(
                            "register the vault with "
                            f"`lit vault add {vault_prefix} <path>` "
                            "or correct the vault prefix"
                        ),
                    )
                )
                continue

            if vault_prefix not in target_ids_cache:
                target_ids_cache[vault_prefix] = _load_vault_paper_ids(
                    Path(entry.path)
                )
            target_ids = target_ids_cache[vault_prefix]
            if target_ids is None:
                out.append(
                    Issue(
                        category="dangling_wikilinks",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"{rel}: [[{raw}]] target vault "
                            f"{vault_prefix!r} is unreadable "
                            f"(no INDEX.json at {entry.path})"
                        ),
                        hint=(
                            f"`lit vault info {vault_prefix}` to inspect; "
                            "restore the vault directory or remove this link"
                        ),
                    )
                )
                continue
            if paper_id not in target_ids:
                out.append(
                    Issue(
                        category="dangling_wikilinks",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"{rel}: [[{raw}]] but vault "
                            f"{vault_prefix!r} has no paper id {paper_id!r}"
                        ),
                        hint=(
                            f"`lit list --vault {vault_prefix}` to find the "
                            "right id, or correct the link"
                        ),
                    )
                )
    return out


def check_taxonomy_drift(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``projects`` / ``topics`` / ``methods`` / ``data`` values absent from TAXONOMY.md."""
    taxonomy_file = vault / "TAXONOMY.md"
    if not taxonomy_file.is_file():
        return [
            Issue(
                category="taxonomy_drift",
                severity="error",
                paper_id=None,
                message="TAXONOMY.md is missing from the vault root",
                hint="run `lit init` in a fresh dir and copy the seed",
            )
        ]
    try:
        registered = parse_taxonomy(taxonomy_file.read_text(encoding="utf-8"))
    except OSError:
        return []
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id") or "(unknown)"
        for dict_name in USER_DICTS:
            field_values = p.get(dict_name) or []
            for value in field_values:
                if str(value) not in registered.get(dict_name, []):
                    out.append(
                        Issue(
                            category="taxonomy_drift",
                            severity="warning",
                            paper_id=str(pid),
                            message=(
                                f"{dict_name} value {value!r} not in TAXONOMY.md "
                                f"(unregistered)"
                            ),
                            hint=(
                                f"register via `lit taxonomy add {dict_name} {value}` "
                                f"or rename it"
                            ),
                        )
                    )
    return out


def check_inbox_staleness(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``status: inbox`` papers older than :data:`INBOX_STALE_DAYS` days."""
    out: list[Issue] = []
    now = datetime.now(timezone.utc)
    threshold = timedelta(days=INBOX_STALE_DAYS)
    for p in papers:
        if p.get("status") != "inbox":
            continue
        created = p.get("created-at")
        if not created:
            continue
        try:
            created_dt = datetime.fromisoformat(str(created))
        except ValueError:
            continue
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        age = now - created_dt
        if age > threshold:
            pid = p.get("id") or "(unknown)"
            out.append(
                Issue(
                    category="inbox_staleness",
                    severity="warning",
                    paper_id=str(pid),
                    message=(
                        f"status=inbox for {age.days} days "
                        f"(>{INBOX_STALE_DAYS} day threshold)"
                    ),
                    hint=(
                        f"promote with `lit modify {pid} --set status=skim` "
                        f"or drop with `lit modify {pid} --set status=dropped`"
                    ),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Vault-level checks
# ---------------------------------------------------------------------------


def check_stale_staging(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Leftover op directories under ``.litman-staging/`` from crashed runs."""
    out: list[Issue] = []
    staging = vault / ".litman-staging"
    if not staging.is_dir():
        return out
    for child in sorted(staging.iterdir()):
        out.append(
            Issue(
                category="stale_staging",
                severity="warning",
                paper_id=None,
                message=(
                    f".litman-staging/{child.name}/ — leftover op dir"
                    if child.is_dir()
                    else f".litman-staging/{child.name} — leftover entry"
                ),
                hint="run `lit health-check --fix` to clean",
            )
        )
    return out


def check_trash_health(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Orphan sidecars + size/age warnings for ``.trash/``."""
    trash_root = vault / TRASH_DIRNAME
    if not trash_root.is_dir():
        return []

    out: list[Issue] = []
    entry_dirs: dict[str, Path] = {}
    sidecars: dict[str, Path] = {}
    for child in trash_root.iterdir():
        if child.is_dir():
            entry_dirs[child.name] = child
        elif child.is_file() and child.name.endswith(".meta.yaml"):
            entry_name = child.name[: -len(".meta.yaml")]
            sidecars[entry_name] = child

    for entry_name, sidecar in sidecars.items():
        if entry_name not in entry_dirs:
            out.append(
                Issue(
                    category="orphan_trash_sidecar",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f".trash/{sidecar.name} has no corresponding entry dir"
                    ),
                    hint="run `lit health-check --fix` to remove",
                )
            )

    n_entries = len(entry_dirs)
    if n_entries > TRASH_SIZE_WARN:
        out.append(
            Issue(
                category="trash_size",
                severity="info",
                paper_id=None,
                message=(
                    f".trash/ holds {n_entries} entries "
                    f"(>{TRASH_SIZE_WARN} threshold)"
                ),
                hint="run `lit trash empty` to permanently delete",
            )
        )

    age_threshold = datetime.now(timezone.utc) - timedelta(
        days=TRASH_AGE_WARN_DAYS
    )
    n_old = 0
    for entry_name in entry_dirs:
        m = re.match(r"^(.+?)-(\d{8}T\d{6}Z)$", entry_name)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(2), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if ts < age_threshold:
            n_old += 1
    if n_old > 0:
        out.append(
            Issue(
                category="trash_age",
                severity="info",
                paper_id=None,
                message=(
                    f"{n_old} trash entr{'y' if n_old == 1 else 'ies'} older "
                    f"than {TRASH_AGE_WARN_DAYS} days"
                ),
                hint="run `lit trash empty` if you no longer need them",
            )
        )

    return out


# ---------------------------------------------------------------------------
# Orchestration + autofix
# ---------------------------------------------------------------------------


# Stable ordering for report output. Errors first, then warnings, then info.
_CHECK_REGISTRY: tuple[tuple[str, Callable[[Path, list[dict[str, Any]]], list[Issue]]], ...] = (
    ("schema", check_schema),
    ("id_consistency", check_id_consistency),
    ("invalid_paper_dirs", check_invalid_paper_dirs),
    ("dangling_refs", check_dangling_refs),
    ("dangling_wikilinks", check_dangling_wikilinks),
    ("taxonomy_drift", check_taxonomy_drift),
    ("bidirectional_refs", check_bidirectional_refs),
    ("inbox_staleness", check_inbox_staleness),
    ("stale_staging", check_stale_staging),
    ("trash_health", check_trash_health),
)


def run_all_checks(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Run every check in registry order; return the flat list of issues."""
    out: list[Issue] = []
    for _, fn in _CHECK_REGISTRY:
        out.extend(fn(vault, papers))
    return out


def group_by_category(issues: Iterable[Issue]) -> dict[str, list[Issue]]:
    """Group issues by ``category`` while preserving registry-defined order."""
    grouped: dict[str, list[Issue]] = {}
    for issue in issues:
        grouped.setdefault(issue.category, []).append(issue)
    return grouped


def apply_autofix(vault: Path, issues: list[Issue]) -> dict[str, int]:
    """Auto-clean fixable categories. Returns ``{category: n_fixed}``.

    Currently fixes:

    * ``stale_staging``  — drops every ``.litman-staging/<op-id>/`` entry.
    * ``orphan_trash_sidecar`` — deletes ``<entry>.meta.yaml`` files in
      ``.trash/`` whose entry dir is missing.
    """
    counts: dict[str, int] = {}

    fixable_present = {i.category for i in issues if i.category in AUTO_FIXABLE_CATEGORIES}

    if "stale_staging" in fixable_present:
        counts["stale_staging"] = cleanup_stale_staging(vault)

    if "orphan_trash_sidecar" in fixable_present:
        n = 0
        trash_root = vault / TRASH_DIRNAME
        if trash_root.is_dir():
            entry_dirs = {c.name for c in trash_root.iterdir() if c.is_dir()}
            for child in trash_root.iterdir():
                if (
                    child.is_file()
                    and child.name.endswith(".meta.yaml")
                ):
                    entry_name = child.name[: -len(".meta.yaml")]
                    if entry_name not in entry_dirs:
                        child.unlink()
                        n += 1
        counts["orphan_trash_sidecar"] = n

    return counts
