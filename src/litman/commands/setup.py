"""``lit setup`` — interactive first-run onboarding wizard (M27).

A pure orchestrator: it chains the five standalone onboarding commands
(install-completion / install-skill / init / sync setup / gui
--make-shortcut) behind a single "press a few enters" TTY flow. It
implements NO new functionality — every step delegates to the existing
command via ``ctx.invoke``, so the wizard and the standalone commands can
never drift.

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

from litman.commands.gui import display_available, gui_cmd, shortcut_path
from litman.commands.init import init_cmd
from litman.commands.install_completion import (
    SUPPORTED_SHELLS,
    completion_installed,
    detect_shell,
    install_completion_cmd,
)
from litman.commands.install_skill import install_skill_cmd
from litman.commands.sync import sync_setup_cmd
from litman.core import agent_prefs, agents
from litman.core.config import load_config
from litman.core.library import DEFAULT_VAULT_NAME, find_vault
from litman.core.skill import (
    refresh_stale_copies,
    skill_status,
    stale_skill_copies,
)
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
    return sys.stdin is not None and sys.stdin.isatty()


@click.command("setup")
@click.pass_context
def setup_cmd(ctx: click.Context) -> None:
    """Interactive first-run onboarding wizard.

    Chains five optional steps behind simple prompts:
      1. shell tab-completion    (default: yes)
      2. agent skill             (pick your agent; default: your current one)
      3. create your first vault (default: yes, if you have none)
      4. cloud sync              (default: no)
      5. desktop shortcut        (default: yes, if the session has a display)

    Every step just runs the matching standalone command, so anything the
    wizard does you can also do or redo directly: lit install-completion,
    lit install-skill, lit init, lit sync setup, lit gui --make-shortcut.
    Automation should call those directly — this wizard only runs in an
    interactive terminal.
    """
    if not _stdin_is_tty():
        raise LitmanError(
            "lit setup is an interactive wizard and needs a terminal. "
            "For automation / CI / agents, call the underlying commands "
            "directly:\n"
            "  lit install-completion <shell>\n"
            "  lit install-skill\n"
            "  lit init <parent-dir>\n"
            "  lit sync setup\n"
            "  lit gui --make-shortcut"
        )

    console.print(
        Panel.fit(
            "This wizard chains five optional steps:\n"
            "  1. shell tab-completion\n"
            "  2. agent skill\n"
            "  3. create your first vault\n"
            "  4. cloud sync\n"
            "  5. desktop shortcut\n\n"
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
    _step_shortcut(ctx, did, skipped)

    _print_summary(did, skipped)


def _step_completion(
    ctx: click.Context, did: list[str], skipped: list[str]
) -> None:
    console.rule("[bold]Step 1/5 — shell completion")
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


def _sweep_other_agent_skills(chosen: str, did: list[str]) -> None:
    """Offer to refresh stale litman-skill copies in the OTHER agents' dirs.

    Runs after the chosen agent's skills are settled (installed, refreshed
    or confirmed up to date — not after a decline): agents can read each
    other's directories (Cursor prefers the Claude dir over the
    open-standard one), so a stale copy installed for another agent may be
    the one actually in effect. Same consent model as the standalone
    command's bare-run sweep — one [Y/n] (default yes) per stale copy;
    absent and linked copies are never touched.
    """
    main_dir = agents.agent_skills_parent_dir(chosen)
    other_dirs = [d for d in agents.skills_parent_dirs() if d != main_dir]
    if not stale_skill_copies(other_dirs):
        return
    refreshed = refresh_stale_copies(
        other_dirs,
        confirm=lambda copy_dir, copy_name: click.confirm(
            f"Skill '{copy_name}' installed for another agent ({copy_dir}) "
            "is out of date with this litman — refresh it too? "
            "(files you added are kept)",
            default=True,
        ),
    )
    n = sum(len(names) for names in refreshed.values())
    if n:
        did.append(f"skill (refreshed {n} cop{'ies' if n != 1 else 'y'} "
                   "in other agent dirs)")


def _step_skill(
    ctx: click.Context, did: list[str], skipped: list[str]
) -> None:
    console.rule("[bold]Step 2/5 — agent skill")

    # Strict three-beat order: ask which agent → record it as the machine-
    # level default → install into that agent's directory. The choice is
    # recorded BEFORE the install so a failed or declined skill step never
    # loses it (bare `lit install-skill`, the GUI red dot and health-check
    # all follow this default). preferences.yaml is machine-global config,
    # NOT a vault TRUTH/DERIVED surface — invariant #16 (the WebUI
    # structured-write whitelist) does not apply.
    console.print(
        "An agent skill lets your AI agent drive litman (optional; the "
        "CLI works fully without it)."
    )
    console.print(
        "[dim]More agents coming (Codex, OpenCode); manage agents anytime "
        "in the GUI.[/]"
    )
    names = [spec.name for spec in agents.supported_agents()]
    current = agent_prefs.load_default_agent()
    chosen = click.prompt(
        "Which agent do you use?",
        type=click.Choice(names),
        default=current if current in names else agents.default_agent_name(),
        show_choices=True,
    )
    agent_prefs.save_default_agent(chosen)
    display = agents.get_agent(chosen).display

    # Re-run idempotency: probe content-level state first (skill_status,
    # against the chosen agent's directory) and expose --force as a prompt
    # only where it changes anything, so wizard users can refresh skills
    # after a litman upgrade without dropping to the standalone command
    # (feedback_wizard_mirrors_command_flags): up-to-date installs
    # auto-skip, stale ones prompt [Y/n] default Y.
    statuses = skill_status(
        parent_dir=agents.agent_skills_parent_dir(chosen)
    )
    bundled = set(statuses)
    already = {
        name
        for name, info in statuses.items()
        if info["state"] != "absent"
    }
    stale = sorted(
        name
        for name, info in statuses.items()
        if info["state"] == "stale"
    )
    if already:
        if already >= bundled:
            if not stale:
                console.print(
                    f"[dim]Skills already installed and up to date "
                    f"({', '.join(sorted(already))}) — nothing to do. "
                    f"(lit install-skill --force re-copies them "
                    f"regardless.)[/]"
                )
                skipped.append("skill (up to date)")
                _sweep_other_agent_skills(chosen, did)
                return
            console.print(
                f"[dim]Skills installed but out of date with this litman "
                f"({', '.join(stale)}).[/]"
            )
            if not click.confirm(
                "Refresh them with the bundled version? "
                "(files you added are kept)",
                default=True,
            ):
                skipped.append("skill (stale, refresh declined)")
                return
            ctx.invoke(install_skill_cmd, agent_name=chosen, force=True)
            did.append(f"skill (refreshed, {display})")
            _sweep_other_agent_skills(chosen, did)
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
        ctx.invoke(install_skill_cmd, agent_name=chosen, force=True)
        did.append(f"skill (refreshed, {display})")
        _sweep_other_agent_skills(chosen, did)
        return

    if not click.confirm(
        f"Install the {display} agent skill now?", default=True
    ):
        skipped.append("skill (declined)")
        return
    ctx.invoke(install_skill_cmd, agent_name=chosen)
    did.append(f"skill ({display})")
    _sweep_other_agent_skills(chosen, did)


def _step_vault(
    ctx: click.Context, did: list[str], skipped: list[str]
) -> None:
    console.rule("[bold]Step 3/5 — create a vault")
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
    console.rule("[bold]Step 4/5 — cloud sync")
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


def _step_shortcut(
    ctx: click.Context, did: list[str], skipped: list[str]
) -> None:
    console.rule("[bold]Step 5/5 — desktop shortcut")
    target = shortcut_path()
    if target.exists():
        console.print(
            f"[dim]Desktop shortcut already exists ({target}); skipping. "
            "Re-create anytime: [bold]lit gui --make-shortcut[/][/]"
        )
        skipped.append("shortcut (already exists)")
        return
    if not display_available():
        console.print(
            "[dim]No graphical display in this session; skipping. Run "
            "[bold]lit gui --make-shortcut[/] from a desktop session.[/]"
        )
        skipped.append("shortcut (headless session)")
        return
    if click.confirm(
        "Create a desktop shortcut? (runs: lit gui --make-shortcut)",
        default=True,
    ):
        ctx.invoke(gui_cmd, make_shortcut=True)
        did.append("desktop shortcut")
    else:
        skipped.append("shortcut (declined)")


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
        "[bold]Next:[/] add your first paper — lit add <pdf> --doi <doi> "
        "(or open the interface with lit gui).",
        "",
        "[dim]Re-run any step directly anytime: lit install-completion / "
        "lit install-skill / lit init / lit sync setup / "
        "lit gui --make-shortcut.[/]",
    ]
    console.print(
        Panel.fit("\n".join(lines), title="lit setup", border_style="green")
    )
