"""``lit sync push`` and ``pull`` refuse a library that syncs two-way.

Two-way sync never uploads ``lit-config.yaml``, ``INDEX.json`` or the code
checkouts, so a pull, which mirrors the cloud with deletion, would delete them
here, and a push would overwrite what the other machines synced. A library
syncs two-way when this machine has two-way records for it, or when the cloud
folder holds the auto-sync stamp. A pull is also refused when the cloud folder
holds no ``lit-config.yaml``. Real rclone against a ``type = local`` remote,
as in ``test_sync_bisync.py``.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from litman.core import locking
from litman.core import sync as core_sync
from litman.core.sync import SYNC_STAMP_FILENAME
from litman.exceptions import SyncError
from tests.commands.test_sync_bisync import (
    _edit_notes,
    _machine,
    _sync,
    _target,
    _wide_consoles,
    _write_paper,
    cloud,
    needs_rclone,
    tmp_path,
)

__all__ = ["_wide_consoles", "cloud", "tmp_path"]  # fixtures, used by name

pytestmark = needs_rclone


def _files(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root`` (links aside), by relative path."""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _refusal(result: object) -> str:
    """The refusal's text; the CLI's main prints it as ``error: …`` and exits 1."""
    exc = getattr(result, "exception", None)
    assert isinstance(exc, SyncError), getattr(result, "output", "")
    assert getattr(result, "exit_code", None) == 1
    return _one_line(str(exc))


def _two_way_library(tmp_path: Path, cloud: Path) -> Path:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    return a


_ONE_WAY_COMMANDS = [["push", "--yes"], ["push", "--dry-run"], ["pull"], ["pull", "--dry-run"]]
_IDS = ["push", "push-dry-run", "pull", "pull-dry-run"]


@pytest.mark.parametrize("args", _ONE_WAY_COMMANDS, ids=_IDS)
def test_a_library_this_machine_syncs_two_way_is_refused(
    tmp_path: Path, cloud: Path, args: list[str]
) -> None:
    a = _two_way_library(tmp_path, cloud)
    # Only this machine's records say two-way: no stamp in the cloud, and a
    # lit-config.yaml there for a pull to find.
    (cloud / "lib" / SYNC_STAMP_FILENAME).unlink()
    shutil.copy2(a / "lit-config.yaml", cloud / "lib" / "lit-config.yaml")
    _edit_notes(a, "p1", "# Paper p1\nnot synced\n", time.time() - 30)
    before = (_files(a), _files(cloud))

    said = _refusal(_sync(a, *args))

    assert said.startswith("This library syncs two-way, so a")
    assert said.endswith("Run `lit sync` instead.")
    assert len(said) <= 130
    assert (_files(a), _files(cloud)) == before
    # The reverse: without this machine's records the same command goes through.
    locking.rmtree(core_sync.sync_workdir(a.resolve(), _target(cloud)))
    assert _sync(a, args[0], "--dry-run").exit_code == 0


@pytest.mark.parametrize("args", _ONE_WAY_COMMANDS, ids=_IDS)
def test_a_cloud_folder_another_machine_syncs_two_way_is_refused(
    tmp_path: Path, cloud: Path, args: list[str]
) -> None:
    _two_way_library(tmp_path, cloud)
    b = _machine(tmp_path, cloud, "b")
    _write_paper(b, "q1")
    # Only the stamp says two-way: this machine never synced two-way, and the
    # cloud holds a lit-config.yaml for a pull to find.
    assert not core_sync.bisync_initialised(core_sync.sync_workdir(b.resolve(), _target(cloud)))
    shutil.copy2(b / "lit-config.yaml", cloud / "lib" / "lit-config.yaml")
    before = (_files(b), _files(cloud))

    said = _refusal(_sync(b, *args))

    way_out = "`lit sync --first-from cloud`" if args[0] == "pull" else "`lit sync`"
    assert said.startswith(f"{_target(cloud)} is synced two-way, so a {args[0]}")
    assert said.endswith(f"Run {way_out} instead.")
    assert (_files(b), _files(cloud)) == before
    # The reverse: without the stamp the same command goes through.
    (cloud / "lib" / SYNC_STAMP_FILENAME).unlink()
    assert _sync(b, args[0], "--dry-run").exit_code == 0


@pytest.mark.parametrize("dry_run", [False, True], ids=["pull", "pull-dry-run"])
@pytest.mark.parametrize("folder", ["missing", "without-config"])
def test_a_pull_from_a_cloud_folder_without_lit_config_is_refused(
    tmp_path: Path, cloud: Path, folder: str, dry_run: bool
) -> None:
    b = _machine(tmp_path, cloud, "b")
    _write_paper(b, "q1")
    if folder == "without-config":
        (cloud / "lib" / "papers" / "x").mkdir(parents=True)
        (cloud / "lib" / "papers" / "x" / "notes.md").write_text("# x\n", encoding="utf-8")
    before = (_files(b), _files(cloud))

    said = _refusal(_sync(b, "pull", *(["--dry-run"] if dry_run else [])))

    assert said.startswith(f"{_target(cloud)} holds no lit-config.yaml, so a pull would delete")
    assert said.endswith("Point `lit sync setup` at the folder you pushed to.")
    assert (_files(b), _files(cloud)) == before
    assert (b / "lit-config.yaml").is_file()
    # The reverse: once a push put a library there, the pull goes through.
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a, "push", "--yes").exit_code == 0
    pulled = _sync(b, "pull", *(["--dry-run"] if dry_run else []))
    assert pulled.exit_code == 0, pulled.output


def test_one_way_sync_leaves_no_stamp_so_push_and_pull_keep_working(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a, "push", "--yes").exit_code == 0
    _write_paper(a, "p2")
    second = _sync(a, "push")
    assert second.exit_code == 0, second.output
    b = _machine(tmp_path, cloud, "b")

    pulled = _sync(b, "pull")

    assert pulled.exit_code == 0, pulled.output
    assert (b / "papers" / "p2" / "metadata.yaml").is_file()
    assert not (cloud / "lib" / SYNC_STAMP_FILENAME).exists()
    for vault in (a, b):
        work = core_sync.sync_workdir(vault.resolve(), _target(cloud))
        assert not (work / core_sync.STAMP_RECORD_NAME).exists()
        assert not core_sync.bisync_initialised(work)


def test_status_reads_in_sync_after_a_two_way_sync(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _two_way_library(tmp_path, cloud)

    status = _sync(a, "status")

    assert status.exit_code == 0, status.output
    said = _one_line(status.output)
    assert "in sync" in said
    assert "Last sync" in said
    assert "Last push" not in said and "Last pull" not in said
    # The reverse: counted the one-way way, INDEX.json and lit-config.yaml,
    # which two-way sync never uploads, leave this side two files ahead.
    monkeypatch.setattr(core_sync.SyncMode, "two_way", property(lambda self: False))
    status = _sync(a, "status")
    said = _one_line(status.output)
    assert "in sync" not in said
    assert "+2 files" in said


def test_status_reads_in_sync_when_an_earlier_push_left_files_in_the_cloud(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library pushed one-way before it synced two-way: the cloud keeps the
    pushed INDEX.json and lit-config.yaml, which two-way sync leaves alone."""
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a, "push", "--yes").exit_code == 0
    assert _sync(a, "--first-from", "local").exit_code == 0
    assert (cloud / "lib" / "lit-config.yaml").is_file()

    status = _sync(a, "status")

    assert status.exit_code == 0, status.output
    assert "in sync" in _one_line(status.output)
    # The reverse: with only the stamp left out of the cloud's count, its two
    # leftover files read as missing here.
    monkeypatch.setattr(
        core_sync, "_two_way_filter_args", lambda: list(core_sync._STAMP_EXCLUDE_ARGS)
    )
    said = _one_line(_sync(a, "status").output)
    assert "in sync" not in said
    assert "-2 files" in said


def test_status_of_a_library_that_never_synced_two_way_is_unchanged(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a, "push", "--yes").exit_code == 0

    said = _one_line(_sync(a, "status").output)

    assert "in sync" in said
    assert "Last push" in said and "Last pull" in said
    assert "Last sync" not in said


def test_the_cloud_check_is_one_listing(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _two_way_library(tmp_path, cloud)
    calls: list[list[str]] = []
    real = core_sync.run_rclone

    def counting(args: list[str], **kwargs: object) -> object:
        calls.append(args)
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(core_sync, "run_rclone", counting)
    with pytest.raises(SyncError):
        core_sync.require_one_way(a, _target(cloud), "pull")

    assert [c[0] for c in calls] == ["lsf"]
