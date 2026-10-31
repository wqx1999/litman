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

from fastapi import HTTPException, Response

from litman.core import agent_prefs, agents
from litman.core.library import create_vault
from litman.core.skill import list_bundled_skills
from litman.server import create_app, routes_agent
from litman.server.routes_agent import agent_status
from tests.server._client import TestClient


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
        "agents": [
            "claude",
            "zcode",
            "agy",
            "codex",
            "dsh",
            "cursor",
            "opencode",
        ],
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
    monkeypatch.setattr(agents, "detect", lambda _spec: True)
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
        "permission_warning": None,
    }
    assert spawned == [(["claude"], vault)]


def test_launch_bodyless_uses_default_agent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "detect", lambda _spec: True)
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
    monkeypatch.setattr(agents, "detect", lambda _spec: True)
    monkeypatch.setattr("litman.core.terminal.spawn_terminal", lambda _a, _c: False)
    resp = _client(vault).post("/api/agent/launch", json={})
    assert resp.json() == {
        "ok": True,
        "mode": "copy",
        "agent": "claude",
        "command": "lit agent",
        "permission_warning": None,
    }


def test_launch_windows_passes_resolved_absolute_command_to_terminal(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(agents, "detect", lambda _spec: True)
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


def test_launch_rejects_agent_that_is_not_installed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "detect", lambda _spec: False)
    spawned: list[object] = []
    monkeypatch.setattr(
        "litman.core.terminal.spawn_terminal",
        lambda argv, cwd: spawned.append((argv, cwd)) or True,
    )

    async def body_agent_name(_request: object) -> str:
        return "codex"

    monkeypatch.setattr(routes_agent, "_body_agent_name", body_agent_name)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(vault=vault)))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_agent.launch_agent(request))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 409
    assert "not installed" in exc_info.value.detail
    assert spawned == []


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
        brand="Future Corp",
        launch="future",
        supported=False,
        install_url="https://example.invalid/future",
        detect_bin="future",
        skill_state=agents._unsupported("future"),
        install_skill=agents._unsupported("future"),
        install_lit_permission=agents._unsupported("future"),
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


def test_status_returns_seven_catalog_entries(vault: Path) -> None:
    body = _status(_client(vault))
    assert [e["name"] for e in body["agents"]] == [
        "claude",
        "zcode",
        "agy",
        "codex",
        "dsh",
        "cursor",
        "opencode",
    ]
    supported = {e["name"]: e["supported"] for e in body["agents"]}
    assert supported == {
        "claude": True,
        "zcode": True,
        "agy": True,
        "codex": True,
        "dsh": True,
        "cursor": True,
        "opencode": True,
    }
    for e in body["agents"]:
        assert set(e) == {
            "name",
            "display",
            "brand",
            "supported",
            "detected",
            "install_url",
            "skill_state",
            "kind",
            "mode",
            "modes",
        }
        assert isinstance(e["detected"], bool)
        assert e["kind"] in {"terminal", "app"}
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
    # A desktop app is found through its own registry key, not PATH.
    assert all(
        entry["detected"] for entry in after["agents"] if entry["kind"] == "terminal"
    )


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
        brand="Future Corp",
        launch="future",
        supported=False,
        install_url="https://example.invalid/future",
        detect_bin="future",
        skill_state=agents._unsupported("future"),
        install_skill=agents._unsupported("future"),
        install_lit_permission=agents._unsupported("future"),
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


def test_skill_install_dsh_lands_in_the_open_standard_dir(vault: Path) -> None:
    """The agent panel's install for DeepSeek Harness copies the skills into
    the open-standard dir (no adapter stubbed), writes no approval rule, and
    its row then reads up to date."""
    from litman.core import skill

    client = _client(vault)
    resp = client.post("/api/agent/skill/install", json={"agent": "dsh"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "dsh"
    assert body["permission"] == {"mode": "manual", "rule": "", "warning": None}

    standard = skill.standard_skills_parent_dir()
    for name in list_bundled_skills():
        assert (standard / name / "SKILL.md").is_file()
    assert not skill.default_skills_parent_dir().exists()
    assert not skill.antigravity_skills_parent_dir().exists()
    rows = {e["name"]: e["skill_state"] for e in _status(client)["agents"]}
    assert rows["dsh"] == "current"


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
        "permission": {
            "mode": "unchanged",
            "rule": "test-isolated",
            "warning": None,
        },
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


def test_put_default_rejects_agent_that_is_not_installed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "detect", lambda _spec: False)
    async def body_agent_name(_request: object) -> str:
        return "codex"

    monkeypatch.setattr(routes_agent, "_body_agent_name", body_agent_name)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_agent.set_default_agent(object()))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 409
    assert "not installed" in exc_info.value.detail
    assert agent_prefs.load_default_agent() is None


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


# ---------------------------------------------------------------------------
# A desktop-app agent (task-zcode-agent): opened, not spawned
# ---------------------------------------------------------------------------


def _record_open_app(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    opened: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "litman.core.app_launch.open_app",
        lambda spec, vault: opened.append((spec.name, vault)),
    )
    monkeypatch.setattr(
        "litman.core.terminal.spawn_terminal",
        lambda _a, _c: pytest.fail("an app must never go through a terminal"),
    )
    return opened


def test_launch_app_opens_it_at_the_server_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "detect", lambda _spec: True)
    opened = _record_open_app(monkeypatch)
    resp = _client(vault).post("/api/agent/launch", json={"agent": "zcode"})
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "mode": "app",
        "agent": "zcode",
        "command": "",
        "permission_warning": None,
    }
    assert opened == [("zcode", vault)]


def test_launch_app_ignores_body_command_and_path_fields(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malicious body: the app opens at the server's vault; `command` and
    `path` are dead."""
    monkeypatch.setattr(agents, "detect", lambda _spec: True)
    opened = _record_open_app(monkeypatch)
    resp = _client(vault).post(
        "/api/agent/launch",
        json={"agent": "zcode", "command": "rm -rf /", "path": str(tmp_path / "x")},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "app"
    assert opened == [("zcode", vault)]


@pytest.mark.parametrize(
    ("default", "command"), [("claude", "lit agent zcode"), ("zcode", "lit agent")]
)
def test_launch_app_without_a_display_falls_back_to_copy(
    vault: Path, monkeypatch: pytest.MonkeyPatch, default: str, command: str
) -> None:
    from litman.core import app_launch

    monkeypatch.setattr(agents, "detect", lambda _spec: True)
    monkeypatch.setattr(agent_prefs, "load_default_agent", lambda: default)

    def no_display(spec: object, _vault: Path) -> None:
        raise app_launch.AppNoDisplay("No display here to open ZCode in.")

    monkeypatch.setattr("litman.core.app_launch.open_app", no_display)
    resp = _client(vault).post("/api/agent/launch", json={"agent": "zcode"})
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "mode": "copy",
        "agent": "zcode",
        "command": command,
        "permission_warning": None,
    }


def test_launch_app_not_installed_is_409_without_path_talk(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "detect", lambda _spec: False)
    opened = _record_open_app(monkeypatch)
    resp = _client(vault).post("/api/agent/launch", json={"agent": "zcode"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "hasn't been opened yet" in detail
    assert "PATH" not in detail
    assert opened == []


def test_launch_app_that_fails_to_start_is_409(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core import app_launch

    monkeypatch.setattr(agents, "detect", lambda _spec: True)

    def broken(spec: object, _vault: Path) -> None:
        raise app_launch.AppNotFound("Could not open ZCode: access denied")

    monkeypatch.setattr("litman.core.app_launch.open_app", broken)
    resp = _client(vault).post("/api/agent/launch", json={"agent": "zcode"})
    assert resp.status_code == 409
    assert "access denied" in resp.json()["detail"]


def test_put_default_app_not_installed_is_409_without_path_talk(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "detect", lambda _spec: False)
    resp = _client(vault).put("/api/agent/default", json={"agent": "zcode"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "cannot be the default" in detail
    assert "hasn't been opened yet" in detail
    assert "PATH" not in detail
    assert agent_prefs.load_default_agent() is None


def test_status_never_leaks_the_app_exe_path(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exe is found in the registry and used server-side only."""
    from litman.core import app_launch

    exe = tmp_path / "Secret Apps" / "ZCode" / "ZCode.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(app_launch, "_platform", lambda: "win32")
    monkeypatch.setattr(
        app_launch, "_read_scheme_command", lambda _scheme: f'"{exe}" -- "%1"'
    )
    resp = _client(vault).get("/api/agent/status")
    zcode = next(e for e in resp.json()["agents"] if e["name"] == "zcode")
    assert zcode["detected"] is True
    assert zcode["kind"] == "app"
    # The Autonomous-mode hint is a CLI line (install-skill / setup); the GUI
    # never shows it, so the status payload doesn't carry it.
    assert "permission_hint" not in zcode
    assert "Secret Apps" not in resp.text
    assert "ZCode.exe" not in resp.text


def test_launch_dsh_on_windows_starts_its_exe_with_its_link(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicking DeepSeek Harness in the picker: the real app_launch, faked down
    to the registry and the process start, starts DSH's exe with `dsh://open`
    and keeps it out of the server's console."""
    from litman.core import app_launch

    exe = tmp_path / "dsh" / "DeepSeek Harness.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(app_launch, "_platform", lambda: "win32")
    monkeypatch.setattr(
        app_launch, "_read_scheme_command", lambda _scheme: f'"{exe}" "%1"'
    )
    started: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        app_launch,
        "_popen_detached_windows",
        lambda argv, env: started.append((argv, env)),
    )
    monkeypatch.setattr(
        app_launch,
        "_open_link_detached_windows",
        lambda _url: pytest.fail("the system opener lets DSH log into our console"),
    )

    resp = _client(vault).post("/api/agent/launch", json={"agent": "dsh"})

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "mode": "app",
        "agent": "dsh",
        "command": "",
        "permission_warning": None,
    }
    [(argv, env)] = started
    assert argv == [str(exe), "dsh://open"]
    assert env["ELECTRON_NO_ATTACH_CONSOLE"] == "1"


def test_put_default_dsh_on_linux_is_409(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DSH has no Linux desktop app, so on Linux it is not installed and
    cannot become the default."""
    from litman.core import app_launch

    monkeypatch.setattr(app_launch, "_platform", lambda: "linux")
    resp = _client(vault).put("/api/agent/default", json={"agent": "dsh"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Agent 'dsh' cannot be the default: DeepSeek Harness isn't installed, "
        "or hasn't been opened yet. Open it once, then try again."
    )
    assert agent_prefs.load_default_agent() is None


# ---------------------------------------------------------------------------
# An agent with both a CLI and an app (task-agent-launch-modes)
# ---------------------------------------------------------------------------

_CLAUDE_CLI_URL = "https://docs.claude.com/en/docs/claude-code/overview"
_CLAUDE_APP_URL = "https://claude.com/download"
_SSH = "1.2.3.4 5 6.7.8.9 22"


def _machine(
    monkeypatch: pytest.MonkeyPatch, *, terminal: bool, app: bool
) -> dict[str, list]:
    """A desktop machine with the CLI and the app installed or not; records
    what a launch starts."""
    from litman.core import app_launch

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.setattr(app_launch, "_platform", lambda: "win32")
    monkeypatch.setattr(
        agents.shutil, "which", lambda name: f"/usr/bin/{name}" if terminal else None
    )
    monkeypatch.setattr(app_launch, "app_installed", lambda _spec: app)
    started: dict[str, list] = {"terminal": [], "app": []}
    monkeypatch.setattr(
        "litman.core.terminal.spawn_terminal",
        lambda argv, _cwd: started["terminal"].append(argv) or True,
    )
    monkeypatch.setattr(
        app_launch,
        "open_app",
        lambda spec, _vault: started["app"].append(spec.name),
    )
    return started


def _launch_claude(vault: Path) -> dict:
    resp = _client(vault).post("/api/agent/launch", json={"agent": "claude"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _claude_entry(vault: Path) -> dict:
    return next(e for e in _status(_client(vault))["agents"] if e["name"] == "claude")


def test_launch_with_both_installed_and_no_choice_goes_to_the_cli(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _machine(monkeypatch, terminal=True, app=True)
    assert _launch_claude(vault) == {
        "ok": True,
        "mode": "spawned",
        "agent": "claude",
        "command": "claude",
        "permission_warning": None,
    }
    assert started == {"terminal": [["claude"]], "app": []}


def test_launch_with_the_app_saved_opens_it_and_says_to_confirm(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _machine(monkeypatch, terminal=True, app=True)
    agent_prefs.save_launch_mode("claude", "app")
    assert _launch_claude(vault) == {
        "ok": True,
        "mode": "app",
        "agent": "claude",
        "command": "",
        "permission_warning": None,
        "hint": "Opened Claude. Confirm the folder to start.",
    }
    assert started == {"terminal": [], "app": ["claude"]}


def test_launch_passes_over_a_saved_choice_not_usable_without_forgetting_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _machine(monkeypatch, terminal=True, app=False)
    agent_prefs.save_launch_mode("claude", "app")
    assert _launch_claude(vault)["mode"] == "spawned"
    assert started["app"] == []
    assert agent_prefs.load_launch_modes() == {"claude": "app"}


def test_launch_with_only_the_app_installed_opens_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _machine(monkeypatch, terminal=False, app=True)
    assert _launch_claude(vault)["mode"] == "app"
    assert started == {"terminal": [], "app": ["claude"]}


def test_launch_with_neither_installed_keeps_the_cli_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _machine(monkeypatch, terminal=False, app=False)
    resp = _client(vault).post("/api/agent/launch", json={"agent": "claude"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Agent 'claude' is not installed or is not available on PATH. "
        "Install it, then recheck."
    )
    assert started == {"terminal": [], "app": []}


def test_catalog_default_app_opens_it_and_neither_installed_talks_app(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row M4 will add: both ways, defaulting to the app, end to end."""
    import dataclasses

    row = dataclasses.replace(agents.get_agent("claude"), default_mode="app")
    monkeypatch.setattr(
        agents, "AGENTS", tuple(row if a.name == "claude" else a for a in agents.AGENTS)
    )
    started = _machine(monkeypatch, terminal=True, app=True)
    assert _launch_claude(vault)["mode"] == "app"
    assert started["app"] == ["claude"]
    assert _claude_entry(vault)["mode"] == "app"

    _machine(monkeypatch, terminal=False, app=False)
    resp = _client(vault).post("/api/agent/launch", json={"agent": "claude"})
    assert resp.status_code == 409
    assert "hasn't been opened yet" in resp.json()["detail"]
    assert "PATH" not in resp.json()["detail"]


def test_launch_over_ssh_never_tries_the_app(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _machine(monkeypatch, terminal=True, app=True)
    monkeypatch.setenv("SSH_CONNECTION", _SSH)
    agent_prefs.save_launch_mode("claude", "app")
    assert _launch_claude(vault)["mode"] == "spawned"
    assert started == {"terminal": [["claude"]], "app": []}


def test_launch_on_linux_without_a_display_never_tries_the_app(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core import app_launch

    started = _machine(monkeypatch, terminal=True, app=True)
    monkeypatch.setattr(app_launch, "_platform", lambda: "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    agent_prefs.save_launch_mode("claude", "app")
    assert _launch_claude(vault)["mode"] == "spawned"
    assert started["app"] == []


def test_launch_claude_on_windows_goes_through_the_short_lived_child(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real app_launch, faked down to the handler probe and Popen."""
    from litman.core import app_launch

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.setattr(app_launch, "_platform", lambda: "win32")
    monkeypatch.setattr(app_launch, "_scheme_has_handler", lambda _scheme: True)
    monkeypatch.setattr(agents.shutil, "which", lambda _name: None)
    started: list[list[str]] = []
    monkeypatch.setattr(
        app_launch.subprocess, "Popen", lambda argv, **_kw: started.append(argv)
    )

    assert _launch_claude(vault)["mode"] == "app"

    [argv] = started
    assert argv[:2] == [sys.executable, "-c"]
    assert argv[-1] == app_launch.workspace_url(agents.get_agent("claude"), vault)
    assert argv[-1].startswith("claude://code/new?folder=")


def test_status_lists_both_ways_with_their_own_downloads(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machine(monkeypatch, terminal=True, app=False)
    entry = _claude_entry(vault)
    assert entry["mode"] == "terminal"
    assert entry["modes"] == [
        {"mode": "terminal", "usable": True, "install_url": _CLAUDE_CLI_URL},
        {"mode": "app", "usable": False, "install_url": _CLAUDE_APP_URL},
    ]


@pytest.mark.parametrize(
    ("terminal", "app", "saved", "mode", "detected"),
    [
        (True, True, None, "terminal", True),
        (True, True, "app", "app", True),
        (True, False, "app", "terminal", True),
        (False, True, None, "app", True),
        (False, True, "terminal", "app", True),
        (False, False, "app", "terminal", False),
    ],
)
def test_status_mode_is_what_the_button_would_do(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
    app: bool,
    saved: str | None,
    mode: str,
    detected: bool,
) -> None:
    _machine(monkeypatch, terminal=terminal, app=app)
    if saved:
        agent_prefs.save_launch_mode("claude", saved)
    entry = _claude_entry(vault)
    assert entry["mode"] == mode
    assert entry["detected"] is detected


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.parametrize("app", [True, False])
def test_status_kind_of_a_row_with_both_stays_terminal_for_the_shipped_panel(
    vault: Path, monkeypatch: pytest.MonkeyPatch, terminal: bool, app: bool
) -> None:
    _machine(monkeypatch, terminal=terminal, app=app)
    assert _claude_entry(vault)["kind"] == "terminal"


def test_status_over_ssh_lists_no_app_for_a_row_with_both(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machine(monkeypatch, terminal=False, app=True)
    monkeypatch.setenv("SSH_CONNECTION", _SSH)
    entry = _claude_entry(vault)
    assert entry["mode"] == "terminal"
    assert entry["detected"] is False
    assert entry["modes"] == [
        {"mode": "terminal", "usable": False, "install_url": _CLAUDE_CLI_URL}
    ]


def test_status_of_every_row_with_one_way_is_unchanged_by_the_session(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: over SSH too, each other row lists its one way as before."""
    monkeypatch.setenv("SSH_CONNECTION", _SSH)
    monkeypatch.setattr(agents.shutil, "which", lambda name: f"/usr/bin/{name}")
    entries = {e["name"]: e for e in _status(_client(vault))["agents"]}
    expected = {
        "zcode": ("app", False, "https://zcode.z.ai"),
        "dsh": ("app", False, "https://www.deepseek.com/harness/"),
        "agy": ("terminal", True, "https://antigravity.google/download#antigravity-cli"),
        "codex": ("terminal", True, "https://developers.openai.com/codex/cli/"),
        "cursor": ("terminal", True, "https://cursor.com/cli"),
        "opencode": ("terminal", True, "https://opencode.ai/"),
    }
    for name, (mode, usable, url) in expected.items():
        assert entries[name]["kind"] == mode
        assert entries[name]["mode"] == mode
        assert entries[name]["detected"] is usable
        assert entries[name]["install_url"] == url
        assert entries[name]["modes"] == [
            {"mode": mode, "usable": usable, "install_url": url}
        ]


def test_a_row_with_one_way_launches_without_a_hint(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved choice the row does not have changes nothing."""
    started = _machine(monkeypatch, terminal=True, app=True)
    path = agent_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("launch_modes:\n  codex: app\n  zcode: terminal\n", encoding="utf-8")

    codex = _client(vault).post("/api/agent/launch", json={"agent": "codex"}).json()
    assert codex["mode"] == "spawned"
    assert started == {"terminal": [["codex"]], "app": []}

    zcode = _client(vault).post("/api/agent/launch", json={"agent": "zcode"}).json()
    assert zcode == {
        "ok": True,
        "mode": "app",
        "agent": "zcode",
        "command": "",
        "permission_warning": None,
    }
    assert started["app"] == ["zcode"]


def test_put_launch_mode_saves_and_status_and_launch_follow(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _machine(monkeypatch, terminal=True, app=True)
    resp = _client(vault).put(
        "/api/agent/launch-mode", json={"agent": "claude", "mode": "app"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "agent": "claude", "mode": "app"}
    assert _claude_entry(vault)["mode"] == "app"
    assert _launch_claude(vault)["mode"] == "app"
    assert started["app"] == ["claude"]

    assert (
        _client(vault)
        .put("/api/agent/launch-mode", json={"agent": "claude", "mode": "terminal"})
        .status_code
        == 200
    )
    assert _claude_entry(vault)["mode"] == "terminal"


def test_put_launch_mode_reads_only_agent_and_mode(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malicious body: `command` and `path` reach neither the file nor a launch."""
    started = _machine(monkeypatch, terminal=True, app=True)
    resp = _client(vault).put(
        "/api/agent/launch-mode",
        json={
            "agent": "claude",
            "mode": "app",
            "command": "rm -rf /",
            "path": str(tmp_path / "x"),
            "target": "evil",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "agent": "claude", "mode": "app"}
    assert started == {"terminal": [], "app": []}
    text = agent_prefs.prefs_path().read_text(encoding="utf-8")
    assert "rm -rf" not in text
    assert "evil" not in text


@pytest.mark.parametrize(
    "body",
    [
        {"agent": "nope", "mode": "app"},
        {"agent": "claude", "mode": "fullscreen"},
        {"agent": "codex", "mode": "app"},
        {"agent": "zcode", "mode": "terminal"},
        {"agent": "claude"},
        {"mode": "app"},
        {"agent": "claude", "mode": 1},
        {"agent": ["claude"], "mode": "app"},
        ["claude", "app"],
    ],
    ids=[
        "unknown-agent",
        "illegal-mode",
        "cli-row-has-no-app",
        "app-row-has-no-cli",
        "no-mode",
        "no-agent",
        "mode-not-a-string",
        "agent-not-a-string",
        "not-a-mapping",
    ],
)
def test_put_launch_mode_refuses_with_400(vault: Path, body: object) -> None:
    resp = _client(vault).put("/api/agent/launch-mode", json=body)
    assert resp.status_code == 400
    assert not agent_prefs.prefs_path().exists()


def test_put_launch_mode_malformed_body_is_400(vault: Path) -> None:
    resp = _client(vault).put(
        "/api/agent/launch-mode",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_put_launch_mode_keeps_the_default_agent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machine(monkeypatch, terminal=True, app=True)
    client = _client(vault)
    assert client.put("/api/agent/default", json={"agent": "codex"}).status_code == 200
    client.put("/api/agent/launch-mode", json={"agent": "claude", "mode": "app"})
    assert _status(client)["default"] == "codex"
    assert client.put("/api/agent/default", json={"agent": "cursor"}).status_code == 200
    assert agent_prefs.load_launch_modes() == {"claude": "app"}


def test_put_default_row_with_both_not_installed_keeps_the_cli_wording(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machine(monkeypatch, terminal=False, app=False)
    resp = _client(vault).put("/api/agent/default", json={"agent": "claude"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "Agent 'claude' cannot be the default because it is not installed or "
        "is not available on PATH."
    )


def test_put_default_row_with_only_its_app_here_is_accepted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machine(monkeypatch, terminal=False, app=True)
    assert (
        _client(vault).put("/api/agent/default", json={"agent": "claude"}).status_code
        == 200
    )


def test_status_with_both_never_leaks_a_process_or_skill_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _machine(monkeypatch, terminal=True, app=True)
    resp = _client(vault).get("/api/agent/status")
    assert "/usr/bin" not in resp.text
    assert sys.executable not in resp.text
