"""Agent onboarding + launch API for the litman webUI (task-agent-onboarding,
building on task-agent-launch; ADR-020 / ADR-021).

Endpoints:

* ``GET  /api/agents``            — supported launchable agents + resolved default
* ``POST /api/agent/launch``      — open the named agent in a terminal at the vault
* ``GET  /api/agent/status``      — the red-dot / setup-panel data source; the
  server computes ``needs_setup`` (single source of truth for the state machine)
* ``POST /api/agent/skill/install`` — install the named agent's skill
* ``PUT  /api/agent/default``     — record the machine-level default agent

The agent set + launch commands + install targets all come from the code-level
catalog (:mod:`litman.core.agents`); the chosen default comes from the
machine-level preferences (:mod:`litman.core.agent_prefs`). Nothing agent
config comes from the vault anymore (D0 retired per-vault ``agents:``).

RED LINE (ADR-020): every endpoint reads an agent NAME only. The launch
command, the skill install target, and the set of installable agents come
exclusively from the server-side catalog; any ``command`` / ``target`` /
``path`` field in a request body is ignored and never executed — the
localhost-bound server must not become a remote-code-execution surface.

RED LINE: the Claude-specific ``~/.claude/skills`` path stays inside the
catalog's claude adapter — it never appears in a response or the status
contract, so new agents can be added without changing this file's shape.

None of these endpoints write to the vault: the skill install writes
``~/.claude/skills`` and the default write goes to the machine-level
``preferences.yaml`` — neither is a TRUTH/DERIVED vault surface, so
invariant #16 (the WebUI structured-write whitelist) does not apply and there
is no drift-ledger pair to register.
"""

from __future__ import annotations

import shlex

from fastapi import APIRouter, HTTPException, Request, Response

from litman.core import agent_prefs, agents, terminal

router = APIRouter(prefix="/api")


def _resolved_default() -> str:
    """The machine-level default agent, falling back to the catalog default."""
    return agent_prefs.load_default_agent() or agents.default_agent_name()


async def _body_agent_name(request: Request) -> str | None:
    """Read ``body["agent"]`` (a string) or ``None``.

    Mirrors the RCE-safe body parse: only the ``agent`` name is ever read; a
    malformed body 400s, a non-string ``agent`` 400s, and every other field
    (``command`` / ``target`` / ``path`` / …) is ignored.
    """
    body = await request.body()
    if not body:
        return None
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if isinstance(payload, dict) and payload.get("agent") is not None:
        name = payload["agent"]
        if not isinstance(name, str):
            raise HTTPException(status_code=400, detail="'agent' must be a string.")
        return name
    return None


@router.get("/agents")
def get_agents() -> dict[str, object]:
    """The launchable (supported) agent names + the resolved default."""
    return {
        "agents": [spec.name for spec in agents.supported_agents()],
        "default": _resolved_default(),
    }


@router.post("/agent/launch")
async def launch_agent(request: Request) -> dict[str, object]:
    """Launch a catalog agent in a terminal window at the vault.

    Body JSON (optional): ``{"agent": "<name>"}`` — an absent body / key
    launches the resolved default. ONLY the name is read; the command comes
    from the catalog, never from the request.
    """
    default = _resolved_default()
    agent_name = (await _body_agent_name(request)) or default

    spec = agents.get_agent(agent_name)
    if spec is None:
        known = ", ".join(a.name for a in agents.AGENTS)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown agent '{agent_name}'. Known agents: {known}.",
        )
    # Greyed catalog placeholders (supported=False) are inert on every axis —
    # reject launch before any PATH probe / copy-fallback (data-driven, no
    # per-agent branch). The copy-fallback below is only for a supported agent
    # whose binary isn't on PATH yet.
    if not spec.supported:
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent_name}' is not available yet.",
        )

    # Module-attribute access (not a from-import) so tests can stub the spawn
    # at its home module — the invariant-#5 purge test drops litman.server*
    # from sys.modules, orphaning any name copied at import.
    argv = shlex.split(spec.launch)
    if argv and terminal.spawn_terminal(argv, request.app.state.vault):
        return {
            "ok": True,
            "mode": "spawned",
            "agent": agent_name,
            "command": spec.launch,
        }

    # Copy fallback: hand back the `lit agent` wrapper (correct from any cwd),
    # not the raw command (which is only correct inside the vault).
    lit_line = "lit agent" if agent_name == default else f"lit agent {agent_name}"
    return {"ok": True, "mode": "copy", "agent": agent_name, "command": lit_line}


@router.get("/agent/status")
def agent_status(response: Response) -> dict[str, object]:
    """Single data source for the GUI red dot + setup panel.

    ``needs_setup`` is the red-dot condition, computed server-side (the client
    never re-derives the state machine):

        needs_setup == NOT( default chosen AND supported AND detected AND
                            skill_installed )

    ``Cache-Control: no-store`` is mandatory: ``detected`` (is the agent binary
    on PATH?) and ``skill_installed`` are live machine state that flips the
    moment the user installs the agent CLI or its skill in a terminal. Without
    it the browser serves a cached "not installed" body, so even a plain reload
    keeps showing the red dot until a hard refresh (same reason paper.pdf sets
    it — the resource is mutable). Localhost, so caching buys nothing anyway.
    """
    response.headers["Cache-Control"] = "no-store"
    entries = [
        {
            "name": spec.name,
            "display": spec.display,
            "supported": spec.supported,
            "detected": agents.detect(spec),
            "install_url": spec.install_url,
        }
        for spec in agents.AGENTS
    ]

    default = agent_prefs.load_default_agent()

    # skill_installed reports the *resolved* default's skill so the panel can
    # show "Ready" vs "Install skill" even before the user has committed a
    # default. Only a supported agent has a skill adapter to probe.
    probe = agents.get_agent(default or agents.default_agent_name())
    skill_installed = bool(probe and probe.supported and probe.skill_installed())

    default_spec = agents.get_agent(default) if default else None
    detected = bool(default_spec and agents.detect(default_spec))
    needs_setup = not (
        default is not None
        and default_spec is not None
        and default_spec.supported
        and detected
        and skill_installed
    )

    return {
        "agents": entries,
        "default": default,
        "skill_installed": skill_installed,
        "needs_setup": needs_setup,
    }


@router.post("/agent/skill/install")
async def install_agent_skill(request: Request) -> dict[str, object]:
    """Install the named agent's skill (default: the resolved default agent).

    Body JSON (optional): ``{"agent": "<name>"}`` — ONLY the name is read; the
    install target lives entirely in the catalog adapter (RED LINE). Unknown
    or unsupported names 400.
    """
    name = (await _body_agent_name(request)) or _resolved_default()
    spec = agents.get_agent(name)
    if spec is None or not spec.supported:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot install a skill for '{name}': not a supported agent.",
        )

    # Adapter returns per-unit install summaries; aggregate to a JSON-able
    # shape WITHOUT the per-skill ``target`` Path (keeps ~/.claude/skills out
    # of the response contract).
    results = spec.install_skill()
    files = [f for result in results for f in result.get("files", [])]
    mode = (
        "overwritten"
        if any(result.get("mode") == "overwritten" for result in results)
        else "created"
    )
    return {"ok": True, "agent": name, "files": files, "mode": mode}


@router.put("/agent/default")
async def set_default_agent(request: Request) -> dict[str, object]:
    """Record the machine-level default agent.

    Body JSON: ``{"agent": "<name>"}``. Unknown / unsupported name → 400
    (``save_default_agent`` validates against the catalog).
    """
    name = await _body_agent_name(request)
    if name is None:
        raise HTTPException(
            status_code=400,
            detail='Body must name an agent: {"agent": "<name>"}.',
        )
    try:
        agent_prefs.save_default_agent(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "default": name}
