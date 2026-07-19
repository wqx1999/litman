"""``python -m litman.commands._splash`` — the desktop-launch splash window.

A throwaway native window shown while the console-less desktop shortcut warms
up the server and the browser paints its first frame. ``gui.py`` Popens this as
its own process and terminates it once a page connects; the window also
self-destructs after ``SPLASH_TIMEOUT_MS`` so a forgotten handle never orphans
it.

Just the mark. The splash is the app icon floating on the desktop — no card, no
wordmark, no progress bar. A bare, recognisable logo reads as "launching" the
way a macOS app icon does, and it is the cleanest thing tkinter can render well.

Why a separate process: tkinter is not thread-safe and macOS requires the GUI
on the main thread, but the parent's main thread is busy running uvicorn. A
child process has its own main thread for the tkinter loop and never contends
with the server.

Crisp on high-DPI: a splash process that never declares DPI awareness is
bitmap-stretched by Windows on a scaled display, so the mark comes out blurred
and jagged. This process opts into per-monitor DPI awareness before the first
window exists (:func:`_enable_dpi_awareness`), reads the scale factor, and picks
the mark size against it. The mark is a set of LANCZOS-downscaled PNGs bundled
at a few sizes (``litman_<px>.png``); the nearest is shown 1:1, never resampled
at runtime (Tk's ``subsample`` is nearest-neighbour and would re-introduce the
jaggies). Pillow is a build-time tool here, never a runtime dependency.

Floating, no box: the window is keyed transparent with ``-transparentcolor``.
The key is pure white and the bundled marks are clamped so no opaque pixel is
exactly ``#ffffff`` — the tile can't be punched through, the fully-transparent
corners composite to white and drop out, and the anti-aliased corner fringe
blends to ~#fdfdfd, invisible against the near-white tile. Where the platform
has no color-key transparency (older Linux/macOS), the white shows as a small
backing square — a graceful, rare fallback (the splash is Windows-shortcut-led).

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

# Target logical (1x) size of the floating mark; the physical pixels are this
# times the DPI scale, matched to the nearest bundled size.
_LOGO = 112
# Transparent margin (logical px) of key colour around the mark, so the corner
# anti-aliasing is never clipped at the window edge.
_MARGIN = 8
# Color key for the transparent window. Pure white: the bundled marks are
# clamped so no opaque pixel equals it, so only the true background drops out.
_KEY = "#ffffff"

# Bundled mark sizes (LANCZOS-downscaled from the 512px master, white-clamped).
# The nearest size to (logical 112 x DPI scale) is shown 1:1 — crisp at any
# scale without a runtime resample.
_ICON_SIZES = (96, 128, 160, 192, 224, 256)


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


def run_splash() -> None:
    """Show the splash and block on its event loop until closed. Never raises.

    Missing tkinter, no display, no bundled mark, or any build failure returns
    silently — the desktop launch still opens the browser, just without a splash.
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
        root.configure(bg=_KEY)
        # Key the background out so only the mark floats. Guarded: a platform
        # without color-key transparency just shows a small white backing.
        with contextlib.suppress(Exception):
            root.attributes("-transparentcolor", _KEY)
        with contextlib.suppress(Exception):
            root.attributes("-topmost", True)

        scale = _dpi_scale(root)

        # No mark, no splash — never a bare empty window.
        icon = _icon_for_scale(scale)
        if not icon:
            root.destroy()
            return
        try:
            photo = tk.PhotoImage(file=icon)
        except Exception:
            root.destroy()
            return

        margin = max(1, round(_MARGIN * scale))
        w = h = photo.width() + 2 * margin
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")

        # Keep `photo` referenced for the life of mainloop, or Tk garbage-
        # collects the image and shows nothing — it stays alive as a local until
        # this function returns.
        tk.Label(root, image=photo, bg=_KEY, borderwidth=0, highlightthickness=0).place(
            relx=0.5, rely=0.5, anchor="center"
        )

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
