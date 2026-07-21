"""Tests for ``lit gui`` — import isolation, the missing-uvicorn guard, the
free-port finder, browser auto-open (--no-browser / --window / headless), the
window watcher's shutdown gate (proc exit AND no live page), and the desktop
shortcut (--make-shortcut). fastapi + uvicorn are core dependencies now, but
the CLI's startup path and this command's guard stay fastapi-free by design
(invariant #5): the import-isolation test proves that, and the guard test
simulates a corrupted install where uvicorn is missing anyway. No test opens
a real window or starts a real server — the watcher tests drive the gate with
an already-exited throwaway process and a hand-fed presence tracker."""

from __future__ import annotations

import builtins
import functools
import importlib
import io
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from click.testing import CliRunner

from litman import cli
from litman.commands import gui
from litman.commands.gui import (
    _DEFAULT_PORT,
    _app_window_argv,
    _find_free_port,
    _open_when_ready,
    _purge_stale_browser_session,
    _quiet_browser_profile,
    _spawn_ready_watcher,
    _stop_server_when_window_closes,
    _terminate_splash_when_visible,
    browser_profile_dir,
    gui_cmd,
    remove_browser_profile,
    shortcut_path,
)
from litman.core.presence import PresenceTracker

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


def _sync_ready_watcher(server, open_browser, stop_event, after_open=None):
    """`_spawn_ready_watcher` stand-in that fires the open synchronously — no
    thread, no ``server.started`` poll — so the browser open and its splash
    hand-off run inside the CliRunner invocation instead of racing it. Returns
    None, so gui_cmd's ``finally`` skips the (unneeded) join. The readiness
    poll itself is covered directly by the A1-A3 unit tests."""
    open_browser()
    if after_open is not None:
        after_open()
    return None


class _FakeProc:
    """subprocess.Popen stand-in for the app window. ``poll()`` reports the
    process as alive until the window "closes", which in these tests only
    happens when the command's cleanup terminates it."""

    def __init__(self, argv) -> None:
        self.argv = argv
        self.returncode = None
        self.terminated = False
        self._closed = threading.Event()

    def poll(self) -> int | None:
        # The watcher polls (never waits): None while the window is open, the
        # exit code once cleanup has terminated it.
        if self._closed.is_set():
            self.returncode = 0
            return 0
        return None

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
    browser-open calls. Returns (opened_urls, spawned_procs).

    The watcher keeps its shipped constants as keyword defaults, and gui_cmd
    offers no injection seam for them — under this harness no page ever
    connects, so a --window test would sit out the full first-connect grace.
    Rebinding the module global to a shortened partial covers every test that
    goes through gui_cmd (the `_open` closure resolves the name at call time).
    """
    opened: list[str] = []
    procs: list[_FakeProc] = []
    _FakeServer.instances.clear()

    def _fake_popen(argv, **kw):
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    monkeypatch.setattr(gui, "_spawn_ready_watcher", _sync_ready_watcher)
    # A console-less launch would Popen the splash; force "has a console" so
    # these pre-splash tests keep exactly their old behaviour. Splash wiring
    # has its own dedicated tests further down.
    monkeypatch.setattr(gui, "_launched_without_console", lambda: False)
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr("uvicorn.Server", _FakeServer)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None)
    monkeypatch.setattr(
        gui,
        "_stop_server_when_window_closes",
        functools.partial(
            gui._stop_server_when_window_closes,
            first_connect_grace=0.2,
            linger=0.1,
            poll=0.02,
        ),
    )
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
    # the process we spawned exits at once — leaving nothing for the Ctrl+C
    # path to terminate, and litman's window inside the user's own session.
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
    assert "--disable-features=Translate" in argv
    assert "--disable-sync" in argv
    # A force-killed browser marks the profile crashed; without this the next
    # window opens under a "restore pages?" bubble for an emptied session.
    assert "--hide-crash-restore-bubble" in argv


def test_gui_window_marks_a_fresh_browser_profile_as_already_run(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    # Without the sentinel Edge restarts itself partway through a new profile's
    # first run — the process we spawned exits while the window lives on in a
    # process we never see. The presence gate survives that handoff on its
    # own; the sentinel stays as defense in depth, and as what keeps Edge from
    # signing the profile into the Windows account.
    vault, _pid = vault_with_paper

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert (browser_profile_dir() / "First Run").is_file()


def test_quiet_browser_profile_writes_nothing_else(tmp_path) -> None:
    # Seeding Chromium's Preferences from outside makes the browser announce
    # that its settings were changed unexpectedly — louder than the prompts it
    # was meant to silence. The sentinel is the whole intervention.
    _quiet_browser_profile(tmp_path)

    assert [p.name for p in tmp_path.iterdir()] == ["First Run"]


def test_quiet_browser_profile_survives_an_unwritable_profile(tmp_path) -> None:
    # A profile we cannot seed still opens a window, just a chattier one — the
    # seeding must never be the thing that stops `lit gui --window`.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("", encoding="utf-8")

    _quiet_browser_profile(not_a_dir)  # must not raise

    assert not_a_dir.is_file()


def test_gui_window_purges_the_stale_browser_session(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    # A Task-Manager-killed browser is recorded as a crash, and the next
    # launch resurrects the dead session's app window alongside the one we
    # asked for — a days-old page against a server that no longer exists.
    # Every launch brings its own URL, so there is never a session worth
    # restoring: the launcher empties the restore state before spawning.
    vault, _pid = vault_with_paper
    default = browser_profile_dir() / "Default"
    (default / "Sessions").mkdir(parents=True)
    (default / "Sessions" / "Session_13342").write_bytes(b"SNSS")
    (default / "Current Session").write_bytes(b"SNSS")

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert not (default / "Sessions").exists()
    assert not (default / "Current Session").exists()


def test_purge_stale_browser_session_takes_only_the_restore_state(
    tmp_path,
) -> None:
    # Only the session-restore state goes. Preferences stay untouched —
    # seeding or wiping them from outside makes the browser announce
    # tampering, the same line _quiet_browser_profile draws.
    default = tmp_path / "Default"
    (default / "Sessions").mkdir(parents=True)
    (default / "Sessions" / "Session_1").write_bytes(b"SNSS")
    for name in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        (default / name).write_bytes(b"SNSS")
    (default / "Preferences").write_text("{}", encoding="utf-8")

    _purge_stale_browser_session(tmp_path)

    assert not (default / "Sessions").exists()
    for name in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        assert not (default / name).exists()
    assert (default / "Preferences").is_file()


def test_purge_stale_browser_session_is_a_noop_on_a_fresh_profile(
    tmp_path,
) -> None:
    _purge_stale_browser_session(tmp_path)  # no Default yet — must not raise

    assert list(tmp_path.iterdir()) == []


def test_gui_window_ties_the_server_to_the_window(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    """Closing the app window must stop the server. Here the command's own
    cleanup closes it and no page ever connected (fake server), so the gate
    exits via the first-connect grace; the point is that gui_cmd wired *this*
    process and *this* tracker to *this* server, so the watcher fires."""
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


# ---------------------------------------------------------------------------
# the window watcher's AND gate — proc exit alone must not stop the server
# (task-window-presence-gate)
# ---------------------------------------------------------------------------


def _exited_proc() -> subprocess.Popen[bytes]:
    """A real process that has already exited — the Edge self-restart shape:
    the process we spawned is gone, and whether the window is too is exactly
    what the presence tracker has to answer."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc


def _live_proc() -> subprocess.Popen[bytes]:
    """A real process that stays alive — the Windows shape: Edge keeps the
    browser process resident (Startup boost, single-instance-per-profile) long
    after the app window is closed, so ``proc`` never exits even though every
    page is gone. Callers must terminate it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_watcher_stops_on_page_close_even_if_the_process_never_exits() -> None:
    # The Windows bug this whole rewrite is about: our spawned browser process
    # stays resident forever, but the window is closed and its page's presence
    # socket has dropped. The server must still stop — the last live page, not
    # the process, is the signal. (The old code blocked on proc.wait() here and
    # never reached the presence loop, so the server lingered.)
    tracker = PresenceTracker()
    tracker.connect()
    proc, server = _live_proc(), _FakeServer()
    try:
        watcher = threading.Thread(
            target=_stop_server_when_window_closes,
            args=(proc, server, tracker),
            kwargs={"first_connect_grace": 30.0, "linger": 0.2, "poll": 0.02},
            daemon=True,
        )
        watcher.start()
        time.sleep(0.3)  # the process is alive and a page holds the socket
        assert server.should_exit is False
        tracker.disconnect()  # the user closes the window; the socket drops
        watcher.join(timeout=5)
        assert not watcher.is_alive()
        assert server.should_exit is True  # stopped despite the live process
    finally:
        proc.terminate()
        proc.wait()


def test_watcher_waits_while_the_window_is_up_but_no_page_connected_yet() -> None:
    # The other side of not-waiting-on-the-process: a window slow to paint its
    # first page. The process is alive and no presence socket has opened, so the
    # first-connect grace must NOT fire — that clock is only for a launch whose
    # process is already gone. A live window past the grace stays served.
    proc, server = _live_proc(), _FakeServer()
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(proc, server, PresenceTracker()),
        kwargs={"first_connect_grace": 0.1, "linger": 0.1, "poll": 0.02},
        daemon=True,
    )
    try:
        watcher.start()
        time.sleep(0.4)  # well past the grace, but the process is still alive
        assert server.should_exit is False
    finally:
        proc.terminate()
        proc.wait()
        watcher.join(timeout=5)  # observes the exit, applies grace, exits clean


def test_watcher_exits_after_grace_when_no_page_ever_connected() -> None:
    # The browser never came up at all: no page will ever connect, so the
    # gate must fall back to the first-connect grace — a failed launch may
    # not leave an orphaned server behind.
    proc, server = _exited_proc(), _FakeServer()
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(proc, server, PresenceTracker()),
        kwargs={"first_connect_grace": 1.0, "linger": 0.1, "poll": 0.02},
        daemon=True,
    )
    watcher.start()
    assert server.should_exit is False  # inside the grace window
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert server.should_exit is True


def test_watcher_holds_while_a_page_is_connected() -> None:
    # The bug scenario itself: the spawned process is gone (Chromium handed
    # the window to a process we never see) but the page is alive and holds
    # the presence socket. The server must stay up on the page's account —
    # and stop only a linger after the page goes away.
    tracker = PresenceTracker()
    tracker.connect()
    proc, server = _exited_proc(), _FakeServer()
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(proc, server, tracker),
        kwargs={"first_connect_grace": 0.1, "linger": 0.2, "poll": 0.02},
        daemon=True,
    )
    watcher.start()
    time.sleep(0.5)  # well past the grace: the page is what holds the server
    assert server.should_exit is False
    tracker.disconnect()  # the user closes the last page
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert server.should_exit is True


def test_watcher_survives_a_reload_inside_the_linger() -> None:
    # F5: the page's socket drops and the reloaded page reconnects a moment
    # later. The gap must not read as "the last page closed" — that is what
    # the linger is for.
    tracker = PresenceTracker()
    tracker.connect()
    proc, server = _exited_proc(), _FakeServer()
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(proc, server, tracker),
        kwargs={"first_connect_grace": 0.05, "linger": 0.8, "poll": 0.02},
        daemon=True,
    )
    watcher.start()
    tracker.disconnect()  # the old page tears down...
    time.sleep(0.1)  # ...a reload-sized gap, well inside the linger...
    tracker.connect()  # ...and the reloaded page arrives
    time.sleep(1.0)  # past the linger as measured from the disconnect
    assert server.should_exit is False
    # Let the watcher finish so the thread does not outlive the test.
    tracker.disconnect()
    watcher.join(timeout=5)
    assert server.should_exit is True


class _TornTracker:
    """A tracker pinned in the torn one-poll view: a connect is landing right
    now, so ever_connected is already True while last_zero is still None.
    ``PresenceTracker.snapshot()`` confines this to a single poll in real use;
    pinning it proves the watcher's None guard absorbs it (``None`` in the
    idle subtraction would be a TypeError that kills the daemon silently)."""

    def __init__(self) -> None:
        self.polls = 0
        self.state: tuple[int, bool, float | None] = (0, True, None)

    def snapshot(self) -> tuple[int, bool, float | None]:
        self.polls += 1
        return self.state


def test_watcher_torn_snapshot_neither_breaks_nor_crashes() -> None:
    tracker = _TornTracker()
    proc, server = _exited_proc(), _FakeServer()
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(proc, server, tracker),
        kwargs={"first_connect_grace": 0.05, "linger": 0.05, "poll": 0.02},
        daemon=True,
    )
    watcher.start()
    time.sleep(0.3)
    # Still alive and still polling: no exit (the torn view is not idleness)
    # and no crash (the None guard held).
    assert watcher.is_alive()
    assert server.should_exit is False
    assert tracker.polls >= 2
    # Resolve the view to long-idle so the watcher can finish cleanly.
    tracker.state = (0, True, time.monotonic() - 60.0)
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
    """Fallback arm: no shell API reachable (this POSIX host has no
    ctypes.windll) → the literal %USERPROFILE%\\Desktop."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    assert shortcut_path() == tmp_path / "profile" / "Desktop" / "litman.lnk"


def test_shortcut_path_win32_honors_onedrive_desktop_redirect(
    monkeypatch, tmp_path
) -> None:
    """OneDrive folder backup moves Desktop to %USERPROFILE%\\OneDrive\\Desktop;
    the shortcut must follow the shell's answer (SHGetFolderPathW), or the
    installer promises a Desktop icon the user cannot see."""
    import types

    redirected = tmp_path / "profile" / "OneDrive" / "Desktop"

    class _Buf:
        value = str(redirected)

    fake_ctypes = types.SimpleNamespace(
        create_unicode_buffer=lambda n: _Buf(),
        windll=types.SimpleNamespace(
            shell32=types.SimpleNamespace(SHGetFolderPathW=lambda *a: 0)
        ),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    assert shortcut_path() == redirected / "litman.lnk"


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


# ===========================================================================
# Part A — readiness poll replaces the flat 1s browser timer
# ===========================================================================


class _StartFlag:
    """Minimal uvicorn.Server stand-in exposing just the ``started`` flag the
    readiness poller reads."""

    def __init__(self, started: bool = False) -> None:
        self.started = started


def test_open_when_ready_opens_only_after_started_flips() -> None:
    # A1: the poller must wait for `server.started`, then open exactly once.
    server = _StartFlag(started=False)
    calls: list[float] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_open_when_ready,
        args=(server, lambda: calls.append(time.monotonic()), stop),
        kwargs={"ready_timeout": 5.0, "ready_poll": 0.01},
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    assert calls == []  # still closed: server has not started
    server.started = True
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(calls) == 1  # opened once, and only after started flipped


def test_open_when_ready_timeout_backstop_opens_anyway() -> None:
    # A1: a server that never lists still gets a best-effort open at the
    # deadline (the shipped "always try to open" behaviour), not a hang.
    server = _StartFlag(started=False)
    calls: list[int] = []
    stop = threading.Event()
    _open_when_ready(
        server, lambda: calls.append(1), stop,
        ready_timeout=0.05, ready_poll=0.01,
    )
    assert calls == [1]


def test_open_when_ready_skips_open_when_already_stopped() -> None:
    # D2/SF-2: if the stop event is set (server.run() raised before listening),
    # the poller must never open a browser onto a dead server.
    server = _StartFlag(started=False)
    calls: list[int] = []
    stop = threading.Event()
    stop.set()
    _open_when_ready(
        server, lambda: calls.append(1), stop,
        ready_timeout=5.0, ready_poll=0.01,
    )
    assert calls == []


def test_open_when_ready_skips_open_when_stopped_after_ready() -> None:
    # D2/SF-2 (the real race): started is True, but the stop event lands before
    # the open. The re-check after the loop must still refuse to open.
    server = _StartFlag(started=True)
    calls: list[int] = []
    stop = threading.Event()
    stop.set()
    _open_when_ready(
        server, lambda: calls.append(1), stop,
        ready_timeout=5.0, ready_poll=0.01,
    )
    assert calls == []


def test_spawn_ready_watcher_runs_after_open_once_after_open() -> None:
    # inject-seam (Part A -> Part C hand-off): drive the REAL
    # _spawn_ready_watcher — so both the thread wiring AND the after_open kwarg
    # being threaded into _open_when_ready are exercised — with a non-None
    # after_open. The shipped splash hand-off branch (`if after_open is not
    # None: after_open()`) is otherwise only reached via the _sync_ready_watcher
    # stand-in, so a real wiring break would keep the whole suite green. The
    # hand-off must fire exactly once, and only after the browser open.
    server = _StartFlag(started=True)
    order: list[str] = []
    stop = threading.Event()
    watcher = _spawn_ready_watcher(
        server,
        lambda: order.append("open"),
        stop,
        after_open=lambda: order.append("after"),
    )
    watcher.join(timeout=2)
    assert not watcher.is_alive()
    assert order == ["open", "after"]  # once, and after the open


def test_spawn_ready_watcher_skips_after_open_on_stop_path() -> None:
    # inject-seam (stop path): the A3 shape through the REAL watcher — stop set
    # before the server ever lists. Neither the open nor the splash hand-off may
    # run onto a server that is shutting down.
    server = _StartFlag(started=False)
    order: list[str] = []
    stop = threading.Event()
    stop.set()
    watcher = _spawn_ready_watcher(
        server,
        lambda: order.append("open"),
        stop,
        after_open=lambda: order.append("after"),
    )
    watcher.join(timeout=2)
    assert not watcher.is_alive()
    assert order == []  # no open, no after_open


def test_open_when_ready_real_server_default_constants() -> None:
    # A2 (inject-seam real default): drive the REAL _spawn_ready_watcher +
    # _open_when_ready with the shipped READY_TIMEOUT/READY_POLL against a real
    # uvicorn.Server. The poller must open once the socket is actually
    # listening. Server runs on the main thread (uvicorn installs signal
    # handlers there); the watcher's open stops it so run() returns.
    import uvicorn

    from litman.server import create_app

    port = _find_free_port(_DEFAULT_PORT)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(None), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    opened: list[bool] = []
    stop = threading.Event()

    def _open() -> None:
        opened.append(True)
        server.should_exit = True

    watcher = _spawn_ready_watcher(server, _open, stop)  # DEFAULT constants
    try:
        server.run()  # blocks until _open sets should_exit
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert not watcher.is_alive()
    assert opened == [True]  # opened after the real server became ready


def test_ready_watcher_is_reaped_when_server_never_listens() -> None:
    # A3: the stop event cleanly terminates the poller (no browser opened, no
    # daemon left hanging) — what gui_cmd's finally relies on when run() raises.
    server = _StartFlag(started=False)  # never lists
    opened: list[int] = []
    stop = threading.Event()
    watcher = _spawn_ready_watcher(server, lambda: opened.append(1), stop)
    stop.set()  # exactly what gui_cmd's finally does
    watcher.join(timeout=2)
    assert not watcher.is_alive()
    assert watcher not in threading.enumerate()
    assert opened == []


def test_gui_cmd_reaps_ready_thread_when_server_run_raises(
    monkeypatch, vault_with_paper
) -> None:
    # A3 through gui_cmd: server.run() raising must not leave the readiness
    # thread (a real one here — not the synchronous harness) dangling.
    vault, _pid = vault_with_paper
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(webbrowser, "open", lambda url: None)

    class _BoomServer:
        def __init__(self, *a, **k) -> None:
            self.started = False
            self.should_exit = False

        def run(self) -> None:
            raise RuntimeError("startup boom")

    monkeypatch.setattr("uvicorn.Server", _BoomServer)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None)

    before = set(threading.enumerate())
    result = CliRunner().invoke(gui_cmd, ["--library", str(vault)])

    assert result.exit_code != 0  # the RuntimeError (a bug) propagates
    time.sleep(0.2)
    leaked = [
        t for t in threading.enumerate() if t not in before and t.is_alive()
    ]
    assert leaked == [], f"leaked threads after server.run() raised: {leaked}"


# ===========================================================================
# Part C — tkinter splash (trigger, silent degradation, lifecycle, headless)
# ===========================================================================


class _Tty:
    def isatty(self) -> bool:
        return True


def test_launched_without_console_true_for_litw_none_stdout(monkeypatch) -> None:
    # C1: litw handed the process no stdio (recorded in _LAUNCHED_HEADLESS).
    monkeypatch.setattr(cli, "_LAUNCHED_HEADLESS", True)
    assert cli._launched_without_console() is True


def test_launched_without_console_false_in_a_terminal(monkeypatch) -> None:
    # C1: a real terminal (tty) already shows the console.print feedback.
    monkeypatch.setattr(cli, "_LAUNCHED_HEADLESS", False)
    monkeypatch.setattr(sys, "__stdout__", _Tty())
    assert cli._launched_without_console() is False


def test_launched_without_console_true_when_redirected_nontty(monkeypatch) -> None:
    # C1: POSIX .desktop / .app launch — stdout is a real object but not a tty.
    monkeypatch.setattr(cli, "_LAUNCHED_HEADLESS", False)
    monkeypatch.setattr(sys, "__stdout__", io.StringIO())  # isatty() is False
    assert cli._launched_without_console() is True


def _splash_launched(argvs: list[list[str]]) -> bool:
    return any("litman.commands._splash" in argv for argv in argvs)


@pytest.fixture
def splash_gui(monkeypatch):
    """gui_cmd harness that records splash vs window Popens separately. Unlike
    gui_harness it does NOT force _launched_without_console — each test sets the
    console / display / window / chromium combination it needs. Defaults to no
    Chromium (so --window falls back to a tab, keeping the splash hand-off from
    blocking on a presence connection that will never come)."""
    _FakeServer.instances.clear()
    rec = SimpleNamespace(
        splashes=[], window_procs=[], opened=[], argvs=[]
    )

    def _popen(argv, **kw):
        rec.argvs.append(list(argv))
        proc = _FakeProc(argv)
        if "litman.commands._splash" in argv:
            rec.splashes.append(proc)
        else:
            rec.window_procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(webbrowser, "open", lambda url: rec.opened.append(url))
    monkeypatch.setattr("uvicorn.Server", _FakeServer)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None)
    monkeypatch.setattr(gui, "_spawn_ready_watcher", _sync_ready_watcher)
    # We assert splash wiring, not the window watcher — stop it lingering.
    monkeypatch.setattr(gui, "_stop_server_when_window_closes", lambda *a, **k: None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    return rec


def test_want_splash_true_for_consoleless_window_with_display(
    monkeypatch, splash_gui, vault_with_paper
) -> None:
    # C1 truth table: litw --window + display → splash IS launched.
    vault, _pid = vault_with_paper
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: True)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert _splash_launched(splash_gui.argvs)
    assert len(splash_gui.splashes) == 1


def test_want_splash_false_in_a_terminal_window(
    monkeypatch, splash_gui, vault_with_paper
) -> None:
    # C1 truth table: terminal --window (has a tty) → NO splash.
    vault, _pid = vault_with_paper
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: False)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert not _splash_launched(splash_gui.argvs)


def test_want_splash_false_without_window(
    monkeypatch, splash_gui, vault_with_paper
) -> None:
    # C1 truth table: tab mode (no --window), even console-less → NO splash.
    vault, _pid = vault_with_paper
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: True)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert not _splash_launched(splash_gui.argvs)


def test_want_splash_false_headless_keeps_remote_capability(
    monkeypatch, splash_gui, vault_with_paper
) -> None:
    # C5 red line: no display → want_splash is False, NO splash Popen ever, and
    # the server still prints URL + tunnel line (remote capability intact).
    vault, _pid = vault_with_paper
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(gui, "_launched_without_console", lambda: True)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert not _splash_launched(splash_gui.argvs)
    assert "http://127.0.0.1:" in result.output
    assert "SSH tunnel" in result.output


def test_splash_popen_failure_degrades_silently(
    monkeypatch, vault_with_paper
) -> None:
    # C2 (positive + reverse): a splash Popen that raises (no tkinter / no
    # `python -m` in this env) must not surface — and, reverse-verified, the
    # startup must actually COMPLETE (server started, browser opened, exit 0),
    # not merely "no exception".
    vault, _pid = vault_with_paper
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: True)
    monkeypatch.setattr(gui, "_spawn_ready_watcher", _sync_ready_watcher)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # tab fallback
    _FakeServer.instances.clear()
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    def _popen(argv, **kw):
        if "litman.commands._splash" in argv:
            raise FileNotFoundError("splash cannot start here")
        return _FakeProc(argv)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr("uvicorn.Server", _FakeServer)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **k: None)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    (server,) = _FakeServer.instances
    assert server.ran  # reverse-verify: the server actually started
    assert opened == [_served_url(result.output)]  # ...and the browser opened


class _FakeSplash:
    def __init__(self) -> None:
        self.terminated = 0

    def terminate(self) -> None:
        self.terminated += 1


def test_terminate_splash_when_visible_closes_on_presence() -> None:
    # C3: splash stays up until a page paints (presence connects), then closes.
    splash = _FakeSplash()
    tracker = PresenceTracker()
    stop = threading.Event()
    thread = threading.Thread(
        target=_terminate_splash_when_visible,
        args=(splash, tracker, stop),
        kwargs={"splash_timeout": 5.0, "poll": 0.01},
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    assert splash.terminated == 0  # no page yet — hold the splash
    tracker.connect()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert splash.terminated == 1


def test_terminate_splash_when_visible_backstops_on_timeout() -> None:
    # C3: presence never arrives → the splash_timeout backstop still closes it.
    splash = _FakeSplash()
    _terminate_splash_when_visible(
        splash, PresenceTracker(), threading.Event(),
        splash_timeout=0.05, poll=0.01,
    )
    assert splash.terminated == 1


def test_terminate_splash_when_visible_backstops_on_stop_event() -> None:
    # C3: the server shutting down (stop set) closes the splash immediately.
    splash = _FakeSplash()
    stop = threading.Event()
    stop.set()
    _terminate_splash_when_visible(
        splash, PresenceTracker(), stop, splash_timeout=5.0, poll=0.01
    )
    assert splash.terminated == 1


def test_terminate_splash_when_visible_real_default_timeout() -> None:
    # C3 inject-seam real default: exercise the shipped SPLASH_TIMEOUT (25s)
    # end-to-end without waiting it out — presence is already connected, so it
    # terminates at once through the real default path.
    splash = _FakeSplash()
    tracker = PresenceTracker()
    tracker.connect()
    _terminate_splash_when_visible(splash, tracker, threading.Event())
    assert splash.terminated == 1


def test_terminate_splash_when_visible_suppresses_oserror() -> None:
    # C3: terminate is idempotent/suppressed — a splash already gone must not
    # crash the watcher.
    class _BadSplash:
        def terminate(self) -> None:
            raise OSError("already gone")

    tracker = PresenceTracker()
    tracker.connect()
    _terminate_splash_when_visible(_BadSplash(), tracker, threading.Event())


def test_splash_terminated_on_tab_fallback(
    monkeypatch, splash_gui, vault_with_paper
) -> None:
    # C3 (SF-5): --window with no Chromium falls back to a tab; there is no
    # window to paint, so the splash is closed at once.
    vault, _pid = vault_with_paper
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: True)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert len(splash_gui.splashes) == 1
    assert splash_gui.splashes[0].terminated


def test_window_success_hands_splash_to_presence_watcher(
    monkeypatch, splash_gui, vault_with_paper
) -> None:
    # C3: on a real app window, the splash close is handed to the presence
    # watcher (not closed immediately) — with the true tracker and splash proc.
    vault, _pid = vault_with_paper
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: True)
    monkeypatch.setattr(
        shutil, "which",
        lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None,
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        gui, "_terminate_splash_when_visible",
        lambda splash, presence, stop, **kw: calls.append((splash, presence)),
    )

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert len(splash_gui.window_procs) == 1  # a real window was spawned
    assert len(calls) == 1
    splash, presence = calls[0]
    assert splash is splash_gui.splashes[0]
    assert isinstance(presence, PresenceTracker)


def test_splash_terminated_in_finally(
    monkeypatch, splash_gui, vault_with_paper
) -> None:
    # C3: with the presence hand-off stubbed out, the ONLY thing left to close
    # the splash on a real app window is gui_cmd's finally backstop.
    vault, _pid = vault_with_paper
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: True)
    monkeypatch.setattr(
        shutil, "which",
        lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None,
    )
    monkeypatch.setattr(gui, "_terminate_splash_when_visible", lambda *a, **k: None)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert len(splash_gui.splashes) == 1
    assert splash_gui.splashes[0].terminated  # closed by the finally backstop
