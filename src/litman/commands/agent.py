"""``lit agent [NAME]`` — start the configured AI agent inside the vault.

One command replaces the manual open-terminal → cd-to-vault → run-agent
ritual: resolve the active vault, look NAME up in the config's ``agents``
map (default: ``default_agent``), and hand the process over to that command
with the vault as working directory.

ADR-020: an agent is nothing but a configurable command line — adding
another agent CLI is one config entry, never code. On POSIX the launch is a
true ``exec`` (process replacement), so Ctrl-C / exit semantics belong to
the agent, not to a lingering ``lit`` shell; Windows has no exec, so it
falls back to a child process whose exit code is passed through.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import click

from litman.core.config import load_config
from litman.core.library import find_vault, resolve_library_or_vault
from litman.exceptions import LitmanError


@click.command("agent")
@click.argument("name", required=False, default=None)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help=(
        "Override the active vault. Discovery order: this flag / $LIT_LIBRARY, "
        "then the active registered vault, then cwd-walk."
    ),
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
def agent_cmd(name: str | None, library: Path | None, vault_name: str | None) -> None:
    """Start your AI agent in the vault directory.

    Runs the command configured for NAME in lit-config.yaml's ``agents:``
    map (no NAME uses ``default_agent``) with the active vault as working
    directory. Add an agent by adding one line under ``agents:``.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    cfg = load_config(vault)
    agent_name = name or cfg.default_agent
    cmd_str = cfg.agents.get(agent_name)
    if cmd_str is None:
        configured = ", ".join(sorted(cfg.agents)) or "(none)"
        raise LitmanError(
            f"Unknown agent '{agent_name}'. Configured agents: {configured}. "
            "Add it under 'agents:' in lit-config.yaml."
        )
    argv = shlex.split(cmd_str)
    if not argv:
        raise LitmanError(
            f"Agent '{agent_name}' has an empty command in lit-config.yaml — "
            "set it to the command line that starts the agent."
        )
    if shutil.which(argv[0]) is None:
        raise LitmanError(
            f"Agent command '{argv[0]}' not found on PATH — is it installed?"
        )

    if sys.platform == "win32":
        # No real exec on Windows: run as a child and pass its exit code on.
        result = subprocess.run(argv, cwd=vault)
        sys.exit(result.returncode)

    os.chdir(vault)
    os.execvp(argv[0], argv)
