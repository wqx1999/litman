"""Agent catalog tests (task-agent-onboarding AC5 + task-multi-agent-skills).

The catalog is the code-level source of truth for which agents litman can
launch/onboard. These tests pin the five-agent shape, the supported set
(claude + gemini + cursor), generic (per-agent-branch-free) detection, the
skill adapters' delegation to ``core.skill`` (claude → the Claude Code dir,
gemini/cursor → the shared open-standard dir), the per-agent skills-dir
resolvers, and the loud failure of an unsupported agent's placeholder
adapter. Detection is driven entirely through a monkeypatched
``shutil.which`` — no real binary is probed.
"""

from __future__ import annotations

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
    names = [spec.name for spec in AGENTS]
    assert names == ["claude", "codex", "cursor", "gemini", "opencode"]
    assert len(set(names)) == 5


def test_supported_set_is_claude_gemini_cursor() -> None:
    supported = {spec.name for spec in AGENTS if spec.supported}
    assert supported == {"claude", "gemini", "cursor"}
    assert [s.name for s in supported_agents()] == ["claude", "cursor", "gemini"]


def test_every_spec_carries_display_and_install_url() -> None:
    for spec in AGENTS:
        assert spec.display  # non-empty display name
        assert spec.install_url.startswith("http")


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
    )
    assert detect(spec) is False
    assert probed == ["my-agent"]


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


@pytest.mark.parametrize("name", ["codex", "opencode"])
def test_unsupported_agent_adapters_raise_not_implemented(name: str) -> None:
    spec = get_agent(name)
    assert spec.supported is False
    assert spec.skills_dir is None
    with pytest.raises(NotImplementedError):
        spec.install_skill()
    with pytest.raises(NotImplementedError):
        spec.skill_state()


# ---------------------------------------------------------------------------
# gemini / cursor adapters — the shared open-standard directory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["gemini", "cursor"])
def test_new_agent_skill_state_probes_standard_dir(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gemini/cursor probes resolve the open-standard dir at CALL time
    (module-attribute seam): a patched resolver + a real install there flips
    the state, no other seam touched."""
    standard = tmp_path / "std-skills"
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: standard
    )
    spec = get_agent(name)
    assert spec.skill_state() == "absent"

    from litman.core.skill import install_all_skills

    install_all_skills(parent_dir=standard)
    assert spec.skill_state() == "current"


@pytest.mark.parametrize("name", ["gemini", "cursor"])
def test_new_agent_install_skill_writes_standard_dir(
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


def test_claude_and_standard_dirs_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing for gemini/cursor never touches the claude dir and vice
    versa — the two supported locations are independent."""
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir",
        lambda: tmp_path / "claude-skills",
    )
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir",
        lambda: tmp_path / "std-skills",
    )
    get_agent("gemini").install_skill()
    assert get_agent("cursor").skill_state() == "current"  # shared dir
    assert get_agent("claude").skill_state() == "absent"

    get_agent("claude").install_skill()
    assert get_agent("claude").skill_state() == "current"
    (tmp_path / "std-skills" / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    assert get_agent("gemini").skill_state() == "stale"
    assert get_agent("claude").skill_state() == "current"  # unaffected


# ---------------------------------------------------------------------------
# skills-dir resolvers (agent_skills_parent_dir / skills_parent_dirs)
# ---------------------------------------------------------------------------


def test_agent_skills_parent_dir_routes_by_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_dir = tmp_path / "claude-skills"
    standard = tmp_path / "std-skills"
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir", lambda: claude_dir
    )
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: standard
    )
    assert agent_skills_parent_dir("claude") == claude_dir
    assert agent_skills_parent_dir("gemini") == standard
    assert agent_skills_parent_dir("cursor") == standard


@pytest.mark.parametrize("name", ["codex", "opencode", "nope"])
def test_agent_skills_parent_dir_rejects_non_supported(name: str) -> None:
    with pytest.raises(ValueError, match="Supported agents"):
        agent_skills_parent_dir(name)


def test_skills_parent_dirs_dedupes_with_stable_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude dir + the shared open-standard dir = exactly two, catalog
    order, the gemini/cursor duplicate collapsed."""
    claude_dir = tmp_path / "claude-skills"
    standard = tmp_path / "std-skills"
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir", lambda: claude_dir
    )
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: standard
    )
    assert skills_parent_dirs() == [claude_dir, standard]


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
