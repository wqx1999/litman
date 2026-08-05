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
import os
import plistlib
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
from litman.commands import _mac_shell, gui
from litman.commands.gui import (
    _DEFAULT_PORT,
    _app_window_argv,
    _find_free_port,
    _migrate_legacy_http_cache,
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
from litman.core import locking


def _slashed(p: str) -> str:
    """``p`` with forward slashes, so a path assertion reads the same anywhere.

    The separator is not part of any contract these tests are about — the
    branch taken is. ``monkeypatch.setattr(sys, "platform", ...)`` moves the
    branch but not ``pathlib``, which always speaks the real host's flavour.
    """
    return str(p).replace(os.sep, "/")


def _host_abs(posix_path: str) -> str:
    """``posix_path`` made absolute on the *real* host, not the faked one.

    ``_resolve_lit_executable`` runs whatever ``shutil.which`` returns through
    ``Path(...).resolve()``, and that is real-OS pathlib no matter what
    ``sys.platform`` has been set to. On Windows a leading slash is
    drive-relative, so a ``/opt/...`` stub comes back as ``D:\\opt\\...`` and
    no literal can match it. Anchoring at ``C:`` keeps the round-trip exact.
    """
    if sys.platform == "win32":
        return str(Path("C:/" + posix_path.lstrip("/")).resolve())
    return posix_path


@pytest.fixture(autouse=True)
def _no_real_native_window(request, monkeypatch) -> None:
    """Keep a faked platform from opening a real macOS window.

    A test that sets ``sys.platform`` to "darwin" and runs ``gui_cmd --window``
    reaches the native shell for real when the host *is* a Mac: pywebview
    imports, a window opens over whatever the developer is doing, and the run
    blocks until someone closes it. On Linux the same test falls straight
    through to the browser, so the trap is invisible where the suite usually
    runs — and this repo has no CI to run it anywhere else. Neutralise the
    loader by default; a test that means to drive the shell patches
    ``load_webview`` itself, which lands after this and wins. The three tests
    whose subject *is* the loader carry ``real_webview_loader`` — without the
    opt-out this fixture would answer for them, and the two expecting ``None``
    would keep passing while proving nothing.
    """
    if "real_webview_loader" in request.keywords:
        return
    monkeypatch.setattr(_mac_shell, "load_webview", lambda: None)


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


def test_migrate_legacy_http_cache_removes_only_cache_and_marks_done(
    tmp_path,
) -> None:
    """An upgraded app profile loses the pre-no-store HTTP cache once only."""
    default = tmp_path / "Default"
    cache = default / "Cache" / "Cache_Data"
    cache.mkdir(parents=True)
    (cache / "cached-410").write_bytes(b"gone")
    (default / "Preferences").write_text("{}", encoding="utf-8")
    local_storage = default / "Local Storage"
    local_storage.mkdir()
    (local_storage / "state").write_bytes(b"keep")

    _migrate_legacy_http_cache(tmp_path)

    assert not (default / "Cache").exists()
    assert (tmp_path / ".api-no-store-v1").is_file()
    assert (default / "Preferences").is_file()
    assert (local_storage / "state").read_bytes() == b"keep"

    # The marker makes this a migration, not a cache wipe on every launch.
    replacement = default / "Cache"
    replacement.mkdir()
    (replacement / "new-cache").write_bytes(b"healthy")
    _migrate_legacy_http_cache(tmp_path)
    assert (replacement / "new-cache").read_bytes() == b"healthy"


def test_gui_window_migrates_legacy_http_cache_before_launch(
    gui_harness, chromium_on_path, vault_with_paper
) -> None:
    vault, _pid = vault_with_paper
    cache = browser_profile_dir() / "Default" / "Cache"
    cache.mkdir(parents=True)
    (cache / "cached-410").write_bytes(b"gone")

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert not cache.exists()
    assert (browser_profile_dir() / ".api-no-store-v1").is_file()


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


def _crashed_proc() -> subprocess.Popen[bytes]:
    """A real process that exited non-zero — the snap-confinement shape: the
    browser started, could not lock the profile it was handed, and aborted
    rather than corrupt it. It differs from _exited_proc only in its status,
    which is the whole point: that is what tells a failed launch from a
    browser that exited having done its job."""
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
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


def test_watcher_opens_a_tab_when_the_window_dies_before_any_page() -> None:
    # Popen said the browser started; the browser disagreed a moment later —
    # an Ubuntu snap Chromium refused the hidden --user-data-dir and aborted.
    # Before this, the grace above simply stopped the server, so *installing*
    # Chromium left the user worse off than owning no Chromium at all: the
    # tab fallback only ever ran for a browser that failed to start. The
    # server must stay up long enough for the tab to arrive and take over.
    tracker, server = PresenceTracker(), _FakeServer()
    calls: list[int] = []
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(_crashed_proc(), server, tracker),
        kwargs={
            "on_launch_failed": lambda: (calls.append(1), True)[1],
            # Long, so a fallback that fired on this clock would time the
            # test out: a crash is judged on launch_failure_grace.
            "first_connect_grace": 30.0,
            "launch_failure_grace": 0.2,
            "linger": 0.2,
            "poll": 0.02,
        },
        daemon=True,
    )
    watcher.start()
    time.sleep(0.6)  # past the grace that used to be a death sentence
    assert calls == [1] and server.should_exit is False
    tracker.connect()  # the tab loads the SPA
    time.sleep(0.3)
    assert server.should_exit is False  # and now holds the server on its own
    tracker.disconnect()  # the user closes the tab: normal shutdown resumes
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert server.should_exit is True


def test_watcher_gives_up_when_the_tab_fallback_also_fails() -> None:
    # webbrowser.open returns False on a box with no browser it can drive.
    # Nothing is coming, so the original verdict stands — a failed launch may
    # not leave an orphaned server behind just because we tried twice.
    server = _FakeServer()
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(_crashed_proc(), server, PresenceTracker()),
        kwargs={
            "on_launch_failed": lambda: False,
            # Long, so a fallback that fired on this clock would time the
            # test out: a crash is judged on launch_failure_grace.
            "first_connect_grace": 30.0,
            "launch_failure_grace": 0.2,
            "linger": 0.1,
            "poll": 0.02,
        },
        daemon=True,
    )
    watcher.start()
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert server.should_exit is True


def test_watcher_offers_the_tab_fallback_once_and_then_gives_up() -> None:
    # The tab opened and its page never connected either (the default browser
    # is a text-mode one, or the user closed it before it loaded). One more
    # grace, then stop — the fallback is an extra chance, not a loop that
    # keeps a dead launch alive by re-offering itself forever.
    server = _FakeServer()
    calls: list[int] = []
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(_crashed_proc(), server, PresenceTracker()),
        kwargs={
            "on_launch_failed": lambda: (calls.append(1), True)[1],
            # Long, so a fallback that fired on this clock would time the
            # test out: a crash is judged on launch_failure_grace.
            "first_connect_grace": 30.0,
            "launch_failure_grace": 0.2,
            "never_connected_timeout": 1.0,
            "linger": 0.1,
            "poll": 0.02,
        },
        daemon=True,
    )
    watcher.start()
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert calls == [1]  # offered once, not once per poll
    assert server.should_exit is True


def test_watcher_never_offers_the_tab_after_a_clean_exit() -> None:
    # The other half of the exit-status rule, and what keeps the fallback from
    # becoming a nuisance: a browser that exits 0 with no page ever connected
    # is the long-standing hand-off shape (Chromium passed the URL to an
    # instance we cannot see) or a user who shut the window before it painted.
    # Neither wants a tab opened at them, so the original verdict stands.
    server = _FakeServer()
    calls: list[int] = []
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(_exited_proc(), server, PresenceTracker()),
        kwargs={
            "on_launch_failed": lambda: (calls.append(1), True)[1],
            "first_connect_grace": 0.2,
            # Long, and never reached: a clean exit is not a crash.
            "launch_failure_grace": 30.0,
            "linger": 0.1,
            "poll": 0.02,
        },
        daemon=True,
    )
    watcher.start()
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert calls == []  # never offered
    assert server.should_exit is True


def test_watcher_never_offers_the_tab_while_the_window_is_alive() -> None:
    # The guard on the whole mechanism: a window merely slow to paint must not
    # have a second browser opened on top of it. Only an exited process can
    # have failed this way.
    proc, server = _live_proc(), _FakeServer()
    calls: list[int] = []
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(proc, server, PresenceTracker()),
        kwargs={
            "on_launch_failed": lambda: (calls.append(1), True)[1],
            "first_connect_grace": 0.05,
            "linger": 0.1,
            "poll": 0.02,
        },
        daemon=True,
    )
    try:
        watcher.start()
        time.sleep(0.4)  # many graces' worth, with the process still up
        assert calls == [] and server.should_exit is False
    finally:
        proc.terminate()
        proc.wait()
        watcher.join(timeout=5)


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


def test_watcher_gives_up_when_a_live_window_never_loads_a_page() -> None:
    # The two Windows facts meet: Edge keeps our spawned process resident
    # forever, and no page ever connects (the window came up on an error page,
    # a stale cached response, anything). The first-connect grace waits on the
    # process, so on its own it never fires — and the server outlives every
    # window there is. The outer bound is what closes that.
    proc, server = _live_proc(), _FakeServer()
    try:
        watcher = threading.Thread(
            target=_stop_server_when_window_closes,
            args=(proc, server, PresenceTracker()),
            kwargs={
                "first_connect_grace": 30.0,
                "never_connected_timeout": 0.3,
                "linger": 0.1,
                "poll": 0.02,
            },
            daemon=True,
        )
        watcher.start()
        watcher.join(timeout=5)
        assert not watcher.is_alive()
        assert server.should_exit is True
    finally:
        proc.terminate()
        proc.wait()


def test_watcher_runs_with_no_process_to_poll() -> None:
    # The --window fallback: no Chromium found, so the URL opened as a tab in
    # the user's everyday browser and there is no process of ours to watch.
    # The page is then the only signal — and it has to be enough, because a
    # shortcut launch has no console to Ctrl+C from.
    tracker = PresenceTracker()
    tracker.connect()
    server = _FakeServer()
    watcher = threading.Thread(
        target=_stop_server_when_window_closes,
        args=(None, server, tracker),
        kwargs={"first_connect_grace": 30.0, "linger": 0.1, "poll": 0.02},
        daemon=True,
    )
    watcher.start()
    time.sleep(0.25)
    assert server.should_exit is False  # the tab is open; nothing to stop
    tracker.disconnect()
    watcher.join(timeout=5)
    assert not watcher.is_alive()
    assert server.should_exit is True


# ---------------------------------------------------------------------------
# shutdown escalation — asking uvicorn to stop is not the same as stopping
# ---------------------------------------------------------------------------


def test_escalate_stands_down_the_moment_the_server_returns() -> None:
    # The normal path: server.run() returns in well under a second, gui_cmd's
    # `finally` sets the event, and neither of the louder stages ever happens.
    server, stopped = _FakeServer(), threading.Event()
    killed: list[bool] = []
    stopped.set()

    gui._escalate_shutdown(
        server,
        stopped,
        force_after=5.0,
        hard_after=10.0,
        hard_exit=lambda: killed.append(True),
    )

    assert not hasattr(server, "force_exit")
    assert killed == []


def test_escalate_forces_a_server_that_ignores_the_request() -> None:
    # One connection uvicorn cannot finish draining. `force_exit` is its own
    # escape hatch, normally reached only by a second Ctrl+C — which is
    # precisely what a console-less litw.exe launch cannot send.
    server, stopped = _FakeServer(), threading.Event()
    killed: list[bool] = []

    thread = threading.Thread(
        target=gui._escalate_shutdown,
        args=(server, stopped),
        kwargs={
            "force_after": 0.1,
            "hard_after": 10.0,
            "hard_exit": lambda: killed.append(True),
        },
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)
    assert server.force_exit is True  # insisted
    assert killed == []  # but still waiting, not killing
    stopped.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert killed == []


def test_escalate_kills_a_server_that_ignores_force_too() -> None:
    # The end of the line. A GUI process with no window and no console is
    # unreachable by any means the user has, and on Windows it also blocks its
    # own next `uv tool install --force` — so leaving is better than staying.
    server, stopped = _FakeServer(), threading.Event()
    killed: list[bool] = []

    gui._escalate_shutdown(
        server,
        stopped,
        force_after=0.05,
        hard_after=0.1,
        hard_exit=lambda: killed.append(True),
    )

    assert server.force_exit is True
    assert killed == [True]


def test_window_fallback_to_a_tab_still_watches_the_page(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    # --window on a box with no Chrome/Edge/Chromium at all. It degrades to a
    # tab, and used to degrade the shutdown contract with it: no process, so
    # no watcher, so nothing could ever stop the server — and the launch this
    # matters for (the desktop shortcut) has no Ctrl+C either.
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    watched: list[object] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(gui, "_app_window_argv", lambda url: None)
    monkeypatch.setattr(
        gui,
        "_stop_server_when_window_closes",
        lambda proc, *a, on_launch_failed=None, **k: watched.append(
            (proc, on_launch_failed)
        ),
    )

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert opened == [_served_url(result.output)] and procs == []
    # The page gate ran with nothing to poll — and no tab fallback armed:
    # this launch *is* the tab, so there is nothing left to fall back to.
    assert watched == [(None, None)]


def test_window_launch_arms_the_tab_fallback(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    # The seam that makes a snap Chromium's abort survivable: a launch that
    # spawned a window hands the watcher a way back to a tab, aimed at the
    # same URL the window was given. Without the callback the watcher can
    # only stop the server, which is how installing a browser came to break
    # the desktop shortcut on Ubuntu.
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    armed: list[object] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        gui, "_app_window_argv", lambda url: ["chromium", f"--app={url}"]
    )
    monkeypatch.setattr(
        gui,
        "_stop_server_when_window_closes",
        lambda proc, *a, on_launch_failed=None, **k: armed.append(on_launch_failed),
    )

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert len(procs) == 1 and opened == []  # a window went up, not a tab
    assert len(armed) == 1 and armed[0] is not None
    # The command has returned, so its `finally` has set stop_event: the
    # window's non-zero exit is our own terminate, not a crash, and a tab
    # thrown up as the run ends is worse than no tab at all.
    assert armed[0]() is False and opened == []


def test_plain_tab_mode_keeps_the_ctrl_c_contract(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    # The counterpart: a terminal `lit gui` (no --window) must NOT acquire a
    # page-presence gate. What the terminal started, the terminal stops —
    # closing one tab has never been a reason to take the server with it.
    opened, _procs = gui_harness
    vault, _pid = vault_with_paper
    watched: list[object] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        gui,
        "_stop_server_when_window_closes",
        lambda proc, *a, **k: watched.append(proc),
    )

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert opened == [_served_url(result.output)]
    assert watched == []


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
    assert remove_browser_profile() == []


def test_remove_browser_profile_deletes_it() -> None:
    profile = browser_profile_dir()
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text("{}", encoding="utf-8")

    assert remove_browser_profile() == [profile]
    assert not profile.exists()


# ---------------------------------------------------------------------------
# snap-confined browsers: the profile has to live where they can write
# ---------------------------------------------------------------------------


def _fake_snap(monkeypatch, tmp_path, name="chromium"):
    """A machine with snap `name` installed, entirely inside tmp_path.

    The per-user snap area follows $LITMAN_REGISTRY_DIR, which the autouse
    isolation fixture already points at tmp_path — so nothing here can reach
    a developer's real ~/snap, which matters because the uninstall sweep
    deletes what it finds.
    """
    root = Path(os.environ["LITMAN_REGISTRY_DIR"]).expanduser() / "snap"
    (root / name / "common").mkdir(parents=True)
    monkeypatch.setattr(gui, "_SNAP_BIN", tmp_path / "snapbin")
    (tmp_path / "snapbin").mkdir()
    monkeypatch.setattr(gui, "_SNAP_ROOT", tmp_path / "snaproot")
    (tmp_path / "snaproot" / name).mkdir(parents=True)
    return root


def test_snap_sweep_cannot_reach_a_real_home(monkeypatch, tmp_path):
    # Reverse check on a rule that *deletes* directories. The sweep root
    # follows $LITMAN_REGISTRY_DIR, so neither this suite nor a sandboxed run
    # can walk into a developer's actual ~/snap. Plant a decoy there and prove
    # nothing so much as looks at it.
    decoy = tmp_path / "real-home"
    planted = decoy / "snap" / "chromium" / "common" / "litman-browser-profile"
    planted.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: decoy))

    assert gui._snap_user_root() != decoy / "snap"
    assert gui.browser_profile_dirs() == []
    assert remove_browser_profile() == []
    assert planted.is_dir()


def test_snap_sweep_uses_the_real_home_when_nothing_overrides_it(monkeypatch):
    # And the seam's real default, which the test above can never exercise:
    # unsandboxed, ~/snap is exactly where snapd puts these.
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    assert gui._snap_user_root() == Path.home() / "snap"


def test_snap_name_reads_the_snap_from_a_wrapper_path(monkeypatch, tmp_path):
    # /snap/bin/chromium resolves to snapd itself, so the name is all there is
    # to go on — confirmed against the /snap/<name> directory the snap owns.
    monkeypatch.setattr(gui, "_SNAP_BIN", tmp_path / "bin")
    monkeypatch.setattr(gui, "_SNAP_ROOT", tmp_path / "root")
    (tmp_path / "root" / "chromium").mkdir(parents=True)

    assert gui._snap_name(tmp_path / "bin" / "chromium") == "chromium"
    # Aliased as <snap>.<app>: the snap is the part before the dot.
    assert gui._snap_name(tmp_path / "bin" / "chromium.foo") == "chromium"
    # Not installed as a snap of that name — do not invent a path for it.
    assert gui._snap_name(tmp_path / "bin" / "brave") is None
    # An ordinary browser off PATH is not a snap at all.
    assert gui._snap_name("/usr/bin/google-chrome") is None


def test_browser_profile_dir_moves_out_of_the_hidden_cache_for_a_snap(
    monkeypatch, tmp_path
):
    # The bug: snapd's `home` interface does not reach hidden directories, so
    # the ~/.cache default made Ubuntu's snap Chromium abort on its
    # SingletonLock rather than run a profile it could not lock. ~/snap/<snap>
    # /common is the writable area it is granted.
    root = _fake_snap(monkeypatch, tmp_path)
    exe = tmp_path / "snapbin" / "chromium"

    profile = browser_profile_dir(exe)

    assert profile == root / "chromium" / "common" / "litman-browser-profile"
    assert ".cache" not in str(profile)  # the hidden dir snapd will not grant


def test_browser_profile_dir_is_unchanged_for_an_ordinary_browser(monkeypatch):
    # The guard on the whole mechanism: only a snap gets moved. Windows,
    # macOS and every deb/rpm Chromium keep the cache dir they have always had.
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    assert browser_profile_dir("/usr/bin/google-chrome") == browser_profile_dir()


def test_app_window_argv_hands_a_snap_the_profile_it_can_write(monkeypatch, tmp_path):
    # End to end through the real flag builder: the --user-data-dir on the
    # command line is the moved one, and it is the browser we found that
    # decides so.
    root = _fake_snap(monkeypatch, tmp_path)
    exe = tmp_path / "snapbin" / "chromium"
    exe.write_text("")
    monkeypatch.setattr(gui, "_find_chromium", lambda: str(exe))

    argv = gui._app_window_argv("http://127.0.0.1:8765")

    assert argv is not None and argv[0] == str(exe)
    want = root / "chromium" / "common" / "litman-browser-profile"
    assert f"--user-data-dir={want}" in argv


def test_uninstall_sweep_finds_the_snap_profile_too(monkeypatch, tmp_path):
    # `lit uninstall` promises the profile does not outlive the install, and
    # it must keep that promise for a profile it moved. The sweep goes by
    # directory name, because by uninstall time the snap may be gone.
    root = _fake_snap(monkeypatch, tmp_path)
    snap_profile = root / "chromium" / "common" / "litman-browser-profile"
    snap_profile.mkdir(parents=True)
    default = browser_profile_dir()
    default.mkdir(parents=True, exist_ok=True)
    # A neighbour in the same snap that is emphatically not ours.
    bystander = root / "chromium" / "common" / "chromium"
    bystander.mkdir()

    assert set(gui.browser_profile_dirs()) == {default, snap_profile}
    assert set(remove_browser_profile()) == {default, snap_profile}
    assert bystander.is_dir()  # untouched


def test_app_window_argv_darwin_runs_the_bundle_binary(monkeypatch, tmp_path):
    # `open -na` asks Launch Services to start the app and returns immediately,
    # so it can never own the window. Run the binary inside the bundle instead.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Every darwin browser-lookup test redirects _DARWIN_SYSTEM_APPS as well:
    # HOME alone leaves the system folder real, so on a Mac host the answer
    # comes from whatever browsers that machine happens to have installed.
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", tmp_path / "no-system-apps")
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


def test_app_window_argv_darwin_finds_every_bundle_it_lists(monkeypatch, tmp_path):
    # macOS puts no browser binary on PATH, so _DARWIN_APP_CANDIDATES *is* the
    # lookup — a name that fails to resolve silently demotes an installed
    # browser to a plain tab. Each name goes through the real resolver rather
    # than being asserted against the tuple, which would only restate the source.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", tmp_path / "no-system-apps")
    apps = tmp_path / "Applications"

    # The list is a capability promise, so pin it: a name quietly dropped here
    # is a browser that stops working with nothing else noticing.
    assert set(gui._DARWIN_APP_CANDIDATES) == {
        "Google Chrome",
        "Microsoft Edge",
        "Chromium",
        "Brave Browser",
    }

    for app in gui._DARWIN_APP_CANDIDATES:
        binary = apps / f"{app}.app" / "Contents" / "MacOS" / app
        binary.parent.mkdir(parents=True)
        binary.touch()

        argv = _app_window_argv("http://127.0.0.1:8765")

        assert argv is not None, f"{app} is installed but yielded no app window"
        assert argv[0] == str(binary)
        locking.rmtree(apps)


def test_app_window_argv_darwin_prefers_chrome_to_the_forks(monkeypatch, tmp_path):
    # Tuple order is preference order. A machine carrying both Chrome and a fork
    # must get Chrome — the forks are the fallback, not a coin flip on whatever
    # the filesystem hands back first.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", tmp_path / "no-system-apps")
    for app in ("Brave Browser", "Chromium", "Google Chrome"):
        binary = tmp_path / "Applications" / f"{app}.app" / "Contents" / "MacOS" / app
        binary.parent.mkdir(parents=True)
        binary.touch()

    argv = _app_window_argv("http://127.0.0.1:8765")

    assert argv is not None
    assert _slashed(argv[0]).endswith(
        "Google Chrome.app/Contents/MacOS/Google Chrome"
    )


def test_app_window_argv_darwin_searches_the_system_folder_first(
    monkeypatch, tmp_path
):
    # /Applications is where a Mac browser actually lands, and it is searched
    # ahead of ~/Applications. Every other test here seeds only HOME, so all of
    # them would stay green if the system folder fell out of the search.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", system_apps)
    tail = Path("Google Chrome.app") / "Contents" / "MacOS" / "Google Chrome"
    for root in (system_apps, home / "Applications"):
        (root / tail).parent.mkdir(parents=True)
        (root / tail).touch()

    argv = _app_window_argv("http://127.0.0.1:8765")

    assert argv is not None
    assert argv[0] == str(system_apps / tail)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "POSIX-only premise, and unfakeable here: refresh_path's non-win32 arm "
        "joins PATH with ':', which is also the drive separator — a real "
        "Windows bin dir cannot survive that round-trip. Windows reads its "
        "PATH from the registry instead (refresh_path's other arm)."
    ),
)
def test_app_window_argv_reaches_a_browser_the_session_path_omits(
    monkeypatch, tmp_path
):
    # A shortcut-launched litman inherits the desktop session's PATH, which has
    # neither ~/.local/bin nor a Homebrew prefix. A browser installed there must
    # still yield an app window rather than degrading to a plain tab.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    chromium = local_bin / "chromium"
    chromium.touch()

    def fake_which(name):
        candidate = local_bin / name
        on_path = str(local_bin) in os.environ["PATH"].split(":")
        return str(candidate) if on_path and candidate.exists() else None

    monkeypatch.setattr(shutil, "which", fake_which)

    argv = _app_window_argv("http://127.0.0.1:8765")

    assert argv is not None
    assert argv[0] == str(chromium)


# ---------------------------------------------------------------------------
# --make-shortcut (task-gui-desktop-entry D3)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_lit_on_path(monkeypatch):
    """Pin `lit` resolution to a fixed path (with a space, to prove quoting)
    so shortcut content is deterministic regardless of the test host PATH."""
    fake = _host_abs("/opt/lit tools/bin/lit")
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


def test_app_window_flags_carry_the_wm_class_only_on_linux(monkeypatch) -> None:
    # X11 matches the window's WM_CLASS against StartupWMClass in
    # litman.desktop; Windows groups by AppUserModelID and macOS by owning
    # bundle, so --class would be noise there at best.
    url = "http://127.0.0.1:8765"
    monkeypatch.setattr(sys, "platform", "linux")
    assert f"--class={gui._LINUX_WM_CLASS}" in gui._app_window_flags(
        url, "/usr/bin/chromium"
    )
    for platform in ("win32", "darwin"):
        monkeypatch.setattr(sys, "platform", platform)
        flags = gui._app_window_flags(url, "/usr/bin/chromium")
        assert not [f for f in flags if f.startswith("--class")]


def test_app_window_flags_force_x11_only_when_xwayland_is_reachable(
    monkeypatch,
) -> None:
    # Native-Wayland Chromium ignores --class, so the WM_CLASS fix only
    # works through XWayland; without $DISPLAY forcing x11 would trade a
    # wrongly-badged window for none at all.
    url = "http://127.0.0.1:8765"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    assert "--ozone-platform=x11" in gui._app_window_flags(url, "/usr/bin/chromium")

    monkeypatch.delenv("DISPLAY")
    assert "--ozone-platform=x11" not in gui._app_window_flags(
        url, "/usr/bin/chromium"
    )

    monkeypatch.setenv("DISPLAY", ":0")
    for platform in ("win32", "darwin"):
        monkeypatch.setattr(sys, "platform", platform)
        flags = gui._app_window_flags(url, "/usr/bin/chromium")
        assert not [f for f in flags if f.startswith("--ozone-platform")]


def test_make_shortcut_linux_wm_class_matches_the_window_flag(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # One test through both production chains: the .desktop's StartupWMClass
    # and the window's --class must be the same string or the taskbar match
    # silently breaks — only --class regroups the window under a name no
    # .desktop claims (falling back to the 16px tab favicon), only
    # StartupWMClass never matches the browser's own class. Either half
    # alone is worse than neither.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output
    content = (tmp_path / "share" / "applications" / "litman.desktop").read_text(
        encoding="utf-8"
    )
    wm_class = [
        line.removeprefix("StartupWMClass=")
        for line in content.splitlines()
        if line.startswith("StartupWMClass=")
    ]

    flags = gui._app_window_flags("http://127.0.0.1:8765", "/usr/bin/chromium")
    class_values = [
        f.removeprefix("--class=") for f in flags if f.startswith("--class=")
    ]

    assert wm_class == class_values == ["litman"]


# ---------------------------------------------------------------------------
# win32 taskbar branding (task-launcher-identity Part D)
# ---------------------------------------------------------------------------


def test_gui_window_starts_the_taskbar_brander(
    gui_harness, chromium_on_path, vault_with_paper, monkeypatch
) -> None:
    # The relaunch properties die with the window, so every --window launch
    # that spawned a browser must arm the brander (a no-op off win32) and
    # hand it the stop event that ends its polling on shutdown.
    handed: list[threading.Event] = []
    monkeypatch.setattr(
        gui, "_brand_windows_taskbar", lambda ev: handed.append(ev)
    )
    vault, _pid = vault_with_paper

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert len(handed) == 1
    assert isinstance(handed[0], threading.Event)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the off-win32 arm; on win32 branding is the real behaviour",
)
def test_brand_windows_taskbar_is_a_no_op_off_win32(monkeypatch) -> None:
    from litman.commands import _win_taskbar

    calls: list[object] = []
    monkeypatch.setattr(_win_taskbar, "adopt_window", lambda *a, **k: calls.append(a))

    assert gui._brand_windows_taskbar(threading.Event()) is None
    assert calls == []


def test_brand_windows_taskbar_hands_the_worker_its_pieces(monkeypatch) -> None:
    from litman.commands import _win_taskbar

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        _win_taskbar,
        "adopt_window",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(gui, "_shortcut_executable", lambda: r"C:\tools\litw.exe")
    stop = threading.Event()

    thread = gui._brand_windows_taskbar(stop)
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    (args, kwargs), = calls
    icon, relaunch = args
    assert icon.endswith("litman.ico")
    # The same command the pinned button will run: pinning a branded group
    # snapshots RelaunchCommand, so it must bring litman back, not the
    # browser.
    assert relaunch == r"C:\tools\litw.exe gui --window"
    # A bound method compares by behavior, not identity: the worker's
    # give_up must track this launch's stop event.
    assert kwargs["give_up"]() is False
    stop.set()
    assert kwargs["give_up"]() is True


def test_brand_windows_taskbar_contains_a_failing_worker(monkeypatch) -> None:
    # Cosmetic best-effort: a raise anywhere in the worker dies quietly in
    # its own thread and takes nothing of the launch with it.
    from litman.commands import _win_taskbar

    def boom(*a, **k):
        raise RuntimeError("no shell here")

    monkeypatch.setattr(_win_taskbar, "adopt_window", boom)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(gui, "_shortcut_executable", lambda: "litw")

    thread = gui._brand_windows_taskbar(threading.Event())
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


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
    # Every darwin shortcut test redirects _DARWIN_SYSTEM_APPS: unpatched, the
    # code would probe — or on a Mac host, write into — the real /Applications.
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", tmp_path / "no-system-apps")

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    app = tmp_path / "Applications" / "litman.app"
    stub = app / "Contents" / "MacOS" / "litman"
    assert stub.is_file()
    if sys.platform != "win32":
        # NTFS has no exec bit — chmod cannot set it and stat never reports
        # it — so this half is asserted on the arm that can hold it. Every
        # other assertion in this test still runs on Windows.
        assert stub.stat().st_mode & 0o111, "launcher stub must be executable"
    stub_text = stub.read_text(encoding="utf-8")
    # Marked and exec'd: the env prefix tells the launched process it wears
    # the bundle's identity; exec is what hands that identity over.
    assert (
        f'LITMAN_DARWIN_APP_LAUNCH=1 exec "{fake_lit_on_path}" gui --window'
        in stub_text
    )
    # Every exec line carries the marker — the no-log fallback line included.
    assert (
        stub_text.count("exec ")
        == stub_text.count("LITMAN_DARWIN_APP_LAUNCH=1 exec ")
        == 2
    )
    plist = (app / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert "CFBundleExecutable" in plist


def test_make_shortcut_darwin_bundle_carries_the_icon(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # Without both halves — the .icns inside Resources AND the plist key
    # naming it — the Dock and Launchpad draw the generic executable tile,
    # which is what earlier installs got.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", tmp_path / "no-system-apps")

    assert CliRunner().invoke(gui_cmd, ["--make-shortcut"]).exit_code == 0

    app = tmp_path / "Applications" / "litman.app"
    icns = app / "Contents" / "Resources" / "litman.icns"
    assert icns.is_file()
    assert icns.read_bytes()[:4] == b"icns", "must be a real icon file"
    plist = (app / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert "<key>CFBundleIconFile</key><string>litman.icns</string>" in plist


def test_make_shortcut_darwin_survives_a_missing_icon_asset(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # An install whose package data lost the .icns still deserves a working
    # launcher: the key drops out rather than the shortcut. Asserting the key
    # is *absent* matters as much as the file — a plist naming an icon that
    # is not there is a bundle macOS reports as broken.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", tmp_path / "no-system-apps")
    monkeypatch.setattr(
        "litman.commands.gui._icon_path", lambda name: tmp_path / "gone" / name
    )

    assert CliRunner().invoke(gui_cmd, ["--make-shortcut"]).exit_code == 0

    app = tmp_path / "Applications" / "litman.app"
    assert (app / "Contents" / "MacOS" / "litman").is_file()
    assert not (app / "Contents" / "Resources" / "litman.icns").exists()
    plist = (app / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert "CFBundleIconFile" not in plist


def test_make_shortcut_darwin_prefers_system_applications(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # Finder's sidebar "Applications" is hardwired to /Applications; a bundle
    # in ~/Applications is invisible from the one place users look. When the
    # system folder takes writes the bundle belongs there — and the Finder-drag
    # hint must not print, or it would be shouting on every run.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    system_apps.mkdir()
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", system_apps)

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    assert (system_apps / "litman.app" / "Contents" / "MacOS" / "litman").is_file()
    assert not (tmp_path / "Applications" / "litman.app").exists()
    assert "Finder" not in result.output


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permission bits",
)
@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the unwritable dir is built with mkdir(mode=0o500); Windows ignores "
        "the mode, and the CI account is admin, so the write succeeds and "
        "there is no rejection left to fall back from"
    ),
)
def test_make_shortcut_darwin_falls_back_when_system_apps_reject_writes(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # Writability is probed by writing, not os.access: non-admin accounts, MDM
    # and TCC each veto in their own way. The fallback names the Finder drag as
    # the way over — Finder has an escalation channel, a CLI must not — and
    # still exits 0: a shortcut in ~/Applications is an install, not an error.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    system_apps.mkdir(mode=0o500)
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", system_apps)

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    stub = tmp_path / "Applications" / "litman.app" / "Contents" / "MacOS" / "litman"
    assert stub.is_file()
    assert not (system_apps / "litman.app").exists()
    assert "no write access" in result.output
    assert "Finder" in result.output


def test_make_shortcut_darwin_migrates_the_user_home_bundle(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # One copy, ever: a bundle left behind in ~/Applications is how a stale
    # stub outlives an upgrade — --make-shortcut refreshes the preferred home
    # while yesterday's copy keeps launching yesterday's code.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", system_apps)
    assert CliRunner().invoke(gui_cmd, ["--make-shortcut"]).exit_code == 0
    user_bundle = tmp_path / "Applications" / "litman.app"
    assert user_bundle.is_dir()

    system_apps.mkdir()  # today the system home takes writes
    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    assert (system_apps / "litman.app" / "Contents" / "MacOS" / "litman").is_file()
    assert not user_bundle.exists()
    # The (path, existed) contract sees through the move: an install was
    # already present, so this is an update, not a first run.
    assert "updated" in result.output


def test_make_shortcut_darwin_leaves_a_foreign_bundle_alone(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # /Applications is shared territory: a litman.app whose Info.plist is not
    # ours is never rewritten, migrated or deleted — the user home takes over,
    # without the drag hint (dragging onto a stranger's bundle is no way out).
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", system_apps)
    foreign_plist = system_apps / "litman.app" / "Contents" / "Info.plist"
    foreign_plist.parent.mkdir(parents=True)
    foreign_plist.write_text("<string>com.example.imposter</string>", encoding="utf-8")

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    stub = tmp_path / "Applications" / "litman.app" / "Contents" / "MacOS" / "litman"
    assert stub.is_file()
    assert foreign_plist.read_text(encoding="utf-8") == (
        "<string>com.example.imposter</string>"
    )
    assert not (system_apps / "litman.app" / "Contents" / "MacOS").exists()
    assert "Finder" not in result.output


def _seed_binary_plist_bundle(system_apps: Path) -> Path:
    """A foreign litman.app whose Info.plist is binary — Xcode's default, so
    the realistic foreign bundle. Returns the plist path."""
    foreign_plist = system_apps / "litman.app" / "Contents" / "Info.plist"
    foreign_plist.parent.mkdir(parents=True)
    with foreign_plist.open("wb") as fh:
        plistlib.dump(
            {"CFBundleIdentifier": "com.example.imposter"},
            fh,
            fmt=plistlib.FMT_BINARY,
        )
    # The fixture must reproduce the crash input: a binary plist a text read
    # dies on (UnicodeDecodeError is a ValueError, which no OSError guard
    # catches), or this test proves nothing.
    with pytest.raises(UnicodeDecodeError):
        foreign_plist.read_text(encoding="utf-8")
    return foreign_plist


def test_make_shortcut_darwin_survives_a_binary_plist_foreign_bundle(
    monkeypatch, tmp_path, fake_lit_on_path
) -> None:
    # Same verdict as the text-plist imposter above — not ours, not touched,
    # user home takes over — but the probe must reach it without crashing on
    # bytes it cannot decode.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", system_apps)
    foreign_plist = _seed_binary_plist_bundle(system_apps)
    original = foreign_plist.read_bytes()

    result = CliRunner().invoke(gui_cmd, ["--make-shortcut"])
    assert result.exit_code == 0, result.output

    stub = tmp_path / "Applications" / "litman.app" / "Contents" / "MacOS" / "litman"
    assert stub.is_file()
    assert foreign_plist.read_bytes() == original
    assert not (system_apps / "litman.app" / "Contents" / "MacOS").exists()


def test_remove_shortcut_darwin_spares_a_binary_plist_foreign_bundle(
    monkeypatch, tmp_path
) -> None:
    # Uninstall sweeps both bundle homes; the binary-plist stranger must
    # neither crash the sweep nor be taken down by it.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", system_apps)
    foreign_plist = _seed_binary_plist_bundle(system_apps)

    assert gui.remove_shortcut() is None
    assert foreign_plist.exists()


def test_shortcut_path_darwin_never_answers_with_a_foreign_bundle(
    monkeypatch, tmp_path
) -> None:
    # The occupied-slot fallback: /Applications takes writes but a stranger's
    # litman.app sits on the path. Answering with it would put that app in
    # uninstall's removal preview and make setup call the step already done —
    # the user home is the only honest answer (where _create_shortcut_darwin
    # would install too).
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", system_apps)
    foreign_plist = system_apps / "litman.app" / "Contents" / "Info.plist"
    foreign_plist.parent.mkdir(parents=True)
    foreign_plist.write_text("<string>com.example.imposter</string>", encoding="utf-8")

    assert shortcut_path() == tmp_path / "Applications" / "litman.app"


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX /bin/sh")
def test_darwin_stub_logs_the_launch_and_runs_anyway_without_a_log(
    monkeypatch, tmp_path
) -> None:
    # The stub is the one piece of this bundle no test host can be macOS for,
    # but it is plain POSIX sh — so run it. Finder gives it no console, so the
    # log is the only evidence a failed launch ever leaves; and a log that
    # cannot be opened must cost the diagnostic, never the launch itself.
    lit = tmp_path / "fake lit"  # space: the quoting is what is under test
    # The mark line proves the assignment prefix really rides into the
    # exec'd program's environment — on both stub branches: the logged one
    # here and the no-log fallback below.
    lit.write_text(
        "#!/bin/sh\n"
        'echo "argv: $*"\n'
        'echo "mark: ${LITMAN_DARWIN_APP_LAUNCH:-unset}"\n'
        "echo oops >&2\n",
        encoding="utf-8",
    )
    lit.chmod(0o755)
    stub = tmp_path / "stub.sh"
    stub.write_text(
        gui._DARWIN_STUB.format(lit=lit, log_dir=gui._DARWIN_LOG_DIR),
        encoding="utf-8",
    )

    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run(
        ["/bin/sh", str(stub)],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0
    assert done.stdout == "", "a logged launch must print nothing to the console"
    log = home / "Library" / "Logs" / "litman" / "litman.log"
    assert log.read_text(encoding="utf-8") == "argv: gui --window\nmark: 1\noops\n"

    # Second run truncates: the log records the last launch, it does not grow.
    subprocess.run(
        ["/bin/sh", str(stub)],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )
    assert log.read_text(encoding="utf-8").count("argv:") == 1

    # A home the log cannot be created under: same launch (marker included),
    # output falls back to the console instead of taking the app down with it.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    done = subprocess.run(
        ["/bin/sh", str(stub)],
        env={**os.environ, "HOME": str(blocked)},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0
    assert done.stdout == "argv: gui --window\nmark: 1\n"


# ---------------------------------------------------------------------------
# macOS native shell (task-mac-native-shell): darwin --window owns its window
# ---------------------------------------------------------------------------


class _ShellEvent:
    """pywebview's ``window.events.closed += handler`` shape."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler) -> _ShellEvent:
        self.handlers.append(handler)
        return self


class _ShellWindow:
    def __init__(self, title: str, html: str | None, **kw) -> None:
        self.title = title
        self.created_html = html
        self.kw = kw
        self.ops: list[tuple[str, str]] = []
        self.events = SimpleNamespace(closed=_ShellEvent())
        self.destroyed = threading.Event()

    def load_url(self, url: str) -> None:
        self.ops.append(("load_url", url))

    def load_html(self, html: str) -> None:
        self.ops.append(("load_html", html))

    def close(self) -> None:
        for handler in list(self.events.closed.handlers):
            handler()

    def destroy(self) -> None:
        # pywebview's destroy fires the closed event and, as the last
        # window, ends the GUI loop.
        self.destroyed.set()
        self.close()


class _ShellWebview:
    """pywebview module stand-in driven through the injection seam.

    ``script`` plays the GUI loop: it receives ``(self, drive)`` and decides
    when the readiness driver runs and when the window closes. pywebview runs
    the ``start()`` func on a thread of its own; running it synchronously
    here makes every sequence assertion exact. The default script is the
    happy path — the loop comes up, then the user closes the window."""

    def __init__(self, script=None) -> None:
        self.settings: dict[str, object] = {}
        self.windows: list[_ShellWindow] = []
        self.script = script

    def create_window(self, title, url=None, html=None, **kw) -> _ShellWindow:
        window = _ShellWindow(title, html, **kw)
        self.windows.append(window)
        return window

    def start(self, func=None, args=None, **kw) -> None:
        def drive() -> None:
            if func is not None:
                func(*(args or ()))

        if self.script is None:
            drive()
            self.windows[0].close()
        else:
            self.script(self, drive)


class _ShellServer:
    """uvicorn.Server stand-in for run_shell: ``run()`` parks on whatever
    thread it was given until ``should_exit``, like the real loop's 100ms
    poll — so the join in run_shell's tail is actually exercised."""

    def __init__(self, started: bool = True) -> None:
        self.started = started
        self.should_exit = False
        self.force_exit = False
        self.ran_on: threading.Thread | None = None

    def run(self) -> None:
        self.ran_on = threading.current_thread()
        while not self.should_exit:
            time.sleep(0.005)


@pytest.fixture
def quiet_mac_gate(monkeypatch):
    """run_shell arms the browser route's presence gate on a daemon thread as
    its backup exit signal; park it so no test leaves a 180s poller behind."""
    monkeypatch.setattr(
        gui, "_stop_server_when_window_closes", lambda *a, **k: None
    )


@pytest.fixture
def fake_darwin(monkeypatch):
    """Pretend to be macOS without tripping the darwin-only stdlib imports.

    urllib.request imports the mac-only ``_scproxy`` C module at module level
    whenever ``sys.platform`` reads "darwin" — so on a Linux host it must be
    imported (and cached) while the platform is still honest, or the first
    server import under the fake platform dies on it."""
    import urllib.request  # noqa: F401  (imported for the cache side effect)

    monkeypatch.setattr(sys, "platform", "darwin")


def test_load_mac_shell_returns_the_loaded_webview(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(_mac_shell, "load_webview", lambda: sentinel)
    assert gui._load_mac_shell() is sentinel


def test_load_mac_shell_swallows_a_broken_probe(monkeypatch) -> None:
    # AC-S2's contract at the seam: any blow-up inside the probe reads as
    # "no native window here", never as a failed launch.
    def _boom():
        raise ImportError("pyobjc fell over")

    monkeypatch.setattr(_mac_shell, "load_webview", _boom)
    assert gui._load_mac_shell() is None


@pytest.mark.real_webview_loader
def test_mac_shell_load_webview_none_without_pywebview(monkeypatch) -> None:
    # The Linux/Windows shape of every install, and the macOS shape of a
    # broken one: no webview importable.
    real_import = builtins.__import__

    def _no_webview(name, *args, **kwargs):
        if name == "webview" or name.startswith("webview."):
            raise ImportError("No module named 'webview'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_webview)
    assert _mac_shell.load_webview() is None


@pytest.mark.real_webview_loader
def test_mac_shell_load_webview_returns_the_module(monkeypatch) -> None:
    fake = SimpleNamespace(settings={})
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert _mac_shell.load_webview() is fake


def test_mac_shell_pages_are_self_contained() -> None:
    # These pages exist precisely when the server can serve nothing, so they
    # may reference nothing over the network; the book mark rides inline.
    for html in (_mac_shell._loading_html(), _mac_shell._error_html()):
        assert "http://" not in html and "https://" not in html
        assert "data:image/png;base64," in html
        # These two pages are the only thing here that follows the OS
        # appearance — the app itself has its own toggle. Lose the dark half
        # and a white slab flashes in a dark Mac's face on every launch, with
        # nothing else in the suite to notice.
        assert "@media (prefers-color-scheme: dark)" in html
    assert "Starting litman" in _mac_shell._loading_html()
    # One verdict + one way out, on budget.
    assert "didn't start" in _mac_shell._error_html()
    assert "Close this window" in _mac_shell._error_html()


def test_mac_shell_close_callback_stops_the_server(quiet_mac_gate) -> None:
    # AC-S3: the closed event is the main exit signal — should_exit flips
    # the moment the window goes, inside the GUI loop, not merely when
    # run_shell unwinds afterwards.
    server = _ShellServer()
    seen: list[tuple[str, bool]] = []

    def script(fake, drive):
        drive()
        seen.append(("before close", server.should_exit))
        fake.windows[0].close()
        seen.append(("after close", server.should_exit))

    _mac_shell.run_shell(
        _ShellWebview(script), server, "http://127.0.0.1:8765", PresenceTracker()
    )

    assert seen == [("before close", False), ("after close", True)]


def test_mac_shell_runs_the_server_off_the_main_thread(quiet_mac_gate) -> None:
    # Cocoa owns the main thread, so uvicorn moves off it — where it skips
    # signal-handler installation on its own. run_shell's tail joins the
    # server thread, so ran_on is settled by the time this reads it.
    server = _ShellServer()

    _mac_shell.run_shell(
        _ShellWebview(), server, "http://127.0.0.1:8765", PresenceTracker()
    )

    assert server.ran_on is not None
    assert server.ran_on is not threading.main_thread()


def test_mac_shell_window_opens_on_loading_and_swaps_to_the_app(
    quiet_mac_gate,
) -> None:
    # Loading → load_url ordering: the window is born on the inline loading
    # page (WKWebView would otherwise sit plain white until uvicorn listens)
    # and swaps to the served app exactly once, on readiness.
    server = _ShellServer(started=True)
    url = "http://127.0.0.1:8765"
    fake = _ShellWebview()

    _mac_shell.run_shell(fake, server, url, PresenceTracker())

    (window,) = fake.windows
    assert window.title == "litman"
    assert window.created_html == _mac_shell._loading_html()
    assert window.ops == [("load_url", url)]


def test_mac_shell_swaps_to_the_error_page_when_the_server_never_starts(
    quiet_mac_gate,
) -> None:
    # WKWebView paints plain white against a dead server — no free browser
    # error page exists inside the shell, so run_shell owes the user one.
    server = _ShellServer(started=False)  # never listens; run() still parks
    fake = _ShellWebview()

    _mac_shell.run_shell(
        fake,
        server,
        "http://127.0.0.1:8765",
        PresenceTracker(),
        ready_timeout=0.05,
        ready_poll=0.01,
    )

    (window,) = fake.windows
    assert window.ops == [("load_html", _mac_shell._error_html())]


def test_mac_shell_close_during_startup_skips_the_swap(quiet_mac_gate) -> None:
    # The user closes the loading window before the server is up: the closed
    # callback stands the readiness poller down, so neither the URL nor the
    # error page is loaded into a corpse — and the server is asked to exit.
    server = _ShellServer(started=False)

    def script(fake, drive):
        fake.windows[0].close()  # closed before the loop's driver ran
        drive()

    fake = _ShellWebview(script)
    _mac_shell.run_shell(
        fake,
        server,
        "http://127.0.0.1:8765",
        PresenceTracker(),
        ready_timeout=5.0,
        ready_poll=0.01,
    )

    assert fake.windows[0].ops == []
    assert server.should_exit is True


def test_mac_shell_enables_downloads(quiet_mac_gate) -> None:
    # pywebview refuses downloads by default, and the vault hands out PDFs.
    fake = _ShellWebview()

    _mac_shell.run_shell(
        fake, _ShellServer(), "http://127.0.0.1:8765", PresenceTracker()
    )

    assert fake.settings == {"ALLOW_DOWNLOADS": True}


def test_mac_shell_arms_the_presence_gate_as_backup(monkeypatch) -> None:
    # The backup exit signal: the same page gate the browser route runs, with
    # no process to poll — a window that dies without its closed event still
    # stops the server.
    calls: list[tuple] = []
    monkeypatch.setattr(
        gui,
        "_stop_server_when_window_closes",
        lambda proc, server, presence, **kw: calls.append(
            (proc, server, presence)
        ),
    )
    server = _ShellServer()
    tracker = PresenceTracker()

    _mac_shell.run_shell(
        _ShellWebview(), server, "http://127.0.0.1:8765", tracker
    )

    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == [(None, server, tracker)]


def test_mac_shell_forces_a_server_that_ignores_the_ask(
    quiet_mac_gate, monkeypatch
) -> None:
    # The compressed escalation ladder: ask (should_exit), insist
    # (force_exit) — and the daemon flag on the server thread is the "leave"
    # stage, so a wedged uvicorn cannot hold the closed window's process open.
    monkeypatch.setattr(gui, "FORCE_EXIT_AFTER", 0.1)
    monkeypatch.setattr(gui, "HARD_EXIT_AFTER", 0.3)

    class _WedgedServer(_ShellServer):
        def run(self) -> None:
            self.ran_on = threading.current_thread()
            while not self.force_exit:
                time.sleep(0.005)

    server = _WedgedServer()
    _mac_shell.run_shell(
        _ShellWebview(), server, "http://127.0.0.1:8765", PresenceTracker()
    )

    assert server.force_exit is True


def test_mac_shell_closes_the_window_when_the_server_stops(
    quiet_mac_gate,
) -> None:
    # Self-update (POST /api/self-update) and the presence gate both stop
    # the server from inside the process. The shell must then take the
    # window down too — a dead SPA in a live window, whose process the
    # relaunch would double, is what the browser route's `finally`
    # terminate prevents.
    class _StopsItself(_ShellServer):
        def run(self) -> None:
            self.ran_on = threading.current_thread()
            # Returns at once: stopped from inside, not by the window.

    server = _StopsItself(started=True)

    def script(fake, drive):
        drive()
        # The GUI loop parks until something destroys the window.
        assert fake.windows[0].destroyed.wait(timeout=5)

    fake = _ShellWebview(script)
    assert (
        _mac_shell.run_shell(fake, server, "http://127.0.0.1:8765", PresenceTracker())
        is True
    )
    assert fake.windows[0].destroyed.is_set(), "server stopped, window stayed"


def test_mac_shell_leaves_the_error_page_up_when_the_server_never_started(
    quiet_mac_gate,
) -> None:
    # The reaper's guard rail: a server that never listened has its verdict
    # on screen — the window is the only place that verdict lives, so it
    # must not be taken down with the corpse.
    class _DiesAtStartup(_ShellServer):
        def run(self) -> None:
            self.ran_on = threading.current_thread()
            # Dies without ever listening; started stays False.

    server = _DiesAtStartup(started=False)

    def script(fake, drive):
        drive()  # dead server thread → error page, at once
        time.sleep(0.3)  # plenty of time for a wrong destroy to land
        fake.windows[0].close()  # the user reads the verdict and closes

    fake = _ShellWebview(script)
    _mac_shell.run_shell(
        fake,
        server,
        "http://127.0.0.1:8765",
        PresenceTracker(),
        ready_timeout=30.0,
        ready_poll=0.01,
    )

    (window,) = fake.windows
    assert window.ops == [("load_html", _mac_shell._error_html())]
    assert not window.destroyed.is_set()


def test_mac_shell_error_page_short_circuits_on_a_dead_server_thread(
    quiet_mac_gate,
) -> None:
    # AC-S10 side of the readiness poll: a server thread that died before
    # listening has no readiness coming, so the spinner must give way to the
    # error page at once, not after the full timeout.
    class _DiesAtStartup(_ShellServer):
        def run(self) -> None:
            self.ran_on = threading.current_thread()

    server = _DiesAtStartup(started=False)
    fake = _ShellWebview()
    started_at = time.monotonic()

    _mac_shell.run_shell(
        fake,
        server,
        "http://127.0.0.1:8765",
        PresenceTracker(),
        ready_timeout=30.0,
        ready_poll=0.01,
    )

    elapsed = time.monotonic() - started_at
    (window,) = fake.windows
    assert window.ops == [("load_html", _mac_shell._error_html())]
    assert elapsed < 5.0, f"error page waited out the timeout ({elapsed:.1f}s)"


def test_mac_shell_window_failure_before_the_server_falls_back() -> None:
    # A window layer that fails with the server not yet running must report
    # the launch unconsumed (False) and leave the server untouched — the
    # caller still owns it and the browser route can make good on it.
    class _NoWindowWebview(_ShellWebview):
        def create_window(self, *a, **kw):
            raise RuntimeError("no WindowServer on this box")

    server = _ShellServer()

    ran = _mac_shell.run_shell(
        _NoWindowWebview(), server, "http://127.0.0.1:8765", PresenceTracker()
    )

    assert ran is False
    assert server.ran_on is None  # the server never ran...
    assert server.should_exit is False  # ...and was never asked to stop


def test_mac_shell_start_failure_still_collects_the_server(
    quiet_mac_gate,
) -> None:
    # The late failure point: the GUI loop itself refuses to run, after the
    # server thread is already up. run_shell owns the cleanup — server
    # stopped and collected — and reports the launch consumed (True: there
    # is nothing left to fall back onto).
    class _NoLoopWebview(_ShellWebview):
        def start(self, func=None, args=None, **kw) -> None:
            raise RuntimeError("cocoa refused the loop")

    server = _ShellServer()  # parks until should_exit

    ran = _mac_shell.run_shell(
        _NoLoopWebview(), server, "http://127.0.0.1:8765", PresenceTracker()
    )

    assert ran is True
    assert server.should_exit is True
    assert server.ran_on is not None  # it really ran — and was collected


def test_gui_window_darwin_selects_the_native_shell(
    monkeypatch, gui_harness, fake_darwin, vault_with_paper
) -> None:
    # AC-S1 (darwin arm): darwin + --window hands the launch to the native
    # runner — no browser process, no tab — and the self-update relaunch
    # recipe stays the shortcut one, so double-click semantics survive an
    # in-app update.
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    # A bundle launch that succeeds natively must never respawn itself: the
    # `procs == []` below covers the marker path too.
    monkeypatch.setenv("LITMAN_DARWIN_APP_LAUNCH", "1")
    monkeypatch.setattr(
        shutil, "which", lambda name: _host_abs("/opt/bin/lit") if name == "lit" else None
    )
    shell = object()
    monkeypatch.setattr(gui, "_load_mac_shell", lambda: shell)
    runs: list[tuple] = []
    monkeypatch.setattr(
        # True: the session ran under the shell — gui_cmd must simply return.
        _mac_shell,
        "run_shell",
        lambda *a, **k: (runs.append((a, k)), True)[1],
    )
    import litman.server as server_mod

    real_create_app = server_mod.create_app
    apps: list = []

    def _spy_create_app(vault_arg):
        application = real_create_app(vault_arg)
        apps.append(application)
        return application

    monkeypatch.setattr(server_mod, "create_app", _spy_create_app)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    ((args, kwargs),) = runs
    assert kwargs == {}
    passed_webview, passed_server, passed_url, passed_presence = args
    assert passed_webview is shell
    (fake_server,) = _FakeServer.instances
    assert passed_server is fake_server
    assert passed_url == _served_url(result.output)
    (application,) = apps
    assert passed_presence is application.state.presence
    # run_shell owns server.run; gui_cmd must not fall through and run it too.
    assert fake_server.ran is False
    assert procs == [] and opened == []  # no browser of any kind
    # AC-S2 reverse: a working shell prints no fallback line.
    assert "Native window unavailable" not in result.output
    assert "Close the window to stop the server" in result.output
    assert application.state.self_update_relaunch == [
        _host_abs("/opt/bin/lit"),
        "gui",
        "--window",
    ]


def test_gui_darwin_tab_mode_never_goes_native(
    monkeypatch, gui_harness, fake_darwin, vault_with_paper
) -> None:
    # Plain `lit gui` keeps the tab contract on macOS too: the shell is for
    # --window only.
    opened, procs = gui_harness
    vault, _pid = vault_with_paper

    def _boom():
        raise AssertionError("tab mode must never probe the mac shell")

    monkeypatch.setattr(gui, "_load_mac_shell", _boom)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert opened == [_served_url(result.output)] and procs == []


def test_gui_window_darwin_shell_failure_falls_back_to_the_browser_unchanged(
    monkeypatch, gui_harness, fake_darwin, vault_with_paper, tmp_path
) -> None:
    # AC-S2: a probe that blows up (ImportError included) must cost nothing —
    # the Chromium route runs byte-for-byte as it always did, announced by
    # one dim line. The break is planted below gui's own seam (in
    # load_webview), so the catch inside the real _load_mac_shell is what
    # this drives — not the belt-and-braces try around it. No app-launch
    # marker: this is the terminal shape, which serves in place.
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.delenv("LITMAN_DARWIN_APP_LAUNCH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(gui, "_DARWIN_SYSTEM_APPS", tmp_path / "no-system-apps")
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

    def _boom():
        raise ImportError("no pywebview on this box")

    monkeypatch.setattr(_mac_shell, "load_webview", _boom)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert "Native window unavailable" in result.output  # the one dim line
    url = _served_url(result.output)
    profile = (
        Path(os.environ["LITMAN_REGISTRY_DIR"]).expanduser() / "browser-profile"
    )
    (proc,) = procs
    assert proc.argv == [
        str(binary),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--disable-sync",
        "--hide-crash-restore-bubble",
    ]
    assert opened == []


def test_gui_window_darwin_window_layer_failure_falls_back_to_the_browser(
    monkeypatch, gui_harness, fake_darwin, vault_with_paper
) -> None:
    # The late twin of the test above: pywebview imported fine, but the
    # window layer failed before the server ever ran (run_shell → False).
    # The launch is still gui_cmd's to make good on — same dim line, and the
    # server runs under the browser route.
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.delenv("LITMAN_DARWIN_APP_LAUNCH", raising=False)
    monkeypatch.setattr(gui, "_load_mac_shell", lambda: object())
    monkeypatch.setattr(_mac_shell, "run_shell", lambda *a, **k: False)
    monkeypatch.setattr(
        gui, "_app_window_argv", lambda url: ["chromium", f"--app={url}"]
    )

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert "Native window unavailable" in result.output
    (fake_server,) = _FakeServer.instances
    assert fake_server.ran  # gui_cmd still ran the server itself
    (proc,) = procs
    assert proc.argv == ["chromium", f"--app={_served_url(result.output)}"]
    assert opened == []


def test_shed_darwin_app_identity_without_marker_is_a_noop(monkeypatch) -> None:
    # A terminal launch has no bundle identity to shed: no marker, no spawn.
    monkeypatch.delenv("LITMAN_DARWIN_APP_LAUNCH", raising=False)

    def _boom(*a, **k):
        raise AssertionError("no marker means no respawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert gui._shed_darwin_app_identity() is False


def test_shed_darwin_app_identity_survives_an_unresolvable_lit(
    monkeypatch,
) -> None:
    # No `lit` to respawn is no reason to serve nothing: report False so the
    # caller serves in place with the residual identity risk.
    from litman.exceptions import LitmanError

    monkeypatch.setenv("LITMAN_DARWIN_APP_LAUNCH", "1")

    def _no_lit():
        raise LitmanError("no lit anywhere")

    monkeypatch.setattr(gui, "_resolve_lit_executable", _no_lit)
    assert gui._shed_darwin_app_identity() is False


def test_gui_window_darwin_bundle_fallback_respawns_without_identity(
    monkeypatch, gui_harness, fake_darwin, vault_with_paper
) -> None:
    # M3 plan B: a bundle launch (stub marker in env) whose native shell is
    # unavailable must not serve a browser window while wearing litman.app's
    # identity — that is the double-click deadlock shape. It hands the launch
    # to a detached, marker-stripped child and exits.
    opened, _procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setenv("LITMAN_DARWIN_APP_LAUNCH", "1")
    monkeypatch.setattr(gui, "_load_mac_shell", lambda: None)
    monkeypatch.setattr(
        shutil, "which", lambda name: _host_abs("/opt/bin/lit") if name == "lit" else None
    )
    spawned: list[tuple[list, dict]] = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kw: (spawned.append((list(argv), kw)), _FakeProc(argv))[1],
    )

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    ((argv, kwargs),) = spawned
    assert argv == [_host_abs("/opt/bin/lit"), "gui", "--window"]
    assert kwargs["start_new_session"] is True
    # The stripped marker is the recursion gate: the child, falling back
    # again, serves in place.
    assert "LITMAN_DARWIN_APP_LAUNCH" not in kwargs["env"]
    (fake_server,) = _FakeServer.instances
    assert fake_server.ran is False  # the parent serves nothing...
    assert opened == []  # ...and opens nothing
    assert "relaunching in the browser" in result.output
    assert "falling back to the browser" not in result.output


def test_gui_window_darwin_bundle_fallback_after_window_failure_respawns(
    monkeypatch, gui_harness, fake_darwin, vault_with_paper
) -> None:
    # Same hand-off from the other fallback door: pywebview loaded but the
    # window layer failed before the server ran (run_shell → False).
    opened, _procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setenv("LITMAN_DARWIN_APP_LAUNCH", "1")
    monkeypatch.setattr(gui, "_load_mac_shell", lambda: object())
    monkeypatch.setattr(_mac_shell, "run_shell", lambda *a, **k: False)
    monkeypatch.setattr(
        shutil, "which", lambda name: _host_abs("/opt/bin/lit") if name == "lit" else None
    )
    spawned: list[tuple[list, dict]] = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kw: (spawned.append((list(argv), kw)), _FakeProc(argv))[1],
    )

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    ((argv, kwargs),) = spawned
    assert argv == [_host_abs("/opt/bin/lit"), "gui", "--window"]
    assert kwargs["start_new_session"] is True
    assert "LITMAN_DARWIN_APP_LAUNCH" not in kwargs["env"]
    (fake_server,) = _FakeServer.instances
    assert fake_server.ran is False
    assert opened == []


def test_gui_window_darwin_respawn_failure_serves_in_place(
    monkeypatch, gui_harness, fake_darwin, vault_with_paper
) -> None:
    # The child could not be spawned: serving with the residual identity
    # risk beats serving nothing, so the launch stays here and takes the
    # ordinary in-place browser route.
    opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setenv("LITMAN_DARWIN_APP_LAUNCH", "1")
    monkeypatch.setattr(gui, "_load_mac_shell", lambda: None)
    monkeypatch.setattr(
        shutil, "which", lambda name: _host_abs("/opt/bin/lit") if name == "lit" else None
    )
    monkeypatch.setattr(
        gui, "_app_window_argv", lambda url: ["chromium", f"--app={url}"]
    )

    def _popen(argv, **kw):
        if list(argv)[1:3] == ["gui", "--window"]:  # the respawn attempt
            raise OSError("spawn failed")
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert "falling back to the browser" in result.output
    (fake_server,) = _FakeServer.instances
    assert fake_server.ran  # the launch stayed here and served
    (proc,) = procs
    assert proc.argv == ["chromium", f"--app={_served_url(result.output)}"]
    assert opened == []


def test_gui_window_off_darwin_never_reads_the_app_launch_marker(
    monkeypatch, gui_harness, chromium_on_path, vault_with_paper
) -> None:
    # The marker is a darwin-bundle contract; on any other platform a stray
    # variable in the environment must not so much as be consulted.
    _opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setenv("LITMAN_DARWIN_APP_LAUNCH", "1")

    def _boom():
        raise AssertionError("the marker must never be consulted off darwin")

    monkeypatch.setattr(gui, "_shed_darwin_app_identity", _boom)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    assert len(procs) == 1  # the ordinary browser window launch


def test_gui_window_linux_argv_is_unchanged_by_the_native_shell(
    monkeypatch, gui_harness, chromium_on_path, vault_with_paper
) -> None:
    # AC-S1 (Linux arm): the native shell is darwin-only — never probed here —
    # and the spawned window's argv is byte-for-byte the pre-shell recipe.
    _opened, procs = gui_harness
    vault, _pid = vault_with_paper

    def _boom():
        raise AssertionError("the mac shell must never be probed off darwin")

    monkeypatch.setattr(gui, "_load_mac_shell", _boom)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    url = _served_url(result.output)
    profile = (
        Path(os.environ["LITMAN_REGISTRY_DIR"]).expanduser() / "browser-profile"
    )
    (proc,) = procs
    assert proc.argv == [
        "/usr/bin/google-chrome",
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--disable-sync",
        "--hide-crash-restore-bubble",
        "--class=litman",
        "--ozone-platform=x11",
    ]


def test_gui_window_win32_argv_is_unchanged_by_the_native_shell(
    monkeypatch, gui_harness, vault_with_paper
) -> None:
    # AC-S1 (Windows arm): same guarantee as the Linux one, minus the two
    # Linux-only flags.
    _opened, procs = gui_harness
    vault, _pid = vault_with_paper
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "C:\\browsers\\google-chrome"
        if name == "google-chrome"
        else None,
    )

    def _boom():
        raise AssertionError("the mac shell must never be probed off darwin")

    monkeypatch.setattr(gui, "_load_mac_shell", _boom)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    assert result.exit_code == 0, result.output
    url = _served_url(result.output)
    profile = (
        Path(os.environ["LITMAN_REGISTRY_DIR"]).expanduser() / "browser-profile"
    )
    (proc,) = procs
    assert proc.argv == [
        "C:\\browsers\\google-chrome",
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--disable-sync",
        "--hide-crash-restore-bubble",
    ]


@pytest.mark.real_webview_loader
@pytest.mark.skipif(
    sys.platform != "darwin", reason="the real WKWebView shell needs macOS"
)
def test_mac_shell_real_default_end_to_end() -> None:
    # AC-S5, the inject-seam red line: the shipped default through the real
    # seam — real pywebview import, real window, real close, server really
    # gone. No fakes anywhere.
    import uvicorn

    from litman.server import create_app

    webview = gui._load_mac_shell()
    assert webview is not None, "pywebview must be installed on the mac host"

    port = _find_free_port(_DEFAULT_PORT)
    application = create_app(None)
    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=port, log_level="warning"
        )
    )

    failures: list[str] = []

    def _close_once_up() -> None:
        import contextlib

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if getattr(server, "started", False) and webview.windows:
                time.sleep(1.0)  # let the readiness swap land in the window
                webview.windows[0].destroy()
                return
            time.sleep(0.05)
        # A silent return here would leave run_shell parked in the GUI loop
        # forever: record the failure, then force everything down so the
        # main thread comes back and the assertion below can fire.
        failures.append("server/window never came up within 15s")
        server.should_exit = True
        for window in list(webview.windows):
            with contextlib.suppress(Exception):
                window.destroy()

    threading.Thread(target=_close_once_up, daemon=True).start()
    _mac_shell.run_shell(
        webview, server, f"http://127.0.0.1:{port}", application.state.presence
    )

    assert failures == []
    assert server.started is True  # it really served
    assert server.should_exit is True  # and was really asked to stop
    # ...and really stopped: no live listener holds the port. SO_REUSEADDR,
    # because the shell's real HTTP traffic leaves TIME_WAIT pairs a bare
    # bind trips over on BSD — while a socket still LISTENing would refuse
    # this bind regardless, which is the property under test.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


# ---------------------------------------------------------------------------
# bundled icon assets (task-gui-desktop-entry D3, package-data)
# ---------------------------------------------------------------------------


def test_bundled_icons_resolve_via_importlib_resources() -> None:
    from importlib.resources import files

    for name in ("litman.png", "litman.ico", "litman.icns"):
        icon = files("litman").joinpath("assets", "icons", name)
        assert icon.is_file(), f"missing bundled icon {name}"
        assert len(icon.read_bytes()) > 0


def test_bundled_icns_carries_the_sizes_macos_draws() -> None:
    # The .icns is a build-time artifact no test host here can regenerate (it
    # is rendered from assets/icon.svg), so what a test can still do is refuse
    # a replacement that is not a real icon file or that dropped the sizes the
    # Dock and Retina Launchpad ask for. A truncated or single-size .icns is
    # accepted by nothing on macOS and shows as the generic tile again.
    import struct
    from importlib.resources import files

    raw = files("litman").joinpath("assets", "icons", "litman.icns").read_bytes()
    assert raw[:4] == b"icns"
    declared = struct.unpack(">I", raw[4:8])[0]
    assert declared == len(raw), "declared length must match the file"

    chunks, offset = {}, 8
    while offset < declared:
        kind = raw[offset : offset + 4].decode("latin1")
        length = struct.unpack(">I", raw[offset + 4 : offset + 8])[0]
        assert 8 < length <= declared - offset, f"bad chunk length for {kind}"
        chunks[kind] = raw[offset + 8 : offset + length]
        offset += length
    assert offset == declared, "chunk lengths must tile the file exactly"

    # ic07/ic08/ic09 are 128/256/512; ic13/ic14 the Retina Dock pair.
    for kind in ("ic07", "ic08", "ic09", "ic13", "ic14"):
        assert kind in chunks, f"missing {kind}"
        assert chunks[kind][:8] == b"\x89PNG\r\n\x1a\n", f"{kind} is not a PNG"


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


def test_open_when_ready_gives_up_early_when_told() -> None:
    # The mac shell's probe: a server thread that died has no readiness
    # coming, so the poll must break to the open decision at once instead of
    # sitting out the timeout (the caller's callback shows the error page).
    server = _StartFlag(started=False)
    calls: list[int] = []
    started_at = time.monotonic()
    _open_when_ready(
        server, lambda: calls.append(1), threading.Event(),
        ready_timeout=30.0, ready_poll=0.01, give_up=lambda: True,
    )
    assert calls == [1]  # opened (the callback decides what that means)
    assert time.monotonic() - started_at < 5.0


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


def test_want_splash_false_on_macos(monkeypatch, splash_gui, vault_with_paper) -> None:
    # Same conditions as the test above — the ones that DO launch a splash —
    # differing only in the platform. Aqua's Tk ignores overrideredirect, so the
    # splash arrives as an ordinary titled window (Tk's default "tk" in the
    # title bar, traffic lights, its own Dock tile) that impersonates litman's
    # main window until it vanishes. Launch Services already bounces the Dock
    # icon, which is the feedback the splash exists to provide.
    vault, _pid = vault_with_paper
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(gui, "_launched_without_console", lambda: True)

    result = CliRunner().invoke(gui_cmd, ["--library", str(vault), "--window"])

    # Subject first: this is the assertion the guard exists for, and it must be
    # the one that fails when the guard goes away.
    assert not _splash_launched(splash_gui.argvs)
    assert result.exit_code == 0, result.output


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


# ---------------------------------------------------------------------------
# launcher self-heal + console-shortcut warning (task-win-update-hardening)
# ---------------------------------------------------------------------------


def test_repair_launcher_stubs_reports_what_it_restored(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    from litman.core import launcher_stubs

    monkeypatch.setattr(launcher_stubs, "repair_default", lambda: ["litw.exe"])
    gui._repair_launcher_stubs()
    assert "restored missing launcher litw.exe" in capsys.readouterr().out


def test_repair_launcher_stubs_never_raises(monkeypatch) -> None:
    """Best-effort: a repair blow-up must not take `lit gui` down with it."""
    monkeypatch.setattr(sys, "platform", "win32")
    from litman.core import launcher_stubs

    def _boom() -> list[str]:
        raise OSError("disk on fire")

    monkeypatch.setattr(launcher_stubs, "repair_default", _boom)
    gui._repair_launcher_stubs()  # no exception


def test_repair_launcher_stubs_noop_off_windows(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    from litman.core import launcher_stubs

    def _boom() -> list[str]:
        raise AssertionError("must not be called off win32")

    monkeypatch.setattr(launcher_stubs, "repair_default", _boom)
    gui._repair_launcher_stubs()
    assert capsys.readouterr().out == ""


def test_warn_console_shortcut_speaks_up_on_lit_fallback(
    monkeypatch, capsys
) -> None:
    """litw.exe missing → the shortcut targets console lit.exe; that fallback
    must be loud (a console shortcut dies with its console window)."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        gui, "_shortcut_executable", lambda: r"C:\bin\lit.exe"
    )
    gui._warn_console_shortcut()
    out = capsys.readouterr().out
    assert "litw.exe" in out
    assert "console window" in out


def test_warn_console_shortcut_silent_when_litw_is_used(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        gui, "_shortcut_executable", lambda: r"C:\bin\litw.exe"
    )
    gui._warn_console_shortcut()
    assert capsys.readouterr().out == ""
