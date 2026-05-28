"""Health-check engine for ``lit health-check`` (M2.8).

Each ``check_*`` function is a self-contained probe that returns a list of
:class:`Issue` records. The CLI command in ``commands/health.py`` calls
:func:`run_all_checks` to gather every issue and renders the result.

Design notes:

* Checks are pure functions of ``(vault, papers)`` plus whatever extra disk
  reads they need (TAXONOMY.md, ``.litman-staging/``, ``.trash/``).
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
* invariant #12 bidirectional duality is enforced via
  :func:`check_code_clone_integrity`.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ruamel.yaml import YAML, YAMLError

from litman.core.atomic import cleanup_stale_staging
from litman.core.code import CODES_DIRNAME, REPO_META_FILENAME
from litman.core.id import is_valid_id
from litman.core.notes import enumerate_markdown_files, parse_wikilink_target
from litman.core.relations import ALL_REF_FIELDS, RELATION_PAIRS, REVERSE_REF_FIELDS
from litman.core.taxonomy import USER_DICTS, parse_taxonomy
from litman.core.trash import TRASH_DIRNAME, TRASH_MAX_ENTRIES
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

# Trash-health warning threshold. Half of the count-based eviction cap
# (TRASH_MAX_ENTRIES=100, see core/trash.py) — the "approaching limit"
# midpoint that forms a "50 heads-up → 100 auto-evict" ladder.
TRASH_SIZE_WARN = 50


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

# Fixed enums where ``None`` is a legitimate "not yet evaluated" state
# rather than a schema error. M29: `priority` and `type` are personal-
# evaluation fields the user fills after reading; `lit add` writes None,
# and `lit-reading` B10 self-check is the surfacing path. ``status``
# stays required (its "not yet evaluated" state is the explicit value
# "inbox", not None).
_OPTIONAL_FIXED_ENUMS: frozenset[str] = frozenset({"priority", "type"})

# Forward + reverse relation fields (ADR-012). Sourced from the shared
# RELATION_PAIRS map so dangling-ref scans cover reverse fields too.
_REF_FIELDS: tuple[str, ...] = ALL_REF_FIELDS
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")

# Inline deletion-status marker the CLI maintains on same-vault wikilinks
# (M24 / ADR-013). check_dangling_wikilinks peeks at the slice AFTER each
# match's ``]]`` for this suffix; _WIKILINK_RE itself is left untouched so
# rename + cross-vault logic stay agnostic to it.
_DELETED_SUFFIX = " (deleted)"


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
                if field in _OPTIONAL_FIXED_ENUMS:
                    # legitimate "not yet evaluated" — see M29
                    continue
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
    """Any relation field (forward or reverse) referencing missing ids.

    Covers ``related`` / ``contradicts`` / ``extends`` and their ADR-012
    reverse fields ``contradicted-by`` / ``extended-by`` (the full
    ``ALL_REF_FIELDS`` set), so a reverse edge left dangling by a deletion
    or rename is reported the same as a forward one.
    """
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
    """Every relation edge must have its ADR-012 paired reverse edge.

    Three pairs are validated via ``RELATION_PAIRS``:

    * ``related`` is self-paired — A.related:[B] implies B.related:[A].
    * ``extends`` ↔ ``extended-by`` — A.extends:[B] implies
      B.extended-by:[A].
    * ``contradicts`` ↔ ``contradicted-by`` — A.contradicts:[B] implies
      B.contradicted-by:[A].

    ADR-012 retired the old "extends is intentionally directional" stance:
    directional relations are now stored symmetrically and maintained by
    the CLI's auto double-write, so a one-directional residual means the
    pairing got out of sync (e.g. a hand-edit or an interrupted write) and
    is reported as an error. Only edges whose other endpoint exists are
    flagged; missing endpoints are reported by ``check_dangling_refs``.
    """
    by_id = {str(p.get("id")): p for p in papers if p.get("id")}
    out: list[Issue] = []
    # Deduplicate symmetric (``related``) findings: A↔B and B↔A are one
    # missing pairing. Directional pairs are never collapsed — the missing
    # side is unambiguous, so each is reported on its own field.
    seen_related_pairs: set[tuple[str, str]] = set()
    for pid, paper in by_id.items():
        for field, reverse in RELATION_PAIRS.items():
            for ref in paper.get(field) or []:
                ref = str(ref)
                other = by_id.get(ref)
                if other is None:
                    continue
                other_vals = {str(x) for x in (other.get(reverse) or [])}
                if pid in other_vals:
                    continue
                if field == reverse:
                    pair = tuple(sorted((pid, ref)))
                    if pair in seen_related_pairs:
                        continue
                    seen_related_pairs.add(pair)
                # Phrase the fix as a command the CLI accepts: reverse
                # fields are not user-settable, so re-run the *forward*
                # edge and let the auto double-write regenerate the
                # reverse. When the residual sits on a reverse field, the
                # forward edge lives on the other paper (``ref``).
                if field in REVERSE_REF_FIELDS:
                    fwd, owner, tgt = reverse, ref, pid
                else:
                    fwd, owner, tgt = field, pid, ref
                out.append(
                    Issue(
                        category="bidirectional_refs",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"{pid!r}.{field} contains {ref!r} but "
                            f"{ref!r}.{reverse} does not contain {pid!r}"
                        ),
                        hint=(
                            f"`lit modify {owner} --rm-tag {fwd}={tgt}` then "
                            f"`--add-tag {fwd}={tgt}` to re-sync the pairing"
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
    """``[[id]]`` / ``[[vault:id]]`` in tracked notes vs the deletion-tag truth.

    M8.4 extends the legacy check: the inner target of every ``[[...]]``
    is parsed via :func:`parse_wikilink_target`; if a vault prefix is
    present we cross-check against that vault's registry entry +
    INDEX.json instead of the current vault's paper set.

    Same-vault ``[[X]]`` (M24 deletion-tag drift, two warning cases):

    * **Missing-tag** (``warning``) — ``[[X]]`` whose ``papers/X/`` does
      NOT exist and which is NOT followed by ``" (deleted)"``. The link
      points at a paper not in the vault and carries no deletion marker, so
      an agent reading the note may hallucinate the paper is still present.
      The filesystem cannot tell "deleted" from "never existed" (ADR-013:
      ``papers/X/`` presence is the only truth), so both collapse to this
      one warning — there is no separate "genuinely never existed" error
      for the same-vault case, which avoids double-reporting one ``[[X]]``.
    * **Stale-tag** (``warning``) — ``[[X]] (deleted)`` whose ``papers/X/``
      DOES exist (the paper was restored but the tag was not cleared, e.g.
      an agent rewrote the note past the de-annotation). The marker now
      lies; health-check surfaces it for the next write to clean.
    * A correctly-tagged deleted link (``[[X]] (deleted)`` with ``papers/X/``
      absent) and a correctly-bare live link (``[[X]]`` with ``papers/X/``
      present) are both clean — no issue.

    Failure modes the check distinguishes for CROSS-vault links (errors —
    a fork prefix that cannot resolve is a genuine breakage, not drift):

    * **Unregistered vault** — the prefix points at a name not in
      ``~/.config/litman/vaults.yaml``. Surfaced with a "register or
      correct the name" hint.
    * **Vault registered but unreadable** — the registry entry exists
      but the path no longer holds an INDEX.json (directory moved /
      vault never pushed). Surfaced with a "check vault info" hint.
    * **Paper id not found in target vault** — the vault loaded
      cleanly but the id is absent from its INDEX.json.
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
        # Same-vault dedup must account for per-occurrence tag state: the
        # same ``[[X]]`` can appear both ``(deleted)``-tagged and bare in
        # one file, and only the bare one is a missing-tag drift. Keying on
        # ``raw`` alone would drop the bare occurrence after a tagged one was
        # seen (M24.2 regression). Cross-vault links carry no tag, so their
        # dedup collapses on ``raw`` regardless of the (always-False) flag.
        seen: set[tuple[str, bool]] = set()
        for m in _WIKILINK_RE.finditer(text):
            raw = m.group(1).strip()
            if not raw:
                continue
            # Peek at the char(s) after this link's ``]]`` for the deletion
            # marker (M24). Done on the slice, not the regex, so the form
            # rename + cross-vault logic see is unchanged.
            has_deleted_tag = text.startswith(_DELETED_SUFFIX, m.end())
            key = (raw, has_deleted_tag)
            if key in seen:
                continue
            seen.add(key)

            vault_prefix, paper_id = parse_wikilink_target(raw)

            if vault_prefix is None:
                # Same-vault form: reconcile against the filesystem truth
                # (paper present ⇔ in known_ids). Two M24 drift warnings.
                exists = paper_id in known_ids
                if not exists and not has_deleted_tag:
                    # Missing-tag: link to an absent paper with no marker.
                    out.append(
                        Issue(
                            category="dangling_wikilinks",
                            severity="warning",
                            paper_id=None,
                            message=(
                                f"{rel}: [[{raw}]] points at a paper not in the "
                                "vault but is not tagged (deleted)"
                            ),
                            hint=(
                                "mark it `[[id]] (deleted)`, correct the id, or "
                                "remove the bracket-link"
                            ),
                        )
                    )
                elif exists and has_deleted_tag:
                    # Stale-tag: marker says deleted but the paper is back.
                    out.append(
                        Issue(
                            category="dangling_wikilinks",
                            severity="warning",
                            paper_id=None,
                            message=(
                                f"{rel}: [[{raw}]] (deleted) but the paper exists "
                                "(stale deletion tag)"
                            ),
                            hint=(
                                "drop the ` (deleted)` marker — the paper was "
                                "restored"
                            ),
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


def check_project_config_consistency(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """TAXONOMY.md ``## projects`` ↔ lit-config.yaml ``projects:`` same set.

    M15 added ``lit project`` which writes both truth sources atomically.
    Old hand-edited vaults (or a half-applied legacy edit) may have a name
    in one but not the other. Either direction is a warning, not an error:
    nothing is actively broken, but `lit link` / register-first validation
    will behave inconsistently until reconciled. NOT auto-fixable — the
    user decides which side is authoritative.
    """
    taxonomy_file = vault / "TAXONOMY.md"
    if not taxonomy_file.is_file():
        return []
    try:
        registered = parse_taxonomy(taxonomy_file.read_text(encoding="utf-8"))
    except OSError:
        return []

    from litman.core.config import load_config

    try:
        config = load_config(vault)
    except Exception:
        # ConfigError surfaces via `lit config show`; don't double-report.
        return []

    taxonomy_names = set(registered.get("projects", []))
    config_names = set(config.projects)

    out: list[Issue] = []
    for name in sorted(taxonomy_names - config_names):
        out.append(
            Issue(
                category="project_config_consistency",
                severity="warning",
                paper_id=None,
                message=(
                    f"project {name!r} is in TAXONOMY.md but not in "
                    "lit-config.yaml's projects: map"
                ),
                hint=(
                    f"`lit project add {name} --path <path>` to register "
                    "its path, or remove it from TAXONOMY.md via "
                    f"`lit project rm {name}`"
                ),
            )
        )
    for name in sorted(config_names - taxonomy_names):
        out.append(
            Issue(
                category="project_config_consistency",
                severity="warning",
                paper_id=None,
                message=(
                    f"project {name!r} is in lit-config.yaml but not in "
                    "TAXONOMY.md's ## projects section"
                ),
                hint=(
                    f"`lit project add {name} --path <existing-path>` to "
                    "complete the registration (or fix the yaml by hand)"
                ),
            )
        )
    return out


def check_project_path_exists(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Every lit-config.yaml ``projects:`` path exists and is a directory.

    Common after cross-machine sync (rclone / USB) where the registry
    travels but the project working directories live at different absolute
    paths per machine. Warning (not error) + NOT auto-fixable: only the
    user knows the correct path on this machine.
    """
    from litman.core.config import load_config

    try:
        config = load_config(vault)
    except Exception:
        return []

    out: list[Issue] = []
    for name, path_str in sorted(config.projects.items()):
        project_dir = Path(path_str).expanduser()
        if not project_dir.exists():
            out.append(
                Issue(
                    category="project_path_exists",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"project {name!r} path does not exist: {path_str}"
                    ),
                    hint=(
                        f"`lit project set-path {name} <correct-path>` "
                        "(likely cross-machine path drift)"
                    ),
                )
            )
        elif not project_dir.is_dir():
            out.append(
                Issue(
                    category="project_path_exists",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"project {name!r} path is not a directory: "
                        f"{path_str}"
                    ),
                    hint=(
                        f"`lit project set-path {name} <correct-path>`"
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
    """Leftover op directories under ``.litman-staging/`` (M17 tri-state).

    Three outcomes per leftover op directory:

    * No ``COMMITTED`` sentinel → clean abort before the commit decision
      point. Normal, recoverable via roll-back → ``info`` severity, in
      ``AUTO_FIXABLE_CATEGORIES`` (``--fix`` rolls it back).
    * ``COMMITTED`` present, manifest fully replayable → torn commit that
      roll-forward can finish → ``info`` severity, auto-fixable. (In
      practice the vault-open hook has already healed this before
      health-check runs; this branch covers a direct call / a race.)
    * ``COMMITTED`` present but a manifested relpath is missing from both
      staging and target (or the manifest is unreadable) → unrecoverable
      data loss → ``error`` severity, **not** auto-fixable: a human must
      decide. Reuses :class:`RecoveryResult.message`.

    The probe is read-only — it inspects on-disk state and reuses the
    recovery classifier without mutating anything (``--fix`` performs the
    actual roll-back / roll-forward through ``cleanup_stale_staging``).
    """
    from litman.core.atomic import _SENTINEL_FILENAME, RecoveryResult

    out: list[Issue] = []
    staging = vault / ".litman-staging"
    if not staging.is_dir():
        return out
    for child in sorted(staging.iterdir()):
        if not child.is_dir():
            out.append(
                Issue(
                    category="stale_staging",
                    severity="info",
                    paper_id=None,
                    message=(
                        f".litman-staging/{child.name} — leftover entry"
                    ),
                    hint="run `lit health-check --fix` to clean",
                )
            )
            continue

        sentinel = child / _SENTINEL_FILENAME
        if not sentinel.is_file():
            out.append(
                Issue(
                    category="stale_staging",
                    severity="info",
                    paper_id=None,
                    message=(
                        f".litman-staging/{child.name}/ — leftover op dir "
                        "(clean abort, no commit record)"
                    ),
                    hint="run `lit health-check --fix` to clean",
                )
            )
            continue

        result = _classify_torn_op(child, vault, RecoveryResult)
        if result.kind == "unrecoverable":
            out.append(
                Issue(
                    category="stale_staging_unrecoverable",
                    severity="error",
                    paper_id=None,
                    message=result.message or (
                        f".litman-staging/{child.name}/ — unrecoverable "
                        "torn commit"
                    ),
                    hint=(
                        "data was lost mid-commit; inspect "
                        f".litman-staging/{child.name}/ manually — "
                        "NOT auto-fixed"
                    ),
                )
            )
        else:
            out.append(
                Issue(
                    category="stale_staging",
                    severity="info",
                    paper_id=None,
                    message=(
                        f".litman-staging/{child.name}/ — torn commit, "
                        "roll-forward pending"
                    ),
                    hint="run `lit health-check --fix` to roll forward",
                )
            )
    return out


def _classify_torn_op(
    op_dir: Path,
    vault: Path,
    result_cls: type,
) -> Any:
    """Read-only classification of a ``COMMITTED`` op dir.

    Returns a :class:`RecoveryResult`-shaped object with ``kind`` of
    ``rolled_forward`` (all files replayable) or ``unrecoverable`` (some
    manifested relpath lost from both sides, or manifest unreadable).
    Performs no filesystem mutation — health-check must be able to report
    without changing state.
    """
    # Shared, side-effect-free helpers from the recoverer so the
    # unrecoverable-detection predicate AND the Chinese data-loss-path
    # message strings have a single source of truth and cannot drift
    # between the read-only classifier and the mutating recoverer.
    from litman.core.atomic import (
        _manifest_unreadable_message,
        _read_manifest_relpaths,
        _unrecoverable_message,
    )

    op_id = op_dir.name
    relpaths = _read_manifest_relpaths(op_dir)
    if relpaths is None:
        return result_cls(
            op_id=op_id,
            kind="unrecoverable",
            n_files=0,
            message=_manifest_unreadable_message(op_id),
        )

    recoverable = 0
    unrecoverable: list[str] = []
    for relpath in relpaths:
        staging_path = op_dir / relpath
        target_path = vault / relpath
        if staging_path.exists() or target_path.exists():
            recoverable += 1
        else:
            unrecoverable.append(relpath)

    if unrecoverable:
        return result_cls(
            op_id=op_id,
            kind="unrecoverable",
            n_files=recoverable,
            # Read-only probe never promotes: pending voice + the real
            # count of files --fix WOULD roll forward (not 0). The
            # mutating recoverer passes mode="done" + its promoted count.
            message=_unrecoverable_message(
                op_id, unrecoverable, recoverable, mode="pending"
            ),
        )
    return result_cls(
        op_id=op_id,
        kind="rolled_forward",
        n_files=recoverable,
        message=None,
    )


def check_pdf_viewer(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Probe whether ``lit open`` (M9.1) has a usable PDF viewer.

    Three outcomes:

    * Configured viewer present on PATH, or no config + platform default
      available → no issue (silent ✓ in the health report).
    * Configured ``default_pdf_viewer`` set but not on PATH → warning;
      ``lit open`` would exit 2 with a "install / fix config" hint.
    * No configured viewer and the platform default (``xdg-open`` /
      ``wslview``) is missing on Linux → warning. (macOS always has
      ``open``; Windows always has ``os.startfile``.)

    Lazy imports avoid pulling pydantic + the viewer module at health-check
    module-load time — both are heavier than checks needs for the unrelated
    schema / id / ref probes.
    """
    from litman.core.config import load_config
    from litman.core.viewer import detect_platform_viewer, is_headless

    try:
        config = load_config(vault)
    except Exception:
        # ConfigError is surfaced by `lit config show` / any command that
        # loads config; suppress here to avoid double-reporting.
        return []

    out: list[Issue] = []
    configured = config.default_pdf_viewer
    if configured:
        if shutil.which(configured) is None:
            out.append(
                Issue(
                    category="pdf_viewer",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"configured default_pdf_viewer "
                        f"{configured!r} is not on PATH"
                    ),
                    hint=(
                        "install the program or update default_pdf_viewer "
                        "in lit-config.yaml"
                    ),
                )
            )
        return out

    viewer = detect_platform_viewer()
    if viewer is None:
        out.append(
            Issue(
                category="pdf_viewer",
                severity="warning",
                paper_id=None,
                message=(
                    "no platform PDF viewer detected — "
                    "`lit open` will exit 2 and print the path only"
                ),
                hint=(
                    "install xdg-utils (Linux) or wslview (WSL), "
                    "or set default_pdf_viewer in lit-config.yaml"
                ),
            )
        )
    elif viewer == "xdg-open" and is_headless():
        out.append(
            Issue(
                category="pdf_viewer",
                severity="warning",
                paper_id=None,
                message=(
                    "xdg-open is installed but no graphical display is "
                    "reachable in this session — `lit open` will exit 2 "
                    "and print the path only"
                ),
                hint=(
                    "set default_pdf_viewer in lit-config.yaml to a viewer "
                    "usable without an X display, or run lit open from a "
                    "session with DISPLAY/WAYLAND_DISPLAY set"
                ),
            )
        )
    return out


def check_trash_health(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Orphan sidecars + size warning for ``.trash/``."""
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
                    f".trash/ holds {n_entries} entries; oldest are "
                    f"auto-evicted at {TRASH_MAX_ENTRIES}"
                ),
                hint="run `lit trash empty` to clear it now",
            )
        )

    return out


def check_code_clone_integrity(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Enforce invariant #12 — paper ``code-clones`` ↔ ``codes/<name>/`` duality.

    Three failure modes, all under the single ``code_clone_integrity``
    category so the report groups them together:

    * **dangling ref** (``error``) — a paper's ``code-clones`` lists a
      ``<name>`` with no ``codes/<name>/`` directory + ``repo-meta.yaml``
      on disk. Active misdirection: the reader follows the link and finds
      nothing. One Issue per ``(paper_id, missing_repo_name)`` pair so
      each can be addressed individually.
    * **dangling clone** (``warning``) — ``codes/<name>/repo-meta.yaml``
      exists but no paper references ``<name>``. Disk hygiene only; the
      clone wastes space but does not mislead.
    * **missing repo-meta** (``error``) — ``codes/<name>/`` is a directory
      but has no ``repo-meta.yaml``. Whatever atomic clone+link op
      created the directory failed to land the metadata file, so the repo
      is unrecoverable and unreferenceable.

    Defensive: silently skips when ``codes/`` does not exist (vault may
    legitimately have no clones) and skips non-directory children of
    ``codes/`` (stray files / symlinks are not in scope here). Per
    invariant #12 "不自动修复" the category is NOT in
    :data:`AUTO_FIXABLE_CATEGORIES`.
    """
    codes_dir = vault / CODES_DIRNAME
    if not codes_dir.is_dir():
        return []

    disk_repos: set[str] = set()
    disk_dirs_no_meta: set[str] = set()
    for child in codes_dir.iterdir():
        if not child.is_dir():
            continue
        if (child / REPO_META_FILENAME).is_file():
            disk_repos.add(child.name)
        else:
            disk_dirs_no_meta.add(child.name)

    # Build referenced_repos plus a reverse map {repo_name: [paper_ids…]}
    # so dangling-ref Issues can pin to the offending paper(s).
    references: dict[str, list[str]] = {}
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        for name in p.get("code-clones") or []:
            if not isinstance(name, str) or not name:
                continue
            references.setdefault(name, []).append(str(pid))
    referenced_repos = set(references.keys())

    out: list[Issue] = []

    # Dangling refs: emit one Issue per (paper, missing repo) pair.
    for repo_name in sorted(referenced_repos - disk_repos):
        for pid in references[repo_name]:
            out.append(
                Issue(
                    category="code_clone_integrity",
                    severity="error",
                    paper_id=pid,
                    message=(
                        f"{pid!r}.code-clones references {repo_name!r} "
                        f"but no codes/{repo_name}/repo-meta.yaml exists"
                    ),
                    hint=(
                        f"`lit modify {pid} --rm-tag code-clones={repo_name}` "
                        "to drop the broken edge, or `lit code restore-all` "
                        "to re-clone if upstream metadata is recoverable"
                    ),
                )
            )

    # Dangling clones: vault-level finding, no owning paper.
    for repo_name in sorted(disk_repos - referenced_repos):
        out.append(
            Issue(
                category="code_clone_integrity",
                severity="warning",
                paper_id=None,
                message=(
                    f"codes/{repo_name}/ is a dangling clone — "
                    f"no paper references {repo_name!r} in code-clones"
                ),
                hint=(
                    f"`lit code rm {repo_name}` to delete, or "
                    f"`lit code link {repo_name} --paper <id>` to bind it"
                ),
            )
        )

    # Broken clone dirs: codes/<name>/ without repo-meta.yaml. Vault-level.
    for repo_name in sorted(disk_dirs_no_meta):
        out.append(
            Issue(
                category="code_clone_integrity",
                severity="error",
                paper_id=None,
                message=(
                    f"codes/{repo_name}/ exists but has no "
                    f"{REPO_META_FILENAME} (broken clone, likely from a "
                    "failed atomic clone+link op)"
                ),
                hint=(
                    "`lit code restore-all` to re-clone if the upstream "
                    "is known, or remove the directory manually"
                ),
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
    ("project_config_consistency", check_project_config_consistency),
    ("project_path_exists", check_project_path_exists),
    ("bidirectional_refs", check_bidirectional_refs),
    ("inbox_staleness", check_inbox_staleness),
    ("stale_staging", check_stale_staging),
    ("trash_health", check_trash_health),
    ("pdf_viewer", check_pdf_viewer),
    ("code_clone_integrity", check_code_clone_integrity),
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
