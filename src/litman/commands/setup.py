"""``lit setup`` — interactive first-run onboarding wizard (M27).

A pure orchestrator: it chains the four standalone onboarding commands
(install-completion / install-skill / init / sync setup) behind a single
"press a few enters" TTY flow. It implements NO new functionality — every
step delegates to the existing command via ``ctx.invoke``, so the wizard
and the standalone commands can never drift.

Only runs interactively (invariant #5: the CLI is fully usable without the
wizard; ADR-007: agents/automation take the non-TTY path via the standalone
commands). cloud-provider choice + OAuth are owned entirely by
``rclone config`` (invariant #6 analogue: litman touches neither LLM nor
cloud credentials).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

from litman.commands.init import init_cmd
from litman.commands.install_completion import (
    SUPPORTED_SHELLS,
    completion_installed,
    detect_shell,
    install_completion_cmd,
)
from litman.commands.install_skill import install_skill_cmd
from litman.commands.sync import sync_setup_cmd
from litman.core.config import load_config
from litman.core.library import DEFAULT_VAULT_NAME, find_vault
from litman.core.skill import installed_skill_names, list_bundled_skills
from litman.core.vault_registry import (
    VaultRegistryError,
    ensure_name_registrable,
    load_registry,
)
from litman.exceptions import LibraryNotFoundError, LitmanError

console = Console()


def _stdin_is_tty() -> bool:
    """Indirection so tests can force the interactive branch. ``lit setup``
    is TTY-only; automation calls the standalone commands instead."""
    return sys.stdin.isatty()


@click.command("setup")
@click.pass_context
def setup_cmd(ctx: click.Context) -> None:
    """Interactive first-run onboarding wizard.

    Chains four optional steps behind simple prompts:
      1. shell tab-completion    (default: yes)
      2. agent skill             (default: Claude Code)
      3. create your first vault (default: yes, if you have none)
      4. cloud sync              (default: no)

    Every step just runs the matching standalone command, so anything the
    wizard does you can also do or redo directly: lit install-completion,
    lit install-skill, lit init, lit sync setup. Automation should call
    those directly — this wizard only runs in an interactive terminal.
    """
    if not _stdin_is_tty():
        raise LitmanError(
            "lit setup is an interactive wizard and needs a terminal. "
            "For automation / CI / agents, call the underlying commands "
            "directly:\n"
            "  lit install-completion <shell>\n"
            "  lit install-skill\n"
            "  lit init <parent-dir>\n"
            "  lit sync setup"
        )

    console.print(
        Panel.fit(
            "This wizard chains four optional steps:\n"
            "  1. shell tab-completion\n"
            "  2. agent skill (Claude Code)\n"
            "  3. create your first vault\n"
            "  4. cloud sync\n\n"
            "[dim]Press Enter to accept each [default]. Ctrl-C to bail.[/]",
            title="lit setup",
            border_style="cyan",
        )
    )

    did: list[str] = []
    skipped: list[str] = []

    _step_completion(ctx, did, skipped)
    _step_skill(ctx, did, skipped)
    _step_vault(ctx, did, skipped)
    _step_sync(ctx, did, skipped)

    _print_summary(did, skipped)


def _step_completion(
    ctx: click.Context, did: list[str], skipped: list[str]
) -> None:
    console.rule("[bold]Step 1/4 — shell completion")
    shell = detect_shell()
    if shell is None:
        console.print(
            "[yellow]Could not detect your shell from $SHELL.[/] Run "
            f"[bold]lit install-completion <{'/'.join(SUPPORTED_SHELLS)}>[/] "
            "later to enable tab-completion."
        )
        skipped.append("completion (shell not detected)")
        return
    if completion_installed(shell):
        console.print(
            f"[dim]Completion already installed for {shell}; skipping.[/]"
        )
        skipped.append(f"completion ({shell}, already installed)")
        return
    if click.confirm(f"Install tab-completion for {shell}?", default=True):
        ctx.invoke(install_completion_cmd, shell=shell)
        did.append(f"completion ({shell})")
    else:
        skipped.append("completion (declined)")


def _step_skill(
    ctx: click.Context, did: list[str], skipped: list[str]
) -> None:
    console.rule("[bold]Step 2/4 — agent skill")

    # Re-run idempotency: install_skill_cmd defaults to force=False and the
    # underlying install_all_skills raises SkillInstallError on the first
    # already-present target. Probe state first and expose --force as a
    # prompt so wizard users can refresh skills (e.g., after a litman
    # upgrade ships updated skill content) without dropping to the
    # standalone command. Mirrors feedback_wizard_mirrors_command_flags.
    bundled = set(list_bundled_skills())
    already = installed_skill_names()
    if already:
        if already >= bundled:
            console.print(
                f"[dim]Skills already installed "
                f"({', '.join(sorted(already))}).[/]"
            )
            if not click.confirm(
                "Reinstall (overwrite with the bundled version)?",
                default=False,
            ):
                skipped.append("skill (already installed)")
                return
            ctx.invoke(install_skill_cmd, force=True)
            did.append("skill (reinstalled, Claude Code)")
            return
        missing = bundled - already
        console.print(
            f"[dim]Some skills already installed "
            f"({', '.join(sorted(already))}); "
            f"missing ({', '.join(sorted(missing))}).[/]"
        )
        # --force is required because install_all_skills raises on the
        # first present target; we cannot install just the missing ones
        # via install_all without it. Default Y because the user clearly
        # wanted skills before.
        if not click.confirm(
            "Install missing skills (also refreshes present ones with the "
            "bundled version)?",
            default=True,
        ):
            skipped.append("skill (partially installed)")
            return
        ctx.invoke(install_skill_cmd, force=True)
        did.append("skill (refreshed, Claude Code)")
        return

    console.print(
        "An agent skill lets Claude Code drive litman (optional; the CLI "
        "works fully without it)."
    )
    # Numbered choice (not free-text): pick by number, default 1. When a
    # second backend ships, add "3) <name>" here and map it below.
    choice = click.prompt(
        "Install agent skill?  1) Claude Code   2) skip",
        type=click.IntRange(1, 2),
        default=1,
    )
    if choice == 2:
        skipped.append("skill (declined)")
        return
    ctx.invoke(install_skill_cmd)  # installs all bundled Claude Code skills
    did.append("skill (Claude Code)")


def _step_vault(
    ctx: click.Context, did: list[str], skipped: list[str]
) -> None:
    console.rule("[bold]Step 3/4 — create a vault")
    reg = load_registry()

    if not reg.vaults:
        if not click.confirm("Create your first vault now?", default=True):
            skipped.append("vault (declined)")
            return
        default_name = DEFAULT_VAULT_NAME
    else:
        console.print(
            f"[dim]You already have {len(reg.vaults)} registered "
            "vault(s); setup is not the place to manage multiple vaults "
            "(use lit init / lit vault add).[/]"
        )
        if not click.confirm("Create another vault?", default=False):
            skipped.append("vault (already have one)")
            return
        # The bare default 'literature_vault' would collide with the existing
        # registration, so suggest a collision-free '<base>_<n>' instead. User
        # can still type their own name (validated below).
        default_name = _suggest_distinct_name(reg)

    name = _prompt_vault_name(reg, default_name)

    parent = click.prompt(
        "Parent directory for the vault (the CLI creates a "
        f"'{name}/' subdir inside it)",
        type=click.Path(file_okay=False, path_type=Path),
        default=str(Path.cwd()),
    )
    # Wizard ties --name and --register-as together so on-disk and registry
    # names agree. Advanced users wanting them to differ go through `lit init`
    # directly (`--name X --register-as Y`).
    ctx.invoke(
        init_cmd, parent_dir=Path(parent), name=name, register_as=name
    )
    did.append("vault")


def _suggest_distinct_name(reg) -> str:
    """Return ``DEFAULT_VAULT_NAME`` if it is free, else the first
    ``<base>_<n>`` (n=2,3,...) that is not already in the registry. Seeds
    the wizard's name prompt with a collision-free default so the common
    case is one Enter, not a re-prompt loop."""
    used = {v.name for v in reg.vaults}
    if DEFAULT_VAULT_NAME not in used:
        return DEFAULT_VAULT_NAME
    n = 2
    while f"{DEFAULT_VAULT_NAME}_{n}" in used:
        n += 1
    return f"{DEFAULT_VAULT_NAME}_{n}"


def _prompt_vault_name(reg, default: str) -> str:
    """Loop until the user gives a name that passes ensure_name_registrable.
    The suggested default is already collision-free, so the loop only fires
    when the user types a name that clashes with an existing registration."""
    while True:
        name = click.prompt(
            "Vault name (used as the on-disk subdir AND the registry name)",
            type=str,
            default=default,
        ).strip()
        try:
            ensure_name_registrable(reg, name)
            return name
        except VaultRegistryError as e:
            console.print(f"[red]{e}[/]\nTry another name.")


def _step_sync(
    ctx: click.Context, did: list[str], skipped: list[str]
) -> None:
    console.rule("[bold]Step 4/4 — cloud sync")
    if shutil.which("rclone") is None:
        console.print(
            "[yellow]rclone not found on PATH.[/] Install it "
            "(https://rclone.org/install/), then run [bold]lit sync "
            "setup[/] to enable cloud sync."
        )
        skipped.append("sync (rclone not installed)")
        return
    try:
        vault = find_vault(None)  # active vault via the standard chain
    except LibraryNotFoundError:
        console.print(
            "[dim]No active vault to attach sync to; skipping. Run "
            "[bold]lit sync setup[/] after you create one.[/]"
        )
        skipped.append("sync (no vault)")
        return
    if load_config(vault).sync is not None:
        if not click.confirm(
            "Sync is already configured for the active vault. Reconfigure?",
            default=False,
        ):
            skipped.append("sync (already configured)")
            return
    if click.confirm("Set up cloud sync now?", default=False):
        ctx.invoke(sync_setup_cmd)
        did.append("sync")
    else:
        skipped.append("sync (declined)")


def _print_summary(did: list[str], skipped: list[str]) -> None:
    lines = ["[bold green]Setup complete.[/]", ""]
    if did:
        lines.append("[bold]Done:[/]")
        lines += [f"  [green]•[/] {x}" for x in did]
    if skipped:
        lines.append("[bold]Skipped:[/]")
        lines += [f"  [dim]•[/] {x}" for x in skipped]
    lines += [
        "",
        "[dim]Re-run any step directly anytime: lit install-completion / "
        "lit install-skill / lit init / lit sync setup.[/]",
    ]
    console.print(
        Panel.fit("\n".join(lines), title="lit setup", border_style="green")
    )
