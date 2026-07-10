"""Tests for ``litman.cli._ensure_std_streams``.

The desktop shortcut runs ``litw``, a windows-subsystem launcher: Windows
attaches no console, so CPython hands the process ``sys.stdout is None``. Rich
and uvicorn's logging both write to those streams. These tests simulate that
process — the one place the real thing cannot be exercised from Linux — and
pin the two properties that keep it alive: the streams get replaced, and a log
file exists to read when the invisible process fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.console import Console

from litman.cli import _ensure_std_streams


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "logs"
    monkeypatch.setattr("platformdirs.user_log_dir", lambda *a, **k: str(target))
    return target


def test_noop_when_streams_are_real() -> None:
    # The POSIX and console-Windows path: both streams exist, touch nothing.
    before_out, before_err = sys.stdout, sys.stderr
    _ensure_std_streams()
    assert sys.stdout is before_out and sys.stderr is before_err


def test_replaces_none_streams_with_a_log_file(
    monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    _ensure_std_streams()

    assert sys.stdout is not None and sys.stderr is not None
    log = log_dir / "litw.log"
    assert log.is_file()

    # The whole point: a Rich print inside a console-less process must not
    # raise, and must leave something a user can read afterwards.
    Console(file=sys.stdout).print("boom: no vault found")
    sys.stdout.flush()
    assert "boom: no vault found" in log.read_text(encoding="utf-8")


def test_truncates_the_log_per_launch(
    monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    # uvicorn logs a line per HTTP request; an append-mode log would grow
    # without bound across launches. This is a crash file, not an audit trail.
    log_dir.mkdir(parents=True)
    (log_dir / "litw.log").write_text("stale session\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    _ensure_std_streams()

    assert "stale session" not in (log_dir / "litw.log").read_text(encoding="utf-8")


def test_falls_back_to_devnull_when_the_log_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No console must never mean no start: an unwritable log dir downgrades to
    # os.devnull rather than killing the launch.
    def _explode(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("platformdirs.user_log_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(Path, "mkdir", _explode)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    _ensure_std_streams()

    assert sys.stdout is not None and sys.stderr is not None
    Console(file=sys.stdout).print("swallowed, but not fatal")


def test_only_the_missing_stream_is_replaced(
    monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    real_err = sys.stderr
    monkeypatch.setattr(sys, "stdout", None)

    _ensure_std_streams()

    assert sys.stdout is not None
    assert sys.stderr is real_err
