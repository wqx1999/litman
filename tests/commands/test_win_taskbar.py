"""The portable half of the win32 taskbar branding (litman.commands._win_taskbar).

The COM half needs a real Windows shell; what a POSIX host can hold to
account is the ABI layout the COM calls depend on (an undersized PROPVARIANT
is silent stack corruption, not an error message) and the window-claiming
predicate, pinned to AppUserModelIDs measured on a real Win11 taskbar."""

from __future__ import annotations

import ctypes

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
