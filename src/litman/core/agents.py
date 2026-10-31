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

litman supports seven agents today. Two have a skills directory of their
own: Claude Code (``~/.claude/skills``) and Antigravity CLI (``agy``, whose
only user-installable skills location is its own app-data directory
``~/.gemini/antigravity-cli/skills`` — it does not read the open-standard
dir). The other five — ZCode, DeepSeek Harness, Codex, Cursor, and OpenCode —
all discover skills from the Agent Skills open-standard directory
``~/.agents/skills`` and so share one adapter shape (Cursor additionally does
a compatibility read of the Claude dir).

How an agent starts follows from its row (:func:`launch_modes`): ``launch`` is
a command run in a terminal at the vault, and ``open_url`` is a desktop app
that litman opens itself through :mod:`litman.core.app_launch`, with no
terminal in between. Claude Code has both, ZCode and DeepSeek Harness only the
app, and the other four only the CLI. Consumers branch on the mode, the same
data-driven way they branch on ``supported``.

All seven are ``supported=True`` — the catalog carries no greyed placeholder
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
from typing import Any, Literal

from litman.core import agent_permissions, app_launch, skill
from litman.core.skill import aggregate_skill_state, install_all_skills

LaunchMode = Literal["terminal", "app"]

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

    ``launch`` and ``detect_bin`` are ``""`` on a row that is only an app; an
    app is found and opened through ``url_scheme`` and the fields after it.
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
    # The app's link scheme. On Windows a row with app_args finds the exe from
    # the scheme's registry open command, and a row without counts as installed
    # on that exe or on the system knowing a handler for the scheme; Linux looks
    # the scheme's handler up in the XDG mimeapps.list / mimeinfo.cache files.
    url_scheme: str | None = None
    # Windows: the exe's arguments; "{vault}" becomes the vault path.
    app_args: tuple[str, ...] = ()
    # The link that opens the app; a "{vault}" in it becomes the
    # percent-encoded vault path.
    open_url: str | None = None
    # macOS: the bundle's name under /Applications or ~/Applications.
    mac_bundle: str | None = None
    # For an agent litman writes no approval rule for: how the user lets lit
    # run in the app. Shown when its skills are installed, never on launch.
    permission_hint: str | None = None
    # On a row with both ways: the one to use when the user has not chosen.
    # Ignored on a row with one way.
    default_mode: LaunchMode = "terminal"
    # On a row with both ways: where to get the app (install_url is the CLI's).
    app_install_url: str | None = None
    # The success line after opening an app that then asks the user something.
    app_open_hint: str | None = None


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
# from Cursor's — no per-vendor resolver or generated config file. ZCode
# reads the same directory (checked on Windows: skills installed there for
# Codex showed up in ZCode), and so does DeepSeek Harness (checked on Windows:
# it loaded both litman skills from there), so their adapters are the same too.
#
# Order: claude (the fallback default tops the picker), zcode, agy, codex,
# dsh, cursor, opencode — set by wangq (2026-10-30), who put DeepSeek Harness
# right under Codex. The GUI picker and `skills_parent_dirs()` follow this
# order.
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
        url_scheme="claude",
        # The app asks the user to trust a folder passed by link, every time.
        open_url="claude://code/new?folder={vault}",
        mac_bundle="Claude.app",
        app_install_url="https://claude.com/download",
        app_open_hint="Opened Claude. Confirm the folder to start.",
    ),
    AgentSpec(
        name="zcode",
        display="ZCode",
        brand="GLM · 智谱",
        # A desktop app: never a PATH command. ZCode also ships a CLI named
        # `zcode`, which `lit agent` would otherwise exec instead.
        launch="",
        supported=True,
        install_url="https://zcode.z.ai",
        detect_bin="",
        skill_state=lambda: aggregate_skill_state(
            parent_dir=skill.standard_skills_parent_dir()
        ),
        install_skill=lambda: install_all_skills(
            parent_dir=skill.standard_skills_parent_dir(), overwrite=True
        ),
        install_lit_permission=(
            lambda: agent_permissions.no_lit_permission("zcode")
        ),
        skills_dir=lambda: skill.standard_skills_parent_dir(),
        url_scheme="zcode",
        app_args=("--open-workspace", "{vault}"),
        open_url="zcode://workspace/open?path={vault}",
        mac_bundle="ZCode.app",
        permission_hint=(
            "In ZCode, switch to Autonomous mode so lit runs without asking "
            "each time."
        ),
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
        name="dsh",
        display="DeepSeek Harness",
        brand="DeepSeek · 深度求索",
        # A desktop app: never a PATH command, as for ZCode.
        launch="",
        supported=True,
        install_url="https://www.deepseek.com/harness/",
        detect_bin="",
        skill_state=lambda: aggregate_skill_state(
            parent_dir=skill.standard_skills_parent_dir()
        ),
        install_skill=lambda: install_all_skills(
            parent_dir=skill.standard_skills_parent_dir(), overwrite=True
        ),
        install_lit_permission=(
            lambda: agent_permissions.no_lit_permission("dsh")
        ),
        skills_dir=lambda: skill.standard_skills_parent_dir(),
        url_scheme="dsh",
        # Its link, passed to the exe as its registered open command does:
        # started through the exe, the app stays out of our console (handed
        # to the system opener, it logs there). No vault in either, as DSH
        # takes no folder and reopens its last session.
        app_args=("dsh://open",),
        open_url="dsh://open",
        mac_bundle="DeepSeek Harness.app",
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


# Where a per-user agent install lands, relative to $HOME. A desktop launcher
# inherits the graphical session's PATH, and that session sources no shell rc —
# Ubuntu's Wayland session never reads ~/.profile — so a shortcut-started
# litman sees only the system dirs. Every agent installs under one of these,
# which is why a missing ~/.local/bin hides all five at once while each one
# still resolves fine in a terminal.
_POSIX_USER_BIN_DIRS = (
    ".local/bin",
    "bin",
    ".opencode/bin",
    ".bun/bin",
    ".npm-global/bin",
    ".cargo/bin",
)

# The same hole one level up, absolute: a macOS app bundle launched from Finder
# or the Dock inherits launchd's PATH (/usr/bin:/bin:/usr/sbin:/sbin), which
# contains neither Homebrew prefix — so a brew-installed agent is invisible to
# a double-clicked litman even though every terminal finds it.
_POSIX_SYSTEM_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def _posix_probe_bin_dirs() -> list[str]:
    """Existing bin dirs a desktop session's PATH may omit, in fixed order.

    Re-probed on every call so a CLI installed while the GUI is running is
    picked up by the next Recheck instead of needing a restart.
    """
    candidates: list[str] = []
    try:
        home = Path.home()
    except RuntimeError:  # pragma: no cover - no resolvable home directory
        pass
    else:
        candidates += [str(home / rel) for rel in _POSIX_USER_BIN_DIRS]
    candidates += _POSIX_SYSTEM_BIN_DIRS
    return [path for path in candidates if Path(path).is_dir()]


def _merge_path_entries(
    raw_values: list[str], sep: str, *, fold_case: bool
) -> None:
    """Rewrite ``PATH`` as ``raw_values`` joined, first occurrence winning."""
    entries: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        for raw_entry in os.path.expandvars(raw_value).split(sep):
            entry = raw_entry.strip().strip('"')
            if not entry:
                continue
            key = entry.rstrip("\\/")
            if fold_case:
                key = key.casefold()
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    os.environ["PATH"] = sep.join(entries)


def refresh_path() -> None:
    """Merge the live environment's agent locations into this process's PATH.

    A running process keeps the environment it inherited at startup, and on
    either platform that environment can be missing an agent the user has
    installed — for different reasons, so each gets its own source:

    * Windows: installers write the new PATH to the registry, which a running
      server never re-reads.
    * POSIX: a desktop-launched process inherits the graphical session's PATH,
      which omits the bin dirs a shell rc would have added (see
      :data:`_POSIX_USER_BIN_DIRS` and :data:`_POSIX_SYSTEM_BIN_DIRS`).

    Idempotent, and preserves process-only entries (an activated uv/venv bin
    dir, say) by keeping the inherited PATH first. Updating ``os.environ``
    also lets a subsequent Launch inherit the same live PATH.
    """
    if sys.platform == "win32":
        raw = [os.environ.get("PATH", ""), *_windows_registry_path_values()]
        _merge_path_entries(raw, ";", fold_case=True)
        return
    raw = [os.environ.get("PATH", ""), *_posix_probe_bin_dirs()]
    _merge_path_entries(raw, ":", fold_case=False)


def resolve_launch(spec: AgentSpec) -> str | None:
    """Resolve ``spec``'s launch executable against the live environment.

    None for a row that is only a desktop app, which has no launch command.
    """
    if not spec.launch:
        return None
    refresh_path()
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
    dir, the open-standard dir, then Antigravity CLI's app-data dir.
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


def launch_modes(spec: AgentSpec) -> list[LaunchMode]:
    """The ways ``spec`` can start, from its data alone: a launch command is
    the terminal, an open link is the app."""
    modes: list[LaunchMode] = []
    if spec.launch:
        modes.append("terminal")
    if spec.open_url:
        modes.append("app")
    return modes


def machine_modes(spec: AgentSpec) -> list[LaunchMode]:
    """The ways ``spec`` could start on this machine, installed or not.

    A row with both ways loses the app where it would not reach the user. A
    row that is only an app keeps it: :func:`litman.core.app_launch.open_app`
    decides what happens there, as it always has.
    """
    modes = launch_modes(spec)
    if len(modes) > 1 and not app_launch.app_reaches_user():
        return [mode for mode in modes if mode != "app"]
    return modes


def mode_usable(spec: AgentSpec, mode: str) -> bool:
    """Can ``spec`` start this way here right now?

    Generic, data-driven — no per-agent branching. The app asks
    :func:`litman.core.app_launch.app_installed`; the terminal probes
    ``detect_bin`` if set, otherwise the first token of the launch command.
    """
    if mode not in machine_modes(spec):
        return False
    if mode == "app":
        return app_launch.app_installed(spec)
    refresh_path()
    probe = spec.detect_bin or shlex.split(spec.launch)[0]
    return shutil.which(probe) is not None


def detect(spec: AgentSpec) -> bool:
    """Can ``spec`` start at all here, in any of its ways?"""
    return any(mode_usable(spec, mode) for mode in machine_modes(spec))


def _first_usable(spec: AgentSpec, order: tuple[LaunchMode, ...]) -> LaunchMode:
    """The first mode in ``order`` usable here; with none usable, the first
    this machine could use, which is the one whose download to point at."""
    possible = [mode for mode in order if mode in machine_modes(spec)]
    if len(set(possible)) > 1:
        for mode in possible:
            if mode_usable(spec, mode):
                return mode
    return possible[0] if possible else spec.default_mode


def choose_mode(spec: AgentSpec, saved: LaunchMode | None = None) -> LaunchMode:
    """The way the GUI button starts ``spec``: the user's saved choice if it
    is usable, else the catalog default if it is, else the other way. A saved
    choice that is not usable right now is passed over, not forgotten."""
    if saved is not None and mode_usable(spec, saved):
        return saved
    return _first_usable(spec, (spec.default_mode, "terminal", "app"))


def cli_mode(spec: AgentSpec) -> LaunchMode:
    """The way ``lit agent`` starts ``spec``: typed in a terminal, it runs in
    that terminal when it can. No saved choice or catalog default is read, so
    the ``lit agent`` line the GUI hands out means the same everywhere."""
    return _first_usable(spec, ("terminal", "app"))


def mode_install_url(spec: AgentSpec, mode: str) -> str:
    """Where to get ``spec`` for this way of starting it."""
    if mode == "app" and spec.app_install_url:
        return spec.app_install_url
    return spec.install_url
