"""``python -m litman.commands._splash`` — the desktop-launch splash window.

A throwaway native window shown while the console-less desktop shortcut warms
up the server and the browser paints its first frame. ``gui.py`` Popens this as
its own process and terminates it once a page connects; the window also
self-destructs after ``SPLASH_TIMEOUT_MS`` so a forgotten handle never orphans
it.

Why a separate process: tkinter is not thread-safe and macOS requires the GUI
on the main thread, but the parent's main thread is busy running uvicorn. A
child process has its own main thread for the tkinter loop and never contends
with the server.

Crisp on high-DPI: a splash process that never declares DPI awareness is
bitmap-stretched by Windows on a scaled display, so the logo and the text come
out blurred and jagged. This process opts into per-monitor DPI awareness before
the first window exists (:func:`_enable_dpi_awareness`), reads the scale factor,
and sizes everything — window, fonts (in pixels), the brand mark — against it.
The mark itself is a set of LANCZOS-downscaled PNGs bundled at a few sizes
(``litman_<px>.png``); the nearest one is shown 1:1, so it is never resampled at
runtime (Tk's ``subsample`` is nearest-neighbour and would re-introduce the
jaggies). Pillow is a build-time tool here, never a runtime dependency.

Stdlib + tkinter only. It must stay import-light — the parent Popens it before
importing uvicorn — so it imports nothing from litman beyond the tiny top-level
package (only to locate the bundled icon). Missing tkinter, no display, or any
build error: return in silence. The shortcut still opens the browser window;
it just does so without a splash.
"""

from __future__ import annotations

import contextlib
import sys

# Independent of gui.py's SPLASH_TIMEOUT (seconds): this process cannot import
# gui.py without dragging in the heavy server chain, so the self-destruct
# backstop keeps its own value here, in milliseconds for tkinter's after().
SPLASH_TIMEOUT_MS = 25000

# Window and layout in *logical* units (1x). Every pixel figure is multiplied
# by the detected DPI scale at build time, so the physical window grows on a
# scaled display instead of being stretched by the OS.
_WIDTH = 300
_HEIGHT = 216
_LOGO = 76          # target logical size of the brand mark
_WORD_PT = 22       # "litman" wordmark
_SUB_PT = 12        # "Starting…" subtitle
_TRACK_W = 150      # progress track length
_TRACK_H = 4        # progress track / segment thickness
_SEG_W = 48         # moving highlight length
_PAD_TOP = 32       # card top → mark
_GAP_MARK = 12      # mark → wordmark
_GAP_WORD = 5       # wordmark → subtitle
_PAD_BOTTOM = 30    # progress track → card bottom

# macOS-neutral palette (matches the webUI: ink #1c1c1e, accent #007aff).
_BORDER = "#cfcfd6"
_CARD = "#ffffff"
_INK = "#1c1c1e"
_SUBTLE = "#8a8a90"
_TRACK = "#e6e6ea"
_ACCENT = "#007aff"

# Bundled brand-mark sizes (LANCZOS-downscaled from the 512px master). The
# nearest size to (logical 76 x DPI scale) is shown 1:1 — crisp at any scale
# without a runtime resample.
_ICON_SIZES = (64, 96, 128, 160, 192)


def _enable_dpi_awareness() -> None:
    """Opt this process into per-monitor DPI awareness (Windows). No-op else.

    Must run before the first Tk window is created — awareness set afterward is
    ignored and Windows bitmap-stretches the window. Tries the modern
    per-monitor-v2 context first, then older shcore / user32 fallbacks, and
    swallows everything: a splash that can't set awareness is merely blurry, not
    broken.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except Exception:
        return
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4 (Win10 1703+).
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    # PROCESS_PER_MONITOR_DPI_AWARE == 2 (Win8.1+).
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # system-DPI aware (Vista+)
    except Exception:
        return


def _dpi_scale(root: object) -> float:
    """DPI scale factor (1.0 = 96 dpi), clamped to [1.0, 3.0].

    Windows only: with awareness set, ``winfo_fpixels('1i')`` reports the real
    monitor DPI. macOS/Linux Tk handle HiDPI transparently (points, Retina
    backing scale), so 1.0 is correct there and avoids double-scaling.
    """
    if sys.platform != "win32":
        return 1.0
    try:
        dpi = float(root.winfo_fpixels("1i"))  # type: ignore[attr-defined]
    except Exception:
        return 1.0
    return min(3.0, max(1.0, dpi / 96.0))


def _nearest_size(target: int, sizes: tuple[int, ...] = _ICON_SIZES) -> int:
    """The bundled icon size closest to ``target`` pixels."""
    return min(sizes, key=lambda s: abs(s - target))


def _icon_for_scale(scale: float) -> str:
    """Absolute path to the best-fit bundled mark PNG, or "" if unavailable.

    Replicates gui.py's ``_icon_path`` idiom (litman installs unpacked, so the
    resource has a stable filesystem path) without importing gui.py — that would
    pull in the whole server chain this process exists to avoid.
    """
    size = _nearest_size(round(_LOGO * scale))
    try:
        from importlib.resources import files

        return str(files("litman").joinpath("assets", "icons", f"litman_{size}.png"))
    except Exception:
        return ""


def _fonts() -> tuple[tuple[str, str], tuple[str, str]]:
    """(family, weight) for the wordmark and the subtitle, per platform.

    Named families, not ``TkDefaultFont``: the wordmark wants a clean UI
    semibold, and pinning the family keeps the render predictable. Weight is ""
    when the family already carries it (Segoe UI Semibold) so no synthetic bold
    is layered on top.
    """
    if sys.platform == "win32":
        return ("Segoe UI Semibold", ""), ("Segoe UI", "")
    if sys.platform == "darwin":
        return ("Helvetica Neue", "bold"), ("Helvetica Neue", "")
    return ("TkDefaultFont", "bold"), ("TkDefaultFont", "")


def _mk_font(spec: tuple[str, str], px: int) -> tuple:
    """A tkinter font tuple sized in *pixels* (negative = absolute px)."""
    family, weight = spec
    return (family, -px, weight) if weight else (family, -px)


def _round_window_corners(root: object) -> None:
    """Ask the Win11 compositor to round the window corners. No-op else.

    Best-effort DWM attribute: real, anti-aliased rounding with no colour-key
    fringing. Unsupported on Win10 / borderless windows it can't round — the
    call errors and is swallowed, leaving crisp square corners.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()  # type: ignore[attr-defined]
        # c_void_p, not a bare int: HWND is pointer-sized and a bare Python int
        # argument defaults to c_int, truncating the handle on 64-bit Windows.
        hwnd = ctypes.c_void_p(root.winfo_id())  # type: ignore[attr-defined]
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(pref),
            ctypes.sizeof(pref),
        )
    except Exception:
        return


def run_splash() -> None:
    """Show the splash and block on its event loop until closed. Never raises.

    Missing tkinter, no display, or any build failure returns silently — the
    desktop launch still opens the browser, just without a splash.
    """
    try:
        import tkinter as tk
    except Exception:
        return

    # Before the first window exists, or Windows stretches (blurs) it.
    _enable_dpi_awareness()

    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.configure(bg=_BORDER)
        with contextlib.suppress(Exception):
            root.attributes("-topmost", True)

        scale = _dpi_scale(root)

        def px(v: float) -> int:
            return max(1, round(v * scale))

        w, h = px(_WIDTH), px(_HEIGHT)
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")

        # Single flat card with a 1px hairline (the outer border colour showing
        # through), not a card inset inside a filled frame — that double layer
        # reads as a cheap bevel. The hairline stays 1 physical px at any DPI.
        card = tk.Frame(root, bg=_CARD)
        card.place(x=1, y=1, width=w - 2, height=h - 2)

        # Brand mark: the nearest bundled size shown 1:1 (never resampled). Keep
        # `photo` referenced for the life of mainloop, or Tk garbage-collects the
        # image and shows nothing — it stays alive as a local until this returns.
        photo: object | None = None
        icon = _icon_for_scale(scale)
        if icon:
            try:
                photo = tk.PhotoImage(file=icon)
                tk.Label(card, image=photo, bg=_CARD).pack(
                    pady=(px(_PAD_TOP), px(_GAP_MARK))
                )
            except Exception:
                photo = None

        word_spec, sub_spec = _fonts()
        tk.Label(
            card, text="litman", bg=_CARD, fg=_INK,
            font=_mk_font(word_spec, px(_WORD_PT)),
        ).pack()
        tk.Label(
            card, text="Starting…", bg=_CARD, fg=_SUBTLE,
            font=_mk_font(sub_spec, px(_SUB_PT)),
        ).pack(pady=(px(_GAP_WORD), 0))

        # Indeterminate progress: a short accent segment ping-ponging along a
        # thin track. Both are rounded-cap lines (a pill), not rectangles, so the
        # ends read soft. No fake percentage — the startup stages aren't
        # quantifiable.
        track_w, track_h, seg_w = px(_TRACK_W), px(_TRACK_H), px(_SEG_W)
        r = track_h / 2.0
        bar = tk.Canvas(
            card, width=track_w, height=track_h, bg=_CARD, highlightthickness=0
        )
        bar.pack(side="bottom", pady=(0, px(_PAD_BOTTOM)))
        bar.create_line(
            r, r, track_w - r, r, fill=_TRACK, width=track_h, capstyle="round"
        )
        seg = bar.create_line(
            r, r, r + seg_w, r, fill=_ACCENT, width=track_h, capstyle="round"
        )
        span = max(1, track_w - seg_w)
        state = {"pos": 0.0, "dir": 1.0}

        def _tick() -> None:
            state["pos"] += state["dir"] * span / 42.0
            if state["pos"] >= span:
                state["pos"], state["dir"] = float(span), -1.0
            elif state["pos"] <= 0:
                state["pos"], state["dir"] = 0.0, 1.0
            bar.coords(seg, state["pos"] + r, r, state["pos"] + seg_w - r, r)
            root.after(16, _tick)

        _round_window_corners(root)
        root.after(16, _tick)
        # Self-destruct backstop: never outlive the launch, even if the parent
        # forgets to terminate this process.
        root.after(int(SPLASH_TIMEOUT_MS), root.destroy)
        root.mainloop()
        # Reference photo after mainloop so it is provably kept alive above.
        del photo
    except Exception:
        return


if __name__ == "__main__":
    run_splash()
