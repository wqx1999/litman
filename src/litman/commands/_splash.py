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

Stdlib + tkinter only. It must stay import-light — the parent Popens it before
importing uvicorn — so it imports nothing from litman beyond the tiny top-level
package (only to locate the bundled icon). Missing tkinter, no display, or any
build error: return in silence. The shortcut still opens the browser window;
it just does so without a splash.
"""

from __future__ import annotations

# Independent of gui.py's SPLASH_TIMEOUT (seconds): this process cannot import
# gui.py without dragging in the heavy server chain, so the self-destruct
# backstop keeps its own value here, in milliseconds for tkinter's after().
SPLASH_TIMEOUT_MS = 25000

_WIDTH = 260
_HEIGHT = 200
_BG = "#f5f5f7"
_CARD = "#ffffff"
_INK = "#1c1c1e"
_SUBTLE = "#8a8a90"
_TRACK = "#e5e5ea"
_ACCENT = "#007aff"


def _icon_file() -> str:
    """Absolute path to the bundled 512px icon, or "" if unavailable.

    Replicates gui.py's ``_icon_path`` idiom (litman installs unpacked, so the
    resource has a stable filesystem path) without importing gui.py — that
    would pull in the whole server chain this process exists to avoid.
    """
    try:
        from importlib.resources import files

        return str(files("litman").joinpath("assets", "icons", "litman.png"))
    except Exception:
        return ""


def run_splash() -> None:
    """Show the splash and block on its event loop until closed. Never raises.

    Missing tkinter, no display, or any build failure returns silently — the
    desktop launch still opens the browser, just without a splash.
    """
    try:
        import tkinter as tk
    except Exception:
        return

    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.configure(bg=_BG)

        x = (root.winfo_screenwidth() - _WIDTH) // 2
        y = (root.winfo_screenheight() - _HEIGHT) // 2
        root.geometry(f"{_WIDTH}x{_HEIGHT}+{x}+{y}")

        card = tk.Frame(root, bg=_CARD)
        card.place(x=8, y=8, width=_WIDTH - 16, height=_HEIGHT - 16)

        # Brand mark: reuse the bundled 512px icon subsampled to ~64px. Tk 8.6
        # reads PNG natively (no new dependency). Keep `photo` referenced for
        # the life of mainloop, or Tk garbage-collects the image and shows
        # nothing — it stays alive as a local until this function returns.
        photo: object | None = None
        icon = _icon_file()
        if icon:
            try:
                image = tk.PhotoImage(file=icon)
                factor = max(1, image.width() // 64)
                image = image.subsample(factor, factor)
                tk.Label(card, image=image, bg=_CARD).pack(pady=(28, 8))
                photo = image
            except Exception:
                photo = None

        tk.Label(
            card, text="litman", bg=_CARD, fg=_INK,
            font=("TkDefaultFont", 18, "bold"),
        ).pack()
        tk.Label(
            card, text="Starting…", bg=_CARD, fg=_SUBTLE,
            font=("TkDefaultFont", 10),
        ).pack(pady=(2, 0))

        # Indeterminate progress: a short highlight segment ping-ponging along
        # a thin track — a soft, non-nagging pulse (index.css update-halo).
        # No fake percentage: the startup stages are not quantifiable.
        track_w = _WIDTH - 16 - 56
        seg_w = 56
        bar = tk.Canvas(
            card, width=track_w, height=3, bg=_TRACK, highlightthickness=0
        )
        bar.pack(side="bottom", pady=(0, 24))
        seg = bar.create_rectangle(0, 0, seg_w, 3, fill=_ACCENT, width=0)
        span = max(1, track_w - seg_w)
        state = {"pos": 0.0, "dir": 1.0}

        def _tick() -> None:
            state["pos"] += state["dir"] * span / 40.0
            if state["pos"] >= span:
                state["pos"], state["dir"] = float(span), -1.0
            elif state["pos"] <= 0:
                state["pos"], state["dir"] = 0.0, 1.0
            bar.coords(seg, state["pos"], 0, state["pos"] + seg_w, 3)
            root.after(16, _tick)

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
