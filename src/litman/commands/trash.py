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

import json
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from litman.commands._hub_report import (
    hub_settlement_lines,
    project_folder_not_updated,
)
from litman.commands._options import format_option, library_option, vault_option
from litman.core import confirm as confirm_mod
from litman.core.code import (
    CODES_DIRNAME,
    NOTES_FILENAME,
    REPO_DIRNAME,
    REPO_META_FILENAME,
    bind_paper_to_repo,
    fetch_repo,
    make_repo_meta,
    write_notes,
    write_repo_meta,
)
from litman.core.config import load_config
from litman.core.confirm import _confirm_destructive
from litman.core.correctors import reconcile_derived
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.locking import rmtree
from litman.core.trash import (
    RestoreResult,
    TrashEntry,
    count_replaced_folders,
    empty_trash,
    list_trash,
    resolve_trash_entry,
    restore_from_trash,
)
from litman.exceptions import CodeError

console = Console()


@click.group("trash")
def trash_group() -> None:
    """Manage the recoverable-delete bin under <vault>/.trash/.

    Capped at 100 entries; `lit rm` evicts the oldest when full.
    """


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _trash_row(e: TrashEntry) -> dict[str, Any]:
    """One trash entry as a JSON row, keyed the way the sidecar is.

    Carries the two fields the table has no room for: ``entry_path``, and
    the ``orphan_repos`` a restore would have to re-clone.
    """
    return {
        "paper_id": e.paper_id,
        "deleted_at": e.deleted_at,
        "cascade_was_used": e.cascade_was_used,
        "title": e.title,
        "entry_name": e.entry_name,
        "entry_path": str(e.entry_path),
        "orphan_repos": e.orphan_repos,
    }


@trash_group.command("list")
@format_option
@library_option
@vault_option
def trash_list_cmd(
    output_format: str, library: Path | None, vault_name: str | None
) -> None:
    """Show trash entries, newest first."""
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    entries = list_trash(vault)

    if output_format == "json":
        # Before the empty message: an empty trash is `[]`, not prose.
        click.echo(
            json.dumps([_trash_row(e) for e in entries], ensure_ascii=False)
        )
        return

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
    vault: Path,
    paper_id: str,
    repo_name: str,
    upstream: str,
    *,
    link_errors: list[OSError],
) -> None:
    """Re-clone a 1:1 hard-deleted repo and re-bind it to ``paper_id``.

    POST-transaction (step 3): reuses the INTERNAL clone + repo-meta + bind
    logic of ``core.code`` for the single repo A needs, NOT the vault-level
    ``restore_missing_repos``. The restore already put back
    ``codes/<repo>/repo-meta.yaml`` (papers:[A]) and the saved ``notes.md``;
    this fetches ``repo/`` into it, adds a ``notes.md`` when there was none to
    put back, and binds via ``bind_paper_to_repo``, which also
    links the repo into A's projects. A project ``litman_code/`` the bind
    could not update lands in ``link_errors``.

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
    had_meta = (repo_root / REPO_META_FILENAME).is_file()
    repo_root.mkdir(parents=True, exist_ok=True)
    try:
        fetch_repo(upstream, repo_dir)
        if not had_meta:
            write_repo_meta(
                repo_root, make_repo_meta(name=repo_name, upstream=upstream)
            )
        if not (repo_root / NOTES_FILENAME).exists():
            write_notes(repo_root, repo_name, upstream)
        # Bind INSIDE the guard so a bind failure also rolls back the
        # half-built checkout (invariant #12: no clone without link).
        bind_paper_to_repo(vault, paper_id, repo_name, link_errors=link_errors)
    except Exception as e:
        # Roll back what this attempt built so a failed re-clone (clone,
        # repo-meta, OR bind) leaves no half-built checkout — the binding on
        # the paper side is kept (caller's warning path). The repo-meta.yaml
        # the restore put back stays: it holds the upstream, and
        # `lit code restore-all` re-clones from it. Normalize to CodeError so
        # the caller's single ``except CodeError`` handles every failure
        # uniformly: ``bind_paper_to_repo`` can raise PaperNotFoundError,
        # which is not a CodeError and would otherwise escape and crash an
        # already-committed restore.
        # locking.rmtree, not shutil: a half-finished `git clone` already
        # wrote read-only .git objects, which Windows refuses to unlink — a
        # bare rmtree would strand them and wedge every future re-clone on
        # "directory not empty".
        rmtree(repo_dir if had_meta else repo_root, ignore_errors=True)
        if isinstance(e, CodeError):
            raise
        raise CodeError(f"re-clone of {repo_name!r} failed: {e}") from e


def _handle_missing_repos(
    vault: Path,
    result: RestoreResult,
    *,
    skip_confirm: bool,
    link_errors: list[OSError],
) -> None:
    """Step 3: re-clone any 1:1 hard-deleted repo A still binds.

    With ``-y`` (``skip_confirm``) the re-clone is auto-attempted with no
    prompt; otherwise the user is prompted per repo (the only interactive
    point in restore), and with no terminal to ask it counts as a refusal.
    On refuse OR failure the binding A.code-clones:[X] is KEPT and a warning
    is emitted — re-clone is never a precondition for the restore's success
    (decision #2/#3). The restore put ``codes/<repo>/repo-meta.yaml`` back
    with the upstream, so the way out it names is ``lit code restore-all``;
    with no upstream recorded there is nothing to clone, and it names
    ``lit health-check``.
    """
    for repo_name, upstream in sorted(result.missing_repos.items()):
        # The upstream is also on screen (review F20), though repo-meta.yaml
        # now keeps it on disk too.
        url_note = f" [dim](upstream: {escape(upstream)})[/]" if upstream else ""
        how = "lit code restore-all" if upstream else "lit health-check"
        if not skip_confirm and not confirm_mod._stdin_is_tty():
            # Nobody to ask: no clone, as with a "no", and never read stdin.
            console.print(
                f"  [yellow]Kept binding to '{escape(repo_name)}' without "
                f"re-clone[/]{url_note} [dim](run `{how}`)[/]",
                soft_wrap=True,
            )
            continue
        if not skip_confirm:
            do_clone = click.confirm(
                f"Repo '{repo_name}' was hard-deleted; re-clone from "
                f"{upstream or '(no url)'}?",
                default=True,
            )
            if not do_clone:
                console.print(
                    f"  [yellow]Kept binding to '{escape(repo_name)}' "
                    f"without re-clone[/]{url_note} [dim](run `{how}`)[/]",
                    soft_wrap=True,
                )
                continue
        try:
            _reclone_missing_repo(
                vault, result.paper_id, repo_name, upstream,
                link_errors=link_errors,
            )
        except CodeError as e:
            first_line = str(e).splitlines()[0] if str(e) else "clone failed"
            console.print(
                f"  [yellow]Re-clone of '{escape(repo_name)}' failed[/] "
                f"[dim]({escape(first_line)})[/]; binding kept{url_note}, "
                f"run `{how}` once it can be reached.",
                soft_wrap=True,
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
        "repo without prompting (script / agent path). Without it and with "
        "no terminal, the re-clone is skipped and the command for it printed."
    ),
)
@library_option
@vault_option
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
    and project links + REFERENCES.md are re-created. A 1:1 repo that was
    hard-deleted at rm time is re-cloned (prompted, auto with -y, skipped
    with no terminal); an edge
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
    # task-write-perf: reuse the list the staged INDEX.json was rendered from
    # and add just the reappearing paper's view links ({} → snapshot).
    reconcile_derived(
        vault,
        papers=result.surviving_papers,
        project_refs=False,
        views_delta=[(result.paper_id, {}, result.restored_view_fields)],
    )

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

    # Only the move-aside (see commands/_hub_report): a preserved folder is the
    # only copy of what was in it, so relocating it is never silent.
    for line in hub_settlement_lines(0, result.hub_moved_aside):
        console.print(f"  {line}", soft_wrap=True)

    # Step 3: re-clone any 1:1 hard-deleted repo (POST-transaction, may fail
    # without rolling back the restore).
    link_errors = list(result.link_errors)
    _handle_missing_repos(
        vault, result, skip_confirm=skip_confirm, link_errors=link_errors
    )

    # TAXONOMY drift is intentionally left to health-check — restore never
    # edits TAXONOMY (invariant #2). The `[[A]] (deleted)` de-annotation is
    # done inside restore_from_trash's transaction (M24).

    console.print("[dim]INDEX.json + views/ refreshed.[/]")
    # One line however many project folders refused, from either step.
    if link_errors:
        console.print(project_folder_not_updated(link_errors[0]), soft_wrap=True)


# ---------------------------------------------------------------------------
# empty
# ---------------------------------------------------------------------------


def _empty_subject(n_entries: int, n_kept: int) -> str:
    """Name what `lit trash empty` is about to remove.

    Two kinds of thing live in ``.trash/``: deleted papers, which can be
    restored, and folders `lit health-check --fix` moved out of a project hub,
    which cannot. One phrase, used by the dry-run header, the confirmation and
    the success line, so the three can never disagree about the count.
    """
    parts = []
    if n_entries:
        parts.append(f"{n_entries} entr{'y' if n_entries == 1 else 'ies'}")
    if n_kept:
        parts.append(
            f"{n_kept} replaced project folder{'' if n_kept == 1 else 's'}"
        )
    # Total on purpose: a caller that reaches zero of both still has to render
    # a sentence, and an empty one reads as "( removed)".
    return " + ".join(parts) or "nothing"


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
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview only — list every trash entry that would be permanently "
    "removed, then exit without emptying the trash.",
)
@library_option
@vault_option
def trash_empty_cmd(
    skip_confirm: bool,
    dry_run: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Permanently delete every trash entry.

    --dry-run lists every entry that would be removed (no truncation) and
    exits without emptying the trash.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    entries = list_trash(vault)
    # Folders `lit health-check --fix` moved out of a project hub. They are
    # deliberately invisible to list / restore, so this is the only command
    # that can clear them — it must not report an empty trash while they sit
    # there (and, for a code hub, sync a whole repo checkout to the cloud).
    n_kept = count_replaced_folders(vault)
    if not entries and not n_kept:
        console.print("[dim](trash is already empty)[/]")
        return

    if dry_run:
        console.print(
            f"[bold]Would permanently delete[/] "
            f"{_empty_subject(len(entries), n_kept)} "
            "[dim](dry-run)[/]"
        )
        for e in entries:
            console.print(
                f"  - {escape(e.paper_id)} [dim]({escape(e.deleted_at)})[/]"
            )
        console.print(
            "[dim]Dry-run only — nothing deleted. "
            "Drop --dry-run to empty the trash for real.[/]"
        )
        return

    # Route the confirm through the shared _confirm_destructive (same gate
    # taxonomy/project/rm use): --yes proceeds silently, a non-tty stdin
    # without --yes is refused WITHOUT reading stdin (so `echo y | lit trash
    # empty` can no longer bypass the prompt), and an interactive tty renders
    # the warning block + a default-No prompt.
    warning_lines = [
        f"[bold yellow]About to permanently delete[/] "
        f"{_empty_subject(len(entries), n_kept)}:"
    ]
    for e in entries[:10]:
        warning_lines.append(
            f"  - {escape(e.paper_id)} [dim]({escape(e.deleted_at)})[/]"
        )
    if len(entries) > 10:
        warning_lines.append(f"  ... and {len(entries) - 10} more")
    warning_lines.append("[bold red]Not recoverable.[/]")
    if not _confirm_destructive(warning_lines, yes=skip_confirm):
        console.print("[dim]Aborted. Trash unchanged.[/]")
        return

    n = empty_trash(vault)
    # Recounted, not assumed: empty_trash is best-effort per child, and the
    # count it reports is what actually went. The folders answer the same way.
    n_kept_removed = n_kept - count_replaced_folders(vault)
    if not n and not n_kept_removed:
        # A green tick over an empty trash that is still full is the one thing
        # this must never print.
        console.print(
            "[yellow]Nothing was removed[/] — every entry is still locked or "
            "unreadable.\n"
            "[dim]Close anything using the vault, then run `lit trash empty` "
            "again.[/]"
        )
        return
    console.print(
        f"[bold green]✓ Emptied[/] trash "
        f"[dim]({_empty_subject(n, n_kept_removed)} removed)[/]"
    )
