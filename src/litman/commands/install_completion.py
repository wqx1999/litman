"""``lit install-completion <shell>`` — wire shell tab-completion (M11).

Click 8 already emits per-shell completion scripts via the protocol::

    _LIT_COMPLETE=<shell>_source lit

For bash and zsh, the canonical way to enable completion is to ``eval`` the
source command from the shell's startup file (``~/.bashrc`` / ``~/.zshrc``);
fish keeps shell completion files in ``~/.config/fish/completions/<cmd>.fish``
and sources them automatically. This command does the writing for the user
so they do not have to copy the magic string from documentation.

Idempotent: each install marks its block with the sentinel comment
``# lit-completion (do not edit)`` so reruns detect the existing block by
substring rather than exact whitespace match. If found, the install is a
no-op (the eval line / source command is not duplicated).

Not bundled with ``lit install-skill``: completion has no relationship to
Claude Code skills and runs entirely inside the user's shell.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.exceptions import LitmanError

console = Console()

SENTINEL = "# lit-completion (do not edit)"

SUPPORTED_SHELLS: tuple[str, ...] = ("bash", "zsh", "fish")


class _ShellPlan(NamedTuple):
    """Where the completion block lives, and what to write."""

    target_path: Path
    block: str
    description: str


def _build_plan(shell: str, home: Path) -> _ShellPlan:
    """Materialise the per-shell write plan."""
    if shell == "bash":
        return _ShellPlan(
            target_path=home / ".bashrc",
            block=(
                f"\n{SENTINEL}\n"
                'eval "$(_LIT_COMPLETE=bash_source lit)"\n'
            ),
            description="appended to ~/.bashrc",
        )
    if shell == "zsh":
        return _ShellPlan(
            target_path=home / ".zshrc",
            block=(
                f"\n{SENTINEL}\n"
                'eval "$(_LIT_COMPLETE=zsh_source lit)"\n'
            ),
            description="appended to ~/.zshrc",
        )
    if shell == "fish":
        return _ShellPlan(
            target_path=home / ".config" / "fish" / "completions" / "lit.fish",
            block=(
                f"{SENTINEL}\n"
                "_LIT_COMPLETE=fish_source lit | source\n"
            ),
            description="written to ~/.config/fish/completions/lit.fish",
        )
    raise LitmanError(
        f"Unsupported shell {shell!r}. "
        f"Supported: {', '.join(SUPPORTED_SHELLS)}."
    )


def _install_block(plan: _ShellPlan) -> bool:
    """Append (bash/zsh) or write (fish) the block. Returns True iff a write
    happened (False = idempotent no-op because the sentinel was already
    present).
    """
    target = plan.target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if SENTINEL in existing:
            return False
        new_content = existing
        if not new_content.endswith("\n"):
            new_content += "\n"
        new_content += plan.block
        target.write_text(new_content, encoding="utf-8")
        return True
    target.write_text(plan.block, encoding="utf-8")
    return True


@click.command("install-completion")
@click.argument(
    "shell",
    type=click.Choice(SUPPORTED_SHELLS, case_sensitive=False),
)
def install_completion_cmd(shell: str) -> None:
    """Install lit shell tab-completion for the current user.

    Supported shells: bash / zsh / fish. For bash/zsh the
    completion eval line is appended to ~/.bashrc / ~/.zshrc; for
    fish a self-sourcing snippet lands in
    ~/.config/fish/completions/lit.fish. Idempotent — re-running detects
    the existing block via a sentinel comment and skips the rewrite.

    Restart the shell (or run source ~/.zshrc etc.) to activate
    completion. After that, lit show <Tab> lists paper ids in the
    active vault.
    """
    shell = shell.lower()
    home = Path.home()
    plan = _build_plan(shell, home)
    written = _install_block(plan)

    if written:
        lines = [
            f"[bold green]Completion installed for {shell}.[/]",
            f"[dim]File:[/] {plan.target_path}",
            "",
            f"Activate: restart your shell, or `source {plan.target_path}`.",
            "Test: `lit show <Tab>` should list paper ids in the active vault.",
        ]
    else:
        lines = [
            f"[yellow]Completion already installed for {shell}.[/]",
            f"[dim]File:[/] {plan.target_path}",
            f"[dim]Sentinel:[/] {SENTINEL}",
            "",
            "No changes made (re-running this command is safe).",
        ]

    console.print(
        Panel.fit(
            "\n".join(lines),
            title=f"lit install-completion {escape(shell)}",
            border_style="green" if written else "yellow",
        )
    )
