"""Tests for the ``litman.commands._splash`` subprocess (Part C, C4).

The splash process must stay import-light (the parent Popens it before it even
imports uvicorn) and must degrade in silence — a missing tkinter or an absent
display returns without a traceback, so the desktop launch still opens the
browser window, just without a splash. No test reaches ``root.mainloop()``:
the two silent-return paths are forced before a window is ever built, so this
never hangs, display or not. The real on-screen render is manual acceptance
(it needs a display), noted in the dev-writer report.
"""

from __future__ import annotations

import subprocess
import sys

from litman.commands import _splash


def test_splash_module_stays_import_light() -> None:
    # Hermetic: a fresh interpreter that imports ONLY _splash must not pull the
    # heavy chain (fastapi / pypdf / httpx / uvicorn / server / gui). Run in a
    # subprocess so the assertion is immune to whatever this pytest session has
    # already imported.
    code = (
        "import sys, litman.commands._splash;"
        "heavy=('fastapi','pypdf','httpx','uvicorn',"
        "'litman.server','litman.commands.gui');"
        "bad=[m for m in heavy if m in sys.modules];"
        "print(','.join(bad));"
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"heavy modules leaked into `import litman.commands._splash`: "
        f"{result.stdout!r} {result.stderr!r}"
    )


def test_run_splash_silent_when_tkinter_missing(monkeypatch) -> None:
    # No tkinter (some stripped Linux python3 lacks python3-tk): `import
    # tkinter` raises, and run_splash returns without raising.
    monkeypatch.setitem(sys.modules, "tkinter", None)
    assert _splash.run_splash() is None


def test_run_splash_silent_when_display_unavailable(monkeypatch) -> None:
    # No display: Tk() raises TclError before any window is built. run_splash
    # must swallow it and return (never reaching mainloop, so never hanging).
    import tkinter

    def _boom(*a, **k):
        raise tkinter.TclError("no display name and no $DISPLAY")

    monkeypatch.setattr(tkinter, "Tk", _boom)
    assert _splash.run_splash() is None


def test_icon_file_resolves_to_bundled_png() -> None:
    path = _splash._icon_file()
    assert path.endswith("litman.png")
    assert "assets" in path and "icons" in path


def test_splash_timeout_ms_is_a_positive_backstop() -> None:
    assert isinstance(_splash.SPLASH_TIMEOUT_MS, int)
    assert _splash.SPLASH_TIMEOUT_MS > 0
