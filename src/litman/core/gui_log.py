"""The GUI log: what the litman window could not do, in full, for the agent.

The window says what happened in one short line; the person takes it to
their agent. Everything behind that line lands here: the full text the
server answered with (it names the way out), the code the page picked its
line by, and a traceback when the server itself failed. Also what the page
shows that no answer carries: project folders a delete could not clean, the
SI window's doubts about a file, a failed automatic update, and the page's
own failures (pdf.js), which it posts. ``lit gui-log`` prints it.

One file per machine, ``gui-log.jsonl`` beside the vault registry (next to
``self-update.log``), one JSON object per line. Past :data:`MAX_BYTES` it is
moved to ``gui-log.1.jsonl`` and a new one starts, so the two hold at most
about twice that. Writing never fails a request: a log that cannot be
written costs one line on stderr.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from litman.core.dates import now_iso
from litman.core.replace import replace_file
from litman.core.vault_registry import registry_path

LOG_FILENAME = "gui-log.jsonl"
OLD_LOG_FILENAME = "gui-log.1.jsonl"

#: Past this size the log is moved aside and a new one starts.
MAX_BYTES = 1 << 20

#: Per entry: the text is cut after this many characters, a traceback keeps
#: this many from its end (where the failing line is).
TEXT_CAP = 8 * 1024
TRACEBACK_CAP = 32 * 1024

#: An entry equal to one written this recently is not written again: the
#: page asks every few seconds, and a library gone mid-session answers each
#: of those with the same refusal.
REPEAT_WINDOW_S = 600.0

Kind = Literal["error", "unexpected", "warning", "si-check", "page"]


def log_path() -> Path:
    """``gui-log.jsonl`` beside the vault registry (honours the env override)."""
    return registry_path().parent / LOG_FILENAME


def old_log_path() -> Path:
    """``gui-log.1.jsonl``: the log as it was when it last grew too large."""
    return registry_path().parent / OLD_LOG_FILENAME


def log_files() -> list[Path]:
    """The log files that exist. What ``lit uninstall`` removes."""
    return [p for p in (log_path(), old_log_path()) if p.is_file()]


def remove_log_files() -> dict[str, object]:
    """Delete :func:`log_files`, and the config dir too once it is empty.

    Used by ``lit uninstall``. Returns ``{"removed", "dir_removed"}``;
    ``removed`` lists the files actually deleted.
    """
    removed: list[Path] = []
    for target in (log_path(), old_log_path()):
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        removed.append(target)
    dir_removed = False
    parent = registry_path().parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            dir_removed = True
    except OSError:
        pass
    return {"removed": removed, "dir_removed": dir_removed}


def _cut(text: str, cap: int, *, keep_end: bool = False) -> str:
    if len(text) <= cap:
        return text
    if keep_end:
        return "…" + text[-(cap - 1):]
    return text[: cap - 1] + "…"


def _append(entry: Mapping[str, Any]) -> None:
    """Add one line to the log, moving a full log aside first."""
    path = log_path()
    # A lone surrogate (an undecodable file name in an OS message) would stop
    # a strict encode; escaped, it is still valid JSON.
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode(
        "utf-8", "backslashreplace"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(line) > MAX_BYTES:
        try:
            replace_file(path, old_log_path())
            size = 0
        except OSError:
            # Still held open elsewhere (Windows): keep appending, try next time.
            pass
    if size:
        # A write cut short left half a line: start on a fresh one, or this
        # entry would be glued to it and lost with it.
        with open(path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                line = b"\n" + line
    with open(path, "ab") as handle:
        handle.write(line)


class GuiLog:
    """Writes the entries of one GUI server.

    It remembers what it wrote in the last :data:`REPEAT_WINDOW_S`, so a
    failure the page meets on every poll is written once.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: dict[str, float] = {}

    def record(
        self,
        *,
        kind: Kind,
        text: str,
        vault: Path | None = None,
        route: str | None = None,
        status: int | None = None,
        code: str | None = None,
        params: Mapping[str, object] | None = None,
        traceback: str | None = None,
    ) -> bool:
        """Write one entry; False when it repeats a recent one or could not be written."""
        try:
            clean = {str(k): str(v) for k, v in (params or {}).items()}
            text = _cut(str(text), TEXT_CAP)
            key = json.dumps(
                [kind, status, code, clean, text], sort_keys=True, ensure_ascii=False
            )
            entry: dict[str, Any] = {
                "time": now_iso(),
                "vault": str(vault) if vault is not None else None,
                "route": route,
                "status": status,
                "kind": kind,
                "code": code,
                "params": clean,
                "text": text,
                "traceback": (
                    _cut(traceback, TRACEBACK_CAP, keep_end=True) if traceback else None
                ),
            }
            with self._lock:
                now = self._clock()
                self._recent = {
                    k: at for k, at in self._recent.items() if now - at < REPEAT_WINDOW_S
                }
                if key in self._recent:
                    return False
                _append(entry)
                self._recent[key] = now
        except Exception as exc:
            print(
                f"litman: could not write the GUI log {log_path()} "
                f"({exc.__class__.__name__}: {exc})",
                file=sys.stderr,
            )
            return False
        return True


def read_entries() -> list[dict[str, Any]]:
    """Every entry in the two log files, oldest first.

    A line that is not a JSON object with a ``time`` (a write cut short) is
    skipped.

    Raises:
        OSError: a log file exists but cannot be read.
    """
    out: list[dict[str, Any]] = []
    for path in (old_log_path(), log_path()):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        for raw in data.splitlines():
            try:
                entry = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(entry, dict) and entry_time(entry) is not None:
                out.append(entry)
    return out


def entry_time(entry: Mapping[str, Any]) -> datetime | None:
    """When ``entry`` was written, timezone-aware; None when unreadable."""
    try:
        at = datetime.fromisoformat(str(entry["time"]))
    except (KeyError, ValueError):
        return None
    return at if at.tzinfo is not None else at.astimezone()
