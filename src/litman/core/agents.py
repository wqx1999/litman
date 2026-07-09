"""Agent catalog — the code-level source of truth for the AI agents litman
knows how to launch and onboard (task-agent-onboarding, ADR-020 / ADR-021).

The catalog is a frozen table: one :class:`AgentSpec` per agent carrying its
display name, launch command, official install URL, a detection binary, and
two skill-adapter callables. Every per-agent difference lives in this data;
consumers (the ``lit agent`` CLI, the GUI agent button, the ``/api/agent/*``
endpoints) iterate the catalog generically — there is deliberately no
``if name == "claude"`` branch anywhere (red line: zero per-agent code).

v1.1.1 ships exactly one *supported* agent, Claude Code. The other four
(Codex / Cursor / Gemini CLI / OpenCode) exist here as ``supported=False``
placeholders so the picker renders a stable, greyed-out roadmap and so the
seam is already N-agent shaped; v1.1.2 fills in their real adapters. Their
adapter callables raise :class:`NotImplementedError` — generic code never
reaches them (every consumer gates on ``supported``), so a programming error
that *does* call one fails loudly instead of silently misbehaving.

The Claude-Code-specific ``~/.claude/skills`` location is reached ONLY through
the claude adapter's callables (they delegate to :mod:`litman.core.skill`); it
must never leak into an endpoint, the ``/api/agent/status`` contract, or the
frontend. That agent-agnostic boundary is what keeps v1.1.2 cheap.
"""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from litman.core.skill import install_all_skills, installed_skill_names

# Fallback default when the user has not chosen one (no per-vault override
# anymore — the machine-level preferences.yaml or this constant decide).
_DEFAULT_AGENT_NAME = "claude"


def _unsupported(name: str) -> Callable[..., Any]:
    """Build a skill-adapter callable for a not-yet-supported agent.

    ``supported=False`` agents are gated out of every generic consumer, so
    this is never called in normal operation; it exists only so a mis-call
    fails loudly (v1.1.2 replaces it with the real adapter).
    """

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError(
            f"Agent {name!r} has no skill adapter yet (supported=False) — it "
            "is a v1.1.2 placeholder. Implement its adapter before wiring it "
            "into onboarding."
        )

    return _raise


@dataclass(frozen=True)
class AgentSpec:
    """One agent's launch + onboarding metadata.

    Frozen: the catalog is a constant that consumers read, never mutate. The
    two adapter callables encapsulate everything agent-specific about skill
    detection / installation so callers stay agent-agnostic.
    """

    name: str
    display: str
    launch: str
    supported: bool
    install_url: str
    detect_bin: str
    skill_installed: Callable[[], bool]
    install_skill: Callable[[], Any]


# The one supported agent in v1.1.1. Its skill adapter reuses core.skill
# verbatim: "installed" == the bundled skill directories exist under
# ~/.claude/skills (installed_skill_names treats directory-presence as
# installed), and install == copy every bundled skill in, overwriting
# (install_all_skills). Those two callables are the ONLY place the
# ~/.claude/skills path is reachable.
#
# The four placeholders below carry best-effort launch commands / install
# URLs so v1.1.2 can implement their adapters without re-editing this table;
# verify each vendor's exact CLI + docs URL when un-greying it.
AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="claude",
        display="Claude Code",
        launch="claude",
        supported=True,
        install_url="https://docs.claude.com/en/docs/claude-code/overview",
        detect_bin="claude",
        skill_installed=lambda: bool(installed_skill_names()),
        install_skill=lambda: install_all_skills(overwrite=True),
    ),
    AgentSpec(
        name="codex",
        display="Codex",
        launch="codex",
        supported=False,
        install_url="https://developers.openai.com/codex/cli/",
        detect_bin="codex",
        skill_installed=_unsupported("codex"),
        install_skill=_unsupported("codex"),
    ),
    AgentSpec(
        name="cursor",
        display="Cursor",
        launch="cursor-agent",
        supported=False,
        install_url="https://cursor.com/cli",
        detect_bin="cursor-agent",
        skill_installed=_unsupported("cursor"),
        install_skill=_unsupported("cursor"),
    ),
    AgentSpec(
        name="gemini",
        display="Gemini CLI",
        launch="gemini",
        supported=False,
        install_url="https://github.com/google-gemini/gemini-cli",
        detect_bin="gemini",
        skill_installed=_unsupported("gemini"),
        install_skill=_unsupported("gemini"),
    ),
    AgentSpec(
        name="opencode",
        display="OpenCode",
        launch="opencode",
        supported=False,
        install_url="https://opencode.ai/",
        detect_bin="opencode",
        skill_installed=_unsupported("opencode"),
        install_skill=_unsupported("opencode"),
    ),
)


def get_agent(name: str) -> AgentSpec | None:
    """Return the catalog entry named ``name``, or ``None`` if unknown."""
    for spec in AGENTS:
        if spec.name == name:
            return spec
    return None


def supported_agents() -> list[AgentSpec]:
    """Return the catalog entries that are launchable / onboardable today."""
    return [spec for spec in AGENTS if spec.supported]


def default_agent_name() -> str:
    """The catalog fallback default when the user has not chosen one."""
    return _DEFAULT_AGENT_NAME


def detect(spec: AgentSpec) -> bool:
    """Is ``spec``'s command present on PATH?

    Generic, data-driven — no per-agent branching. Probes ``detect_bin`` if
    set, otherwise the first token of the launch command.
    """
    probe = spec.detect_bin or shlex.split(spec.launch)[0]
    return shutil.which(probe) is not None
