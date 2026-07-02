"""The three drift-correction modes (M30 / ADR-015 §Decision).

ADR-015 separates *detection* (one tagged check registry) from *correction*
(three modes, chosen by a check's ``klass``):

* **regen** — klass A (derived↔truth). The derived artifact holds nothing not
  already in TRUTH, so the fix is to drop it and recompute. Lossless; any
  over-deletion self-heals on the next scan once a flaky mount returns.
* **resolve** — klass B-ext (truth↔external dir). litman cannot pick which
  side is right, so it prompts the user, reusing the mount-safe bounded-stat
  (ADR-014) so a hung HPC mount never looks like a deleted directory. This
  mode is NOT implemented in this module: the bespoke ``[Y/n]`` prompts live
  in ``commands/_drift.py`` (a generic ``resolve()`` extraction was prototyped
  and then dropped as a duplicate of that working machinery).
* **annotate** — klass B-auth (truth↔authored prose). The target is
  hand-written notes / relation fields, NOT regenerable: only mark in place
  (``[[X]]`` → ``[[X]] (deleted)``), never delete the prose.

``regen`` / ``regen_index_drop_ids`` / ``annotate`` are wired into ``cli.py``
/ ``health.py``; B-external resolution stays in ``commands/_drift.py``.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from litman.core import views
from litman.core.checks import Issue
from litman.core.notes import annotate_deleted_wikilinks, enumerate_markdown_files

__all__ = [
    "reconcile_derived",
    "regen",
    "regen_index_drop_ids",
    "annotate",
]


# ---------------------------------------------------------------------------
# reconcile_derived — the single TRUTH→DERIVED rebuild path (M30 Phase 4)
# ---------------------------------------------------------------------------


def reconcile_derived(
    vault: Path,
    *,
    papers: list[dict] | None = None,
    project_refs: bool = True,
) -> dict[str, int]:
    """Recompute the derived artifacts (INDEX.json + views/ [+ project refs]) from TRUTH.

    **The one shared rebuild path** (M30 Phase 4 / ADR-015 §Decision). Both the
    klass-A ``regen`` corrector (``lit health-check --fix``, ledger #1/#2/#3)
    and every write command's post-commit derived rebuild funnel through here,
    so INDEX and views are *always* regenerated together — a command can never
    rewrite one and forget the other.

    The derived artifacts are a pure function of the per-paper ``metadata.yaml``
    truth, so the correct rebuild is to recompute them wholesale. Lossless:
    nothing in INDEX/views/project-refs is not also in metadata.

    * ``INDEX.json`` (#1) — ``write_index`` (always).
    * ``views/by-*/`` (#2) — ``rebuild_views`` (always; coupled to INDEX so the
      two never diverge).
    * project ``litman_reflib/`` symlinks + ``REFERENCES.md`` (#3) — rebuilt
      only when ``project_refs=True`` (the full klass-A set). Write commands
      that already own a narrower project-side update (``project rm``'s targeted
      teardown) or do not touch the ``projects`` field at all pass
      ``project_refs=False`` to stay behavior-preserving — the funnel unifies
      the INDEX↔views coupling without widening what each command does.

    Args:
        vault: Vault root.
        papers: Already-loaded paper list to reuse (perf: a write command that
            spliced its in-memory diff into ``list_papers`` output passes it in
            to avoid a redundant re-read). ``None`` → load fresh from disk
            (the ``--fix`` / corrector path, which always rebuilds from truth).
        project_refs: When ``True`` (default), also rebuild every configured
            project's symlinks + ``REFERENCES.md``.

    Returns ``{"index": 1, "views": <n_symlinks>, "project_refs": <n_projects>}``.

    Tier-2 only when ``papers is None``: it calls ``list_papers`` (reads every
    metadata.yaml), so it MUST NOT run inside the Tier-1 hook (invariant #15).
    The hook uses :func:`regen_index_drop_ids` instead.
    """
    # Local import: document.list_papers pulls the heavier ruamel-typ machinery
    # that views/notes don't need, and keeps correctors importable without a
    # cycle through commands/.
    if papers is None:
        from litman.core.document import list_papers

        papers = list_papers(vault)

    views.write_index(vault, papers)
    view_counts = views.rebuild_views(vault, papers)

    n_projects = 0
    if project_refs:
        # Project-side derived artifacts (#3). Only when projects are
        # configured; rebuild_all_* skip unreachable project dirs internally.
        from litman.core.config import load_config
        from litman.core.project_link import rebuild_all_project_links
        from litman.core.project_refs import rebuild_all_project_refs
        from litman.exceptions import ConfigError

        # ONLY the config load is swallowed: a broken / unreadable
        # lit-config.yaml surfaces via `lit config show`, and
        # check_project_references already returns [] on a broken config — so
        # there is no #3 drift to repair and we skip the project-side rebuild
        # entirely. The rebuild calls themselves are NOT in this try: a genuine
        # filesystem failure (permission error writing REFERENCES.md, symlink
        # failure on a reachable project dir) must propagate so
        # health.py:_apply_fixes does not falsely report "project_references: 1"
        # for a repair that actually failed (invariant #14 — no silent-skip).
        try:
            config = load_config(vault)
        except ConfigError:
            config = None
        if config is not None:
            projects = dict(config.projects)
            if projects:
                rebuild_all_project_links(vault, projects)
                rebuild_all_project_refs(vault, projects)
                n_projects = len(projects)

    return {
        "index": 1,
        "views": sum(view_counts.values()),
        "project_refs": n_projects,
    }


# ---------------------------------------------------------------------------
# regen — klass A (derived↔truth): recompute the derived artifact from TRUTH
# ---------------------------------------------------------------------------


def regen(vault: Path, issues: list[Issue] | None = None) -> dict[str, int]:
    """Recompute the full derived set (INDEX + views + project refs) from TRUTH.

    The ``lit health-check --fix`` klass-A corrector (ledger #1/#2/#3). A thin
    wrapper over :func:`reconcile_derived` (the shared rebuild path) so the
    ``--fix`` corrector and write commands can never diverge on how a derived
    artifact is rebuilt — that single-path guarantee is the whole point of
    M30 Phase 4.

    ``issues`` is accepted for a uniform corrector signature but unused — regen
    always rebuilds the full derived set rather than patching per-issue (cheaper
    to reason about and idempotent).
    """
    return reconcile_derived(vault, project_refs=True)


def regen_index_drop_ids(vault: Path, dead_ids: list[str]) -> int:
    """Metadata-free klass-A INDEX repair: drop vanished ids from ``INDEX.json``.

    The Tier-1 per-command hook (spec §6) repairs an ``INDEX.json`` ↔
    ``papers/`` drift caused by a manual ``rm`` of a paper directory. The full
    :func:`regen` rebuilds INDEX from every ``metadata.yaml`` via
    ``list_papers`` — that violates invariant #15 (Tier 1 never reads per-paper
    metadata). This helper instead edits the existing INDEX in place: it reads
    only ``INDEX.json``, removes the entries whose id is in ``dead_ids``, and
    rewrites the file (delegating to
    :func:`litman.core.views.rewrite_index_dropping_ids`). No ``metadata.yaml``
    is opened, no ``list_papers`` call — safe on the hot path.

    Lossless: INDEX is a derived projection, so dropping a dead entry can only
    over-prune if the paper actually still exists, and the next full scan
    (``lit health-check`` / a write command's regen) re-adds it from truth.

    Returns the number of INDEX entries dropped.
    """
    if not dead_ids:
        return 0
    return views.rewrite_index_dropping_ids(vault, set(dead_ids))


# ---------------------------------------------------------------------------
# annotate — klass B-auth (truth↔authored prose): mark in place, never delete
# ---------------------------------------------------------------------------


def annotate(
    vault: Path, deleted_ids: list[str], *, targeted: bool = False
) -> int:
    """Mark same-vault ``[[id]]`` links to deleted papers as ``[[id]] (deleted)``.

    Wraps :func:`litman.core.notes.annotate_deleted_wikilinks`, applying it to
    every note in the wikilink scope for each id in ``deleted_ids``. Authored
    prose is only annotated in place, never rewritten or deleted (klass B-auth,
    ADR-015). Returns the number of files whose content actually changed.

    Args:
        vault: Vault root.
        deleted_ids: Paper ids whose links should be tagged ``(deleted)``.
        targeted: Tier-1 vanished-id path (§6). When ``True``, the scan is
            grep-narrowed: a note's full ``annotate_deleted_wikilinks`` rewrite
            (parse every ``[[...]]``, resolve each target) only runs on files
            whose raw text contains at least one of the ids as a substring.
            Files that never mention any vanished id are skipped after a single
            cheap ``in`` test, so the Tier-1 hook does not pay the full wikilink
            parse over every note in the vault for a one-paper deletion. The
            untargeted path (``False``, used by ``lit health-check``) runs the
            full rewrite on every note, which is equivalent (the rewrite is a
            no-op on files that don't mention the id) but does the parse work
            unconditionally.
    """
    if not deleted_ids:
        return 0

    n_touched = 0
    skipped: list[Path] = []
    for md_path in enumerate_markdown_files(vault):
        try:
            original = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # No silent-skip (invariant #14 / review A2): a note we cannot read
            # (permissions, or non-UTF-8 — a UnicodeDecodeError that the old
            # `except OSError` did not even catch, so it crashed the hook) may
            # still hold a [[deleted_id]] that check_dangling_wikilinks flagged.
            # Dropping it quietly would let the caller claim every dangling link
            # was annotated. Record + warn so the user knows this file was not.
            skipped.append(md_path)
            continue
        if targeted and not any(did in original for did in deleted_ids):
            # Grep-narrow: no vanished id appears as a substring, so no
            # ``[[id]]`` can resolve to one — skip the full parse entirely.
            continue
        updated = original
        for deleted_id in deleted_ids:
            updated = annotate_deleted_wikilinks(updated, deleted_id)
        if updated != original:
            md_path.write_text(updated, encoding="utf-8")
            n_touched += 1
    if skipped:
        err = Console(stderr=True)
        joined = ", ".join(str(p) for p in skipped)
        err.print(
            f"[yellow]warning:[/] could not read {len(skipped)} note file(s) "
            f"while annotating deleted links — left unchanged (a [[id]] (deleted)"
            f" tag may be missing there): {joined}"
        )
    return n_touched
