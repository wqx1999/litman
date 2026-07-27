"""Detached helper that upgrades litman after the server exits.

The GUI's one-click update cannot upgrade in place: the running server holds
the tool venv open (Windows locks the loaded binaries outright, and lazy
imports elsewhere would mix old and new code in one process). So the endpoint
writes a tiny platform shell script, spawns it fully detached, and lets the
server exit; the helper waits for the PID to vanish, runs the installer's
upgrade command, and relaunches the GUI. The industry-standard updater shape
(a separate process swaps the app while it is down), scaled to a shell script.

Guard rails (task-one-click-update, all four):

* **Hard timeout** — the helper waits for the server PID for at most
  ``wait_timeout_s``; on timeout it gives up, touches the failure flag, and
  removes itself without changing anything.
* **Failure visibility** — everything appends to ``self-update.log`` beside
  the vault registry; any failure also writes ``self-update-failed``, which
  the next server start surfaces once through ``GET /api/version``.
* **Relaunch race guard** — before relaunching, the helper probes the old
  port; if something already listens there (the user beat it to a restart),
  it skips the relaunch instead of stacking a second instance.
* **Script hygiene** — the script is written to the system temp dir, deletes
  itself on every exit path, interpolates no user input, and quotes every
  path it embeds.

The script text is deliberately dumb: straight-line whitelisted commands with
values substituted by :func:`build_helper_script`. Tests assert on the text.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from litman.core.vault_registry import registry_path

LOG_FILENAME = "self-update.log"
FAIL_FLAG_FILENAME = "self-update-failed"

#: How long the helper waits for the server process to disappear. Generous on
#: purpose: antivirus scanners can keep file handles alive well past process
#: exit, and a helper that gives up cleanly beats one that upgrades early.
DEFAULT_WAIT_TIMEOUT_S = 60


def log_path() -> Path:
    """``self-update.log`` beside the vault registry (honours the env override)."""
    return registry_path().parent / LOG_FILENAME


def fail_flag_path() -> Path:
    """``self-update-failed`` beside the vault registry."""
    return registry_path().parent / FAIL_FLAG_FILENAME


def build_helper_script(
    *,
    pid: int,
    port: int,
    upgrade_cmd: list[str],
    relaunch_cmd: list[str],
    log: Path,
    fail_flag: Path,
    wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
    windows: bool | None = None,
) -> str:
    """Render the platform helper script as text.

    ``windows`` defaults to the running platform; tests pass it explicitly to
    render both variants anywhere.
    """
    if windows is None:
        windows = sys.platform == "win32"
    if windows:
        return _build_bat(
            pid=pid,
            port=port,
            upgrade=_quote_win(upgrade_cmd),
            relaunch=_quote_win(relaunch_cmd),
            log=str(log),
            fail_flag=str(fail_flag),
            timeout=wait_timeout_s,
        )
    return _build_sh(
        pid=pid,
        port=port,
        upgrade=_quote_sh(upgrade_cmd),
        relaunch=_quote_sh(relaunch_cmd),
        log=str(log),
        fail_flag=str(fail_flag),
        timeout=wait_timeout_s,
    )


def _quote_sh(argv: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(a) for a in argv)


def _quote_win(argv: list[str]) -> str:
    # cmd.exe quoting: wrap anything with a space in double quotes. The argv
    # only ever carries installer names and our own executable paths — no user
    # input — so this stays simple on purpose.
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


def _build_sh(
    *, pid: int, port: int, upgrade: str, relaunch: str, log: str, fail_flag: str, timeout: int
) -> str:
    return f"""#!/bin/sh
# litman self-update helper (auto-generated; deletes itself on exit).
LOG="{log}"
echo "[helper] started, waiting for server pid {pid}" >> "$LOG"
i=0
while kill -0 {pid} 2>/dev/null; do
  i=$((i+1))
  if [ "$i" -ge {timeout} ]; then
    echo "[helper] gave up: server still running after {timeout}s" >> "$LOG"
    echo "timeout waiting for litman to exit" > "{fail_flag}"
    rm -f "$0"
    exit 1
  fi
  sleep 1
done
echo "[helper] server gone, upgrading" >> "$LOG"
if {upgrade} >> "$LOG" 2>&1; then
  echo "[helper] upgrade ok" >> "$LOG"
else
  echo "[helper] upgrade command failed" >> "$LOG"
  echo "upgrade command failed; see self-update.log" > "{fail_flag}"
  rm -f "$0"
  exit 1
fi
if command -v curl >/dev/null 2>&1 && curl -s -o /dev/null --max-time 2 "http://127.0.0.1:{port}/"; then
  echo "[helper] litman already running again, not relaunching" >> "$LOG"
else
  echo "[helper] relaunching" >> "$LOG"
  {relaunch} >> "$LOG" 2>&1 &
fi
rm -f "$0"
exit 0
"""


def _build_bat(
    *, pid: int, port: int, upgrade: str, relaunch: str, log: str, fail_flag: str, timeout: int
) -> str:
    # NB: batch reads itself line by line, so self-deletion must be the very
    # last statement on every exit path (the `del ... & exit` idiom).
    return f"""@echo off
rem litman self-update helper (auto-generated; deletes itself on exit).
set "LOG={log}"
echo [helper] started, waiting for server pid {pid} >> "%LOG%"
set /a i=0
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if errorlevel 1 goto gone
set /a i+=1
if %i% geq {timeout} (
  echo [helper] gave up: server still running after {timeout}s >> "%LOG%"
  echo timeout waiting for litman to exit > "{fail_flag}"
  goto cleanup
)
timeout /t 1 /nobreak >nul
goto wait
:gone
echo [helper] server gone, upgrading >> "%LOG%"
{upgrade} >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [helper] upgrade command failed >> "%LOG%"
  echo upgrade command failed; see self-update.log > "{fail_flag}"
  goto cleanup
)
echo [helper] upgrade ok >> "%LOG%"
netstat -ano | findstr /C:":{port} " >nul 2>&1
if not errorlevel 1 (
  echo [helper] litman already running again, not relaunching >> "%LOG%"
  goto cleanup
)
echo [helper] relaunching >> "%LOG%"
start "" {relaunch}
:cleanup
del "%~f0" & exit /b 0
"""


def write_and_spawn_helper(
    *,
    pid: int,
    port: int,
    upgrade_cmd: list[str],
    relaunch_cmd: list[str],
    wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
) -> Path:
    """Write the helper script to the system temp dir and spawn it detached.

    Returns the script path. The spawned process must survive this server's
    exit: ``start_new_session`` on POSIX, a detached console-less ``cmd`` on
    Windows. Never blocks on the child.
    """
    windows = sys.platform == "win32"
    suffix = ".bat" if windows else ".sh"
    script = build_helper_script(
        pid=pid,
        port=port,
        upgrade_cmd=upgrade_cmd,
        relaunch_cmd=relaunch_cmd,
        log=log_path(),
        fail_flag=fail_flag_path(),
        wait_timeout_s=wait_timeout_s,
        windows=windows,
    )
    fd, name = tempfile.mkstemp(prefix="litman-self-update-", suffix=suffix)
    path = Path(name)
    with open(fd, "w", encoding="utf-8", newline="\r\n" if windows else "\n") as fh:
        fh.write(script)

    if windows:
        creationflags = (
            subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )
        subprocess.Popen(
            ["cmd", "/c", str(path)],
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            ["sh", str(path)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    return path
