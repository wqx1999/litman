"""Running ``lit gui`` servers, recorded so the CLI can find one (ADR-027).

``lit add --si <paper>`` hands the work to the GUI page the user has open:
it needs the port of a GUI that serves the same library. ``lit gui`` walks up
from 8765 when a port is taken, so the port is not knowable in advance —
each running server records itself as one small JSON file::

    <registry dir>/gui/<host>-<pid>.json
    {"app": "litman", "version": "…", "host": "…", "pid": 4242,
     "port": 8765, "vault": "/abs/path/or/null", "started": 1760950000.0}

The registry dir follows ``$LITMAN_REGISTRY_DIR`` (dev and production stay
apart), and it may be a home directory shared by many machines, or a synced
one. Hence the host in the name and in the record: ``127.0.0.1`` on another
node is another machine, so another host's record is never pinged and never
deleted.

A record may outlive its server — SIGKILL, a crash, a closed ssh session —
so :func:`find_gui` asks each one over HTTP (``GET /api/si/ping``), never by
signalling the pid (on Windows that would kill it). It deletes only a record
whose server is certainly gone; a slow answer keeps it, or a busy but live
GUI would vanish from the CLI's view for good.

Standard library only, apart from httpx imported inside :func:`find_gui`:
``commands/gui.py`` imports this module on the CLI's import path.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from litman import __version__
from litman.core.fs_identity import same_entry
from litman.core.vault_registry import registry_path

if TYPE_CHECKING:
    import httpx

GUI_DIRNAME = "gui"
APP_NAME = "litman"
PING_PATH = "/api/si/ping"

# The record is written before the server listens (lifespan startup runs
# before the bind), so a refused connection in the first seconds means "not
# up yet", not "dead".
_STARTUP_GRACE_S = 30.0
# Connect generously: Windows takes 1-2 s to report a closed local port as
# refused, and a shorter wait would keep a dead record forever as "timed out".
_CONNECT_TIMEOUT_S = 3.0
_READ_TIMEOUT_S = 2.0

_HOST_UNSAFE_RE = re.compile(r"[^a-z0-9._-]")
_HOST_MAX = 64

# The names litman itself writes in ``gui/``: a record (which must also read as
# one, see :func:`gui_record_files`), and the temp file a write killed between
# create and rename strands beside it. Anything else there is not ours to delete.
_RECORD_NAME_RE = re.compile(r"[a-z0-9._-]*-[0-9]+\.json")
_TEMP_NAME_RE = re.compile(r"\.[a-z0-9._-]*-[0-9]+\.json\.[0-9a-f]{32}\.tmp")


@dataclass(frozen=True)
class GuiServer:
    """A live GUI serving the library asked about."""

    port: int
    pid: int
    pages: int
    vault: Path
    url: str


def host_id() -> str:
    """This machine's name as records carry it."""
    return socket.gethostname().strip().lower()


def gui_dir() -> Path:
    """Where the records live: ``gui/`` beside the vault registry."""
    return registry_path().parent / GUI_DIRNAME


def _file_host(host: str) -> str:
    return _HOST_UNSAFE_RE.sub("_", host)[:_HOST_MAX]


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` whole or not at all, readable by the user
    alone on POSIX. A uniquely named temp file, so two writers never meet."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        with suppress(FileNotFoundError):
            tmp.unlink()


def register_gui(port: int, vault: Path | None) -> Path:
    """Record this process as a GUI on ``port`` serving ``vault``.

    Returns the record's path, for :func:`unregister_gui`.
    """
    folder = gui_dir()
    folder.mkdir(parents=True, exist_ok=True)
    host = host_id()
    pid = os.getpid()
    path = folder / f"{_file_host(host)}-{pid}.json"
    record = {
        "app": APP_NAME,
        "version": __version__,
        "host": host,
        "pid": pid,
        "port": port,
        "vault": str(Path(vault).resolve()) if vault is not None else None,
        "started": time.time(),
    }
    _write_private(path, json.dumps(record, indent=2) + "\n")
    return path


def unregister_gui(path: Path | None) -> None:
    """Remove a record, and ``gui/`` with it once empty. Never raises."""
    if path is None:
        return
    with suppress(OSError):
        path.unlink()
    with suppress(OSError):
        path.parent.rmdir()


def gui_record_files() -> list[Path]:
    """Every file litman wrote in ``gui/``, whichever host wrote it.

    A record is recognised by its name and by its ``app`` field, not by the
    full shape :func:`_read_record` demands: a record another litman version
    wrote is still ours to remove. A temp file only ever had half a record in
    it, so its name has to do.
    """
    try:
        entries = sorted(gui_dir().iterdir())
    except OSError:
        return []
    return [
        path
        for path in entries
        if path.is_file()
        and (
            (_RECORD_NAME_RE.fullmatch(path.name) and _written_by_litman(path))
            or _TEMP_NAME_RE.fullmatch(path.name)
        )
    ]


def _written_by_litman(path: Path) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("app") == APP_NAME


def gui_dir_is_empty() -> bool:
    """``gui/`` exists and holds nothing: a leftover only an rmdir clears."""
    try:
        return not any(gui_dir().iterdir())
    except OSError:
        return False


def remove_gui_records() -> dict[str, object]:
    """Delete :func:`gui_record_files`, then ``gui/`` and the config dir
    around it once each is empty — also when ``gui/`` held no record at all.
    Used by ``lit uninstall``.

    Every host's records go, not only this one's: the uninstall drops the
    whole registry dir, and a record left in it would keep the dir alive. A
    GUI still running just loses its record — ``lit add --si`` stops finding
    it, and its own exit finds nothing to remove, which
    :func:`unregister_gui` tolerates. A file litman did not write keeps
    ``gui/`` (and so the config dir) in place.

    Returns ``{"path", "removed", "kept", "dir_removed"}``: ``gui/``, the
    files deleted, what is left in ``gui/``, and whether ``gui/`` is gone.
    """
    folder = gui_dir()
    removed: list[Path] = []
    for path in gui_record_files():
        try:
            path.unlink()
        except FileNotFoundError:
            # A GUI exiting right now took its record first.
            continue
        removed.append(path)
    for empty in (folder, folder.parent):
        try:
            if empty.is_dir() and not any(empty.iterdir()):
                empty.rmdir()
        except OSError:
            pass
    try:
        kept = sorted(folder.iterdir())
    except OSError:
        kept = []
    return {
        "path": folder,
        "removed": removed,
        "kept": kept,
        "dir_removed": not folder.exists(),
    }


def _read_record(path: Path) -> dict[str, Any] | None:
    """The record in ``path``, or ``None`` when it is not one of ours."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("app") != APP_NAME:
        return None
    host = record.get("host")
    pid, port = _int_or_none(record.get("pid")), _int_or_none(record.get("port"))
    if (
        not isinstance(host, str)
        or pid is None
        or port is None
        or not 0 < port < 65536
        or _seconds_or_none(record.get("started")) is None
    ):
        return None
    return record


def _int_or_none(value: object) -> int | None:
    """``value`` when it is an int (and not a bool), else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _seconds_or_none(value: object) -> float | None:
    """``value`` as a finite float, else ``None``: JSON lets NaN, Infinity
    and integers too big for a float through."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) else None


def _ping(client: httpx.Client, record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``("alive", answer)``, ``("dead", {})`` or ``("unknown", {})``."""
    import httpx

    try:
        response = client.get(f"http://127.0.0.1:{record['port']}{PING_PATH}")
    except httpx.ConnectError:
        if time.time() - float(record["started"]) > _STARTUP_GRACE_S:
            return "dead", {}
        return "unknown", {}
    except httpx.HTTPError:
        # Timeouts and every other transport failure: maybe busy, maybe not.
        return "unknown", {}
    if response.status_code == 404 or 300 <= response.status_code < 400:
        return "dead", {}
    if response.status_code != 200:
        return "unknown", {}
    try:
        answer = response.json()
    except ValueError:
        return "dead", {}
    if (
        not isinstance(answer, dict)
        or answer.get("app") != APP_NAME
        or answer.get("pid") != record["pid"]
        or answer.get("host") != record["host"]
    ):
        # Something else listens on that port now: the record's server is gone.
        return "dead", {}
    return "alive", answer


def find_gui(
    vault: Path, *, client: httpx.Client | None = None
) -> GuiServer | None:
    """A live GUI on this machine serving ``vault``, or ``None``.

    Every record of this host is pinged. A record is deleted when its server
    is certainly gone — the port refuses connections (after the startup
    grace), answers 404 or a redirect, or answers as something that is not
    that litman process — and kept when the answer is merely slow or odd.
    Unreadable records and other hosts' records are never touched. Of several
    matches, one with an open page wins, then the most recently started.
    """
    import httpx

    folder = gui_dir()
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return None
    me = host_id()
    own_client = client is None
    if client is None:
        client = httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
        )
    found: list[tuple[GuiServer, float]] = []
    try:
        for path in paths:
            record = _read_record(path)
            if record is None or record["host"] != me:
                continue
            outcome, answer = _ping(client, record)
            if outcome == "dead":
                unregister_gui(path)
                continue
            if outcome != "alive":
                continue
            served = answer.get("vault")
            if not isinstance(served, str) or not same_entry(Path(served), vault):
                continue
            pages = _int_or_none(answer.get("pages"))
            port = int(record["port"])
            found.append(
                (
                    GuiServer(
                        port=port,
                        pid=int(record["pid"]),
                        pages=pages if pages is not None else 0,
                        vault=Path(served),
                        url=f"http://127.0.0.1:{port}",
                    ),
                    float(record["started"]),
                )
            )
    finally:
        if own_client:
            client.close()
    if not found:
        return None
    found.sort(key=lambda hit: (hit[0].pages > 0, hit[1]), reverse=True)
    return found[0][0]


def other_host_guis(vault: Path) -> list[tuple[str, float]]:
    """``(host, started)`` of each GUI recorded for ``vault`` on another
    machine, newest first.

    Only the files are read: another host's ``127.0.0.1`` is another machine,
    so these records are never pinged and never deleted, and any of them may
    be stale. What the CLI says when no GUI here serves the library.
    """
    try:
        paths = sorted(gui_dir().glob("*.json"))
    except OSError:
        return []
    me = host_id()
    found: list[tuple[str, float]] = []
    for path in paths:
        record = _read_record(path)
        if record is None or record["host"] == me:
            continue
        served = record.get("vault")
        if not isinstance(served, str) or not same_entry(Path(served), vault):
            continue
        found.append((record["host"], float(record["started"])))
    found.sort(key=lambda hit: hit[1], reverse=True)
    return found


__all__ = [
    "GuiServer",
    "find_gui",
    "gui_dir",
    "gui_dir_is_empty",
    "gui_record_files",
    "host_id",
    "other_host_guis",
    "register_gui",
    "remove_gui_records",
    "unregister_gui",
]
