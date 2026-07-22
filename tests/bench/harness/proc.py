"""Process-group-safe bounded spawn for the live-agent sites.

The bench spawns headless agents (``claude -p``, ``cursor``, ``agy``,
``opencode``) that themselves fork tool children — a ``bash`` tool child, a
model-provider connection, an ``opencode export`` subprocess. Those grandchildren
inherit the write-end of the parent's captured stdout/stderr pipes. Plain
``subprocess.run(timeout=)`` on timeout SIGKILLs ONLY the direct child, so an
orphaned grandchild keeps a pipe write-end open and the post-timeout drain blocks
in ``read()`` forever, waiting on an EOF that never comes (observed live: an
opencode job hung ~1h18m at 0% CPU until ``scancel``'s cgroup-wide signal freed
it).

:func:`run_bounded` closes that hole: the child leads its own session/process
group (``start_new_session=True``), and a timeout SIGKILLs the WHOLE group
(``os.killpg``), so every pipe-holder dies at once and the drain returns
immediately. This is the only place the bench should spawn a bounded, capturing
agent process.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass


@dataclass
class BoundedResult:
    stdout: bytes | str  # str iff text=True, else bytes
    stderr: bytes | str  # same mode; captured so a full stderr pipe can't deadlock the child
    exit_code: int  # child returncode on completion; -1 when timed_out
    timed_out: bool


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (POSIX); fall back to killing the
    lone child on non-POSIX or if the group is already gone."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


def run_bounded(
    argv,
    *,
    env=None,
    cwd=None,
    timeout,  # seconds; REQUIRED (keyword). float|int.
    text: bool = False,
    stdin=subprocess.DEVNULL,
) -> BoundedResult:
    """Run argv with a hard timeout that reaps the ENTIRE process tree.

    Unlike subprocess.run(timeout=), a timeout here kills the child's whole
    process group, so grandchildren that inherited the capture pipes cannot keep
    the drain blocked. Captures stdout AND stderr (communicate drains both
    concurrently — a full stderr pipe never deadlocks the child).

    On timeout: the group is SIGKILLed, whatever was buffered before the kill is
    returned as partial stdout/stderr, exit_code = -1, timed_out = True.
    text=True decodes both streams as utf-8 errors="replace" (never raises on a
    truncated multibyte tail); text=False returns raw bytes.

    Limitation (honest): a grandchild that calls setsid() itself escapes the
    group. Not observed for the bench agents; documented so it isn't a surprise.
    """
    popen_kwargs = {
        "env": env,
        "cwd": cwd,
        "stdin": stdin,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **popen_kwargs)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        out, err = proc.communicate()  # group dead → pipe write-ends closed, returns at once
    exit_code = -1 if timed_out else proc.returncode
    if text:
        out = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out
        err = err.decode("utf-8", errors="replace") if isinstance(err, bytes) else err
    return BoundedResult(stdout=out, stderr=err, exit_code=exit_code, timed_out=timed_out)
