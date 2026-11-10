"""A sandbox that refuses litman's data folder: one line in litman's words, never a traceback.

An agent's sandbox (DeepSeek Harness in workspace-write mode, say) lets it
write only in its workspace, and litman keeps its sync records in its data
folder (:func:`litman.core.sync.sync_data_dir`). The refusal is played by
``os.open`` and ``os.mkdir`` refusing every path in that folder, as such a
sandbox does, rather than by file modes, which Windows and root ignore.
"""

from __future__ import annotations

import errno
import io
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from rich.console import Console

from litman import cli as cli_mod
from litman.cli import _sync_stopped_line, cli
from litman.commands import sync as sync_cmd
from litman.core import sync as core_sync
from litman.core.config import load_config
from litman.core.sync import SYNC_STATE_FILENAME
from litman.exceptions import SyncAccessError
from litman.server.sync_scheduler import SyncScheduler
from tests.commands.test_sync_disconnect import _files, _records, _set_up, spied, tmp_path

__all__ = ["spied", "tmp_path"]  # fixtures, used by name

_DENIED = os.strerror(errno.EACCES)


def _inside(path: Any, root: Path) -> bool:
    try:
        Path(os.path.abspath(os.fsdecode(path))).relative_to(os.path.abspath(root))
    except (TypeError, ValueError):
        return False
    return True


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[str]]:
    """Call it to start refusing litman's data folder; it returns what was refused."""
    refused: list[str] = []

    def start() -> list[str]:
        root = core_sync.sync_data_dir()
        real_open, real_mkdir = os.open, os.mkdir

        def guarded_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if _inside(path, root):
                refused.append(os.fsdecode(path))
                raise PermissionError(errno.EACCES, _DENIED, os.fsdecode(path))
            return real_open(path, flags, *args, **kwargs)

        def guarded_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
            if _inside(path, root):
                refused.append(os.fsdecode(path))
                raise PermissionError(errno.EACCES, _DENIED, os.fsdecode(path))
            real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", guarded_open)
        monkeypatch.setattr(os, "mkdir", guarded_mkdir)
        return refused

    return start


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> tuple[int | str | None, list[str]]:
    """Run the real ``main()``, as ``lit`` runs it; its exit code and the lines it printed."""
    printed = io.StringIO()
    console = Console(file=printed, width=400, color_system=None)
    with monkeypatch.context() as run:
        run.setattr(sys, "argv", ["lit", *argv])
        run.setattr(cli_mod, "console", console)
        run.setattr(sync_cmd, "console", console)
        for name in ("stdout", "stderr"):
            run.setattr(sys, name, io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
        run.setattr(cli_mod, "_LAUNCHED_HEADLESS", cli_mod._LAUNCHED_HEADLESS)
        with pytest.raises(SystemExit) as exit_info:
            cli_mod.main()
    return exit_info.value.code, printed.getvalue().splitlines()


def _not_written(workdir: Path) -> str:
    return (
        f"litman could not write its sync records in {workdir} ({_DENIED}), so nothing "
        "was changed. Allow writing to that folder, then run it again."
    )


def _not_removed(what: object, target: str) -> str:
    return (
        f"litman could not remove {what} ({_DENIED}), so this computer is still set up "
        f"to sync with {target}. Once it can be removed, run `lit sync disconnect` again."
    )


def _everything(vault: Path) -> dict[str, bytes]:
    """The library's files and litman's data folder, by path."""
    data = core_sync.sync_data_dir()
    out = {f"lib/{k}": v for k, v in _files(vault).items()}
    if data.is_dir():
        out.update({f"data/{k}": v for k, v in _files(data).items()})
    return out


# ---------------------------------------------------------------------------
# The sync lock refused: every command that takes it
# ---------------------------------------------------------------------------


_COMMANDS = {
    "sync": ["sync"],
    "disconnect": ["sync", "disconnect"],
    "push": ["sync", "push", "--yes"],
    "pull": ["sync", "pull"],
}


@pytest.mark.parametrize("synced_before", [True, False], ids=["records", "no-folder"])
@pytest.mark.parametrize("verb", list(_COMMANDS))
def test_each_command_says_it_in_one_line_and_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spied: list[str],
    sandbox: Callable[[], list[str]],
    verb: str,
    synced_before: bool,
) -> None:
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault.resolve(), target)
    if synced_before:
        _records(workdir)
    monkeypatch.setattr(sync_cmd, "remote_exists", lambda _name: True)
    monkeypatch.setattr(sync_cmd, "require_one_way", lambda *_args: None)
    before = _everything(vault)
    refused = sandbox()

    code, lines = _main(monkeypatch, *_COMMANDS[verb], "--library", str(vault))

    assert code == 1
    assert lines[-1] == f"error: {_not_written(workdir)}"
    assert not any("Traceback" in line for line in lines)
    assert refused, "the sandbox was never asked"
    assert spied == []
    assert load_config(vault).sync is not None
    after = _everything(vault)
    state = f"lib/{SYNC_STATE_FILENAME}"
    if verb != "sync":
        assert after == before
        return
    # `lit sync` records its failed run, as every failed sync does; nothing else.
    assert {k: v for k, v in after.items() if k != state} == {
        k: v for k, v in before.items() if k != state
    }
    run = core_sync.read_sync_state(vault).last_run
    assert run is not None
    assert (run.trigger, run.result, run.reason) == ("terminal", "failed", "access")
    assert run.error == _not_written(workdir)


def test_the_line_after_other_commands_and_the_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spied: list[str],
    sandbox: Callable[[], list[str]],
) -> None:
    """No words for "access" of its own: the record's error says why and what to do."""
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault.resolve(), target)
    _records(workdir)
    sandbox()
    with pytest.raises(SyncAccessError):
        core_sync.two_way_sync(vault, target, lock_wait=0)
    run = core_sync.read_sync_state(vault).last_run
    assert run is not None
    at = datetime.fromisoformat(run.at).astimezone()

    assert _sync_stopped_line(run, now=at) == (
        f"Cloud sync failed at {at:%H:%M}. Run `lit sync status` for why and what to do."
    )
    monkeypatch.setattr(sync_cmd, "console", Console(width=400, color_system=None))
    result = CliRunner().invoke(cli, ["sync", "status", "--local", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith("Last run:"))
    assert lines[head].endswith(", `lit sync` in a terminal: failed.")
    assert lines[head + 1] == f"  {_not_written(workdir)}"
    assert not any(line.startswith("What to do:") for line in lines)


def test_the_save_lock_refused_mid_sync_says_it_stopped_part_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "records"
    real_open = os.open

    def refuse_pass_lock(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.fsdecode(path).endswith(core_sync.PASS_LOCK_NAME):
            raise PermissionError(errno.EACCES, _DENIED, os.fsdecode(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_pass_lock)

    with pytest.raises(SyncAccessError) as caught:
        with core_sync.hold_pass_lock(workdir):
            raise AssertionError("held")

    assert str(caught.value) == (
        f"litman could not write its sync records in {workdir} ({_DENIED}), so this sync "
        "stopped part-way. Allow writing to that folder, then run `lit sync` again."
    )
    assert isinstance(caught.value.__cause__, PermissionError)


# ---------------------------------------------------------------------------
# lit sync disconnect: a record it could not remove
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["record", "state-file", "config"])
def test_a_removal_refused_leaves_it_set_up_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """One message for any refusal, a Windows program holding the file included."""
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault, target)
    _records(workdir)
    record = workdir / "s.path2.lst"
    # As the command names them: it resolves the library's folder.
    state = vault.resolve() / SYNC_STATE_FILENAME
    config = vault.resolve() / "lit-config.yaml"
    assert state.is_file()
    real_unlink = os.unlink
    refused = {"record": record.name, "state-file": state.name}.get(which)

    def refuse(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(os.fsdecode(path)).name == refused:
            raise PermissionError(errno.EACCES, _DENIED, os.fsdecode(path))
        real_unlink(path, *args, **kwargs)

    def refuse_config(path: Path, _data: Any) -> None:
        tmp = path.with_suffix(".yaml.tmp")
        raise PermissionError(errno.EACCES, _DENIED, str(tmp))

    monkeypatch.setattr(os, "unlink", refuse)
    if which == "config":
        monkeypatch.setattr(core_sync, "_write_config", refuse_config)
    what = {
        "record": record,
        "state-file": state,
        "config": f"the `sync:` block from {config}",
    }[which]

    code, lines = _main(monkeypatch, "sync", "disconnect", "--library", str(vault))

    assert code == 1
    assert lines == [f"error: {_not_removed(what, target)}"]
    assert load_config(vault).sync is not None
    assert record.exists() == (which == "record")
    assert state.is_file() == (which != "config")


# ---------------------------------------------------------------------------
# The GUI's auto sync
# ---------------------------------------------------------------------------


def test_classified_as_needing_the_user() -> None:
    err = SyncAccessError("x")
    assert core_sync.classify_sync_error(err) == ("needs-user", "access")
    assert core_sync.run_from_error(err, "auto").reason == "access"


def _gui(vault: Path, monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> SyncScheduler:
    monkeypatch.setattr(core_sync, "remote_exists", lambda _name: True)
    monkeypatch.setattr(core_sync, "require_bisync_rclone", lambda: core_sync.BISYNC_MIN_VERSION)
    return SyncScheduler(SimpleNamespace(vault=vault.resolve()), clock=lambda: clock[0])


def test_refused_in_the_gui_it_stops_the_auto_sync_until_the_user_acts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spied: list[str],
    sandbox: Callable[[], list[str]],
) -> None:
    """It does not heal by itself: no retry every few minutes, the stop says so once."""
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault.resolve(), target)
    _records(workdir)
    clock = [1000.0]
    gui = _gui(vault, monkeypatch, clock)
    runs: list[str] = []

    def sync(*args: Any, **kwargs: Any) -> Any:
        runs.append("sync")
        return core_sync.two_way_sync(*args, **kwargs)

    gui.sync_fn = sync
    sandbox()

    gui.step()

    status = gui.status()
    assert runs == ["sync"]
    assert (status["state"], status["reason"]) == ("error", "access")
    assert status["message"] == _not_written(workdir)
    assert status["notice"] == {
        "kind": "stopped", "reason": "access", "message": _not_written(workdir)
    }
    assert status["nextTryAt"] is None
    run = core_sync.read_sync_state(vault).last_run
    assert run is not None and (run.trigger, run.reason) == ("auto", "access")
    clock[0] += 24 * 3600
    gui.step()
    assert runs == ["sync"]
    assert spied == []


def test_a_terminal_refused_in_a_sandbox_does_not_stop_the_gui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's `lit sync` was refused and recorded; the GUI beside it syncs on."""
    vault, target = _set_up(tmp_path)
    workdir = core_sync.sync_workdir(vault.resolve(), target)
    _records(workdir)
    core_sync.record_run(
        vault, core_sync.run_from_error(SyncAccessError(_not_written(workdir)), "terminal")
    )
    clock = [1000.0]
    gui = _gui(vault, monkeypatch, clock)
    runs: list[str] = []

    def synced(*_args: Any, **_kwargs: Any) -> core_sync.TwoWayResult:
        runs.append("sync")
        return core_sync.TwoWayResult(
            target_url=target,
            mode="sync",
            chosen=False,
            came_down=core_sync.FileChanges(),
            went_up=core_sync.FileChanges(),
            conflicts=(),
            remote_changed=False,
            local_backups=(),
            projects_without_folder=(),
        )

    gui.sync_fn = synced
    gui.step()

    assert runs == ["sync"]
    assert gui.status()["state"] == "ok"
    assert gui.status()["notice"] is None
