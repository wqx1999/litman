"""Tests for ``lit gui`` — import isolation, the extra-missing guard, and the
free-port finder. These run WITHOUT fastapi installed (the helper + guard are
fastapi-free by design, invariant #5)."""

from __future__ import annotations

import builtins
import importlib
import socket
import sys

from click.testing import CliRunner

from litman.commands.gui import _DEFAULT_PORT, _find_free_port, gui_cmd

# ---------------------------------------------------------------------------
# A1(a) — importing the CLI must not pull fastapi into the process
# ---------------------------------------------------------------------------


def test_cli_import_does_not_load_fastapi() -> None:
    # Drop any fastapi/server modules a prior test may have imported, then
    # re-import the CLI from scratch and assert it stayed fastapi-free.
    for mod in list(sys.modules):
        if mod == "fastapi" or mod.startswith("fastapi.") or mod.startswith(
            "litman.cli"
        ) or mod.startswith("litman.server"):
            del sys.modules[mod]
    importlib.import_module("litman.cli")
    assert "fastapi" not in sys.modules


# ---------------------------------------------------------------------------
# A1(b) — extra-missing guard: friendly message + non-zero exit
# ---------------------------------------------------------------------------


def test_gui_without_web_extra_errors_with_hint(
    monkeypatch,
) -> None:
    real_import = builtins.__import__

    def _no_uvicorn(name, *args, **kwargs):
        if name == "uvicorn" or name.startswith("uvicorn."):
            raise ImportError("No module named 'uvicorn'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_uvicorn)

    result = CliRunner().invoke(gui_cmd, [])
    assert result.exit_code != 0
    assert "litman[web]" in result.output


# ---------------------------------------------------------------------------
# A6 — free-port finder (Jupyter model: never errors on a busy port)
# ---------------------------------------------------------------------------


def test_find_free_port_returns_default_when_free() -> None:
    assert _find_free_port(_DEFAULT_PORT) == _DEFAULT_PORT


def test_find_free_port_increments_when_busy() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", _DEFAULT_PORT))
        occupied.listen(1)
        chosen = _find_free_port(_DEFAULT_PORT)
    assert chosen >= _DEFAULT_PORT + 1


def test_find_free_port_binds_loopback_only() -> None:
    # The returned port must be bindable on 127.0.0.1 — proves the probe
    # targets loopback, not 0.0.0.0.
    port = _find_free_port(_DEFAULT_PORT)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
