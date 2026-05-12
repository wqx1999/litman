"""``lit install-skill`` — copy bundled Claude Code skills (M4.3 + M9.2).

Default behaviour installs **every** bundled skill (currently
``lit-library`` + ``lit-reading``) into ``~/.claude/skills/<name>/``,
each under its own subdir, so re-running this command after a litman
update picks up newly-added skills. Use ``--skill <name>`` to install
just one.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.core.skill import (
    DEFAULT_PARENT_DIR,
    install_all_skills,
    install_skill,
    list_bundled_skills,
)

console = Console()


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
    "--parent-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=DEFAULT_PARENT_DIR,
    show_default=True,
    help=(
        "Parent directory under which each skill gets its own subdir. "
        "Default puts skills where Claude Code auto-discovers them."
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
def install_skill_cmd(
    skill_name: str | None, parent_dir: Path, force: bool
) -> None:
    """Install the bundled litman Claude Code skills.

    Currently bundled: ``lit-library`` (vault write / management) and
    ``lit-reading`` (paper-discussion read companion). Both are
    **optional** — the ``lit`` CLI is fully usable without any skill,
    but installing them makes agent-mediated workflows nicer.

    Running this command does NOT install Claude Code itself, configure
    API keys, or modify any of the user's other skills. It only copies
    files into ``--parent-dir``.
    """
    if skill_name is not None:
        results = [
            install_skill(
                target=parent_dir / skill_name,
                overwrite=force,
                name=skill_name,
            )
        ]
    else:
        results = install_all_skills(
            parent_dir=parent_dir, overwrite=force
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
    available = ", ".join(list_bundled_skills())
    lines.append("")
    lines.append(
        "[dim]Next:[/] open a new Claude Code session — the agent will "
        "pick up the skill(s) via their frontmatter. "
        "(No restart needed; Claude Code scans the skills dir on each "
        "session start.)"
    )
    lines.append(f"[dim]Bundled skills available: {escape(available)}[/]")

    console.print(
        Panel.fit(
            "\n".join(lines),
            title="lit install-skill",
            border_style="green",
        )
    )
