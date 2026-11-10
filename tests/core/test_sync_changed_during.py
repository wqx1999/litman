"""What a two-way sync tells apart from changes made here while it ran.

No rclone: the backups and listings rclone leaves are written by hand, in the
form rclone 1.75.2 writes them.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from litman.core import sync as core_sync
from litman.core.sync import BisyncOps

_NOTES = "papers/p1/notes.md"


def _ns(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0,
    *, fraction_ns: int = 0,
) -> int:
    moment = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    return int(moment.timestamp()) * 1_000_000_000 + fraction_ns


def _write(path: Path, text: str, mtime_ns: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _state(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


# -- a change here that a pass then replaced ----------------------------------


def test_a_backup_unlike_the_file_before_the_sync_is_a_change_the_cloud_replaced(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write(vault / _NOTES, "# the cloud's copy, which the pass brought\n", _ns(2026, 11, 6, 9))
    backup = tmp_path / "backup-run"
    # What the pass replaced: written here after rclone listed the library.
    copy = _write(backup / _NOTES, "# written here while the sync ran\n", _ns(2026, 11, 6, 8, 30))
    before = {_NOTES: (4, _ns(2026, 11, 6, 8))}
    ops = [BisyncOps(remote_changed=False, path1_writes=frozenset({_NOTES}))]

    rows = core_sync._replaced_during(vault, before, [backup], ops)

    assert [(r.path, r.paper_id, r.kept, r.backup) for r in rows] == [
        (_NOTES, "p1", "cloud", copy)
    ]
    # The reverse: the backup is the file as it was before the sync, which
    # is the pass's own replacement.
    assert core_sync._replaced_during(vault, {_NOTES: _state(copy)}, [backup], ops) == []


def test_a_later_pass_backing_up_an_earlier_pass_s_write_is_no_change(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / _NOTES, "# third\n", _ns(2026, 11, 6, 10))
    first, second = tmp_path / "run1", tmp_path / "run2"
    _write(first / _NOTES, "# as before the sync\n", _ns(2026, 11, 6, 8))
    # What the first pass wrote, which the second pass then replaced.
    _write(second / _NOTES, "# the first pass's\n", _ns(2026, 11, 6, 9))
    before = {_NOTES: _state(first / _NOTES)}
    wrote = BisyncOps(remote_changed=False, path1_writes=frozenset({_NOTES}))

    assert core_sync._replaced_during(vault, before, [first, second], [wrote, wrote]) == []
    # The reverse: had the first pass not written it, the second pass's
    # backup is a change made here in between.
    other = BisyncOps(remote_changed=True, path1_writes=frozenset())
    rows = core_sync._replaced_during(vault, before, [first, second], [other, wrote])
    assert [r.backup for r in rows] == [second / _NOTES]


def test_partial_files_and_conflict_copies_in_a_backup_are_no_change(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    backup = tmp_path / "run"
    _write(backup / "papers/p1/paper.pdf.partial", "half", _ns(2026, 11, 6, 9))
    _write(backup / "papers/p1/notes.md.sync-conflict1", "copy", _ns(2026, 11, 6, 9))
    _write(backup / "papers/p1/.DS_Store", "finder", _ns(2026, 11, 6, 9))
    ops = [BisyncOps(remote_changed=False, path1_writes=frozenset())]

    assert core_sync._replaced_during(vault, {}, [backup], ops) == []
    # The reverse: a library file there, new since the sync began, is one.
    _write(backup / "papers/p1/new.md", "made here", _ns(2026, 11, 6, 9))
    assert [r.path for r in core_sync._replaced_during(vault, {}, [backup], ops)] == [
        "papers/p1/new.md"
    ]


# -- a file the sync wrote, written again here after it ---------------------------


def test_a_file_the_sync_wrote_and_something_wrote_after_it_is_unsynced() -> None:
    before = {_NOTES: (1, 10)}
    after = {_NOTES: (2, 30)}
    ops = [BisyncOps(remote_changed=False, path1_writes=frozenset({_NOTES}))]

    rewritten = core_sync._changed_during(before, after, ops, frozenset(), {_NOTES: (2, 20)})
    assert rewritten == ((_NOTES,), False)
    # The reverse: still as rclone wrote it.
    as_written = core_sync._changed_during(before, after, ops, frozenset(), {_NOTES: (2, 30)})
    assert as_written == ((), False)
    # No listing to tell by: the next look syncs once more.
    assert core_sync._changed_during(before, after, ops, frozenset(), None) == ((), True)
    # Deleted here after the sync wrote it.
    gone = core_sync._changed_during(before, {}, ops, frozenset(), {_NOTES: (2, 20)})
    assert gone == ((_NOTES,), False)


def test_rclone_s_listing_of_this_side_reads_as_a_snapshot(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    listing = work / "lib..fake-cloud_lib.path1.lst"
    listing.write_text(
        "# bisync listing v1 from 2026-11-06T08:55:16.556785892+0000\n"
        '-      553 - - 2026-11-06T08:42:50.217819629+0000 "TAXONOMY.md"\n'
        'd       -1 - - 2026-11-06T08:42:50.217819629+0000 "papers"\n'
        '-       36 md5:0f3a - 2026-11-06T09:55:16.2437+0100 "papers/p1/notes \\"q\\".md"\n'
        "-       14 - - 2026-11-06T08:42:51Z \"papers/p1/paper.pdf\"\n",
        encoding="utf-8",
    )

    assert core_sync._path1_listing(work) == {
        "TAXONOMY.md": (553, _ns(2026, 11, 6, 8, 42, 50, fraction_ns=217819629)),
        'papers/p1/notes "q".md': (36, _ns(2026, 11, 6, 8, 55, 16, fraction_ns=243700000)),
        "papers/p1/paper.pdf": (14, _ns(2026, 11, 6, 8, 42, 51)),
    }
    # Unreadable, or a second listing it cannot choose between: none.
    listing.write_text("- garbage\n", encoding="utf-8")
    assert core_sync._path1_listing(work) is None
    listing.write_text("", encoding="utf-8")
    (work / "other.path1.lst").write_text("", encoding="utf-8")
    assert core_sync._path1_listing(work) is None


def test_the_listing_s_time_is_the_file_s_own(tmp_path: Path) -> None:
    """As rclone writes it (RFC 3339, nanoseconds, the offset as +hhmm), the
    time equals what the library's own stat walk reads."""
    path = _write(tmp_path / "f.md", "x" * 7, _ns(2026, 3, 1, 23, 59, 58, fraction_ns=123456789))
    size, mtime_ns = _state(path)
    stamp = datetime.fromtimestamp(mtime_ns // 1_000_000_000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "s.path1.lst").write_text(
        f'-        {size} - - {stamp}.{mtime_ns % 1_000_000_000:09d}+0000 "f.md"\n',
        encoding="utf-8",
    )

    assert core_sync._path1_listing(work) == {"f.md": (size, mtime_ns)}
