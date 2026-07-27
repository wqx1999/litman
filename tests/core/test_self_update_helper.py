"""Tests for the detached self-update helper (task-one-click-update).

Two layers. Script GENERATION is asserted textually for both platforms — the
scripts are straight-line whitelisted commands, so their text is the contract
(wait loop, hard timeout, failure flag, relaunch race guard, self-deletion).
Then the POSIX script is RUN for real (inject-seam discipline: one test drives
the true default end to end): a dead PID makes it upgrade + "relaunch"
immediately, a live PID with a shrunk timeout makes it give up cleanly —
asserting the log lands, the flag appears only on failure, and the script
removes itself on every exit path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from litman.core import self_update_helper as helper


def _build(
    tmp_path: Path,
    *,
    windows: bool,
    timeout: int = 60,
    port: int = 8765,
    stub_paths: list[Path] | None = None,
) -> str:
    return helper.build_helper_script(
        pid=4242,
        port=port,
        upgrade_cmd=["uv", "tool", "upgrade", "litman"],
        relaunch_cmd=["/opt/py env/bin/lit", "gui", "--window"],
        log=tmp_path / "self-update.log",
        fail_flag=tmp_path / "self-update-failed",
        wait_timeout_s=timeout,
        windows=windows,
        stub_paths=stub_paths,
    )


# ---------------------------------------------------------------- generation


def test_sh_script_contains_the_contract(tmp_path: Path) -> None:
    s = _build(tmp_path, windows=False)
    assert s.startswith("#!/bin/sh")
    assert "kill -0 4242" in s  # waits for THE server pid
    assert "-ge 60" in s  # hard timeout cap
    assert "uv tool upgrade litman" in s
    assert "timeout waiting for litman to exit" in s  # flag on timeout
    assert "upgrade command failed" in s  # flag on failed upgrade
    assert "http://127.0.0.1:8765/" in s  # relaunch race guard probes the port
    assert "'/opt/py env/bin/lit' gui --window" in s  # path with space is quoted
    # Self-deletes on every exit path: timeout bail-out + the single shared
    # tail (a failed upgrade continues into the relaunch attempt).
    assert s.count('rm -f "$0"') == 2


def test_bat_script_contains_the_contract(tmp_path: Path) -> None:
    s = _build(tmp_path, windows=True)
    assert s.startswith("@echo off")
    assert 'tasklist /FI "PID eq 4242"' in s
    assert "geq 60" in s
    assert "uv tool upgrade litman" in s
    assert "timeout waiting for litman to exit" in s
    assert "upgrade command failed" in s
    assert 'findstr /C:":8765 "' in s
    assert '"/opt/py env/bin/lit" gui --window' in s
    assert 'del "%~f0"' in s  # batch self-delete idiom, last statement
    # Sleeping MUST use the ping idiom: in the detached console-less cmd this
    # script runs in (stdin on NUL), timeout.exe exits instantly with "Input
    # redirection is not supported" and the wait loop races the server exit.
    assert "ping -n 2 127.0.0.1" in s
    assert "timeout /t" not in s


def test_bat_script_moves_stubs_aside_and_settles_them(tmp_path: Path) -> None:
    """Each stub: renamed aside pre-upgrade; post-upgrade the .old is dropped
    when the stub came back, renamed back when it did not (both outcomes)."""
    lit = r"C:\Users\W X\.local\bin\lit.exe"
    litw = r"C:\Users\W X\.local\bin\litw.exe"
    s = _build(tmp_path, windows=True, stub_paths=[Path(lit), Path(litw)])
    for stub in (lit, litw):
        assert f'if exist "{stub}" move /y "{stub}" "{stub}.old"' in s
        assert (
            f'if exist "{stub}" (del "{stub}.old" >nul 2>&1) '
            f'else (move /y "{stub}.old" "{stub}" >nul 2>&1)'
        ) in s
    # The settle block sits between the upgrade and the failure branch, so it
    # runs on success AND failure — deleting only on success would brick the
    # install on the installer's "nothing to upgrade" fast path.
    assert s.index("upgrade litman") < s.index(f'del "{lit}.old"')
    assert s.index(f'del "{lit}.old"') < s.index("if defined FAILED")


def test_bat_failure_path_still_reaches_the_relaunch(tmp_path: Path) -> None:
    """No `goto cleanup` inside the failure branch: a failed upgrade writes
    the flag and then falls through to the port guard + relaunch, so the old
    version comes back and surfaces the failure toast."""
    s = _build(tmp_path, windows=True)
    failure = s.index("upgrade command failed; see self-update.log")
    assert "goto cleanup" not in s[failure : s.index("netstat")]


def test_timeout_is_parameterised(tmp_path: Path) -> None:
    assert "-ge 7" in _build(tmp_path, windows=False, timeout=7)
    assert "geq 7" in _build(tmp_path, windows=True, timeout=7)


def test_paths_live_beside_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path))
    assert helper.log_path() == tmp_path / "self-update.log"
    assert helper.fail_flag_path() == tmp_path / "self-update-failed"


# ------------------------------------------------------------- real execution


@pytest.mark.skipif(sys.platform == "win32", reason="drives the POSIX sh script")
def test_sh_script_runs_dead_pid_upgrades_and_cleans_up(tmp_path: Path) -> None:
    """Dead PID → wait loop exits at once, 'upgrade' runs, script self-deletes.

    The upgrade command is a stand-in that records its run; the relaunch is a
    marker write. Port 1 is never listening, so the race guard lets the
    relaunch through.
    """
    log = tmp_path / "self-update.log"
    flag = tmp_path / "self-update-failed"
    upgraded = tmp_path / "upgraded"
    relaunched = tmp_path / "relaunched"
    # A PID that is certainly gone: spawn-and-reap a short-lived child.
    child = subprocess.Popen(["true"])
    child.wait()
    script = tmp_path / "helper.sh"
    script.write_text(
        helper.build_helper_script(
            pid=child.pid,
            port=1,
            upgrade_cmd=["touch", str(upgraded)],
            relaunch_cmd=["touch", str(relaunched)],
            log=log,
            fail_flag=flag,
            windows=False,
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(["sh", str(script)], capture_output=True, timeout=30)
    assert proc.returncode == 0
    assert upgraded.exists()
    _wait_for(relaunched)  # relaunch is backgrounded; give it a beat
    assert not flag.exists()
    assert "upgrade ok" in log.read_text(encoding="utf-8")
    assert not script.exists()  # self-deleted


@pytest.mark.skipif(sys.platform == "win32", reason="drives the POSIX sh script")
def test_sh_script_times_out_without_touching_anything(tmp_path: Path) -> None:
    """Live PID + tiny timeout → gives up, flags, changes nothing, self-deletes."""
    log = tmp_path / "self-update.log"
    flag = tmp_path / "self-update-failed"
    upgraded = tmp_path / "upgraded"
    script = tmp_path / "helper.sh"
    script.write_text(
        helper.build_helper_script(
            pid=os.getpid(),  # this test process: alive for the whole run
            port=1,
            upgrade_cmd=["touch", str(upgraded)],
            relaunch_cmd=["true"],
            log=log,
            fail_flag=flag,
            wait_timeout_s=2,
            windows=False,
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(["sh", str(script)], capture_output=True, timeout=30)
    assert proc.returncode == 1
    assert not upgraded.exists()  # gave up WITHOUT upgrading
    assert flag.read_text(encoding="utf-8").startswith("timeout")
    assert "gave up" in log.read_text(encoding="utf-8")
    assert not script.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="drives the POSIX sh script")
def test_sh_script_flags_a_failed_upgrade_and_still_relaunches(tmp_path: Path) -> None:
    """Failed upgrade → flag written AND the old version is relaunched, so the
    restarted GUI surfaces the failure immediately instead of just vanishing."""
    log = tmp_path / "self-update.log"
    flag = tmp_path / "self-update-failed"
    relaunched = tmp_path / "relaunched"
    child = subprocess.Popen(["true"])
    child.wait()
    script = tmp_path / "helper.sh"
    script.write_text(
        helper.build_helper_script(
            pid=child.pid,
            port=1,
            upgrade_cmd=["false"],
            relaunch_cmd=["touch", str(relaunched)],
            log=log,
            fail_flag=flag,
            windows=False,
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(["sh", str(script)], capture_output=True, timeout=30)
    assert proc.returncode == 1
    assert flag.read_text(encoding="utf-8").startswith("upgrade command failed")
    _wait_for(relaunched)  # relaunch still happens (backgrounded)
    assert not script.exists()


def test_write_and_spawn_uses_temp_and_detaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawn seam: script written under temp, child fully detached."""
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path))
    calls: list[dict[str, object]] = []

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"argv": argv, **kwargs})

        class _P:
            pid = 999

        return _P()

    monkeypatch.setattr(helper.subprocess, "Popen", fake_popen)
    path = helper.write_and_spawn_helper(
        pid=123,
        port=8765,
        upgrade_cmd=["uv", "tool", "upgrade", "litman"],
        relaunch_cmd=["lit", "gui", "--window"],
    )
    try:
        assert path.exists()
        assert path.name.startswith("litman-self-update-")
        [call] = calls
        if sys.platform == "win32":
            assert call["argv"][0] == "cmd"
            assert call["creationflags"]  # detached, console-less
        else:
            assert call["argv"][0] == "sh"
            assert call["start_new_session"] is True
        assert call["stdin"] is subprocess.DEVNULL
    finally:
        path.unlink(missing_ok=True)


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)
