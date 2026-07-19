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

import os
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


def test_nearest_size_picks_the_closest_bundled_size() -> None:
    assert _splash._nearest_size(76) == 64       # 1x display
    assert _splash._nearest_size(114) == 128      # 1.5x
    assert _splash._nearest_size(152) == 160      # 2x
    assert _splash._nearest_size(228) == 192      # 3x (clamps to the top size)


def test_icon_for_scale_resolves_to_a_bundled_png_that_exists() -> None:
    for scale in (1.0, 1.5, 2.0, 3.0):
        path = _splash._icon_for_scale(scale)
        assert path.endswith(".png")
        assert "assets" in path and "icons" in path
        assert os.path.exists(path), f"missing bundled mark for scale {scale}: {path}"


def test_every_declared_icon_size_is_bundled() -> None:
    # Guards packaging: each size named in _ICON_SIZES must ship as a real file,
    # or a high-DPI splash silently loses its mark.
    for size in _splash._ICON_SIZES:
        path = _splash._icon_for_scale(size / _splash._LOGO)
        assert path.endswith(f"litman_{size}.png")
        assert os.path.exists(path)


def test_dpi_scale_is_one_off_windows() -> None:
    # Off Windows Tk handles HiDPI itself; the manual factor must stay 1.0 so we
    # never double-scale. (This test host is not win32.)
    if sys.platform != "win32":
        assert _splash._dpi_scale(object()) == 1.0


def test_enable_dpi_awareness_never_raises() -> None:
    # A no-op off Windows; on Windows every probe is guarded. Either way it must
    # return without raising so it can run unconditionally before Tk().
    assert _splash._enable_dpi_awareness() is None


def test_mk_font_sizes_in_absolute_pixels() -> None:
    # Negative size == absolute pixels (immune to tk scaling); weight only when
    # the family does not already carry it.
    assert _splash._mk_font(("Segoe UI Semibold", ""), 22) == ("Segoe UI Semibold", -22)
    assert _splash._mk_font(("Helvetica Neue", "bold"), 12) == ("Helvetica Neue", -12, "bold")


def test_splash_timeout_ms_is_a_positive_backstop() -> None:
    assert isinstance(_splash.SPLASH_TIMEOUT_MS, int)
    assert _splash.SPLASH_TIMEOUT_MS > 0
