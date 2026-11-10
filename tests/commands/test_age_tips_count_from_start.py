"""The age tips count a never-run from when it could first have run here.

"No `lit health-check` in 14+ days" and "no `lit sync` in 7+ days" fired on a
library added that day, and right after `lit sync setup`: a missing timestamp
counts as stale, and two ways in left none. An agent passed the false "14 days"
on to the user. Now adding an existing library in the GUI starts the health
clock as `lit init` and `lit vault add` do, and `lit sync setup` records when it
ran (``set-up`` in this machine's sync state), which the sync tip counts from
until this machine first syncs. With no stamp at all (a registry entry or a
setup from an older litman) the tips fire as before.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.sync import (
    SyncState,
    read_sync_state,
    stamp_push,
    stamp_set_up,
    write_sync_state,
)
from litman.core.vault_registry import (
    add_vault,
    find_by_name,
    load_registry,
    mark_health_checked,
    save_registry,
)

_HEALTH_TIP = "no `lit health-check` in 14+ days"
_SYNC_TIP = "no `lit sync` in 7+ days"
# `lit sync setup --remote` asks rclone whether the remote exists.
_needs_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not on PATH")


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _tips(*args: str) -> str:
    result = CliRunner().invoke(cli, ["list", *args])
    assert result.exit_code == 0, result.output
    return " ".join(result.stderr.split())


def _register(vault: Path, *, checked: str | None) -> None:
    reg = add_vault(load_registry(), "main", vault)
    if checked is not None:
        reg = mark_health_checked(reg, "main", checked)
    save_registry(reg)


# ------------------------------------------------------------- health-check tip


def test_a_library_added_in_the_gui_gets_no_health_tip(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    served = create_vault(tmp_path, name="served")
    added = create_vault(tmp_path, name="added")

    resp = TestClient(create_app(served)).post(
        "/api/vaults", json={"name": "added", "path": str(added)}
    )

    assert resp.status_code == 200, resp.text
    entry = find_by_name(load_registry(), "added")
    # The first entry of an empty registry is the active one.
    assert entry is not None and entry.is_active
    assert entry.last_health_check_at is not None
    assert _HEALTH_TIP not in _tips()


def test_an_entry_from_before_the_stamp_still_gets_the_health_tip(tmp_path: Path) -> None:
    """No stamp at all: registered by a litman that did not start the clock."""
    _register(create_vault(tmp_path), checked=None)

    assert _HEALTH_TIP in _tips()


def test_a_check_that_is_really_old_still_gets_the_health_tip(tmp_path: Path) -> None:
    _register(create_vault(tmp_path), checked=_days_ago(15))

    assert _HEALTH_TIP in _tips()


# --------------------------------------------------------------------- sync tip


@pytest.fixture
def fake_cloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An rclone config with one ``local`` remote, as ``tests/commands/test_sync.py``."""
    conf = tmp_path / "rclone.conf"
    conf.write_text("[fake-cloud]\ntype = local\nnounc = false\n", encoding="utf-8")
    storage = tmp_path / "cloud-storage"
    storage.mkdir()
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    return storage


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """The active library, health-check fresh so only the sync tip is in play."""
    vault = create_vault(tmp_path)
    _register(vault, checked=_days_ago(0))
    return vault


def _set_up(vault: Path, storage: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["sync", "setup", "--remote", "fake-cloud", "--path", str(storage),
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output


def _with_remote(vault: Path) -> None:
    """A ``sync:`` block in lit-config.yaml, as a setup leaves it."""
    cfg = vault / "lit-config.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8") + "\nsync:\n  remote: testremote\n  path: ''\n",
        encoding="utf-8",
    )


@_needs_rclone
def test_sync_setup_records_when_it_ran(library: Path, fake_cloud: Path) -> None:
    before = datetime.now(UTC) - timedelta(seconds=5)

    _set_up(library, fake_cloud)

    set_up = read_sync_state(library).set_up
    assert set_up is not None
    assert datetime.fromisoformat(set_up) >= before


@_needs_rclone
def test_right_after_sync_setup_there_is_no_sync_tip(library: Path, fake_cloud: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["sync", "setup", "--remote", "fake-cloud", "--path", str(fake_cloud),
         "--library", str(library)],
    )

    assert result.exit_code == 0, result.output
    assert "no `lit sync` in" not in " ".join(result.output.split())
    assert _SYNC_TIP not in _tips()


def test_a_setup_this_week_that_never_synced_gets_no_sync_tip(library: Path) -> None:
    _with_remote(library)
    write_sync_state(library, SyncState(set_up=_days_ago(2)))

    assert _SYNC_TIP not in _tips()


def test_a_setup_that_never_synced_for_a_week_gets_the_sync_tip(library: Path) -> None:
    _with_remote(library)
    write_sync_state(library, SyncState(set_up=_days_ago(8)))

    assert _SYNC_TIP in _tips()


def test_a_setup_from_an_older_litman_still_gets_the_sync_tip(library: Path) -> None:
    """A sync state with no ``set-up``, or none at all: as before, the tip fires."""
    _with_remote(library)
    assert _SYNC_TIP in _tips()

    write_sync_state(library, SyncState())
    assert _SYNC_TIP in _tips()


def test_a_fresh_setup_does_not_hide_an_old_sync(library: Path) -> None:
    """Once this machine has synced, that time counts, not the setup."""
    _with_remote(library)
    write_sync_state(library, SyncState(last_sync=_days_ago(8), set_up=_days_ago(0)))

    assert _SYNC_TIP in _tips()


def test_set_up_survives_the_other_stamps_and_older_writes_are_read(
    tmp_path: Path,
) -> None:
    vault = create_vault(tmp_path)
    stamp_set_up(vault)
    set_up = read_sync_state(vault).set_up

    stamp_push(vault)

    state = read_sync_state(vault)
    assert (state.set_up, state.last_push is not None) == (set_up, True)
    # A state file an older litman wrote: no set-up key, read as None.
    (vault / ".litman-sync-state.yaml").write_text(
        "last-push:\nlast-pull:\nlast-sync:\nlast-sync-result:\n", encoding="utf-8"
    )
    assert read_sync_state(vault).set_up is None
