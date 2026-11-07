"""``lit gui-log``: the GUI log, newest first, as a table at a terminal and JSON elsewhere."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from litman.cli import cli
from litman.core.gui_log import log_path
from litman.core.library import create_vault
from litman.core.vault_registry import add_vault, load_registry, save_registry


def _ago(**delta: float) -> str:
    return (datetime.now().astimezone() - timedelta(**delta)).isoformat(timespec="seconds")


def _entry(text: str, at: str, **fields: Any) -> dict[str, Any]:
    return {
        "time": at,
        "vault": None,
        "route": "PUT /api/pins/x",
        "status": 404,
        "kind": "error",
        "code": None,
        "params": {},
        "text": text,
        "traceback": None,
        **fields,
    }


def _write(*entries: dict[str, Any]) -> None:
    log_path().parent.mkdir(parents=True, exist_ok=True)
    with log_path().open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _run(*args: str) -> Result:
    result = CliRunner().invoke(cli, ["gui-log", *args])
    assert result.exit_code == 0, result.output
    return result


def test_off_a_terminal_it_prints_the_entries_as_json_newest_first() -> None:
    _write(
        _entry("older", _ago(hours=5)),
        _entry("newest", _ago(minutes=1)),
        _entry("middle", _ago(hours=1)),
    )

    entries = json.loads(_run().stdout)

    assert [e["text"] for e in entries] == ["newest", "middle", "older"]
    # The log's own objects, as they are.
    assert set(entries[0]) == {
        "time", "vault", "route", "status", "kind", "code", "params", "text", "traceback",
    }


def test_the_window_and_the_limit() -> None:
    _write(
        _entry("ten days ago", _ago(days=10)),
        *[_entry(f"recent {n}", _ago(minutes=n + 1)) for n in range(3)],
    )

    assert [e["text"] for e in json.loads(_run("--limit", "2").stdout)] == [
        "recent 0", "recent 1",
    ]
    assert "ten days ago" not in _run().output
    assert "ten days ago" in _run("--days", "11").output


def test_entries_only_older_than_the_window_say_how_far_back_to_look() -> None:
    _write(_entry("long ago", _ago(days=9, hours=1)))

    said = " ".join(_run("--format", "table").output.split())

    newest = (datetime.now().astimezone() - timedelta(days=9, hours=1)).strftime("%Y-%m-%d")
    assert f"No entries in the last 7 days; the newest is from {newest}." in said
    assert "--days 10 to see it." in said
    # JSON stays a plain list.
    assert json.loads(_run().stdout) == []


def test_an_empty_log() -> None:
    assert json.loads(_run().stdout) == []
    assert "The GUI log is empty" in _run("--format", "table").output


def test_the_table_names_the_library_and_shows_the_full_text(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    vault = create_vault(tmp_path / "home")
    save_registry(add_vault(load_registry(), "thesis", vault))
    _write(
        _entry(
            "Project 'p' resolves to /x, but that folder does not exist on this machine.\n"
            'Run `lit project set-path p "<new-path>"` to point it at where the folder is now.',
            _ago(minutes=3),
            vault=str(vault),
            route="POST /api/paper/a/project",
            status=400,
            code="project-path-missing",
        ),
        _entry(
            "RuntimeError: boom",
            _ago(minutes=2),
            kind="unexpected",
            code="unexpected",
            status=500,
            traceback="Traceback (most recent call last):\nRuntimeError: boom",
        ),
    )

    out = _run("--format", "table").output
    said = " ".join(out.split())

    assert "thesis" in said and str(vault) not in said
    assert "POST /api/paper/a/project" in said and "project-path-missing" in said
    assert 'Run `lit project set-path p "<new-path>"` to point it at where the folder' in said
    assert "traceback: see --format json" in said
    # The traceback itself only in JSON.
    assert "most recent call last" not in out
    assert "most recent call last" in json.loads(_run().stdout)[0]["traceback"]


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="chmod 000 does not make a file unreadable on Windows or for root",
)
def test_an_unreadable_log_says_so() -> None:
    _write(_entry("x", _ago(minutes=1)))
    log_path().chmod(0)
    try:
        result = CliRunner().invoke(cli, ["gui-log"])
    finally:
        log_path().chmod(0o644)

    assert result.exit_code == 1
    assert "Cannot read" in str(result.exception)
    assert "lit gui-log" in str(result.exception)
