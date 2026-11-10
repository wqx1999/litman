"""The port a ``lit gui`` started as its own process really listens on.

A test that starts ``lit gui --port N`` cannot assume the server is on ``N``.
The port is free when the test picks it, but ``lit gui`` probes it again
only after its own interpreter has started, and anything may take it in that
gap: another process, or another pytest-xdist worker. ``lit gui`` then walks
up to the next free port, and a test still talking to ``N`` would reach
whatever holds it now. The server records the port it took in
``<registry dir>/gui/<host>-<pid>.json`` (``core/gui_registry.py``), so the
test reads it from there.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


def gui_port(registry_dir: Path, proc: subprocess.Popen[Any], timeout: float = 60) -> int:
    """The port ``proc`` (a ``lit gui`` using ``registry_dir``) recorded.

    Raises AssertionError if the process exits or records nothing in time.
    """
    folder = registry_dir / "gui"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in folder.glob(f"*-{proc.pid}.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # caught between the write and the rename
            return int(record["port"])
        if proc.poll() is not None:
            raise AssertionError(f"lit gui exited with {proc.returncode} before it recorded a port")
        time.sleep(0.1)
    raise AssertionError(f"lit gui recorded no port in {folder} within {timeout} s")
