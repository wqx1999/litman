"""Shared pieces of the health checks: the :class:`Issue` record, the
:class:`CheckSpec` ledger entry and its tag vocabularies, the categories
``--fix`` may clean, the report keys and the thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from litman.core.yaml_pool import ThreadLocalYAML

_yaml = ThreadLocalYAML(typ="safe")

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
KLASSES: frozenset[str] = frozenset({"A", "B-ext", "B-auth", "validity", "env"})
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
            (truth↔external dir), ``"B-auth"`` (truth↔authored truth),
            ``"validity"`` (truth-internal integrity, not a drift), or
            ``"env"`` (a capability of the host, not a state of the library —
            nothing drifted and nothing is broken, so it can never be
            "corrected", only reported).
        correction: ``"regen"`` / ``"resolve"`` / ``"annotate"`` /
            ``"report"`` — the correction mode this check's findings map to.
    """

    category: str
    fn: _CheckFn
    tier: Literal["cheap", "full"]
    klass: Literal["A", "B-ext", "B-auth", "validity", "env"]
    correction: Literal["regen", "resolve", "annotate", "report"]


# Issue categories that ``--fix`` will auto-clean. See :func:`apply_autofix`.
# Phase 1 keeps this exactly as the historical set (stale_staging +
# orphan_trash_sidecar). Broadening ``--fix`` to all klass-A regen is Phase 2;
# until then this stays a literal so external ``--fix`` behavior is unchanged.
# ``discussion_scaffold`` joins them as a third validity fix: creating a missing
# empty log (or restoring its header) is a pure add, never an overwrite.
# ``skill_drift`` is the fourth: re-copying the bundled skill files over a
# stale installed copy is lossless (the installed dir is a deploy artifact;
# user-added files next to SKILL.md are never touched).
#
# ``retired_priority`` is the fifth and it BREAKS that pattern — it is the
# first arm that can lose data, so it is admitted on a different argument.
# The migration copies the retired paper-level grade onto every project the
# paper is linked to; a paper in NO project has nowhere to put it and the
# letter is dropped outright (ADR-025 decision 6). What is lost is a value the
# schema no longer has a place for: `priority` meant "important" with no "to
# what", which `status` already carries for an unlinked paper. Leaving it
# unfixed is not the safer option either — the field is retired, so every
# paper still holding it is a permanent schema error, and the only other way
# out is hand-editing every metadata.yaml.
#
# The consent this rests on is per-finding, not per-report: check_schema gives
# a paper in NO project a different message, naming the drop. It deliberately
# is not "the report lists every affected paper first" — _CATEGORY_PREVIEW
# folds each category after 5 entries unless `--all` is passed, so on a
# 21-paper library most are never printed by name.
#
# ``dangling_refs`` is the sixth and the second that can lose authored data: a
# relation edge naming a paper no longer in the library (deleted outside
# litman, lost by a sync, or in the trash). What goes is a pointer to nothing.
# It is admitted because nothing else can remove a reverse one — `lit modify`
# refuses to name a reverse field (ADR-012) and the forward edge it mirrors
# lived on the missing paper — so without this arm the finding is an error no
# command clears, and it blocks `lit sync push`. A trashed paper keeps every
# edge it carries itself and gets their other halves back on restore; the cost
# is a half-edge only the surviving paper held. Per-finding consent again, the
# same way: an edge into the trash gets its own message, offering the restore.
#
# ``supplementary_missing`` / ``supplementary_unrecorded`` are the SI pair, and
# the only arms that ask before acting. Recording a file found in a paper's
# supplementary/ folder verifies it first and never takes one that belongs
# elsewhere; one that cannot be confirmed goes in only on a yes. Dropping the
# record of an SI file that is gone loses a pointer to nothing, but a
# half-pulled sync looks the same, so it is asked about too (default yes) —
# and with no one to ask, both are left in the report with the explicit
# command that would do it.
#
# ``dirty_metadata`` is lossless again: a title, venue or author name still
# carrying line breaks, indentation or markup from a publisher's record is
# rewritten as the plain line it reads as — every word kept, only the layout
# and the tags gone. Never the id (the finding names the `lit rename` when the
# id was made from the broken title). In notes.md only the first line changes:
# the `# <title>` heading is derived from the title and follows it, as after
# `lit modify --set title=`, even where the user rewrote it.
#
# ``unfinished_rename`` is lossless where it is offered: it moves a paper's
# folder to the id its metadata.yaml holds and writes no content. It is
# offered only when that id is valid and free, no other folder claims it, no
# unfinished write in .litman-staging/ moves either folder, and nothing in the
# library — a relation field, a wikilink, a bound repo — still names the
# folder (_unfinished_rename_blocker). Any other mismatch stays a
# `paper_dir_validity` finding for a person. `lit health-check --fix` runs it
# itself, before the derived rebuild (commands/health.py, _apply_fixes).
AUTO_FIXABLE_CATEGORIES: frozenset[str] = frozenset(
    {
        "stale_staging",
        "orphan_trash_sidecar",
        "discussion_scaffold",
        "skill_drift",
        "retired_priority",
        "dangling_refs",
        "supplementary_missing",
        "supplementary_unrecorded",
        "dirty_metadata",
        "unfinished_rename",
    }
)

# Key under which apply_autofix reports the (folder, reason) of each notes.md
# the dirty_metadata arm could not read, so its heading keeps the old title.
# Detail for the CLI to print, not a category.
DIRTY_NOTES_UNREADABLE = "dirty_metadata_notes_unreadable"
# Same, for the (id, clean-derived id) pairs of papers whose id came from the
# title that arm cleaned: the `lit rename` it leaves to the user.
DIRTY_RENAMES = "dirty_metadata_renames"
# Key under which `lit health-check --fix` reports the (folder, metadata id,
# why and what to do) of each folder finish_unfinished_renames could not move.
UNFINISHED_RENAME_LEFT = "unfinished_rename_left"

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
