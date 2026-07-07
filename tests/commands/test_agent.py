"""Tests for ``lit agent`` (task-agent-launch, ADR-020).

The command resolves the vault, looks NAME up in the config's ``agents``
map (default ``default_agent``), and hands the process over to the
configured command with the vault as working directory — POSIX via
``os.execvp``, Windows via a ``subprocess.run`` child whose exit code is
passed through.

One test drives the REAL path end-to-end through a true subprocess (no
monkeypatch): a fully-stubbed suite can stay green while the live exec path
is broken, so the injectable seam must have one un-stubbed consumer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import litman
from litman.cli import cli
from litman.commands.agent import agent_cmd
from litman.core.config import load_config
from litman.core.library import create_vault
from litman.exceptions import ConfigError, LitmanError


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _write_config(vault: Path, agents_yaml: str, default: str) -> None:
    """Replace the seed config with one carrying a custom agents map."""
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\n"
        f"agents:\n{agents_yaml}"
        f"default_agent: {default}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AC1 — real end-to-end spawn (no monkeypatch on the exec path)
# ---------------------------------------------------------------------------


def test_agent_real_spawn_runs_in_vault(vault: Path, tmp_path: Path) -> None:
    """A true ``lit agent <name>`` subprocess execs the configured command
    with the vault as cwd — the probe command prints its own getcwd()."""
    probe = f"{sys.executable} -c 'import os; print(os.getcwd())'"
    _write_config(vault, f"  probe: {probe}\n", "probe")

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    src_dir = Path(litman.__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    # Isolate from this machine's real vault registry; --library drives
    # discovery so the empty registry is never consulted anyway.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "litman", "agent", "probe", "--library", str(vault)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == vault.resolve()


# ---------------------------------------------------------------------------
# AC2 — friendly errors
# ---------------------------------------------------------------------------


def test_agent_unknown_name_lists_configured(vault: Path) -> None:
    result = CliRunner().invoke(agent_cmd, ["nope", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    message = str(result.exception)
    assert "Unknown agent 'nope'" in message
    assert "claude" in message  # the configured names are listed


def test_agent_command_missing_from_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("litman.commands.agent.shutil.which", lambda _n: None)
    result = CliRunner().invoke(agent_cmd, ["--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    assert "'claude' not found on PATH" in str(result.exception)


def test_agent_empty_command_is_friendly_error(vault: Path) -> None:
    _write_config(vault, '  blank: ""\n', "blank")
    result = CliRunner().invoke(agent_cmd, ["--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    assert "empty command" in str(result.exception)


def test_default_agent_must_be_registered(vault: Path) -> None:
    _write_config(vault, "  claude: claude\n", "codex")
    with pytest.raises(ConfigError, match="default_agent 'codex'"):
        load_config(vault)


# ---------------------------------------------------------------------------
# AC3 — exec/chdir mechanics (POSIX branch), zero-config default
# ---------------------------------------------------------------------------


@pytest.fixture
def exec_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    """Force the POSIX branch and capture chdir/execvp instead of running."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "litman.commands.agent.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(os, "chdir", lambda p: captured.setdefault("cwd", Path(p)))
    monkeypatch.setattr(
        os, "execvp", lambda file, argv: captured.update(file=file, argv=argv)
    )
    return captured


def test_agent_execs_shlex_split_argv_in_vault(
    vault: Path, exec_capture: dict[str, object]
) -> None:
    _write_config(vault, "  claude: claude --continue\n", "claude")
    result = CliRunner().invoke(agent_cmd, ["claude", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert exec_capture["file"] == "claude"
    assert exec_capture["argv"] == ["claude", "--continue"]
    assert Path(str(exec_capture["cwd"])).resolve() == vault.resolve()


def test_agent_zero_config_defaults_to_claude(
    vault: Path, exec_capture: dict[str, object]
) -> None:
    """A fresh seed vault needs no config edits: bare `lit agent` → claude."""
    result = CliRunner().invoke(agent_cmd, ["--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert exec_capture["argv"] == ["claude"]


def test_agent_windows_child_exit_code_passes_through(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "litman.commands.agent.shutil.which", lambda name: f"C:\\bin\\{name}.exe"
    )
    calls: dict[str, object] = {}

    def fake_run(argv: list[str], cwd: Path) -> SimpleNamespace:
        calls["argv"] = argv
        calls["cwd"] = cwd
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr("litman.commands.agent.subprocess.run", fake_run)
    result = CliRunner().invoke(agent_cmd, ["--library", str(vault)])
    assert result.exit_code == 7
    assert calls["argv"] == ["claude"]
    assert Path(str(calls["cwd"])).resolve() == vault.resolve()


# ---------------------------------------------------------------------------
# AC4 — seed defaults + config show surface
# ---------------------------------------------------------------------------


def test_fresh_vault_seeds_agent_defaults(vault: Path) -> None:
    cfg = load_config(vault)
    assert cfg.agents == {"claude": "claude"}
    assert cfg.default_agent == "claude"


def test_config_show_displays_agent_keys(vault: Path) -> None:
    result = CliRunner().invoke(cli, ["config", "show", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "agents" in result.output
    assert "default_agent" in result.output
