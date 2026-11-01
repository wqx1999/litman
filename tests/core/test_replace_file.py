"""``replace_file``: a program holding a file for a moment must not stop a write.

The holder is faked at ``os.replace`` here, with a fake clock, on every platform.
The last test holds a real file on a real Windows.
"""

from __future__ import annotations

import ast
import errno
import os
import threading
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import litman
from litman.core import replace as replace_mod
from litman.core.replace import rename_path, replace_file

_ROOT = Path(litman.__file__).parent
# Where os.replace and os.rename may be called: nothing else may go round the wait.
_MAY_CALL_IT_BARE = {"core/replace.py"}


def _held(winerror: int | None) -> PermissionError:
    err = PermissionError(errno.EACCES, "Access is denied")
    if winerror is not None:
        err.winerror = winerror  # type: ignore[attr-defined]  # Windows sets it itself
    return err


class _Clock:
    """Time that passes only when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(
        replace_mod,
        "time",
        types.SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep),
    )
    return fake


def _on(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(replace_mod, "_platform", lambda: platform)


def _replace_failing(
    monkeypatch: pytest.MonkeyPatch, failure: Callable[[int], OSError | None]
) -> list[Any]:
    """Make ``os.replace`` fail as ``failure(call number)`` says, then really run."""
    calls: list[Any] = []
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        calls.append(src)
        err = failure(len(calls))
        if err is not None:
            raise err
        real(src, dst)

    monkeypatch.setattr(replace_mod.os, "replace", replace)
    return calls


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "new.tmp", tmp_path / "file"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    return src, dst


@pytest.mark.parametrize(
    "winerror", [5, 32], ids=["access-denied", "sharing-violation"]
)
def test_a_folder_held_for_a_moment_is_renamed_in_the_end(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path, winerror: int
) -> None:
    _on(monkeypatch, "win32")
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    real = os.rename
    calls: list[Any] = []

    def rename(a: Any, b: Any) -> None:
        calls.append(a)
        if len(calls) <= 3:
            raise _held(winerror)
        real(a, b)

    monkeypatch.setattr(replace_mod.os, "rename", rename)

    rename_path(src, dst)

    assert len(calls) == 4
    assert dst.is_dir()
    assert clock.sleeps == [0.01, 0.02, 0.04]


def test_a_rename_gets_the_same_budget_and_the_same_refusals(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, tmp_path: Path
) -> None:
    _on(monkeypatch, "win32")
    calls: list[Any] = []

    def rename(a: Any, _b: Any) -> None:
        calls.append(a)
        raise _held(5)

    monkeypatch.setattr(replace_mod.os, "rename", rename)

    with pytest.raises(PermissionError):
        rename_path(tmp_path / "a", tmp_path / "b")

    assert clock.now == pytest.approx(2.0)
    assert len(calls) > 5

    calls.clear()
    clock.now = 0.0

    def other(a: Any, _b: Any) -> None:
        calls.append(a)
        raise _held(1314)

    monkeypatch.setattr(replace_mod.os, "rename", other)

    with pytest.raises(PermissionError):
        rename_path(tmp_path / "a", tmp_path / "b")

    assert len(calls) == 1
    assert clock.now == 0.0


@pytest.mark.parametrize("winerror", [5, 32])
def test_a_file_held_for_a_moment_is_replaced_in_the_end(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    files: tuple[Path, Path],
    winerror: int,
) -> None:
    _on(monkeypatch, "win32")
    calls = _replace_failing(
        monkeypatch, lambda n: _held(winerror) if n <= 3 else None
    )
    src, dst = files

    replace_file(src, dst)

    assert len(calls) == 4
    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()
    assert clock.sleeps == [0.01, 0.02, 0.04]


def test_a_file_never_let_go_raises_once_the_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, files: tuple[Path, Path]
) -> None:
    _on(monkeypatch, "win32")
    last: list[PermissionError] = []

    def always(_n: int) -> OSError:
        last.append(_held(5))
        return last[-1]

    calls = _replace_failing(monkeypatch, always)
    src, dst = files

    with pytest.raises(PermissionError) as raised:
        replace_file(src, dst)

    assert raised.value is last[-1]
    assert clock.now == pytest.approx(2.0)
    assert max(clock.sleeps) <= 0.2
    assert len(calls) > 5
    assert dst.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(
    "failure",
    [
        _held(None),
        _held(1314),
        FileNotFoundError(errno.ENOENT, "gone"),
        OSError(errno.ENOSPC, "No space left on device"),
    ],
    ids=["no-code", "another-code", "not-found", "disk-full"],
)
def test_any_other_failure_is_raised_at_once(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    files: tuple[Path, Path],
    failure: OSError,
) -> None:
    _on(monkeypatch, "win32")
    calls = _replace_failing(monkeypatch, lambda _n: failure)

    with pytest.raises(type(failure)):
        replace_file(*files)

    assert len(calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_never_waits(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    files: tuple[Path, Path],
    platform: str,
) -> None:
    _on(monkeypatch, platform)
    calls = _replace_failing(monkeypatch, lambda _n: _held(5))

    with pytest.raises(PermissionError):
        replace_file(*files)

    assert len(calls) == 1
    assert clock.sleeps == []


def test_it_replaces_for_real_with_nothing_faked(files: tuple[Path, Path]) -> None:
    src, dst = files
    replace_file(src, dst)
    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to replace an open file")
def test_a_really_open_file_is_replaced_once_it_is_closed(
    files: tuple[Path, Path],
) -> None:
    src, dst = files
    holder = dst.open("rb")
    closer = threading.Timer(0.2, holder.close)
    try:
        # Without this the test could pass with nothing held.
        with pytest.raises(PermissionError):
            os.replace(src, dst)
        closer.start()
        started = time.monotonic()
        replace_file(src, dst)
        assert time.monotonic() - started < 2.0
    finally:
        closer.cancel()
        holder.close()

    assert dst.read_text(encoding="utf-8") == "new"


def _bare_replaces(source: str) -> list[str]:
    """``os.replace(...)`` and ``<path>.replace(<target>)`` in ``source``.

    One argument and no keyword is what tells a path's ``replace`` from a
    string's, which takes two, and a datetime's, which takes keywords.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "replace":
            continue
        is_os = isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
        if is_os or (len(node.args) == 1 and not node.keywords):
            found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


_FILES = sorted(
    p
    for p in _ROOT.rglob("*.py")
    if p.relative_to(_ROOT).as_posix() not in _MAY_CALL_IT_BARE
)


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.relative_to(_ROOT).as_posix())
def test_every_replace_goes_through_replace_file(path: Path) -> None:
    assert _bare_replaces(path.read_text(encoding="utf-8")) == []


def _bare_renames(source: str) -> list[str]:
    """``os.rename(...)`` and ``<path>.rename(<target>)`` in ``source``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "rename":
            continue
        is_os = isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
        if is_os or (len(node.args) == 1 and not node.keywords):
            found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


def _shutil_moves(source: str) -> list[str]:
    """``shutil.move(...)``, called or imported, in ``source``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "move"
            and isinstance(node.value, ast.Name)
            and node.value.id == "shutil"
        ):
            found.append(f"{node.lineno}: {ast.unparse(node)}")
        if isinstance(node, ast.ImportFrom) and node.module == "shutil":
            found += [f"{node.lineno}: {a.name}" for a in node.names if a.name == "move"]
    return found


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.relative_to(_ROOT).as_posix())
def test_every_rename_goes_through_rename_path(path: Path) -> None:
    assert _bare_renames(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "path", sorted(_ROOT.rglob("*.py")), ids=lambda p: p.relative_to(_ROOT).as_posix()
)
def test_nothing_uses_shutil_move(path: Path) -> None:
    """It copies after ANY failed rename, then deletes the source."""
    assert _shutil_moves(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("os.rename(a, b)", 1),
        ("src.rename(dest)", 1),
        ("Path(x).rename(dest)", 1),
        ("rename_path(a, b)", 0),
        ("frame.rename(columns=c)", 0),
    ],
)
def test_the_rename_scan_tells_them_apart(source: str, found: int) -> None:
    assert len(_bare_renames(source)) == found


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("shutil.move(a, b)", 1),
        ("from shutil import move", 1),
        ("shutil.copytree(a, b)", 0),
        ("stage.move_dir(a, b)", 0),
        ("from shutil import copy2", 0),
    ],
)
def test_the_move_scan_tells_them_apart(source: str, found: int) -> None:
    assert len(_shutil_moves(source)) == found


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("os.replace(a, b)", 1),
        ("tmp.replace(path)", 1),
        ("Path(x).replace(path)", 1),
        ("replace_file(a, b)", 0),
        ("text.replace('a', 'b')", 0),
        ("when.replace(tzinfo=UTC)", 0),
    ],
)
def test_the_scan_tells_them_apart(source: str, found: int) -> None:
    assert len(_bare_replaces(source)) == found
