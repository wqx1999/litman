"""The three drift-correction modes (M30 / ADR-015 §Decision).

ADR-015 separates *detection* (one tagged check registry) from *correction*
(three modes, chosen by a check's ``klass``):

* **regen** — klass A (derived↔truth). The derived artifact holds nothing not
  already in TRUTH, so the fix is to drop it and recompute. Lossless; any
  over-deletion self-heals on the next scan once a flaky mount returns.
* **resolve** — klass B-ext (truth↔external dir). litman cannot pick which
  side is right, so it prompts the user. Reuses the mount-safe bounded-stat
  (ADR-014) so a hung HPC mount never looks like a deleted directory.
* **annotate** — klass B-auth (truth↔authored prose). The target is
  hand-written notes / relation fields, NOT regenerable: only mark in place
  (``[[X]]`` → ``[[X]] (deleted)``), never delete the prose.

Phase 1 extracts these as standalone, unit-testable functions. They are NOT
yet wired into ``cli.py`` / ``health.py`` (that is Phase 2); the bespoke
prompt copies in ``commands/_drift.py`` stay in place and working until then.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import click

from litman.commands._drift import _default_tty_probe
from litman.core.checks import Issue
from litman.core.notes import annotate_deleted_wikilinks, enumerate_markdown_files
from litman.core import views

__all__ = ["regen", "regen_index_drop_ids", "resolve", "annotate"]


# ---------------------------------------------------------------------------
# regen — klass A (derived↔truth): recompute the derived artifact from TRUTH
# ---------------------------------------------------------------------------


def regen(vault: Path, issues: list[Issue] | None = None) -> dict[str, int]:
    """Recompute the derived artifacts (INDEX.json + views/ + project refs) from TRUTH.

    The derived artifacts are a pure function of the per-paper ``metadata.yaml``
    truth, so the correct repair for any klass-A drift is to drop them and
    rebuild wholesale. Lossless: nothing in INDEX/views/project-refs is not also
    in metadata. Covers all three klass-A pairs (ledger #1/#2/#3):

    * ``INDEX.json`` (#1) — ``write_index``.
    * ``views/by-*/`` (#2) — ``rebuild_views``.
    * project ``litman_reflib/`` symlinks + ``REFERENCES.md`` (#3) —
      ``rebuild_all_project_links`` + ``rebuild_all_project_refs``, but only for
      projects whose directory is reachable on this machine (unreachable ones
      are skipped, not an error — see ``project_path_exists`` / ADR-014).

    ``issues`` is accepted for a uniform corrector signature but unused —
    regen always rebuilds the full derived set rather than patching per-issue
    (cheaper to reason about and idempotent). Returns
    ``{"index": 1, "views": <n_symlinks>, "project_refs": <n_projects>}``.

    This is Tier-2 only: it calls ``list_papers`` (reads every metadata.yaml),
    so it MUST NOT run inside the Tier-1 hook (invariant #15). The hook uses
    :func:`regen_index_drop_ids` instead.
    """
    # Local import: document.list_papers pulls the heavier ruamel-typ machinery
    # that views/notes don't need, and keeps correctors importable without a
    # cycle through commands/.
    from litman.core.document import list_papers

    papers = list_papers(vault)
    views.write_index(vault, papers)
    view_counts = views.rebuild_views(vault, papers)

    # Project-side derived artifacts (#3). Only when projects are configured;
    # rebuild_all_* skip unreachable project dirs internally.
    from litman.core.config import load_config
    from litman.core.project_link import rebuild_all_project_links
    from litman.core.project_refs import rebuild_all_project_refs
    from litman.exceptions import ConfigError

    n_projects = 0
    # ONLY the config load is swallowed: a broken / unreadable lit-config.yaml
    # surfaces via `lit config show`, and check_project_references already
    # returns [] on a broken config — so there is no #3 drift to repair and we
    # skip the project-side rebuild entirely. The rebuild calls themselves are
    # NOT in this try: a genuine filesystem failure (permission error writing
    # REFERENCES.md, symlink failure on a reachable project dir) must propagate
    # so health.py:_apply_fixes does not falsely report "project_references: 1"
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
# resolve — klass B-ext (truth↔external dir): prompt; litman cannot pick a side
# ---------------------------------------------------------------------------


def resolve(
    issue: Issue,
    *,
    default_yes: bool,
    status: dict[str, bool | None] | None = None,
    paths: list[str] | None = None,
    prompt: str | None = None,
    stdin_is_tty: Callable[[], bool] | None = None,
    confirm_fn: Callable[..., bool] | None = None,
) -> bool:
    """Prompt the user to resolve a B-external drift; return True iff they consent.

    Check-agnostic generalization of the ``[Y/n]`` machinery currently
    duplicated in ``commands/_drift.py``. **Probe ownership is the caller's**:
    when a mount probe gates the prompt (``paths`` is given), the caller MUST
    also pass the bounded-stat ``status`` for those paths. ``resolve`` never
    runs its own :func:`_exists_bounded` — Phase 2 wires every B-external check
    through a single shared 0.5s bounded-stat budget (spec §7), so a corrector
    silently re-statting here would double-charge that budget and split the
    mount probe across two call sites. Passing the verdict in keeps the probe
    in exactly one place.

    The caller also supplies the destructive-default policy:

    * Destructive-but-lossless prunes (dangling registry entry) pass
      ``default_yes=True``.
    * Irreversible cascades (``project rm``) pass ``default_yes=False`` so a
      reflexive Enter never triggers them.

    Resolution rules:

    * If ``paths`` is given and *none* of the paths resolved (per ``status``)
      to a definite ``False`` (i.e. all ``True`` or ``None``/unknown), there is
      no confirmed drift to resolve → return ``False`` without prompting. A
      ``None`` from a slow / dropped mount must never drive a destructive
      prompt (ADR-014).
    * Non-TTY → never prompt, never mutate: return ``False`` (the caller's
      automation path reports to stderr and defers to an explicit fix command).
    * TTY → ask ``[Y/n]`` (default per ``default_yes``); return the answer.

    Args:
        issue: The drift finding (its ``message`` seeds the default prompt).
        default_yes: Default for the confirm; ``True`` = destructive-lossless.
        status: Bounded-stat result (from :func:`_exists_bounded`) keyed by
            path. Required whenever ``paths`` is given — ``resolve`` does not
            probe the filesystem itself.
        paths: Paths whose ``status`` must show a definite ``False`` for the
            drift to count as confirmed. Omit ``paths`` (and ``status``) to
            skip the confirmation gate (caller already established the drift).
        prompt: Override the prompt text (defaults to a generic resolve line).
        stdin_is_tty: TTY-probe indirection for tests.
        confirm_fn: ``click.confirm`` indirection for tests.

    Raises:
        ValueError: if ``paths`` is given without a corresponding ``status``
            (probe ownership is the caller's — see above).
    """
    probe = stdin_is_tty or _default_tty_probe
    confirm = confirm_fn or click.confirm

    if paths is not None:
        if status is None:
            raise ValueError(
                "resolve(paths=...) requires status=...: the caller owns the "
                "bounded-stat probe (shared 0.5s budget); resolve never stats."
            )
        if not any(status.get(p) is False for p in paths):
            # No path is a confirmed absence — unknown (None) / present (True)
            # is not actionable drift.
            return False

    if not probe():
        # Automation: never mutate without consent. Caller reports + defers.
        return False

    text = prompt or f"{issue.message} — resolve now?"
    return bool(confirm(text, default=default_yes))


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
    for md_path in enumerate_markdown_files(vault):
        try:
            original = md_path.read_text(encoding="utf-8")
        except OSError:
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
    return n_touched
