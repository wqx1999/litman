"""Tests for ``litman.cli``'s standard streams and the error line ``main``
prints.

The desktop shortcut runs ``litw``, a windows-subsystem launcher: Windows
attaches no console, so CPython hands the process ``sys.stdout is None``. Rich
and uvicorn's logging both write to those streams. These tests simulate that
process — the one place the real thing cannot be exercised from Linux — and
pin the two properties that keep it alive: the streams get replaced, and a log
file exists to read when the invisible process fails.

``_force_utf8_output`` and ``main``'s ``LitmanError`` handler come last: what
they print must survive a character the stream cannot encode, and a pipe's 80
columns. tests/commands/test_si_cli.py drives both through a real ``lit``
process.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

from litman import cli as cli_mod
from litman.cli import _ensure_std_streams, _force_utf8_output
from litman.exceptions import LitmanError


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


def test_force_utf8_output_prints_an_unencodable_character_as_an_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A POSIX file name's undecodable byte reaches Python as a lone surrogate,
    # which no UTF-8 stream can encode. Changing only the encoding left both
    # streams strict, and printing one killed the command with a traceback.
    buffers = {"stdout": io.BytesIO(), "stderr": io.BytesIO()}
    for name, buffer in buffers.items():
        monkeypatch.setattr(sys, name, io.TextIOWrapper(buffer, encoding="ascii"))

    _force_utf8_output()

    for name, buffer in buffers.items():
        stream = getattr(sys, name)
        assert (stream.encoding, stream.errors) == ("utf-8", "backslashreplace")
        stream.write("\u2713 caf\udce9")
        stream.flush()
        assert buffer.getvalue() == b"\xe2\x9c\x93 caf\\udce9"


def _error_line(monkeypatch: pytest.MonkeyPatch, message: str) -> str:
    """What ``main()`` prints when the command raises ``LitmanError(message)``."""

    def fail() -> None:
        raise LitmanError(message)

    printed = io.StringIO()
    monkeypatch.setattr(cli_mod, "cli", fail)
    # In for the module's console: a pipe's 80 columns, and no colour whatever
    # FORCE_COLOR says (the real one's width depends on how pytest was run).
    monkeypatch.setattr(
        cli_mod, "console", Console(file=printed, width=80, color_system=None)
    )
    # main() reconfigures sys.stdout / sys.stderr: hand it throwaway streams
    # rather than pytest's capture, and put back the launch shape it records.
    for name in ("stdout", "stderr"):
        monkeypatch.setattr(sys, name, io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    monkeypatch.setattr(cli_mod, "_LAUNCHED_HEADLESS", cli_mod._LAUNCHED_HEADLESS)

    with pytest.raises(SystemExit) as exit_info:
        cli_mod.main()

    assert exit_info.value.code == 1
    return printed.getvalue()


@pytest.mark.parametrize(
    "message",
    [
        "No such file: /home/someone/Downloads/supporting-information/2019_Yang/"
        "ci9b00237_si_001[final].pdf",
        "Cannot read /home/someone/literature_vault/papers/2019_Yang_Concepts/"
        "metadata.yaml [line 3]\nRun `lit health-check` to see why.",
    ],
    ids=["one-line", "two-lines"],
)
def test_main_prints_a_litman_error_line_for_line(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    # Piped, an error folded at column 80 reads as two, and a path cut in half
    # no longer names its file; a lowercase [tag] in it is text, not markup.
    assert len(message.splitlines()[0]) > 80
    assert _error_line(monkeypatch, message) == f"error: {message}\n"
