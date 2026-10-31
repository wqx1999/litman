"""Opening a desktop-app agent at the vault (task-zcode-agent).

The platform is faked through ``app_launch._platform`` — never ``sys.platform``,
which is the whole interpreter's — and the registry, the app folders,
``xdg-mime`` and the process starters through the module's other seams, so
every platform's branch runs on every CI runner. No real app is started.
"""

from __future__ import annotations

import ctypes
import dataclasses
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from litman.core import agents, app_launch

ZCODE = agents.get_agent("zcode")
CLAUDE = agents.get_agent("claude")
# The conftest stubs these probes for every test; captured at import, before it.
_REAL_XDG_DEFAULT_HANDLER = app_launch._xdg_default_handler
_REAL_SCHEME_HAS_HANDLER = app_launch._scheme_has_handler


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Record every process / opener ``open_app`` would start."""
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        app_launch,
        "_popen_detached_windows",
        lambda argv, env: calls.append(("popen", (argv, env))),
    )
    monkeypatch.setattr(app_launch, "_spawn", lambda argv: calls.append(("spawn", argv)))
    monkeypatch.setattr(
        app_launch,
        "_open_link_detached_windows",
        lambda url: calls.append(("link", url)),
    )
    return calls


def _on(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(app_launch, "_platform", lambda: platform)


def _registered_exe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, exists: bool = True
) -> Path:
    """Fake the registry naming an exe under a folder with a space in it."""
    exe = tmp_path / "Program Files" / "ZCode" / "ZCode.exe"
    if exists:
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
    monkeypatch.setattr(
        app_launch, "_read_scheme_command", lambda _scheme: f'"{exe}" -- "%1"'
    )
    return exe


# ---------------------------------------------------------------------------
# The registry's open command → the exe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "exe"),
    [
        (r'"D:\Zcode\ZCode.exe" -- "%1"', r"D:\Zcode\ZCode.exe"),
        (
            r'"C:\Program Files\ZCode\ZCode.exe" -- "%1"',
            r"C:\Program Files\ZCode\ZCode.exe",
        ),
        (r'D:\Zcode\ZCode.exe -- "%1"', r"D:\Zcode\ZCode.exe"),
        (
            r'C:\Program Files\ZCode\ZCode.exe -- "%1"',
            r"C:\Program Files\ZCode\ZCode.exe",
        ),
        (r"D:\Zcode\ZCODE.EXE", r"D:\Zcode\ZCODE.EXE"),
        (r'D:\my.exes\ZCode.exe -- "%1"', r"D:\my.exes\ZCode.exe"),
        ("", None),
        (None, None),
        (r'D:\Zcode\ZCode -- "%1"', None),
        (r'"D:\Zcode\ZCode" -- "%1"', None),
    ],
)
def test_exe_from_open_command(command: str | None, exe: str | None) -> None:
    assert app_launch.exe_from_open_command(command) == exe


def test_windows_exe_is_the_registered_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    exe = tmp_path / "Program Files" / "ZCode" / "ZCode.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")

    def read(scheme: str) -> str:
        asked.append(scheme)
        return f'"{exe}" -- "%1"'

    monkeypatch.setattr(app_launch, "_read_scheme_command", read)
    assert app_launch.windows_exe(ZCODE) == exe
    assert asked == [ZCODE.url_scheme]


def test_windows_exe_missing_file_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registered_exe(monkeypatch, tmp_path, exists=False)
    assert app_launch.windows_exe(ZCODE) is None


def test_windows_exe_without_registry_key_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed but never opened: the app has not written its key yet."""
    monkeypatch.setattr(app_launch, "_read_scheme_command", lambda _scheme: None)
    assert app_launch.windows_exe(ZCODE) is None


# ---------------------------------------------------------------------------
# The vault link
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "folder", ["lit vault", "文献库", "a&b", "c#d", "e%20f", "g+h", "x?y=z"]
)
def test_workspace_url_round_trips_the_vault_path(
    tmp_path: Path, folder: str
) -> None:
    vault = tmp_path / folder
    url = app_launch.workspace_url(ZCODE, vault)
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        ZCODE.url_scheme,
        "workspace",
        "/open",
    )
    assert parts.fragment == ""
    assert parse_qs(parts.query) == {"path": [str(vault)]}


# ---------------------------------------------------------------------------
# Installed or not, per platform
# ---------------------------------------------------------------------------


def test_installed_on_windows_needs_the_registered_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, "win32")
    _registered_exe(monkeypatch, tmp_path)
    assert app_launch.app_installed(ZCODE) is True

    monkeypatch.setattr(app_launch, "_read_scheme_command", lambda _scheme: None)
    assert app_launch.app_installed(ZCODE) is False


@pytest.mark.parametrize("where", ["system", "user"])
def test_installed_on_macos_finds_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    _on(monkeypatch, "darwin")
    dirs = {"system": tmp_path / "Applications", "user": tmp_path / "home" / "Applications"}
    monkeypatch.setattr(app_launch, "_app_dirs", lambda: list(dirs.values()))
    assert app_launch.app_installed(ZCODE) is False

    (dirs[where] / ZCODE.mac_bundle).mkdir(parents=True)
    assert app_launch.app_installed(ZCODE) is True


def test_installed_on_linux_asks_xdg_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "linux")
    asked: list[str] = []

    def handler(scheme: str) -> str:
        asked.append(scheme)
        return "zcode-handler.desktop"

    monkeypatch.setattr(app_launch, "_xdg_default_handler", handler)
    assert app_launch.app_installed(ZCODE) is True
    assert asked == [ZCODE.url_scheme]

    monkeypatch.setattr(app_launch, "_xdg_default_handler", lambda _scheme: "")
    assert app_launch.app_installed(ZCODE) is False


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every XDG base dir at an empty tmp dir."""
    roots = {
        "config_home": tmp_path / "config",
        "config_dir": tmp_path / "etc-xdg",
        "data_home": tmp_path / "local-share",
        "data_dir": tmp_path / "usr-share",
    }
    monkeypatch.setenv("XDG_CONFIG_HOME", str(roots["config_home"]))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(roots["config_dir"]))
    monkeypatch.setenv("XDG_DATA_HOME", str(roots["data_home"]))
    monkeypatch.setenv("XDG_DATA_DIRS", str(roots["data_dir"]))
    return roots


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_xdg_no_mime_files_is_no_handler(xdg: dict[str, Path]) -> None:
    assert _REAL_XDG_DEFAULT_HANDLER("zcode") == ""


def test_xdg_user_default_is_the_handler(xdg: dict[str, Path]) -> None:
    """What the app's own `xdg-mime default` writes on first run."""
    _write(
        xdg["config_home"] / "mimeapps.list",
        "[Default Applications]\n"
        "text/html=firefox.desktop\n"
        "x-scheme-handler/zcode=zcode.desktop\n",
    )
    assert _REAL_XDG_DEFAULT_HANDLER("zcode") == "zcode.desktop"


def test_xdg_package_cache_is_the_handler(xdg: dict[str, Path]) -> None:
    """What a package install leaves: a system mimeinfo.cache entry."""
    _write(
        xdg["data_dir"] / "applications" / "mimeinfo.cache",
        "[MIME Cache]\nx-scheme-handler/zcode=zcode.desktop;other.desktop;\n",
    )
    assert _REAL_XDG_DEFAULT_HANDLER("zcode") == "zcode.desktop"


@pytest.mark.parametrize(
    "text",
    [
        "[Default Applications]\nx-scheme-handler/zcodex=other.desktop\n",
        "[Default Applications]\nx-scheme-handler/zcode=\n",
        "[Removed Associations]\nx-scheme-handler/zcode=zcode.desktop\n",
        "x-scheme-handler/zcode=zcode.desktop\n",
    ],
    ids=["other-scheme", "empty", "removed-section", "no-section"],
)
def test_xdg_entries_that_name_no_handler(xdg: dict[str, Path], text: str) -> None:
    _write(xdg["config_home"] / "mimeapps.list", text)
    assert _REAL_XDG_DEFAULT_HANDLER("zcode") == ""


def test_xdg_unreadable_file_is_skipped(xdg: dict[str, Path]) -> None:
    (xdg["config_home"] / "mimeapps.list").mkdir(parents=True)  # not a file
    _write(
        xdg["data_home"] / "applications" / "mimeapps.list",
        "[Added Associations]\nx-scheme-handler/zcode=zcode.desktop;\n",
    )
    assert _REAL_XDG_DEFAULT_HANDLER("zcode") == "zcode.desktop"


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("missing", ["url_scheme", "mac_bundle"])
def test_missing_catalog_fields_never_read_as_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, missing: str
) -> None:
    """Whatever the probes say, a row without the field a platform needs is
    not installed — never report a missing app as present."""
    _on(monkeypatch, platform)
    _registered_exe(monkeypatch, tmp_path)
    apps = tmp_path / "Applications"
    (apps / ZCODE.mac_bundle).mkdir(parents=True)
    monkeypatch.setattr(app_launch, "_app_dirs", lambda: [apps])
    monkeypatch.setattr(app_launch, "_xdg_default_handler", lambda _s: "x.desktop")
    spec = dataclasses.replace(ZCODE, **{missing: None})
    expected = missing == "mac_bundle" and platform != "darwin"
    assert app_launch.app_installed(spec) is expected


# ---------------------------------------------------------------------------
# Opening the app
# ---------------------------------------------------------------------------


def test_windows_starts_the_exe_at_the_vault_without_a_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "win32")
    exe = _registered_exe(monkeypatch, tmp_path)
    vault = tmp_path / "lit 测试 库"

    app_launch.open_app(ZCODE, vault)

    assert len(started) == 1
    kind, (argv, env) = started[0]
    assert kind == "popen"
    assert argv == [str(exe), "--open-workspace", str(vault)]
    assert env["ELECTRON_NO_ATTACH_CONSOLE"] == "1"
    assert env["PATH"] == os.environ["PATH"]


def test_windows_detached_start_gives_the_app_no_console_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real seam, with only Popen stubbed: valid NUL handles on all three
    streams (Electron then never attaches to the parent's console), detached,
    its own process group, no shell."""
    seen: list[tuple[object, dict]] = []
    monkeypatch.setattr(
        app_launch.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw))
    )
    env = {"ELECTRON_NO_ATTACH_CONSOLE": "1"}
    app_launch._popen_detached_windows(["C:/ZCode/ZCode.exe", "--x"], env)

    [(argv, kw)] = seen
    assert argv == ["C:/ZCode/ZCode.exe", "--x"]
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
    assert kw["env"] is env
    assert kw["creationflags"] & 0x00000008  # DETACHED_PROCESS
    assert kw["creationflags"] & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    assert not kw.get("shell")


def test_windows_without_an_arg_template_opens_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "win32")
    monkeypatch.setattr(app_launch, "_scheme_has_handler", lambda _scheme: True)
    spec = dataclasses.replace(ZCODE, app_args=())
    vault = tmp_path / "vault"

    app_launch.open_app(spec, vault)

    assert started == [("link", app_launch.workspace_url(spec, vault))]


def test_macos_opens_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "darwin")
    (tmp_path / ZCODE.mac_bundle).mkdir()
    monkeypatch.setattr(app_launch, "_app_dirs", lambda: [tmp_path])
    vault = tmp_path / "vault"

    app_launch.open_app(ZCODE, vault)

    assert started == [("spawn", ["open", app_launch.workspace_url(ZCODE, vault)])]


@pytest.mark.parametrize("display", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_linux_with_a_display_opens_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list, display: str
) -> None:
    _on(monkeypatch, "linux")
    monkeypatch.setattr(app_launch, "_xdg_default_handler", lambda _s: "x.desktop")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv(display, ":0")
    vault = tmp_path / "vault"

    app_launch.open_app(ZCODE, vault)

    assert started == [("spawn", ["xdg-open", app_launch.workspace_url(ZCODE, vault)])]


def test_linux_without_a_display_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "linux")
    monkeypatch.setattr(app_launch, "_xdg_default_handler", lambda _s: "x.desktop")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    with pytest.raises(app_launch.AppNoDisplay, match="lit agent zcode"):
        app_launch.open_app(ZCODE, tmp_path / "vault")
    assert started == []


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_not_installed_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list, platform: str
) -> None:
    _on(monkeypatch, platform)
    monkeypatch.setattr(app_launch, "_read_scheme_command", lambda _s: None)
    monkeypatch.setattr(app_launch, "_app_dirs", lambda: [tmp_path])
    monkeypatch.setattr(app_launch, "_xdg_default_handler", lambda _s: "")
    monkeypatch.setenv("DISPLAY", ":0")

    with pytest.raises(app_launch.AppNotFound) as exc_info:
        app_launch.open_app(ZCODE, tmp_path / "vault")
    message = str(exc_info.value)
    assert "ZCode isn't installed" in message
    assert "hasn't been opened yet" in message
    assert len(message) <= 120
    assert started == []


def test_a_start_that_fails_reads_as_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, "darwin")
    (tmp_path / ZCODE.mac_bundle).mkdir()
    monkeypatch.setattr(app_launch, "_app_dirs", lambda: [tmp_path])

    def broken(_argv: list[str]) -> None:
        raise OSError("no such file: open")

    monkeypatch.setattr(app_launch, "_spawn", broken)
    with pytest.raises(app_launch.AppNotFound, match="no such file: open"):
        app_launch.open_app(ZCODE, tmp_path / "vault")


# ---------------------------------------------------------------------------
# DeepSeek Harness: no exe arguments, and a link without the vault in it
# ---------------------------------------------------------------------------

DSH = agents.get_agent("dsh")


def _registered_dsh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The open command DSH writes for its link on its first start."""
    exe = tmp_path / "dsh" / "DeepSeek Harness.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(
        app_launch,
        "_read_scheme_command",
        lambda scheme: f'"{exe}" "%1"' if scheme == "dsh" else None,
    )
    return exe


def test_dsh_on_windows_starts_its_exe_with_the_bare_link_without_a_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    """Registered, so installed; its exe gets the link as the registry's "%1"
    would, through the start that keeps the app out of our console (handed to
    the system opener, it logs there). The vault goes nowhere: DSH reopens its
    last session."""
    _on(monkeypatch, "win32")
    exe = _registered_dsh(monkeypatch, tmp_path)
    assert app_launch.app_installed(DSH) is True

    app_launch.open_app(DSH, tmp_path / "lit 测试 库")

    assert len(started) == 1
    kind, (argv, env) = started[0]
    assert kind == "popen"
    assert argv == [str(exe), "dsh://open"]
    assert env["ELECTRON_NO_ATTACH_CONSOLE"] == "1"


def test_dsh_on_windows_never_opened_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    """DSH registers its link on its first start; until then there is none."""
    _on(monkeypatch, "win32")
    monkeypatch.setattr(app_launch, "_read_scheme_command", lambda _scheme: None)
    assert app_launch.app_installed(DSH) is False

    with pytest.raises(app_launch.AppNotFound) as exc_info:
        app_launch.open_app(DSH, tmp_path / "vault")
    assert str(exc_info.value) == (
        "DeepSeek Harness isn't installed, or hasn't been opened yet. "
        "Open it once, then try again."
    )
    assert started == []


def test_dsh_on_macos_is_its_bundle_opened_by_its_bare_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "darwin")
    monkeypatch.setattr(app_launch, "_app_dirs", lambda: [tmp_path])
    assert app_launch.app_installed(DSH) is False

    (tmp_path / "DeepSeek Harness.app").mkdir()
    assert app_launch.app_installed(DSH) is True

    app_launch.open_app(DSH, tmp_path / "vault")

    assert started == [("spawn", ["open", "dsh://open"])]


def test_dsh_on_linux_is_not_installed(
    xdg: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started: list,
) -> None:
    """DSH ships no Linux desktop app, so nothing handles its link."""
    _on(monkeypatch, "linux")
    monkeypatch.setattr(app_launch, "_xdg_default_handler", _REAL_XDG_DEFAULT_HANDLER)
    monkeypatch.setenv("DISPLAY", ":0")
    assert app_launch.app_installed(DSH) is False

    with pytest.raises(app_launch.AppNotFound, match="hasn't been opened yet"):
        app_launch.open_app(DSH, tmp_path / "vault")
    assert started == []


def test_only_an_app_whose_link_or_arguments_name_the_vault_opens_at_it() -> None:
    """What `lit agent` may claim: "at <vault>" only when the vault is passed on."""
    assert app_launch.opens_at_vault(ZCODE) is True
    assert app_launch.opens_at_vault(DSH) is False
    assert app_launch.opens_at_vault(dataclasses.replace(DSH, app_args=("{vault}",)))
    assert app_launch.opens_at_vault(
        dataclasses.replace(DSH, open_url="dsh://open?path={vault}")
    )


# ---------------------------------------------------------------------------
# A packaged app on Windows: no exe to find or start (task-agent-launch-modes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("handled", [True, False])
def test_windows_row_without_args_is_installed_when_its_link_has_a_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handled: bool
) -> None:
    """The handler decides, whatever the registry's open command says."""
    _on(monkeypatch, "win32")
    _registered_exe(monkeypatch, tmp_path)
    asked: list[str] = []

    def has_handler(scheme: str) -> bool:
        asked.append(scheme)
        return handled

    monkeypatch.setattr(app_launch, "_scheme_has_handler", has_handler)
    assert app_launch.app_installed(CLAUDE) is handled
    assert asked == ["claude"]


@pytest.mark.parametrize("name", ["zcode", "dsh"])
def test_windows_row_with_args_still_needs_the_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _on(monkeypatch, "win32")
    monkeypatch.setattr(
        app_launch,
        "_scheme_has_handler",
        lambda _scheme: pytest.fail("a row with app_args goes by its exe"),
    )
    spec = agents.get_agent(name)
    assert app_launch.app_installed(spec) is False

    _registered_exe(monkeypatch, tmp_path)
    assert app_launch.app_installed(spec) is True


class _FakeShlwapi:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def AssocQueryStringW(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


@pytest.mark.parametrize(
    ("result", "handled"),
    # 0x80070483, ERROR_NO_ASSOCIATION as ctypes hands it back: a signed int.
    [(0, True), (0x80070483 - 2**32, False)],
)
def test_scheme_handler_probe_is_the_display_name_call(
    monkeypatch: pytest.MonkeyPatch, result: int, handled: bool
) -> None:
    shlwapi = _FakeShlwapi(result)
    loaded: list[str] = []

    def win_dll(name: str) -> _FakeShlwapi:
        loaded.append(name)
        return shlwapi

    monkeypatch.setattr(ctypes, "WinDLL", win_dll, raising=False)
    assert _REAL_SCHEME_HAS_HANDLER("claude") is handled
    assert loaded == ["shlwapi"]
    [(flags, which, scheme, extra, _out, _size)] = shlwapi.calls
    assert (flags, which, extra) == (0, 4, None)
    assert cast(ctypes.c_wchar_p, scheme).value == "claude"


def test_scheme_handler_probe_that_fails_reads_as_no_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_name: str) -> object:
        raise OSError("no such library")

    monkeypatch.setattr(ctypes, "WinDLL", broken, raising=False)
    assert _REAL_SCHEME_HAS_HANDLER("claude") is False


def test_scheme_handler_real_probe_finds_no_handler_for_a_made_up_scheme() -> None:
    """Un-stubbed: off Windows there is no WinDLL, on Windows no such scheme."""
    assert _REAL_SCHEME_HAS_HANDLER("litman-no-such-scheme") is False


def test_scheme_handler_real_probe_answers_for_a_registered_scheme() -> None:
    """The control for the test above: the real call can answer yes, so its
    no is not a broken ctypes signature. The scheme is registered here, as the
    runner's own associations are not something to count on."""
    if sys.platform != "win32":
        pytest.skip("Win32 API, Windows only")
    import winreg

    scheme = f"litman-test-{os.getpid()}"
    command = rf"Software\Classes\{scheme}\shell\open\command"
    assert _REAL_SCHEME_HAS_HANDLER(scheme) is False
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, command) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{sys.executable}" "%1"')
    try:
        result = app_launch._handler_name_result(scheme)
        assert result == 0, f"HRESULT {result}"
        assert _REAL_SCHEME_HAS_HANDLER(scheme) is True
    finally:
        # Leaf first: DeleteKey refuses a key that has subkeys.
        parts = command.split("\\")
        for depth in range(len(parts), 2, -1):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, "\\".join(parts[:depth]))
    assert _REAL_SCHEME_HAS_HANDLER(scheme) is False


def test_diag_print_handler_results(capsys: pytest.CaptureFixture[str]) -> None:
    """TEMPORARY: prints the raw results into the CI log, then gets removed."""
    with capsys.disabled():
        for name in (".txt", ".exe", ".bat", ".html", "http", "https", "mailto", "ms-settings", "litman-none"):
            result = app_launch._handler_name_result(name)
            shown = None if result is None else hex(result & 0xFFFFFFFF)
            print(f"\nLITMAN-DIAG {name} {shown}")


def test_windows_opens_a_packaged_app_by_its_link_through_the_short_lived_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "win32")
    monkeypatch.setattr(app_launch, "_scheme_has_handler", lambda _scheme: True)
    vault = tmp_path / "lit 测试 库"

    app_launch.open_app(CLAUDE, vault)

    assert started == [("link", app_launch.workspace_url(CLAUDE, vault))]


def test_windows_packaged_app_not_installed_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "win32")
    with pytest.raises(app_launch.AppNotFound, match="Claude Code isn't installed"):
        app_launch.open_app(CLAUDE, tmp_path / "vault")
    assert started == []


def test_short_lived_child_is_exactly_this_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real seam, with only Popen stubbed. Each part is what kept a
    measured cold start out of the console: a separate python, NUL on all
    three streams, detached."""
    seen: list[tuple[object, dict]] = []
    monkeypatch.setattr(
        app_launch.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw))
    )
    url = "claude://code/new?folder=C%3A%5Clib"

    app_launch._open_link_detached_windows(url)

    assert seen == [
        (
            [sys.executable, "-c", "import os,sys; os.startfile(sys.argv[1])", url],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "creationflags": 0x00000008 | 0x00000200,
                "close_fds": True,
            },
        )
    ]


def test_short_lived_child_that_cannot_start_reads_as_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, "win32")
    monkeypatch.setattr(app_launch, "_scheme_has_handler", lambda _scheme: True)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("no python here")

    monkeypatch.setattr(app_launch.subprocess, "Popen", broken)
    with pytest.raises(app_launch.AppNotFound, match="no python here"):
        app_launch.open_app(CLAUDE, tmp_path / "vault")


def test_claude_on_macos_is_its_bundle_opened_by_its_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "darwin")
    monkeypatch.setattr(app_launch, "_app_dirs", lambda: [tmp_path])
    assert app_launch.app_installed(CLAUDE) is False

    (tmp_path / "Claude.app").mkdir()
    assert app_launch.app_installed(CLAUDE) is True

    vault = tmp_path / "vault"
    app_launch.open_app(CLAUDE, vault)
    assert started == [("spawn", ["open", app_launch.workspace_url(CLAUDE, vault)])]


def test_claude_on_linux_is_its_link_handler_opened_by_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list
) -> None:
    _on(monkeypatch, "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(app_launch, "_xdg_default_handler", lambda _s: "claude.desktop")
    vault = tmp_path / "vault"

    app_launch.open_app(CLAUDE, vault)

    assert started == [("spawn", ["xdg-open", app_launch.workspace_url(CLAUDE, vault)])]


@pytest.mark.parametrize(
    ("vault", "encoded"),
    [
        (r"C:\lib", "C%3A%5Clib"),
        (r"C:\Users\Wang Q\My Lib", "C%3A%5CUsers%5CWang%20Q%5CMy%20Lib"),
        (r"D:\文献库", "D%3A%5C%E6%96%87%E7%8C%AE%E5%BA%93"),
    ],
)
def test_claude_link_encodes_a_windows_vault_path(vault: str, encoded: str) -> None:
    url = app_launch.workspace_url(CLAUDE, cast(Path, PureWindowsPath(vault)))
    assert url == f"claude://code/new?folder={encoded}"
    assert parse_qs(urlsplit(url).query) == {"folder": [vault]}


@pytest.mark.parametrize(
    ("platform", "ssh", "display", "reaches"),
    [
        ("win32", None, None, True),
        ("darwin", None, None, True),
        ("linux", None, ":0", True),
        ("linux", None, None, False),
        ("win32", "SSH_CONNECTION", None, False),
        ("darwin", "SSH_CLIENT", None, False),
        ("linux", "SSH_CONNECTION", ":0", False),
    ],
)
def test_app_reaches_user(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    ssh: str | None,
    display: str | None,
    reaches: bool,
) -> None:
    _on(monkeypatch, platform)
    for variable in ("SSH_CONNECTION", "SSH_CLIENT", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(variable, raising=False)
    if ssh:
        monkeypatch.setenv(ssh, "1.2.3.4 5 6.7.8.9 22")
    if display:
        monkeypatch.setenv("DISPLAY", display)
    assert app_launch.app_reaches_user() is reaches
