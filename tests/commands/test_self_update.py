"""Tests for ``lit self-update`` (task-self-update D3, AC4).

The three dispatch branches are covered:

* **uv** — ``_is_editable_install`` mocked False, ``uv tool list`` mocked to
  mention litman → ``uv tool upgrade litman`` is the subprocess run.
* **pipx** — same, but only ``pipx list`` mentions litman.
* **reject** — the editable/dev branch (the local conda env is a live editable
  install, so ``_is_editable_install`` is exercised for real elsewhere; here it
  is asserted via a mock for determinism) and the no-tool error branch.

Every subprocess (probe + upgrade) is mocked — no real uv/pipx/pip is invoked
and nothing is ever upgraded.
"""

from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands import self_update as su
from litman.exceptions import SelfUpdateError


def _no_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(su, "_is_editable_install", lambda: False)


def _fake_which(present: set[str]):
    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return _which


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# reject branch (AC4)
# ---------------------------------------------------------------------------


def test_editable_install_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Editable/dev install → manual hint, NO upgrade attempted."""
    monkeypatch.setattr(su, "_is_editable_install", lambda: True)

    ran: list[list[str]] = []
    monkeypatch.setattr(
        su.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _completed()
    )

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert "editable" in result.output.lower()
    assert ran == []  # never shelled out to any upgrade


def test_no_tool_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not editable, neither uv nor pipx on PATH → error exit with manual cmd."""
    _no_editable(monkeypatch)
    monkeypatch.setattr(su.shutil, "which", _fake_which(set()))

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SelfUpdateError)
    assert "Neither uv nor pipx" in str(result.exception)


def test_tool_present_but_not_managing_litman(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv/pipx exist but neither lists litman → manual hint, no upgrade."""
    _no_editable(monkeypatch)
    monkeypatch.setattr(su.shutil, "which", _fake_which({"uv", "pipx"}))
    monkeypatch.setattr(
        su, "_run_capture", lambda cmd, **kw: _completed(stdout="something-else 1.0\n")
    )

    ran: list[list[str]] = []
    monkeypatch.setattr(
        su.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _completed()
    )

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert "not installed via uv or pipx" in result.output
    assert ran == []


# ---------------------------------------------------------------------------
# uv / pipx dispatch
# ---------------------------------------------------------------------------


def _wire_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: str,
    upgrade_rc: int = 0,
) -> list[list[str]]:
    """Wire probes so ``manager`` (uv|pipx) manages litman; capture upgrade cmds.

    Returns the list the mocked ``subprocess.run`` appends each invoked command
    to (the upgrade + the post-verify ``lit --version``).
    """
    _no_editable(monkeypatch)
    monkeypatch.setattr(su.shutil, "which", _fake_which({"uv", "pipx"}))

    def _run_capture(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["uv", "tool"] and cmd[2] == "list":
            return _completed(stdout="litman v1.1.0\n" if manager == "uv" else "other\n")
        if cmd[:2] == ["pipx", "list"]:
            return _completed(stdout="package litman 1.1.0\n" if manager == "pipx" else "other\n")
        if cmd[:2] == ["lit", "--version"]:
            return _completed(stdout="lit, version 9.9.9\n")
        return _completed(stdout="")

    monkeypatch.setattr(su, "_run_capture", _run_capture)
    monkeypatch.setattr(su.update_check, "_fetch_latest_version", lambda **kw: "9.9.9")

    ran: list[list[str]] = []

    def _run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        return _completed(returncode=upgrade_rc)

    monkeypatch.setattr(su.subprocess, "run", _run)
    return ran


def test_uv_branch_runs_uv_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _wire_dispatch(monkeypatch, manager="uv")
    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert ["uv", "tool", "upgrade", "litman"] in ran
    assert "9.9.9" in result.output


def test_pipx_branch_runs_pipx_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _wire_dispatch(monkeypatch, manager="pipx")
    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert ["pipx", "upgrade", "litman"] in ran


def test_uv_preferred_over_pipx(monkeypatch: pytest.MonkeyPatch) -> None:
    """When BOTH list litman, uv is chosen (probe order)."""
    _no_editable(monkeypatch)
    monkeypatch.setattr(su.shutil, "which", _fake_which({"uv", "pipx"}))
    monkeypatch.setattr(
        su, "_run_capture", lambda cmd, **kw: _completed(stdout="litman 1.1.0\n")
    )
    monkeypatch.setattr(su.update_check, "_fetch_latest_version", lambda **kw: "9.9.9")

    ran: list[list[str]] = []
    monkeypatch.setattr(
        su.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _completed()
    )

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert ["uv", "tool", "upgrade", "litman"] in ran
    assert ["pipx", "upgrade", "litman"] not in ran


def test_upgrade_nonzero_exit_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_dispatch(monkeypatch, manager="uv", upgrade_rc=3)
    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SelfUpdateError)
    assert "exited with code 3" in str(result.exception)


def test_confirm_abort_skips_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without -y, a 'no' answer aborts before any upgrade subprocess."""
    ran = _wire_dispatch(monkeypatch, manager="uv")
    result = CliRunner().invoke(cli, ["self-update"], input="n\n")
    assert result.exit_code != 0  # click abort
    assert ran == []
