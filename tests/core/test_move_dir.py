"""Moving a paper's folder when a file in it is open.

``shutil.move`` copies after any failed rename and then deletes the source, so a
PDF open in a viewer left a copy in ``.trash`` and the folder missing files. The
refusal is faked at ``os.rename`` on every platform; the tests at the end hold a
real file on a real Windows.
"""

from __future__ import annotations

import errno
import os
import shutil
import threading
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core import atomic, project_link
from litman.core import replace as replace_mod
from litman.core.library import create_vault
from litman.core.locking import lock_truth_file
from litman.core.replace import move_dir
from litman.core.trash import _sidecar_path, list_trash, move_to_trash
from tests.commands.test_link import _make_paper

PAPER = "2024_Foo_Bar"

_ON_WINDOWS = pytest.mark.skipif(
    os.name != "nt", reason="only Windows refuses to move a folder with a file open"
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    vault = create_vault(tmp_path)
    folder = _make_paper(vault, PAPER)
    (folder / "paper.pdf").write_bytes(b"%PDF-1.7 held")
    (folder / "notes.md").write_text("mine\n", encoding="utf-8")
    return vault


def _folder(vault: Path) -> Path:
    return vault / "papers" / PAPER


def _contents(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in folder.rglob("*")
        if p.is_file()
    }


def _nothing_in_trash(vault: Path) -> bool:
    return list((vault / ".trash").iterdir()) == []


def _refuse_rename(monkeypatch: Any, err: OSError) -> None:
    def rename(_src: Any, _dst: Any) -> None:
        raise err

    monkeypatch.setattr(os, "rename", rename)


class _Clock:
    """Time that passes only when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize(
    "refusal",
    [
        PermissionError(errno.EACCES, "Access is denied"),
        OSError(errno.EBUSY, "Device or resource busy"),
    ],
    ids=["access-denied", "busy"],
)
def test_a_refused_move_to_trash_touches_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch, refusal: OSError
) -> None:
    before = _contents(_folder(vault))
    _refuse_rename(monkeypatch, refusal)

    with pytest.raises(type(refusal)):
        move_to_trash(vault, PAPER)

    assert _contents(_folder(vault)) == before
    assert _nothing_in_trash(vault)


def test_a_cross_device_move_to_trash_copies_and_keeps_its_sidecar(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The read-only metadata.yaml is what a bare rmtree trips over on Windows.
    lock_truth_file(_folder(vault) / "metadata.yaml")
    before = _contents(_folder(vault))
    _refuse_rename(monkeypatch, OSError(errno.EXDEV, "Invalid cross-device link"))

    entry = move_to_trash(vault, PAPER)

    assert _contents(entry) == before
    assert not _folder(vault).exists()
    assert _sidecar_path(entry).is_file()


def test_a_copy_that_fails_leaves_no_half_entry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _contents(_folder(vault))
    _refuse_rename(monkeypatch, OSError(errno.EXDEV, "Invalid cross-device link"))

    def copytree(_src: Any, dst: Any, **_kw: Any) -> None:
        Path(dst).mkdir()
        (Path(dst) / "half").write_text("x", encoding="utf-8")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", copytree)

    with pytest.raises(OSError, match="No space"):
        move_to_trash(vault, PAPER)

    assert _contents(_folder(vault)) == before
    assert _nothing_in_trash(vault)


def test_move_dir_moves_for_real_with_nothing_faked(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "a").write_text("a", encoding="utf-8")

    move_dir(src, dst)

    assert not src.exists()
    assert (dst / "a").read_text(encoding="utf-8") == "a"


def test_rm_with_a_refused_move_keeps_the_folder_and_can_be_run_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _contents(_folder(vault))

    with monkeypatch.context() as held:
        _refuse_rename(held, PermissionError(errno.EACCES, "Access is denied"))
        refused = CliRunner().invoke(cli, ["rm", PAPER, "--yes", "--library", str(vault)])

    assert refused.exception is None, refused.output
    assert "could not remove" in refused.output
    assert _contents(_folder(vault)) == before
    assert list_trash(vault) == []

    again = CliRunner().invoke(cli, ["rm", PAPER, "--yes", "--library", str(vault)])
    assert again.exit_code == 0, again.output
    assert not _folder(vault).exists()
    assert _contents(list_trash(vault)[0].entry_path) == before


def test_a_refused_restore_leaves_the_entry_in_the_trash(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = move_to_trash(vault, PAPER)
    before = _contents(entry)

    with monkeypatch.context() as held:
        _refuse_rename(held, PermissionError(errno.EACCES, "Access is denied"))
        refused = CliRunner().invoke(cli, ["trash", "restore", PAPER, "--library", str(vault)])

    assert isinstance(refused.exception, PermissionError)
    assert not _folder(vault).exists()
    assert _contents(entry) == before
    assert _sidecar_path(entry).is_file()


def test_a_restore_that_fails_later_puts_the_folder_back_without_copying(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The way back goes through ``move_dir`` too."""
    entry = move_to_trash(vault, PAPER)
    before = _contents(entry)
    copied: list[Any] = []
    monkeypatch.setattr(shutil, "copytree", lambda *a, **_k: copied.append(a))

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("the staged write failed")

    monkeypatch.setattr(atomic.StagedWrite, "write_text", boom)
    result = CliRunner().invoke(cli, ["trash", "restore", PAPER, "--library", str(vault)])

    assert isinstance(result.exception, RuntimeError)
    assert copied == []
    assert not _folder(vault).exists()
    assert _contents(entry) == before


def _moves() -> dict[str, Callable[[Path, Path], Any]]:
    return {
        "move_dir": move_dir,
        "_rename_dir": atomic._rename_dir,
        "_move_aside": lambda src, dst: project_link._move_aside(src, dst.parent, dst.name),
    }


@pytest.mark.parametrize("name", list(_moves()))
def test_every_folder_move_waits_out_a_moment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    clock = _Clock()
    monkeypatch.setattr(
        replace_mod,
        "time",
        types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(replace_mod, "_platform", lambda: "win32")
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    real = os.rename
    calls: list[Any] = []

    def rename(a: Any, b: Any) -> None:
        calls.append(a)
        if len(calls) <= 2:
            err = PermissionError(errno.EACCES, "Access is denied")
            err.winerror = 5  # type: ignore[attr-defined]  # Windows sets it itself
            raise err
        real(a, b)

    monkeypatch.setattr(os, "rename", rename)

    _moves()[name](src, dst)

    assert len(calls) == 3
    assert clock.now > 0
    assert dst.is_dir()
    assert not src.exists()


@_ON_WINDOWS
def test_a_really_open_file_keeps_the_folder_whole_and_the_trash_empty(vault: Path) -> None:
    before = _contents(_folder(vault))
    with open(_folder(vault) / "paper.pdf", "rb"):
        with pytest.raises(PermissionError) as refused:
            move_to_trash(vault, PAPER)
        assert replace_mod.refused_by_windows(refused.value)

    assert _contents(_folder(vault)) == before
    assert _nothing_in_trash(vault)


@_ON_WINDOWS
def test_a_file_really_open_for_a_moment_lets_the_move_finish(vault: Path) -> None:
    before = _contents(_folder(vault))
    holder = open(_folder(vault) / "paper.pdf", "rb")  # noqa: SIM115 — closed by the timer
    closer = threading.Timer(0.2, holder.close)
    try:
        # Without this the test could pass with nothing held.
        with pytest.raises(PermissionError):
            os.rename(_folder(vault), _folder(vault).with_name("elsewhere"))
        closer.start()
        entry = move_to_trash(vault, PAPER)
    finally:
        closer.cancel()
        holder.close()

    assert _contents(entry) == before
    assert not _folder(vault).exists()


@_ON_WINDOWS
def test_rm_with_the_pdf_really_open_keeps_the_folder_whole(vault: Path) -> None:
    before = _contents(_folder(vault))
    with open(_folder(vault) / "paper.pdf", "rb"):
        result = CliRunner().invoke(cli, ["rm", PAPER, "--yes", "--library", str(vault)])

    assert result.exception is None, result.output
    assert "could not remove" in result.output
    assert _contents(_folder(vault)) == before
    assert list_trash(vault) == []


@_ON_WINDOWS
def test_shutil_move_really_does_gut_a_folder_with_an_open_file(tmp_path: Path) -> None:
    """Why ``move_dir`` exists: what the same hold does to ``shutil.move``."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "a_first").write_text("a", encoding="utf-8")
    (src / "z_held").write_text("z", encoding="utf-8")

    with open(src / "z_held", "rb"), pytest.raises(PermissionError):
        shutil.move(str(src), str(dst))

    assert not (src / "a_first").exists()
    assert (dst / "a_first").is_file()
