"""``lit trash`` — manage the recoverable-delete bin.

Three subcommands:

* ``list``    — enumerate trash entries (id, deleted_at, cascade flag, title)
* ``restore`` — move an entry back to ``papers/<id>/`` and symmetrically
  rebuild its relationship network (literature ref reverse edges, repo
  bindings, project symlinks), then optionally re-clone any 1:1 repo that was
  hard-deleted at rm time
* ``empty``   — permanently delete every entry (with y/N confirmation)

Trash storage layout and atomicity rules live in :mod:`litman.core.trash`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from litman.core.code import (
    CODES_DIRNAME,
    REPO_DIRNAME,
    bind_paper_to_repo,
    clone_repo,
    make_repo_meta,
    write_notes,
    write_repo_meta,
)
from litman.core.config import load_config
from litman.core.correctors import reconcile_derived
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.trash import (
    RestoreResult,
    empty_trash,
    list_trash,
    resolve_trash_entry,
    restore_from_trash,
)
from litman.exceptions import CodeError, TrashError

console = Console()


@click.group("trash")
def trash_group() -> None:
    """Manage the recoverable-delete bin under <vault>/.trash/.

    Capped at 100 entries; `lit rm` evicts the oldest when full.
    """


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@trash_group.command("list")
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
def trash_list_cmd(library: Path | None, vault_name: str | None) -> None:
    """Show trash entries, newest first."""
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    entries = list_trash(vault)
    if not entries:
        console.print("[dim](trash is empty)[/]")
        return

    table = Table(
        title=f"Trash ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("paper id")
    table.add_column("deleted_at")
    table.add_column("cascade", justify="center")
    table.add_column("title", overflow="fold")
    table.add_column("entry_name", style="dim")
    for e in entries:
        title = e.title if e.title else "[dim]—[/]"
        cascade_mark = "✓" if e.cascade_was_used else ""
        table.add_row(
            escape(e.paper_id),
            escape(e.deleted_at),
            cascade_mark,
            escape(title) if e.title else "[dim]—[/]",
            escape(e.entry_name),
        )
    console.print(table)
    console.print(
        "[dim]Restore via `lit trash restore <paper_id>` "
        "(or pass full entry_name to disambiguate).[/]"
    )


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def _reclone_missing_repo(
    vault: Path, paper_id: str, repo_name: str, upstream: str
) -> None:
    """Re-clone a 1:1 hard-deleted repo and re-bind it to ``paper_id``.

    POST-transaction (step 3): reuses the INTERNAL clone + repo-meta + bind
    logic of ``core.code`` for the single repo A needs, NOT the vault-level
    ``restore_missing_repos``. Builds ``codes/<repo>/{repo, repo-meta.yaml,
    notes.md}`` then binds via ``bind_paper_to_repo`` (papers:[A]).

    Raises:
        CodeError: empty upstream, clone failure, or bind failure. The caller
            turns this into a kept-binding + warning (decision #3).
    """
    if not upstream:
        raise CodeError(
            f"repo {repo_name!r} has no recorded upstream url to re-clone from"
        )
    repo_root = vault / CODES_DIRNAME / repo_name
    repo_dir = repo_root / REPO_DIRNAME
    repo_root.mkdir(parents=True, exist_ok=True)
    try:
        clone_repo(upstream, repo_dir)
        meta = make_repo_meta(name=repo_name, upstream=upstream, papers=[])
        write_repo_meta(repo_root, meta)
        write_notes(repo_root, repo_name, upstream)
        # Bind INSIDE the guard so a bind failure also rolls back the
        # half-built dir (invariant #12: no clone without link).
        bind_paper_to_repo(vault, paper_id, repo_name)
    except Exception as e:
        # Roll back the half-built codes/<repo>/ so a failed re-clone (clone,
        # repo-meta, OR bind) leaves no dangling dir — the binding on the
        # paper side is kept (caller's warning path); health-check backstops
        # the missing repo. Normalize to CodeError so the caller's single
        # ``except CodeError`` handles every failure uniformly:
        # ``bind_paper_to_repo`` can raise PaperNotFoundError, which is not a
        # CodeError and would otherwise escape and crash an already-committed
        # restore.
        shutil.rmtree(repo_root, ignore_errors=True)
        if isinstance(e, CodeError):
            raise
        raise CodeError(f"re-clone of {repo_name!r} failed: {e}") from e


def _handle_missing_repos(
    vault: Path,
    result: RestoreResult,
    *,
    skip_confirm: bool,
) -> None:
    """Step 3: re-clone any 1:1 hard-deleted repo A still binds.

    With ``-y`` (``skip_confirm``) the re-clone is auto-attempted with no
    prompt; otherwise the user is prompted per repo (the only interactive
    point in restore). On refuse OR failure the binding A.code-clones:[X] is
    KEPT and a warning is emitted — re-clone is never a precondition for the
    restore's success (decision #2/#3); health-check backstops the dangling
    code ref.
    """
    for repo_name, upstream in sorted(result.missing_repos.items()):
        # The trash sidecar (sole on-disk record of this 1:1 orphan's upstream
        # URL) is already gone by now, and it is auto-fix-deletable anyway, so
        # the URL must be echoed on every non-success path or it is lost for
        # good (review F20). `upstream` is carried in result.missing_repos, so
        # we always have it here even with -y (no prompt shown).
        url_note = f" [dim](upstream: {escape(upstream)})[/]" if upstream else ""
        if not skip_confirm:
            do_clone = click.confirm(
                f"Repo '{repo_name}' was hard-deleted; re-clone from "
                f"{upstream or '(no url)'}?",
                default=True,
            )
            if not do_clone:
                console.print(
                    f"  [yellow]Kept binding to '{escape(repo_name)}' "
                    f"without re-clone[/]{url_note} [dim](run "
                    "`lit health-check`)[/]"
                )
                continue
        try:
            _reclone_missing_repo(
                vault, result.paper_id, repo_name, upstream
            )
        except CodeError as e:
            first_line = str(e).splitlines()[0] if str(e) else "clone failed"
            console.print(
                f"  [yellow]Re-clone of '{escape(repo_name)}' failed[/] "
                f"[dim]({escape(first_line)})[/]; binding kept{url_note}, "
                "run `lit health-check`."
            )
            continue
        console.print(
            f"  [green]Re-cloned[/] '{escape(repo_name)}' "
            f"[dim]from {escape(upstream)}[/]"
        )


@trash_group.command("restore")
@click.argument("paper_id_or_entry")
@click.option(
    "--yes",
    "-y",
    "skip_confirm",
    is_flag=True,
    default=False,
    help=(
        "Non-interactive: auto-attempt the re-clone of any hard-deleted "
        "repo without prompting (script / agent path)."
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
def trash_restore_cmd(
    paper_id_or_entry: str,
    skip_confirm: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Restore a trashed paper to papers/<id>/ and rebuild its relations.

    Pass either the paper id (must be unambiguous) or the full entry name
    (<id>-<UTC-timestamp>). The paper's sealed fields drive a symmetric
    rebuild: opposite papers' paired reverse edges, surviving repo bindings,
    and project symlinks + REFERENCES.md are re-created. A 1:1 repo that was
    hard-deleted at rm time is re-cloned (prompted, or auto with -y); an edge
    whose opposite is no longer in the library is silently dropped.
    Refreshes INDEX.json and views/.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    entry = resolve_trash_entry(vault, paper_id_or_entry)
    registry = load_config(vault).projects

    # Steps 0-2: id-slot check, folder move, symmetric reverse-edge rebuild
    # (all atomic). Raises TrashError on a live-id collision (step 0).
    result = restore_from_trash(vault, entry, registry=registry)

    # Refresh INDEX + views from the post-restore paper list via the shared
    # funnel (M30 Phase 4): the two are recomputed together. project_refs=False:
    # restore_from_trash already rebuilt the restored paper's project symlinks +
    # REFERENCES.md (result.projects_rebuilt) — behavior unchanged.
    fresh_papers = list_papers(vault)
    reconcile_derived(vault, papers=fresh_papers, project_refs=False)

    console.print(
        f"[bold green]✓ Restored[/] {escape(entry.paper_id)} "
        f"[dim]→ papers/{escape(entry.paper_id)}/[/]"
    )
    if result.reverse_edges_rebuilt:
        n = len(result.reverse_edges_rebuilt)
        console.print(
            f"  Rebuilt reverse edges in [bold]{n}[/] paper"
            f"{'s' if n != 1 else ''} "
            f"[dim]({escape(', '.join(sorted(result.reverse_edges_rebuilt)))})[/]"
        )
    if result.repos_rebound:
        n = len(result.repos_rebound)
        console.print(
            f"  Re-bound to [bold]{n}[/] repo{'s' if n != 1 else ''} "
            f"[dim]({escape(', '.join(sorted(result.repos_rebound)))})[/]"
        )
    if result.projects_rebuilt:
        n = len(result.projects_rebuilt)
        console.print(
            f"  Re-linked into [bold]{n}[/] project"
            f"{'s' if n != 1 else ''} "
            f"[dim]({escape(', '.join(sorted(result.projects_rebuilt)))})[/]"
        )

    # Step 3: re-clone any 1:1 hard-deleted repo (POST-transaction, may fail
    # without rolling back the restore).
    _handle_missing_repos(vault, result, skip_confirm=skip_confirm)

    # TAXONOMY drift is intentionally left to health-check — restore never
    # edits TAXONOMY (invariant #2). The `[[A]] (deleted)` de-annotation is
    # done inside restore_from_trash's transaction (M24).

    console.print("[dim]INDEX.json + views/ refreshed.[/]")


# ---------------------------------------------------------------------------
# empty
# ---------------------------------------------------------------------------


@trash_group.command("empty")
@click.option(
    "--yes",
    "-y",
    "skip_confirm",
    is_flag=True,
    default=False,
    help="Skip the y/N confirmation prompt.",
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
def trash_empty_cmd(
    skip_confirm: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Permanently delete every trash entry."""
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    entries = list_trash(vault)
    if not entries:
        console.print("[dim](trash is already empty)[/]")
        return

    if not skip_confirm:
        console.print(
            f"[bold yellow]About to permanently delete[/] "
            f"{len(entries)} trash entr{'y' if len(entries) == 1 else 'ies'}:"
        )
        for e in entries[:10]:
            console.print(
                f"  - {escape(e.paper_id)} [dim]({escape(e.deleted_at)})[/]"
            )
        if len(entries) > 10:
            console.print(f"  ... and {len(entries) - 10} more")
        console.print("[bold red]Not recoverable.[/]")
        if not click.confirm("Continue?", default=False):
            console.print("[dim]Aborted. Trash unchanged.[/]")
            return

    n = empty_trash(vault)
    console.print(
        f"[bold green]✓ Emptied[/] trash "
        f"[dim]({n} entr{'y' if n == 1 else 'ies'} removed)[/]"
    )
