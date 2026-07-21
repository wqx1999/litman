"""Agent onboarding + launch endpoint tests (task-agent-onboarding).

Covers AC1 (real skill-install e2e), AC2 (status catalog view), AC3 (RCE:
name-only bodies), AC6 (server-side needs_setup truth table), plus the
retained launch behaviour re-pointed at the catalog + machine-level prefs.

RED LINE (ADR-020): every endpoint accepts an agent NAME only — the launch
command, install target, and set of installable agents come from the
server-side catalog. The malicious-body tests are the regression gate.

Guarded with ``importorskip`` so the suite still collects when fastapi is
absent (invariant #5). The autouse ``_isolate_registry`` fixture redirects
``$LITMAN_REGISTRY_DIR`` at a tmp dir, which isolates preferences.yaml too.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import Response
from fastapi.testclient import TestClient

from litman.core import agent_prefs, agents
from litman.core.library import create_vault
from litman.core.skill import list_bundled_skills
from litman.server import create_app, routes_agent
from litman.server.routes_agent import agent_status


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _status(client: TestClient) -> dict:
    return client.get("/api/agent/status").json()


# ---------------------------------------------------------------------------
# GET /api/agents + POST /api/agent/launch (re-pointed at catalog + prefs)
# ---------------------------------------------------------------------------


def test_get_agents_lists_supported_and_default(vault: Path) -> None:
    resp = _client(vault).get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == {
        "agents": ["claude", "agy", "codex", "cursor", "opencode"],
        "default": "claude",
    }


def test_launch_unknown_agent_is_400(vault: Path) -> None:
    resp = _client(vault).post("/api/agent/launch", json={"agent": "nope"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Unknown agent 'nope'" in detail
    assert "claude" in detail


def test_launch_ignores_body_command_field(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malicious body: argv comes from the catalog; `command` is dead."""
    spawned: list[tuple[list[str], Path]] = []

    def fake_spawn(argv: list[str], cwd: Path) -> bool:
        spawned.append((argv, cwd))
        return True

    monkeypatch.setattr("litman.core.terminal.spawn_terminal", fake_spawn)
    resp = _client(vault).post(
        "/api/agent/launch", json={"agent": "claude", "command": "rm -rf /"}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "mode": "spawned",
        "agent": "claude",
        "command": "claude",
    }
    assert spawned == [(["claude"], vault)]


def test_launch_bodyless_uses_default_agent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("litman.core.terminal.spawn_terminal", lambda _a, _c: True)
    resp = _client(vault).post("/api/agent/launch")
    assert resp.status_code == 200
    assert resp.json()["agent"] == "claude"
    assert resp.json()["mode"] == "spawned"


def test_launch_copy_fallback_wraps_as_lit_agent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No terminal available → mode "copy" with the `lit agent` wrapper. Only a
    SUPPORTED agent (claude, the default) reaches the copy-fallback."""
    monkeypatch.setattr("litman.core.terminal.spawn_terminal", lambda _a, _c: False)
    resp = _client(vault).post("/api/agent/launch", json={})
    assert resp.json() == {
        "ok": True,
        "mode": "copy",
        "agent": "claude",
        "command": "lit agent",
    }


def test_launch_windows_passes_resolved_absolute_command_to_terminal(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    codex = r"C:\Users\Wang\AppData\Roaming\npm\codex.CMD"
    monkeypatch.setattr(agents, "resolve_launch", lambda _spec: codex)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "litman.core.terminal.spawn_terminal",
        lambda argv, _cwd: spawned.append(argv) or True,
    )
    async def body_agent_name(_request: object) -> str:
        return "codex"

    monkeypatch.setattr(routes_agent, "_body_agent_name", body_agent_name)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(vault=vault)))
    body = asyncio.run(routes_agent.launch_agent(request))  # type: ignore[arg-type]
    assert body["mode"] == "spawned"
    assert spawned == [[codex]]


def test_launch_unsupported_agent_is_400(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``supported=False`` placeholder is rejected with the distinct "not
    available yet" message before any PATH probe / copy-fallback — inert on the
    launch axis. No greyed agent ships in the catalog today, so a synthetic one
    is injected to exercise the dormant gating branch (not a live catalog
    instance)."""
    placeholder = agents.AgentSpec(
        name="future",
        display="Future Agent",
        launch="future",
        supported=False,
        install_url="https://example.invalid/future",
        detect_bin="future",
        skill_state=agents._unsupported("future"),
        install_skill=agents._unsupported("future"),
    )
    monkeypatch.setattr(agents, "AGENTS", (*agents.AGENTS, placeholder))
    spawned: list[object] = []
    monkeypatch.setattr(
        "litman.core.terminal.spawn_terminal",
        lambda a, c: spawned.append((a, c)) or True,
    )
    resp = _client(vault).post("/api/agent/launch", json={"agent": "future"})
    assert resp.status_code == 400
    assert "not available yet" in resp.json()["detail"]
    assert spawned == []  # never reached the spawn path


def test_launch_non_string_agent_is_400(vault: Path) -> None:
    resp = _client(vault).post("/api/agent/launch", json={"agent": ["claude"]})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# AC2 — GET /api/agent/status catalog view
# ---------------------------------------------------------------------------


def test_status_returns_five_catalog_entries(vault: Path) -> None:
    body = _status(_client(vault))
    assert [e["name"] for e in body["agents"]] == [
        "claude",
        "agy",
        "codex",
        "cursor",
        "opencode",
    ]
    supported = {e["name"]: e["supported"] for e in body["agents"]}
    assert supported == {
        "claude": True,
        "agy": True,
        "codex": True,
        "cursor": True,
        "opencode": True,
    }
    for e in body["agents"]:
        assert set(e) == {
            "name",
            "display",
            "supported",
            "detected",
            "install_url",
            "skill_state",
        }
        assert isinstance(e["detected"], bool)
        # Per-agent skill verdict: every catalog agent is supported today, so
        # each reads a real verdict. The null branch is the latent
        # supported=False placeholder contract (no such entry ships now).
        if e["supported"]:
            assert e["skill_state"] in {"absent", "stale", "current"}
        else:
            assert e["skill_state"] is None
    # top-level fields present
    assert "default" in body
    assert isinstance(body["skill_installed"], bool)
    assert body["skill_state"] in {"absent", "stale", "current"}
    assert isinstance(body["needs_setup"], bool)


def test_status_sets_no_store_cache_header(vault: Path) -> None:
    """`detected` / `skill_installed` are live machine state; the browser must
    never serve a cached body (else a plain reload keeps showing the red dot
    until a hard refresh)."""
    resp = _client(vault).get("/api/agent/status")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


def test_status_recheck_detects_cli_added_to_live_windows_registry_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same running server sees a CLI installed between two status calls."""
    registry_paths: list[str] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PATH", r"C:\Windows\System32")
    monkeypatch.setattr(
        agents, "_windows_registry_path_values", lambda: registry_paths.copy()
    )
    monkeypatch.setattr(
        agents.shutil,
        "which",
        lambda name: (
            rf"C:\new-agent-bin\{name}.exe"
            if r"C:\new-agent-bin" in os.environ["PATH"]
            else None
        ),
    )
    monkeypatch.setattr(agents, "aggregate_skill_state", lambda *a, **k: "absent")

    before = agent_status(Response())
    assert not any(entry["detected"] for entry in before["agents"])

    registry_paths.append(r"C:\new-agent-bin")
    after = agent_status(Response())
    assert all(entry["detected"] for entry in after["agents"])


def test_status_never_leaks_skill_paths(vault: Path) -> None:
    """No skills filesystem path — neither ~/.claude/skills nor the shared
    ~/.agents/skills — may appear anywhere in the status contract (the
    install-url is docs.claude.com, which is fine — the red line is the
    skills *directory*, not the substring "claude")."""
    raw = _client(vault).get("/api/agent/status").text
    assert ".claude/skills" not in raw
    assert ".agents/skills" not in raw
    assert "/skills" not in raw


def test_status_greyed_placeholder_reads_null_skill_state(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``supported=False`` catalog entry reports ``skill_state: null`` — its
    adapter is never probed. No such agent ships today, so a synthetic
    placeholder is injected to exercise the dormant status branch (the
    ``else None`` output the api.ts contract still advertises as ``| null``)."""
    placeholder = agents.AgentSpec(
        name="future",
        display="Future Agent",
        launch="future",
        supported=False,
        install_url="https://example.invalid/future",
        detect_bin="future",
        skill_state=agents._unsupported("future"),
        install_skill=agents._unsupported("future"),
    )
    monkeypatch.setattr(agents, "AGENTS", (*agents.AGENTS, placeholder))
    entries = {e["name"]: e for e in _status(_client(vault))["agents"]}
    assert entries["future"]["supported"] is False
    assert entries["future"]["skill_state"] is None  # never probed
    # a live supported entry still reports a concrete verdict
    assert entries["claude"]["skill_state"] in {"absent", "stale", "current"}


def test_status_per_agent_skill_state_is_independent(vault: Path) -> None:
    """Per-agent skill_state reads each agent's own directory. cursor, codex,
    and opencode share the open-standard ~/.agents/skills dir, so installing
    there flips all three of their rows together; claude's and agy's own dirs
    stay independent. Every catalog agent is supported now, so each row reads a
    real verdict — no null placeholder remains. Drives the REAL resolvers +
    copies (conftest isolates all three dirs at tmp paths)."""
    from litman.core import skill

    client = _client(vault)

    def states() -> dict[str, str | None]:
        return {
            e["name"]: e["skill_state"] for e in _status(client)["agents"]
        }

    before = states()
    assert before["claude"] == "absent"
    assert before["agy"] == "absent"
    assert before["cursor"] == "absent"
    assert before["codex"] == "absent"
    assert before["opencode"] == "absent"

    skill.install_all_skills(parent_dir=skill.standard_skills_parent_dir())
    after_standard = states()
    # The shared open-standard dir flips cursor, codex AND opencode together.
    assert after_standard["cursor"] == "current"
    assert after_standard["codex"] == "current"
    assert after_standard["opencode"] == "current"
    assert after_standard["claude"] == "absent"  # untouched
    assert after_standard["agy"] == "absent"  # untouched

    skill.install_all_skills(parent_dir=skill.default_skills_parent_dir())
    assert states()["claude"] == "current"
    skill.install_all_skills(
        parent_dir=skill.antigravity_skills_parent_dir()
    )
    assert states()["agy"] == "current"

    # Tamper the standard-dir copy: cursor, codex and opencode all flip stale
    # together (shared dir); claude and agy are unaffected.
    tampered = (
        skill.standard_skills_parent_dir() / "lit-library" / "SKILL.md"
    )
    tampered.write_text("OUTDATED\n", encoding="utf-8")
    tampered_states = states()
    assert tampered_states["cursor"] == "stale"
    assert tampered_states["codex"] == "stale"
    assert tampered_states["opencode"] == "stale"
    assert tampered_states["claude"] == "current"
    assert tampered_states["agy"] == "current"


def test_skill_install_agy_lands_in_antigravity_dir(vault: Path) -> None:
    """POST /api/agent/skill/install {"agent": "agy"} really copies the
    bundled skills into the Antigravity CLI dir (no adapter stubbed) and
    the other two locations stay untouched."""
    from litman.core import skill

    client = _client(vault)
    resp = client.post("/api/agent/skill/install", json={"agent": "agy"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["agent"] == "agy"
    assert "SKILL.md" in body["files"]

    antigravity = skill.antigravity_skills_parent_dir()
    for name in list_bundled_skills():
        assert (antigravity / name / "SKILL.md").is_file()
    assert not skill.default_skills_parent_dir().exists()
    assert not skill.standard_skills_parent_dir().exists()


# ---------------------------------------------------------------------------
# AC1 — real end-to-end skill install (destination redirected to tmp; the copy
# itself is un-stubbed — inject-seam / M34 lesson). Never touches real ~/.claude.
# ---------------------------------------------------------------------------


def test_skill_install_e2e_really_copies_and_status_flips(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_skills = tmp_path / "fake-home" / ".claude" / "skills"

    import litman.core.skill as skill_mod

    real_install = skill_mod.install_all_skills
    real_probe = skill_mod.aggregate_skill_state
    # Redirect only the DESTINATION (the sanctioned $HOME/parent_dir redirect);
    # the real bundled-file copy still runs end to end.
    monkeypatch.setattr(
        agents,
        "install_all_skills",
        lambda overwrite=True: real_install(parent_dir=tmp_skills, overwrite=overwrite),
    )
    monkeypatch.setattr(
        agents,
        "aggregate_skill_state",
        lambda *a, **k: real_probe(parent_dir=tmp_skills),
    )

    client = _client(vault)
    before = _status(client)
    assert before["skill_installed"] is False
    assert before["skill_state"] == "absent"

    resp = client.post("/api/agent/skill/install", json={"agent": "claude"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["agent"] == "claude"
    assert "SKILL.md" in body["files"]  # real files reported
    assert body["mode"] in {"created", "overwritten"}

    # Real files really landed on disk under the redirected dir.
    bundled = list_bundled_skills()
    assert bundled  # sanity: the package actually bundles skills
    for skill_name in bundled:
        assert (tmp_skills / skill_name / "SKILL.md").is_file()

    after = _status(client)
    assert after["skill_installed"] is True
    assert after["skill_state"] == "current"

    # Stale round-trip: tamper one installed file → status flips to "stale";
    # the SAME install endpoint refreshes it back to "current" (the panel's
    # "Update skill" action).
    tampered = tmp_skills / bundled[0] / "SKILL.md"
    tampered.write_text("OUTDATED LOCAL COPY\n", encoding="utf-8")
    assert _status(client)["skill_state"] == "stale"

    resp = client.post("/api/agent/skill/install", json={"agent": "claude"})
    assert resp.status_code == 200
    assert _status(client)["skill_state"] == "current"
    assert "OUTDATED LOCAL COPY" not in tampered.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC3 — RCE: name-only bodies; unsupported/unknown rejected
# ---------------------------------------------------------------------------


def test_skill_install_ignores_body_target_field(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_install_all(overwrite: bool = False, **kw: object) -> list[dict]:
        captured["overwrite"] = overwrite
        captured["kw"] = kw
        return [{"name": "lit-library", "files": ["SKILL.md"], "mode": "created"}]

    monkeypatch.setattr(agents, "install_all_skills", fake_install_all)
    resp = _client(vault).post(
        "/api/agent/skill/install",
        json={"agent": "claude", "target": "/etc", "command": "rm -rf /"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "agent": "claude",
        "files": ["SKILL.md"],
        "mode": "created",
    }
    # The installer got ONLY the server-side overwrite flag — nothing from the
    # body (target/command) reached it.
    assert captured == {"overwrite": True, "kw": {}}


def test_skill_install_unknown_agent_is_400(vault: Path) -> None:
    resp = _client(vault).post("/api/agent/skill/install", json={"agent": "nope"})
    assert resp.status_code == 400


def test_put_default_unknown_agent_is_400(vault: Path) -> None:
    resp = _client(vault).put("/api/agent/default", json={"agent": "nope"})
    assert resp.status_code == 400


def test_put_default_missing_agent_is_400(vault: Path) -> None:
    resp = _client(vault).put("/api/agent/default", json={})
    assert resp.status_code == 400


def test_put_default_persists_and_status_reflects(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the REAL prefs write + read through the endpoints (tmp-isolated
    preferences.yaml), then confirms status reflects the persisted default."""
    monkeypatch.setattr(agents, "detect", lambda spec: True)
    monkeypatch.setattr(agents, "aggregate_skill_state", lambda *a, **k: "current")
    client = _client(vault)

    assert agent_prefs.load_default_agent() is None  # nothing chosen yet
    resp = client.put("/api/agent/default", json={"agent": "claude"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "default": "claude"}

    assert agent_prefs.load_default_agent() == "claude"  # really persisted
    body = _status(client)
    assert body["default"] == "claude"
    assert body["needs_setup"] is False


# ---------------------------------------------------------------------------
# AC6 — needs_setup truth table (all computed server-side)
# ---------------------------------------------------------------------------


def test_needs_setup_true_when_no_default(vault: Path) -> None:
    body = _status(_client(vault))
    assert body["default"] is None
    assert body["needs_setup"] is True


def test_needs_setup_true_when_default_not_detected(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_prefs, "load_default_agent", lambda: "claude")
    monkeypatch.setattr(agents, "detect", lambda spec: False)
    monkeypatch.setattr(agents, "aggregate_skill_state", lambda *a, **k: "current")
    assert _status(_client(vault))["needs_setup"] is True


def test_needs_setup_true_when_detected_but_no_skill(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_prefs, "load_default_agent", lambda: "claude")
    monkeypatch.setattr(agents, "detect", lambda spec: True)
    monkeypatch.setattr(agents, "aggregate_skill_state", lambda *a, **k: "absent")
    body = _status(_client(vault))
    assert body["skill_installed"] is False
    assert body["skill_state"] == "absent"
    assert body["needs_setup"] is True


def test_needs_setup_true_when_skill_stale(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale skill is part of needs_setup: the red dot must surface "your
    installed skill is out of date" the same way it surfaces "not installed" —
    otherwise the update is invisible until something breaks (the silent-
    failure mode this arm exists to kill)."""
    monkeypatch.setattr(agent_prefs, "load_default_agent", lambda: "claude")
    monkeypatch.setattr(agents, "detect", lambda spec: True)
    monkeypatch.setattr(agents, "aggregate_skill_state", lambda *a, **k: "stale")
    body = _status(_client(vault))
    assert body["skill_installed"] is True  # installed, just out of date
    assert body["skill_state"] == "stale"
    assert body["needs_setup"] is True


def test_needs_setup_false_when_detected_and_skill_installed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_prefs, "load_default_agent", lambda: "claude")
    monkeypatch.setattr(agents, "detect", lambda spec: True)
    monkeypatch.setattr(agents, "aggregate_skill_state", lambda *a, **k: "current")
    body = _status(_client(vault))
    assert body["skill_installed"] is True
    assert body["skill_state"] == "current"
    assert body["needs_setup"] is False


def test_needs_setup_default_agy_follows_antigravity_dir(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=agy row of the needs_setup matrix: the top-level skill_state
    follows the DEFAULT agent's directory (the Antigravity CLI dir), not
    claude's — real resolvers + real copies, only `detect` stubbed."""
    from litman.core import skill

    monkeypatch.setattr(agent_prefs, "load_default_agent", lambda: "agy")
    monkeypatch.setattr(agents, "detect", lambda spec: True)
    client = _client(vault)

    body = _status(client)
    assert body["skill_state"] == "absent"
    assert body["needs_setup"] is True

    # Installing into the CLAUDE dir must not satisfy an agy default.
    skill.install_all_skills(parent_dir=skill.default_skills_parent_dir())
    body = _status(client)
    assert body["skill_state"] == "absent"
    assert body["needs_setup"] is True

    skill.install_all_skills(
        parent_dir=skill.antigravity_skills_parent_dir()
    )
    body = _status(client)
    assert body["skill_state"] == "current"
    assert body["skill_installed"] is True
    assert body["needs_setup"] is False
