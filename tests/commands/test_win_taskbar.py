"""The portable half of the win32 taskbar branding (litman.commands._win_taskbar).

The COM half needs a real Windows shell; what a POSIX host can hold to
account is the ABI layout the COM calls depend on (an undersized PROPVARIANT
is silent stack corruption, not an error message), the window-claiming
predicate, pinned to AppUserModelIDs measured on a real Win11 taskbar — and
the whole _brand_pass / adopt_window control flow, driven through the module's
COM seams by _FakeShell below. This file is the only guard that flow has."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

from litman.commands import _win_taskbar as wt


def test_propvariant_matches_the_com_abi_size() -> None:
    # 2-byte type tag + three reserved words + 16-byte union. COM writes the
    # whole thing into the caller's buffer regardless of which arm is used.
    assert ctypes.sizeof(wt._PROPVARIANT) == 8 + 2 * ctypes.sizeof(ctypes.c_void_p)


def test_propertykey_matches_the_com_abi_size() -> None:
    # GUID (16) + 32-bit property id. c_ulong would inflate both fields to 8
    # bytes under LP64 — the layout must hold on every host the tests run on.
    assert ctypes.sizeof(wt._PROPERTYKEY) == 20
    assert ctypes.sizeof(wt._GUID) == 16


def test_guid_parses_into_little_endian_fields() -> None:
    guid = wt._guid(wt._PKEY_APPUSERMODEL)
    assert guid.data1 == 0x9F4C2855
    assert guid.data2 == 0x9F79
    assert guid.data3 == 0x4B39
    assert bytes(guid.data4) == bytes.fromhex("A8D0E1D42DE1D5F3")


def test_aumid_match_claims_ours_and_only_ours() -> None:
    # Measured on Win11 (2026-08-04): litman's window under Edge carries the
    # first value; the rest are the neighbours the match must never claim.
    assert wt._is_litman_aumid("MSEdge.127.0.0.1_/.browserprole.Default")
    # Same window under another browser: only the BaseAppId changes.
    assert wt._is_litman_aumid("Chrome.127.0.0.1_/.browserprole.Default")
    # The browser's own windows.
    assert not wt._is_litman_aumid("MSEdge")
    assert not wt._is_litman_aumid("Chrome")
    # A stranger's --app window on the browser's default profile: no
    # trailing dot — the dedicated-profile suffix is what marks a window as
    # ours, and litman always launches with its own --user-data-dir.
    assert not wt._is_litman_aumid("MSEdge.127.0.0.1_/")
    assert not wt._is_litman_aumid(None)
    assert not wt._is_litman_aumid("")


# ---------------------------------------------------------------------------
# _brand_pass / adopt_window control flow, through the COM seams
# ---------------------------------------------------------------------------

_OURS = "MSEdge.127.0.0.1_/.browserprole.Default"
_ICON = r"C:\litman\litman.ico"
_CMD = r"C:\tools\litw.exe gui --window"


class _FakeShell:
    """The COM seams _brand_pass drives, wired to an in-memory window table.

    Windows are per-hwnd dicts of pid -> value; the "store" handed out is the
    hwnd itself (the patched seams take it opaquely). Every write, Commit and
    taskbar re-add lands in one ordered event log, because the order *is* the
    contract under test: the display name doubles as the done-marker, so it
    must be the last write to land.
    """

    def __init__(self, windows: dict[int, dict[int, str]]) -> None:
        self.windows = windows
        self.events: list[tuple[object, ...]] = []
        self.fail_once: set[tuple[int, int]] = set()

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(wt, "_visible_windows", lambda: list(self.windows))
        monkeypatch.setattr(wt, "_window_store", lambda hwnd: hwnd)
        monkeypatch.setattr(
            wt, "_read_string", lambda store, pid: self.windows[store].get(pid)
        )
        monkeypatch.setattr(wt, "_write_string", self._write)
        monkeypatch.setattr(wt, "_com_method", self._com_method)
        monkeypatch.setattr(wt, "_release", lambda store: None)
        monkeypatch.setattr(
            wt, "_readd_taskbar_tab", lambda hwnd: self.events.append(("readd", hwnd))
        )

    def _write(self, store: int, pid: int, text: str) -> bool:
        if (store, pid) in self.fail_once:
            self.fail_once.discard((store, pid))
            self.events.append(("write-failed", store, pid))
            return False
        self.windows[store][pid] = text
        self.events.append(("write", store, pid))
        return True

    def _com_method(self, store: int, slot: int, *argtypes: object):
        # _release is patched out above, so the only direct _com_method call
        # left in _brand_pass is Commit (slot 7).
        assert slot == 7, f"unexpected vtable slot {slot}"
        return lambda: self.events.append(("commit", store)) or 0


def _fake_ctypes() -> SimpleNamespace:
    """Just enough of ctypes for adopt_window's CoInitialize on POSIX."""
    return SimpleNamespace(
        windll=SimpleNamespace(ole32=SimpleNamespace(CoInitialize=lambda p: 0))
    )


def test_brand_pass_never_touches_a_strangers_window(monkeypatch) -> None:
    # The browser's own window and an unstamped one: zero writes, zero blinks
    # — rewriting a stranger's relaunch properties would hijack its button.
    shell = _FakeShell({11: {wt._PID_ID: "MSEdge"}, 12: {}})
    shell.install(monkeypatch)

    assert wt._brand_pass(_ICON, _CMD) is False
    assert shell.events == []


def test_brand_pass_skips_a_branded_window_without_counting_it(monkeypatch) -> None:
    # Already wearing the marker: no rewrite, no taskbar re-add (the blink is
    # user-visible), and no success either — it is not this pass's work.
    shell = _FakeShell(
        {7: {wt._PID_ID: _OURS, wt._PID_RELAUNCH_DISPLAY_NAME: wt._DISPLAY_NAME}}
    )
    shell.install(monkeypatch)

    assert wt._brand_pass(_ICON, _CMD) is False
    assert shell.events == []


def test_adopt_window_keeps_waiting_past_anothers_branded_window(
    monkeypatch,
) -> None:
    # The double-launch trap: instance two sees instance one's branded window.
    # Claiming it as success would end the polling before instance two's own
    # window exists — so only the deadline ends it, and it reports False.
    shell = _FakeShell(
        {7: {wt._PID_ID: _OURS, wt._PID_RELAUNCH_DISPLAY_NAME: wt._DISPLAY_NAME}}
    )
    shell.install(monkeypatch)
    monkeypatch.setattr(wt, "ctypes", _fake_ctypes())

    assert wt.adopt_window(_ICON, _CMD, timeout=0.05, poll=0.01) is False
    assert shell.events == []


def test_brand_pass_writes_the_marker_last_then_commits_then_readds(
    monkeypatch,
) -> None:
    shell = _FakeShell({5: {wt._PID_ID: "Chrome.127.0.0.1_/.browserprole.Default"}})
    shell.install(monkeypatch)

    assert wt._brand_pass(_ICON, _CMD) is True
    # Full order pinned, not just membership: command and icon first, the
    # display name (the done-marker) dead last, Commit after all three, and
    # the taskbar re-add only after the Commit that makes it worth a blink.
    assert shell.events == [
        ("write", 5, wt._PID_RELAUNCH_COMMAND),
        ("write", 5, wt._PID_RELAUNCH_ICON),
        ("write", 5, wt._PID_RELAUNCH_DISPLAY_NAME),
        ("commit", 5),
        ("readd", 5),
    ]
    assert shell.windows[5][wt._PID_RELAUNCH_ICON] == f"{_ICON},0"
    assert shell.windows[5][wt._PID_RELAUNCH_COMMAND] == _CMD


def test_brand_pass_partial_failure_leaves_no_marker_and_retries(
    monkeypatch,
) -> None:
    # A failed icon write short-circuits: no Commit, no blink, and — because
    # the marker is written last — no marker, so the next pass retries the
    # same window instead of trusting a half-branded one.
    shell = _FakeShell({5: {wt._PID_ID: _OURS}})
    shell.fail_once.add((5, wt._PID_RELAUNCH_ICON))
    shell.install(monkeypatch)

    assert wt._brand_pass(_ICON, _CMD) is False
    assert shell.events == [
        ("write", 5, wt._PID_RELAUNCH_COMMAND),
        ("write-failed", 5, wt._PID_RELAUNCH_ICON),
    ]
    assert wt._PID_RELAUNCH_DISPLAY_NAME not in shell.windows[5]

    shell.events.clear()
    assert wt._brand_pass(_ICON, _CMD) is True
    assert shell.events == [
        ("write", 5, wt._PID_RELAUNCH_COMMAND),
        ("write", 5, wt._PID_RELAUNCH_ICON),
        ("write", 5, wt._PID_RELAUNCH_DISPLAY_NAME),
        ("commit", 5),
        ("readd", 5),
    ]


def test_adopt_window_gives_up_before_the_first_scan(monkeypatch) -> None:
    # give_up is the server's stop event: a shutdown that beat the window to
    # existence must not spend even one EnumWindows sweep on cosmetics.
    passes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wt, "_brand_pass", lambda icon, cmd: passes.append((icon, cmd)) or True
    )
    monkeypatch.setattr(wt, "ctypes", _fake_ctypes())

    assert wt.adopt_window(_ICON, _CMD, give_up=lambda: True) is False
    assert passes == []


def test_adopt_window_deadline_expires_after_scanning(monkeypatch) -> None:
    # A window that never appears: the poll must both actually look and
    # actually stop — at least one sweep, then False at the deadline.
    passes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wt, "_brand_pass", lambda icon, cmd: passes.append((icon, cmd)) or False
    )
    monkeypatch.setattr(wt, "ctypes", _fake_ctypes())

    assert wt.adopt_window(_ICON, _CMD, timeout=0.0, poll=0.0) is False
    assert len(passes) >= 1
