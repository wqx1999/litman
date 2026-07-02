"""``lit uninstall`` — reverse ``lit setup`` (teardown counterpart).

Removes the artifacts ``lit setup`` placed OUTSIDE the pipx venv:

* the bundled Claude Code skills (``~/.claude/skills/lit-*``),
* the shell tab-completion block(s),
* the vault registry (``vaults.yaml`` — the list of vault names/paths).

It deliberately does NOT remove the ``lit`` CLI itself: a running command
cannot cleanly delete the pipx environment it is executing from, so the
final ``pipx uninstall litman`` step is printed for the user to run. It
also NEVER touches vault data — papers, PDFs, notes and annotations stay
exactly where they are; only the registry pointers to them are dropped.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.commands.install_completion import (
    SUPPORTED_SHELLS,
    completion_installed,
    uninstall_completion,
)
from litman.core.skill import installed_skill_names, uninstall_skill
from litman.core.vault_registry import registry_path, remove_registry

console = Console()

_PIPX_STEP = (
    "Remove the CLI itself (a running command can't delete its own\n"
    "pipx environment):\n"
    "  [bold]pipx uninstall litman[/]"
)

_VAULT_SAFE = (
    "[dim]Your vault directories (papers, PDFs, notes, annotations) are "
    "NOT touched.[/]"
)


@click.command("uninstall")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be removed; change nothing.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
def uninstall_cmd(dry_run: bool, yes: bool) -> None:
    """Remove what `lit setup` installed, except the CLI and your vaults.

    Reverses the setup wizard: deletes the bundled Claude Code skills, the
    shell tab-completion block, and the vault registry (the list of vault
    names/paths — NOT the vaults themselves). Your papers, PDFs, notes and
    annotations are never touched.

    This does NOT uninstall the lit CLI, because a running command can't
    delete its own pipx environment. Finish with: pipx uninstall litman.

    Use --dry-run to preview, -y/--yes to skip the confirmation.
    """
    home = Path.home()
    skills_parent = home / ".claude" / "skills"

    skills = sorted(installed_skill_names(skills_parent))
    shells = [s for s in SUPPORTED_SHELLS if completion_installed(s, home)]
    reg = registry_path()
    reg_present = reg.is_file()

    plan_lines: list[str] = []
    if skills:
        plan_lines.append(f"[bold]Claude Code skills[/] [dim]({skills_parent})[/]:")
        plan_lines += [f"  [red]•[/] {escape(name)}" for name in skills]
    if shells:
        plan_lines.append("[bold]Shell completion:[/]")
        plan_lines += [f"  [red]•[/] {escape(s)}" for s in shells]
    if reg_present:
        plan_lines.append(
            "[bold]Vault registry[/] [dim](de-registers vaults; data kept)[/]:"
        )
        plan_lines.append(f"  [red]•[/] {escape(str(reg))}")

    if not plan_lines:
        console.print(
            Panel.fit(
                "Nothing to remove — no bundled skills, shell completion, or "
                "vault registry found.\n\n" + _PIPX_STEP,
                title="lit uninstall",
                border_style="yellow",
            )
        )
        return

    console.print(
        Panel.fit(
            "\n".join(plan_lines) + "\n\n" + _VAULT_SAFE,
            title="lit uninstall — would remove" if dry_run else "lit uninstall",
            border_style="red",
        )
    )

    if dry_run:
        console.print("[dim](dry run — nothing was changed.)[/]")
        console.print(Panel.fit(_PIPX_STEP, border_style="cyan"))
        return

    if not yes:
        click.confirm("Remove the items listed above?", default=False, abort=True)

    done: list[str] = []
    for name in skills:
        result = uninstall_skill(name, skills_parent)
        if result["mode"] == "removed":
            done.append(f"skill {name}")
        elif result["mode"] == "kept":
            leftover = result["leftover"]
            assert isinstance(leftover, list)
            done.append(
                f"skill {name} (bundled files removed; kept "
                f"{len(leftover)} user file(s) + dir)"
            )
        elif result["mode"] == "skipped":
            done.append(f"skill {name} (skipped — symlinked dir left in place)")
    for shell in shells:
        uninstall_completion(shell, home)
        done.append(f"completion ({shell})")
    if reg_present and remove_registry()["removed"]:
        done.append("vault registry")

    out = ["[bold green]Removed:[/]"]
    out += [f"  [green]•[/] {escape(x)}" for x in done]
    out += ["", _VAULT_SAFE, "", _PIPX_STEP]
    console.print(
        Panel.fit("\n".join(out), title="lit uninstall", border_style="green")
    )
