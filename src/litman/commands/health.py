"""``lit health-check`` — vault-wide consistency probe.

Surfaces problems that the schema-less ``lit add`` / ``lit modify`` / ``lit
rename`` / ``lit rm`` flow can leave behind: schema gaps, dangling references,
half-finished renames, stale staging dirs, etc. See :mod:`litman.core.checks`
for the per-check semantics.

The CLI is read-only by default. ``--fix`` auto-regenerates every derived
(klass-A) artifact — lossless recompute from TRUTH — plus the legacy
validity auto-fixes (stale staging dirs + orphan trash sidecars). klass-B
drift (registry / project / taxonomy / code-clone) needs per-case user
judgment and stays report-only — ``--fix`` never picks a side (ADR-015).

Exit code: ``1`` if any issue is found (so CI / cron can gate on it),
``0`` if the vault is clean.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape

from litman.commands._options import library_option, vault_option
from litman.core.checks import (
    AUTO_FIXABLE_CATEGORIES,
    Issue,
    apply_autofix,
    group_by_category,
    klass_a_checks,
    run_all_checks,
)
from litman.core.correctors import regen
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.locking import ensure_truth_locked

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
    "paper_dir_validity": (
        "Paper directory integrity (dir name / parseable metadata / id / paper.pdf)"
    ),
    "duplicate_doi": "Duplicate DOIs (--paper-doi lookups become ambiguous)",
    "discussion_scaffold": "Discussion log scaffold (discussion.md + its format header)",
    "index_vs_disk": "INDEX.json vs papers/ on disk",
    "views_vs_metadata": "Views (by-*/) vs metadata",
    "project_references": "Project REFERENCES.md / litman_reflib / litman_code vs membership",
    "project_bridge_dangling": (
        "Project bridge symlinks (litman_reflib / litman_code) resolve to a live target"
    ),
    "links_unsupported": "Folder links (views/ + project shortcuts)",
    "dangling_refs": "Dangling references (related/contradicts/extends + reverse)",
    "dangling_wikilinks": "Dangling [[id]] wikilinks in notes",
    "relevance_orphan": "Orphan relevance-<project> annotations",
    "taxonomy_drift": "Taxonomy drift (unregistered values)",
    "project_config_consistency": (
        "Project registry consistency (TAXONOMY.md vs lit-config.yaml)"
    ),
    "project_path_exists": "Project path existence (config paths on disk)",
    "bidirectional_refs": "Bidirectional 'related' asymmetry",
    "inbox_staleness": "Inbox staleness (>14 days)",
    "stale_staging": ".litman-staging/ leftovers",
    "orphan_trash_sidecar": "Orphan .trash/ sidecars",
    "trash_size": "Trash bloat (entry count)",
    "pdf_viewer": "PDF viewer availability (for `lit open`)",
    "code_clone_integrity": "Code clone integrity (clones vs metadata refs)",
    "vault_registry_drift": "Vault registry drift (registered path missing)",
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


def _render_report(issues: list[Issue]) -> None:
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
        for issue in items_sorted:
            console.print(_render_issue_line(issue))


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
        "Auto-regenerate all derived (klass-A) artifacts (lossless recompute "
        "from metadata) plus clean stale staging dirs / orphan trash sidecars. "
        "Registry / project / taxonomy / code-clone drift stays report-only "
        "(it needs a per-case decision; --fix never picks a side)."
    ),
)
@library_option
@vault_option
def health_check_cmd(
    do_fix: bool, library: Path | None, vault_name: str | None
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
            f"[dim](metadata.yaml / TAXONOMY.md / paper.pdf made read-only)[/]"
        )

    issues = run_all_checks(vault, papers)
    _render_report(issues)

    if do_fix and issues:
        applied = _apply_fixes(vault, issues)
        if applied:
            console.print("\n[bold]Auto-fix:[/]")
            for cat, n in applied.items():
                if n > 0:
                    console.print(
                        f"  [green]✓[/] {escape(cat)}: cleaned {n} item"
                        f"{'s' if n != 1 else ''}"
                    )
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


def _apply_fixes(vault: Path, issues: list[Issue]) -> dict[str, int]:
    """Auto-fix the fixable subset: klass-A regen + legacy validity cleanups.

    Two correction paths, both lossless (ADR-015):

    * **klass-A regen** — any klass-A category present (derived↔truth drift)
      triggers a single full ``regen`` (drop INDEX.json + views, recompute from
      metadata). Reported under each fired klass-A category for transparency.
    * **legacy validity** — ``stale_staging`` roll-back/forward and
      ``orphan_trash_sidecar`` removal stay routed through
      :func:`apply_autofix`, unchanged.

    klass-B drift (registry / project / taxonomy / code-clone) is never fixed
    here — it needs a per-case user decision (the Tier-1 ``resolve`` prompt or
    an explicit ``lit`` command). Returns ``{category: n_fixed}``.
    """
    counts: dict[str, int] = {}

    klass_a_present = {
        i.category for i in issues if i.category in _KLASS_A_CATEGORIES
    }
    if klass_a_present:
        regen(vault, issues)
        # A regen is a single wholesale rebuild; attribute one cleaned unit to
        # each fired klass-A category so the report names what was healed.
        for cat in klass_a_present:
            counts[cat] = 1

    counts.update(apply_autofix(vault, issues))
    return counts
