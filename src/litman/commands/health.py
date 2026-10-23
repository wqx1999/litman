"""``lit health-check`` — vault-wide consistency probe.

Surfaces problems that the schema-less ``lit add`` / ``lit modify`` / ``lit
rename`` / ``lit rm`` flow can leave behind: schema gaps, dangling references,
half-finished renames, stale staging dirs, etc. See :mod:`litman.core.checks`
for the per-check semantics.

Each category lists its first few findings and folds the rest into a count;
``--all`` prints every one. See ``_CATEGORY_PREVIEW`` for why the cap is a
display decision and never a check-layer one.

The CLI is read-only by default. ``--fix`` auto-regenerates every derived
(klass-A) artifact — lossless recompute from TRUTH — plus the legacy
validity auto-fixes (stale staging dirs + orphan trash sidecars) and the
arms ``checks.AUTO_FIXABLE_CATEGORIES`` has grown since, among them removing
relation edges that name a paper no longer in the library. klass-B
drift (registry / project / taxonomy / code-clone) needs per-case user
judgment and stays report-only — ``--fix`` never picks a side (ADR-015).

Exit code: ``1`` if any error or warning is found (so CI / cron can gate on
it), ``0`` if the vault is clean. ``info`` findings never gate — see the
command docstring below for why.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.console import Console
from rich.markup import escape

from litman.commands._hub_report import hub_settlement_lines
from litman.commands._options import library_option, vault_option
from litman.core import confirm as confirm_mod
from litman.core.checks import (
    AUTO_FIXABLE_CATEGORIES,
    DIRTY_NOTES_KEPT,
    DIRTY_RENAMES,
    UNFINISHED_RENAME_LEFT,
    Issue,
    apply_autofix,
    finish_unfinished_renames,
    group_by_category,
    klass_a_checks,
    run_all_checks,
)
from litman.core.correctors import regen
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.locking import ensure_truth_locked

if TYPE_CHECKING:
    from litman.core.supplementary import SiFixPolicy

console = Console()

# Categories whose findings ``--fix`` auto-regenerates (klass A, lossless).
# Derived from the tagged registry so it cannot drift from the ledger; the
# legacy validity auto-fixes (stale_staging, orphan_trash_sidecar) are added
# in ``_fixable_categories`` below.
_KLASS_A_CATEGORIES: frozenset[str] = frozenset(
    spec.category for spec in klass_a_checks()
)


def _fixable_categories() -> frozenset[str]:
    """All categories ``--fix`` will clean: klass-A regen + legacy validity.

    ``AUTO_FIXABLE_CATEGORIES`` keeps the two historical validity fixes
    (stale_staging roll-back/forward + orphan_trash_sidecar removal) wired
    through :func:`apply_autofix`; the klass-A set is the M30 broadening to
    lossless regen of every derived artifact.
    """
    return _KLASS_A_CATEGORIES | AUTO_FIXABLE_CATEGORIES

# Issues listed per category before the rest fold into a count. A vault that
# predates a guard can hold hundreds of one kind — the `placeholder_metadata`
# / `placeholder_id` pair on a library imported before either existed is the
# case that forced this — and printing every one buries the other categories
# under a screen of identical yellow. Folding is a display decision only: the
# checks still report everything, the per-category count and the summary are
# still exact, and `--all` prints the lot. Suppressing at the *check* layer
# instead would mean the vault has problems litman deliberately never
# mentions, which is the opposite of what a health check is for.
#
# Five, matching `lit rename`'s back-reference list — the same "enough to see
# the pattern, not enough to scroll" judgment, and no second number to explain.
_CATEGORY_PREVIEW = 5

# Severity ordering for sort within a category and visual styling.
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
_SEVERITY_STYLE = {
    "error": "bold red",
    "warning": "yellow",
    "info": "cyan",
}

# Pretty headers for each category. New checks added to the registry should
# get a header here too — fall back to the raw category name otherwise.
_CATEGORY_HEADERS: dict[str, str] = {
    "schema": "Schema (required fields + fixed enums)",
    "retired_priority": "Retired field (paper-level priority)",
    "placeholder_metadata": "Filler metadata (title / authors hold a placeholder)",
    "placeholder_id": "Filler inside a paper id (folder name, wikilink, cite key)",
    "weak_id_keyword": "Uninformative id keyword (and a title that cannot fix it)",
    "dirty_metadata": (
        "Line breaks, stray spaces or markup in title / journal / authors"
    ),
    "paper_dir_validity": (
        "Paper directory integrity (dir name / parseable metadata / id / paper.pdf)"
    ),
    "unfinished_rename": "Half-finished renames (folder name vs metadata id)",
    "duplicate_doi": "Duplicate DOIs (--paper-doi lookups become ambiguous)",
    "discussion_scaffold": "Discussion log scaffold (discussion.md + its format header)",
    "index_vs_disk": "INDEX.json vs papers/ on disk",
    "views_vs_metadata": "Views (by-*/) vs metadata",
    "project_references": "Project REFERENCES.md / litman_reflib / litman_code vs membership",
    "project_bridge_dangling": (
        "Project bridge links (litman_reflib / litman_code) resolve to a live target"
    ),
    "links_unsupported": "Folder links (views/ + project shortcuts)",
    "dangling_refs": "Dangling references (related/contradicts/extends + reverse)",
    "dangling_wikilinks": "Dangling [[id]] wikilinks in notes",
    "relevance_orphan": "Orphan relevance-<project> annotations",
    "priority_orphan": "Orphan priority-<project> grades",
    "taxonomy_drift": "Taxonomy drift (unregistered values)",
    "taxonomy_similar": "Look-alike vocabulary values (topics / methods / data)",
    "taxonomy_unused": "Vocabulary values no paper uses",
    "taxonomy_singletons": "Vocabularies mostly made of values used by one paper",
    "project_config_consistency": (
        "Project registry consistency (TAXONOMY.md vs lit-config.yaml)"
    ),
    "project_path_exists": "Project path existence (config paths on disk)",
    "bidirectional_refs": "Bidirectional 'related' asymmetry",
    "inbox_staleness": "Inbox staleness (>14 days)",
    "stale_staging": ".litman-staging/ leftovers",
    "orphan_trash_sidecar": "Orphan .trash/ sidecars",
    "trash_size": "Trash bloat (entry count)",
    "replaced_folders": "Folders kept out of project hubs",
    "pdf_viewer": "PDF viewer availability (for `lit open`)",
    "skill_drift": "Agent skill freshness (installed vs bundled)",
    "code_clone_integrity": "Code clone integrity (clones vs metadata refs)",
    "vault_registry_drift": "Vault registry drift (registered path missing)",
    "supplementary_field": "SI records (the supplementary field in metadata.yaml)",
    "supplementary_missing": "SI files recorded but missing",
    "supplementary_changed": "SI files whose content changed",
    "supplementary_unrecorded": "Files in supplementary/ with no record",
    "supplementary_layout": "SI folder layout litman will not change (links, files, folders)",
    "supplementary_unchecked": "SI litman cannot check (unreadable, or no checksum on record)",
    "supplementary_cited": "SI citations in notes / discussion that no longer match",
}


def _render_issue_line(issue: Issue, max_msg_width: int = 100) -> str:
    style = _SEVERITY_STYLE.get(issue.severity, "white")
    badge = f"[{style}]{issue.severity:>7}[/]"
    where = f"[bold]{escape(issue.paper_id)}[/]" if issue.paper_id else "[dim]<vault>[/]"
    msg = escape(issue.message)
    line = f"  {badge}  {where}  {msg}"
    if issue.hint:
        line += f"\n           [dim]→ {escape(issue.hint)}[/]"
    return line


def _render_report(issues: list[Issue], show_all: bool = False) -> None:
    if not issues:
        return
    grouped = group_by_category(issues)
    fixable = _fixable_categories()
    for category, items in grouped.items():
        header = _CATEGORY_HEADERS.get(category, category)
        n = len(items)
        fixable_marker = (
            " [dim](fixable via --fix)[/]"
            if category in fixable
            else ""
        )
        console.print(
            f"\n[bold]{escape(header)}[/] "
            f"[dim]({n} issue{'s' if n != 1 else ''})[/]"
            f"{fixable_marker}"
        )
        items_sorted = sorted(
            items,
            key=lambda i: (
                _SEVERITY_RANK.get(i.severity, 99),
                i.paper_id or "",
            ),
        )
        shown = items_sorted if show_all else items_sorted[:_CATEGORY_PREVIEW]
        for issue in shown:
            # soft_wrap: several hints are a complete `lit rename <old> <new>`
            # meant to be copied straight out of the terminal, and rich's own
            # wrapping puts a hard newline inside the command at 80 columns.
            # Letting the terminal wrap instead keeps the copied text one line.
            console.print(_render_issue_line(issue), soft_wrap=True)
        hidden = n - len(shown)
        if hidden:
            console.print(
                f"  [dim]… and {hidden} more in this category. "
                f"`lit health-check --all` lists every one.[/]"
            )


def _summarize(issues: list[Issue], n_papers: int) -> None:
    if not issues:
        console.print(
            f"\n[bold green]✓ All checks passed[/] "
            f"[dim]({n_papers} paper{'s' if n_papers != 1 else ''}, "
            "no issues found)[/]"
        )
        return

    n_err = sum(1 for i in issues if i.severity == "error")
    n_warn = sum(1 for i in issues if i.severity == "warning")
    n_info = sum(1 for i in issues if i.severity == "info")
    fixable = _fixable_categories()
    n_fixable = sum(1 for i in issues if i.category in fixable)
    console.print(
        f"\n[bold]Summary:[/] "
        f"{len(issues)} issue{'s' if len(issues) != 1 else ''} across "
        f"{len(group_by_category(issues))} categor"
        f"{'y' if len(group_by_category(issues)) == 1 else 'ies'} "
        f"[dim](errors: {n_err}, warnings: {n_warn}, info: {n_info})[/]"
    )
    if n_fixable:
        console.print(
            f"[dim]Tip: {n_fixable} issue{'s' if n_fixable != 1 else ''} "
            f"can be auto-cleaned with `lit health-check --fix`.[/]"
        )


@click.command("health-check")
@click.option(
    "--fix",
    "do_fix",
    is_flag=True,
    default=False,
    help=(
        "Auto-regenerate all derived artifacts from metadata, clean stale "
        "staging dirs / orphan trash sidecars, refresh stale installed agent "
        "skills, rename a paper folder to the id in its metadata.yaml when a "
        "rename stopped halfway, migrate the retired paper-level `priority` onto "
        "priority-<project> (a paper in no project loses it), drop relation "
        "links to papers no longer in the library (deleted, lost, or in the "
        "trash), rewrite titles, journal names and authors that carry line "
        "breaks, stray spaces or markup (and the notes.md heading lit add "
        "wrote from the title), record files found in a paper's "
        "supplementary/ folder once "
        "they check out and drop the records of SI files that are gone (both "
        "ask first at a terminal), "
        "and put links back where a copied project folder left real "
        "folders (one that does "
        "not match the vault is kept under .trash/replaced-folders/). "
        "Registry / project / code-clone drift and every taxonomy finding stay "
        "report-only (each needs a per-case decision; --fix never picks a side)."
    ),
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help=(
        f"List every finding. Without it each category shows its first "
        f"{_CATEGORY_PREVIEW} and folds the rest into a count — pass this "
        "when working through a category paper by paper, or when an agent "
        "needs the full list to act on."
    ),
)
@library_option
@vault_option
def health_check_cmd(
    do_fix: bool, show_all: bool, library: Path | None, vault_name: str | None
) -> None:
    """Run vault-wide consistency checks.

    Exits 0 on a clean vault, 1 if any error or warning is found (so the command
    can gate cron / CI tasks). With --fix the exit code reflects post-fix state —
    if every issue was in a fixable category, the second pass is clean and the
    command exits 0.

    ``info`` findings do NOT gate the exit code. An info is by definition
    advisory: it names something the user may want to know, not something wrong
    with the library. The case that forced the distinction is
    ``links_unsupported`` — a vault on a drive that cannot hold folder links
    (FAT32 / exFAT, network shares) is perfectly healthy, it simply cannot be
    decorated with views/ and project shortcuts, and exiting 1 forever over
    that would be telling the user their library is broken when it is not.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    papers = list_papers(vault)
    n_papers = len(papers)
    console.print(
        f"[dim]Running health checks on vault:[/] {escape(str(vault))} "
        f"[dim]({n_papers} paper{'s' if n_papers != 1 else ''})[/]"
    )

    # Tier-2 re-lock backstop (M32, ADR-015 prevention arm): re-assert the
    # read-only lock on any TRUTH file found writable (legacy-vault first run /
    # post-crash / post-pull bypass). Regen-style validity correction — runs
    # unconditionally (not gated on --fix; over-locking is lossless and never
    # touches content). Stat-only sweep, so it respects invariant #15.
    n_relocked = ensure_truth_locked(vault)
    if n_relocked:
        console.print(
            f"[green]✓[/] Re-locked {n_relocked} TRUTH file"
            f"{'s' if n_relocked != 1 else ''} "
            f"[dim](metadata.yaml / TAXONOMY.md / paper.pdf / SI files made "
            "read-only)[/]"
        )

    issues = run_all_checks(vault, papers)
    _render_report(issues, show_all=show_all)

    # --fix runs even with a clean report: the fixable set is wider than the
    # report (the skill sweep refreshes stale copies in non-default agent
    # directories the check deliberately does not probe). With nothing to
    # do, _apply_fixes returns {} and this block is output-silent.
    if do_fix:
        applied = _apply_fixes(vault, issues)
        if applied:
            console.print("\n[bold]Auto-fix:[/]")
            hub_reported = False
            for cat, n in applied.items():
                # hub_* carry detail about the project-link rebuild, not a
                # per-category count; they render under project_references.
                # The kept notes.md and rename lists render under
                # dirty_metadata, the folders left as they were under
                # unfinished_rename.
                if cat.startswith("hub_") or cat in (
                    DIRTY_NOTES_KEPT,
                    DIRTY_RENAMES,
                    UNFINISHED_RENAME_LEFT,
                ):
                    continue
                if n > 0:
                    console.print(
                        f"  [green]✓[/] {escape(cat)}: cleaned {n} item"
                        f"{'s' if n != 1 else ''}"
                    )
                if cat == "project_references":
                    _report_hub_settlements(applied)
                    hub_reported = True
                if cat == "dirty_metadata":
                    _report_notes_kept(applied)
                    _report_renames(applied)
                if cat == "unfinished_rename":
                    _report_renames_left(applied)
            if not hub_reported:
                # The rebuild can settle hub folders while some OTHER klass-A
                # category is what fired it.
                _report_hub_settlements(applied)
            # Re-run checks so the post-fix summary is honest.
            papers = list_papers(vault)
            issues = run_all_checks(vault, papers)
            n_papers = len(papers)

    _summarize(issues, n_papers)

    # Refresh the active vault's last_health_check_at ON SUCCESS, REGARDLESS of
    # findings (M30 §5: the nudge means "you haven't *looked* in 2 weeks", not
    # "your library is dirty"). Only when the resolved vault IS the active
    # registered vault — a `--library` / `--vault` override to an unregistered
    # path must NOT refresh (and has no registry entry to refresh anyway).
    _refresh_active_health_check_timestamp(vault)

    # Errors and warnings gate; info does not. See the command docstring — an
    # info-only run means "here is something to know", not "your library is
    # damaged", and a cron/CI gate that fires on it is a false alarm.
    if any(i.severity != "info" for i in issues):
        sys.exit(1)


def _refresh_active_health_check_timestamp(vault: Path) -> None:
    """Stamp ``last_health_check_at`` on the active registry entry if it matches.

    Best-effort: a registry write failure must not turn a successful
    health-check into a crash. If it cannot write, the next run simply nudges
    again — nothing silently wrong (no state was claimed to change).
    """
    from datetime import datetime, timezone

    from litman.core.vault_registry import (
        VaultRegistryError,
        find_active,
        load_registry,
        mark_health_checked,
        save_registry,
    )

    try:
        reg = load_registry()
        active = find_active(reg)
        if active is None:
            return
        if Path(active.path).resolve() != Path(vault).resolve():
            return
        ts = datetime.now(timezone.utc).isoformat()
        save_registry(mark_health_checked(reg, active.name, ts))
    except (VaultRegistryError, OSError):
        pass


def _report_hub_settlements(applied: dict[str, Any]) -> None:
    """Say what the rebuild did with hub positions real folders were sitting in.

    Reported under ``project_references`` rather than as its own category: it
    is one rebuild, and the folders were never a separate finding.
    """
    replaced = applied.get("hub_replaced_copies")
    kept = applied.get("hub_moved_aside")
    for line in hub_settlement_lines(
        replaced if isinstance(replaced, int) else 0,
        [str(p) for p in kept] if isinstance(kept, list) else [],
    ):
        console.print(f"    {line}", soft_wrap=True)


def _report_notes_kept(applied: dict[str, Any]) -> None:
    """Name each paper whose notes.md heading the metadata cleanup left alone.

    Its title is clean in metadata.yaml now, but notes.md no longer opens with
    the heading ``lit add`` wrote, so that line is the user's to change.
    """
    kept = applied.get(DIRTY_NOTES_KEPT)
    for folder in kept if isinstance(kept, list) else []:
        console.print(
            f"    [yellow]kept[/] {escape(folder)}: notes.md heading differs "
            "from the one lit add wrote, left as is",
            soft_wrap=True,
        )


def _report_renames(applied: dict[str, Any]) -> None:
    """Give the ``lit rename`` for each id made from a title the fix cleaned.

    The report's finding said so too, but it can sit past the per-category
    fold, and once the title is clean no later run can tell.
    """
    renames = applied.get(DIRTY_RENAMES)
    for pid, target in renames if isinstance(renames, list) else []:
        console.print(
            f"    [yellow]rename[/] {escape(pid)}: `lit rename {escape(pid)} "
            f"{escape(target)}` if you want the id to match the clean title",
            soft_wrap=True,
        )


def _report_renames_left(applied: dict[str, Any]) -> None:
    """Name each folder the ``unfinished_rename`` arm could not rename.

    The finding stays in the report after the fix, but the reason the move
    failed is known only now.
    """
    left = applied.get(UNFINISHED_RENAME_LEFT)
    for folder, target, reason in left if isinstance(left, list) else []:
        console.print(
            f"    [yellow]left[/] papers/{escape(folder)}/: could not rename it "
            f"to papers/{escape(target)}/ ({escape(reason)}); close any file "
            "open from it and run --fix again",
            soft_wrap=True,
        )


def _si_fix_policy() -> SiFixPolicy:
    """How ``--fix`` settles the SI cases it must not decide alone.

    At a terminal it asks: whether to drop the records of SI files that are
    gone (default yes — the file is not there) and whether to record a file it
    cannot confirm belongs to the paper (default no). With nobody to ask it
    does neither and prints the command that would.
    """
    from litman.core.supplementary import SiFixPolicy, shell_path, si_stem

    interactive = confirm_mod._stdin_is_tty()

    def confirm_drop(paper_id: str, names: list[str]) -> bool:
        listed = ", ".join(names)
        if interactive:
            return click.confirm(
                f"{paper_id}: {listed} recorded but missing. "
                "Drop the record(s)?",
                default=True,
            )
        flags = " ".join(f"--si {si_stem(name)}" for name in names)
        record, them = ("record", "it") if len(names) == 1 else ("records", "them")
        console.print(
            f"  [yellow]kept[/] {escape(paper_id)}: {record} of missing "
            f"{escape(listed)} [dim](drop {them}: `lit rm {escape(paper_id)} "
            f"{flags} --yes`)[/]",
            soft_wrap=True,
        )
        return False

    def confirm_unverified(paper_id: str, path: Path, verdict: Any) -> bool:
        if interactive:
            return click.confirm(
                f"{paper_id}: can't confirm {path.name} belongs to it. "
                "Record it anyway?",
                default=False,
            )
        command = f"lit add --si {paper_id} {shell_path(path)} --force"
        console.print(
            f"  [yellow]left[/] {escape(str(path))}: can't confirm it belongs "
            f"to {escape(paper_id)} [dim](record it: `{escape(command)}`)[/]",
            soft_wrap=True,
        )
        return False

    def on_rejected(paper_id: str, path: Path, verdict: Any) -> None:
        console.print(
            f"  [yellow]left[/] {escape(str(path))}: {escape(verdict.reason)} "
            "[dim](move it out of supplementary/)[/]",
            soft_wrap=True,
        )

    return SiFixPolicy(confirm_drop, confirm_unverified, on_rejected)


def _apply_fixes(vault: Path, issues: list[Issue]) -> dict[str, Any]:
    """Auto-fix the fixable subset: klass-A regen + legacy validity cleanups.

    Two correction paths (ADR-015):

    * **klass-A regen** — any klass-A category present (derived↔truth drift)
      triggers a single full ``regen`` (drop INDEX.json + views, recompute from
      metadata). Lossless. Reported under each fired klass-A category for
      transparency. A half-renamed paper's folder is renamed before it
      (``unfinished_rename``), and a folder that moved triggers the regen too.
    * **validity** — ``stale_staging`` roll-back/forward and
      ``orphan_trash_sidecar`` removal stay routed through
      :func:`apply_autofix`, unchanged, alongside the scaffold / skill / retired
      -field and dangling-edge arms it has grown since. All lossless except
      those two; see ``checks.AUTO_FIXABLE_CATEGORIES`` for why they are in.

    klass-B drift (registry / project / taxonomy / code-clone) is never fixed
    here — it needs a per-case user decision (the Tier-1 ``resolve`` prompt or
    an explicit ``lit`` command). Returns ``{category: n_fixed}`` plus, when a
    klass-A regen ran, ``hub_replaced_copies`` / ``hub_moved_aside`` — detail
    about the project-link rebuild, not categories — and, from the
    ``dirty_metadata`` arm, ``dirty_metadata_notes_kept`` /
    ``dirty_metadata_renames`` (``checks.DIRTY_NOTES_KEPT`` /
    ``checks.DIRTY_RENAMES``) when it has notes.md files or ids to name, and
    ``unfinished_rename_left`` (``checks.UNFINISHED_RENAME_LEFT``) when a
    folder could not be renamed.
    """
    counts: dict[str, Any] = {}

    # Folders first. Until a half-renamed paper's folder carries its metadata
    # id, the rebuild links views and project hubs to a folder that is not
    # there — on Windows that fails and reads as "cannot hold folder links".
    moved = 0
    if any(i.category == "unfinished_rename" for i in issues):
        moved, left = finish_unfinished_renames(vault)
        counts["unfinished_rename"] = moved
        if left:
            counts[UNFINISHED_RENAME_LEFT] = left
        issues = [i for i in issues if i.category != "unfinished_rename"]

    klass_a_present = {
        i.category for i in issues if i.category in _KLASS_A_CATEGORIES
    }
    if klass_a_present or moved:
        regenerated = regen(vault, issues)
        # A regen is a single wholesale rebuild; attribute one cleaned unit to
        # each fired klass-A category so the report names what was healed.
        for cat in klass_a_present:
            counts[cat] = 1
        # Passed through, not counted: how many hub folder copies the rebuild
        # deleted, and which differing ones it kept. core does not print.
        for key in ("hub_replaced_copies", "hub_moved_aside"):
            if key in regenerated:
                counts[key] = regenerated[key]

    counts.update(apply_autofix(vault, issues, si_policy=_si_fix_policy()))
    return counts
