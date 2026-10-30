"""The check registry and what runs it: the ordered ledger of every check,
the cheap and klass-A subsets, the full run, the push gate, grouping, and
``--fix`` (:func:`apply_autofix`, :func:`finish_unfinished_renames`).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ruamel.yaml.error import YAMLError

from litman.core.atomic import DirMoveError, cleanup_stale_staging, staged_write
from litman.core.checks._base import (
    AUTO_FIXABLE_CATEGORIES,
    DIRTY_NOTES_UNREADABLE,
    DIRTY_RENAMES,
    CheckSpec,
    Issue,
    _yaml,
)
from litman.core.checks.links import (
    check_bidirectional_refs,
    check_dangling_refs,
    check_dangling_wikilinks,
    check_views_vs_metadata,
)
from litman.core.checks.maintenance import (
    _default_agent_skill_probe,
    check_code_clone_integrity,
    check_notes_from_trash,
    check_pdf_viewer,
    check_skill_drift,
    check_stale_staging,
    check_supplementary,
    check_supplementary_cited,
    check_trash_health,
    check_vault_registry_drift,
)
from litman.core.checks.metadata import (
    _unfinished_rename_blocker,
    check_dirty_metadata,
    check_discussion_scaffold,
    check_duplicate_doi,
    check_inbox_staleness,
    check_index_vs_disk,
    check_paper_dir_validity,
    check_placeholder_id,
    check_placeholder_metadata,
    check_schema,
    check_weak_id_keyword,
)
from litman.core.checks.projects import (
    check_config_readable,
    check_link_support,
    check_priority_orphan,
    check_project_bridge_dangling,
    check_project_config_consistency,
    check_project_path_exists,
    check_project_references,
    check_relevance_orphan,
)
from litman.core.checks.taxonomy import (
    check_taxonomy_drift,
    check_taxonomy_similar,
    check_taxonomy_singletons,
    check_taxonomy_unused,
)
from litman.core.document import list_papers
from litman.core.id import is_valid_id
from litman.core.notes import heal_discussion_scaffold
from litman.core.trash import TRASH_DIRNAME

if TYPE_CHECKING:
    from litman.core.supplementary import SiFixPolicy


# ---------------------------------------------------------------------------
# Orchestration + autofix
# ---------------------------------------------------------------------------


# The drift ledger (M30 / invariant #14): every check carries its tier, drift
# class, and correction mode. Registry order is the report order (errors first
# read naturally because severity is re-sorted per-category in health.py).
#
# Tag rationale (mapped from spec §3 ledger + §3 non-drift-diagnostics table):
#   schema — fixed-enum validity, truth-internal → klass=validity, report.
#   dirty_metadata — a title / journal / booktitle / publisher / author name
#       holding line breaks, indentation or markup, stored before the ingest
#       boundary cleaned it → klass=validity (truth-internal, nothing derived
#       disagrees), correction=regen through the validity-fix path
#       (AUTO_FIXABLE_CATEGORIES): the rewrite is deterministic and keeps
#       every word. Never the id.
#   paper_dir_validity — truth-internal structural integrity (dir name / parseable
#       metadata / id match / paper.pdf), directory-enumeration truth side →
#       klass=validity, report. (M30 Phase 3: merges + replaces the old
#       id_consistency + invalid_paper_dirs and kills their except: continue
#       silent skips, invariant #14.) An id mismatch `--fix` can settle by
#       renaming the folder is emitted as `unfinished_rename` instead, which
#       rides the validity-fix path (AUTO_FIXABLE_CATEGORIES).
#   index_vs_disk (#1) — INDEX.json (derived) ↔ papers/ dir → klass=A, regen.
#       Cheap: reads only INDEX ids + papers/ listing, no per-paper metadata
#       (invariant #15) → tier=cheap. Tier-1 repairs it metadata-free.
#   views_vs_metadata (#2) — views/by-*/ (derived) ↔ metadata → klass=A, regen.
#       Full: the missing-link direction needs per-paper tag values.
#   project_references (#3) — project REFERENCES.md / litman_reflib (derived) ↔
#       membership → klass=A, regen. Full (reads projects + relevance).
#   project_bridge_dangling (#3's cheap arm) — the litman_reflib/litman_code
#       bridge links resolve to a live target. Disjoint from
#       project_references by construction: that check compares link NAMES to
#       membership and is blind to a link whose name matches but whose target
#       moved (the vault-moved case). Cheap: config + hub listing +
#       bounded-stat of link targets, no per-paper metadata (invariant #15) →
#       tier=cheap, klass=A, regen (`--fix` regen repairs it; the Tier-1 hook
#       gates the same rebuild behind a [Y/n] because rebuilding reads
#       metadata).
#   links_unsupported — the ONLY klass=env entry: a capability of the host,
#       not a state of the library. Nothing drifted, nothing is broken, and no
#       correction exists (hence report + severity=info, which health-check does
#       not gate its exit code on). It exists so that when a filesystem cannot
#       hold folder links — POSIX symlinks and Windows junctions alike need
#       link storage, which FAT32 / exFAT and most network shares lack —
#       views_vs_metadata and project_references can go SILENT about links they
#       know cannot exist, and the absence is still explained once instead of
#       reported ~6x per paper as unfixable damage.
#   dangling_refs / bidirectional_refs — authored relation fields, surfaced for
#       the user/CLI to re-sync → klass=B-auth, correction=report. A dangling
#       edge points at nothing, so there is no side to re-sync it with; `--fix`
#       removes it (AUTO_FIXABLE_CATEGORIES, as with retired_priority).
#   dangling_wikilinks — authored prose marked in place (`(deleted)`) →
#       klass=B-auth, correction=annotate.
#   relevance_orphan (#11) — authored relevance-<project> annotation orphaned
#       from membership → klass=B-auth, report (never auto-delete authored text).
#   priority_orphan — the same cross-entity reference for the paired
#       priority-<project> grade (ADR-025) → klass=B-auth, report, tier=full
#       (a variable-length key can never enter the INDEX projection, so this
#       can never drop to Tier-1, invariant #15). The RETIRED paper-level
#       `priority` it replaces is a different finding: check_schema reports it
#       under `retired_priority`, and that one IS auto-fixable — see
#       AUTO_FIXABLE_CATEGORIES for why the migration is admitted despite
#       being the only lossy arm.
#   taxonomy_drift (#10) / project_config_consistency (#8) /
#       code_clone_integrity (#6a/#6b/#6c/#6d) — truth↔external/controlled-dict,
#       litman cannot pick a side → klass=B-ext, correction=resolve. #6d is the
#       symmetric forward check: repo-meta.papers names a live paper whose
#       code-clones does not list the repo back (one-sided binding).
#   taxonomy_similar / taxonomy_unused / taxonomy_singletons — the shape of
#       the topics / methods / data vocabulary itself (look-alike pairs, values
#       no paper uses, dicts mostly made of once-used values). Nothing is
#       inconsistent, so klass=validity, report, severity=info (health-check
#       does not gate its exit code on them: a pair kept on purpose would
#       otherwise fail every run). Never --fix: which name survives a merge,
#       and whether a value is registered ahead of its papers, is the user's.
#       tier=full (they count usage over every paper).
#   vault_registry_drift (#4) — truth↔external dir, machine-level (registry ↔
#       vault dir), bounded-stat only, no per-paper metadata → tier=cheap,
#       klass=B-ext, resolve. (M30 Phase 2: detection moved here from the two
#       divergent copies in _drift.py + health.py so registry drift uses
#       bounded-stat in every path.)
#   project_path_exists (#5) — truth↔external dir, cheap (config-path stat
#       only, no per-paper metadata) → tier=cheap, klass=B-ext, resolve.
#   inbox_staleness / stale_staging / trash_health / pdf_viewer — non-drift
#       diagnostics, surface only → klass=validity, correction=report.
#   notes_from_trash — a repo note a restore wrote beside a different
#       notes.md (codes/<name>/notes.from-trash.md): two notes of the user's,
#       nothing derived, nothing for litman to pick → klass=validity, report,
#       severity=info, never --fix (the replaced_folders reasoning).
#   supplementary (#13) — metadata `supplementary` records ↔ the files in
#       papers/<id>/supplementary/ → klass=B-auth (both sides are authored
#       truth), report; tier=full (the records are not in the INDEX
#       projection, invariant #15). Of its six finding categories only
#       missing / unrecorded are `--fix`able, with consent (see
#       AUTO_FIXABLE_CATEGORIES); field / changed / layout / unchecked need
#       a person.
#   supplementary_cited (#14) — si-NN citations in notes.md / discussion.md
#       ↔ the supplementary records (number, original name; a discussion
#       section's date against the record's `added`) → klass=B-auth, report
#       (warning, never --fix: the text is the user's); tier=full (reads every
#       paper's notes); outside the push gate.
_CHECK_REGISTRY: tuple[CheckSpec, ...] = (
    CheckSpec("schema", check_schema, "full", "validity", "report"),
    # Same shape as schema (metadata read, nothing derived to regenerate), but
    # about content rather than structure: only the user knows the real author,
    # so it reports and never fixes.
    CheckSpec(
        "placeholder_metadata",
        check_placeholder_metadata,
        "full",
        "validity",
        "report",
    ),
    # Same reads as placeholder_metadata, but the repair needs no one's
    # knowledge — the clean text is already in the field — so, like
    # discussion_scaffold, it rides the validity-fix path instead of reporting.
    CheckSpec(
        "dirty_metadata", check_dirty_metadata, "full", "validity", "regen"
    ),
    # The id half of placeholder_metadata, registered next to it so a legacy
    # vault reports both together. Never `regen`: renaming is a cascade over
    # other papers' ref lists and notes, and which handle a paper should carry
    # is a judgment call, so `--fix` must not pick one (ADR-015).
    CheckSpec("placeholder_id", check_placeholder_id, "full", "validity", "report"),
    # Same tier/klass as placeholder_metadata and for the same reason: it reads
    # metadata, judges content rather than structure, and only the reader knows
    # the replacement — so it reports and never fixes.
    CheckSpec(
        "weak_id_keyword", check_weak_id_keyword, "full", "validity", "report"
    ),
    CheckSpec("duplicate_doi", check_duplicate_doi, "full", "validity", "report"),
    CheckSpec(
        "paper_dir_validity", check_paper_dir_validity, "full", "validity", "report"
    ),
    # Not klass A: the log is authored TRUTH, not a derived artifact, so `--fix`
    # must not route it through the wholesale `regen` (which would report it
    # healed while writing no file). It rides the validity-fix path instead.
    CheckSpec(
        "discussion_scaffold",
        check_discussion_scaffold,
        "full",
        "validity",
        "regen",
    ),
    CheckSpec("index_vs_disk", check_index_vs_disk, "cheap", "A", "regen"),
    CheckSpec("views_vs_metadata", check_views_vs_metadata, "full", "A", "regen"),
    CheckSpec(
        "project_references", check_project_references, "full", "A", "regen"
    ),
    CheckSpec(
        "project_bridge_dangling",
        check_project_bridge_dangling,
        "cheap",
        "A",
        "regen",
    ),
    CheckSpec(
        "links_unsupported",
        check_link_support,
        "full",
        "env",
        "report",
    ),
    CheckSpec("dangling_refs", check_dangling_refs, "full", "B-auth", "report"),
    CheckSpec(
        "dangling_wikilinks", check_dangling_wikilinks, "full", "B-auth", "annotate"
    ),
    CheckSpec(
        "relevance_orphan", check_relevance_orphan, "full", "B-auth", "report"
    ),
    CheckSpec(
        "priority_orphan", check_priority_orphan, "full", "B-auth", "report"
    ),
    CheckSpec("taxonomy_drift", check_taxonomy_drift, "full", "B-ext", "resolve"),
    CheckSpec("taxonomy_similar", check_taxonomy_similar, "full", "validity", "report"),
    CheckSpec("taxonomy_unused", check_taxonomy_unused, "full", "validity", "report"),
    CheckSpec(
        "taxonomy_singletons", check_taxonomy_singletons, "full", "validity", "report"
    ),
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
    # skill_drift — the installed agent-skill copies are deploy artifacts of
    # the *package* (regen = re-copy, lossless), but they are machine-level,
    # not vault-derived: klass A would route them into the vault-wide regen
    # (which never touches the skills dir) and into the klass-A category
    # sets. Like discussion_scaffold they ride the validity-fix path instead
    # (AUTO_FIXABLE_CATEGORIES + apply_autofix).
    CheckSpec("skill_drift", check_skill_drift, "full", "validity", "regen"),
    CheckSpec(
        "code_clone_integrity",
        check_code_clone_integrity,
        "full",
        "B-ext",
        "resolve",
    ),
    CheckSpec(
        "notes_from_trash", check_notes_from_trash, "full", "validity", "report"
    ),
    CheckSpec("supplementary", check_supplementary, "full", "B-auth", "report"),
    CheckSpec(
        "supplementary_cited", check_supplementary_cited, "full", "B-auth", "report"
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
#   - retired_priority: a schema-vintage marker, not damage. Every paper is
#     readable and its grade intact, so an unmigrated vault mirrors losslessly
#     — and the backup has to stay available across the upgrade window, which
#     is exactly when the user has not run `--fix` yet (ADR-025).
#   - supplementary_cited: warnings only, so it could never block; listed so
#     a push does not read every paper's notes.md / discussion.md for nothing.
#   - taxonomy_similar / taxonomy_unused / taxonomy_singletons: info only, so
#     they can never block; listed so a push skips the pairwise value scan.
_PUSH_GATE_EXCLUDED_CATEGORIES: frozenset[str] = frozenset(
    {
        "vault_registry_drift",
        "dangling_wikilinks",
        "retired_priority",
        "supplementary_cited",
        "taxonomy_similar",
        "taxonomy_unused",
        "taxonomy_singletons",
    }
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

    The exclusion set is consulted twice: per spec (a check whose registry
    category is listed never runs) AND per emitted issue. One check can emit
    several categories — ``check_schema`` emits both ``schema`` and
    ``retired_priority`` — so only the issue-level pass can gate one and let
    the other through.

    This is a different layer from atomic crash-safety: a torn atomic op
    (F3) is recovered at vault-open time and may leave no trace here, while
    this gate catches damage atomic recovery cannot (hand-edited broken
    schema, half-finished paper dirs). The two are complementary.
    """
    out: list[Issue] = []
    for spec in _CHECK_REGISTRY:
        if spec.klass == "A" or spec.category in _PUSH_GATE_EXCLUDED_CATEGORIES:
            continue
        out.extend(
            i
            for i in spec.fn(vault, papers)
            if i.severity == "error"
            and i.category not in _PUSH_GATE_EXCLUDED_CATEGORIES
        )
    return out


def group_by_category(issues: Iterable[Issue]) -> dict[str, list[Issue]]:
    """Group issues by ``category`` while preserving registry-defined order."""
    grouped: dict[str, list[Issue]] = {}
    for issue in issues:
        grouped.setdefault(issue.category, []).append(issue)
    return grouped


def finish_unfinished_renames(
    vault: Path,
) -> tuple[int, list[tuple[str, str, str]]]:
    """Rename every folder whose metadata id names where it belongs.

    Re-reads ``papers/`` rather than trusting the issue list, and asks
    :func:`_unfinished_rename_blocker` again right before each move (an
    earlier move can take the name a later folder wants). Each move is its
    own ``staged_write``, so a crash mid-move is settled by the next command.
    A folder that cannot be moved is left and returned as
    ``(folder, metadata id, why and what to do)``; the rest still move.

    Returns ``(moved, left)`` and rebuilds nothing: the caller runs the one
    derived rebuild, after every folder is in place.
    """
    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return 0, []
    papers = list_papers(vault)
    moved = 0
    left: list[tuple[str, str, str]] = []
    for child in sorted(papers_dir.iterdir()):
        meta_file = child / "metadata.yaml"
        if not is_valid_id(child.name) or not meta_file.is_file():
            continue
        try:
            data = _yaml.load(meta_file.read_text(encoding="utf-8"))
        except (OSError, YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        meta_id = data.get("id")
        if not meta_id or meta_id == child.name:
            continue
        if _unfinished_rename_blocker(vault, child.name, data, papers) is not None:
            continue
        op_id = f"fix-rename-{child.name}-to-{meta_id}"
        if os.path.lexists(vault / ".litman-staging" / op_id):
            left.append(
                (
                    child.name,
                    meta_id,
                    "its earlier --fix is kept in .litman-staging/; "
                    "settle that finding first",
                )
            )
            continue
        try:
            with staged_write(vault, op_id=op_id) as stage:
                stage.move_dir(f"papers/{child.name}", f"papers/{meta_id}")
        except DirMoveError as err:
            left.append(
                (
                    child.name,
                    meta_id,
                    f"{err.reason}; close any file open from it and run --fix again",
                )
            )
            continue
        moved += 1
    return moved, left


def apply_autofix(
    vault: Path,
    issues: list[Issue],
    *,
    si_policy: SiFixPolicy | None = None,
) -> dict[str, Any]:
    """Auto-clean fixable categories. Returns ``{category: n_fixed}``.

    Plus, when the ``dirty_metadata`` arm could not read a notes.md, the
    ``(folder, reason)`` pairs under :data:`DIRTY_NOTES_UNREADABLE`, and when
    it cleaned a title an id was derived from, the ``(id, clean-derived id)``
    pairs under :data:`DIRTY_RENAMES` — detail for the CLI, not categories.

    ``unfinished_rename`` is not fixed here: ``lit health-check --fix``
    renames those folders itself (:func:`finish_unfinished_renames`), before
    the derived rebuild and before every arm below, since until a folder
    carries its id the arms that look a paper up by id see it as missing.

    Currently fixes:

    * ``stale_staging``  — drops every ``.litman-staging/<op-id>/`` entry.
    * ``orphan_trash_sidecar`` — deletes ``<entry>.meta.yaml`` files in
      ``.trash/`` whose entry dir is missing.
    * ``discussion_scaffold`` — creates the missing ``discussion.md`` (or
      re-inserts its append-format header) for each flagged paper. Additive
      only: an existing log keeps every dated section it already holds.
    * ``skill_drift`` — re-copies each stale installed agent skill from the
      bundled package content (lossless: the installed dir is a deploy
      artifact, and files the user added next to SKILL.md are kept). Every
      run also sweeps the OTHER known agent directories for stale copies
      and refreshes those too (counted under the same category): a stale
      non-default copy may be the one an agent actually reads — Cursor
      prefers the Claude dir over the open-standard one. The fix is wider
      than the check on purpose; the check keeps its default-dir-only probe.
    * ``retired_priority`` — migrates the retired paper-level ``priority``
      onto the per-project ``priority-<project>`` keys (ADR-025). Not
      lossless; see :data:`AUTO_FIXABLE_CATEGORIES`.
    * ``dangling_refs`` — removes every relation edge naming a paper no longer
      in the library, forward and reverse. Also not lossless; see the same
      constant.
    * ``dirty_metadata`` — rewrites titles, venues and author names that carry
      line breaks, stray spaces or markup, and the notes.md heading, which
      follows the title (:func:`litman.core.ripple.clean_dirty_metadata`).
      Lossless; never the id.
    * ``supplementary_missing`` / ``supplementary_unrecorded`` — drops the
      records of SI files that are gone and records files found in a paper's
      ``supplementary/`` folder (verified first), asking through
      ``si_policy``. Without one, nobody is asked: only files that verify are
      recorded, and the rest stays in the report.
    """
    counts: dict[str, Any] = {}

    fixable_present = {i.category for i in issues if i.category in AUTO_FIXABLE_CATEGORIES}

    if "skill_drift" in fixable_present:
        from litman.core.skill import (
            SkillInstallError,
            install_skill,
            skill_status,
        )

        n = 0
        # Same resolution as check_skill_drift: the fix targets the resolved
        # default agent's directory — the one the issues came from.
        probe = _default_agent_skill_probe()
        if probe is not None:
            _agent_name, parent_dir = probe
            # Re-derive which skills are stale (issues carry prose, not
            # state); only those are refreshed — absent skills stay a
            # respected opt-out.
            for name, info in sorted(
                skill_status(parent_dir=parent_dir).items()
            ):
                if info["state"] != "stale":
                    continue
                try:
                    install_skill(
                        target=parent_dir / name, overwrite=True, name=name
                    )
                except (SkillInstallError, OSError):
                    # Best-effort: an unwritable skills dir must not abort
                    # the whole `--fix` run. The skill stays flagged and is
                    # re-offered on the next health-check.
                    continue
                n += 1
        counts["skill_drift"] = n

    # Cross-directory skill sweep — every fix run, not gated on a
    # skill_drift finding: the check probes only the default agent's dir,
    # but a stale copy installed for ANOTHER agent may be the one actually
    # in effect (Cursor reads the Claude dir first). Autofix is already
    # explicit consent, so stale copies elsewhere are refreshed outright
    # (absent stays an opt-out, linked stays dev-managed); the helper is
    # best-effort per copy for the same reason as the arm above.
    from litman.core.agents import skills_parent_dirs
    from litman.core.skill import refresh_stale_copies

    probe = _default_agent_skill_probe()
    main_dir = probe[1] if probe is not None else None
    swept = refresh_stale_copies(
        d for d in skills_parent_dirs() if d != main_dir
    )
    n_swept = sum(len(names) for names in swept.values())
    if n_swept:
        counts["skill_drift"] = counts.get("skill_drift", 0) + n_swept

    if "discussion_scaffold" in fixable_present:
        n = 0
        for issue in issues:
            if issue.category != "discussion_scaffold" or not issue.paper_id:
                continue
            try:
                if heal_discussion_scaffold(vault, issue.paper_id):
                    n += 1
            except (OSError, UnicodeDecodeError):
                # An unwritable folder or an unreadable (non-UTF-8) log must not
                # abort the whole `--fix` and discard the fixes already applied
                # — and the unreadable one is never blindly overwritten, which
                # is what its finding promised. It stays flagged and is
                # re-offered on the next health-check.
                continue
        counts["discussion_scaffold"] = n

    if "retired_priority" in fixable_present:
        # Local import: core.ripple imports core.correctors inside its functions
        # and core.correctors imports this package, so checks -> ripple ->
        # correctors -> checks is a loop. One local edge is enough to keep it out
        # of module load; both stay local, as ripple.migrate_retired_priority notes.
        from litman.core.ripple import migrate_retired_priority

        counts["retired_priority"] = migrate_retired_priority(vault)

    if "dangling_refs" in fixable_present:
        # Lazy for the same reason as the arm above.
        from litman.core.ripple import drop_dangling_relations

        counts["dangling_refs"] = drop_dangling_relations(vault)

    if "dirty_metadata" in fixable_present:
        # Lazy for the same reason as the arm above.
        from litman.core.ripple import clean_dirty_metadata

        n_cleaned, notes_unreadable, renames = clean_dirty_metadata(vault)
        counts["dirty_metadata"] = n_cleaned
        if notes_unreadable:
            counts[DIRTY_NOTES_UNREADABLE] = notes_unreadable
        if renames:
            counts[DIRTY_RENAMES] = renames

    si_present = fixable_present & {
        "supplementary_missing",
        "supplementary_unrecorded",
    }
    if si_present:
        from litman.core.supplementary import NO_PROMPT_POLICY, fix_supplementary

        si_ids = {
            i.paper_id for i in issues if i.category in si_present and i.paper_id
        }
        fixed = fix_supplementary(
            vault, si_ids, policy=si_policy or NO_PROMPT_POLICY
        )
        for category in si_present:
            counts[category] = fixed[category]

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
