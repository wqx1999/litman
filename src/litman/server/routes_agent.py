"""Agent-launch API endpoints for the litman webUI (task-agent-launch).

``GET /api/agents`` lists the configured agent names; ``POST
/api/agent/launch`` opens the named agent's configured command in a native
terminal window at the vault. When no window can spawn (headless / remote
server, no terminal emulator), the response degrades to ``mode: "copy"``
carrying a ``lit agent …`` line for the user to paste into their own
terminal — which is the normal outcome on an HPC deployment.

RED LINE (ADR-020): the launch endpoint accepts an agent NAME only. The
command text always comes from the server-side vault config; any
command-like field in the request body is ignored and never executed — the
localhost-bound server must not become a remote-code-execution surface.

Neither endpoint writes to the vault (not a TRUTH/DERIVED surface;
invariant #16 does not apply).
"""

from __future__ import annotations

import shlex

from fastapi import APIRouter, HTTPException, Request

from litman.core import terminal
from litman.core.config import load_config

router = APIRouter(prefix="/api")


@router.get("/agents")
def get_agents(request: Request) -> dict[str, object]:
    """The configured agent names + which one is the default."""
    cfg = load_config(request.app.state.vault)
    return {"agents": list(cfg.agents), "default": cfg.default_agent}


@router.post("/agent/launch")
async def launch_agent(request: Request) -> dict[str, object]:
    """Launch a configured agent in a terminal window at the vault.

    Body JSON (optional): ``{"agent": "<name>"}`` — an absent body / key
    launches ``default_agent``. ONLY the name is read; every other field is
    ignored (the command comes from config, never from the request).
    """
    body = await request.body()
    payload: object = None
    if body:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Body must be JSON.") from exc

    cfg = load_config(request.app.state.vault)
    agent_name = cfg.default_agent
    if isinstance(payload, dict) and payload.get("agent") is not None:
        agent_name = payload["agent"]
        if not isinstance(agent_name, str):
            raise HTTPException(status_code=400, detail="'agent' must be a string.")

    cmd_str = cfg.agents.get(agent_name)
    if cmd_str is None:
        configured = ", ".join(sorted(cfg.agents)) or "(none)"
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown agent '{agent_name}'. Configured agents: {configured}."
            ),
        )

    # Module-attribute access (not a from-import) so tests can stub the spawn
    # at its home module — the invariant-#5 purge test drops litman.server*
    # from sys.modules, which orphans any name this module copied at import.
    argv = shlex.split(cmd_str)
    if argv and terminal.spawn_terminal(argv, request.app.state.vault):
        return {
            "ok": True,
            "mode": "spawned",
            "agent": agent_name,
            "command": cmd_str,
        }

    # Copy fallback: hand back the `lit agent` wrapper (correct from any cwd),
    # not the raw command (which is only correct inside the vault).
    lit_line = (
        "lit agent" if agent_name == cfg.default_agent else f"lit agent {agent_name}"
    )
    return {"ok": True, "mode": "copy", "agent": agent_name, "command": lit_line}
