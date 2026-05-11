"""``lit install-skill`` — copy the bundled lit-library skill (M4.3)."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.core.skill import (
    DEFAULT_TARGET,
    SKILL_NAME,
    install_skill,
)

console = Console()


@click.command("install-skill")
@click.option(
    "--target",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=DEFAULT_TARGET,
    show_default=True,
    help=(
        "Where to install the lit-library skill. Default puts it where "
        "Claude Code auto-discovers user-level skills."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Overwrite files inside an existing target directory. Files in "
        "the target that are NOT part of the bundled skill are left in "
        "place (defensive: any local additions are preserved)."
    ),
)
def install_skill_cmd(target: Path, force: bool) -> None:
    """Install the bundled lit-library Claude Code skill.

    The skill teaches Claude Code how to drive the ``lit`` CLI: read a
    PDF, extract metadata as JSON, call ``lit add --from-llm-json``,
    bind code repos, browse the vault, manage the taxonomy. This is an
    **optional** convenience layer — the CLI is fully usable without
    the skill, but installing it makes agent-mediated workflows nicer.

    Running this command does NOT install Claude Code itself, configure
    API keys, or modify any of the user's other skills. It only copies
    files into ``--target``.
    """
    result = install_skill(target=target, overwrite=force)

    body = (
        f"[bold green]Skill {result['mode']}:[/] {escape(SKILL_NAME)}\n"
        f"[dim]Target:[/] {result['target']}\n"
        f"[dim]Files:[/] {', '.join(escape(f) for f in result['files'])}\n\n"
        "[dim]Next:[/] open a new Claude Code session in any directory; "
        f"the agent will pick up the skill via its frontmatter. "
        "(No restart needed — Claude Code scans the skills dir on each "
        "session start.)"
    )
    console.print(
        Panel.fit(body, title="lit install-skill", border_style="green")
    )
