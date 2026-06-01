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
  metadata id out of sync — caught by :func:`check_paper_dir_validity`.
* M2.7 ``lit rm`` is also two-phase; orphan paper dirs (file commit
  succeeded but ``move_to_trash`` failed) surface via
  :func:`check_paper_dir_validity` (structural) and
  :func:`check_index_vs_disk` (the INDEX↔papers cheap diff).
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
from typing import Any, Callable, Iterable, Literal

from ruamel.yaml import YAML, YAMLError

from litman.core.atomic import cleanup_stale_staging
from litman.core.code import CODES_DIRNAME, REPO_DIRNAME, REPO_META_FILENAME
from litman.core.dates import is_iso_date, is_iso_datetime
from litman.core.id import is_valid_id
from litman.core.notes import enumerate_markdown_files, parse_wikilink_target
from litman.core.relations import ALL_REF_FIELDS, RELATION_PAIRS, REVERSE_REF_FIELDS
from litman.core.taxonomy import USER_DICTS, parse_taxonomy
from litman.core.trash import TRASH_DIRNAME, TRASH_MAX_ENTRIES
from litman.exceptions import ConfigError, VaultRegistryError

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


# Tag value vocabularies. Kept as module constants so tests can assert a
# CheckSpec's tags are drawn from these exact sets without re-deriving them.
TIERS: frozenset[str] = frozenset({"cheap", "full"})
KLASSES: frozenset[str] = frozenset({"A", "B-ext", "B-auth", "validity"})
CORRECTIONS: frozenset[str] = frozenset(
    {"regen", "resolve", "annotate", "report"}
)

_CheckFn = Callable[[Path, list[dict[str, Any]]], list["Issue"]]


@dataclass(frozen=True)
class CheckSpec:
    """A registered health check plus its drift-ledger metadata (M30).

    Per invariant #14, every registered check declares which drift class it
    covers, which tier it runs in, and how it is corrected. Bundling the
    metadata onto the check function (rather than a parallel frozenset) makes
    the ledger a single source of truth and turns "is this check tagged"
    into a mechanical, testable property.

    Attributes:
        category: Stable category id (matches the ``Issue.category`` strings
            the function emits, used for grouping + report headers).
        fn: The pure ``check_*(vault, papers) -> list[Issue]`` probe.
        tier: ``"cheap"`` (eligible for the Tier-1 per-command hook —
            INDEX/registry/listing/bounded-stat only, invariant #15) or
            ``"full"`` (Tier-2 ``health-check`` only).
        klass: drift class — ``"A"`` (derived↔truth), ``"B-ext"``
            (truth↔external dir), ``"B-auth"`` (truth↔authored truth), or
            ``"validity"`` (truth-internal integrity, not a drift).
        correction: ``"regen"`` / ``"resolve"`` / ``"annotate"`` /
            ``"report"`` — the correction mode this check's findings map to.
    """

    category: str
    fn: _CheckFn
    tier: Literal["cheap", "full"]
    klass: Literal["A", "B-ext", "B-auth", "validity"]
    correction: Literal["regen", "resolve", "annotate", "report"]


# Issue categories that ``--fix`` will auto-clean. See :func:`apply_autofix`.
# Phase 1 keeps this exactly as the historical set (stale_staging +
# orphan_trash_sidecar). Broadening ``--fix`` to all klass-A regen is Phase 2;
# until then this stays a literal so external ``--fix`` behavior is unchanged.
AUTO_FIXABLE_CATEGORIES: frozenset[str] = frozenset(
    {"stale_staging", "orphan_trash_sidecar"}
)

# Threshold (days) for ``status: inbox`` papers to be flagged as stale.
INBOX_STALE_DAYS = 14

# Threshold (days) since the last successful ``lit health-check`` before the
# Tier-2 staleness nudge fires (M30 Phase 5). Deliberately a SEPARATE constant
# from :data:`INBOX_STALE_DAYS` — they answer different questions ("you haven't
# *looked* in 2 weeks" vs "this paper has sat in the inbox 2 weeks") and may
# diverge later. Non-configurable (a configurable interval is a stealth disable).
HEALTH_CHECK_STALE_DAYS = 14

# Threshold (days) since the last successful ``lit sync push`` before the
# post-dispatch nudge reminds the user to back the vault up to its configured
# remote (M30 Phase 5, sync arm). Only fires when a remote is configured
# (``lit-config.yaml`` ``sync`` set). Shorter than HEALTH_CHECK_STALE_DAYS: an
# un-pushed vault is unbacked-up data at risk, a more time-sensitive concern
# than un-inspected drift. Non-configurable (a configurable interval is a
# stealth disable).
SYNC_STALE_DAYS = 7

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


def fixed_enum_values(field: str) -> frozenset[str] | None:
    """Allowed values for a fixed-enum metadata field, or ``None`` if ``field``
    is not a fixed enum.

    Read-only accessor so write commands (``lit modify --set``) can enforce the
    same enum the read-side ``check_schema`` enforces, without reaching into
    this module's private table. ``None`` is *additionally* a legal value for
    the fields reported by :func:`fixed_enum_allows_none`.
    """
    return _FIXED_ENUM_VALUES.get(field)


def fixed_enum_allows_none(field: str) -> bool:
    """Whether ``field`` is a fixed enum for which ``None`` ("not yet
    evaluated") is legal (``priority`` / ``type``; M29). ``status`` is not —
    its unevaluated state is the explicit value ``inbox``."""
    return field in _OPTIONAL_FIXED_ENUMS

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
        # Timestamp format (invariant #11 + review F9). The two technical
        # audit stamps must parse as ISO 8601 datetimes; the two semantic
        # date fields (None = not yet) must be strict YYYY-MM-DD. A malformed
        # value used to be invisible: check_schema never validated format and
        # check_inbox_staleness silently skipped an unparseable created-at, so
        # garbage could sit forever unreported.
        for field in ("created-at", "updated-at"):
            value = p.get(field)
            if value is None:
                continue  # missing already flagged by the required-field loop
            if not is_iso_datetime(value):
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"field {field!r} value {value!r} is not an "
                            "ISO 8601 datetime"
                        ),
                        hint="machine-maintained audit field; restore a valid timestamp",
                    )
                )
        for field in ("read-date", "last-revisited"):
            value = p.get(field)
            if value is None:
                continue  # not yet read / revisited — legitimate
            if not is_iso_date(value):
                out.append(
                    Issue(
                        category="schema",
                        severity="error",
                        paper_id=pid,
                        message=(
                            f"field {field!r} value {value!r} is not a "
                            "YYYY-MM-DD date"
                        ),
                        hint=f"correct via `lit modify {pid} --set {field}=<YYYY-MM-DD>`",
                    )
                )
    return out


def check_paper_dir_validity(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Per-``papers/<dir>`` structural integrity (M30; klass=validity, report).

    Truth side is the ``papers/`` directory enumeration — NOT ``list_papers``,
    which silently drops a paper with unparseable YAML (invariant: detection
    truth-side = directory enumeration). Merges and replaces the old
    ``check_invalid_paper_dirs`` + ``check_id_consistency`` so a single pass
    over ``papers/`` reports every structural fault and the
    ``except: continue`` silent skip of corrupt metadata is gone (invariant
    #14, no-silent-skip).

    Per directory child, emits ``error`` for:

    * a non-directory entry under ``papers/`` (``warning``);
    * a directory name that is not a valid paper id;
    * ``metadata.yaml`` missing (orphan from a failed rm / interrupted add);
    * ``metadata.yaml`` present but **unparseable or empty** — the paper is
      invisible to ``list_papers``, ``INDEX``, and every metadata-keyed check,
      so it is reported here instead of vanishing silently;
    * ``metadata.yaml`` ``id`` field ≠ directory name (half-finished rename);
    * ``paper.pdf`` missing (``lit open`` depends on it; irreplaceable).

    ``notes.md`` / ``discussion.md`` absence is NOT checked — empty/deleted is
    a legitimate state (nothing in the system depends on them existing).
    """
    out: list[Issue] = []
    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return out
    for child in sorted(papers_dir.iterdir()):
        if not child.is_dir():
            out.append(
                Issue(
                    category="paper_dir_validity",
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
                    category="paper_dir_validity",
                    severity="error",
                    paper_id=child.name,
                    message=(
                        f"papers/{child.name}/ name is not a valid paper id"
                    ),
                    hint="rename the directory or move it out of papers/",
                )
            )
            continue

        meta_file = child / "metadata.yaml"
        if not meta_file.is_file():
            out.append(
                Issue(
                    category="paper_dir_validity",
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
        else:
            # No silent-skip: a parse failure / empty metadata is itself a
            # finding. A corrupt metadata.yaml drops the paper out of
            # list_papers / INDEX / every metadata-keyed check, so the
            # structural check (which reads the file directly here) is the
            # only surface that can report it.
            parse_failed = False
            try:
                data = _yaml.load(meta_file.read_text(encoding="utf-8"))
            except (OSError, YAMLError) as exc:
                out.append(
                    Issue(
                        category="paper_dir_validity",
                        severity="error",
                        paper_id=child.name,
                        message=(
                            f"papers/{child.name}/metadata.yaml is unparseable "
                            f"({exc.__class__.__name__}) — paper invisible to "
                            "all checks/INDEX"
                        ),
                        hint=(
                            "fix the YAML syntax by hand; until then this paper "
                            "is silently dropped from list/INDEX"
                        ),
                    )
                )
                data = None
                parse_failed = True
            if parse_failed:
                pass  # already reported above
            elif data is None or data == {}:
                # Empty / comment-only YAML loads to None (or {}). list_papers
                # drops it, so report it here instead of letting it vanish.
                out.append(
                    Issue(
                        category="paper_dir_validity",
                        severity="error",
                        paper_id=child.name,
                        message=(
                            f"papers/{child.name}/metadata.yaml is empty — "
                            "paper invisible to all checks/INDEX"
                        ),
                        hint="populate metadata.yaml (at least id/title/year)",
                    )
                )
            elif not isinstance(data, dict):
                # Non-mapping top level (e.g. a bare list/scalar) is as broken
                # as a syntax error for our purposes.
                out.append(
                    Issue(
                        category="paper_dir_validity",
                        severity="error",
                        paper_id=child.name,
                        message=(
                            f"papers/{child.name}/metadata.yaml is not a mapping "
                            "— paper invisible to all checks/INDEX"
                        ),
                        hint="rewrite metadata.yaml as a key: value mapping",
                    )
                )
            elif isinstance(data, dict):
                meta_id = data.get("id")
                if meta_id and meta_id != child.name:
                    out.append(
                        Issue(
                            category="paper_dir_validity",
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

        if not (child / "paper.pdf").is_file():
            out.append(
                Issue(
                    category="paper_dir_validity",
                    severity="error",
                    paper_id=child.name,
                    message=(
                        f"papers/{child.name}/paper.pdf is missing — "
                        "`lit open` cannot show it and the file is irreplaceable"
                    ),
                    hint="restore the PDF into the paper folder",
                )
            )
    return out


def check_index_vs_disk(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``INDEX.json`` ↔ ``papers/`` reconciliation (ledger #1; cheap, klass A).

    The most fundamental drift pair, previously checked nowhere (spec §1.2): a
    manual ``rm -rf papers/<id>/`` leaves a dead INDEX entry forever. Reads
    ONLY the INDEX id set (:func:`views.load_index_ids`) + the ``papers/``
    directory listing (invariant #15: no per-paper ``metadata.yaml``), so it is
    safe in the Tier-1 hot path.

    Two directions:

    * **vanished id** (in INDEX, dir absent) — ``error``. Repairable by the
      klass-A regen (the Tier-1 hook drops it metadata-free; ``health-check
      --fix`` rebuilds INDEX from truth). The ``paper_id`` is set so the hook
      can collect the vanished ids for targeted wikilink annotation.
    * **un-indexed dir** (dir present, not in INDEX) — ``warning``. Adding it
      to INDEX needs the paper's metadata → Tier-2; the cheap path can only
      warn. Typically a corrupt ``metadata.yaml`` (dropped by ``list_papers``
      so the last write command never indexed it) or an interrupted ``add``.

    The ``papers`` argument is ignored — using it would route detection through
    ``list_papers`` (the corrupt-paper blind spot). Directory enumeration is
    the uncorrupted truth side (spec §4).
    """
    from litman.core import views

    index_ids = views.load_index_ids(vault)
    if index_ids is None:
        # No INDEX.json yet (or unparseable): nothing to reconcile against. A
        # fresh vault has no INDEX until the first write command; a corrupt
        # INDEX is a Tier-2 concern (the next regen rewrites it).
        index_ids = set()

    papers_dir = vault / "papers"
    disk_ids: set[str] = set()
    if papers_dir.is_dir():
        for child in papers_dir.iterdir():
            if child.is_dir() and is_valid_id(child.name):
                disk_ids.add(child.name)

    out: list[Issue] = []
    for vanished in sorted(index_ids - disk_ids):
        out.append(
            Issue(
                category="index_vs_disk",
                severity="error",
                paper_id=vanished,
                message=(
                    f"INDEX.json lists {vanished!r} but papers/{vanished}/ "
                    "no longer exists (manual delete or corrupt metadata)"
                ),
                hint="run `lit health-check --fix` (or any write command) to regen INDEX",
            )
        )
    for unindexed in sorted(disk_ids - index_ids):
        out.append(
            Issue(
                category="index_vs_disk",
                severity="warning",
                paper_id=unindexed,
                message=(
                    f"papers/{unindexed}/ exists but is not in INDEX.json — "
                    "not indexed (corrupt metadata / interrupted add)"
                ),
                hint="run `lit health-check` to see the structural fault",
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

    # Same-vault existence is reconciled against papers/<id>/ directory presence
    # (ADR-013 / review F8), not the list_papers projection, so no id set is
    # precomputed here; cross-vault refs resolve against the target's INDEX.json.
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
        rel = md_path.relative_to(vault)
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            # No silent-skip (M30 / invariant #14): a notes file we cannot read
            # means its wikilinks go unchecked, which is itself a finding.
            out.append(
                Issue(
                    category="dangling_wikilinks",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"could not read notes file {rel} "
                        f"({exc.__class__.__name__}) — skipped"
                    ),
                    hint="check file permissions / encoding",
                )
            )
            continue
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
                # Same-vault form: reconcile against the filesystem truth —
                # ``papers/<id>/`` directory presence (ADR-013), NOT list_papers
                # membership. A paper with corrupt / empty metadata.yaml is
                # dropped by list_papers but its directory still exists, so
                # keying on known_ids would falsely flag a live [[X]] as
                # "not in the vault" (review F8). The corrupt paper itself is
                # owned by check_paper_dir_validity. Two M24 drift warnings.
                exists = is_valid_id(paper_id) and (
                    vault / "papers" / paper_id
                ).is_dir()
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


def check_views_vs_metadata(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``views/by-*/`` symlink hubs ↔ metadata (ledger #2; full, klass A).

    The ``views/by-{project,topic,method,status}/`` symlink hubs are a derived
    projection of each paper's list/scalar tag fields (see ``core/views.py``).
    Out-of-band edits (manual ``rm`` of a paper, hand-edited tags) leave them
    disagreeing with metadata. Full-tier because the "missing symlink"
    direction needs the per-paper tag values (not in the thin INDEX
    projection). Repair is the shared klass-A regen (``rebuild_views``).

    Two directions, both ``error`` (a wrong view misleads the agent's
    bucket-based retrieval):

    * **dangling / extra symlink** — a ``views/<view>/<bucket>/<id>`` entry
      whose owning paper no longer carries that tag value (or no longer
      exists). The symlink claims a membership metadata does not.
    * **missing symlink** — a paper's tag value implies a
      ``views/<view>/<value>/<id>`` symlink that is absent on disk.

    Uses the same ``LIST_VIEW_FIELDS`` / ``SCALAR_VIEW_FIELDS`` /
    ``_safe_name`` mapping as the builder so detection and repair cannot drift.
    """
    from litman.core.views import (
        LIST_VIEW_FIELDS,
        SCALAR_VIEW_FIELDS,
        _safe_name,
    )

    views_dir = vault / "views"
    if not views_dir.is_dir():
        # No views hub yet — nothing derived to disagree. A first write
        # command / regen creates it.
        return []

    # Expected membership per view: {view_name: {(bucket, paper_id)}}.
    expected: dict[str, set[tuple[str, str]]] = {
        v: set() for v in (*LIST_VIEW_FIELDS, *SCALAR_VIEW_FIELDS)
    }
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        pid = str(pid)
        for view_name, field_name in LIST_VIEW_FIELDS.items():
            for value in p.get(field_name) or []:
                expected[view_name].add((_safe_name(str(value)), pid))
        for view_name, field_name in SCALAR_VIEW_FIELDS.items():
            value = p.get(field_name)
            if value:
                expected[view_name].add((_safe_name(str(value)), pid))

    out: list[Issue] = []
    for view_name in (*LIST_VIEW_FIELDS, *SCALAR_VIEW_FIELDS):
        view_dir = views_dir / view_name
        on_disk: set[tuple[str, str]] = set()
        if view_dir.is_dir():
            for bucket in view_dir.iterdir():
                if not bucket.is_dir():
                    continue
                for entry in bucket.iterdir():
                    if entry.is_symlink():
                        on_disk.add((bucket.name, entry.name))

        for bucket, pid in sorted(on_disk - expected[view_name]):
            out.append(
                Issue(
                    category="views_vs_metadata",
                    severity="error",
                    paper_id=pid,
                    message=(
                        f"views/{view_name}/{bucket}/{pid} symlink has no "
                        "matching metadata tag (stale derived view)"
                    ),
                    hint="run `lit health-check --fix` to rebuild views from metadata",
                )
            )
        for bucket, pid in sorted(expected[view_name] - on_disk):
            out.append(
                Issue(
                    category="views_vs_metadata",
                    severity="error",
                    paper_id=pid,
                    message=(
                        f"metadata implies views/{view_name}/{bucket}/{pid} "
                        "but the symlink is missing (derived view out of date)"
                    ),
                    hint="run `lit health-check --fix` to rebuild views from metadata",
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
    except OSError as exc:
        # No silent-skip (invariant #14): the file is present but unreadable
        # (permissions, non-UTF-8, dropped mount). Swallowing it would report a
        # clean vault while taxonomy governance is actually un-checkable — and
        # would contradict the missing-file branch above, which DOES emit.
        return [
            Issue(
                category="taxonomy_drift",
                severity="error",
                paper_id=None,
                message=(
                    f"TAXONOMY.md is present but unreadable "
                    f"({exc.__class__.__name__}): taxonomy drift cannot be "
                    "checked"
                ),
                hint="fix the file's permissions / encoding, then re-run",
            )
        ]
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


def check_config_readable(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``lit-config.yaml`` parses (invariant #14 no-silent-skip; review F6/F27).

    Owns the "config is present but unparseable" finding so the config-dependent
    checks (project consistency / path-exists / references / pdf-viewer) can
    narrow their guard to ``except ConfigError: return []`` and defer here
    instead of each reporting a clean vault while config-keyed governance is
    actually blind. Emitting it once, from a dedicated cheap-tier check, mirrors
    ``check_vault_registry_drift``'s corrupt-registry finding and keeps the
    Tier-1 per-command hook and Tier-2 ``health-check`` consistent.

    Cheap-tier safe (invariant #15): reads only the single ``lit-config.yaml``
    file (the same bounded read ``check_project_path_exists`` already does),
    never per-paper ``metadata.yaml``.
    """
    from litman.core.config import load_config

    try:
        load_config(vault)
    except ConfigError as exc:
        return [
            Issue(
                category="config_unreadable",
                severity="error",
                paper_id=None,
                message=(
                    f"lit-config.yaml is unreadable ({exc.__class__.__name__}): "
                    "config-dependent checks cannot run"
                ),
                hint="inspect / repair lit-config.yaml (see `lit config show`)",
            )
        ]
    return []


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
        # Unreadable TAXONOMY.md is OWNED by check_taxonomy_drift (it emits the
        # error); deferring here keeps it a single report (invariant #14 is
        # satisfied by the other check, not violated by this return []).
        return []

    from litman.core.config import load_config

    try:
        config = load_config(vault)
    except ConfigError:
        # Unparseable config is OWNED by check_config_readable (it emits the
        # error); deferring keeps it a single report. Narrowed from
        # `except Exception` (review F6) so a genuine bug in this check now
        # propagates instead of masquerading as a clean vault.
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


def check_vault_registry_drift(
    vault: Path,
    papers: list[dict[str, Any]],
    *,
    exists_status: dict[str, bool | None] | None = None,
) -> list[Issue]:
    """Registered vault entries whose on-disk directory has gone missing.

    Machine-level drift (registry ↔ vault dir, ledger #4). The ``vault`` /
    ``papers`` arguments are ignored — the registry is a user-level truth
    source, not vault-scoped — but the ``(vault, papers)`` signature is kept
    so the check slots into ``_CHECK_REGISTRY`` like any other.

    Uses the mount-safe **bounded-stat** probe (:func:`_drift._exists_bounded`,
    ADR-014) rather than a bare ``stat()``, which hangs or false-reports on a
    dropped HPC mount. This is the
    single-detection-core fix for the M30 §1.1 divergence: registry drift now
    uses bounded-stat in **both** the per-command hook AND ``health-check``.
    Only a definite ``False`` counts as drift; ``None`` (timeout / OSError) is
    "unknown" and never flagged — a slow mount must not look like a deleted
    vault. Reading the registry + bounded-stat is invariant-#15-safe (no
    per-paper ``metadata.yaml`` read).

    A corrupt registry is now an emitted finding (M30 Phase 3 / invariant #14
    no-silent-skip): "I cannot read the registry" is itself drift, not a clean
    state. The Tier-1 hook's ``check_and_prompt_registry_drift`` surfaces the
    same case to stderr, so both sides are consistent.

    ``exists_status`` (M30 Phase 5 / verification task 3): when the Tier-1 cheap
    hook has already bounded-stat'd these paths as part of a single shared 0.5s
    budget, it threads the pre-resolved ``{path: bool|None}`` map in so this
    check does not re-probe. Tier-2 (``run_all_checks``) passes ``None`` →
    falls back to its own :func:`_exists_bounded`, byte-unchanged.
    """
    from litman.commands._drift import _exists_bounded
    from litman.core.vault_registry import load_registry

    try:
        reg = load_registry()
    except VaultRegistryError as exc:
        return [
            Issue(
                category="vault_registry_drift",
                severity="error",
                paper_id=None,
                message=(
                    f"vault registry is unreadable ({exc.__class__.__name__}): "
                    "drift cannot be checked"
                ),
                hint="inspect / repair vaults.yaml (see `lit vault list`)",
            )
        ]

    if not reg.vaults:
        return []

    paths = [v.path for v in reg.vaults]
    if exists_status is None:
        status = _exists_bounded(paths)
    else:
        # Pre-resolved by the shared cheap-hook budget; any path the hook did
        # not include resolves to None (= unknown, never flagged).
        status = {p: exists_status.get(p) for p in paths}
    return [
        Issue(
            category="vault_registry_drift",
            severity="warning",
            paper_id=None,
            message=(
                f"registered vault {entry.name!r} points at "
                f"{entry.path} but that path no longer exists"
            ),
            hint=f"lit vault remove {entry.name}",
        )
        for entry in reg.vaults
        if status.get(entry.path) is False
    ]


def check_project_path_exists(
    vault: Path,
    papers: list[dict[str, Any]],
    *,
    exists_status: dict[str, bool | None] | None = None,
) -> list[Issue]:
    """Every lit-config.yaml ``projects:`` path exists and is a directory.

    Common after cross-machine sync (rclone / USB) where the registry
    travels but the project working directories live at different absolute
    paths per machine. Warning (not error) + NOT auto-fixable: only the
    user knows the correct path on this machine.

    Mount-safe (ADR-014): each configured path is probed with the bounded-stat
    (:func:`_drift._exists_bounded`) so a dropped HPC mount yields ``None``
    (unknown, never flagged) instead of hanging on a bare ``stat()``. This
    keeps the divergence-free guarantee in the ``health-check`` path too, not
    just the hook. A definite ``False`` is "does not exist"; a ``True`` that is
    not a directory is still surfaced via a direct ``is_dir`` check (the path
    is reachable, so the extra stat is cheap and cannot hang).

    ``exists_status`` (M30 Phase 5 / verification task 3): the Tier-1 cheap hook
    threads in its single shared bounded-stat result so this check does not
    re-probe. Tier-2 passes ``None`` → falls back to its own probe, unchanged.
    """
    from litman.commands._drift import _exists_bounded
    from litman.core.config import load_config

    try:
        config = load_config(vault)
    except ConfigError:
        # Owned by check_config_readable (also cheap-tier, so it fires in the
        # same Tier-1 hook). Narrowed from `except Exception` (review F6/F27)
        # so a real bug propagates instead of reporting a clean vault.
        return []

    if not config.projects:
        return []

    paths = {
        name: str(Path(path_str).expanduser())
        for name, path_str in config.projects.items()
    }
    if exists_status is None:
        status = _exists_bounded(list(paths.values()))
    else:
        status = {p: exists_status.get(p) for p in paths.values()}

    out: list[Issue] = []
    for name, path_str in sorted(config.projects.items()):
        project_dir = Path(path_str).expanduser()
        present = status.get(str(project_dir))
        if present is None:
            # Unknown (slow / dropped mount) — never flag (ADR-014).
            continue
        if present is False:
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


def check_project_references(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Project ``REFERENCES.md`` / ``litman_reflib/`` ↔ membership (ledger #3).

    For each configured project whose directory is reachable (bounded-stat),
    compare the derived ``<project_dir>/litman_reflib/`` against the project's
    membership set (papers whose ``projects`` field contains the name):

    * the generated ``REFERENCES.md`` content vs the freshly rendered content;
    * the ``litman_reflib/<id>`` symlink set vs the membership ids.

    Either mismatch is a klass-A drift (the derived artifact is a pure function
    of metadata). Full-tier: needs per-paper ``projects`` + ``relevance``
    (REFERENCES.md embeds the relevance annotation). Repair is the shared regen
    (``rebuild_all_project_refs`` + ``rebuild_all_project_links``). Only checked
    when the project dir is definitely present — an unreachable / not-yet-
    mounted dir is left to ``project_path_exists`` (ledger #5), not flagged as
    content drift here.
    """
    from litman.commands._drift import _exists_bounded
    from litman.core.config import load_config
    from litman.core.project_refs import (
        LITERATURE_SUBDIR,
        REFERENCES_FILENAME,
        _papers_for_project,
        render_references_md,
    )

    try:
        config = load_config(vault)
    except ConfigError:
        # Owned by check_config_readable; narrowed from `except Exception`
        # (review F6) so real bugs propagate.
        return []
    if not config.projects:
        return []

    paths = {
        name: str(Path(path_str).expanduser())
        for name, path_str in config.projects.items()
    }
    status = _exists_bounded(list(paths.values()))

    out: list[Issue] = []
    for name in sorted(config.projects):
        project_dir = Path(paths[name])
        if status.get(str(project_dir)) is not True:
            # Unreachable / unknown — project_path_exists owns that case.
            continue
        if not project_dir.is_dir():
            continue

        reflib = project_dir / LITERATURE_SUBDIR
        member_ids = {
            str(p.get("id"))
            for p in _papers_for_project(papers, name)
            if p.get("id")
        }

        # 1) REFERENCES.md content vs freshly rendered (banner timestamp is the
        #    only volatile line; compare with the timestamp pinned so a stale
        #    body — not just a clock difference — is what we report).
        refs_file = reflib / REFERENCES_FILENAME
        existing = None
        if refs_file.is_file():
            try:
                existing = refs_file.read_text(encoding="utf-8")
            except OSError as exc:
                out.append(
                    Issue(
                        category="project_references",
                        severity="warning",
                        paper_id=None,
                        message=(
                            f"could not read {refs_file} "
                            f"({exc.__class__.__name__}) — skipped"
                        ),
                        hint="check file permissions; run `lit refresh-views`",
                    )
                )
                continue
        if existing is not None:
            pinned_now = _references_banner_timestamp(existing)
            expected = render_references_md(
                vault, name, now=pinned_now, papers=papers
            )
            if _strip_references_banner(existing) != _strip_references_banner(
                expected
            ):
                out.append(
                    Issue(
                        category="project_references",
                        severity="error",
                        paper_id=None,
                        message=(
                            f"project {name!r} REFERENCES.md is out of date "
                            "with the membership set (derived content drift)"
                        ),
                        hint="run `lit health-check --fix` to regenerate it",
                    )
                )
        elif member_ids:
            out.append(
                Issue(
                    category="project_references",
                    severity="error",
                    paper_id=None,
                    message=(
                        f"project {name!r} has {len(member_ids)} member paper(s) "
                        "but no litman_reflib/REFERENCES.md"
                    ),
                    hint="run `lit health-check --fix` to generate it",
                )
            )

        # 2) litman_reflib/<id> symlink set vs membership.
        link_ids: set[str] = set()
        if reflib.is_dir():
            for entry in reflib.iterdir():
                if entry.is_symlink():
                    link_ids.add(entry.name)
        for extra in sorted(link_ids - member_ids):
            out.append(
                Issue(
                    category="project_references",
                    severity="error",
                    paper_id=extra,
                    message=(
                        f"{project_dir / LITERATURE_SUBDIR / extra} symlink has "
                        f"no matching membership in project {name!r}"
                    ),
                    hint="run `lit health-check --fix` to rebuild project links",
                )
            )
        for missing in sorted(member_ids - link_ids):
            out.append(
                Issue(
                    category="project_references",
                    severity="error",
                    paper_id=missing,
                    message=(
                        f"project {name!r} membership implies a litman_reflib "
                        f"symlink for {missing!r} but it is missing"
                    ),
                    hint="run `lit health-check --fix` to rebuild project links",
                )
            )
    return out


def _references_banner_timestamp(text: str) -> str:
    """Extract the ``<!-- Last updated: ... -->`` timestamp from a REFERENCES.md.

    Returns the embedded timestamp so the freshly rendered comparison body uses
    the same banner line (the timestamp is the only legitimately-volatile part;
    we want to flag a stale *body*, not a clock difference). Falls back to an
    empty string when no banner is present.
    """
    m = re.search(r"<!-- Last updated: (.*?) -->", text)
    return m.group(1) if m else ""


def _strip_references_banner(text: str) -> str:
    """Drop the auto-generated banner comment lines for body-only comparison."""
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.startswith("<!-- ")
    )


def check_relevance_orphan(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``relevance-<project>`` field whose paper is not in that project (ledger #11).

    A ``relevance-<project>`` annotation on a paper whose ``projects`` list does
    NOT contain ``<project>`` is an orphan (e.g. the paper was unlinked without
    purging relevance, or a ``project rm``/``rename`` did not cascade). klass
    B-auth: the relevance text is hand-authored, so this is **report-only** —
    never auto-deleted. Full-tier (the ``relevance-*`` fields are not in the
    INDEX projection).
    """
    out: list[Issue] = []
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        member_projects = set(p.get("projects") or [])
        for key in p:
            if not isinstance(key, str) or not key.startswith("relevance-"):
                continue
            project = key[len("relevance-"):]
            if not project:
                continue
            if project not in member_projects:
                out.append(
                    Issue(
                        category="relevance_orphan",
                        severity="warning",
                        paper_id=str(pid),
                        message=(
                            f"{pid!r} has a {key!r} annotation but its projects "
                            f"list does not contain {project!r} (orphan relevance)"
                        ),
                        hint=(
                            f"`lit modify {pid} --add-tag projects={project}` to "
                            f"re-link, or remove the {key} field by hand "
                            "(authored text — never auto-deleted)"
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
    except ConfigError:
        # Owned by check_config_readable; narrowed from `except Exception`
        # (review F6) so real bugs propagate rather than reading as clean.
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

    # #6b: repo-meta.yaml present but the actual codes/<name>/repo/ checkout is
    # missing. The metadata wrapper survived but the git clone did not (e.g. a
    # synced vault that excludes the bulky repo/, or an interrupted clone).
    # `lit code restore-all` re-clones from the recorded upstream.
    for repo_name in sorted(disk_repos):
        if not (codes_dir / repo_name / REPO_DIRNAME).is_dir():
            out.append(
                Issue(
                    category="code_clone_integrity",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"codes/{repo_name}/ has {REPO_META_FILENAME} but no "
                        f"{REPO_DIRNAME}/ checkout (clone missing)"
                    ),
                    hint=(
                        f"`lit code restore-all` to re-clone {repo_name} from "
                        "its recorded upstream, or `lit code rm "
                        f"{repo_name}` to drop the metadata"
                    ),
                )
            )

    # #6c: repo-meta.yaml's reverse ``papers:`` list ↔ those papers actually
    # exist on disk. A paper id recorded in the back-reference but with no
    # papers/<id>/ directory is a dangling reverse edge (the paper was rm'd
    # without updating the repo's reverse list). Directory enumeration is the
    # truth side (no per-paper metadata read needed for existence).
    existing_paper_dirs = set()
    papers_root = vault / "papers"
    if papers_root.is_dir():
        existing_paper_dirs = {
            c.name for c in papers_root.iterdir() if c.is_dir()
        }
    for repo_name in sorted(disk_repos):
        meta_file = codes_dir / repo_name / REPO_META_FILENAME
        try:
            meta = _yaml.load(meta_file.read_text(encoding="utf-8"))
        except (OSError, YAMLError) as exc:
            out.append(
                Issue(
                    category="code_clone_integrity",
                    severity="error",
                    paper_id=None,
                    message=(
                        f"codes/{repo_name}/{REPO_META_FILENAME} is unparseable "
                        f"({exc.__class__.__name__}) — cannot verify its papers"
                    ),
                    hint="fix the YAML by hand or `lit code rm` + re-clone",
                )
            )
            continue
        if not isinstance(meta, dict):
            continue
        for back_ref in meta.get("papers") or []:
            if not isinstance(back_ref, str) or not back_ref:
                continue
            if back_ref not in existing_paper_dirs:
                out.append(
                    Issue(
                        category="code_clone_integrity",
                        severity="error",
                        paper_id=back_ref,
                        message=(
                            f"codes/{repo_name}/{REPO_META_FILENAME} lists paper "
                            f"{back_ref!r} but papers/{back_ref}/ does not exist"
                        ),
                        hint=(
                            # review F30: there is no `lit code unlink`. The
                            # stale reverse edge lives in repo-meta.papers;
                            # clear it by hand, or restore the paper if it was
                            # mis-deleted.
                            f"remove {back_ref!r} from the papers: list in "
                            f"codes/{repo_name}/{REPO_META_FILENAME}, or "
                            f"`lit trash restore {back_ref}` if it was "
                            "mis-deleted"
                        ),
                    )
                )

    return out


# ---------------------------------------------------------------------------
# Orchestration + autofix
# ---------------------------------------------------------------------------


# The drift ledger (M30 / invariant #14): every check carries its tier, drift
# class, and correction mode. Registry order is the report order (errors first
# read naturally because severity is re-sorted per-category in health.py).
#
# Tag rationale (mapped from spec §3 ledger + §3 non-drift-diagnostics table):
#   schema — fixed-enum validity, truth-internal → klass=validity, report.
#   paper_dir_validity — truth-internal structural integrity (dir name / parseable
#       metadata / id match / paper.pdf), directory-enumeration truth side →
#       klass=validity, report. (M30 Phase 3: merges + replaces the old
#       id_consistency + invalid_paper_dirs and kills their except: continue
#       silent skips, invariant #14.)
#   index_vs_disk (#1) — INDEX.json (derived) ↔ papers/ dir → klass=A, regen.
#       Cheap: reads only INDEX ids + papers/ listing, no per-paper metadata
#       (invariant #15) → tier=cheap. Tier-1 repairs it metadata-free.
#   views_vs_metadata (#2) — views/by-*/ (derived) ↔ metadata → klass=A, regen.
#       Full: the missing-symlink direction needs per-paper tag values.
#   project_references (#3) — project REFERENCES.md / litman_reflib (derived) ↔
#       membership → klass=A, regen. Full (reads projects + relevance).
#   dangling_refs / bidirectional_refs — authored relation fields, surfaced for
#       the user/CLI to re-sync → klass=B-auth, correction=report.
#   dangling_wikilinks — authored prose marked in place (`(deleted)`) →
#       klass=B-auth, correction=annotate.
#   relevance_orphan (#11) — authored relevance-<project> annotation orphaned
#       from membership → klass=B-auth, report (never auto-delete authored text).
#   taxonomy_drift (#10) / project_config_consistency (#8) /
#       code_clone_integrity (#6a/#6b/#6c) — truth↔external/controlled-dict,
#       litman cannot pick a side → klass=B-ext, correction=resolve.
#   vault_registry_drift (#4) — truth↔external dir, machine-level (registry ↔
#       vault dir), bounded-stat only, no per-paper metadata → tier=cheap,
#       klass=B-ext, resolve. (M30 Phase 2: detection moved here from the two
#       divergent copies in _drift.py + health.py so registry drift uses
#       bounded-stat in every path.)
#   project_path_exists (#5) — truth↔external dir, cheap (config-path stat
#       only, no per-paper metadata) → tier=cheap, klass=B-ext, resolve.
#   inbox_staleness / stale_staging / trash_health / pdf_viewer — non-drift
#       diagnostics, surface only → klass=validity, correction=report.
_CHECK_REGISTRY: tuple[CheckSpec, ...] = (
    CheckSpec("schema", check_schema, "full", "validity", "report"),
    CheckSpec(
        "paper_dir_validity", check_paper_dir_validity, "full", "validity", "report"
    ),
    CheckSpec("index_vs_disk", check_index_vs_disk, "cheap", "A", "regen"),
    CheckSpec("views_vs_metadata", check_views_vs_metadata, "full", "A", "regen"),
    CheckSpec(
        "project_references", check_project_references, "full", "A", "regen"
    ),
    CheckSpec("dangling_refs", check_dangling_refs, "full", "B-auth", "report"),
    CheckSpec(
        "dangling_wikilinks", check_dangling_wikilinks, "full", "B-auth", "annotate"
    ),
    CheckSpec(
        "relevance_orphan", check_relevance_orphan, "full", "B-auth", "report"
    ),
    CheckSpec("taxonomy_drift", check_taxonomy_drift, "full", "B-ext", "resolve"),
    CheckSpec(
        "project_config_consistency",
        check_project_config_consistency,
        "full",
        "B-ext",
        "resolve",
    ),
    CheckSpec(
        "config_unreadable", check_config_readable, "cheap", "validity", "report"
    ),
    CheckSpec(
        "vault_registry_drift",
        check_vault_registry_drift,
        "cheap",
        "B-ext",
        "resolve",
    ),
    CheckSpec(
        "project_path_exists", check_project_path_exists, "cheap", "B-ext", "resolve"
    ),
    CheckSpec(
        "bidirectional_refs", check_bidirectional_refs, "full", "B-auth", "report"
    ),
    CheckSpec("inbox_staleness", check_inbox_staleness, "full", "validity", "report"),
    CheckSpec("stale_staging", check_stale_staging, "full", "validity", "report"),
    CheckSpec("trash_health", check_trash_health, "full", "validity", "report"),
    CheckSpec("pdf_viewer", check_pdf_viewer, "full", "validity", "report"),
    CheckSpec(
        "code_clone_integrity",
        check_code_clone_integrity,
        "full",
        "B-ext",
        "resolve",
    ),
)


def cheap_checks() -> tuple[CheckSpec, ...]:
    """Registered checks eligible for the Tier-1 per-command hook.

    Every returned spec has ``tier == "cheap"`` and (per invariant #15) reads
    only INDEX/registry/directory-listings/bounded-stat — never per-paper
    ``metadata.yaml``. Phase 2 wires this into ``LitGroup.invoke``.
    """
    return tuple(spec for spec in _CHECK_REGISTRY if spec.tier == "cheap")


def klass_a_checks() -> tuple[CheckSpec, ...]:
    """Registered derived↔truth checks (klass A), correctable by regen.

    Phase 2's ``health-check --fix`` auto-regens this set (lossless); Phase 1
    only exposes the accessor.
    """
    return tuple(spec for spec in _CHECK_REGISTRY if spec.klass == "A")


def run_all_checks(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Run every check in registry order; return the flat list of issues."""
    out: list[Issue] = []
    for spec in _CHECK_REGISTRY:
        out.extend(spec.fn(vault, papers))
    return out


# Checks whose findings must NOT block a cloud push: they do not mean the
# vault *content being mirrored* is corrupt.
#   - klass A (index / views / project-refs) is excluded structurally below
#     (derived from TRUTH, fixable by a lossless regen).
#   - vault_registry_drift: the per-machine registry lives outside the vault
#     and is never pushed; its only error case is "registry unreadable", a
#     config problem, not corrupt vault content (and find_vault already
#     succeeded to reach the gate). Blocking a backup on it is a false stop.
#   - dangling_wikilinks: its only *error* cases are cross-vault links
#     ([[other-vault:id]]) — unregistered / unreadable / id-not-found all
#     depend on this machine's registry + sibling vaults (external state, e.g.
#     a fresh machine that pulled vault A but has not yet registered sibling
#     B), and the malformed-link error is a minor authored typo (annotate
#     class), below the bar for blocking a whole-vault backup. Same-vault
#     dangling links are warnings and never block regardless.
_PUSH_GATE_EXCLUDED_CATEGORIES: frozenset[str] = frozenset(
    {"vault_registry_drift", "dangling_wikilinks"}
)


def run_push_integrity_errors(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Error-severity findings that should block a cloud push (C-ops1).

    ``lit sync push`` mirrors the vault over its only cloud backup
    (invariant #9: the vault is not git-tracked), so a corrupted local state
    must not be pushed. This runs every registered check EXCEPT those that do
    not reflect corruption of the *vault content being mirrored*: the klass-A
    derived<->truth checks (index / views / project-refs — pure functions of
    TRUTH, fixable by a lossless regen) and the categories in
    ``_PUSH_GATE_EXCLUDED_CATEGORIES``. Excluding them matters because blocking
    a backup on regen-fixable drift would only depress how often the user backs
    up, which is itself less safe. Only ``error``-severity findings from the
    remaining checks (TRUTH validity, authored refs, governance) are returned;
    ``warning`` / ``info`` never block.

    This is a different layer from atomic crash-safety: a torn atomic op
    (F3) is recovered at vault-open time and may leave no trace here, while
    this gate catches damage atomic recovery cannot (hand-edited broken
    schema, half-finished paper dirs). The two are complementary.
    """
    out: list[Issue] = []
    for spec in _CHECK_REGISTRY:
        if spec.klass == "A" or spec.category in _PUSH_GATE_EXCLUDED_CATEGORIES:
            continue
        out.extend(i for i in spec.fn(vault, papers) if i.severity == "error")
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
                        try:
                            child.unlink()
                        except OSError:
                            # Best-effort: a locked / unremovable sidecar must
                            # not abort the whole `--fix` run and discard the
                            # fixes already applied. It stays an orphan and is
                            # re-detected (and re-offered for fixing) on the
                            # next health-check.
                            continue
                        n += 1
        counts["orphan_trash_sidecar"] = n

    return counts
