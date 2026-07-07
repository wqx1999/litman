"""Unit tests for ``core.terminal.spawn_terminal`` (task-agent-launch AC6).

Every spawn is stubbed — no test opens a real window. The one un-stubbed
spawn in the suite is `lit agent`'s AC1 subprocess test (a windowless probe
process), which covers the live path.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from litman.core.terminal import spawn_terminal


@pytest.fixture
def popen_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], object]]:
    """Record every Popen (argv, cwd) instead of spawning."""
    calls: list[tuple[list[str], object]] = []

    class _FakePopen:
        def __init__(self, argv: list[str], cwd: object = None, **_kw: object):
            calls.append((argv, cwd))

    monkeypatch.setattr("litman.core.terminal.subprocess.Popen", _FakePopen)
    return calls


def _which_only(monkeypatch: pytest.MonkeyPatch, hit: str) -> None:
    monkeypatch.setattr(
        "litman.core.terminal.shutil.which",
        lambda name: f"/usr/bin/{hit}" if name == hit else None,
    )


@pytest.fixture
def linux_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


def test_linux_headless_returns_false_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert spawn_terminal(["claude"], tmp_path) is False
    assert popen_calls == []


def test_linux_gnome_terminal_uses_double_dash(
    monkeypatch: pytest.MonkeyPatch,
    linux_display: None,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    _which_only(monkeypatch, "gnome-terminal")
    assert spawn_terminal(["claude", "--continue"], tmp_path) is True
    argv, cwd = popen_calls[0]
    assert argv == ["/usr/bin/gnome-terminal", "--", "claude", "--continue"]
    assert cwd == str(tmp_path)


def test_linux_xterm_uses_dash_e_with_argv(
    monkeypatch: pytest.MonkeyPatch,
    linux_display: None,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    _which_only(monkeypatch, "xterm")
    assert spawn_terminal(["claude", "--continue"], tmp_path) is True
    argv, _cwd = popen_calls[0]
    assert argv == ["/usr/bin/xterm", "-e", "claude", "--continue"]


def test_linux_xfce4_terminal_gets_single_command_string(
    monkeypatch: pytest.MonkeyPatch,
    linux_display: None,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    _which_only(monkeypatch, "xfce4-terminal")
    assert spawn_terminal(["claude", "--continue"], tmp_path) is True
    argv, _cwd = popen_calls[0]
    assert argv == ["/usr/bin/xfce4-terminal", "-e", "claude --continue"]


def test_linux_probe_order_prefers_x_terminal_emulator(
    monkeypatch: pytest.MonkeyPatch,
    linux_display: None,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    """The Debian alternatives symlink wins over specific emulators."""
    monkeypatch.setattr(
        "litman.core.terminal.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    assert spawn_terminal(["claude"], tmp_path) is True
    argv, _cwd = popen_calls[0]
    assert argv[0] == "/usr/bin/x-terminal-emulator"
    assert argv[1] == "-e"


def test_linux_no_terminal_found_returns_false(
    monkeypatch: pytest.MonkeyPatch,
    linux_display: None,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("litman.core.terminal.shutil.which", lambda _n: None)
    assert spawn_terminal(["claude"], tmp_path) is False
    assert popen_calls == []


def test_darwin_osascript_quotes_the_cwd(
    monkeypatch: pytest.MonkeyPatch,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cwd = tmp_path / "my vault"  # space → must be quoted inside the script
    cwd.mkdir()
    assert spawn_terminal(["claude"], cwd) is True
    argv, _cwd = popen_calls[0]
    assert argv[0] == "osascript"
    script = argv[2]
    assert script.startswith('tell application "Terminal" to do script "')
    assert shlex.quote(str(cwd)) in script.replace('\\"', '"')


def test_win32_prefers_windows_terminal(
    monkeypatch: pytest.MonkeyPatch,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "litman.core.terminal.shutil.which",
        lambda name: "C:\\wt.exe" if name == "wt" else None,
    )
    assert spawn_terminal(["claude"], tmp_path) is True
    argv, _cwd = popen_calls[0]
    assert argv == ["C:\\wt.exe", "-d", str(tmp_path), "claude"]


def test_win32_falls_back_to_cmd_start(
    monkeypatch: pytest.MonkeyPatch,
    popen_calls: list[tuple[list[str], object]],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("litman.core.terminal.shutil.which", lambda _n: None)
    assert spawn_terminal(["claude"], tmp_path) is True
    argv, _cwd = popen_calls[0]
    assert argv == ["cmd", "/c", "start", "cmd", "/K", "claude"]


def test_spawn_failure_returns_false_never_raises(
    monkeypatch: pytest.MonkeyPatch,
    linux_display: None,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "litman.core.terminal.shutil.which", lambda _n: "/usr/bin/xterm"
    )

    def _boom(*_a: object, **_kw: object) -> None:
        raise OSError("spawn refused")

    monkeypatch.setattr("litman.core.terminal.subprocess.Popen", _boom)
    assert spawn_terminal(["claude"], tmp_path) is False
