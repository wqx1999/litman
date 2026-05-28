"""``lit health-check`` — vault-wide consistency probe.

Surfaces problems that the schema-less ``lit add`` / ``lit modify`` / ``lit
rename`` / ``lit rm`` flow can leave behind: schema gaps, dangling references,
half-finished renames, stale staging dirs, etc. See :mod:`litman.core.checks`
for the per-check semantics.

The CLI is read-only by default. ``--fix`` applies the auto-fixable subset
(stale staging dirs + orphan trash sidecars). Other categories print a hint
pointing at the manual remediation command.

Exit code: ``1`` if any issue is found (so CI / cron can gate on it),
``0`` if the vault is clean.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape

from litman.core.checks import (
    AUTO_FIXABLE_CATEGORIES,
    Issue,
    apply_autofix,
    group_by_category,
    run_all_checks,
)
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.vault_registry import find_dangling, load_registry
from litman.exceptions import VaultRegistryError

console = Console()

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
    "id_consistency": "ID consistency (dir name vs metadata id)",
    "invalid_paper_dirs": "Invalid paper directories",
    "dangling_refs": "Dangling references (related/contradicts/extends + reverse)",
    "dangling_wikilinks": "Dangling [[id]] wikilinks in notes",
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


def _vault_registry_drift_issues() -> list[Issue]:
    """Compute Issues for any dangling vault registration entries.

    Registry drift is a user-level concern (not vault-scoped), so it lives
    outside ``_CHECK_REGISTRY``. We still surface it through the same
    ``Issue`` pipeline so ``health-check`` stays the canonical "everything
    I should know" report. The day-to-day surfacing path is the root-group
    hook (M28); this is the cron / "I want a full audit" path.
    """
    try:
        reg = load_registry()
    except VaultRegistryError:
        return []
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
        for entry in find_dangling(reg)
    ]


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
    for category, items in grouped.items():
        header = _CATEGORY_HEADERS.get(category, category)
        n = len(items)
        fixable_marker = (
            " [dim](fixable via --fix)[/]"
            if category in AUTO_FIXABLE_CATEGORIES
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
    n_fixable = sum(
        1 for i in issues if i.category in AUTO_FIXABLE_CATEGORIES
    )
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
        f"Auto-clean fixable categories: "
        f"{', '.join(sorted(AUTO_FIXABLE_CATEGORIES))}. "
        "Other issues stay report-only."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def health_check_cmd(
    do_fix: bool, library: Path | None, vault_name: str | None
) -> None:
    """Run vault-wide consistency checks.

    Exits 0 on a clean vault, 1 if any issue is found (so the command can
    gate cron / CI tasks). With --fix the exit code reflects post-fix
    state — if every issue was in a fixable category, the second pass is
    clean and the command exits 0.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    papers = list_papers(vault)
    n_papers = len(papers)
    console.print(
        f"[dim]Running health checks on vault:[/] {escape(str(vault))} "
        f"[dim]({n_papers} paper{'s' if n_papers != 1 else ''})[/]"
    )

    issues = run_all_checks(vault, papers)
    issues.extend(_vault_registry_drift_issues())
    _render_report(issues)

    if do_fix and issues:
        fix_counts = apply_autofix(vault, issues)
        if fix_counts:
            console.print("\n[bold]Auto-fix:[/]")
            for cat, n in fix_counts.items():
                if n > 0:
                    console.print(
                        f"  [green]✓[/] {escape(cat)}: cleaned {n} item"
                        f"{'s' if n != 1 else ''}"
                    )
            # Re-run checks so the post-fix summary is honest.
            papers = list_papers(vault)
            issues = run_all_checks(vault, papers)
            issues.extend(_vault_registry_drift_issues())
            n_papers = len(papers)

    _summarize(issues, n_papers)

    if issues:
        sys.exit(1)
