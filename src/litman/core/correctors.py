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

__all__ = ["regen", "resolve", "annotate"]


# ---------------------------------------------------------------------------
# regen — klass A (derived↔truth): recompute the derived artifact from TRUTH
# ---------------------------------------------------------------------------


def regen(vault: Path, issues: list[Issue] | None = None) -> dict[str, int]:
    """Recompute the derived artifacts (``INDEX.json`` + ``views/by-*/``) from TRUTH.

    The derived artifacts are a pure function of the per-paper ``metadata.yaml``
    truth, so the correct repair for any klass-A drift is to drop them and
    rebuild wholesale. Lossless: nothing in INDEX/views is not also in metadata.

    ``issues`` is accepted for a uniform corrector signature but unused —
    regen always rebuilds the full derived set rather than patching per-issue
    (cheaper to reason about and idempotent). Returns
    ``{"index": 1, "views": <n_symlinks>}`` for caller-side reporting.
    """
    # Local import: document.list_papers pulls the heavier ruamel-typ machinery
    # that views/notes don't need, and keeps correctors importable without a
    # cycle through commands/.
    from litman.core.document import list_papers

    papers = list_papers(vault)
    views.write_index(vault, papers)
    view_counts = views.rebuild_views(vault, papers)
    return {"index": 1, "views": sum(view_counts.values())}


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
        targeted: Phase-1 flag for the Tier-1 vanished-id path (§6). When the
            id set is small the annotate is the same in-place rewrite either
            way; the flag is accepted so the Tier-1 hook can pass it
            explicitly. (A true grep-narrowed scan is a Phase-3 optimization;
            in Phase 1 both branches enumerate the same scope, and the wikilink
            rewrite is a no-op on files that don't mention the id.)
    """
    if not deleted_ids:
        return 0

    n_touched = 0
    for md_path in enumerate_markdown_files(vault):
        try:
            original = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = original
        for deleted_id in deleted_ids:
            updated = annotate_deleted_wikilinks(updated, deleted_id)
        if updated != original:
            md_path.write_text(updated, encoding="utf-8")
            n_touched += 1
    return n_touched
