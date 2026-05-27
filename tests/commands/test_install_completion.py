"""Tests for ``lit install-completion <shell>`` (M11)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands.install_completion import (
    SENTINEL,
    completion_installed,
    detect_shell,
)


def _run(runner: CliRunner, *args: str) -> "object":
    return runner.invoke(cli, ["install-completion", *args])


# ---------------------------------------------------------------------------
# bash / zsh: append to ~/.bashrc / ~/.zshrc
# ---------------------------------------------------------------------------


def test_install_zsh_writes_eval_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = _run(runner, "zsh")
    assert result.exit_code == 0, result.output

    zshrc = tmp_path / ".zshrc"
    assert zshrc.is_file()
    body = zshrc.read_text(encoding="utf-8")
    assert SENTINEL in body
    assert '_LIT_COMPLETE=zsh_source lit' in body
    assert 'eval "$' in body


def test_install_zsh_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    r1 = _run(runner, "zsh")
    assert r1.exit_code == 0, r1.output
    body1 = (tmp_path / ".zshrc").read_text(encoding="utf-8")

    r2 = _run(runner, "zsh")
    assert r2.exit_code == 0, r2.output
    body2 = (tmp_path / ".zshrc").read_text(encoding="utf-8")

    assert body1 == body2
    assert body2.count(SENTINEL) == 1
    assert body2.count("_LIT_COMPLETE=zsh_source lit") == 1
    assert "already installed" in r2.output


def test_install_zsh_preserves_existing_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    zshrc = tmp_path / ".zshrc"
    pre = "# user's existing config\nexport FOO=bar\n"
    zshrc.write_text(pre, encoding="utf-8")

    runner = CliRunner()
    result = _run(runner, "zsh")
    assert result.exit_code == 0, result.output
    body = zshrc.read_text(encoding="utf-8")
    assert body.startswith(pre)
    assert SENTINEL in body


def test_install_bash_writes_eval_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = _run(runner, "bash")
    assert result.exit_code == 0, result.output

    bashrc = tmp_path / ".bashrc"
    assert bashrc.is_file()
    body = bashrc.read_text(encoding="utf-8")
    assert SENTINEL in body
    assert "_LIT_COMPLETE=bash_source lit" in body


def test_install_bash_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    _run(runner, "bash")
    _run(runner, "bash")
    body = (tmp_path / ".bashrc").read_text(encoding="utf-8")
    assert body.count("_LIT_COMPLETE=bash_source lit") == 1


# ---------------------------------------------------------------------------
# fish: write to ~/.config/fish/completions/lit.fish
# ---------------------------------------------------------------------------


def test_install_fish_writes_to_fish_completion_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = _run(runner, "fish")
    assert result.exit_code == 0, result.output

    fish_file = tmp_path / ".config" / "fish" / "completions" / "lit.fish"
    assert fish_file.is_file()
    body = fish_file.read_text(encoding="utf-8")
    assert SENTINEL in body
    assert "_LIT_COMPLETE=fish_source lit | source" in body


def test_install_fish_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    _run(runner, "fish")
    _run(runner, "fish")
    body = (
        tmp_path / ".config" / "fish" / "completions" / "lit.fish"
    ).read_text(encoding="utf-8")
    assert body.count("_LIT_COMPLETE=fish_source lit") == 1


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_install_unsupported_shell_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = _run(runner, "csh")
    assert result.exit_code != 0
    out = (result.output or "") + (
        str(result.exception) if result.exception else ""
    )
    assert "csh" in out.lower() or "invalid" in out.lower() or "choose" in out.lower()


def test_install_completion_help_lists_shells() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["install-completion", "--help"])
    assert result.exit_code == 0
    for shell in ("bash", "zsh", "fish"):
        assert shell in result.output


# ---------------------------------------------------------------------------
# M27 — shell detection + no-arg invocation + completion_installed helper
# ---------------------------------------------------------------------------


def test_detect_shell_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    assert detect_shell() == "zsh"
    monkeypatch.setenv("SHELL", "/bin/tcsh")
    assert detect_shell() is None
    monkeypatch.delenv("SHELL", raising=False)
    assert detect_shell() is None


def test_install_completion_no_arg_uses_detected_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    runner = CliRunner()
    result = runner.invoke(cli, ["install-completion"])
    assert result.exit_code == 0, result.output

    bashrc = tmp_path / ".bashrc"
    assert bashrc.is_file()
    assert SENTINEL in bashrc.read_text(encoding="utf-8")


def test_install_completion_no_arg_undetectable_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SHELL", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["install-completion"])
    assert result.exit_code != 0
    out = (result.output or "") + (
        str(result.exception) if result.exception else ""
    )
    assert "pass it explicitly" in out.lower()


def test_completion_installed_true_after_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert completion_installed("bash", home=tmp_path) is False
    runner = CliRunner()
    result = _run(runner, "bash")
    assert result.exit_code == 0, result.output
    assert completion_installed("bash", home=tmp_path) is True
    # A shell that was never installed is still reported as not installed.
    assert completion_installed("zsh", home=tmp_path) is False
