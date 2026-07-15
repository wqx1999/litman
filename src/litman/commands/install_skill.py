"""``lit install-skill`` — copy the bundled agent skills (M4.3 + M9.2).

Default behaviour installs **every** bundled skill (currently
``lit-library`` + ``lit-reading``) into the skills directory of the
machine-level default agent, each skill under its own subdir. ``--agent
<name>`` targets another supported agent's directory instead;
``--parent-dir <path>`` is the manual escape hatch (mutually exclusive
with ``--agent``). Use ``--skill <name>`` to install just one skill.

Re-running the command is the upgrade path and is drift-aware: an
installed skill whose content matches this litman's bundle reports
"up to date" and exits 0; a stale one prompts for a refresh ([Y/n],
interactive runs) or asks for ``--force`` (non-interactive runs, so an
agent or script never overwrites silently). A linked skill dir
(symlink / junction to a dev checkout) is always left untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.core import agent_prefs, agents
from litman.core.skill import (
    SkillInstallError,
    bundled_skill_root,
    install_skill,
    list_bundled_skills,
    skill_status,
)

console = Console()


def _stdin_is_tty() -> bool:
    """Seam for tests (mirrors ``litman.commands.setup._stdin_is_tty``)."""
    return sys.stdin.isatty()


@click.command("install-skill")
@click.option(
    "--skill",
    "skill_name",
    type=str,
    default=None,
    help=(
        "Install only this bundled skill (e.g. 'lit-library', "
        "'lit-reading'). Default: install every bundled skill."
    ),
)
@click.option(
    "--agent",
    "agent_name",
    type=click.Choice([spec.name for spec in agents.supported_agents()]),
    default=None,
    help=(
        "Install into this agent's skills directory. Default: the "
        "machine-level default agent (lit agent --set-default). "
        "Mutually exclusive with --parent-dir."
    ),
)
@click.option(
    "--parent-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help=(
        "Parent directory under which each skill gets its own subdir. "
        "Default follows the default agent's directory, where that agent "
        "auto-discovers skills. Mutually exclusive with --agent."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Overwrite files inside an existing target directory without "
        "asking. Files in the target that are NOT part of the bundled "
        "skill are left in place (defensive: any local additions are "
        "preserved)."
    ),
)
def install_skill_cmd(
    skill_name: str | None,
    agent_name: str | None,
    parent_dir: Path | None,
    force: bool,
) -> None:
    """Install the bundled litman agent skills.

    Currently bundled: lit-library (vault write / management) and
    lit-reading (paper-discussion read companion). Both are
    **optional** — the lit CLI is fully usable without any skill,
    but installing them makes agent-mediated workflows nicer.

    Skills land in the directory your default agent discovers; --agent
    targets another supported agent's directory instead.

    Safe to re-run after a litman upgrade: skills that already match
    this litman's bundled content are reported up to date, out-of-date
    ones are offered a refresh.

    Running this command does NOT install the agent itself, configure
    API keys, or modify any of the user's other skills. It only copies
    files into the chosen directory.
    """
    if agent_name is not None and parent_dir is not None:
        raise click.UsageError(
            "--agent and --parent-dir are mutually exclusive; pass at "
            "most one."
        )
    if parent_dir is None:
        resolved = agent_name or (
            agent_prefs.load_default_agent() or agents.default_agent_name()
        )
        try:
            parent_dir = agents.agent_skills_parent_dir(resolved)
        except ValueError as exc:
            # Only reachable for a bare run whose recorded default is not a
            # supported catalog agent (hand-edited preferences) — --agent is
            # already validated by click.Choice.
            raise click.UsageError(str(exc)) from exc

    if skill_name is not None:
        bundled_skill_root(skill_name)  # unknown name → SkillInstallError
        names = [skill_name]
    else:
        names = list_bundled_skills()

    statuses = skill_status(parent_dir=parent_dir)

    # Non-interactive runs must never overwrite silently, and must not
    # half-install before failing: refuse up front if any target is stale.
    if not force:
        stale = [n for n in names if statuses[n]["state"] == "stale"]
        if stale and not _stdin_is_tty():
            raise SkillInstallError(
                f"Skill(s) out of date with this litman install: "
                f"{', '.join(stale)}. Pass --force to refresh (bundled "
                "files are overwritten; files you added are kept)."
            )

    results = []
    up_to_date: list[str] = []
    linked: list[str] = []
    declined: list[str] = []
    for name in names:
        state = statuses[name]["state"]
        if state == "linked":
            linked.append(name)
            continue
        if not force and state == "current":
            up_to_date.append(name)
            continue
        if not force and state == "stale":
            if not click.confirm(
                f"Skill '{name}' is out of date with this litman — "
                "refresh it? (bundled files are overwritten; files you "
                "added are kept)",
                default=True,
            ):
                declined.append(name)
                continue
        results.append(
            install_skill(
                target=parent_dir / name,
                overwrite=(state != "absent"),
                name=name,
            )
        )

    lines = []
    for r in results:
        lines.append(
            f"[bold green]Skill {r['mode']}:[/] {escape(r['name'])}"
        )
        lines.append(f"  [dim]Target:[/] {r['target']}")
        lines.append(
            f"  [dim]Files:[/] "
            f"{', '.join(escape(f) for f in r['files'])}"
        )
    for name in up_to_date:
        lines.append(
            f"[bold green]Skill up to date:[/] {escape(name)}"
        )
    for name in linked:
        lines.append(
            f"[yellow]Skill linked (managed elsewhere), left "
            f"untouched:[/] {escape(name)}"
        )
    for name in declined:
        lines.append(f"[dim]Skill skipped:[/] {escape(name)}")
    available = ", ".join(list_bundled_skills())
    lines.append("")
    lines.append(
        "[dim]Next:[/] open a new agent session — skills are discovered "
        "on session start."
    )
    lines.append(f"[dim]Bundled skills available: {escape(available)}[/]")

    console.print(
        Panel.fit(
            "\n".join(lines),
            title="lit install-skill",
            border_style="green",
        )
    )
