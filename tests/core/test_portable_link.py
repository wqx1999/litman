"""Tests for ``core.portable_link`` — cross-platform folder-link helper (ADR-005).

The POSIX success path (relative symlinks) is exercised for real on
Linux/macOS. The Windows path (junctions) is dispatch-tested through litman's
own ``_create_junction`` seam, plus real-junction tests that only run on a
Windows host. The degrade path mocks the underlying ``Path.symlink_to`` /
``_create_junction`` to raise ``OSError`` so the warning + return-False
contract is verified without needing an exFAT drive.
"""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest
from rich.console import Console

import litman.core.portable_link as portable_link
from litman.core.portable_link import (
    is_portable_link,
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


def _refuse_links(monkeypatch: pytest.MonkeyPatch, errno: int = 13) -> None:
    """Make BOTH link mechanisms fail, as an exFAT drive would.

    Poisoning only ``Path.symlink_to`` leaves the Windows arm free to make a
    junction, so every degrade test silently exercised the success path there.
    """

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(errno, "Permission denied (mocked)")

    monkeypatch.setattr(Path, "symlink_to", boom)
    monkeypatch.setattr(portable_link, "_create_junction", boom)


def _dir_with(tmp_path: Path, name: str, body: str) -> Path:
    """A directory target carrying one readable file.

    Every link litman makes points at a paper DIRECTORY, and a Windows
    junction can only point at a directory in the first place — so a
    file-to-file link tests a shape the product never builds and the platform
    cannot express. Linking dirs keeps these tests portable AND closer to
    what production does.
    """
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "f.txt").write_text(body, encoding="utf-8")
    return d


def test_make_portable_link_creates_link(tmp_path: Path) -> None:
    target = _dir_with(tmp_path, "target", "hi")
    link = tmp_path / "link"

    ok = make_portable_link(link, target)
    assert ok is True
    assert is_portable_link(link)
    assert (link / "f.txt").read_text(encoding="utf-8") == "hi"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a junction stores an absolute target by construction (ADR-005 "
    "accepts that); relative storage is the symlink arm's property",
)
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
    target = _dir_with(tmp_path, "target", "ok")
    link = tmp_path / "deep" / "nested" / "link"
    assert not link.parent.exists()

    ok = make_portable_link(link, target)
    assert ok is True
    assert is_portable_link(link)


def test_make_portable_link_overwrites_existing_symlink(
    tmp_path: Path,
) -> None:
    """Upsert semantics: stale link is replaced, not stacked."""
    target1 = _dir_with(tmp_path, "t1", "one")
    target2 = _dir_with(tmp_path, "t2", "two")
    link = tmp_path / "link"

    make_portable_link(link, target1)
    assert (link / "f.txt").read_text(encoding="utf-8") == "one"
    make_portable_link(link, target2)
    assert (link / "f.txt").read_text(encoding="utf-8") == "two"


def test_make_portable_link_overwrites_existing_regular_file(
    tmp_path: Path,
) -> None:
    """If a real file sits at link_path, it is removed first."""
    target = _dir_with(tmp_path, "target", "from-link")
    link = tmp_path / "link"
    link.write_text("stale-real-file", encoding="utf-8")

    ok = make_portable_link(link, target)
    assert ok is True
    assert is_portable_link(link)
    assert (link / "f.txt").read_text(encoding="utf-8") == "from-link"


# ---------------------------------------------------------------------------
# remove_link_if_present
# ---------------------------------------------------------------------------


def test_remove_link_if_present_removes_symlink(tmp_path: Path) -> None:
    target = _dir_with(tmp_path, "target", "x")
    link = tmp_path / "link"
    make_portable_link(link, target)
    assert is_portable_link(link)

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
    target = _dir_with(tmp_path, "target", "x")
    link = tmp_path / "link"

    # Force the warning console to write to a deterministic buffer.
    # Reading capsys directly is fragile with Rich; instead replace the
    # module-level Console with one that goes to stderr but isn't captured
    # — we verify the return value here, and verify the warning was emitted
    # in a separate test via a custom Console.
    _refuse_links(monkeypatch)

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
    target = _dir_with(tmp_path, "target", "x")
    _refuse_links(monkeypatch)

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
    target = _dir_with(tmp_path, "target", "x")
    _refuse_links(monkeypatch)

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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the POSIX-host behaviour of _create_junction; on a real "
    "Windows kernel it succeeds, which is the point of the junction arm",
)
def test_create_junction_off_windows_raises_oserror(tmp_path: Path) -> None:
    """The real ``_create_junction`` on a POSIX host must surface ``OSError``
    (no ``_winapi``, no ``cmd``) — the one exception type the degrade path
    catches — so a forced-win32 probe reads deterministically as "none"."""
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(OSError):
        portable_link._create_junction(tmp_path / "lnk", target)


# ---------------------------------------------------------------------------
# is_portable_link — the single link-detection predicate
# ---------------------------------------------------------------------------


def test_is_portable_link_symlink_true(tmp_path: Path) -> None:
    target = tmp_path / "target-dir"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert is_portable_link(link) is True


def test_is_portable_link_real_entries_false(tmp_path: Path) -> None:
    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    real_file = tmp_path / "real.txt"
    real_file.write_text("hi", encoding="utf-8")
    assert is_portable_link(real_dir) is False
    assert is_portable_link(real_file) is False
    assert is_portable_link(tmp_path / "absent") is False


def test_is_portable_link_junction_true(tmp_path, fake_junction) -> None:
    """Windows regression (2026-07-14 manual round): junctions answer
    ``is_junction()`` only, never ``is_symlink()`` — bare ``is_symlink()``
    detection made every litman link invisible on NTFS."""
    link = fake_junction(tmp_path / "junction")
    assert not link.is_symlink()
    assert is_portable_link(link) is True


def test_remove_link_if_present_removes_junction_stand_in(
    tmp_path, fake_junction
) -> None:
    """The unlink→rmdir fallback removes a junction-shaped entry (a directory
    to POSIX delete calls) without recursing into anything."""
    link = fake_junction(tmp_path / "junction")
    assert remove_link_if_present(link) is True
    assert not link.exists()


@pytest.mark.skipif(
    portable_link.sys.platform != "win32",
    reason="real junctions need a Windows kernel",
)
class TestRealJunctionsOnWindows:
    """Live verification on a real Windows host (wangq's manual round covers
    the same ground via the CLI; any on-Windows pytest run picks these up).
    They pin the two load-bearing stdlib facts the Linux suite can only
    assert through the seam."""

    def test_link_traverses_and_reads_as_junction(self, tmp_path: Path) -> None:
        target = tmp_path / "papers-dir"
        target.mkdir()
        (target / "inside.txt").write_text("hi", encoding="utf-8")
        link = tmp_path / "junction"

        assert make_portable_link(link, target) is True
        # A junction is a mount-point reparse point, NOT a symlink: Python
        # answers is_junction() only. This is why every detection site must
        # go through is_portable_link() — bare is_symlink() sees nothing.
        assert not link.is_symlink()
        assert link.is_junction()
        assert is_portable_link(link)
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


def test_make_portable_link_on_real_directory_warns_in_plain_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real folder in the link position is reported as a real folder.

    Letting ``unlink()`` discover it hands the user the OS's words — on
    Windows "[WinError 5] Access is denied" — and sends them looking for a
    permissions problem that does not exist. The folder itself is untouched
    either way.
    """
    target = _dir_with(tmp_path, "target", "x")
    occupied = _dir_with(tmp_path, "link", "mine")

    print_calls: list[tuple[object, ...]] = []

    class _RecordingConsole:
        def print(self, *args: object, **_kw: object) -> None:
            print_calls.append(args)

    monkeypatch.setattr(portable_link, "_console", _RecordingConsole())

    ok = make_portable_link(occupied, target)

    assert ok is False
    assert occupied.is_dir() and not is_portable_link(occupied)
    assert (occupied / "f.txt").read_text(encoding="utf-8") == "mine"
    assert len(print_calls) == 1
    said = " ".join(str(a) for a in print_calls[0])
    assert "is a real folder, not a litman link" in said
    assert "lit health-check --fix" in said
    assert "Errno" not in said
    assert "WinError" not in said
    assert "could not replace existing entry" not in said


# ---------------------------------------------------------------------------
# Warning text: what Rich actually renders, not what we hand it
# ---------------------------------------------------------------------------


def _render(monkeypatch: pytest.MonkeyPatch, fn: object, *args: object) -> str:
    """Run a warning through a REAL Rich console and return the output.

    A recording stub captures the markup string, which is exactly the thing
    that cannot show a markup bug: `[draft]` in a path looks fine until Rich
    parses it as a style tag and drops it.
    """
    buf = io.StringIO()
    monkeypatch.setattr(
        portable_link, "_console", Console(file=buf, width=400, no_color=True)
    )
    fn(*args)  # type: ignore[operator]
    return buf.getvalue()


def test_warnings_keep_square_brackets_in_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder named ``proj [draft]`` must survive into the message.

    These are the messages whose whole job is "go and clear that folder";
    handing back a path that does not exist is worse than saying nothing.
    """
    bracketed = Path("/tmp/proj [draft]/litman_reflib/p1")

    unmovable = _render(
        monkeypatch,
        portable_link.warn_hub_entry_unmovable,
        bracketed,
        OSError(13, "Permission denied"),
    )
    assert "proj [draft]" in unmovable
    assert "Permission denied" in unmovable

    is_dir = _render(
        monkeypatch,
        portable_link._warn_link_obstructed,
        bracketed,
        IsADirectoryError(f"{bracketed} is a directory"),
    )
    assert "proj [draft]" in is_dir
    assert "is a real folder, not a litman link" in is_dir

    other = _render(
        monkeypatch,
        portable_link._warn_link_obstructed,
        bracketed,
        OSError(16, "Device or resource busy"),
    )
    assert "proj [draft]" in other
    assert "Device or resource busy" in other


def test_unmovable_warning_drops_the_errno_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``strerror``, not ``str(err)``: the errno and the repeated path are
    noise in a message that already names the folder."""
    out = _render(
        monkeypatch,
        portable_link.warn_hub_entry_unmovable,
        Path("/tmp/proj/litman_reflib/p1"),
        OSError(13, "Permission denied", "/tmp/proj/litman_reflib/p1"),
    )
    assert "Permission denied" in out
    assert "Errno" not in out


def test_copytree_failure_list_collapses_to_one_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shutil.Error`` is an ``OSError``, so ``except OSError`` catches it —
    and it stringifies to one tuple per failed file. Reachable on the target
    machine: project on D:, vault on C: → EXDEV → copytree → a locked PDF.
    """
    err = shutil.Error(
        [
            (f"/x/f{i}.pdf", f"/y/f{i}.pdf", f"[Errno 13] Permission denied: '/x/f{i}.pdf'")
            for i in range(3)
        ]
    )
    assert len(str(err)) > 200  # what would have been printed raw

    reason = portable_link._os_error_reason(err)
    assert len(reason) <= portable_link._MAX_REASON_CHARS
    assert "f0.pdf" in reason
    assert "(+2 more)" in reason

    out = _render(
        monkeypatch,
        portable_link.warn_hub_entry_unmovable,
        Path("/tmp/proj/litman_code/repo"),
        err,
    )
    assert "f2.pdf" not in out
    assert "(+2 more)" in out


def test_os_error_reason_is_capped_whatever_it_is() -> None:
    long_err = OSError(13, "x" * 500)
    reason = portable_link._os_error_reason(long_err)
    assert len(reason) <= portable_link._MAX_REASON_CHARS
    assert reason.endswith("…")
    # A bare OSError with no strerror still says something.
    assert portable_link._os_error_reason(OSError("boom")) == "boom"
