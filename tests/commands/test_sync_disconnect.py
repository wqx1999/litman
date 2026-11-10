"""``lit sync disconnect``: this computer stops syncing; library, backups, cloud and remote stay.

Real two-way and one-way syncs run against a ``type = local`` rclone remote, as
in ``test_sync_bisync.py``. What needs no rclone (the config edit, the order of
removal, the lock, a sync that waited for it) runs without one: a fake record
folder, and an ``rclone`` that is not installed or answers from a stub.
"""

from __future__ import annotations

import errno
import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner, Result
from rich.console import Console

from litman.cli import cli
from litman.commands import sync as sync_cmd
from litman.core import sync as core_sync
from litman.core.config import load_config
from litman.core.library import create_vault
from litman.core.shell import shell_arg
from litman.core.sync import (
    SYNC_STATE_FILENAME,
    SetupPayload,
    SyncState,
    remove_sync_from_config,
    write_sync_to_config,
)
from litman.core.sync_lock import FileLock, SharedHold, read_holder
from litman.core.vault_registry import (
    add_vault,
    load_registry,
    mark_health_checked,
    save_registry,
)
from litman.exceptions import (
    SyncAccessError,
    SyncBusyError,
    SyncError,
    SyncLockError,
    SyncNotSetUpError,
)
from litman.server.sync_scheduler import SaveGate, SyncScheduler
from tests.commands.test_sync_bisync import (
    _machine,
    _sync,
    _target,
    _write_paper,
    cloud,
    needs_rclone,
    tmp_path,
)

__all__ = ["cloud", "tmp_path"]  # fixtures, used by name


@pytest.fixture(autouse=True)
def _wide_consoles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let panels print unwrapped, so phrases stay whole."""
    monkeypatch.setattr(sync_cmd, "console", Console(width=400))


_HEAD = "Still there, for you to remove if you want:"
_REVOKE = (
    "  • rclone's access to the cloud account: revoke it in the account's security "
    "settings, once no computer uses rclone with it."
)
_NOTHING = "This library has no cloud sync set up, so there is nothing to disconnect."


def _disconnect(vault: Path) -> Result:
    return CliRunner().invoke(cli, ["sync", "disconnect", "--library", str(vault)])


def _panel(output: str) -> tuple[list[str], list[str]]:
    """The text inside the result panel, and the lines after it.

    Rich draws a legacy Windows console's panel with square or ASCII corners.
    """
    lines = output.splitlines()
    top = next(i for i, line in enumerate(lines) if line[:1] in ("╭", "┌", "+"))
    bottom = next(i for i, line in enumerate(lines) if i > top and line[:1] in ("╰", "└", "+"))
    inside = [line.strip("│|┃ ").rstrip() for line in lines[top + 1 : bottom]]
    return inside, lines[bottom + 1 :]


def _verdict(target: str) -> list[str]:
    return [f"This computer no longer syncs with {target}.", "The library here is unchanged."]


def _listed(
    target: str,
    *,
    backup: bool = True,
    remote: str = "fake-cloud",
    local_backup: Path | None = None,
) -> list[str]:
    """What disconnect lists when this computer's rclone has the remote."""
    lines = [_HEAD]
    if backup:
        lines.append(
            "  • The cloud folder, and its backups once a sync has made any. Leave them "
            "while another computer syncs with them:"
        )
    else:
        lines.append("  • The cloud folder. Leave it while another computer uses it:")
    lines.append(f"      rclone purge {shell_arg(target)}")
    if backup:
        lines.append(f"      rclone purge {shell_arg(core_sync.remote_backup_root(target))}")
    lines += [f"  • The rclone remote: rclone config delete {shell_arg(remote)}", _REVOKE]
    if local_backup is not None:
        lines.append(f"  • Backups on this computer: {local_backup}")
    return lines


def _files(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` by relative path, but ``views/`` (links to the papers)."""
    out: dict[str, bytes] = {}
    for top, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not (Path(top) == root and d == "views"))
        for name in sorted(names):
            path = Path(top) / name
            if not path.is_symlink():
                out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def _set_up(tmp_path: Path, path: str | None = None) -> tuple[Path, str]:
    """A library set up to sync, as ``lit sync setup`` leaves it; no rclone needed."""
    parent = tmp_path / "machine"
    parent.mkdir()
    vault = create_vault(parent)
    folder = str(tmp_path / "cloud" / "lib") if path is None else path
    write_sync_to_config(vault / "lit-config.yaml", SetupPayload(remote="fake-cloud", path=folder))
    core_sync.stamp_set_up(vault)
    return vault, f"fake-cloud:{folder}"


def _records(workdir: Path) -> None:
    """The records a two-way sync leaves in ``workdir``, as stand-ins."""
    workdir.mkdir(parents=True, exist_ok=True)
    for name in (
        "s.path2.lst",
        "s.path1.lst",
        "s.path1.lst-old",
        "filters.txt",
        core_sync.SNAPSHOT_NAME,
        core_sync.STAMP_RECORD_NAME,
    ):
        (workdir / name).write_text("x", encoding="utf-8")
    assert core_sync.bisync_initialised(workdir)


def _no_rclone(monkeypatch: pytest.MonkeyPatch) -> None:
    """rclone is not installed, and nothing may run it."""
    monkeypatch.setattr(core_sync, "RCLONE_BIN", "litman-test-no-such-rclone")

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("rclone was run")

    monkeypatch.setattr(core_sync, "run_rclone", refuse)
    monkeypatch.setattr(core_sync, "run_bisync", refuse)


def _rclone_lists(monkeypatch: pytest.MonkeyPatch, remotes: str) -> list[bool]:
    """An installed rclone whose ``listremotes`` prints ``remotes``; records whether it may ask."""
    monkeypatch.setattr(core_sync, "RCLONE_BIN", sys.executable)
    never_ask: list[bool] = []

    def run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args == ["listremotes"], args
        never_ask.append(core_sync._NEVER_ASK.get())
        return subprocess.CompletedProcess(args, 0, stdout=remotes, stderr="")

    monkeypatch.setattr(core_sync, "run_rclone", run)
    return never_ask


def _while_held(
    workdir: Path, start: Callable[[threading.Event], Any], change: Callable[[], object]
) -> dict[str, Any]:
    """Run ``start`` in a thread while this one holds the sync lock; ``change`` once it waits."""
    waiting = threading.Event()
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["result"] = start(waiting)
        except Exception as err:  # the test reads it back
            out["error"] = err

    with core_sync.hold_sync_lock(workdir, by="lit gui", wait=0):
        thread = threading.Thread(target=run)
        thread.start()
        assert waiting.wait(10)
        change()
    thread.join(20)
    assert not thread.is_alive()
    return out


# ---------------------------------------------------------------------------
# Real syncs, then disconnect
# ---------------------------------------------------------------------------


@needs_rclone
def test_after_a_two_way_sync_only_this_machines_records_go(tmp_path: Path, cloud: Path) -> None:
    vault = _machine(tmp_path, cloud, "a")
    _write_paper(vault, "p1")
    assert _sync(vault).exit_code == 0
    target = _target(cloud)
    paths = core_sync.bisync_paths(vault, target)
    assert core_sync.bisync_initialised(paths.workdir)
    assert paths.local_backup_root.is_dir()
    library = _files(vault)
    clouds = _files(cloud)
    rclone_conf = (tmp_path / "rclone.conf").read_bytes()

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    inside, after = _panel(result.output)
    assert inside == _verdict(target)
    assert after == _listed(target, local_backup=paths.local_backup_root)
    assert load_config(vault).sync is None
    assert not paths.workdir.exists()
    assert not (vault / SYNC_STATE_FILENAME).exists()
    # The library, the backups, both cloud folders and the remote are as they were.
    now = _files(vault)
    for name in ("lit-config.yaml", SYNC_STATE_FILENAME):
        library.pop(name)
        now.pop(name, None)
    assert now == library
    assert paths.local_backup_root.is_dir()
    assert _files(cloud) == clouds
    assert (tmp_path / "rclone.conf").read_bytes() == rclone_conf


@needs_rclone
def test_set_up_again_the_next_sync_is_a_first_sync(tmp_path: Path, cloud: Path) -> None:
    vault = _machine(tmp_path, cloud, "a")
    _write_paper(vault, "p1")
    assert _sync(vault).exit_code == 0
    assert _disconnect(vault).exit_code == 0
    write_sync_to_config(
        vault / "lit-config.yaml", SetupPayload(remote="fake-cloud", path=str(cloud / "lib"))
    )

    result = _sync(vault)

    assert result.exit_code == sync_cmd.EXIT_SYNC_NEEDS_CHOICE, result.output
    assert "This machine and the cloud both hold a library already." in result.output
    assert "records of its last sync are gone" not in result.output


@needs_rclone
def test_after_a_push_one_cloud_folder_and_no_backups(tmp_path: Path, cloud: Path) -> None:
    vault = _machine(tmp_path, cloud, "a")
    _write_paper(vault, "p1")
    pushed = CliRunner().invoke(cli, ["sync", "push", "--yes", "--library", str(vault)])
    assert pushed.exit_code == 0, pushed.output
    target = _target(cloud)
    workdir = core_sync.sync_workdir(vault, target)
    assert workdir.is_dir()

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    inside, after = _panel(result.output)
    assert inside == _verdict(target)
    assert after == _listed(target, backup=False)
    assert not workdir.exists()
    assert not (vault / SYNC_STATE_FILENAME).exists()
    assert (cloud / "lib" / "papers" / "p1" / "metadata.yaml").is_file()


@needs_rclone
def test_a_remote_gone_from_rclone_gets_no_rclone_commands(tmp_path: Path, cloud: Path) -> None:
    vault = _machine(tmp_path, cloud, "a")
    _write_paper(vault, "p1")
    assert _sync(vault).exit_code == 0
    (tmp_path / "rclone.conf").write_text("", encoding="utf-8")
    target = _target(cloud)

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    _, after = _panel(result.output)
    backups = core_sync.bisync_paths(vault, target).local_backup_root
    assert after == [
        _HEAD,
        f"  • The cloud folder {target}, and its backups "
        f"{core_sync.remote_backup_root(target)} once a sync has made any. rclone here has "
        "no remote fake-cloud, so remove them in the drive itself; leave them while "
        "another computer syncs with them.",
        _REVOKE,
        f"  • Backups on this computer: {backups}",
    ]
    assert load_config(vault).sync is None


@needs_rclone
def test_a_running_gui_stops_syncing_without_a_restart(tmp_path: Path, cloud: Path) -> None:
    vault = _machine(tmp_path, cloud, "a")
    _write_paper(vault, "p1")
    assert _sync(vault).exit_code == 0
    gui = SyncScheduler(SimpleNamespace(vault=vault.resolve()), clock=lambda: 1000.0)
    gui.step()
    assert gui.status()["state"] == "ok", gui.status()
    assert SaveGate().hold_for(vault.resolve()) is not None

    assert _disconnect(vault).exit_code == 0
    gui.step()

    assert gui.status()["state"] == "not-configured"
    assert gui.status_for(vault.resolve()) is None
    assert SaveGate().hold_for(vault.resolve()) is None
    assert not core_sync.sync_workdir(vault, _target(cloud)).exists()
    assert not (vault / SYNC_STATE_FILENAME).exists()


# ---------------------------------------------------------------------------
# Without rclone: what is said
# ---------------------------------------------------------------------------


def test_set_up_but_never_synced_and_no_rclone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_rclone(monkeypatch)
    vault, target = _set_up(tmp_path)
    assert (vault / SYNC_STATE_FILENAME).is_file()
    workdir = core_sync.sync_workdir(vault, target)
    assert not workdir.exists()

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    inside, after = _panel(result.output)
    assert inside == _verdict(target)
    assert after == [
        _HEAD,
        f"  • The cloud folder {target}, and its backups "
        f"{core_sync.remote_backup_root(target)} once a sync has made any. rclone is not "
        "installed here, so remove them in the drive itself; leave them while another "
        "computer syncs with them.",
        _REVOKE,
    ]
    assert load_config(vault).sync is None
    assert not (vault / SYNC_STATE_FILENAME).exists()
    # Taking the lock made the folder; it does not stay.
    assert not workdir.exists()


def test_rclone_is_asked_for_its_remotes_without_a_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    never_ask = _rclone_lists(monkeypatch, "other:\nfake-cloud:\n")
    vault, target = _set_up(tmp_path)

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    assert _panel(result.output)[1] == _listed(target)
    assert never_ask == [True]


def test_an_rclone_that_cannot_tell_gets_its_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_sync, "RCLONE_BIN", sys.executable)

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise SyncError("rclone exited 1 for `rclone listremotes`")

    monkeypatch.setattr(core_sync, "run_rclone", broken)
    vault, target = _set_up(tmp_path)

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    assert _panel(result.output)[1] == _listed(target)


def test_a_one_way_library_without_rclone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_rclone(monkeypatch)
    vault, target = _set_up(tmp_path)
    core_sync.stamp_push(vault)

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    assert _panel(result.output)[1] == [
        _HEAD,
        f"  • The cloud folder {target}. rclone is not installed here, so remove it in the "
        "drive itself; leave it while another computer uses it.",
        _REVOKE,
    ]


def test_the_remotes_top_level_gets_no_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rclone_lists(monkeypatch, "fake-cloud:\n")
    vault, target = _set_up(tmp_path, path="")
    core_sync.stamp_push(vault)
    assert target == "fake-cloud:"

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    assert _panel(result.output)[1] == [
        _HEAD,
        "  • The library's files at the top of fake-cloud:. They share it with the rest of "
        "the drive, so remove them in the drive itself.",
        "  • The rclone remote: rclone config delete fake-cloud",
        _REVOKE,
    ]
    assert "rclone purge" not in result.output


def test_a_narrow_terminal_keeps_every_command_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_cmd, "console", Console(width=40))
    _rclone_lists(monkeypatch, "fake-cloud:\n")
    vault, target = _set_up(tmp_path)

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    for command in _listed(target)[2:5]:
        assert command in lines, command


def test_no_sync_set_up_touches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_rclone(monkeypatch)
    vault = create_vault(tmp_path)
    core_sync.write_sync_state(vault, SyncState(last_push="2026-01-01T00:00:00+00:00"))
    before = _files(vault)

    result = _disconnect(vault)

    assert result.exit_code == 0, result.output
    assert result.output == _NOTHING + "\n"
    assert _files(vault) == before


def test_two_way_options_before_it_are_refused() -> None:
    result = CliRunner().invoke(cli, ["sync", "--force", "disconnect"])

    assert result.exit_code == 2
    assert "--force belongs to two-way `lit sync`, not to `lit sync disconnect`" in result.output


def test_other_commands_say_nothing_of_sync_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_rclone(monkeypatch)
    vault, _ = _set_up(tmp_path)
    eight_days = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    core_sync.write_sync_state(vault, SyncState(last_push=eight_days))
    core_sync.record_run(vault, core_sync.run_from_error(SyncError("boom"), "auto"))
    registry = add_vault(load_registry(), "main", vault)
    save_registry(mark_health_checked(registry, "main", datetime.now(UTC).isoformat()))
    before = CliRunner().invoke(cli, ["list"])
    assert before.exit_code == 0, before.output
    assert "Cloud sync failed" in before.output + before.stderr

    assert _disconnect(vault).exit_code == 0
    after = CliRunner().invoke(cli, ["list"])

    assert after.exit_code == 0, after.output
    assert "Cloud sync" not in after.output + after.stderr
    assert "lit sync" not in after.output + after.stderr


# ---------------------------------------------------------------------------
# The sync lock
# ---------------------------------------------------------------------------


def test_another_sync_running_leaves_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_rclone(monkeypatch)
    monkeypatch.setattr(sync_cmd, "SYNC_LOCK_WAIT", 0.2)
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)
    before = _files(vault)

    with core_sync.hold_sync_lock(workdir, by="lit gui", wait=0):
        holder = read_holder(workdir / core_sync.SYNC_LOCK_NAME)
        result = _disconnect(vault)

    assert result.exit_code == sync_cmd.EXIT_SYNC_BUSY, result.output
    assert holder is not None
    assert "lit sync disconnect — nothing was changed" in result.output
    assert (
        f"Another sync of this library is running ({holder.describe()}), so this computer "
        "is still connected. Try again once it is done."
    ) in result.output
    assert _files(vault) == before
    assert core_sync.bisync_initialised(workdir)


def test_it_waits_for_a_sync_to_end_then_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_rclone(monkeypatch)
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)
    held = threading.Event()

    def gui_sync() -> None:
        with core_sync.hold_sync_lock(workdir, by="lit gui", wait=0):
            held.set()
            time.sleep(0.6)

    thread = threading.Thread(target=gui_sync)
    thread.start()
    assert held.wait(10)
    result = _disconnect(vault)
    thread.join(10)

    assert result.exit_code == 0, result.output
    assert "Another sync of this library is running (lit gui, since " in result.output
    assert "waiting up to 90 seconds" in result.output
    assert load_config(vault).sync is None
    assert not workdir.exists()


def test_a_file_system_that_refuses_locks_does_not_stop_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)

    def refused(self: FileLock) -> bool:
        raise SyncLockError("The file system refuses file locks.")

    monkeypatch.setattr(FileLock, "try_acquire", refused)

    gone = core_sync.disconnect_sync(vault, lock_wait=0)

    assert gone is not None
    assert load_config(vault).sync is None
    assert not workdir.exists()


@pytest.mark.parametrize("meanwhile", ["disconnected", "set-up-elsewhere"])
def test_a_disconnect_that_waited_reads_the_config_again(
    tmp_path: Path, meanwhile: str
) -> None:
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)
    config = vault / "lit-config.yaml"

    def change() -> None:
        if meanwhile == "disconnected":
            assert remove_sync_from_config(config)
        else:
            write_sync_to_config(
                config, SetupPayload(remote="fake-cloud", path=str(tmp_path / "elsewhere"))
            )

    out = _while_held(
        workdir,
        lambda waiting: core_sync.disconnect_sync(
            vault, lock_wait=10, on_lock_wait=lambda _holder: waiting.set()
        ),
        change,
    )

    if meanwhile == "disconnected":
        assert out == {"result": None}
        assert not workdir.exists()
    else:
        err = out["error"]
        assert isinstance(err, SyncNotSetUpError)
        assert str(err) == (
            f"This library is no longer set up to sync with {target}, so nothing was "
            "changed. Run `lit sync disconnect` again."
        )
        assert core_sync.bisync_initialised(workdir)
        configured = load_config(vault).sync
        assert configured is not None
        assert configured.path == str(tmp_path / "elsewhere")


def test_a_held_save_lock_does_not_stop_it(tmp_path: Path) -> None:
    """A GUI save in progress holds ``pass.lock``: Windows cannot delete that file.

    The records go all the same; the folder itself may stay, holding only the
    lock file, so only what holds on every platform is asserted.
    """
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)
    save = SharedHold(core_sync.pass_lock_path(vault, target))
    assert save.try_enter()
    try:
        gone = core_sync.disconnect_sync(vault, lock_wait=0)
    finally:
        save.leave()

    assert gone is not None
    assert not core_sync.bisync_initialised(workdir)
    assert load_config(vault).sync is None
    assert not (vault / SYNC_STATE_FILENAME).exists()


# ---------------------------------------------------------------------------
# Order of removal
# ---------------------------------------------------------------------------


def test_cut_off_before_the_config_it_leaves_a_first_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)

    def disk_full(_path: Path) -> bool:
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as patched:
        patched.setattr(core_sync, "remove_sync_from_config", disk_full)
        with pytest.raises(SyncAccessError, match="No space left"):
            core_sync.disconnect_sync(vault, lock_wait=0)

    assert not core_sync.bisync_initialised(workdir)
    assert sorted(os.listdir(workdir)) == [core_sync.SYNC_LOCK_NAME]
    assert not (vault / SYNC_STATE_FILENAME).exists()
    assert load_config(vault).sync is not None

    assert core_sync.disconnect_sync(vault, lock_wait=0) is not None
    assert load_config(vault).sync is None
    assert not workdir.exists()


def test_rclones_path1_listings_go_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first record that cannot go, whichever it is, finds them gone already."""
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)
    real = core_sync.remove_file

    def held_by_a_scanner(path: Path) -> None:
        if ".path1.lst" not in Path(path).name:
            raise PermissionError(errno.EACCES, "held", str(path))
        real(path)

    monkeypatch.setattr(core_sync, "remove_file", held_by_a_scanner)

    with pytest.raises(SyncAccessError, match=r"\(held\), so this computer is still set up"):
        core_sync.disconnect_sync(vault, lock_wait=0)

    assert not core_sync.bisync_initialised(workdir)
    assert (workdir / "filters.txt").exists()
    assert (vault / SYNC_STATE_FILENAME).exists()
    assert load_config(vault).sync is not None


# ---------------------------------------------------------------------------
# A sync that waited for the lock while the library was disconnected
# ---------------------------------------------------------------------------


@pytest.fixture
def spied(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What rclone was asked to transfer: nothing, in these tests."""
    calls: list[str] = []

    def bisync(*_args: Any, **_kwargs: Any) -> None:
        calls.append("bisync")
        raise AssertionError("bisync ran")

    def mirror(*_args: Any, **_kwargs: Any) -> None:
        calls.append("mirror")
        raise AssertionError("rclone sync ran")

    monkeypatch.setattr(core_sync, "run_bisync", bisync)
    monkeypatch.setattr(core_sync, "_mirror", mirror)
    monkeypatch.setattr(core_sync, "require_bisync_rclone", lambda: core_sync.BISYNC_MIN_VERSION)
    monkeypatch.setattr(core_sync, "require_bisync_names_fit", lambda _vault, _target: None)
    return calls


@pytest.mark.parametrize(
    ("verb", "done"), [("sync", "synced"), ("push", "pushed"), ("pull", "pulled")]
)
def test_a_sync_that_waited_stops_once_the_library_is_disconnected(
    tmp_path: Path, spied: list[str], verb: str, done: str
) -> None:
    vault, target = _set_up(tmp_path)
    (vault / SYNC_STATE_FILENAME).unlink()
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)
    runs: dict[str, Callable[..., object]] = {
        "sync": core_sync.two_way_sync,
        "push": core_sync.push,
        "pull": core_sync.pull,
    }
    run = runs[verb]

    out = _while_held(
        workdir,
        lambda waiting: run(
            vault, target, lock_wait=10, on_lock_wait=lambda _holder: waiting.set()
        ),
        lambda: remove_sync_from_config(vault / "lit-config.yaml"),
    )

    err = out["error"]
    assert isinstance(err, SyncNotSetUpError)
    assert str(err) == (
        f"This library is no longer set up to sync with {target}, so nothing was {done}."
    )
    assert spied == []
    # Not recorded: no state file comes back, and no "Cloud sync stopped" line.
    assert not (vault / SYNC_STATE_FILENAME).exists()


def test_a_sync_set_up_elsewhere_meanwhile_stops_too(tmp_path: Path, spied: list[str]) -> None:
    vault, target = _set_up(tmp_path)
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(remote="fake-cloud", path=str(tmp_path / "elsewhere")),
    )

    with pytest.raises(SyncNotSetUpError, match="no longer set up to sync with"):
        core_sync.two_way_sync(vault, target, lock_wait=0)

    assert spied == []


def test_the_gui_looks_at_the_config_again_instead_of_backing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, target = _set_up(tmp_path)
    _records(core_sync.sync_workdir(vault.resolve(), target))
    monkeypatch.setattr(core_sync, "require_bisync_rclone", lambda: core_sync.BISYNC_MIN_VERSION)
    monkeypatch.setattr(core_sync, "remote_exists", lambda _name: True)
    calls: list[str] = []

    def disconnected_meanwhile(*_args: Any, **_kwargs: Any) -> None:
        calls.append("sync")
        raise SyncNotSetUpError(f"This library is no longer set up to sync with {target}.")

    gui = SyncScheduler(SimpleNamespace(vault=vault.resolve()), clock=lambda: 1000.0)
    gui.sync_fn = disconnected_meanwhile

    gui.step()

    assert calls == ["sync"]
    assert gui.status()["state"] != "offline"
    assert gui.status()["nextTryAt"] is None
    remove_sync_from_config(vault / "lit-config.yaml")
    gui.step()
    assert gui.status()["state"] == "not-configured"
    assert calls == ["sync"]


def test_the_gui_writes_no_owed_stamp_for_a_disconnected_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, target = _set_up(tmp_path)
    _records(core_sync.sync_workdir(vault.resolve(), target))
    monkeypatch.setattr(core_sync, "require_bisync_rclone", lambda: core_sync.BISYNC_MIN_VERSION)
    monkeypatch.setattr(core_sync, "remote_exists", lambda _name: True)
    written: list[str] = []
    monkeypatch.setattr(
        core_sync, "write_owed_stamp", lambda _workdir, url: written.append(url) or True
    )
    def busy(*_args: Any, **_kwargs: Any) -> None:
        raise SyncBusyError("Another sync of this library is running.")

    gui = SyncScheduler(SimpleNamespace(vault=vault.resolve()), clock=lambda: 1000.0)
    gui.sync_fn = busy
    gui.step()
    library = gui._library
    assert library is not None and library.target_url == target

    # Read the config, then disconnected before the stamp went out.
    remove_sync_from_config(vault / "lit-config.yaml")

    assert gui._write_owed_stamp(library) is True
    assert written == []


# ---------------------------------------------------------------------------
# remove_sync_from_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            "# top\nlibrary_name: demo\nsync:\n  remote: gdrive\n  path: litman\n"
            "  exclude_repos: false\n# about projects\n\nprojects: {}   # inline\n",
            "# top\nlibrary_name: demo\n# about projects\n\nprojects: {}   # inline\n",
            id="comment-after-the-block-stays",
        ),
        pytest.param(
            "library_name: demo   # name\nsync:\n  remote: gdrive\n"
            "  exclude_repos: false   # eol\n# about projects\nprojects:\n  a: /x\n",
            "library_name: demo   # name\n# about projects\nprojects:\n  a: /x\n",
            id="its-own-comments-go",
        ),
        pytest.param(
            "library_name: demo\nsync: {remote: gdrive, path: litman}\n# about projects\n"
            "projects: {}\n",
            "library_name: demo\n# about projects\nprojects: {}\n",
            id="flow-style",
        ),
        pytest.param(
            "library_name: demo\nprojects:\n  a: /x\nsync:\n  remote: gdrive\n# end note\n",
            "library_name: demo\nprojects:\n  a: /x\n# end note\n",
            id="last-after-a-block",
        ),
        pytest.param(
            "library_name: demo\nprojects: {a: /x}\nsync:\n  remote: gdrive\n# end note\n",
            "library_name: demo\nprojects: {a: /x}\n# end note\n",
            id="last-after-a-flow-mapping",
        ),
        pytest.param(
            "library_name: demo\ncodes_ignore_patterns:\n  - repo/\nsync:\n  remote: gdrive\n"
            "\n# about projects\nprojects: {}\n",
            "library_name: demo\ncodes_ignore_patterns:\n  - repo/\n\n# about projects\n"
            "projects: {}\n",
            id="after-a-list",
        ),
        pytest.param(
            "sync:\n  remote: gdrive\n# lib name next\nlibrary_name: demo\n",
            "# lib name next\nlibrary_name: demo\n",
            id="first-key",
        ),
        pytest.param(
            "library_name: demo\nsync:\n  remote: gdrive\n  path: x\nprojects: {}\n",
            "library_name: demo\nprojects: {}\n",
            id="no-comments",
        ),
    ],
)
def test_the_sync_block_goes_and_the_rest_stays(tmp_path: Path, before: str, after: str) -> None:
    config = tmp_path / "lit-config.yaml"
    config.write_text(before, encoding="utf-8")

    assert remove_sync_from_config(config) is True

    assert config.read_text(encoding="utf-8") == after


def test_setup_then_removal_gives_back_a_new_librarys_config(tmp_path: Path) -> None:
    """``lit init``'s file, as a round trip writes it, its commented ``sync:`` example included."""
    vault = create_vault(tmp_path)
    config = vault / "lit-config.yaml"
    round_trip = io.StringIO()
    core_sync._yaml.dump(core_sync._yaml.load(config.read_text(encoding="utf-8")), round_trip)
    write_sync_to_config(config, SetupPayload(remote="gdrive", path="litman"))

    assert remove_sync_from_config(config) is True

    text = config.read_text(encoding="utf-8")
    assert text == round_trip.getvalue()
    assert "# Cloud sync target (M6)." in text
    assert "\nsync:" not in text


def test_a_config_without_sync_is_left_alone(tmp_path: Path) -> None:
    config = tmp_path / "lit-config.yaml"
    config.write_text("library_name: demo   # mine\n", encoding="utf-8")

    assert remove_sync_from_config(config) is False

    assert config.read_text(encoding="utf-8") == "library_name: demo   # mine\n"
