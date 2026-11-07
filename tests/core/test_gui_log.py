"""The GUI log file: one JSON object per line beside the registry, capped, never in the way.

Every test runs against the per-test registry dir the suite's isolation sets
up, so ``log_path()`` is this test's own file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litman.core import gui_log
from litman.core.gui_log import (
    GuiLog,
    entry_time,
    log_files,
    log_path,
    old_log_path,
    read_entries,
    remove_log_files,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _record(log: GuiLog, text: str = "It failed.", **fields: object) -> bool:
    return log.record(kind="error", text=text, **fields)  # type: ignore[arg-type]


def test_an_entry_is_written_beside_the_registry_and_read_back(tmp_path: Path) -> None:
    vault = tmp_path / "lib"

    assert _record(
        GuiLog(),
        "Project 'p' resolves to /x, but that folder does not exist on this machine.",
        vault=vault,
        route="POST /api/paper/a/project",
        status=400,
        code="project-path-missing",
        params={"project": "p"},
    )

    assert log_path().name == "gui-log.jsonl"
    assert log_path().parent == Path(gui_log.registry_path()).parent
    [entry] = read_entries()
    assert entry["vault"] == str(vault)
    assert entry["route"] == "POST /api/paper/a/project"
    assert (entry["status"], entry["kind"], entry["code"]) == (400, "error", "project-path-missing")
    assert entry["params"] == {"project": "p"}
    assert entry["text"].startswith("Project 'p' resolves to /x")
    assert entry["traceback"] is None
    assert entry_time(entry) is not None
    # One object per line, and nothing else in the file.
    lines = log_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == entry


def test_the_same_failure_within_the_window_is_written_once() -> None:
    clock = _Clock()
    log = GuiLog(clock=clock)

    assert _record(log, "The library is no longer at /x.", status=410, code="vault-gone")
    clock.now += 60
    assert not _record(log, "The library is no longer at /x.", status=410, code="vault-gone")
    assert len(read_entries()) == 1

    # The reverse: another file, or the same one past the window, is written.
    assert _record(log, "The library is no longer at /x.", status=410, code="vault-gone",
                   params={"file": "si-02.pdf"})
    clock.now += gui_log.REPEAT_WINDOW_S
    assert _record(log, "The library is no longer at /x.", status=410, code="vault-gone")
    assert len(read_entries()) == 3


def test_a_long_text_is_cut_and_a_traceback_keeps_its_end() -> None:
    text = "x" * (gui_log.TEXT_CAP + 500)
    trace = "head\n" + "y" * gui_log.TRACEBACK_CAP + "\nRuntimeError: the line that matters"

    assert GuiLog().record(kind="unexpected", text=text, traceback=trace)

    [entry] = read_entries()
    assert len(entry["text"]) == gui_log.TEXT_CAP and entry["text"].endswith("…")
    assert len(entry["traceback"]) == gui_log.TRACEBACK_CAP
    assert entry["traceback"].startswith("…")
    assert entry["traceback"].endswith("RuntimeError: the line that matters")


def test_a_full_log_moves_aside_and_both_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_log, "MAX_BYTES", 600)
    log = GuiLog()

    for n in range(6):
        assert _record(log, f"failure number {n} " + "z" * 100)

    assert old_log_path().is_file() and log_path().is_file()
    assert log_path().stat().st_size <= 600
    texts = [entry["text"] for entry in read_entries()]
    # Oldest first, the newest last; the moved-aside file holds the older ones.
    assert texts[-1].startswith("failure number 5")
    assert texts == sorted(texts, key=lambda t: int(t.split()[2]))
    # The reverse of the cap: under it, nothing moves.
    remove_log_files()
    monkeypatch.setattr(gui_log, "MAX_BYTES", 1 << 20)
    _record(GuiLog(), "small")
    assert not old_log_path().exists()


def test_a_line_cut_short_is_skipped_not_fatal() -> None:
    _record(GuiLog(), "first")
    with log_path().open("ab") as handle:
        handle.write(b'{"time": "2026-11-07T10:00:00+01:00", "text": "cut sh')
    _record(GuiLog(), "second")

    assert [entry["text"] for entry in read_entries()] == ["first", "second"]


def test_text_that_is_not_utf8_clean_is_still_written() -> None:
    # A lone surrogate: what an undecodable file name leaves in an OS message.
    assert _record(GuiLog(), "cannot read caf\udce9.pdf")

    [entry] = read_entries()
    assert entry["text"].startswith("cannot read caf")


def test_a_log_that_cannot_be_written_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The registry dir is a file: nothing can be created inside it.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(blocker))

    assert _record(GuiLog(), "It failed.") is False

    assert "could not write the GUI log" in capsys.readouterr().err
    # The reverse: a writable dir takes the same entry.
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "reg"))
    assert _record(GuiLog(), "It failed.") is True


def test_uninstall_helper_removes_both_files_and_the_empty_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gui_log, "MAX_BYTES", 300)
    for n in range(4):
        _record(GuiLog(), f"entry {n} " + "w" * 120)
    assert set(log_files()) == {log_path(), old_log_path()}

    result = remove_log_files()

    removed = result["removed"]
    assert isinstance(removed, list)
    assert set(removed) == {log_path(), old_log_path()}
    assert result["dir_removed"] is True
    assert not log_path().parent.exists()
    # Nothing left: a second run removes nothing and raises nothing.
    assert remove_log_files()["removed"] == []
