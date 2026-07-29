"""Detached helper that upgrades litman after litman exits.

litman cannot upgrade itself in place: the running process holds the tool venv
open (Windows locks the loaded binaries outright — including its own launcher
stub, which cannot even be renamed out of the way — and lazy imports elsewhere
would mix old and new code in one process). So the caller writes a tiny
platform shell script, spawns it fully detached, and exits; the helper waits
for the PID to vanish, runs the installer's upgrade command, and optionally
relaunches. The industry-standard updater shape (a separate process swaps the
app while it is down), scaled to a shell script.

Two callers, one script. The webUI's one-click update passes a
``relaunch_cmd`` so the window comes back by itself; ``lit self-update`` on
Windows passes none — a terminal command that respawns a terminal would be a
surprise, and the shell prompt is already back. With no relaunch command the
port guard and the relaunch line are omitted entirely rather than rendered
empty (``start ""`` alone opens a stray console window).

Guard rails (task-one-click-update, all four):

* **Hard timeout** — the helper waits for litman's PID for at most
  ``wait_timeout_s``; on timeout it gives up, touches the failure flag, and
  removes itself without changing anything.
* **Failure visibility** — everything appends to ``self-update.log`` beside
  the vault registry; any failure also writes ``self-update-failed``, which
  the next server start surfaces once through ``GET /api/version``.
* **Relaunch race guard** — before relaunching, the helper probes the old
  port; if something already listens there (the user beat it to a restart),
  it skips the relaunch instead of stacking a second instance. Moot for the
  terminal caller, which asks for no relaunch.
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
    port: int = 0,
    upgrade_cmd: list[str],
    relaunch_cmd: list[str] | None = None,
    log: Path,
    fail_flag: Path,
    wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
    windows: bool | None = None,
    stub_paths: list[Path] | None = None,
) -> str:
    """Render the platform helper script as text.

    ``windows`` defaults to the running platform; tests pass it explicitly to
    render both variants anywhere. ``stub_paths`` are the launcher stubs the
    Windows script moves aside before upgrading (see
    :mod:`litman.core.launcher_stubs`); POSIX callers pass none — overwriting
    a running file is fine there. ``relaunch_cmd`` empty (the terminal caller)
    drops the relaunch section, and with it ``port``, which only ever fed the
    relaunch race guard.
    """
    relaunch_cmd = list(relaunch_cmd or [])
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
            stubs=[str(p) for p in (stub_paths or [])],
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
    # cmd.exe quoting: every token double-quoted, unconditionally. A profile
    # path can carry cmd metacharacters without a space (`C:\Users\A&B`), and
    # an unquoted `&` splits the line in two; the child's own argv parsing
    # strips the quotes again. A literal `%` still expands inside a batch
    # script, quoted or not — known corner, left undefended.
    return " ".join(f'"{a}"' for a in argv)


def _build_sh(
    *, pid: int, port: int, upgrade: str, relaunch: str, log: str, fail_flag: str, timeout: int
) -> str:
    # Relaunch even after a failed upgrade: the previous version still runs, and
    # the restarted GUI surfaces the failure flag immediately — a window that
    # never comes back tells the user nothing. Omitted wholesale when the caller
    # asked for no relaunch.
    relaunch_block = (
        f"""if command -v curl >/dev/null 2>&1 && curl -s -o /dev/null --max-time 2 "http://127.0.0.1:{port}/"; then
  echo "[helper] litman already running again, not relaunching" >> "$LOG"
else
  echo "[helper] relaunching" >> "$LOG"
  {relaunch} >> "$LOG" 2>&1 &
fi
"""
        if relaunch
        else ""
    )
    return f"""#!/bin/sh
# litman self-update helper (auto-generated; deletes itself on exit).
LOG="{log}"
echo "[helper] started, waiting for litman pid {pid}" >> "$LOG"
i=0
while kill -0 {pid} 2>/dev/null; do
  i=$((i+1))
  if [ "$i" -ge {timeout} ]; then
    echo "[helper] gave up: litman still running after {timeout}s" >> "$LOG"
    echo "timeout waiting for litman to exit" > "{fail_flag}"
    rm -f "$0"
    exit 1
  fi
  sleep 1
done
echo "[helper] litman gone, upgrading" >> "$LOG"
FAILED=0
if {upgrade} >> "$LOG" 2>&1; then
  echo "[helper] upgrade ok" >> "$LOG"
else
  echo "[helper] upgrade command failed" >> "$LOG"
  echo "upgrade command failed; see self-update.log" > "{fail_flag}"
  FAILED=1
fi
{relaunch_block}rm -f "$0"
exit $FAILED
"""


def _build_bat(
    *,
    pid: int,
    port: int,
    upgrade: str,
    relaunch: str,
    log: str,
    fail_flag: str,
    timeout: int,
    stubs: list[str],
) -> str:
    # NB: batch reads itself line by line, so self-deletion must be the very
    # last statement on every exit path (the `del ... & exit` idiom).
    #
    # Sleeping uses the `ping -n 2` idiom, NOT `timeout /t`: this script runs
    # in a detached console-less cmd with stdin on NUL, where timeout.exe dies
    # instantly with "Input redirection is not supported" — the wait loop
    # would spin through its iterations in seconds and race the server's exit.
    #
    # The stubs are moved aside before the upgrade — not to defeat the lock on
    # a running launcher (nothing can: not even a rename succeeds while it
    # executes, which is why this script exists at all), but because a SECOND
    # litman instance may be up, and because the installer copies onto a path
    # that is cleaner empty. They are settled afterwards on BOTH outcomes:
    # re-created by the upgrade → drop the .old; not re-created (failure, or
    # the installer's "nothing to upgrade" fast path that skips entrypoints)
    # → rename it back, so a launcher never disappears.
    #
    # The upgrade's failure is captured on its own line via `||`, never with a
    # later `if errorlevel 1`: nothing may sit between an external command and
    # an ERRORLEVEL read, and what plain `set` does to ERRORLEVEL when it
    # clears an undefined variable is not reliably documented.
    #
    # The relaunch guard filters netstat down to LISTENING lines. Matching the
    # port alone also hits the TIME_WAIT remnants of the connections the window
    # we just closed had open (Windows holds those ~120s, far longer than the
    # upgrade takes), so the guard would read "already running" every single
    # time and never relaunch. A locale that translated the state word would
    # only cost us the guard, which fails toward relaunching — the direction
    # that leaves the user with a window rather than without one.
    aside = "".join(
        f'if exist "{s}" move /y "{s}" "{s}.old" >nul 2>&1\n' for s in stubs
    )
    relaunch_block = (
        f"""rem Relaunch even after a failed upgrade: the previous version still runs,
rem and the restarted GUI surfaces the failure flag immediately.
netstat -ano | findstr /C:":{port} " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [helper] litman already running again, not relaunching >> "%LOG%"
  goto cleanup
)
echo [helper] relaunching >> "%LOG%"
start "" {relaunch}
"""
        if relaunch
        else ""
    )
    settle = "".join(
        f'if exist "{s}.old" (\n'
        f'  if exist "{s}" (del "{s}.old" >nul 2>&1) '
        f'else (move /y "{s}.old" "{s}" >nul 2>&1)\n'
        f")\n"
        for s in stubs
    )
    return f"""@echo off
rem litman self-update helper (auto-generated; deletes itself on exit).
set "LOG={log}"
echo [helper] started, waiting for litman pid {pid} >> "%LOG%"
set /a i=0
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if errorlevel 1 goto gone
set /a i+=1
if %i% geq {timeout} (
  echo [helper] gave up: litman still running after {timeout}s >> "%LOG%"
  echo timeout waiting for litman to exit > "{fail_flag}"
  goto cleanup
)
ping -n 2 127.0.0.1 >nul
goto wait
:gone
rem The pid waited on is litman's python. Its launcher stub is a SEPARATE
rem parent process (uv's trampoline) that exits a moment later while still
rem holding the very file the move below renames — one extra beat before
rem touching anything.
ping -n 3 127.0.0.1 >nul
echo [helper] litman gone, upgrading >> "%LOG%"
set "FAILED="
{aside}{upgrade} >> "%LOG%" 2>&1 || set "FAILED=1"
{settle}if defined FAILED (
  echo [helper] upgrade command failed >> "%LOG%"
  echo upgrade command failed; see self-update.log > "{fail_flag}"
) else (
  echo [helper] upgrade ok >> "%LOG%"
)
{relaunch_block}:cleanup
del "%~f0" & exit /b 0
"""


def write_and_spawn_helper(
    *,
    pid: int,
    port: int = 0,
    upgrade_cmd: list[str],
    relaunch_cmd: list[str] | None = None,
    wait_timeout_s: int = DEFAULT_WAIT_TIMEOUT_S,
    stub_paths: list[Path] | None = None,
) -> Path:
    """Write the helper script to the system temp dir and spawn it detached.

    Returns the script path. The spawned process must survive litman's own
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
        stub_paths=stub_paths,
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
