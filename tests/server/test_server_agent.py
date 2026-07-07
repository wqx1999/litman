"""Agent-launch endpoint tests (task-agent-launch AC5).

RED LINE (ADR-020): ``POST /api/agent/launch`` accepts an agent NAME only.
The spawned command always comes from the server-side vault config — a
command-like field in the request body must have ZERO effect on what is
spawned. The malicious-body test below is the regression gate for that.

Guarded with ``importorskip`` so the suite still collects when fastapi is
absent (invariant #5)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.library import create_vault
from litman.server import create_app


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _write_config(vault: Path, agents_yaml: str, default: str) -> None:
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\n"
        f"agents:\n{agents_yaml}"
        f"default_agent: {default}\n",
        encoding="utf-8",
    )


def test_get_agents_returns_configured_names(vault: Path) -> None:
    _write_config(vault, "  claude: claude\n  codex: codex\n", "codex")
    resp = _client(vault).get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == {"agents": ["claude", "codex"], "default": "codex"}


def test_get_agents_zero_config_default(vault: Path) -> None:
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
    """Malicious body: argv comes from config; the `command` field is dead."""
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
    monkeypatch.setattr(
        "litman.core.terminal.spawn_terminal", lambda _a, _c: True
    )
    resp = _client(vault).post("/api/agent/launch")
    assert resp.status_code == 200
    assert resp.json()["agent"] == "claude"
    assert resp.json()["mode"] == "spawned"


def test_launch_copy_fallback_wraps_as_lit_agent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No terminal available → mode "copy" with the `lit agent` wrapper (the
    raw command is only correct inside the vault; the wrapper works anywhere)."""
    _write_config(vault, "  claude: claude\n  codex: codex\n", "claude")
    monkeypatch.setattr(
        "litman.core.terminal.spawn_terminal", lambda _a, _c: False
    )
    client = _client(vault)

    resp = client.post("/api/agent/launch", json={})
    assert resp.json() == {
        "ok": True,
        "mode": "copy",
        "agent": "claude",
        "command": "lit agent",
    }

    resp = client.post("/api/agent/launch", json={"agent": "codex"})
    assert resp.json() == {
        "ok": True,
        "mode": "copy",
        "agent": "codex",
        "command": "lit agent codex",
    }


def test_launch_non_string_agent_is_400(vault: Path) -> None:
    resp = _client(vault).post("/api/agent/launch", json={"agent": ["claude"]})
    assert resp.status_code == 400
