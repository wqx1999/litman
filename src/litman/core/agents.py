"""Agent catalog — the code-level source of truth for the AI agents litman
knows how to launch and onboard (task-agent-onboarding, ADR-020 / ADR-021).

The catalog is a frozen table: one :class:`AgentSpec` per agent carrying its
display name, beginner-facing brand, launch command, official install URL, a
detection binary, and adapters for skill state, skill installation, and native
command approval.
Every per-agent difference lives in this data;
consumers (the ``lit agent`` CLI, the GUI agent button, the ``/api/agent/*``
endpoints) iterate the catalog generically — there is deliberately no
``if name == "claude"`` branch anywhere (red line: zero per-agent code).

litman supports five agents today. Two have a skills directory of their
own: Claude Code (``~/.claude/skills``) and Antigravity CLI (``agy``, whose
only user-installable skills location is its own app-data directory
``~/.gemini/antigravity-cli/skills`` — it does not read the open-standard
dir). The other three — Cursor, Codex, and OpenCode — all discover skills
from the Agent Skills open-standard directory ``~/.agents/skills`` and so
share one adapter shape (Cursor additionally does a compatibility read of
the Claude dir).

All five are ``supported=True`` — the catalog carries no greyed placeholder
today. The machinery for one nonetheless stays as dormant capability: the
``supported`` flag on :class:`AgentSpec`, the :func:`_unsupported` placeholder
adapter, and every consumer's ``supported`` gate. A future roadmap agent
litman cannot yet drive is added as a single ``supported=False`` row whose
adapter callables raise :class:`NotImplementedError` — generic code never
reaches them (every consumer gates on ``supported``), so a programming error
that *does* call one fails loudly instead of silently misbehaving.

The per-agent skills locations are reached ONLY through the catalog adapters
(they delegate to :mod:`litman.core.skill`); they must never leak into an
endpoint, the ``/api/agent/status`` contract, or the frontend. That
agent-agnostic boundary is what keeps adding an agent cheap.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from litman.core import agent_permissions, skill
from litman.core.skill import aggregate_skill_state, install_all_skills

# Fallback default when the user has not chosen one (no per-vault override
# anymore — the machine-level preferences.yaml or this constant decide).
_DEFAULT_AGENT_NAME = "claude"


def _unsupported(name: str) -> Callable[..., Any]:
    """Build a skill-adapter callable for a not-yet-supported agent.

    ``supported=False`` agents are gated out of every generic consumer, so
    this is never called in normal operation; it exists only so a mis-call
    fails loudly (the real adapter replaces it when that agent lands).
    """

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError(
            f"Agent {name!r} has no skill adapter yet (supported=False) — it "
            "is a roadmap placeholder. Implement its adapter before wiring it "
            "into onboarding."
        )

    return _raise


@dataclass(frozen=True)
class AgentSpec:
    """One agent's launch + onboarding metadata.

    Frozen: the catalog is a constant that consumers read, never mutate. The
    adapter callables encapsulate everything agent-specific about skill
    detection, installation, and ``lit`` command approval so callers stay
    agent-agnostic.
    """

    name: str
    display: str
    brand: str
    launch: str
    supported: bool
    install_url: str
    detect_bin: str
    skill_state: Callable[[], str]
    install_skill: Callable[[], Any]
    install_lit_permission: Callable[[], agent_permissions.PermissionResult]
    # Where this agent discovers skills, resolved at call time (so a
    # redirected $HOME / test patch on litman.core.skill.* is honored).
    # Catalog-internal: consumers go through agent_skills_parent_dir() /
    # skills_parent_dirs(), never read the path into an endpoint contract.
    skills_dir: Callable[[], Path] | None = None


# Every supported agent's skill adapter reuses core.skill verbatim:
# skill_state content-compares the installed copies against the bundle
# ("absent" — nothing installed / "stale" — installed but out of date with
# this litman / "current"), and install == copy every bundled skill in,
# overwriting (install_all_skills; linked dev-checkout dirs are left
# untouched). Command approval uses each agent's native permission store and
# never enables a global bypass. The adapter callables are the ONLY place the
# skills paths are reachable. The skill.<resolver>() calls go through the module attribute
# (not a from-import) so a single patch on litman.core.skill.* intercepts
# them — the test suite's skills-dir isolation depends on that.
#
# Codex and OpenCode share the open-standard ``~/.agents/skills`` directory
# with Cursor (the litman-bench harness measured both activating skills from
# it and driving litman through them), so their adapters are copied verbatim
# from Cursor's — no per-vendor resolver or generated config file.
#
# Order: claude first (the fallback default tops the picker), the rest
# alphabetical. The GUI picker and `skills_parent_dirs()` follow this order.
AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="claude",
        display="Claude Code",
        brand="Claude · Anthropic",
        launch="claude",
        supported=True,
        install_url="https://docs.claude.com/en/docs/claude-code/overview",
        detect_bin="claude",
        skill_state=lambda: aggregate_skill_state(),
        install_skill=lambda: install_all_skills(overwrite=True),
        install_lit_permission=(
            lambda: agent_permissions.install_claude_lit_permission()
        ),
        skills_dir=lambda: skill.default_skills_parent_dir(),
    ),
    AgentSpec(
        name="agy",
        display="Antigravity CLI",
        brand="Google",
        launch="agy",
        supported=True,
        install_url="https://antigravity.google/download#antigravity-cli",
        detect_bin="agy",
        skill_state=lambda: aggregate_skill_state(
            parent_dir=skill.antigravity_skills_parent_dir()
        ),
        install_skill=lambda: install_all_skills(
            parent_dir=skill.antigravity_skills_parent_dir(), overwrite=True
        ),
        install_lit_permission=(
            lambda: agent_permissions.install_antigravity_lit_permission()
        ),
        skills_dir=lambda: skill.antigravity_skills_parent_dir(),
    ),
    AgentSpec(
        name="codex",
        display="Codex",
        brand="ChatGPT · OpenAI",
        launch="codex",
        supported=True,
        install_url="https://developers.openai.com/codex/cli/",
        detect_bin="codex",
        skill_state=lambda: aggregate_skill_state(
            parent_dir=skill.standard_skills_parent_dir()
        ),
        install_skill=lambda: install_all_skills(
            parent_dir=skill.standard_skills_parent_dir(), overwrite=True
        ),
        install_lit_permission=(
            lambda: agent_permissions.install_codex_lit_permission()
        ),
        skills_dir=lambda: skill.standard_skills_parent_dir(),
    ),
    AgentSpec(
        name="cursor",
        display="Cursor",
        brand="Cursor AI",
        launch="cursor-agent",
        supported=True,
        install_url="https://cursor.com/cli",
        detect_bin="cursor-agent",
        skill_state=lambda: aggregate_skill_state(
            parent_dir=skill.standard_skills_parent_dir()
        ),
        install_skill=lambda: install_all_skills(
            parent_dir=skill.standard_skills_parent_dir(), overwrite=True
        ),
        install_lit_permission=(
            lambda: agent_permissions.install_cursor_lit_permission()
        ),
        skills_dir=lambda: skill.standard_skills_parent_dir(),
    ),
    AgentSpec(
        name="opencode",
        display="OpenCode",
        brand="Open-source AI agent",
        launch="opencode",
        supported=True,
        install_url="https://opencode.ai/",
        detect_bin="opencode",
        skill_state=lambda: aggregate_skill_state(
            parent_dir=skill.standard_skills_parent_dir()
        ),
        install_skill=lambda: install_all_skills(
            parent_dir=skill.standard_skills_parent_dir(), overwrite=True
        ),
        install_lit_permission=(
            lambda: agent_permissions.install_opencode_lit_permission()
        ),
        skills_dir=lambda: skill.standard_skills_parent_dir(),
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


def _windows_registry_path_values() -> list[str]:
    """Read the current machine + user PATH values from the Windows registry.

    A running process keeps the environment it inherited at startup. CLI
    installers update the registry, so a long-running ``lit gui`` process
    otherwise cannot see an agent installed after the server started.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - defensive for non-CPython builds
        return []

    locations = (
        (
            winreg.HKEY_CURRENT_USER,
            r"Environment",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    values: list[str] = []
    for root, key_name in locations:
        try:
            with winreg.OpenKey(root, key_name) as key:
                value, _kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if isinstance(value, str) and value:
            values.append(value)
    return values


def refresh_windows_path() -> None:
    """Merge live Windows registry PATH entries into this process's PATH.

    This is intentionally a no-op off Windows. On Windows it is idempotent and
    preserves process-only entries (for example an activated uv/venv bin dir)
    while adding paths registered by installers since Litman started. Updating
    ``os.environ`` also lets a subsequent Launch inherit the same live PATH.
    """
    if sys.platform != "win32":
        return

    entries: list[str] = []
    seen: set[str] = set()
    raw_values = [os.environ.get("PATH", ""), *_windows_registry_path_values()]
    for raw_value in raw_values:
        for raw_entry in os.path.expandvars(raw_value).split(";"):
            entry = raw_entry.strip().strip('"')
            if not entry:
                continue
            key = entry.rstrip("\\/").casefold()
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    os.environ["PATH"] = ";".join(entries)


def resolve_launch(spec: AgentSpec) -> str | None:
    """Resolve ``spec``'s launch executable against the live environment."""
    refresh_windows_path()
    argv = shlex.split(spec.launch)
    return shutil.which(argv[0]) if argv else None


def default_agent_name() -> str:
    """The catalog fallback default when the user has not chosen one."""
    return _DEFAULT_AGENT_NAME


def agent_skills_parent_dir(name: str) -> Path:
    """Resolve one supported agent's skills parent dir by catalog name.

    The single lookup the CLI / setup / health-check layers go through — the
    path knowledge itself stays in the catalog + :mod:`litman.core.skill`.

    Raises:
        ValueError: ``name`` is unknown, or a ``supported=False`` placeholder
            (no skills directory to install into) — the message lists the
            supported agent names.
    """
    spec = get_agent(name)
    if spec is None or not spec.supported or spec.skills_dir is None:
        known = ", ".join(s.name for s in supported_agents())
        raise ValueError(
            f"No skills directory for agent {name!r}. "
            f"Supported agents: {known}."
        )
    return spec.skills_dir()


def skills_parent_dirs() -> list[Path]:
    """Distinct skills parent dirs across supported agents, stable order.

    Catalog order, first occurrence wins — today that is the Claude Code
    dir, Antigravity CLI's app-data dir, then the open-standard dir.
    ``lit uninstall`` sweeps this full list (not just the default agent's
    dir) so switching defaults never orphans litman files in a previously
    used agent's directory.
    """
    out: list[Path] = []
    for spec in AGENTS:
        if not spec.supported or spec.skills_dir is None:
            continue
        parent = spec.skills_dir()
        if parent not in out:
            out.append(parent)
    return out


def detect(spec: AgentSpec) -> bool:
    """Is ``spec``'s command present on PATH?

    Generic, data-driven — no per-agent branching. Probes ``detect_bin`` if
    set, otherwise the first token of the launch command.
    """
    refresh_windows_path()
    probe = spec.detect_bin or shlex.split(spec.launch)[0]
    return shutil.which(probe) is not None
