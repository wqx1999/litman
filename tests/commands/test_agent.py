"""Tests for ``lit agent`` (task-agent-onboarding; ADR-020 / ADR-021).

The command resolves the vault, resolves which agent to launch (explicit NAME
→ machine-level default in preferences.yaml → catalog fallback), looks the
launch command up in the code-level catalog, and hands the process over with
the vault as working directory — POSIX via ``os.execvp``, Windows via a
``subprocess.run`` child whose exit code is passed through.

One test drives the REAL exec path end-to-end through a true subprocess with a
fake ``claude`` on PATH (no monkeypatch on the exec seam): a fully-stubbed
suite can stay green while the live exec path is broken, so the injectable
seam must have one un-stubbed consumer (M34 inject-seam lesson).
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
from litman.commands.agent import agent_cmd
from litman.core import agent_prefs, agents
from litman.core.library import create_vault
from litman.exceptions import LitmanError


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ---------------------------------------------------------------------------
# Real end-to-end spawn — no monkeypatch on the exec path (inject-seam lesson)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the stand-in `claude` is an extensionless #!/usr/bin/env script made "
        "runnable with chmod 0o755 — neither the shebang nor the mode means "
        "anything to Windows, and PATHEXT will not resolve a name without a "
        "suffix, so shutil.which never finds it. A .cmd shim would test the "
        "shim, not the exec path this covers."
    ),
)
def test_agent_real_spawn_execs_catalog_default_in_vault(
    vault: Path, tmp_path: Path
) -> None:
    """A true ``lit agent`` subprocess execs the catalog default (``claude``)
    with the vault as cwd. A fake ``claude`` on PATH (which prints its getcwd)
    stands in for the real CLI so the exec path runs unmocked."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text("#!/usr/bin/env python3\nimport os\nprint(os.getcwd())\n")
    fake.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    src_dir = Path(litman.__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    # Isolate from this machine's real vault registry; --library drives
    # discovery so the empty registry is never consulted anyway.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "litman", "agent", "--library", str(vault)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == vault.resolve()


# ---------------------------------------------------------------------------
# Resolution + friendly errors
# ---------------------------------------------------------------------------


def test_agent_unknown_name_lists_catalog(vault: Path) -> None:
    result = CliRunner().invoke(agent_cmd, ["nope", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    message = str(result.exception)
    assert "Unknown agent 'nope'" in message
    assert "claude" in message  # the catalog names are listed


def test_agent_unsupported_name_is_rejected(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``supported=False`` placeholder is rejected with the distinct "not
    available yet" message before the PATH probe / exec — even if its binary is
    installed. No greyed agent ships today, so a synthetic one is injected to
    exercise the dormant CLI gating branch (not a live catalog instance)."""
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
    execd: list[object] = []
    monkeypatch.setattr(agents, "resolve_launch", lambda spec: f"/usr/bin/{spec.name}")
    monkeypatch.setattr(os, "execvp", lambda file, argv: execd.append((file, argv)))
    result = CliRunner().invoke(agent_cmd, ["future", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    message = str(result.exception)
    assert "'future' is not available yet" in message
    assert (
        "Supported agents: claude, zcode, agy, codex, dsh, cursor, opencode"
        in message
    )
    assert execd == []  # never reached the exec path


def test_agent_command_missing_from_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "resolve_launch", lambda _spec: None)
    result = CliRunner().invoke(agent_cmd, ["--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    assert "'claude' not found on PATH" in str(result.exception)


# ---------------------------------------------------------------------------
# exec/chdir mechanics (POSIX branch), zero-config default, Windows passthrough
# ---------------------------------------------------------------------------


@pytest.fixture
def exec_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Force the POSIX branch and capture chdir/execvp instead of running."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(agents, "resolve_launch", lambda spec: f"/usr/bin/{spec.name}")
    monkeypatch.setattr(os, "chdir", lambda p: captured.setdefault("cwd", Path(p)))
    monkeypatch.setattr(
        os, "execvp", lambda file, argv: captured.update(file=file, argv=argv)
    )
    return captured


def test_agent_zero_config_defaults_to_claude(
    vault: Path, exec_capture: dict[str, object]
) -> None:
    """A fresh machine needs no config: bare `lit agent` → catalog default."""
    result = CliRunner().invoke(agent_cmd, ["--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert exec_capture["file"] == "claude"
    assert exec_capture["argv"] == ["claude"]
    assert Path(str(exec_capture["cwd"])).resolve() == vault.resolve()


def test_agent_explicit_name_execs_that_catalog_entry(
    vault: Path, exec_capture: dict[str, object]
) -> None:
    result = CliRunner().invoke(agent_cmd, ["claude", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert exec_capture["argv"] == ["claude"]


def test_agent_windows_child_exit_code_passes_through(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        agents, "resolve_launch", lambda spec: f"C:\\bin\\{spec.name}.exe"
    )
    # The default agent has an app too, so its CLI is probed to choose.
    monkeypatch.setattr(agents.shutil, "which", lambda name: f"C:\\bin\\{name}.exe")
    calls: dict[str, object] = {}

    def fake_run(argv: list[str], cwd: Path) -> SimpleNamespace:
        calls["argv"] = argv
        calls["cwd"] = cwd
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr("litman.commands.agent.subprocess.run", fake_run)
    result = CliRunner().invoke(agent_cmd, ["--library", str(vault)])
    assert result.exit_code == 7
    assert calls["argv"] == [r"C:\bin\claude.exe"]
    assert Path(str(calls["cwd"])).resolve() == vault.resolve()


def test_agent_windows_runs_npm_cmd_shim_through_cmd(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    codex = r"C:\Users\Wang\AppData\Roaming\npm\codex.CMD"
    monkeypatch.setattr(agents, "resolve_launch", lambda _spec: codex)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "litman.commands.agent.subprocess.run",
        lambda argv, cwd: calls.append(argv) or SimpleNamespace(returncode=0),
    )

    result = CliRunner().invoke(
        agent_cmd, ["codex", "--library", str(vault)]
    )
    assert result.exit_code == 0
    assert calls == [["cmd", "/c", codex]]


# ---------------------------------------------------------------------------
# --set-default writes the machine-level preference (no vault needed)
# ---------------------------------------------------------------------------


def test_set_default_records_machine_preference() -> None:
    result = CliRunner().invoke(agent_cmd, ["--set-default", "claude"])
    assert result.exit_code == 0, result.output
    assert "Default agent set to 'claude'." in result.output
    assert agent_prefs.load_default_agent() == "claude"


def test_set_default_rejects_unknown_agent() -> None:
    result = CliRunner().invoke(agent_cmd, ["--set-default", "nope"])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    assert "not a supported agent" in str(result.exception)


def test_agent_launches_saved_default(
    vault: Path, exec_capture: dict[str, object]
) -> None:
    """A machine-level default set via prefs drives a bare `lit agent`."""
    agent_prefs.save_default_agent("claude")
    result = CliRunner().invoke(agent_cmd, ["--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert exec_capture["argv"] == ["claude"]


# ---------------------------------------------------------------------------
# A desktop-app agent (task-zcode-agent): open it and return
# ---------------------------------------------------------------------------


def test_agent_app_opens_at_the_vault_and_returns(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core import app_launch

    opened: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        app_launch, "open_app", lambda spec, at: opened.append((spec.name, at))
    )
    monkeypatch.setattr(os, "execvp", lambda *_a: pytest.fail("an app is never exec'd"))
    monkeypatch.setattr(
        "litman.commands.agent.subprocess.run",
        lambda *_a, **_k: pytest.fail("an app is never waited on"),
    )
    result = CliRunner().invoke(agent_cmd, ["zcode", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert opened == [("zcode", vault)]
    assert result.output.replace("\n", "") == f"Opened ZCode at {vault}."


def test_agent_app_not_installed_names_the_download(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core import app_launch

    monkeypatch.setattr(app_launch, "app_installed", lambda _spec: False)
    result = CliRunner().invoke(agent_cmd, ["zcode", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    message = str(result.exception)
    assert "hasn't been opened yet" in message
    assert "https://zcode.z.ai" in message
    assert "PATH" not in message


def test_agent_dsh_on_windows_starts_its_exe_and_does_not_claim_the_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real app_launch, faked down to the registry and the
    process start: `lit agent dsh` starts DSH's exe with its bare link and
    returns. DSH reopens its last session, so the line names no vault."""
    from litman.core import app_launch

    exe = tmp_path / "dsh" / "DeepSeek Harness.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(app_launch, "_platform", lambda: "win32")
    monkeypatch.setattr(
        app_launch, "_read_scheme_command", lambda _scheme: f'"{exe}" "%1"'
    )
    started: list[list[str]] = []
    monkeypatch.setattr(
        app_launch, "_popen_detached_windows", lambda argv, _env: started.append(argv)
    )
    monkeypatch.setattr(
        app_launch,
        "_open_link_detached_windows",
        lambda _url: pytest.fail("the system opener lets DSH log into our console"),
    )

    result = CliRunner().invoke(agent_cmd, ["dsh", "--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert started == [[str(exe), "dsh://open"]]
    assert result.output == "Opened DeepSeek Harness.\n"


def test_agent_dsh_never_opened_names_the_download(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core import app_launch

    monkeypatch.setattr(app_launch, "_platform", lambda: "win32")
    result = CliRunner().invoke(agent_cmd, ["dsh", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    assert str(result.exception) == (
        "DeepSeek Harness isn't installed, or hasn't been opened yet. "
        "Open it once, then try again. "
        "Get DeepSeek Harness: https://www.deepseek.com/harness/"
    )


# ---------------------------------------------------------------------------
# An agent with both a CLI and an app (task-agent-launch-modes): the terminal
# first, whatever the GUI button saved
# ---------------------------------------------------------------------------


def _claude_installed(
    monkeypatch: pytest.MonkeyPatch, *, terminal: bool, app: bool
) -> list[tuple[str, Path]]:
    """A desktop machine; returns the record of apps opened."""
    from litman.core import app_launch

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.setattr(app_launch, "_platform", lambda: "win32")
    monkeypatch.setattr(
        agents.shutil, "which", lambda name: f"/usr/bin/{name}" if terminal else None
    )
    monkeypatch.setattr(app_launch, "app_installed", lambda _spec: app)
    opened: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        app_launch, "open_app", lambda spec, at: opened.append((spec.name, at))
    )
    return opened


def test_agent_with_both_runs_in_the_terminal_even_with_the_app_saved(
    vault: Path, monkeypatch: pytest.MonkeyPatch, exec_capture: dict[str, object]
) -> None:
    opened = _claude_installed(monkeypatch, terminal=True, app=True)
    agent_prefs.save_launch_mode("claude", "app")
    result = CliRunner().invoke(agent_cmd, ["claude", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert exec_capture["argv"] == ["claude"]
    assert opened == []


def test_agent_with_only_its_app_here_opens_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = _claude_installed(monkeypatch, terminal=False, app=True)
    monkeypatch.setattr(os, "execvp", lambda *_a: pytest.fail("no CLI to exec"))
    result = CliRunner().invoke(agent_cmd, ["claude", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert opened == [("claude", vault)]
    assert result.output == f"Opened Claude Code at {vault}.\n"


def test_agent_with_both_and_neither_here_names_the_cli_download(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = _claude_installed(monkeypatch, terminal=False, app=False)
    monkeypatch.setattr(agents, "resolve_launch", lambda _spec: None)
    result = CliRunner().invoke(agent_cmd, ["claude", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    message = str(result.exception)
    assert "'claude' not found on PATH" in message
    assert agents.get_agent("claude").install_url in message
    assert opened == []


def test_agent_with_both_over_ssh_never_opens_the_app(
    vault: Path, monkeypatch: pytest.MonkeyPatch, exec_capture: dict[str, object]
) -> None:
    opened = _claude_installed(monkeypatch, terminal=True, app=True)
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    result = CliRunner().invoke(agent_cmd, ["claude", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert exec_capture["argv"] == ["claude"]
    assert opened == []


def test_agent_app_start_failure_names_the_app_download_not_the_clis(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core import app_launch

    _claude_installed(monkeypatch, terminal=False, app=True)

    def broken(_spec: object, _at: Path) -> None:
        raise app_launch.AppNotFound("Could not open Claude Code: denied")

    monkeypatch.setattr(app_launch, "open_app", broken)
    result = CliRunner().invoke(agent_cmd, ["claude", "--library", str(vault)])
    assert result.exit_code != 0
    assert str(result.exception) == (
        "Could not open Claude Code: denied "
        "Get Claude Code: https://claude.com/download"
    )
