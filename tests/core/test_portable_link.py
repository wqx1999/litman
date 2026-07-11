"""Tests for ``core.portable_link`` — cross-platform folder-link helper (ADR-005).

The POSIX success path (relative symlinks) is exercised for real on
Linux/macOS. The Windows path (junctions) is dispatch-tested through litman's
own ``_create_junction`` seam, plus real-junction tests that only run on a
Windows host. The degrade path mocks the underlying ``Path.symlink_to`` /
``_create_junction`` to raise ``OSError`` so the warning + return-False
contract is verified without needing an exFAT drive.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

import litman.core.portable_link as portable_link
from litman.core.portable_link import (
    make_portable_link,
    remove_link_if_present,
    reset_warning_state,
)


@pytest.fixture(autouse=True)
def _reset_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the once-per-process warning latch before each test."""
    reset_warning_state()


# ---------------------------------------------------------------------------
# Success path — real symlinks on the host filesystem
# ---------------------------------------------------------------------------


def test_make_portable_link_creates_link(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("hi", encoding="utf-8")
    link = tmp_path / "link.txt"

    ok = make_portable_link(link, target)
    assert ok is True
    assert link.is_symlink()
    assert link.read_text(encoding="utf-8") == "hi"


def test_make_portable_link_stores_relative_target(tmp_path: Path) -> None:
    """The stored target must be relative — preserves cross-machine cp -r."""
    target = tmp_path / "subdir" / "target.txt"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    make_portable_link(link, target)

    # readlink returns the literal stored target. Must NOT be absolute.
    stored = link.readlink()
    assert not stored.is_absolute()
    # And it must resolve back to target.
    assert (link.parent / stored).resolve() == target.resolve()


def test_make_portable_link_creates_missing_parent(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("ok", encoding="utf-8")
    link = tmp_path / "deep" / "nested" / "link.txt"
    assert not link.parent.exists()

    ok = make_portable_link(link, target)
    assert ok is True
    assert link.is_symlink()


def test_make_portable_link_overwrites_existing_symlink(
    tmp_path: Path,
) -> None:
    """Upsert semantics: stale link is replaced, not stacked."""
    target1 = tmp_path / "t1.txt"
    target2 = tmp_path / "t2.txt"
    target1.write_text("one", encoding="utf-8")
    target2.write_text("two", encoding="utf-8")
    link = tmp_path / "link.txt"

    make_portable_link(link, target1)
    assert link.read_text(encoding="utf-8") == "one"
    make_portable_link(link, target2)
    assert link.read_text(encoding="utf-8") == "two"


def test_make_portable_link_overwrites_existing_regular_file(
    tmp_path: Path,
) -> None:
    """If a real file sits at link_path, it is removed first."""
    target = tmp_path / "target.txt"
    target.write_text("from-link", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.write_text("stale-real-file", encoding="utf-8")

    ok = make_portable_link(link, target)
    assert ok is True
    assert link.is_symlink()
    assert link.read_text(encoding="utf-8") == "from-link"


# ---------------------------------------------------------------------------
# remove_link_if_present
# ---------------------------------------------------------------------------


def test_remove_link_if_present_removes_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    make_portable_link(link, target)
    assert link.is_symlink()

    assert remove_link_if_present(link) is True
    assert not link.exists()
    # The target file itself MUST survive.
    assert target.exists()


def test_remove_link_if_present_ignores_real_file(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("data", encoding="utf-8")
    # Refuse to delete a real file masquerading at the link path.
    assert remove_link_if_present(real) is False
    assert real.exists()


def test_remove_link_if_present_nonexistent_path(tmp_path: Path) -> None:
    assert remove_link_if_present(tmp_path / "nothing") is False


# ---------------------------------------------------------------------------
# Graceful degrade — OSError from the underlying symlink_to (ADR-005)
# ---------------------------------------------------------------------------


def test_make_portable_link_degrades_on_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the filesystem refuses symlinks, we warn once and return False.
    Caller still gets a clean return value — no exception propagates."""
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"

    def fake_symlink_to(self: Path, _target: str | Path, **_kw: object) -> None:
        raise OSError(13, "Permission denied (mocked)")

    # Force the warning console to write to a deterministic buffer.
    # Reading capsys directly is fragile with Rich; instead replace the
    # module-level Console with one that goes to stderr but isn't captured
    # — we verify the return value here, and verify the warning was emitted
    # in a separate test via a custom Console.
    monkeypatch.setattr(Path, "symlink_to", fake_symlink_to)

    ok = make_portable_link(link, target)
    assert ok is False
    assert not link.exists()


def test_degraded_warning_emits_once_per_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated failures inside one process emit only the first warning.

    We count emissions by stubbing the module-level Console so we don't
    couple the test to Rich's word-wrapping (which would split the hint
    text across lines and break substring matching).
    """
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")

    def fake_symlink_to(self: Path, _target: str | Path, **_kw: object) -> None:
        raise OSError(13, "boom")

    monkeypatch.setattr(Path, "symlink_to", fake_symlink_to)

    print_calls: list[tuple[object, ...]] = []

    class _RecordingConsole:
        def print(self, *args: object, **_kw: object) -> None:
            print_calls.append(args)

    monkeypatch.setattr(portable_link, "_console", _RecordingConsole())

    reset_warning_state()
    for i in range(5):
        make_portable_link(tmp_path / f"link-{i}.txt", target)

    # 5 failed calls, but only the first should print a warning.
    assert len(print_calls) == 1


def test_warning_state_resets_for_isolated_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reset_warning_state()`` re-arms the latch so a fresh test
    observes a fresh first emission."""
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")

    def fake_symlink_to(self: Path, _target: str | Path, **_kw: object) -> None:
        raise OSError(13, "boom")

    monkeypatch.setattr(Path, "symlink_to", fake_symlink_to)

    print_calls: list[tuple[object, ...]] = []

    class _RecordingConsole:
        def print(self, *args: object, **_kw: object) -> None:
            print_calls.append(args)

    monkeypatch.setattr(portable_link, "_console", _RecordingConsole())

    reset_warning_state()
    make_portable_link(tmp_path / "link-a.txt", target)
    assert len(print_calls) == 1

    # Re-arm and verify a second first emission happens.
    reset_warning_state()
    make_portable_link(tmp_path / "link-b.txt", target)
    assert len(print_calls) == 2


# ---------------------------------------------------------------------------
# Platform dispatch — Windows creates junctions, POSIX creates symlinks
# ---------------------------------------------------------------------------


def test_win32_creates_a_junction_and_never_asks_for_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On win32 every link is a junction — symlink privilege is never touched.

    This is the download-and-it-works contract: no Developer Mode, no
    elevation. ``_create_junction`` is faked at litman's own seam (a real
    junction needs a Windows kernel); ``Path.symlink_to`` is poisoned so any
    fallback to symlinks fails the test loudly.
    """
    created: list[tuple[Path, Path]] = []

    def poisoned(self: Path, *_a: object, **_k: object) -> None:
        raise AssertionError("win32 must never create symlinks")

    monkeypatch.setattr(portable_link.sys, "platform", "win32")
    monkeypatch.setattr(
        portable_link,
        "_create_junction",
        lambda link, target: created.append((link, target)),
    )
    monkeypatch.setattr(Path, "symlink_to", poisoned)

    target = tmp_path / "papers-dir"
    target.mkdir()
    link = tmp_path / "views-entry"

    assert make_portable_link(link, target) is True
    assert created == [(link, target)]


def test_win32_junction_failure_degrades_not_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drive that refuses junctions (FAT32/exFAT) degrades exactly like the
    POSIX symlink-refused path: warn once, return False, no exception."""
    monkeypatch.setattr(portable_link.sys, "platform", "win32")

    def refusing(link: Path, target: Path) -> None:
        raise OSError(4390, "The file or directory is not a reparse point")

    monkeypatch.setattr(portable_link, "_create_junction", refusing)

    target = tmp_path / "papers-dir"
    target.mkdir()

    assert make_portable_link(tmp_path / "entry", target) is False


def test_create_junction_off_windows_raises_oserror(tmp_path: Path) -> None:
    """The real ``_create_junction`` on a POSIX host must surface ``OSError``
    (no ``_winapi``, no ``cmd``) — the one exception type the degrade path
    catches — so a forced-win32 probe reads deterministically as "none"."""
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(OSError):
        portable_link._create_junction(tmp_path / "lnk", target)


@pytest.mark.skipif(
    portable_link.sys.platform != "win32",
    reason="real junctions need a Windows kernel",
)
class TestRealJunctionsOnWindows:
    """Live verification on a real Windows host (wangq's manual round covers
    the same ground via the CLI; any on-Windows pytest run picks these up).
    They pin the two load-bearing stdlib facts the Linux suite can only
    assert through the seam."""

    def test_link_traverses_and_reads_as_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "papers-dir"
        target.mkdir()
        (target / "inside.txt").write_text("hi", encoding="utf-8")
        link = tmp_path / "junction"

        assert make_portable_link(link, target) is True
        # Junctions must read as symlinks to every litman detection site.
        assert link.is_symlink()
        assert link.is_junction()
        assert (link / "inside.txt").read_text(encoding="utf-8") == "hi"

    def test_remove_spares_the_target(self, tmp_path: Path) -> None:
        target = tmp_path / "papers-dir"
        target.mkdir()
        (target / "inside.txt").write_text("hi", encoding="utf-8")
        link = tmp_path / "junction"
        make_portable_link(link, target)

        assert remove_link_if_present(link) is True
        assert not link.exists()
        assert (target / "inside.txt").exists()
