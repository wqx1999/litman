"""``lit sync`` command group (M6.1).

Four subcommands wired thinly over ``litman.core.sync``:

- ``setup`` — hand the TTY to ``rclone config`` so the user can register
  a remote, then persist the chosen remote name + path into
  ``lit-config.yaml``'s new ``sync:`` block.
- ``push`` — mirror the vault to the configured remote (``rclone sync``).
- ``pull`` — mirror the configured remote back into the vault (used on a
  fresh machine to materialise the vault).
- ``status`` — show last-push / last-pull timestamps, local + remote file
  counts, and the size delta.

M6.2 will layer ``--exclude-repos`` and ``--dry-run`` onto push/pull plus
a size-warning preflight before the first push. M6.1 keeps the surface
intentionally small so the rclone integration itself is the only thing
being exercised.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from litman.core.config import CONFIG_FILENAME, load_config
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.sync import (
    DEFAULT_EXCLUDES,
    SetupPayload,
    codes_ignore_patterns_to_rclone,
    compute_status,
    format_iso,
    humanize_bytes,
    largest_files,
    list_remotes,
    local_vault_size,
    pull,
    push,
    read_sync_state,
    remote_exists,
    write_sync_to_config,
)
from litman.exceptions import SyncError

console = Console()


@click.group("sync")
def sync_group() -> None:
    """rclone-backed cloud sync for the vault.

    One-way mirror: ``push`` copies the vault to your configured cloud
    remote, ``pull`` reverses the direction for cross-machine restore.
    ``setup`` walks you through registering a remote in ``rclone config``
    and writes the chosen target into ``lit-config.yaml``.

    The vault root file ``.litman-sync-state.yaml`` and the transient
    ``.litman-staging/`` directory are excluded by default and never travel
    between machines.
    """


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
        "interactive `rclone config` step. Validated via `rclone listremotes`."
    ),
)
@click.option(
    "--path",
    "path_arg",
    default=None,
    help=(
        "Path inside the remote where the vault is mirrored "
        "(e.g. 'litman-vault/'). Prompted interactively if omitted."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
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
def sync_setup_cmd(
    remote_arg: str | None,
    path_arg: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Configure the cloud sync target in ``lit-config.yaml``.

    Default flow: invoke ``rclone config`` so wangq can register a new
    remote (browser OAuth, etc. — rclone owns that). After it exits we
    prompt for the remote name and a path inside it, validate the remote
    exists in ``rclone listremotes``, then write to ``lit-config.yaml``.

    Pass ``--remote NAME`` to skip the interactive ``rclone config`` step
    entirely — useful when wangq already has a remote configured and just
    wants to point litman at it.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    config_path = vault / CONFIG_FILENAME

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
            "Remote name", type=str, default=remotes[0] if remotes else None
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

    payload = SetupPayload(
        remote=remote_name,
        path=remote_path,
        exclude_repos=False,
    )
    write_sync_to_config(config_path, payload)

    target = f"{remote_name}:{remote_path}"
    console.print(
        Panel.fit(
            f"[bold green]Sync configured.[/]\n"
            f"[bold]Target:[/] {escape(target)}\n"
            f"[bold]Config:[/] {config_path}\n\n"
            f"[dim]Next:[/] `lit sync push` to upload, or "
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


@sync_group.command("push")
@click.option(
    "--exclude-repos/--include-repos",
    "exclude_repos_flag",
    default=None,
    help=(
        "Apply lit-config.yaml's `codes_ignore_patterns` (default `repo/`) "
        "to the transfer so `codes/*/repo/` checkouts are not uploaded. "
        "When neither flag is given, falls back to `sync.exclude_repos` "
        "in lit-config.yaml. The bulky checkouts can be re-cloned later "
        "with `lit code restore-all`."
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
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
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
def sync_push_cmd(
    exclude_repos_flag: bool | None,
    dry_run: bool,
    yes: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Upload the vault to the configured cloud remote (``rclone sync``).

    One-way mirror: files removed locally are removed on the remote, files
    added or modified locally overwrite the remote. The
    ``.litman-sync-state.yaml`` and ``.litman-staging/`` paths are
    unconditionally excluded.

    On the FIRST push (``last-push`` blank in the sync-state file), prints
    a size preview + top-5 largest files and confirms before transferring.
    Subsequent pushes go straight through; pass ``--yes`` to skip the
    confirm on first push (e.g. in cron / CI). ``--dry-run`` previews any
    push (first or subsequent) without touching the remote.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    target, codes_patterns, default_exclude = _require_sync_configured(vault)
    exclude_repos = _resolve_exclude_repos(exclude_repos_flag, default_exclude)
    extra_excludes = (
        codes_ignore_patterns_to_rclone(codes_patterns) if exclude_repos else ()
    )

    is_first_push = read_sync_state(vault).last_push is None
    if is_first_push and not dry_run:
        _render_size_preview(vault, extra_excludes, exclude_repos)
        if not yes:
            click.confirm(
                "Continue with first push to "
                f"{target}?",
                abort=True,
                default=True,
            )

    banner = (
        f"[bold]Pushing[/] [dim]{vault}[/] → [bold]{escape(target)}[/]"
    )
    if exclude_repos:
        banner += " [dim](codes/*/repo/ excluded)[/]"
    if dry_run:
        banner += " [dim](dry-run)[/]"
    console.print(banner)
    push(vault, target, extra_excludes=extra_excludes, dry_run=dry_run)

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
        "Apply lit-config.yaml's `codes_ignore_patterns` so `codes/*/repo/` "
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
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
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
def sync_pull_cmd(
    exclude_repos_flag: bool | None,
    dry_run: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Download the configured cloud remote into the vault (``rclone sync``).

    Cross-machine restore: on a fresh server, run ``lit init`` to make an
    empty vault, ``rclone config`` to register the remote, ``lit sync setup``
    to point litman at it, then ``lit sync pull`` to materialise everything.

    One-way mirror with deletion: a file present locally but absent on the
    remote is DELETED locally. This is correct for the "freshly cloned"
    scenario; do not run pull against a vault holding unpushed local work
    you care about. Pass ``--dry-run`` to preview safely.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    target, codes_patterns, default_exclude = _require_sync_configured(vault)
    exclude_repos = _resolve_exclude_repos(exclude_repos_flag, default_exclude)
    extra_excludes = (
        codes_ignore_patterns_to_rclone(codes_patterns) if exclude_repos else ()
    )

    banner = (
        f"[bold]Pulling[/] [bold]{escape(target)}[/] → [dim]{vault}[/]"
    )
    if exclude_repos:
        banner += " [dim](codes/*/repo/ excluded)[/]"
    if dry_run:
        banner += " [dim](dry-run)[/]"
    console.print(banner)
    pull(vault, target, extra_excludes=extra_excludes, dry_run=dry_run)

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
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
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
def sync_status_cmd(library: Path | None, vault_name: str | None) -> None:
    """Show last-push / last-pull timestamps and local vs. remote file counts.

    No network mutation. Calls ``rclone size --json`` once to enumerate the
    remote; reads the per-machine ``.litman-sync-state.yaml`` for
    timestamps. Output is a small table the user can eyeball at a glance.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    target, _patterns, _default_exclude = _require_sync_configured(vault)
    report = compute_status(vault, target)

    table = Table(
        title=f"lit sync status — {target}",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Metric", style="bold")
    table.add_column("Value", overflow="fold")
    table.add_row("Target", escape(report.target_url))
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
