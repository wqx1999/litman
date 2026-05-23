"""Tests for the append-only deletion log writer (M23.1).

Covers append semantics, line-per-record JSON, file creation, and non-ASCII
preservation. Only the WRITE side is implemented in M23 (read/query is
proposal ③); these tests parse the file directly to assert what was written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litman.core.deletion_log import LOG_FILENAME, append_log_entry
from litman.core.library import create_vault


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _read_rows(vault: Path) -> list[dict]:
    lines = (vault / LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_append_creates_file(vault: Path) -> None:
    assert not (vault / LOG_FILENAME).exists()
    append_log_entry(vault, {"id": "a", "action": "trashed", "at": "now"})
    rows = _read_rows(vault)
    assert rows == [{"id": "a", "action": "trashed", "at": "now"}]


def test_append_is_append_only(vault: Path) -> None:
    append_log_entry(vault, {"id": "a", "action": "trashed", "at": "t1"})
    append_log_entry(vault, {"id": "b", "action": "purged", "at": "t2"})
    rows = _read_rows(vault)
    assert len(rows) == 2
    assert rows[0]["id"] == "a"
    assert rows[1]["id"] == "b"


def test_append_one_record_per_line(vault: Path) -> None:
    append_log_entry(vault, {"id": "a", "action": "trashed", "at": "t1"})
    append_log_entry(vault, {"id": "b", "action": "trashed", "at": "t2"})
    raw = (vault / LOG_FILENAME).read_text(encoding="utf-8")
    assert raw.count("\n") == 2


def test_append_preserves_non_ascii(vault: Path) -> None:
    append_log_entry(
        vault, {"id": "a", "title": "蛋白质设计", "action": "trashed", "at": "t"}
    )
    rows = _read_rows(vault)
    assert rows[0]["title"] == "蛋白质设计"
    # Stored as raw UTF-8, not \uXXXX escapes.
    assert "蛋白质设计" in (vault / LOG_FILENAME).read_text(encoding="utf-8")
