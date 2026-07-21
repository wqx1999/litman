"""``lit agent [NAME]`` — start the configured AI agent inside the vault.

One command replaces the manual open-terminal → cd-to-vault → run-agent
ritual: resolve the active vault, resolve which agent to launch, and hand the
process over to that agent's command with the vault as working directory.

Which agent runs is resolved machine-globally, not per-vault (ADR-020 /
ADR-021 — "nobody picks their agent per library"): an explicit NAME wins,
else the machine-level default in ``preferences.yaml``
(:mod:`litman.core.agent_prefs`), else the catalog fallback. The launch
command itself comes from the code-level catalog
(:mod:`litman.core.agents`) — adding an agent CLI is one catalog entry, never
new command code.

On POSIX the launch is a true ``exec`` (process replacement), so Ctrl-C /
exit semantics belong to the agent, not a lingering ``lit`` shell; Windows has
no exec, so it falls back to a child process whose exit code is passed through.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import click

from litman.commands._options import library_option, vault_option
from litman.core import agent_prefs, agents
from litman.core.library import find_vault, resolve_library_or_vault
from litman.exceptions import LitmanError


@click.command("agent")
@click.argument("name", required=False, default=None)
@click.option(
    "--set-default",
    "set_default",
    default=None,
    metavar="NAME",
    help=(
        "Record NAME as the machine-level default agent (used by a bare "
        "`lit agent` and the GUI agent button), then exit without launching."
    ),
)
@library_option
@vault_option
def agent_cmd(
    name: str | None,
    set_default: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Start your AI agent in the vault directory.

    Runs the agent's command (from the built-in catalog) with the active vault
    as working directory. With no NAME it launches the machine-level default
    agent (set via `lit setup`, the GUI, or `--set-default`); otherwise the
    named catalog agent.
    """
    if set_default is not None:
        try:
            agent_prefs.save_default_agent(set_default)
        except ValueError as exc:
            raise LitmanError(str(exc)) from exc
        click.echo(f"Default agent set to '{set_default}'.")
        return

    vault = find_vault(resolve_library_or_vault(library, vault_name))
    agent_name = name or agent_prefs.load_default_agent() or agents.default_agent_name()
    spec = agents.get_agent(agent_name)
    if spec is None:
        known = ", ".join(a.name for a in agents.AGENTS)
        raise LitmanError(
            f"Unknown agent '{agent_name}'. Known agents: {known}."
        )
    # Greyed catalog placeholders (supported=False) are inert on every axis —
    # reject launch before the PATH probe (data-driven, no per-agent branch).
    if not spec.supported:
        supported = ", ".join(s.name for s in agents.supported_agents())
        raise LitmanError(
            f"Agent '{agent_name}' is not available yet. "
            f"Supported agents: {supported}."
        )
    # Windows installers update the registry PATH, not already-running
    # processes. Refresh it so this command also works from a shell that was
    # open before the agent CLI was installed.
    agents.refresh_windows_path()
    argv = shlex.split(spec.launch)
    if shutil.which(argv[0]) is None:
        raise LitmanError(
            f"Agent command '{argv[0]}' not found on PATH — is it installed? "
            f"Get {spec.display}: {spec.install_url}"
        )

    if sys.platform == "win32":
        # No real exec on Windows: run as a child and pass its exit code on.
        result = subprocess.run(argv, cwd=vault)
        sys.exit(result.returncode)

    os.chdir(vault)
    os.execvp(argv[0], argv)
