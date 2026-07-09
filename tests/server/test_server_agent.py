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

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core import agent_prefs, agents
from litman.core.library import create_vault
from litman.core.skill import list_bundled_skills
from litman.server import create_app


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
    assert resp.json() == {"agents": ["claude"], "default": "claude"}


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


def test_launch_unsupported_agent_is_400(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A greyed placeholder (supported=False) is rejected before any PATH probe
    / copy-fallback — inert on the launch axis too."""
    spawned: list[object] = []
    monkeypatch.setattr(
        "litman.core.terminal.spawn_terminal",
        lambda a, c: spawned.append((a, c)) or True,
    )
    resp = _client(vault).post("/api/agent/launch", json={"agent": "codex"})
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
        "codex",
        "cursor",
        "gemini",
        "opencode",
    ]
    supported = {e["name"]: e["supported"] for e in body["agents"]}
    assert supported == {
        "claude": True,
        "codex": False,
        "cursor": False,
        "gemini": False,
        "opencode": False,
    }
    for e in body["agents"]:
        assert set(e) == {"name", "display", "supported", "detected", "install_url"}
        assert isinstance(e["detected"], bool)
    # top-level fields present
    assert "default" in body
    assert isinstance(body["skill_installed"], bool)
    assert isinstance(body["needs_setup"], bool)


def test_status_sets_no_store_cache_header(vault: Path) -> None:
    """`detected` / `skill_installed` are live machine state; the browser must
    never serve a cached body (else a plain reload keeps showing the red dot
    until a hard refresh)."""
    resp = _client(vault).get("/api/agent/status")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


def test_status_never_leaks_claude_skill_path(vault: Path) -> None:
    """The ~/.claude/skills filesystem path must never appear in the status
    contract (the install-url is docs.claude.com, which is fine — the red line
    is the skills *directory*, not the substring "claude")."""
    raw = _client(vault).get("/api/agent/status").text
    assert ".claude/skills" not in raw
    assert "/skills" not in raw


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
    real_probe = skill_mod.installed_skill_names
    # Redirect only the DESTINATION (the sanctioned $HOME/parent_dir redirect);
    # the real bundled-file copy still runs end to end.
    monkeypatch.setattr(
        agents,
        "install_all_skills",
        lambda overwrite=True: real_install(parent_dir=tmp_skills, overwrite=overwrite),
    )
    monkeypatch.setattr(
        agents,
        "installed_skill_names",
        lambda *a, **k: real_probe(parent_dir=tmp_skills),
    )

    client = _client(vault)
    assert _status(client)["skill_installed"] is False

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

    assert _status(client)["skill_installed"] is True


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


def test_skill_install_unsupported_agent_is_400(vault: Path) -> None:
    resp = _client(vault).post("/api/agent/skill/install", json={"agent": "codex"})
    assert resp.status_code == 400


def test_skill_install_unknown_agent_is_400(vault: Path) -> None:
    resp = _client(vault).post("/api/agent/skill/install", json={"agent": "nope"})
    assert resp.status_code == 400


def test_put_default_unknown_agent_is_400(vault: Path) -> None:
    resp = _client(vault).put("/api/agent/default", json={"agent": "nope"})
    assert resp.status_code == 400


def test_put_default_unsupported_agent_is_400(vault: Path) -> None:
    resp = _client(vault).put("/api/agent/default", json={"agent": "codex"})
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
    monkeypatch.setattr(agents, "installed_skill_names", lambda *a, **k: {"lit-library"})
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
    monkeypatch.setattr(agents, "installed_skill_names", lambda *a, **k: {"lit-library"})
    assert _status(_client(vault))["needs_setup"] is True


def test_needs_setup_true_when_detected_but_no_skill(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_prefs, "load_default_agent", lambda: "claude")
    monkeypatch.setattr(agents, "detect", lambda spec: True)
    monkeypatch.setattr(agents, "installed_skill_names", lambda *a, **k: set())
    body = _status(_client(vault))
    assert body["skill_installed"] is False
    assert body["needs_setup"] is True


def test_needs_setup_false_when_detected_and_skill_installed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_prefs, "load_default_agent", lambda: "claude")
    monkeypatch.setattr(agents, "detect", lambda spec: True)
    monkeypatch.setattr(agents, "installed_skill_names", lambda *a, **k: {"lit-library"})
    body = _status(_client(vault))
    assert body["skill_installed"] is True
    assert body["needs_setup"] is False
