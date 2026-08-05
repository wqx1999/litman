"""Windows taskbar identity for the ``--window`` app window.

Chromium stamps every ``--app=`` window with an AppUserModelID of its own —
``<browser>.127.0.0.1_/.<profile>`` for litman, measured on Win11 — so the
taskbar already gives litman a button separate from the user's browser
windows. What makes that button *look* like the browser is three further
properties Chromium writes onto the window: relaunch display name, icon and
command, all pointing at the browser binary. Rewriting those three is the
whole fix. The AppUserModelID itself stays untouched — the grouping is
already right, only the face is wrong.

Two measured constraints shape the code:

- The taskbar reads the relaunch properties when a window joins it and does
  not re-read them on Commit, so a plain rewrite changes nothing on screen.
  Removing the tab via ITaskbarList and adding it back forces the re-read
  (verified live: the icon swaps on the spot).
- The properties die with the window, so this runs on every launch, after
  the browser has created the window — hence the polling.

The window is recognised by its AppUserModelID containing ``.127.0.0.1_/.``:
the middle is Chromium's host+path app name for our URL (the port is not part
of it), and the trailing dot only appears when the window runs a dedicated
``--user-data-dir`` — ours always does, a stranger's app window on the
default profile never has one. One match covers all four supported browsers,
which spares us a per-browser BaseAppId table and Chromium's undocumented
trimming of the profile component (``browser-profile`` came back as
``browserprole``).

ctypes only: two COM interfaces of eight vtable slots each are not worth a
pywin32 dependency. Everything Windows-specific lives inside the functions,
so the module imports (and its ABI structs can be tested) anywhere.
"""

from __future__ import annotations

import ctypes
import time
import uuid
from collections.abc import Callable
from ctypes import POINTER, byref, c_int, c_ubyte, c_uint32, c_ushort, c_void_p

# PKEY_AppUserModel_* share one fmtid; the property id picks the member.
_PKEY_APPUSERMODEL = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
_PID_RELAUNCH_COMMAND = 2
_PID_RELAUNCH_ICON = 3
_PID_RELAUNCH_DISPLAY_NAME = 4
_PID_ID = 5

_IID_IPROPERTY_STORE = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"
_CLSID_TASKBAR_LIST = "56FDF344-FD6D-11D0-958A-006097C9A090"
_IID_ITASKBAR_LIST = "56FDF342-FD6D-11D0-958A-006097C9A090"

_VT_LPWSTR = 31
_CLSCTX_INPROC_SERVER = 0x1

_AUMID_MARK = ".127.0.0.1_/."
_DISPLAY_NAME = "litman"


class _GUID(ctypes.Structure):
    # DWORD is 32-bit on Windows regardless of pointer width; c_ulong would
    # be 8 bytes under LP64 and break the layout on the POSIX test hosts.
    _fields_ = [
        ("data1", c_uint32),
        ("data2", c_ushort),
        ("data3", c_ushort),
        ("data4", c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", c_uint32)]


class _PROPVARIANT(ctypes.Structure):
    # The real PROPVARIANT is a 2-byte type tag, three reserved words and a
    # 16-byte union; only the LPWSTR arm is ever touched here. The unused
    # fields exist to make sizeof() come out at the full 24 bytes — COM
    # writes a whole PROPVARIANT into the caller's buffer, and an undersized
    # one is silent stack corruption, not an error.
    _fields_ = [
        ("vt", c_ushort),
        ("_r1", c_ushort),
        ("_r2", c_ushort),
        ("_r3", c_ushort),
        ("p", c_void_p),
        ("_p2", c_void_p),
    ]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _is_litman_aumid(aumid: str | None) -> bool:
    return bool(aumid) and _AUMID_MARK in aumid


def _com_method(obj: c_void_p, slot: int, *argtypes: object) -> Callable[..., int]:
    """Bind vtable slot ``slot`` of ``obj`` as a callable returning HRESULT."""
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(c_int, c_void_p, *argtypes)
    bound = proto(vtable[slot])
    return lambda *args: bound(obj, *args)


def _release(obj: c_void_p) -> None:
    _com_method(obj, 2)()  # IUnknown slot 2


def _window_store(hwnd: int) -> c_void_p | None:
    store = c_void_p()
    iid = _guid(_IID_IPROPERTY_STORE)
    hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(
        c_void_p(hwnd), byref(iid), byref(store)
    )
    return store if hr == 0 and store.value else None


# IPropertyStore vtable: 0-2 IUnknown, 3 GetCount, 4 GetAt, 5 GetValue,
# 6 SetValue, 7 Commit.


def _read_string(store: c_void_p, pid: int) -> str | None:
    key = _PROPERTYKEY(_guid(_PKEY_APPUSERMODEL), pid)
    value = _PROPVARIANT()
    get_value = _com_method(store, 5, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT))
    if get_value(byref(key), byref(value)) != 0:
        return None
    try:
        if value.vt == _VT_LPWSTR and value.p:
            return ctypes.wstring_at(value.p)
        return None
    finally:
        ctypes.windll.ole32.PropVariantClear(byref(value))


def _write_string(store: c_void_p, pid: int, text: str) -> bool:
    key = _PROPERTYKEY(_guid(_PKEY_APPUSERMODEL), pid)
    # SetValue copies the variant during the call, so pointing it at a
    # Python-owned buffer is safe; no CoTaskMemAlloc needed.
    buffer = ctypes.create_unicode_buffer(text)
    value = _PROPVARIANT()
    value.vt = _VT_LPWSTR
    value.p = ctypes.cast(buffer, c_void_p)
    set_value = _com_method(store, 6, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT))
    return set_value(byref(key), byref(value)) == 0


def _visible_windows() -> list[int]:
    user32 = ctypes.windll.user32
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(c_int, c_void_p, c_void_p)

    def keep(hwnd: int | None, _lparam: object) -> int:
        if hwnd is not None and user32.IsWindowVisible(c_void_p(hwnd)):
            found.append(hwnd)
        return 1

    user32.EnumWindows(proto(keep), None)
    return found


def _readd_taskbar_tab(hwnd: int) -> None:
    """Pull the window off the taskbar and put it back.

    The only known way to make the taskbar re-read the relaunch properties
    of a window it already shows; costs one blink of the button.
    """
    taskbar = c_void_p()
    clsid = _guid(_CLSID_TASKBAR_LIST)
    iid = _guid(_IID_ITASKBAR_LIST)
    hr = ctypes.windll.ole32.CoCreateInstance(
        byref(clsid), None, _CLSCTX_INPROC_SERVER, byref(iid), byref(taskbar)
    )
    if hr != 0 or not taskbar.value:
        return
    try:
        # ITaskbarList vtable: 3 HrInit, 4 AddTab, 5 DeleteTab.
        if _com_method(taskbar, 3)() == 0:
            _com_method(taskbar, 5, c_void_p)(c_void_p(hwnd))
            _com_method(taskbar, 4, c_void_p)(c_void_p(hwnd))
    finally:
        _release(taskbar)


def _brand_pass(icon_path: str, relaunch_command: str) -> bool:
    """One sweep over the visible windows; True only when this pass branded one.

    A window already wearing the display name was finished by an earlier pass
    — possibly another instance's — so it is skipped without a write or a
    taskbar blink, but never counted as success: a second instance's own
    window is still on its way, and claiming the first one's would end the
    polling before it arrives."""
    branded = False
    for hwnd in _visible_windows():
        store = _window_store(hwnd)
        if store is None:
            continue
        ok = False
        try:
            if not _is_litman_aumid(_read_string(store, _PID_ID)):
                continue
            if _read_string(store, _PID_RELAUNCH_DISPLAY_NAME) == _DISPLAY_NAME:
                continue
            # Write order is load-bearing: the display name doubles as the
            # done-marker read above, so it lands last — a pass that dies on
            # the command or icon leaves no marker, and the next pass retries
            # the window instead of locking "done" onto the wrong face.
            ok = (
                _write_string(store, _PID_RELAUNCH_COMMAND, relaunch_command)
                and _write_string(store, _PID_RELAUNCH_ICON, f"{icon_path},0")
                and _write_string(store, _PID_RELAUNCH_DISPLAY_NAME, _DISPLAY_NAME)
                and _com_method(store, 7)() == 0  # Commit
            )
        finally:
            _release(store)
        if ok:
            _readd_taskbar_tab(hwnd)
            branded = True
    return branded


def adopt_window(
    icon_path: str,
    relaunch_command: str,
    *,
    give_up: Callable[[], bool] = lambda: False,
    timeout: float = 30.0,
    poll: float = 0.25,
) -> bool:
    """Poll for the app window and rebrand it. True once this call branded one.

    Polling, because the window exists only after the browser gets around to
    creating it — and Chromium stamps the AppUserModelID at creation, so an
    unstamped window simply fails the match this pass and is caught on a
    later one. Windows an earlier call already branded do not count: a second
    instance keeps polling for its *own* window until it appears or the
    deadline runs out (idle sweeps past a long-lived first window are the
    daemon thread's cheap price). ``give_up`` (the server's stop event) ends
    the wait early on shutdown. Best-effort by contract: the caller treats
    False the same as success minus the icon.
    """
    ctypes.windll.ole32.CoInitialize(None)  # per-thread; S_FALSE is fine
    deadline = time.monotonic() + timeout
    while not give_up():
        if _brand_pass(icon_path, relaunch_command):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return False
