"""Tests for ``core.portable_link`` — cross-platform symlink helper (ADR-005).

The success path is exercised on every supported OS (Linux/macOS in CI).
The degrade path is platform-conditional: on Linux we mock the
underlying ``Path.symlink_to`` to raise ``OSError`` so the warning +
return-False contract is verified without needing a Windows runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

import litman.core.portable_link as portable_link
from litman.core.portable_link import (
    make_relative_symlink,
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


def test_make_relative_symlink_creates_link(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("hi", encoding="utf-8")
    link = tmp_path / "link.txt"

    ok = make_relative_symlink(link, target)
    assert ok is True
    assert link.is_symlink()
    assert link.read_text(encoding="utf-8") == "hi"


def test_make_relative_symlink_stores_relative_target(tmp_path: Path) -> None:
    """The stored target must be relative — preserves cross-machine cp -r."""
    target = tmp_path / "subdir" / "target.txt"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    make_relative_symlink(link, target)

    # readlink returns the literal stored target. Must NOT be absolute.
    stored = link.readlink()
    assert not stored.is_absolute()
    # And it must resolve back to target.
    assert (link.parent / stored).resolve() == target.resolve()


def test_make_relative_symlink_creates_missing_parent(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("ok", encoding="utf-8")
    link = tmp_path / "deep" / "nested" / "link.txt"
    assert not link.parent.exists()

    ok = make_relative_symlink(link, target)
    assert ok is True
    assert link.is_symlink()


def test_make_relative_symlink_overwrites_existing_symlink(
    tmp_path: Path,
) -> None:
    """Upsert semantics: stale link is replaced, not stacked."""
    target1 = tmp_path / "t1.txt"
    target2 = tmp_path / "t2.txt"
    target1.write_text("one", encoding="utf-8")
    target2.write_text("two", encoding="utf-8")
    link = tmp_path / "link.txt"

    make_relative_symlink(link, target1)
    assert link.read_text(encoding="utf-8") == "one"
    make_relative_symlink(link, target2)
    assert link.read_text(encoding="utf-8") == "two"


def test_make_relative_symlink_overwrites_existing_regular_file(
    tmp_path: Path,
) -> None:
    """If a real file sits at link_path, it is removed first."""
    target = tmp_path / "target.txt"
    target.write_text("from-link", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.write_text("stale-real-file", encoding="utf-8")

    ok = make_relative_symlink(link, target)
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
    make_relative_symlink(link, target)
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


def test_make_relative_symlink_degrades_on_oserror(
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

    ok = make_relative_symlink(link, target)
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
        make_relative_symlink(tmp_path / f"link-{i}.txt", target)

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
    make_relative_symlink(tmp_path / "link-a.txt", target)
    assert len(print_calls) == 1

    # Re-arm and verify a second first emission happens.
    reset_warning_state()
    make_relative_symlink(tmp_path / "link-b.txt", target)
    assert len(print_calls) == 2
