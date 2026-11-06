"""``lit sync`` command group (M6.1, ADR-030).

``lit sync`` with no subcommand is the two-way sync (``rclone bisync``,
:func:`litman.core.sync.two_way_sync`) of one library used on several
machines. Four subcommands wired thinly over ``litman.core.sync``:

- ``setup`` — hand the TTY to ``rclone config`` so the user can register
  a remote, then persist the chosen remote name + path into
  ``lit-config.yaml``'s new ``sync:`` block.
- ``push`` — mirror the vault to the configured remote (``rclone sync``).
- ``pull`` — mirror the configured remote back into the vault (used on a
  fresh machine to materialise the vault).
- ``status`` — show last-push / last-pull timestamps, local + remote file
  counts, and the size delta.

``push`` and ``pull`` refuse a library that syncs two-way
(:func:`litman.core.sync.require_one_way`).

M6.2 will layer ``--exclude-repos`` and ``--dry-run`` onto push/pull plus
a size-warning preflight before the first push. M6.1 keeps the surface
intentionally small so the rclone integration itself is the only thing
being exercised.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn, cast

import click
from click.core import ParameterSource
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from litman.commands._options import library_option, vault_option
from litman.core.checks import run_push_integrity_errors
from litman.core.code import left_behind_message
from litman.core.config import CONFIG_FILENAME, load_config
from litman.core.confirm import refuse_without_terminal
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.shell import shell_arg
from litman.core.sync import (
    CONFLICT_BACKUP_DAYS,
    DEFAULT_EXCLUDES,
    SYNC_LOCK_WAIT,
    FileChanges,
    FirstFrom,
    LastRun,
    RunConflict,
    SetupPayload,
    TwoWayResult,
    codes_ignore_patterns_to_rclone,
    compute_status,
    conflict_backups_root,
    describe_sync_lock_holder,
    format_iso,
    humanize_bytes,
    largest_files,
    list_remotes,
    local_sync_mode,
    local_vault_size,
    oauth_expired,
    pull,
    push,
    read_sync_mode,
    read_sync_state,
    recent_conflict_backups,
    remote_exists,
    require_one_way,
    sign_in_way_out,
    two_way_sync,
    write_sync_to_config,
)
from litman.core.sync_lock import LockHolder
from litman.exceptions import SyncBusyError, SyncChoiceRequiredError, SyncError

console = Console()
# rclone's own log lines go to stderr, as they do when rclone runs by itself.
_rclone_console = Console(stderr=True)

# Exit codes of two-way `lit sync` beyond 0 (synced) and 1 (failed); documented
# in docs/4-commands.md. Clear of every other litman command's codes.
EXIT_SYNC_NEEDS_CHOICE = 20
EXIT_SYNC_CONFLICTS = 21
# A sync with conflicts exits 21 even when a repo did not follow too; the
# summary lists both.
EXIT_SYNC_REPOS_DIFFER = 22
# Another sync of the same library (the GUI's auto sync, another terminal)
# still held its lock after SYNC_LOCK_WAIT seconds: nothing was synced. Also
# for push and pull.
EXIT_SYNC_BUSY = 23


@click.group("sync", invoke_without_command=True)
@click.option(
    "--first-from",
    "first_from",
    type=click.Choice(["cloud", "local"]),
    default=None,
    help=(
        "Two-way sync only. Compare both sides afresh, with this side winning "
        "every difference; the other side's differing files go to the backup, "
        "and files found on one side only are kept and copied across. Needed "
        "when the first sync finds a library on both sides."
    ),
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help=(
        "Two-way sync only. Go on past rclone's safety stop (more than half of "
        "the files deleted on one side, or every file changed); replaced and "
        "deleted files still go to the backup."
    ),
)
@library_option
@vault_option
@click.pass_context
def sync_group(
    ctx: click.Context,
    first_from: str | None,
    force: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Sync the library with a cloud folder through rclone.

    With no subcommand, lit sync syncs both ways, for one library used on
    several machines: what changed here goes up, what changed on your other
    machines comes down. When both changed the same file, the newer copy is
    kept and the older one goes to a backup folder; a paper deleted on one
    machine stays deleted even if the other changed it, and those changes go
    to the backup. Every file a sync replaces or deletes is kept in a backup
    folder, here and in the cloud.

    The first sync on a machine copies this library up when the cloud folder
    is empty, and copies the cloud's library down when this one is new and
    empty. When both already hold a library it changes nothing and asks you
    to pick one with --first-from.

    Never synced: INDEX.json and views/ (rebuilt after every sync),
    lit-config.yaml (each machine keeps its own project folders) and
    codes/*/repo/ checkouts. A code repo follows by version instead: each
    machine fetches the commit its repo-meta.yaml records from the repo's
    upstream, and moves its own checkout there unless it holds your changes.

    When another sync of this library is running (the GUI's auto sync, or
    lit sync in another terminal), lit sync waits up to 90 seconds for it,
    then gives up without syncing; so do push and pull.

    Exit codes: 0 synced, 1 failed, 20 waiting for your --first-from
    choice, 21 synced with conflicts, 22 synced but a code repo here does
    not match the library (21 when there are conflicts as well), 23 another
    sync of this library was still running, so nothing was synced (also
    push and pull).

    The subcommands are one-way: push mirrors the vault to the cloud as a
    backup, pull restores it from there, setup points litman at an rclone
    remote, status compares the two sides. Once a library syncs two-way,
    push and pull refuse it.
    """
    if ctx.invoked_subcommand is not None:
        _refuse_group_options_before(ctx, ctx.invoked_subcommand)
        return
    _run_two_way(
        first_from=cast("FirstFrom | None", first_from),
        force=force,
        library=library,
        vault_name=vault_name,
    )


def _refuse_group_options_before(ctx: click.Context, sub: str) -> None:
    """Refuse two-way options typed before a subcommand instead of dropping them.

    ``lit sync --library X push`` would otherwise push the active vault, not X.
    Only options typed on the command line count: ``$LIT_LIBRARY`` also fills
    ``--library`` and is meant for the subcommand.
    """

    def typed(name: str) -> bool:
        return ctx.get_parameter_source(name) is ParameterSource.COMMANDLINE

    two_way_only = [
        flag for name, flag in (("first_from", "--first-from"), ("force", "--force")) if typed(name)
    ]
    if two_way_only:
        verb = "belongs" if len(two_way_only) == 1 else "belong"
        raise click.UsageError(
            f"{' and '.join(two_way_only)} {verb} to two-way `lit sync`, not to "
            f"`lit sync {sub}`. Run `lit sync {sub} --help` for its own options."
        )
    misplaced = [
        flag for name, flag in (("library", "--library"), ("vault_name", "--vault")) if typed(name)
    ]
    if misplaced:
        raise click.UsageError(
            f"Put {' / '.join(misplaced)} after the subcommand, so it reaches it: "
            f"`lit sync {sub} {misplaced[0]} ...`."
        )


# ---------------------------------------------------------------------------
# lit sync (two-way)
# ---------------------------------------------------------------------------


def _echo_rclone_line(line: str) -> None:
    _rclone_console.print(f"[dim]{escape(line)}[/]", highlight=False, soft_wrap=True)


def _say_waiting(holder: LockHolder | None) -> None:
    console.print(
        f"[dim]{escape(describe_sync_lock_holder(holder))}; waiting up to "
        f"{SYNC_LOCK_WAIT:.0f} seconds…[/]",
        soft_wrap=True,
    )


def _exit_busy(err: SyncBusyError, title: str) -> NoReturn:
    console.print(Panel.fit(escape(str(err)), title=title, border_style="yellow"))
    sys.exit(EXIT_SYNC_BUSY)


def _describe_changes(changes: FileChanges | None) -> str:
    if changes is None:
        return "[dim]not counted on a full comparison[/]"
    if changes.total == 0:
        return "nothing"
    parts = [
        f"{len(paths)} {label}"
        for paths, label in (
            (changes.added, "new"),
            (changes.changed, "changed"),
            (changes.deleted, "deleted"),
        )
        if paths
    ]
    n = changes.total
    return f"{n} file{'s' if n != 1 else ''} ({', '.join(parts)})"


def _when(timestamp: float | None) -> str:
    if timestamp is None:
        return "?"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _render_two_way_summary(result: TwoWayResult) -> None:
    if result.mode == "sync":
        lines = ["[bold green]Synced.[/]"]
    elif result.chosen:
        winner = "this machine's" if result.mode == "first-local" else "the cloud's"
        lines = [f"[bold green]Started over:[/] {winner} copy won every difference."]
    elif result.mode == "first-local":
        lines = [
            "[bold green]First sync:[/] the cloud folder was empty, so this "
            "library was copied up."
        ]
    else:
        lines = [
            "[bold green]First sync:[/] this library was new, so the cloud's "
            "library was copied down."
        ]
    lines.append(f"[bold]Came down:[/] {_describe_changes(result.came_down)}")
    lines.append(f"[bold]Went up:[/] {_describe_changes(result.went_up)}")

    if result.conflicts:
        n = len(result.conflicts)
        lines.append(
            f"[bold yellow]{n} conflict{'s' if n != 1 else ''}:[/] the same file "
            "changed on both sides; the newer copy was kept, the older one moved "
            "to the backup."
        )
        for c in result.conflicts:
            where = f"{c.paper_id} · {c.path.rsplit('/', 1)[-1]}" if c.paper_id else c.path
            kept, other = (
                ("this machine's", "the cloud's")
                if c.kept == "local"
                else ("the cloud's", "this machine's")
            )
            lines.append(
                f"  • {escape(where)}: kept {kept} copy ({_when(c.kept_mtime)}); "
                f"{other} ({_when(c.moved_mtime)}) → backup"
            )
            if c.restored:
                lines.append(
                    "    [dim]rclone could not tell which copy was newer and kept "
                    "neither; litman put the newer one back.[/]"
                )
    if result.taxonomy_restored:
        added = "; ".join(
            f"{name} {', '.join(values)}" for name, values in result.taxonomy_restored.items()
        )
        lines.append(
            f"[bold]TAXONOMY.md:[/] added back what only the older copy had: {escape(added)}."
        )
    if result.taxonomy_restore_error is not None:
        lines.append(
            "[yellow]TAXONOMY.md values only the older copy had were not added back:[/] "
            f"{escape(result.taxonomy_restore_error)}. That copy is in the backup."
        )
    if result.deleted_folders:
        n = len(result.deleted_folders)
        lines.append(
            f"[bold yellow]{n} folder{'s' if n != 1 else ''} deleted on one machine and "
            "changed on the other:[/] the deletion won, and the changed files are in "
            "the backup."
        )
        for d in result.deleted_folders:
            kind, name = d.folder.split("/", 1)
            label = name if kind == "papers" else d.folder
            where = "here" if d.deleted_here else "on another machine"
            whose = "the other machine's" if d.deleted_here else "this machine's"
            lines.append(
                f"  • {escape(label)}: deleted or renamed {where}; {whose} changes "
                f"({escape(', '.join(d.moved) or 'none')}) → backup"
            )
            if d.in_trash:
                lines.append(
                    "    [dim]It is in the trash: "
                    f"`lit trash restore {escape(shell_arg(name))}` brings it back.[/]"
                )
    if result.deletions_unchecked:
        lines.append(
            "[yellow]Could not read rclone's per-file report:[/] a paper deleted on one "
            "machine and changed on another may be left incomplete. "
            "`lit health-check` finds it."
        )
    if result.unsynced:
        n = len(result.unsynced)
        lines.append(
            f"[yellow]{n} file{'s' if n != 1 else ''} changed here while the sync ran"
            f"[/] and may not have gone up: {escape(', '.join(result.unsynced[:3]))}"
            f"{', …' if n > 3 else ''}. The next `lit sync` takes "
            f"{'them' if n != 1 else 'it'} up."
        )
    lines += _repo_lines(result)
    if result.projects_without_folder:
        names = result.projects_without_folder
        lines.append(
            f"[yellow]No folder on this machine for project{'s' if len(names) != 1 else ''}"
            f":[/] {escape(', '.join(names))}. Set one with "
            f'`lit project set-path {escape(shell_arg(names[0]))} "<folder>"`.'
        )
    if result.rebuild_error is not None:
        lines.append(
            f"[yellow]INDEX.json and views/ were not rebuilt:[/] "
            f"{escape(result.rebuild_error)}. Run `lit refresh-views` once it is fixed."
        )
    elif result.rebuild is not None and result.rebuild.get("link_errors"):
        lines.append(
            "[yellow]Some project folders could not be brought up to date;[/] "
            "`lit refresh-views` tries again."
        )
    console.print(
        Panel.fit(
            "\n".join(lines),
            title=f"lit sync — {escape(result.target_url)}",
            border_style=(
                "yellow"
                if result.conflicts
                or result.deleted_folders
                or result.deletions_unchecked
                or _repos_differ(result)
                else "green"
            ),
        )
    )
    _print_backups(result)


def _repos_differ(result: TwoWayResult) -> bool:
    """Whether a code repo here does not match the library, or could not be checked."""
    return result.repos_error is not None or (
        result.repos is not None and result.repos.mismatched
    )


def _repo_lines(result: TwoWayResult) -> list[str]:
    """The summary's code repos part: what followed, what did not and why."""
    lines: list[str] = []
    report = result.repos
    if report is not None:
        rows: list[str] = []
        recorded = " — recorded for your other machines to follow"
        for done in report.restored:
            verb = "fetched at" if done.action == "cloned" else "moved to"
            on = f"on {escape(done.branch)}" if done.branch else "detached"
            note = recorded if done.name in result.repos_recorded else ""
            rows.append(f"  • {escape(done.name)}: {verb} {done.commit[:7]} ({on}){note}")
        fetched = {done.name for done in report.restored}
        for name in result.repos_recorded:
            if name not in fetched:
                commit, branch = report.unrecorded[name]
                on = f"on {escape(branch)}" if branch else "detached"
                rows.append(f"  • {escape(name)}: at {commit[:7]} ({on}){recorded}")
        for missed in report.not_restored:
            rows.append(
                f"  • [yellow]{escape(missed.name)}:[/] {escape(missed.what)}. "
                f"{escape(missed.way_out)}"
            )
        for name in report.left_behind:
            what, way_out = left_behind_message(name)
            rows.append(f"  • [yellow]{escape(what)}.[/] {escape(way_out)}")
        if rows:
            style = "bold yellow" if report.mismatched else "bold"
            lines += [f"[{style}]Code repos:[/]", *rows]
        if report.uncommitted:
            lines.append(
                f"[yellow]Uncommitted changes in {escape(', '.join(report.uncommitted))} "
                "are not synced.[/]"
            )
        if report.unpushed:
            lines.append(
                f"[yellow]Commits in {escape(', '.join(report.unpushed))} that no remote has "
                "are not synced:[/] push them so your other machines can fetch them."
            )
    if result.repos_error is not None:
        lines.append(
            f"[yellow]Code repos were not brought up to date:[/] "
            f"{escape(result.repos_error)}. The next `lit sync` tries again."
        )
    return lines


def _print_backups(result: TwoWayResult) -> None:
    """List the run's backups under the summary, each path whole on one line to copy.

    Inside the panel a long path would be folded at the border, mid-name.
    """
    paths = [
        *(c.backup for c in result.conflicts),
        *(d.backup for d in result.deleted_folders),
        *result.local_backups,
    ]
    if not paths:
        return
    console.print("[dim]Backups of this sync:[/]")
    for path in paths:
        console.print(f"  {escape(str(path))}", soft_wrap=True, highlight=False)


def _run_two_way(
    *,
    first_from: FirstFrom | None,
    force: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Two-way sync of the vault with its configured remote."""
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    target, _codes_patterns, _exclude_repos = _require_sync_configured(vault)
    banner = f"[bold]Syncing[/] [dim]{escape(str(vault))}[/] ⇄ [bold]{escape(target)}[/]"
    if first_from is not None:
        banner += f" [dim](--first-from {first_from})[/]"
    console.print(banner, soft_wrap=True)
    try:
        result = two_way_sync(
            vault,
            target,
            first_from=first_from,
            force=force,
            on_log=_echo_rclone_line,
            git_progress=console.is_terminal,
            lock_wait=SYNC_LOCK_WAIT,
            on_lock_wait=_say_waiting,
        )
    except SyncBusyError as err:
        _exit_busy(err, "lit sync — nothing was synced")
    except SyncChoiceRequiredError as err:
        console.print(
            Panel.fit(
                escape(str(err)),
                title="lit sync — nothing was synced",
                border_style="yellow",
            )
        )
        sys.exit(EXIT_SYNC_NEEDS_CHOICE)
    _render_two_way_summary(result)
    if result.conflicts or result.deleted_folders:
        sys.exit(EXIT_SYNC_CONFLICTS)
    if _repos_differ(result):
        sys.exit(EXIT_SYNC_REPOS_DIFFER)


# ---------------------------------------------------------------------------
# lit sync setup
# ---------------------------------------------------------------------------


@sync_group.command("setup")
@click.option(
    "--remote",
    "remote_arg",
    default=None,
    help=(
        "Use an already-registered rclone remote by name and skip the "
        "interactive rclone config step. Validated via rclone listremotes. "
        "Required with no terminal."
    ),
)
@click.option(
    "--path",
    "path_arg",
    default=None,
    help=(
        "Path inside the remote where the vault is mirrored "
        "(e.g. 'litman-vault/'). Prompted interactively if omitted; "
        "required with no terminal."
    ),
)
@library_option
@vault_option
def sync_setup_cmd(
    remote_arg: str | None,
    path_arg: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Configure the cloud sync target in lit-config.yaml.

    Default flow: invoke rclone config so wangq can register a new
    remote (browser OAuth, etc. — rclone owns that). After it exits we
    prompt for the remote name and a path inside it, validate the remote
    exists in rclone listremotes, then write to lit-config.yaml.

    Pass --remote NAME to skip the interactive rclone config step
    entirely — useful when wangq already has a remote configured and just
    wants to point litman at it.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    config_path = vault / CONFIG_FILENAME
    # rclone config and the two questions below all need someone at a terminal.
    missing = [
        flag
        for flag, value in (("--remote NAME", remote_arg), ("--path PATH", path_arg))
        if value is None
    ]
    if missing:
        refuse_without_terminal("pass " + " and ".join(missing))

    # If the user didn't pre-supply a remote, hand the TTY to rclone config
    # so the OAuth dance can run. We don't try to automate this step.
    if remote_arg is None:
        existing = list_remotes()
        if existing:
            console.print(
                "[dim]Already-registered rclone remotes:[/] "
                + ", ".join(escape(r) for r in existing)
            )
        console.print(
            Panel.fit(
                "Launching `rclone config` — register a new remote (or "
                "leave the existing ones alone and quit) and return here.\n\n"
                "[dim]Tip: pick `n` (New remote), give it a memorable name, "
                "pick your provider, follow the OAuth prompts, then `q` to "
                "quit.[/]",
                title="lit sync setup — rclone config",
                border_style="cyan",
            )
        )
        import subprocess  # local import keeps top-level cheap

        # Inherit stdio so the rclone TUI works. Don't go through our
        # `run_rclone` wrapper because it captures output by default and
        # would break the interactive experience.
        rc = subprocess.run(["rclone", "config"]).returncode
        if rc != 0:
            raise SyncError(
                f"`rclone config` exited {rc}. Setup aborted; re-run when ready."
            )

    # Now collect the (remote, path) pair. Prompt only for fields not given.
    remotes = list_remotes()
    if not remotes:
        raise SyncError(
            "No rclone remotes registered. Re-run `rclone config` and "
            "create at least one remote, then `lit sync setup` again."
        )

    if remote_arg is None:
        console.print(
            "[dim]Pick the remote you just registered (or any existing one):[/]"
        )
        for r in remotes:
            console.print(f"  - {escape(r)}")
        remote_name = click.prompt(
            "Remote name", type=str, default=remotes[0]
        ).strip()
    else:
        remote_name = remote_arg.strip()

    if not remote_exists(remote_name):
        raise SyncError(
            f"Remote {remote_name!r} is not registered in rclone. "
            f"Available: {', '.join(remotes) or '(none)'}. "
            "Run `rclone config` to create it, then re-run `lit sync setup`."
        )

    if path_arg is None:
        remote_path = click.prompt(
            "Path inside the remote (blank = root)",
            type=str,
            default="litman-vault/",
            show_default=True,
        ).strip()
    else:
        remote_path = path_arg.strip()

    # Preserve a hand-set `sync.exclude_repos` across a re-run of setup: the
    # command exposes no flag for it and write_sync_to_config rewrites the whole
    # `sync:` block, so re-pointing the remote/path must not silently reset it.
    existing_cfg = load_config(vault)
    prior_exclude = (
        existing_cfg.sync.exclude_repos if existing_cfg.sync is not None else False
    )
    payload = SetupPayload(
        remote=remote_name,
        path=remote_path,
        exclude_repos=prior_exclude,
    )
    write_sync_to_config(config_path, payload)

    target = f"{remote_name}:{remote_path}"
    console.print(
        Panel.fit(
            f"[bold green]Sync configured.[/]\n"
            f"[bold]Target:[/] {escape(target)}\n"
            f"[bold]Config:[/] {config_path}\n\n"
            "[dim]Next:[/] `lit sync` to start syncing, or "
            "`lit sync status` to inspect.",
            title="lit sync setup",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# Shared helpers (push / pull / status)
# ---------------------------------------------------------------------------


def _require_sync_configured(vault: Path) -> tuple[str, list[str], bool]:
    """Load config and return ``(target_url, codes_ignore_patterns,
    default_exclude_repos)`` or raise SyncError.

    The second element is the raw ``codes_ignore_patterns`` field from
    lit-config.yaml (the caller translates it to rclone globs when
    ``--exclude-repos`` is in effect). The third element is the boolean
    default from ``sync.exclude_repos`` so the caller can compute the
    effective exclude state when the CLI flag was not passed explicitly.
    """
    config = load_config(vault)
    if config.sync is None:
        raise SyncError(
            "Sync is not configured for this vault. Run `lit sync setup` "
            "first to register a cloud remote."
        )
    if not remote_exists(config.sync.remote):
        raise SyncError(
            f"Configured remote {config.sync.remote!r} is no longer "
            "registered in rclone. Re-run `lit sync setup` or "
            "`rclone config`."
        )
    return (
        config.sync.target_url(),
        list(config.codes_ignore_patterns),
        config.sync.exclude_repos,
    )


def _resolve_exclude_repos(
    flag: bool | None, default_from_config: bool
) -> bool:
    """Decide whether ``--exclude-repos`` is in effect.

    ``flag`` is the result of Click's ``--exclude-repos/--include-repos``
    boolean pair: ``True`` if the user passed ``--exclude-repos``, ``False``
    if they passed ``--include-repos``, and ``None`` if neither was given
    (the latter falls back to the config-file default).
    """
    return default_from_config if flag is None else flag


def _render_size_preview(
    vault: Path,
    extra_excludes: tuple[str, ...],
    exclude_repos_active: bool,
) -> None:
    """Print a size + top-5 file preview before the first push.

    Walks the local vault honoring the same exclude set that the push
    itself will apply, prints the total size + top-5 largest files, and
    nudges the user toward ``--exclude-repos`` when codes/ is not already
    excluded.
    """
    # Re-derive the full exclude set (DEFAULT + extras) for the local walk
    # so the preview matches what rclone will see during the actual push.
    full = (*DEFAULT_EXCLUDES, *extra_excludes)
    size = local_vault_size(vault, excludes=full)
    biggest = largest_files(vault, n=5, excludes=full)

    table = Table(
        title="First-push size preview",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Path", overflow="fold")
    table.add_column("Size", justify="right")
    for rel, nbytes in biggest:
        table.add_row(escape(str(rel)), humanize_bytes(nbytes))
    console.print(table)
    console.print(
        f"[bold]Total:[/] {size.count} files, {humanize_bytes(size.bytes)}"
    )
    if not exclude_repos_active:
        console.print(
            "[dim]Tip: pass `--exclude-repos` (or set "
            "`sync.exclude_repos: true` in lit-config.yaml) to skip "
            "`codes/*/repo/` checkouts — they re-clone on the new "
            "machine via `lit code restore-all`.[/]"
        )


# ---------------------------------------------------------------------------
# lit sync push
# ---------------------------------------------------------------------------


def _preflight_integrity_gate(
    vault: Path, *, force: bool, dry_run: bool
) -> None:
    """Block a push that would mirror a corrupted vault over the cloud backup.

    ``rclone sync`` overwrites the remote, and the remote is the vault's only
    version history (invariant #9: the vault is not git-tracked). Pushing a
    damaged local state (broken metadata, half-restore, hand-edited corruption)
    would clobber the good cloud copy, so any ``error``-severity finding from a
    TRUTH-integrity check aborts the push. Derived drift (INDEX / views /
    project-refs — klass A, fixable by a lossless regen) is deliberately
    excluded: it must never block a backup. ``warning`` / ``info`` findings
    never block either.

    ``--force`` overrides the gate ("I know I'm pushing a damaged state").
    ``--yes`` does NOT — it only skips the size confirmation, so an unattended
    cron push still refuses to mirror a broken vault. On ``--dry-run`` the gate
    reports the errors but never aborts (nothing is transferred either way).
    """
    errors = run_push_integrity_errors(vault, list_papers(vault))
    if not errors:
        return

    shown = errors[:20]
    lines = [
        f"[red]•[/] {escape(i.category)}"
        + (f" [dim]({escape(i.paper_id)})[/]" if i.paper_id else "")
        + f": {escape(i.message)}"
        for i in shown
    ]
    if len(errors) > len(shown):
        lines.append(f"[dim]... and {len(errors) - len(shown)} more[/]")
    body = "\n".join(lines)
    n = len(errors)
    plural = "s" if n != 1 else ""

    if dry_run:
        console.print(
            Panel.fit(
                f"{body}\n\n"
                f"[yellow]{n} integrity error{plural} — a real push would be "
                f"blocked here. Run [bold]lit health-check --fix[/] first.[/]",
                title="integrity gate (dry-run)",
                border_style="yellow",
            )
        )
        return

    if force:
        console.print(
            Panel.fit(
                f"{body}\n\n"
                f"[bold red]--force:[/] pushing over {n} integrity error{plural}. "
                f"The cloud backup will be overwritten with this state.",
                title="integrity gate bypassed",
                border_style="red",
            )
        )
        return

    console.print(
        Panel.fit(
            f"{body}\n\n"
            f"[bold]Push aborted:[/] {n} integrity error{plural} would be "
            f"mirrored over your cloud backup.\n"
            f"Fix with [bold]lit health-check --fix[/], then retry. "
            f"To push anyway, pass [bold]--force[/].",
            title="integrity gate",
            border_style="red",
        )
    )
    sys.exit(1)


@sync_group.command("push")
@click.option(
    "--exclude-repos/--include-repos",
    "exclude_repos_flag",
    default=None,
    help=(
        "Apply lit-config.yaml's codes_ignore_patterns (default repo/) "
        "to the transfer so codes/*/repo/ checkouts are not uploaded. "
        "When neither flag is given, falls back to sync.exclude_repos "
        "in lit-config.yaml. The bulky checkouts can be re-cloned later "
        "with lit code restore-all."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview only — print what rclone would transfer, then exit "
    "without touching the remote.",
)
@click.option(
    "--yes", "-y",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the first-push size confirmation prompt (use in scripts).",
)
@click.option(
    "--force", "-f",
    is_flag=True,
    default=False,
    help="Bypass the pre-push integrity gate and mirror the vault even if "
    "health-check reports errors. --yes does NOT bypass the gate (it only "
    "skips the size confirmation); only --force does.",
)
@library_option
@vault_option
def sync_push_cmd(
    exclude_repos_flag: bool | None,
    dry_run: bool,
    yes: bool,
    force: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Upload the vault to the configured cloud remote (rclone sync).

    One-way mirror: files removed locally are removed on the remote, files
    added or modified locally overwrite the remote. The
    .litman-sync-state.yaml and .litman-staging/ paths are
    unconditionally excluded.

    Before transferring, a full health check runs as an integrity gate: any
    error-severity finding aborts the push so a corrupted local state never
    overwrites the cloud backup (the vault's only version history). Pass
    --force to bypass the gate; --yes does NOT bypass it.

    On the FIRST push (last-push blank in the sync-state file), prints
    a size preview + top-5 largest files and confirms before transferring.
    Subsequent pushes go straight through; pass --yes to skip the
    confirm on first push (e.g. in cron / CI). --dry-run previews any
    push (first or subsequent) without touching the remote.

    Refused, --dry-run too, once the library syncs two-way, on this machine
    or another one: a push would overwrite what the other machines synced.
    Run lit sync instead.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    target, codes_patterns, default_exclude = _require_sync_configured(vault)
    require_one_way(vault, target, "push")
    _preflight_integrity_gate(vault, force=force, dry_run=dry_run)
    exclude_repos = _resolve_exclude_repos(exclude_repos_flag, default_exclude)
    extra_excludes = (
        codes_ignore_patterns_to_rclone(codes_patterns) if exclude_repos else ()
    )

    is_first_push = read_sync_state(vault).last_push is None
    if is_first_push and not dry_run:
        _render_size_preview(vault, extra_excludes, exclude_repos)
        if not yes:
            refuse_without_terminal("pass --yes to confirm")
            click.confirm(
                "Continue with first push to "
                f"{target}?",
                abort=True,
                default=True,
            )

    banner = (
        f"[bold]Pushing[/] [dim]{escape(str(vault))}[/] → [bold]{escape(target)}[/]"
    )
    if exclude_repos:
        banner += " [dim](codes/*/repo/ excluded)[/]"
    if dry_run:
        banner += " [dim](dry-run)[/]"
    console.print(banner, soft_wrap=True)
    try:
        push(
            vault,
            target,
            extra_excludes=extra_excludes,
            dry_run=dry_run,
            lock_wait=SYNC_LOCK_WAIT,
            on_lock_wait=_say_waiting,
        )
    except SyncBusyError as err:
        _exit_busy(err, "lit sync push — nothing was pushed")

    tail = "[dim]`lit sync status` to inspect.[/]"
    if dry_run:
        tail = "[dim]Dry-run only — nothing transferred. Drop --dry-run to push for real.[/]"
    console.print(
        Panel.fit(
            f"[bold green]{'Dry-run complete' if dry_run else 'Push complete'}.[/]\n"
            f"[bold]Target:[/] {escape(target)}\n\n"
            f"{tail}",
            title="lit sync push",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# lit sync pull
# ---------------------------------------------------------------------------


@sync_group.command("pull")
@click.option(
    "--exclude-repos/--include-repos",
    "exclude_repos_flag",
    default=None,
    help=(
        "Apply lit-config.yaml's codes_ignore_patterns so codes/*/repo/ "
        "is not pulled even if the remote happens to hold it. Usually "
        "unnecessary — if you pushed with --exclude-repos, the remote "
        "already lacks repo/ — but kept symmetric for clarity."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview only — print what rclone would transfer, then exit "
    "without modifying the local vault.",
)
@library_option
@vault_option
def sync_pull_cmd(
    exclude_repos_flag: bool | None,
    dry_run: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Download the configured cloud remote into the vault (rclone sync).

    Cross-machine restore: on a fresh server, run lit init to make an
    empty vault, rclone config to register the remote, lit sync setup
    to point litman at it, then lit sync pull to materialise everything.

    One-way mirror with deletion: a file present locally but absent on the
    remote is DELETED locally. This is correct for the "freshly cloned"
    scenario; do not run pull against a vault holding unpushed local work
    you care about. Pass --dry-run to preview safely.

    Refused, --dry-run too, once the library syncs two-way, on this machine
    or another one (run lit sync, or lit sync --first-from cloud on a new
    machine), and when the remote holds no lit-config.yaml.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    target, codes_patterns, default_exclude = _require_sync_configured(vault)
    require_one_way(vault, target, "pull")
    exclude_repos = _resolve_exclude_repos(exclude_repos_flag, default_exclude)
    extra_excludes = (
        codes_ignore_patterns_to_rclone(codes_patterns) if exclude_repos else ()
    )

    banner = (
        f"[bold]Pulling[/] [bold]{escape(target)}[/] → [dim]{escape(str(vault))}[/]"
    )
    if exclude_repos:
        banner += " [dim](codes/*/repo/ excluded)[/]"
    if dry_run:
        banner += " [dim](dry-run)[/]"
    console.print(banner, soft_wrap=True)
    try:
        pull(
            vault,
            target,
            extra_excludes=extra_excludes,
            dry_run=dry_run,
            lock_wait=SYNC_LOCK_WAIT,
            on_lock_wait=_say_waiting,
        )
    except SyncBusyError as err:
        _exit_busy(err, "lit sync pull — nothing was pulled")

    tail = (
        "[dim]Run `lit health-check` to verify, and "
        "`lit code restore-all` if codes/*/repo/ were excluded from "
        "the original push.[/]"
    )
    if dry_run:
        tail = "[dim]Dry-run only — nothing changed locally.[/]"
    console.print(
        Panel.fit(
            f"[bold green]{'Dry-run complete' if dry_run else 'Pull complete'}.[/]\n"
            f"[bold]Source:[/] {escape(target)}\n\n"
            f"{tail}",
            title="lit sync pull",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# lit sync status
# ---------------------------------------------------------------------------


@sync_group.command("status")
@click.option(
    "--local",
    "local_only",
    is_flag=True,
    default=False,
    help=(
        "Only this machine's records, with no rclone call and no network: how "
        "this library syncs (two-way, one-way, or not yet from here), its last "
        "two-way run and recent conflict backups. No file counts."
    ),
)
@library_option
@vault_option
def sync_status_cmd(local_only: bool, library: Path | None, vault_name: str | None) -> None:
    """Show last-push / last-pull timestamps and local vs. remote file counts.

    No network mutation. Calls rclone size --json once to enumerate the
    remote; reads the per-machine .litman-sync-state.yaml for
    timestamps. Output is a small table the user can eyeball at a glance.

    For a library that syncs two-way, both sides are counted the way two-way
    sync carries them, and the last two-way sync is shown instead. Below the
    table comes this machine's last two-way run (a terminal ``lit sync`` or
    the GUI's): how it ended, its conflicts with their backups, the code
    repos it left behind, or its whole error with what to do. Then the
    folders of conflict backups made here in the last days, but the last
    run's. Those two parts show even when the cloud cannot be reached.

    With --local, only this machine's records are read: the Mode row says
    whether the library syncs two-way here, one-way (push / pull), or has not
    synced from here yet; then the last run and the conflict backups. No rclone
    call, so it answers at once and offline.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    if local_only:
        _print_local_status(vault)
        return
    target, codes_patterns, default_exclude = _require_sync_configured(vault)
    last_run = read_sync_state(vault).last_run
    # Mirror the push exclude set so the local count matches what rclone
    # transfers. Without this, `exclude_repos` users see a permanent false
    # "out of sync" delta: push excludes codes/*/repo/, status counted it.
    extra_excludes = (
        codes_ignore_patterns_to_rclone(codes_patterns) if default_exclude else ()
    )
    try:
        two_way = read_sync_mode(vault, target).two_way
        report = compute_status(vault, target, extra_excludes=extra_excludes, two_way=two_way)
    except SyncError:
        # The cloud cannot be listed (an expired sign-in, no network): the
        # record of what went wrong is the part that matters now.
        if last_run is not None:
            _print_last_run(last_run, target)
        _print_conflict_backups(vault, target, last_run)
        raise

    table = Table(
        title=f"lit sync status — {target}",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Metric", style="bold")
    table.add_column("Value", overflow="fold")
    table.add_row("Target", escape(report.target_url))
    if report.two_way:
        table.add_row("Last sync", escape(format_iso(report.state.last_sync)))
    else:
        table.add_row("Last push", escape(format_iso(report.state.last_push)))
        table.add_row("Last pull", escape(format_iso(report.state.last_pull)))
    table.add_row(
        "Local",
        f"{report.local.count} files, {humanize_bytes(report.local.bytes)}",
    )
    table.add_row(
        "Remote",
        f"{report.remote.count} files, {humanize_bytes(report.remote.bytes)}",
    )

    file_delta = report.file_delta
    bytes_delta = report.bytes_delta
    if file_delta == 0 and bytes_delta == 0:
        delta_str = "[green]in sync[/]"
    else:
        sign = "+" if file_delta >= 0 else ""
        bsign = "+" if bytes_delta >= 0 else "-"
        delta_str = (
            f"local − remote = {sign}{file_delta} files, "
            f"{bsign}{humanize_bytes(abs(bytes_delta))}"
        )
    table.add_row("Delta", delta_str)
    console.print(table)
    if last_run is not None:
        _print_last_run(last_run, target)
    _print_conflict_backups(vault, target, last_run)


#: The Mode row of ``lit sync status --local`` (:func:`local_sync_mode`).
_LOCAL_MODE = {
    "two-way": "two-way",
    "one-way": "one-way (push / pull)",
    "unsynced": "not synced from this machine yet",
}


def _print_local_status(vault: Path) -> None:
    """``lit sync status --local``: this machine's records only, no rclone call."""
    config = load_config(vault)
    if config.sync is None:
        console.print(
            "Mode: not set up. `lit sync setup` sets up cloud sync for this library.",
            soft_wrap=True,
        )
        return
    target = config.sync.target_url()
    state = read_sync_state(vault)
    mode = local_sync_mode(vault, target, state)
    table = Table(
        title=f"lit sync status --local — {target}",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Metric", style="bold")
    table.add_column("Value", overflow="fold")
    table.add_row("Target", escape(target))
    table.add_row("Mode", _LOCAL_MODE[mode])
    if mode == "two-way":
        table.add_row("Last sync", escape(format_iso(state.last_sync)))
    else:
        table.add_row("Last push", escape(format_iso(state.last_push)))
        table.add_row("Last pull", escape(format_iso(state.last_pull)))
    console.print(table)
    if state.last_run is not None:
        _print_last_run(state.last_run, target)
    _print_conflict_backups(vault, target, state.last_run)


_RUN_TRIGGER = {
    "terminal": "`lit sync` in a terminal",
    "auto": "the GUI's auto sync",
    "exit": "the GUI's sync on exit",
}
_RUN_RESULT = {
    "ok": "synced",
    "conflicts": "synced, with conflicts",
    "needs-choice": "stopped until you pick which side wins",
    "failed": "failed",
}
_RUN_REASON = {
    "auth": "the cloud refused the sign-in",
    "safety-stop": "rclone's safety check stopped it",
    "lock": "the file system refuses file locks",
    "rclone": "rclone cannot run this sync",
}


def _print_last_run(run: LastRun, target: str) -> None:
    """This machine's last two-way run (:class:`LastRun`), for a person or an agent.

    One line says when, who and how it ended; then the whole error, each
    conflict with its backup, each repo left behind with its way out, and
    what to do. Printed unwrapped, so a long path stays whole.
    """

    def say(text: str) -> None:
        console.print(text, soft_wrap=True, highlight=False)

    who = _RUN_TRIGGER.get(run.trigger, run.trigger)
    how = _RUN_RESULT.get(run.result, run.result)
    if run.result == "conflicts":
        n = run.conflicts_total
        how = f"synced, {n} conflict{'' if n == 1 else 's'}"
    why = _RUN_REASON.get(run.reason or "")
    console.print()
    say(
        f"[bold]Last run:[/] {escape(format_iso(run.at))}, {escape(who)}: "
        f"{escape(how)}{f' ({escape(why)})' if why else ''}."
    )
    for line in (run.error or "").splitlines():
        if line.strip():
            say(f"  {escape(line)}")
    for c in run.conflicts:
        say(f"  • {escape(_run_conflict_text(c))}")
    if run.conflicts_total > len(run.conflicts):
        say(f"  • … and {run.conflicts_total - len(run.conflicts)} more")
    for r in run.repos:
        head = f"code repo {r.name}" if r.name else "code repos"
        say(f"  • {escape(head)}: {escape(r.what)}{f'. {escape(r.way_out)}' if r.way_out else ''}")
    if run.repos_total > len(run.repos):
        say(f"  • … and {run.repos_total - len(run.repos)} more repos")
    way_out = _run_way_out(run, target)
    if way_out:
        say(f"[bold]What to do:[/] {escape(way_out)}")


def _run_conflict_text(c: RunConflict) -> str:
    if c.kept == "local":
        return (
            f"{c.path}: changed on both machines; this machine's copy kept, "
            f"the cloud's is in {c.backup}"
        )
    if c.kept == "cloud":
        return (
            f"{c.path}: changed on both machines; the cloud's copy kept, "
            f"this machine's is in {c.backup}"
        )
    where = "here" if c.kept == "deleted-here" else "on another machine"
    other = "on another machine" if c.kept == "deleted-here" else "here"
    return (
        f"{c.path}/: deleted {where} and changed {other}; the deletion won, "
        f"the changed files are in {c.backup}"
    )


#: How many folders of conflict backups ``lit sync status`` lists.
_BACKUP_FOLDERS_CAP = 10
#: In the name of the copy that lost a conflict (``--conflict-suffix``).
_CONFLICT_COPY = ".sync-conflict"


def _print_conflict_backups(vault: Path, target: str, run: LastRun | None) -> None:
    """The folders of conflict backups made here in the last days, but the last run's.

    For an agent to find a conflict after later syncs replaced the record of
    the run that had it: each folder's time, whether a sync or a GUI save made
    it, its whole path and its first files. The folders that hold a backup the
    last run's record names are left out: that record shows them. Nothing is
    printed when no folder is left. Printed unwrapped, so a path stays whole.
    """
    root = conflict_backups_root(vault, target)
    shown = _folders_named(run, root)
    folders = [f for f in recent_conflict_backups(root) if f.path.name not in shown]
    if not folders:
        return

    def say(text: str) -> None:
        console.print(text, soft_wrap=True, highlight=False)

    listed = folders[:_BACKUP_FOLDERS_CAP]
    # The names of the files listed from a sync's folders, for the two lines
    # that say what they are.
    names = [rel.rsplit("/", 1)[-1] for f in listed if not f.by_save for rel in f.files]
    besides = ", besides the last run's" if shown else ""
    console.print()
    say(f"[bold]Conflict backups from the last {CONFLICT_BACKUP_DAYS} days{besides}:[/]")
    if any(_CONFLICT_COPY in name for name in names):
        say(
            "  A sync keeps the copy that lost as <file>.sync-conflict1 (this machine's) "
            "or .sync-conflict2 (the cloud's)."
        )
    if any(_CONFLICT_COPY not in name for name in names):
        say(
            "  A file under its own name in a sync's folder was changed in a folder "
            "deleted on the other side; the deletion won."
        )
    for f in listed:
        what = "a GUI save over a changed file" if f.by_save else "a sync's conflicts"
        count = f"{f.files_total} file{'' if f.files_total == 1 else 's'}"
        say(f"  • {f.at:%Y-%m-%d %H:%M}, {what}, {count}: {escape(str(f.path))}")
        for rel in f.files:
            say(f"      {escape(rel)}")
        if f.files_total > len(f.files):
            say(f"      … and {f.files_total - len(f.files)} more")
    if len(folders) > _BACKUP_FOLDERS_CAP:
        say(f"  • … and {len(folders) - _BACKUP_FOLDERS_CAP} more folders in {escape(str(root))}")


def _folders_named(run: LastRun | None, root: Path) -> set[str]:
    """The names of the folders under ``root`` that hold a backup ``run``'s record names."""
    names: set[str] = set()
    for c in run.conflicts if run is not None else ():
        try:
            parts = Path(c.backup).relative_to(root).parts
        except ValueError:
            continue
        if parts:
            names.add(parts[0])
    return names


def _run_way_out(run: LastRun, target: str) -> str | None:
    """What to do after ``run``, where its own words do not say it already."""
    if run.result == "conflicts":
        return (
            "Nothing is lost: compare each kept file with its backup and "
            "copy back what should stay."
        )
    if run.result != "failed":
        return None
    if run.reason == "auth":
        # How to sign in again was settled when the sync failed; a record from
        # before that kept it falls back on rclone's own words. Its error names
        # the way back itself when litman wrote it (auth_failure).
        sign_in = run.sign_in or ("browser" if oauth_expired(run.error or "") else None)
        way_out = sign_in_way_out(target.split(":", 1)[0], sign_in)
        return None if way_out in (run.error or "") else way_out
    if run.reason is None:
        return (
            "Run `lit sync` to try again; it shows rclone's whole output. "
            "If the network was down, the GUI's auto sync tries again on its own."
        )
    # safety-stop, lock and rclone: the error names the way out itself.
    return None
