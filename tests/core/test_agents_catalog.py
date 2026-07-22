"""Agent catalog tests (task-agent-onboarding AC5 + the multi-agent-skills
tasks).

The catalog is the code-level source of truth for which agents litman can
launch/onboard. These tests pin the five-agent shape and order (claude first,
the rest alphabetical), that every catalog entry is supported (claude + agy +
codex + cursor + opencode), generic (per-agent-branch-free) detection, the
skill adapters' delegation to ``core.skill`` (claude → the Claude Code dir;
cursor, codex, and opencode → the shared open-standard dir; agy → the
Antigravity CLI app-data dir), the per-agent skills-dir resolvers, and the
loud failure of the dormant ``supported=False`` placeholder machinery (no
live catalog agent uses it today, so it is covered directly via
``_unsupported`` + a synthetic spec). Detection is driven entirely through a
monkeypatched ``shutil.which`` — no real binary is probed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from litman.core import agents
from litman.core.agents import (
    AGENTS,
    AgentSpec,
    agent_skills_parent_dir,
    default_agent_name,
    detect,
    get_agent,
    skills_parent_dirs,
    supported_agents,
)


def test_catalog_has_exactly_five_named_agents() -> None:
    """Claude first (the fallback default tops the picker), the rest
    alphabetical — the GUI picker renders catalog order verbatim."""
    names = [spec.name for spec in AGENTS]
    assert names == ["claude", "agy", "codex", "cursor", "opencode"]
    assert len(set(names)) == 5


def test_supported_set_is_the_five_standard_agents() -> None:
    supported = {spec.name for spec in AGENTS if spec.supported}
    assert supported == {"claude", "agy", "codex", "cursor", "opencode"}
    assert [s.name for s in supported_agents()] == [
        "claude",
        "agy",
        "codex",
        "cursor",
        "opencode",
    ]
    # Every catalog entry is supported today — no greyed placeholder remains.
    assert all(s.supported for s in AGENTS)


def test_every_spec_carries_display_and_install_url() -> None:
    for spec in AGENTS:
        assert spec.display  # non-empty display name
        assert spec.install_url.startswith("http")


def test_antigravity_install_url_targets_cli_download_section() -> None:
    assert (
        get_agent("agy").install_url
        == "https://antigravity.google/download#antigravity-cli"
    )


def test_get_agent_returns_spec_or_none() -> None:
    assert get_agent("claude").name == "claude"
    assert get_agent("codex").name == "codex"
    assert get_agent("nope") is None


def test_default_agent_name_is_claude() -> None:
    assert default_agent_name() == "claude"


def test_agentspec_is_frozen() -> None:
    spec = get_agent("claude")
    with pytest.raises(Exception):
        spec.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Detection is generic — a monkeypatched which flips `detected`, no branching
# ---------------------------------------------------------------------------


def test_detect_is_data_driven_no_per_agent_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """which finds only claude -> only claude detects; which finds all ->
    all detect. Nothing about the agent name enters the decision."""
    monkeypatch.setattr(
        agents.shutil, "which", lambda name: "/usr/bin/x" if name == "claude" else None
    )
    assert detect(get_agent("claude")) is True
    assert detect(get_agent("codex")) is False
    assert detect(get_agent("cursor")) is False

    monkeypatch.setattr(agents.shutil, "which", lambda name: f"/usr/bin/{name}")
    for spec in AGENTS:
        assert detect(spec) is True

    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    for spec in AGENTS:
        assert detect(spec) is False


def test_detect_uses_detect_bin_first_token_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec with no detect_bin falls back to the launch command's first
    token — the generic probe still works."""
    probed: list[str] = []

    def fake_which(name: str) -> str | None:
        probed.append(name)
        return None

    monkeypatch.setattr(agents.shutil, "which", fake_which)
    spec = AgentSpec(
        name="x",
        display="X",
        launch="my-agent --flag",
        supported=False,
        install_url="https://x/",
        detect_bin="",
        skill_state=lambda: "absent",
        install_skill=lambda: [],
        install_lit_permission=lambda: {},
    )
    assert detect(spec) is False
    assert probed == ["my-agent"]


def test_windows_recheck_reads_new_registry_path_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI installed after the GUI started becomes detectable immediately.

    Windows does not update a running server's environment when an installer
    changes the user PATH. Recheck must merge the live registry value, and
    repeated rechecks must not keep appending duplicate entries.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PATH", r"C:\Windows\System32;C:\old-bin")
    monkeypatch.setattr(
        agents,
        "_windows_registry_path_values",
        lambda: [r"C:\new-agent-bin;C:\OLD-BIN"],
    )

    seen_paths: list[str] = []

    def fake_which(name: str) -> str | None:
        seen_paths.append(os.environ["PATH"])
        if name == "codex" and r"C:\new-agent-bin" in os.environ["PATH"]:
            return r"C:\new-agent-bin\codex.exe"
        return None

    monkeypatch.setattr(agents.shutil, "which", fake_which)
    assert detect(get_agent("codex")) is True
    assert detect(get_agent("codex")) is True
    assert seen_paths == [
        r"C:\Windows\System32;C:\old-bin;C:\new-agent-bin",
        r"C:\Windows\System32;C:\old-bin;C:\new-agent-bin",
    ]


# ---------------------------------------------------------------------------
# Claude adapter delegates to core.skill; placeholders raise
# ---------------------------------------------------------------------------


def test_claude_install_skill_routes_to_install_all_skills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_install_all(overwrite: bool = False, **kw: object) -> list[dict]:
        calls["overwrite"] = overwrite
        calls["kw"] = kw
        return [{"name": "lit-library", "files": ["SKILL.md"], "mode": "created"}]

    monkeypatch.setattr(agents, "install_all_skills", fake_install_all)
    result = get_agent("claude").install_skill()
    assert calls == {"overwrite": True, "kw": {}}
    assert result == [{"name": "lit-library", "files": ["SKILL.md"], "mode": "created"}]


def test_claude_skill_state_routes_to_aggregate_skill_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agents, "aggregate_skill_state", lambda *a, **k: "absent"
    )
    assert get_agent("claude").skill_state() == "absent"
    monkeypatch.setattr(
        agents, "aggregate_skill_state", lambda *a, **k: "stale"
    )
    assert get_agent("claude").skill_state() == "stale"


@pytest.mark.parametrize(
    ("name", "function_name"),
    [
        ("claude", "install_claude_lit_permission"),
        ("agy", "install_antigravity_lit_permission"),
        ("codex", "install_codex_lit_permission"),
        ("cursor", "install_cursor_lit_permission"),
        ("opencode", "install_opencode_lit_permission"),
    ],
)
def test_permission_install_routes_through_catalog_adapter(
    name: str, function_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {"mode": "created", "rule": name, "warning": None}
    monkeypatch.setattr(
        f"litman.core.agent_permissions.{function_name}",
        lambda: expected,
    )
    assert get_agent(name).install_lit_permission() == expected


def test_unsupported_placeholder_machinery_raises_not_implemented() -> None:
    """The ``supported=False`` scaffold stays covered without a live catalog
    agent (there is none today): the raw ``_unsupported`` adapter and a
    synthetic placeholder spec both raise ``NotImplementedError``. Generic
    code gates on ``supported`` and never calls these, so a mis-call must fail
    loudly instead of silently misbehaving."""
    with pytest.raises(NotImplementedError):
        agents._unsupported("future")()

    placeholder = AgentSpec(
        name="future",
        display="Future Agent",
        launch="future",
        supported=False,
        install_url="https://example.invalid/future",
        detect_bin="future",
        skill_state=agents._unsupported("future"),
        install_skill=agents._unsupported("future"),
        install_lit_permission=agents._unsupported("future"),
    )
    assert placeholder.supported is False
    assert placeholder.skills_dir is None
    with pytest.raises(NotImplementedError):
        placeholder.install_skill()
    with pytest.raises(NotImplementedError):
        placeholder.skill_state()
    with pytest.raises(NotImplementedError):
        placeholder.install_lit_permission()


# ---------------------------------------------------------------------------
# standard-dir adapters (cursor / codex / opencode all share ~/.agents/skills)
# vs the agy adapter's own Antigravity app-data dir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["cursor", "codex", "opencode"])
def test_standard_dir_agent_skill_state_probes_standard_dir(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cursor / codex / opencode probes all resolve the open-standard dir
    at CALL time (module-attribute seam): a patched resolver + a real install
    there flips the state, no other seam touched. The three share one dir, so
    their adapters are identical in shape."""
    standard = tmp_path / "std-skills"
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: standard
    )
    spec = get_agent(name)
    assert spec.skill_state() == "absent"

    from litman.core.skill import install_all_skills

    install_all_skills(parent_dir=standard)
    assert spec.skill_state() == "current"


def test_agy_skill_state_probes_antigravity_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same call-time seam contract for the agy adapter, against its own
    (non-open-standard) app-data directory."""
    antigravity = tmp_path / "agy-skills"
    monkeypatch.setattr(
        "litman.core.skill.antigravity_skills_parent_dir",
        lambda: antigravity,
    )
    spec = get_agent("agy")
    assert spec.skill_state() == "absent"

    from litman.core.skill import install_all_skills

    install_all_skills(parent_dir=antigravity)
    assert spec.skill_state() == "current"


@pytest.mark.parametrize("name", ["cursor", "codex", "opencode"])
def test_standard_dir_agent_install_skill_writes_standard_dir(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    standard = tmp_path / "std-skills"
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: standard
    )
    results = get_agent(name).install_skill()
    assert results  # every bundled skill installed
    for result in results:
        assert (standard / result["name"] / "SKILL.md").is_file()


def test_agy_install_skill_writes_antigravity_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    antigravity = tmp_path / "agy-skills"
    monkeypatch.setattr(
        "litman.core.skill.antigravity_skills_parent_dir",
        lambda: antigravity,
    )
    results = get_agent("agy").install_skill()
    assert results  # every bundled skill installed
    for result in results:
        assert (antigravity / result["name"] / "SKILL.md").is_file()


def test_three_skills_dirs_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing/tampering in any one of the three DISTINCT skills locations
    (claude's dir, agy's dir, the shared open-standard dir) never touches the
    other two — the three locations are fully independent. cursor / codex /
    opencode share the open-standard dir, so those are not mutually
    independent; that shared-dir behaviour is covered by the server-side
    per-agent test."""
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir",
        lambda: tmp_path / "claude-skills",
    )
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir",
        lambda: tmp_path / "std-skills",
    )
    monkeypatch.setattr(
        "litman.core.skill.antigravity_skills_parent_dir",
        lambda: tmp_path / "agy-skills",
    )
    get_agent("cursor").install_skill()
    assert get_agent("cursor").skill_state() == "current"
    assert get_agent("claude").skill_state() == "absent"
    assert get_agent("agy").skill_state() == "absent"

    get_agent("claude").install_skill()
    get_agent("agy").install_skill()
    assert get_agent("claude").skill_state() == "current"
    assert get_agent("agy").skill_state() == "current"

    (tmp_path / "std-skills" / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    assert get_agent("cursor").skill_state() == "stale"
    assert get_agent("claude").skill_state() == "current"  # unaffected
    assert get_agent("agy").skill_state() == "current"  # unaffected

    (tmp_path / "agy-skills" / "lit-reading" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    assert get_agent("agy").skill_state() == "stale"
    assert get_agent("claude").skill_state() == "current"  # unaffected


# ---------------------------------------------------------------------------
# skills-dir resolvers (agent_skills_parent_dir / skills_parent_dirs)
# ---------------------------------------------------------------------------


def test_agent_skills_parent_dir_routes_by_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_dir = tmp_path / "claude-skills"
    standard = tmp_path / "std-skills"
    antigravity = tmp_path / "agy-skills"
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir", lambda: claude_dir
    )
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: standard
    )
    monkeypatch.setattr(
        "litman.core.skill.antigravity_skills_parent_dir",
        lambda: antigravity,
    )
    assert agent_skills_parent_dir("claude") == claude_dir
    assert agent_skills_parent_dir("cursor") == standard
    assert agent_skills_parent_dir("codex") == standard
    assert agent_skills_parent_dir("opencode") == standard
    assert agent_skills_parent_dir("agy") == antigravity


@pytest.mark.parametrize("name", ["nope"])
def test_agent_skills_parent_dir_rejects_non_supported(name: str) -> None:
    # No known-but-unsupported catalog name remains; an unknown name still
    # hits the same rejection path (spec: covering unknown-name is enough).
    with pytest.raises(ValueError, match="Supported agents"):
        agent_skills_parent_dir(name)


def test_skills_parent_dirs_three_dirs_catalog_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Still exactly three distinct dirs, catalog order (claude, agy, then
    the shared open-standard dir). First-occurrence dedupe collapses the
    three standard-dir agents — codex is now the first of them in catalog
    order, cursor and opencode reuse the same path."""
    claude_dir = tmp_path / "claude-skills"
    standard = tmp_path / "std-skills"
    antigravity = tmp_path / "agy-skills"
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir", lambda: claude_dir
    )
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: standard
    )
    monkeypatch.setattr(
        "litman.core.skill.antigravity_skills_parent_dir",
        lambda: antigravity,
    )
    assert skills_parent_dirs() == [claude_dir, antigravity, standard]


@pytest.mark.no_skills_isolation
def test_standard_skills_parent_dir_respects_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$HOME redirect is honored at call time (same seam contract as the
    claude resolver). Opted out of the autouse isolation, which patches the
    very resolver under test; $HOME is redirected instead."""
    from litman.core.skill import standard_skills_parent_dir

    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    assert standard_skills_parent_dir() == (
        tmp_path / "elsewhere" / ".agents" / "skills"
    )


@pytest.mark.no_skills_isolation
def test_claude_skills_parent_dir_respects_explicit_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core.skill import default_skills_parent_dir

    config_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert default_skills_parent_dir() == config_dir / "skills"


@pytest.mark.no_skills_isolation
def test_antigravity_skills_parent_dir_respects_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same $HOME seam contract for the Antigravity CLI resolver."""
    from litman.core.skill import antigravity_skills_parent_dir

    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    assert antigravity_skills_parent_dir() == (
        tmp_path / "elsewhere" / ".gemini" / "antigravity-cli" / "skills"
    )
