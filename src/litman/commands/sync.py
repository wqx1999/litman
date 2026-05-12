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
from litman.core.library import find_vault
from litman.core.sync import (
    SetupPayload,
    compute_status,
    format_iso,
    humanize_bytes,
    list_remotes,
    pull,
    push,
    remote_exists,
    write_sync_to_config,
)
from litman.exceptions import SyncError

console = Console()


@click.group("sync")
def sync_group() -> None:
    """rclone-backed cloud sync for the vault (M6).

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
def sync_setup_cmd(
    remote_arg: str | None,
    path_arg: str | None,
    library: Path | None,
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
    vault = find_vault(library)
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


def _require_sync_configured(vault: Path) -> str:
    """Load config and return the rclone target URL or raise SyncError."""
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
    return config.sync.target_url()


# ---------------------------------------------------------------------------
# lit sync push
# ---------------------------------------------------------------------------


@sync_group.command("push")
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
def sync_push_cmd(library: Path | None) -> None:
    """Upload the vault to the configured cloud remote (``rclone sync``).

    One-way mirror: files removed locally are removed on the remote, files
    added or modified locally overwrite the remote. The
    ``.litman-sync-state.yaml`` and ``.litman-staging/`` paths are
    unconditionally excluded.

    M6.1 deliberately omits size warnings and ``--exclude-repos`` /
    ``--dry-run`` — those land in M6.2.
    """
    vault = find_vault(library)
    target = _require_sync_configured(vault)
    console.print(
        f"[bold]Pushing[/] [dim]{vault}[/] → [bold]{escape(target)}[/]"
    )
    push(vault, target)
    console.print(
        Panel.fit(
            f"[bold green]Push complete.[/]\n"
            f"[bold]Target:[/] {escape(target)}\n\n"
            f"[dim]`lit sync status` to inspect.[/]",
            title="lit sync push",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# lit sync pull
# ---------------------------------------------------------------------------


@sync_group.command("pull")
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
def sync_pull_cmd(library: Path | None) -> None:
    """Download the configured cloud remote into the vault (``rclone sync``).

    Cross-machine restore: on a fresh server, run ``lit init`` to make an
    empty vault, ``rclone config`` to register the remote, ``lit sync setup``
    to point litman at it, then ``lit sync pull`` to materialise everything.

    One-way mirror with deletion: a file present locally but absent on the
    remote is DELETED locally. This is correct for the "freshly cloned"
    scenario; do not run pull against a vault holding unpushed local work
    you care about.
    """
    vault = find_vault(library)
    target = _require_sync_configured(vault)
    console.print(
        f"[bold]Pulling[/] [bold]{escape(target)}[/] → [dim]{vault}[/]"
    )
    pull(vault, target)
    console.print(
        Panel.fit(
            f"[bold green]Pull complete.[/]\n"
            f"[bold]Source:[/] {escape(target)}\n\n"
            f"[dim]Run `lit health-check` to verify, and "
            "`lit code restore-all` if codes/*/repo/ were excluded from "
            "the original push.[/]",
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
def sync_status_cmd(library: Path | None) -> None:
    """Show last-push / last-pull timestamps and local vs. remote file counts.

    No network mutation. Calls ``rclone size --json`` once to enumerate the
    remote; reads the per-machine ``.litman-sync-state.yaml`` for
    timestamps. Output is a small table the user can eyeball at a glance.
    """
    vault = find_vault(library)
    target = _require_sync_configured(vault)
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
