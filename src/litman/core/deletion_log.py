"""Append-only deletion log at ``<vault>/.deletion-log.jsonl`` (M23.1).

`lit rm` records every removal here so the history survives even after the
trash ring evicts the entry. Each line is one JSON object; the schema is
intentionally loose (invariant #7) but every record carries at least
``{id, action, at}``:

    {"id": "2024_Foo", "title": "...", "action": "trashed",
     "at": "2026-05-23T14:02:11+02:00", "trash_path": "..."}
    {"id": "2024_Bar", "title": "...", "action": "purged",
     "at": "2026-05-23T14:05:43+02:00"}

This module is the CLI-side WRITER only (invariant #1: the tool writes,
the LLM never touches data files). The READ side (``lit show`` not-found
back-lookup, skill surfacing) is proposal ③ and out of scope for M23 —
nothing here parses or queries the file. M23.2 will append ``restored``
rows through this same writer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LOG_FILENAME = ".deletion-log.jsonl"


def _log_path(vault: Path) -> Path:
    return vault / LOG_FILENAME


def append_log_entry(vault: Path, record: dict[str, Any]) -> None:
    """Append one JSON record as a single line to the deletion log.

    Creates the file on first write. Append-mode keeps prior history; the
    log is never rewritten or compacted here. JSON is dumped with
    ``ensure_ascii=False`` so non-ASCII titles stay readable.
    """
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _log_path(vault).open("a", encoding="utf-8") as f:
        f.write(line + "\n")
