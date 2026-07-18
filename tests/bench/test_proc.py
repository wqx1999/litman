"""Deterministic tests for :func:`harness.proc.run_bounded` (the bounded-spawn
primitive that reaps the WHOLE process tree on timeout).

POSIX-only: the fix hinges on ``start_new_session`` + ``os.killpg``, and the bench
runs only on Linux/SLURM. Non-POSIX just falls back to ``proc.kill()`` (the lone
child), which these grandchild-reaping assertions can't exercise, so the module is
skipped there rather than asserting a guarantee the platform can't make.

No randomness / time-of-day: the only clocks are fixed sleeps and a generous
wall-clock ceiling that separates "killed the group" (<10 s) from "waited for the
orphan to exit on its own" (~30 s).
"""

from __future__ import annotations

import os
import time

import pytest

from harness.proc import BoundedResult, run_bounded

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX-only (killpg/start_new_session)")


def test_happy_path_text() -> None:
    r = run_bounded(["bash", "-c", "printf hello; exit 3"], timeout=5, text=True)
    assert isinstance(r, BoundedResult)
    assert r.stdout == "hello"
    assert isinstance(r.stdout, str)
    assert r.exit_code == 3
    assert r.timed_out is False


def test_happy_path_bytes() -> None:
    r = run_bounded(["bash", "-c", "printf hello; exit 3"], timeout=5, text=False)
    assert r.stdout == b"hello"
    assert isinstance(r.stdout, bytes)
    assert r.exit_code == 3
    assert r.timed_out is False


def test_timeout_reaps_lone_child() -> None:
    start = time.monotonic()
    r = run_bounded(["sleep", "30"], timeout=1)
    elapsed = time.monotonic() - start
    assert elapsed < 10, f"lone-child timeout took {elapsed:.1f}s (should return promptly)"
    assert r.timed_out is True
    assert r.exit_code == -1


def test_timeout_kills_grandchildren() -> None:
    """THE regression test. bash backgrounds a ``sleep 30`` grandchild that
    inherits the capture pipe, prints its PID, then foreground-sleeps. A group
    kill reaps the grandchild and the drain returns at once; a lone-child kill
    (the old bug) leaves the orphan holding the pipe and blocks ~30 s."""
    argv = ["bash", "-c", "sleep 30 & echo $!; sleep 30"]
    start = time.monotonic()
    r = run_bounded(argv, timeout=1, text=True)
    elapsed = time.monotonic() - start

    # Primary proof: a broken impl blocks ~30 s waiting on the orphaned sleep.
    assert elapsed < 10, f"timeout drain took {elapsed:.1f}s — grandchild kept the pipe open"
    assert r.timed_out is True
    assert r.exit_code == -1

    # Secondary: the backgrounded grandchild was actually reaped by the group kill.
    pid = int(r.stdout.splitlines()[0])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"grandchild pid {pid} still alive 5 s after the group kill")


def test_decode_replaces_bad_bytes() -> None:
    r = run_bounded(["bash", "-c", r"printf '\xff'"], timeout=5, text=True)
    assert isinstance(r.stdout, str)
    assert "�" in r.stdout
    assert r.timed_out is False
