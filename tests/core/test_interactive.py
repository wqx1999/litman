"""``is_interactive``: a terminal a person is at, not any character device.

Windows reports the NUL device as a tty, and NUL is the stdin an agent's
commands get there. The Windows branch is driven through two seams
(``_on_windows`` / ``_is_console``) so it runs everywhere; the real process
test at the bottom is the one that meets NUL itself, on Windows CI.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

import litman
from litman.core import confirm


class _Answers:
    """A stand-in that has an answer for ``isatty()`` and no OS handle."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _Closed:
    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")


class _WithFd:
    """A tty-looking stream backed by ``fd``, or whose ``fileno()`` raises it."""

    def __init__(self, fd: int | BaseException) -> None:
        self._fd = fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        if isinstance(self._fd, BaseException):
            raise self._fd
        return self._fd


@pytest.fixture(params=[False, True], ids=["posix", "windows"])
def on_windows(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(confirm, "_on_windows", lambda: request.param)
    return bool(request.param)


def test_no_stream_is_not_interactive(on_windows: bool) -> None:
    # litw: a windows-subsystem process starts with stdin / stdout None.
    assert confirm.is_interactive(None) is False


def test_a_stream_that_is_no_tty_is_not_interactive(on_windows: bool) -> None:
    assert confirm.is_interactive(io.StringIO()) is False
    assert confirm.is_interactive(_Answers(False)) is False


def test_a_stream_whose_isatty_raises_is_not_interactive(on_windows: bool) -> None:
    assert confirm.is_interactive(_Closed()) is False
    assert confirm.is_interactive(object()) is False


def test_a_stand_in_without_a_handle_keeps_its_own_answer(on_windows: bool) -> None:
    """A test's fake tty has no fileno(); on Windows too it stays a tty."""
    assert confirm.is_interactive(_Answers(True)) is True
    assert confirm.is_interactive(_WithFd(io.UnsupportedOperation("fileno"))) is True


def test_on_windows_a_tty_counts_only_when_it_is_a_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(confirm, "_on_windows", lambda: True)
    asked: list[int] = []

    def is_console(fd: int) -> bool:
        asked.append(fd)
        return fd == 7

    monkeypatch.setattr(confirm, "_is_console", is_console)
    assert confirm.is_interactive(_WithFd(7)) is True
    assert confirm.is_interactive(_WithFd(3)) is False
    assert asked == [7, 3]


def test_on_windows_a_failing_fileno_is_not_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(confirm, "_on_windows", lambda: True)
    monkeypatch.setattr(confirm, "_is_console", lambda fd: True)
    assert confirm.is_interactive(_WithFd(OSError("bad handle"))) is False


def test_off_windows_no_console_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(confirm, "_on_windows", lambda: False)

    def is_console(fd: int) -> bool:
        raise AssertionError("asked for a console off Windows")

    monkeypatch.setattr(confirm, "_is_console", is_console)
    assert confirm.is_interactive(_WithFd(3)) is True


def test_is_console_answers_no_for_a_bad_handle() -> None:
    assert confirm._is_console(-1) is False


_PROBE = (
    "import sys\n"
    "from litman.core.confirm import is_interactive\n"
    "print(sys.stdin.isatty(), is_interactive(sys.stdin))\n"
)


def test_a_devnull_stdin_is_not_interactive_in_a_real_process(tmp_path: Path) -> None:
    """stdin=DEVNULL is NUL on Windows. The first half is the control: Python
    there does call NUL a tty, so the second half is what tells it apart."""
    env = os.environ.copy()
    env["HOME"] = env["USERPROFILE"] = str(tmp_path)
    env["LITMAN_REGISTRY_DIR"] = str(tmp_path / "registry")
    src_dir = Path(litman.__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    isatty, ours = proc.stdout.split()
    assert isatty == ("True" if sys.platform == "win32" else "False")
    assert ours == "False"
