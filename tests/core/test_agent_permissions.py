"""Native per-agent command approvals installed with litman skills."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from litman.core import agent_permissions


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CURSOR_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    return tmp_path


@pytest.mark.no_skills_isolation
def test_claude_merges_allow_rule_and_is_idempotent(_home: Path) -> None:
    path = _home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"theme": "dark", "permissions": {"allow": ["Bash(git *)"]}}),
        encoding="utf-8",
    )

    first = agent_permissions.install_claude_lit_permission()
    second = agent_permissions.install_claude_lit_permission()

    assert first["mode"] == "updated"
    assert second["mode"] == "unchanged"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["permissions"]["allow"] == ["Bash(git *)", "Bash(lit *)"]


@pytest.mark.no_skills_isolation
def test_claude_adds_powershell_rule_on_windows(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    agent_permissions.install_claude_lit_permission()
    data = json.loads(
        (_home / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert data["permissions"]["allow"] == [
        "Bash(lit *)",
        "PowerShell(lit *)",
    ]


@pytest.mark.no_skills_isolation
def test_claude_honours_explicit_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    result = agent_permissions.install_claude_lit_permission()

    assert result["path"] == config_dir / "settings.json"
    data = json.loads(result["path"].read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(lit *)"]


@pytest.mark.no_skills_isolation
def test_antigravity_normalises_redundant_default_ask_catch_all(
    _home: Path,
) -> None:
    path = _home / ".gemini" / "antigravity-cli" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "colorScheme": "terminal",
                "permissions": {"ask": ["command(*)"]},
            }
        ),
        encoding="utf-8",
    )

    result = agent_permissions.install_antigravity_lit_permission()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["colorScheme"] == "terminal"
    assert data["permissions"]["allow"] == ["command(lit)"]
    assert data["permissions"]["ask"] == []
    assert result["warning"] is None


@pytest.mark.no_skills_isolation
def test_antigravity_keeps_broad_ask_when_removal_could_broaden_access(
    _home: Path,
) -> None:
    path = _home / ".gemini" / "antigravity-cli" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "toolPermission": "always-proceed",
                "permissions": {"ask": ["command(*)"]},
            }
        ),
        encoding="utf-8",
    )

    result = agent_permissions.install_antigravity_lit_permission()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["ask"] == ["command(*)"]
    assert data["permissions"]["allow"] == ["command(lit)"]
    assert "precedence" in result["warning"]


@pytest.mark.no_skills_isolation
def test_antigravity_keeps_strict_mode_and_reports_ignored_allowlist(
    _home: Path,
) -> None:
    path = _home / ".gemini" / "antigravity-cli" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "toolPermission": "strict",
                "permissions": {"ask": ["command(*)"]},
            }
        ),
        encoding="utf-8",
    )

    result = agent_permissions.install_antigravity_lit_permission()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["toolPermission"] == "strict"
    assert data["permissions"]["ask"] == ["command(*)"]
    assert data["permissions"]["allow"] == ["command(lit)"]
    assert result["warning"] is not None
    assert "strict mode" in result["warning"]


@pytest.mark.no_skills_isolation
def test_cursor_installs_only_shell_lit_rule(_home: Path) -> None:
    result = agent_permissions.install_cursor_lit_permission()
    path = _home / ".cursor" / "cli-config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert result["mode"] == "created"
    assert data == {"permissions": {"allow": ["Shell(lit)"]}}


@pytest.mark.no_skills_isolation
def test_cursor_reports_broad_shell_deny(_home: Path) -> None:
    path = _home / ".cursor" / "cli-config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"permissions": {"deny": ["Shell(*)"]}}),
        encoding="utf-8",
    )

    result = agent_permissions.install_cursor_lit_permission()

    assert result["warning"] is not None
    assert "Shell(*)" in result["warning"]


@pytest.mark.no_skills_isolation
def test_cursor_honours_explicit_and_xdg_config_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    via_xdg = agent_permissions.install_cursor_lit_permission()
    assert via_xdg["path"] == xdg / "cursor" / "cli-config.json"

    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(explicit))
    via_explicit = agent_permissions.install_cursor_lit_permission()
    assert via_explicit["path"] == explicit / "cli-config.json"


@pytest.mark.no_skills_isolation
def test_codex_uses_codex_home_and_does_not_replace_user_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    path = codex_home / "rules" / "litman.rules"

    first = agent_permissions.install_codex_lit_permission()
    second = agent_permissions.install_codex_lit_permission()
    assert first["mode"] == "created"
    assert second["mode"] == "unchanged"
    assert 'pattern = ["lit"]' in path.read_text(encoding="utf-8")

    path.write_text("# my own rules\n", encoding="utf-8")
    blocked = agent_permissions.install_codex_lit_permission()
    assert blocked["mode"] == "skipped"
    assert path.read_text(encoding="utf-8") == "# my own rules\n"


@pytest.mark.no_skills_isolation
def test_opencode_puts_specific_rules_after_existing_catch_all(_home: Path) -> None:
    path = _home / ".config" / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"permission": {"bash": {"*": "ask", "git *": "allow"}}}),
        encoding="utf-8",
    )

    result = agent_permissions.install_opencode_lit_permission()

    assert result["mode"] == "updated"
    bash = json.loads(path.read_text(encoding="utf-8"))["permission"]["bash"]
    assert list(bash) == ["*", "git *", "lit", "lit *"]
    assert bash["lit"] == bash["lit *"] == "allow"


@pytest.mark.no_skills_isolation
def test_opencode_does_not_destroy_commented_jsonc(_home: Path) -> None:
    root = _home / ".config" / "opencode"
    root.mkdir(parents=True)
    jsonc = root / "opencode.jsonc"
    original = '{\n  // keep my comment\n  "model": "x",\n}\n'
    jsonc.write_text(original, encoding="utf-8")

    result = agent_permissions.install_opencode_lit_permission()

    assert jsonc.read_text(encoding="utf-8") == original
    fallback = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert fallback["permission"]["bash"]["lit *"] == "allow"
    assert "non-strict JSONC" in result["warning"]


@pytest.mark.no_skills_isolation
def test_opencode_honours_explicit_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "custom" / "active-opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(path))

    result = agent_permissions.install_opencode_lit_permission()

    assert result["path"] == path
    bash = json.loads(path.read_text(encoding="utf-8"))["permission"]["bash"]
    assert bash == {"lit": "allow", "lit *": "allow"}


@pytest.mark.no_skills_isolation
def test_opencode_warns_for_higher_precedence_inline_permissions(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        json.dumps({"permission": {"bash": "ask"}}),
    )

    result = agent_permissions.install_opencode_lit_permission()

    assert result["warning"] is not None
    assert "OPENCODE_CONFIG_CONTENT" in result["warning"]


@pytest.mark.no_skills_isolation
def test_malformed_json_is_left_byte_for_byte_untouched(_home: Path) -> None:
    path = _home / ".cursor" / "cli-config.json"
    path.parent.mkdir(parents=True)
    original = b'{"permissions": '
    path.write_bytes(original)

    result = agent_permissions.install_cursor_lit_permission()

    assert result["mode"] == "skipped"
    assert result["warning"]
    assert path.read_bytes() == original
