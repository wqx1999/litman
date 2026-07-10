"""Tests for ``lit gui`` — import isolation, the missing-uvicorn guard, the
free-port finder, browser auto-open (--no-browser / --window / headless), and
the desktop shortcut (--make-shortcut). fastapi + uvicorn are core
dependencies now, but the CLI's startup path and this command's guard stay
fastapi-free by design (invariant #5): the import-isolation test proves that,
and the guard test simulates a corrupted install where uvicorn is missing
anyway. All browser/process side effects are monkeypatched — no test opens a
real window or starts a real server."""

from __future__ import annotations

import builtins
import importlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from litman.commands.gui import (
    _DEFAULT_PORT,
    _app_window_argv,
    _find_free_port,
    _stop_server_when_window_closes,
    browser_profile_dir,
    gui_cmd,
    remove_browser_profile,
    shortcut_path,
)

# ---------------------------------------------------------------------------
# A1(a) — importing the CLI must not pull fastapi into the process
# ---------------------------------------------------------------------------


def test_cli_import_does_not_load_fastapi() -> None:
    # Drop any fastapi/server modules a prior test may have imported, then
    # re-import the CLI from scratch and assert it stayed fastapi-free.
    for mod in list(sys.modules):
        if mod == "fastapi" or mod.startswith("fastapi.") or mod.startswith(
            "litman.cli"
        ) or mod.startswith("litman.server"):
            del sys.modules[mod]
    importlib.import_module("litman.cli")
    assert "fastapi" not in sys.modules


# ---------------------------------------------------------------------------
# A1(b) — missing-uvicorn guard (corrupted install): friendly message + exit
# ---------------------------------------------------------------------------


def test_gui_without_uvicorn_errors_with_hint(
    monkeypatch,
) -> None:
    real_import = builtins.__import__

    def _no_uvicorn(name, *args, **kwargs):
        if name == "uvicorn" or name.startswith("uvicorn."):
            raise ImportError("No module named 'uvicorn'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_uvicorn)

    result = CliRunner().invoke(gui_cmd, [])
    assert result.exit_code != 0
    # Points at a reinstall, not the (removed) optional extra.
    assert "reinstall litman" in result.output.lower()


# ---------------------------------------------------------------------------
# A6 — free-port finder (Jupyter model: never errors on a busy port)
# ---------------------------------------------------------------------------


def test_find_free_port_returns_default_when_free() -> None:
    assert _find_free_port(_DEFAULT_PORT) == _DEFAULT_PORT


def test_find_free_port_increments_when_busy() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", _DEFAULT_PORT))
        occupied.listen(1)
        chosen = _find_free_port(_DEFAULT_PORT)
    assert chosen >= _DEFAULT_PORT + 1


def test_find_free_port_binds_loopback_only() -> None:
    # The returned port must be bindable on 127.0.0.1 — proves the probe
    # targets loopback, not 0.0.0.0.
    port = _find_free_port(_DEFAULT_PORT)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


# ---------------------------------------------------------------------------
# browser auto-open (task-gui-desktop-entry D1/D2)
# ---------------------------------------------------------------------------


class _FakeTimer:
    """threading.Timer stand-in that fires synchronously on start(), so the
    open happens inside the CliRunner invocation instead of 1s later."""

    def __init__(self, interval: float, fn) -> None:
        self.interval = interval
        self.fn = fn
        self.daemon = False

    def start(self) -> None:
        self.fn()

    def cancel(self) -> None:
        pass


class _FakeProc:
    """subprocess.Popen stand-in for the app window. ``wait()`` blocks like the
    real thing — until the window "closes", which in these tests only happens
    when the command's cleanup terminates it."""

    def __init__(self, argv) -> None:
        self.argv = argv
        self.returncode = None
        self.terminated = False
        self._closed = threading.Event()

    def wait(self, timeout=None) -> int:
        assert self._closed.wait(timeout=5), "window never closed"
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self._closed.set()


class _FakeServer:
    """uvicorn.Server stand-in: records that the window-watcher asked it to
    stop. The real Server polls this same flag every 100ms."""

    instances: ClassVar[list[_FakeServer]] = []

    def __init__(self, *a, **k) -> None:
        self.should_exit = False
        self.ran = False
        _FakeServer.instances.append(self)

    def run(self) -> None:
        self.ran = True


@pytest.fixture
def gui_harness(monkeypatch):
    """Neutralize every side effect of a full `lit gui` run and record the
    browser-open calls. Returns (opened_urls, spawned_procs)."""
    opened: list[str] = []
    procs: list[_FakeProc] = []
    _FakeServer.instances.clear()

    def _fake_popen(argv, **kw):
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    monkeypatch.setattr(threading, "Timer", _FakeTimer)
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr("uvicorn.Server", _FakeServer)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None)
    return opened, procs


def _served_url(output: str) -> str:
    m = re.search(r"http://127\.0\.0\.1:\d+", output)
    assert m, f"no served URL in output: {output!r}"
    return m.group(0)


def test_gui_opens_browser_by_default_with_display(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    opened, _ = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert opened == [_served_url(result.output)]


def test_gui_no_browser_suppresses_open(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setenv("DISPLAY", ":0")

    result = CliRunner().invoke(
        gui_cmd, ["--library", str(vault), "--no-browser"]
    )

    assert result.exit_code == 0, result.output
    assert opened == [] and procs == []


def test_gui_headless_linux_never_opens(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert opened == [] and procs == []
    # 1.1.0 headless behavior unchanged: URL + tunnel line still printed.
    assert "SSH tunnel" in result.output


@pytest.fixture
def chromium_on_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/google-chrome"
        if name == "google-chrome"
        else None,
    )


def test_gui_window_uses_chromium_app_mode(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    opened, procs = gui_harness
    vault, _pid = vault_with_paper

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert opened == []
    assert len(procs) == 1
    argv = procs[0].argv
    assert argv[0] == "/usr/bin/google-chrome"
    assert f"--app={_served_url(result.output)}" in argv


def test_gui_window_gives_the_browser_a_profile_of_its_own(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    # Without --user-data-dir an already-running Chrome adopts the window and
    # the process we spawned exits at once — which the watcher below would read
    # as "the user closed the window" and kill the server a second after start.
    _opened, procs = gui_harness
    vault, _pid = vault_with_paper

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    argv = procs[0].argv
    assert f"--user-data-dir={browser_profile_dir()}" in argv
    # A never-before-used profile otherwise greets the user with a first-run
    # tab and a make-me-default prompt stacked on top of their library.
    assert "--no-first-run" in argv
    assert "--no-default-browser-check" in argv


def test_gui_window_ties_the_server_to_the_window(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    """Closing the app window must stop the server. Here the command's own
    cleanup closes it; the point is that gui_cmd wired *this* process to *this*
    server, so the watcher fires."""
    vault, _pid = vault_with_paper

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])
    assert result.exit_code == 0, result.output

    (server,) = _FakeServer.instances
    assert server.ran
    deadline = time.monotonic() + 5
    while not server.should_exit and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.should_exit, "window closed but the server was never stopped"


def test_gui_window_exit_closes_the_window(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    # The other direction: Ctrl+C stops the server, which must not leave a dead
    # shell of a window on screen. Safe only because the profile is ours alone.
    _opened, procs = gui_harness
    vault, _pid = vault_with_paper

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert procs[0].terminated


def test_gui_window_announces_the_lifecycle(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    vault, _pid = vault_with_paper
    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])
    assert "Close the window to stop the server" in result.output


def test_gui_tab_mode_never_ties_the_server_to_a_browser(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    # A terminal-launched tab keeps the plain Ctrl+C contract: we do not own
    # the user's browser and must never stop on its account.
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert opened == [_served_url(result.output)] and procs == []
    (server,) = _FakeServer.instances
    assert server.should_exit is False
    assert "Close the window" not in result.output


def test_gui_window_falls_back_to_tab_when_no_chromium(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert procs == []
    assert opened == [_served_url(result.output)]
    assert "normal browser tab" in result.output
    # No process to watch → the server must not adopt the window's lifecycle.
    (server,) = _FakeServer.instances
    assert server.should_exit is False


def test_gui_window_and_no_browser_conflict() -> None:
    result = CliRunner().invoke(gui_cmd, ["--window", "--no-browser"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_stop_server_when_window_closes_sets_should_exit() -> None:
    # The seam itself, driven directly: the real uvicorn.Server polls this same
    # attribute every 100ms from its event loop.
    proc, server = _FakeProc(["chrome"]), _FakeServer()
    watcher = threading.Thread(
        target=_stop_server_when_window_closes, args=(proc, server), daemon=True
    )
    watcher.start()
    assert server.should_exit is False  # still open
    proc.terminate()  # the user closes the window
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert server.should_exit is True


# ---------------------------------------------------------------------------
# the app window's own browser profile
# ---------------------------------------------------------------------------


def test_browser_profile_dir_follows_the_registry_override(monkeypatch, tmp_path):
    # Same seam as preferences.yaml, so the autouse _isolate_registry fixture
    # keeps a real Chromium profile out of a developer's home for free.
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    assert browser_profile_dir().parent == tmp_path / "cfg"


def test_browser_profile_dir_defaults_outside_the_config_dir(monkeypatch):
    # Tens of MB of Chromium state must not ride along on a cloud-synced
    # config dir next to vaults.yaml.
    from litman.core.vault_registry import registry_path

    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    assert browser_profile_dir().parent != registry_path().parent


def test_remove_browser_profile_is_a_noop_when_absent() -> None:
    assert remove_browser_profile() is None


def test_remove_browser_profile_deletes_it() -> None:
    profile = browser_profile_dir()
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text("{}", encoding="utf-8")

    assert remove_browser_profile() == profile
    assert not profile.exists()


def test_app_window_argv_darwin_runs_the_bundle_binary(monkeypatch, tmp_path):
    # `open -na` asks Launch Services to start the app and returns immediately,
    # so it can never own the window. Run the binary inside the bundle instead.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    binary = (
        tmp_path
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    )
    binary.parent.mkdir(parents=True)
    binary.touch()

    argv = _app_window_argv("http://127.0.0.1:8765")

    assert argv is not None
    assert argv[0] == str(binary)
    assert "open" not in argv


# ---------------------------------------------------------------------------
# --make-shortcut (task-gui-desktop-entry D3)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_lit_on_path(monkeypatch):
    """Pin `lit` resolution to a fixed path (with a space, to prove quoting)
    so shortcut content is deterministic regardless of the test host PATH."""
    fake = "/opt/lit tools/bin/lit"
    monkeypatch.setattr(
        shutil, "which", lambda name: fake if name == "lit" else None
    )
    return fake


def test_make_shortcut_linux_writes_desktop_file(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))

    def _boom(*a, **k):
        raise AssertionError("--make-shortcut must not start the server")

    monkeypatch.setattr("uvicorn.Server", _boom)

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    desktop = tmp_path / "share" / "applications" / "litman.desktop"
    assert desktop.is_file()
    content = desktop.read_text(encoding="utf-8")
    assert f'Exec="{fake_lit_on_path}" gui --window' in content
    icon_line = next(
        line for line in content.splitlines() if line.startswith("Icon=")
    )
    assert Path(icon_line.removeprefix("Icon=")).is_file()

    # Idempotent re-run: overwrite + "updated", never an error.
    result2 = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result2.exit_code == 0, result2.output
    assert "updated" in result2.output


def test_shortcut_path_win32_is_on_desktop(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    assert shortcut_path() == tmp_path / "profile" / "Desktop" / "litman.lnk"


def test_make_shortcut_win32_builds_powershell_command(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    runs: list[list[str]] = []

    def _fake_run(argv, **kw):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    assert len(runs) == 1
    argv = runs[0]
    assert argv[0] == "powershell"
    script = argv[-1]
    assert "CreateShortcut" in script
    assert "litman.lnk" in script
    # Lands on the actual Desktop, not the Start Menu.
    assert str(tmp_path / "profile" / "Desktop" / "litman.lnk") in script
    assert f"$s.TargetPath = '{fake_lit_on_path}'" in script
    assert "$s.Arguments = 'gui --window'" in script
    assert "litman.ico" in script
    # Explorer caches the icon by the .ico's path, which never changes across
    # upgrades — without this notification an upgraded user keeps the old
    # artwork. It must run after Save() and must not be able to fail the write.
    assert script.index("$s.Save()") < script.index("SHChangeNotify")
    assert "try {" in script and "} catch { }" in script


def test_make_shortcut_win32_targets_the_consoleless_twin(
    monkeypatch, tmp_path
) -> None:
    # Windows reads "does this get a console window" off the exe's PE header,
    # and no .lnk field overrides it — so the shortcut must run litw, not lit.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    exes = {"lit": r"C:\tools\lit.exe", "litw": r"C:\tools\litw.exe"}
    monkeypatch.setattr(shutil, "which", lambda name: exes.get(name))
    runs: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: (
            runs.append(argv),
            subprocess.CompletedProcess(argv, 0),
        )[1],
    )

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    script = runs[0][-1]
    assert "litw.exe" in script
    assert "$s.TargetPath = '" + str(Path(exes["litw"]).resolve()) + "'" in script


def test_make_shortcut_win32_falls_back_to_lit_without_the_twin(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # An install predating litw: a console window is ugly, but a shortcut that
    # refuses to exist is worse.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    runs: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: (
            runs.append(argv),
            subprocess.CompletedProcess(argv, 0),
        )[1],
    )

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output
    assert f"$s.TargetPath = '{fake_lit_on_path}'" in runs[0][-1]


def test_make_shortcut_darwin_builds_app_bundle(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    app = tmp_path / "Applications" / "litman.app"
    stub = app / "Contents" / "MacOS" / "litman"
    assert stub.is_file()
    assert stub.stat().st_mode & 0o111, "launcher stub must be executable"
    assert (
        f'exec "{fake_lit_on_path}" gui --window'
        in stub.read_text(encoding="utf-8")
    )
    plist = (app / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert "CFBundleExecutable" in plist


# ---------------------------------------------------------------------------
# bundled icon assets (task-gui-desktop-entry D3, package-data)
# ---------------------------------------------------------------------------


def test_bundled_icons_resolve_via_importlib_resources() -> None:
    from importlib.resources import files

    for name in ("litman.png", "litman.ico"):
        icon = files("litman").joinpath("assets", "icons", name)
        assert icon.is_file(), f"missing bundled icon {name}"
        assert len(icon.read_bytes()) > 0
