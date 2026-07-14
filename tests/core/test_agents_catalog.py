"""Agent catalog tests (task-agent-onboarding, AC5).

The catalog is the code-level source of truth for which agents litman can
launch/onboard. These tests pin the five-agent shape, the claude-only
``supported`` flag, generic (per-agent-branch-free) detection, the claude
skill adapter's delegation to ``core.skill``, and the loud failure of an
unsupported agent's placeholder adapter. Detection is driven entirely through
a monkeypatched ``shutil.which`` — no real binary is probed.
"""

from __future__ import annotations

import pytest

from litman.core import agents
from litman.core.agents import (
    AGENTS,
    AgentSpec,
    default_agent_name,
    detect,
    get_agent,
    supported_agents,
)


def test_catalog_has_exactly_five_named_agents() -> None:
    names = [spec.name for spec in AGENTS]
    assert names == ["claude", "codex", "cursor", "gemini", "opencode"]
    assert len(set(names)) == 5


def test_only_claude_is_supported() -> None:
    supported = {spec.name for spec in AGENTS if spec.supported}
    assert supported == {"claude"}
    assert [s.name for s in supported_agents()] == ["claude"]


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


@pytest.mark.parametrize("name", ["codex", "cursor", "gemini", "opencode"])
def test_unsupported_agent_adapters_raise_not_implemented(name: str) -> None:
    spec = get_agent(name)
    assert spec.supported is False
    with pytest.raises(NotImplementedError):
        spec.install_skill()
    with pytest.raises(NotImplementedError):
        spec.skill_state()
