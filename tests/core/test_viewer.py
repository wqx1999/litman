"""``core.viewer``'s opener: whether a file opened on this machine reaches a
screen (``open_files_mode``), and ``launch_file`` hearing an opener fail.

No test reaches a real opener. An autouse fixture makes ``_spawn`` and
``_startfile`` fail the test unless the test puts its own in place, and the
platform is faked through ``viewer._platform`` — never ``sys.platform``, which
is the whole interpreter's. The two tests that run the real ``_spawn`` put a
fake ``xdg-open`` first and alone on PATH, with ``_platform`` saying "linux",
so a macOS runner never reaches its own ``open``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from litman.core import viewer

_REAL_SPAWN = viewer._spawn
_REAL_WIN32 = sys.platform == "win32"
_SLEEP = shutil.which("sleep")
_ENV_VARS = ("SSH_CONNECTION", "SSH_CLIENT", "DISPLAY", "WAYLAND_DISPLAY")
_SSH = "10.0.0.1 50022 10.0.0.2 22"

_posix_only = pytest.mark.skipif(
    _REAL_WIN32, reason="the fake opener is a POSIX shell script"
)


@pytest.fixture(autouse=True)
def _no_real_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a test reached a real opener: {args!r}")

    monkeypatch.setattr(viewer, "_spawn", refuse)
    monkeypatch.setattr(viewer, "_startfile", refuse)


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Exactly these of SSH_CONNECTION / SSH_CLIENT / DISPLAY /
    WAYLAND_DISPLAY set: the machine running the suite may have any of them."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _host(monkeypatch: pytest.MonkeyPatch, platform: str, *on_path: str) -> None:
    monkeypatch.setattr(viewer, "_platform", lambda: platform)
    monkeypatch.setattr(
        viewer,
        "_which",
        lambda name: f"/usr/bin/{name}" if name in on_path else None,
    )


class _Opener:
    """Stands in for ``_spawn``: records each argv and returns itself as the
    process, which exits with ``code`` — or with ``code=None`` is still
    running when the wait runs out."""

    def __init__(self, code: int | None = 0, error: OSError | None = None) -> None:
        self.code = code
        self.error = error
        self.argvs: list[list[str]] = []
        self.timeouts: list[float | None] = []

    def __call__(self, argv: list[str]) -> _Opener:
        self.argvs.append(list(argv))
        if self.error is not None:
            raise self.error
        return self

    def wait(self, timeout: float | None = None) -> int:
        self.timeouts.append(timeout)
        if self.code is None:
            raise subprocess.TimeoutExpired("opener", timeout or 0)
        return self.code


def _opener(
    monkeypatch: pytest.MonkeyPatch, code: int | None = 0, error: OSError | None = None
) -> _Opener:
    opener = _Opener(code, error)
    monkeypatch.setattr(viewer, "_spawn", opener)
    return opener


def _file(folder: Path, name: str = "si-02.xlsx") -> Path:
    path = folder / name
    path.write_bytes(b"PK\x03\x04 sheet")
    return path


def _desktop_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, DISPLAY=":0")
    _host(monkeypatch, "linux", "xdg-open")


# ---------------------------------------------------------------------------
# open_files_mode — does a file opened here reach a screen?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "on_path", "env", "mode"),
    [
        ("darwin", (), {}, "desktop"),
        ("win32", (), {}, "desktop"),
        ("linux", ("xdg-open",), {"DISPLAY": ":0"}, "desktop"),
        ("linux", ("xdg-open",), {"WAYLAND_DISPLAY": "wayland-0"}, "desktop"),
        ("linux", ("wslview",), {}, "desktop"),
        ("linux", ("xdg-open", "wslview"), {}, "desktop"),
        ("linux", ("xdg-open",), {}, "none"),
        ("linux", (), {"DISPLAY": ":0"}, "none"),
        ("linux", (), {}, "none"),
        (
            "linux",
            ("xdg-open",),
            {"DISPLAY": "localhost:10.0", "SSH_CONNECTION": _SSH, "SSH_CLIENT": _SSH},
            "none",
        ),
        ("linux", ("xdg-open",), {"DISPLAY": ":0", "SSH_CLIENT": _SSH}, "none"),
        ("linux", ("wslview",), {"SSH_CONNECTION": _SSH}, "none"),
        ("darwin", (), {"SSH_CONNECTION": _SSH}, "none"),
        ("win32", (), {"SSH_CLIENT": _SSH}, "none"),
    ],
    ids=[
        "macos",
        "windows",
        "linux-x11",
        "linux-wayland",
        "wsl",
        "wsl-with-xdg-utils",
        "linux-headless",
        "linux-display-but-no-xdg-open",
        "linux-nothing",
        "hpc-login-node",
        "ssh-client-only",
        "wsl-over-ssh",
        "macos-over-ssh",
        "windows-over-ssh",
    ],
)
def test_open_files_mode(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    on_path: tuple[str, ...],
    env: dict[str, str],
    mode: str,
) -> None:
    _env(monkeypatch, **env)
    _host(monkeypatch, platform, *on_path)
    assert viewer.open_files_mode() == mode


@pytest.mark.parametrize("variable", ["SSH_CONNECTION", "SSH_CLIENT"])
def test_an_ssh_session_has_no_desktop_on_any_host(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Nothing faked but the environment: this holds on every OS the suite
    runs on, with whatever that runner has on PATH."""
    _env(monkeypatch, DISPLAY=":0", **{variable: _SSH})
    assert viewer.is_ssh_session()
    assert viewer.open_files_mode() == "none"


@pytest.mark.parametrize(
    ("env", "ssh"),
    [
        ({}, False),
        ({"DISPLAY": ":0"}, False),
        ({"SSH_CONNECTION": _SSH}, True),
        ({"SSH_CLIENT": _SSH}, True),
        ({"SSH_CONNECTION": ""}, False),
    ],
    ids=["nothing", "display-only", "ssh-connection", "ssh-client", "empty"],
)
def test_is_ssh_session(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], ssh: bool
) -> None:
    _env(monkeypatch, **env)
    assert viewer.is_ssh_session() is ssh


def test_the_seams_read_the_real_platform_and_path() -> None:
    assert viewer._platform() == sys.platform
    assert viewer._which("litman-no-such-program-anywhere") is None


@pytest.mark.parametrize(
    ("platform", "on_path", "expected"),
    [
        ("darwin", (), "open"),
        ("win32", (), viewer.WINDOWS_STARTFILE_SENTINEL),
        ("linux", ("xdg-open", "wslview"), "xdg-open"),
        ("linux", ("wslview",), "wslview"),
        ("linux", (), None),
    ],
)
def test_detect_platform_viewer_follows_the_seams(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    on_path: tuple[str, ...],
    expected: str | None,
) -> None:
    _host(monkeypatch, platform, *on_path)
    assert viewer.detect_platform_viewer() == expected


# ---------------------------------------------------------------------------
# launch_file — the opener's exit decides
# ---------------------------------------------------------------------------


def test_an_opener_exiting_0_opened_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_linux(monkeypatch)
    opener = _opener(monkeypatch, 0)
    sheet = _file(tmp_path)

    assert viewer.launch_file(sheet) == ("xdg-open", "platform")
    assert opener.argvs == [["xdg-open", str(sheet)]]
    assert opener.timeouts == [viewer.OPEN_WAIT_S]


def test_the_wait_stays_short() -> None:
    """Long enough to hear "no app", short enough that a viewer xdg-open
    runs in the foreground never holds the terminal or the click."""
    assert 0 < viewer.OPEN_WAIT_S <= 2


def test_an_opener_still_running_when_the_wait_ends_opened_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_linux(monkeypatch)
    opener = _opener(monkeypatch, None)
    assert viewer.launch_file(_file(tmp_path), wait_s=0.25) == ("xdg-open", "platform")
    assert opener.timeouts == [0.25]


def test_an_opener_exiting_3_found_no_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_linux(monkeypatch)
    _opener(monkeypatch, 3)

    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(_file(tmp_path))

    failed = caught.value
    assert (failed.reason, failed.command) == ("no-app", "xdg-open")
    assert str(failed) == (
        "No app opened si-02.xlsx (xdg-open exited 3). "
        "Install an app for .xlsx files, or open the path below yourself."
    )
    # `lit open` maps FileNotFoundError to exit 2 + the path.
    assert isinstance(failed, FileNotFoundError)


def test_a_pdf_no_app_opened_points_at_the_viewer_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_linux(monkeypatch)
    _opener(monkeypatch, 4)
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(_file(tmp_path, "paper.pdf"))
    assert str(caught.value) == (
        "No app opened paper.pdf (xdg-open exited 4). "
        "Set default_pdf_viewer in lit-config.yaml to a specific viewer."
    )


def test_an_opener_that_will_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_linux(monkeypatch)
    _opener(monkeypatch, error=PermissionError(13, "Permission denied"))
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(_file(tmp_path))
    assert (caught.value.reason, caught.value.command) == ("not-started", "xdg-open")
    assert "could not be started (PermissionError)" in str(caught.value)


def test_mac_open_exiting_1_found_no_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "darwin")
    opener = _opener(monkeypatch, 1)
    sheet = _file(tmp_path)

    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(sheet)

    assert opener.argvs == [["open", str(sheet)]]
    assert (caught.value.reason, caught.value.command) == ("no-app", "open")
    assert "(open exited 1)" in str(caught.value)


def test_windows_hands_the_absolute_path_to_startfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "win32")
    started: list[str] = []
    monkeypatch.setattr(viewer, "_startfile", started.append)
    sheet = _file(tmp_path)

    assert viewer.launch_file(sheet) == ("os.startfile", "platform")
    assert started == [str(sheet)]


def _no_association(path: str) -> None:
    raise OSError(1155, "No application is associated with the specified file")


def test_windows_without_an_associated_app_found_no_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "win32")
    monkeypatch.setattr(viewer, "_startfile", _no_association)

    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(_file(tmp_path))

    assert (caught.value.reason, caught.value.command) == ("no-app", "os.startfile")
    assert str(caught.value) == (
        "Windows has no app for si-02.xlsx, or refused to start one. "
        "Install an app for .xlsx files, or open the path below yourself."
    )


def test_windows_without_a_pdf_app_points_at_the_viewer_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "win32")
    monkeypatch.setattr(viewer, "_startfile", _no_association)

    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(_file(tmp_path, "paper.pdf"))

    assert caught.value.reason == "no-app"
    assert "no associated PDF application, or the launch was refused. Set" in str(
        caught.value
    )


def test_a_configured_viewer_comes_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "linux")  # no platform opener at all
    opener = _opener(monkeypatch, 0)
    pdf = _file(tmp_path, "si-01.pdf")

    assert viewer.launch_file(pdf, "zathura") == ("zathura", "configured")
    assert opener.argvs == [["zathura", str(pdf)]]


def test_a_configured_viewer_that_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "linux")
    pdf = _file(tmp_path, "si-01.pdf")

    _opener(monkeypatch, 1)
    with pytest.raises(viewer.OpenFailed) as exited:
        viewer.launch_file(pdf, "zathura")
    assert (exited.value.reason, exited.value.command) == ("no-app", "zathura")
    assert "Check default_pdf_viewer in lit-config.yaml." in str(exited.value)

    _opener(monkeypatch, error=FileNotFoundError(2, "No such file"))
    with pytest.raises(viewer.OpenFailed) as missing:
        viewer.launch_file(pdf, "zathura")
    assert (missing.value.reason, missing.value.command) == ("not-started", "zathura")
    assert "not found on PATH" in str(missing.value)


def test_xdg_open_without_a_display_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "linux", "xdg-open")
    opener = _opener(monkeypatch, 0)
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(_file(tmp_path))
    assert (caught.value.reason, caught.value.command) == ("not-started", "xdg-open")
    assert "No graphical display detected" in str(caught.value)
    assert opener.argvs == []


def test_xdg_open_without_a_display_falls_back_to_wslview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "linux", "xdg-open", "wslview")
    opener = _opener(monkeypatch, 0)
    sheet = _file(tmp_path)
    assert viewer.launch_file(sheet) == ("wslview", "wsl-fallback")
    assert opener.argvs == [["/usr/bin/wslview", str(sheet)]]


def test_no_opener_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(monkeypatch, DISPLAY=":0")
    _host(monkeypatch, "linux")
    opener = _opener(monkeypatch, 0)
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(_file(tmp_path))
    assert caught.value.reason == "not-started"
    assert opener.argvs == []


def test_a_relative_path_reaches_the_opener_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_linux(monkeypatch)
    opener = _opener(monkeypatch, 0)
    _file(tmp_path)
    monkeypatch.chdir(tmp_path)

    viewer.launch_file(Path("si-02.xlsx"))

    (argv,) = opener.argvs
    assert Path(argv[1]).is_absolute()
    assert Path(argv[1]).resolve() == (tmp_path / "si-02.xlsx").resolve()


def test_launch_pdf_waits_like_launch_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _desktop_linux(monkeypatch)
    pdf = _file(tmp_path, "paper.pdf")
    _opener(monkeypatch, 0)
    assert viewer.launch_pdf(pdf, None) == ("xdg-open", "platform")
    _opener(monkeypatch, 3)
    with pytest.raises(viewer.OpenFailed):
        viewer.launch_pdf(pdf, None)


# ---------------------------------------------------------------------------
# The real _spawn, against a fake xdg-open
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_xdg_open(
    _no_real_opener: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """The real ``_spawn``, with a fake ``xdg-open`` as the only program on
    PATH. It writes its pid and argv next to the returned path, then exits
    with ``$FAKE_OPENER_CODE`` — or, with ``$FAKE_OPENER_SLEEP`` set, turns
    into ``sleep`` and stays running."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "xdg-open"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$$" > "$FAKE_OPENER_LOG.pid"\n'
        'for arg in "$@"; do printf "%s\\n" "$arg"; done > "$FAKE_OPENER_LOG.argv"\n'
        'if [ -n "$FAKE_OPENER_SLEEP" ]; then exec "$FAKE_OPENER_SLEEP" 30; fi\n'
        'exit "${FAKE_OPENER_CODE:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    log = tmp_path / "opener"
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("FAKE_OPENER_LOG", str(log))
    monkeypatch.delenv("FAKE_OPENER_SLEEP", raising=False)
    monkeypatch.delenv("FAKE_OPENER_CODE", raising=False)
    _env(monkeypatch, DISPLAY=":0")
    monkeypatch.setattr(viewer, "_platform", lambda: "linux")
    monkeypatch.setattr(viewer, "_spawn", _REAL_SPAWN)
    return log


def _wait_for_file(path: Path) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8")
        time.sleep(0.05)
    raise AssertionError(f"the fake opener never wrote {path.name}")


@_posix_only
def test_the_real_spawn_hears_the_opener_fail(
    fake_xdg_open: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_OPENER_CODE", "3")
    sheet = _file(tmp_path)

    # A generous wait: on a loaded runner /bin/sh may take longer than the
    # real one to start, and would read as an opener still running.
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_file(sheet, wait_s=10)

    assert (caught.value.reason, caught.value.command) == ("no-app", "xdg-open")
    assert "(xdg-open exited 3)" in str(caught.value)
    argv = _wait_for_file(fake_xdg_open.with_suffix(".argv")).splitlines()
    assert argv == [str(sheet)]


def test_spawn_shares_no_stream_with_the_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin, stdout and stderr all DEVNULL: an app xdg-open keeps in the
    foreground must never hold this process's pipes or terminal."""
    seen: dict[str, object] = {}

    def popen(argv: list[str], **kwargs: object) -> str:
        seen.update(argv=argv, **kwargs)
        return "process"

    monkeypatch.setattr(viewer.subprocess, "Popen", popen)

    assert _REAL_SPAWN(["xdg-open", "si-02.xlsx"]) == "process"
    assert seen == {
        "argv": ["xdg-open", "si-02.xlsx"],
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }


@_posix_only
@pytest.mark.skipif(_SLEEP is None, reason="no sleep program to stand in for an app")
def test_the_real_spawn_leaves_a_running_opener_in_its_own_session(
    fake_xdg_open: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _SLEEP is not None
    monkeypatch.setenv("FAKE_OPENER_SLEEP", _SLEEP)
    sheet = _file(tmp_path)

    assert viewer.launch_file(sheet, wait_s=0.5) == ("xdg-open", "platform")

    pid = int(_wait_for_file(fake_xdg_open.with_suffix(".pid")))
    try:
        # Its own session: a Ctrl-C on `lit gui` or `lit open` leaves the
        # app it started alone.
        assert os.getsid(pid) == pid
        assert os.getsid(pid) != os.getsid(0)
        assert _wait_for_file(fake_xdg_open.with_suffix(".argv")).splitlines() == [
            str(sheet)
        ]
    finally:
        os.kill(pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# launch_url — a web page to the default browser
# ---------------------------------------------------------------------------

_PAGE = "https://pubs.acs.org/doi/10.1021/acs.jmedchem.4c00001?ref=pdf#si"


@pytest.mark.parametrize(
    "url",
    [
        _PAGE,
        "http://example.org",
        "HTTPS://Example.ORG/Path",
        "https://user@example.org:8443/a?b=c",
        "https://a%40b@example.org/x%20y?q=%5C#%25",
        "https://1.2.3.4/x",
        "https://[2001:db8::1]/x",
        "https://xn--caf-dma.example/",
    ],
)
def test_a_web_page_on_another_machine_is_accepted(url: str) -> None:
    assert viewer.web_address_problem(url) is None


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("javascript:alert(1)", "Only an http or https link can go to your browser"),
        ("file:///C:/Windows/notepad.exe", "Only an http or https link can go to your browser"),
        ("data:text/html,<b>x</b>", "Only an http or https link can go to your browser"),
        ("zotero://select/items/1", "Only an http or https link can go to your browser"),
        ("ftp://example.org/x", "Only an http or https link can go to your browser"),
        ("mailto:someone@example.org", "Only an http or https link can go to your browser"),
        ("//example.org/x", "Only an http or https link can go to your browser"),
        ("", "The link is not a single web address"),
        ("https://", "The link is not a single web address"),
        ("https://exa mple.org/", "The link is not a single web address"),
        ("https://example.org/\nx", "The link is not a single web address"),
        ("https://example.org/\x00", "The link is not a single web address"),
        ("https://café.example/", "The link is not a single web address"),
        ("https://[::1/x", "The link is not a single web address"),
        ("http://127.1/", "The link is not a single web address"),
        ("http://0x7f.1/", "The link is not a single web address"),
        ("http://2130706433/", "The link is not a single web address"),
        # Python and a browser would read these as two different machines.
        ("http://127.0.0.1:8765\\@example.org/", "The link is not a single web address"),
        ("http://example.org\\x/", "The link is not a single web address"),
        ("http://127.0.0.%31:8765/", "The link is not a single web address"),
        ("http://%6c%6fcalhost:8765/", "The link is not a single web address"),
        ("http://localhost%2e/", "The link is not a single web address"),
        ("http://127.0.0.1:8765/api/vaults", "The link points at this computer, not the web"),
        ("http://127.8.9.10/", "The link points at this computer, not the web"),
        ("http://localhost:8765/", "The link points at this computer, not the web"),
        ("http://LOCALHOST./", "The link points at this computer, not the web"),
        ("http://app.localhost/", "The link points at this computer, not the web"),
        ("http://[::1]:8765/", "The link points at this computer, not the web"),
        ("http://[::ffff:127.0.0.1]/", "The link points at this computer, not the web"),
        ("http://0.0.0.0:8765/", "The link points at this computer, not the web"),
    ],
)
def test_anything_else_is_refused(url: str, problem: str) -> None:
    assert viewer.web_address_problem(url) == problem


def test_a_refused_address_reaches_no_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    _desktop_linux(monkeypatch)
    opener = _opener(monkeypatch, 0)
    with pytest.raises(ValueError, match="Only an http or https link"):
        viewer.launch_url("file:///etc/passwd")
    assert opener.argvs == []


def test_linux_hands_the_page_to_xdg_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _desktop_linux(monkeypatch)
    opener = _opener(monkeypatch, 0)
    assert viewer.launch_url(_PAGE) == "xdg-open"
    assert opener.argvs == [["xdg-open", _PAGE]]
    assert opener.timeouts == [viewer.OPEN_WAIT_S]


def test_mac_hands_the_page_to_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "darwin")
    opener = _opener(monkeypatch, 0)
    assert viewer.launch_url(_PAGE) == "open"
    assert opener.argvs == [["open", _PAGE]]


def test_windows_hands_the_page_to_startfile(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "win32")
    started: list[str] = []
    monkeypatch.setattr(viewer, "_startfile", started.append)
    assert viewer.launch_url(_PAGE) == "os.startfile"
    assert started == [_PAGE]


def test_windows_without_a_default_browser_found_no_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "win32")
    monkeypatch.setattr(viewer, "_startfile", _no_association)
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_url(_PAGE)
    assert (caught.value.reason, caught.value.command) == ("no-app", "os.startfile")


def test_an_opener_that_takes_no_page_found_no_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _desktop_linux(monkeypatch)
    _opener(monkeypatch, 3)
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_url(_PAGE)
    assert (caught.value.reason, caught.value.command) == ("no-app", "xdg-open")
    assert str(caught.value) == (
        "No app opened the link (xdg-open exited 3). Set a default browser."
    )


def test_a_browser_opener_that_will_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _desktop_linux(monkeypatch)
    _opener(monkeypatch, error=PermissionError(13, "Permission denied"))
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_url(_PAGE)
    assert (caught.value.reason, caught.value.command) == ("not-started", "xdg-open")


def test_a_page_without_a_display_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "linux", "xdg-open")
    opener = _opener(monkeypatch, 0)
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_url(_PAGE)
    assert (caught.value.reason, caught.value.command) == ("not-started", "xdg-open")
    assert opener.argvs == []


def test_a_page_without_a_display_goes_to_wslview(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    _host(monkeypatch, "linux", "xdg-open", "wslview")
    opener = _opener(monkeypatch, 0)
    assert viewer.launch_url(_PAGE) == "wslview"
    assert opener.argvs == [["/usr/bin/wslview", _PAGE]]


def test_a_page_with_no_opener_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, DISPLAY=":0")
    _host(monkeypatch, "linux")
    opener = _opener(monkeypatch, 0)
    with pytest.raises(viewer.OpenFailed) as caught:
        viewer.launch_url(_PAGE)
    assert caught.value.reason == "not-started"
    assert opener.argvs == []
