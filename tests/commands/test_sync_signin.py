"""A cloud that refuses rclone's sign-in stops the sync, and the way back in fits the remote.

An OAuth remote (OneDrive, Google Drive, Dropbox, ...) signs in again through
the browser, `rclone config reconnect`; a password remote (WebDAV and the
like) only by typing the password into `rclone config`, since it refuses
`reconnect`. The end-to-end tests run a real `rclone serve webdav` with basic
auth on this machine, sync a library through it, then make the remote's
password wrong, as when the user revokes an app password. A cloud that cannot
be reached at all stays a network error: retried, never a stop.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core import sync as core_sync
from litman.core.dates import now_iso
from litman.core.library import create_vault
from litman.core.sync import (
    RCLONE_BIN,
    SetupPayload,
    auth_failure,
    read_sync_state,
    remote_sign_in,
    sign_in_way_out,
    write_sync_to_config,
)
from litman.core.vault_registry import add_vault, load_registry, mark_health_checked, save_registry
from litman.exceptions import SyncAuthError
from litman.server import sync_scheduler as sched_mod
from litman.server.sync_scheduler import SyncScheduler
from tests.commands.test_sync_bisync import _sync, _write_paper, needs_rclone, tmp_path

__all__ = ["tmp_path"]  # fixture, used by name

_PASSWORD_WAY_BACK = (
    "Enter the password again with `rclone config` (edit remote wd), then run `lit sync`."
)


def _one_line(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The way back in, by the remote's type
# ---------------------------------------------------------------------------


@needs_rclone
def test_the_way_back_in_follows_the_remotes_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[od]\ntype = onedrive\n\n[gd]\ntype = drive\n\n"
        "[wd]\ntype = webdav\nurl = http://127.0.0.1:9/\n\n[loc]\ntype = local\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))

    assert remote_sign_in("od") == "browser"
    assert remote_sign_in("gd") == "browser"
    # The reverse: neither a WebDAV nor a local remote has a browser sign-in.
    assert remote_sign_in("wd") == "password"
    assert remote_sign_in("loc") == "password"
    assert remote_sign_in("gone") is None


@pytest.mark.parametrize(
    ("sign_in", "verdict", "way_back"),
    [
        (
            "browser",
            "The sign-in to wd has expired, so nothing was synced.",
            "Sign in again with `rclone config reconnect wd:`, then run `lit sync`.",
        ),
        ("password", "wd refused the password, so nothing was synced.", _PASSWORD_WAY_BACK),
        (
            None,
            "wd refused the sign-in, so nothing was synced.",
            "Sign in again with `rclone config` (edit remote wd), then run `lit sync`.",
        ),
    ],
)
def test_the_message_says_what_happened_and_the_way_back_for_the_remote(
    monkeypatch: pytest.MonkeyPatch, sign_in: str | None, verdict: str, way_back: str
) -> None:
    monkeypatch.setattr(core_sync, "remote_sign_in", lambda remote: sign_in)

    err = auth_failure("wd:lib", "CRITICAL: read metadata failed: 401 Unauthorized")

    assert str(err) == f"{verdict} {way_back}\nCRITICAL: read metadata failed: 401 Unauthorized"
    assert err.sign_in == sign_in
    assert sign_in_way_out("wd", sign_in) == way_back


def test_rclones_own_oauth_words_settle_the_way_back_without_asking_rclone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ask(remote: str) -> str:
        raise AssertionError("rclone was asked")

    monkeypatch.setattr(core_sync, "remote_sign_in", ask)

    err = auth_failure("gdrive:lib", "ERROR : couldn't fetch token: invalid_grant")

    assert err.sign_in == "browser"
    assert "`rclone config reconnect gdrive:`" in str(err)


def test_a_remote_name_with_a_space_is_quoted_in_the_command() -> None:
    assert "`rclone config reconnect \"my cloud:\"`" in sign_in_way_out("my cloud", "browser")


# ---------------------------------------------------------------------------
# A real WebDAV server that turns the password down
# ---------------------------------------------------------------------------


@dataclass
class _Dav:
    """A local `rclone serve webdav` (user ``u``, password ``right``) and the remote ``wd``."""

    port: int
    server: subprocess.Popen[bytes]

    def set_password(self, password: str) -> None:
        subprocess.run(
            [RCLONE_BIN, "config", "update", "wd", "pass", password],
            check=True,
            capture_output=True,
            timeout=60,
        )

    def stop(self) -> None:
        if self.server.poll() is None:
            self.server.terminate()
            self.server.wait(timeout=30)


# rclone's own line once it listens: `... WebDav Server started on [http://127.0.0.1:PORT/]`.
_SERVING = re.compile(r"started on \[http://127\.0\.0\.1:(\d+)/\]")


@pytest.fixture
def dav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Dav]:
    conf = tmp_path / "rclone.conf"
    conf.write_text("", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    root = tmp_path / "dav"
    root.mkdir()
    # Port 0, and the port read back from rclone's log: a port probed free
    # first and handed to rclone later can be taken in between, by another
    # process or another pytest-xdist worker, and the test would then talk
    # to whatever holds it.
    log = tmp_path / "dav.log"
    with open(log, "wb") as out:
        server = subprocess.Popen(
            [
                RCLONE_BIN, "serve", "webdav", str(root),
                "--addr", "127.0.0.1:0", "--user", "u", "--pass", "right",
            ],
            stdout=subprocess.DEVNULL,
            stderr=out,
        )
    served = _Dav(0, server)
    try:
        deadline = time.monotonic() + 30
        while True:
            match = _SERVING.search(log.read_text(encoding="utf-8", errors="replace"))
            if match is not None:
                served.port = int(match.group(1))
                break
            assert server.poll() is None, log.read_text(encoding="utf-8", errors="replace")
            assert time.monotonic() < deadline, (
                "rclone serve webdav printed no 'started on' line in 30 s:\n"
                + log.read_text(encoding="utf-8", errors="replace")
            )
            time.sleep(0.1)
        subprocess.run(
            [
                RCLONE_BIN, "config", "create", "wd", "webdav",
                "url", f"http://127.0.0.1:{served.port}/", "vendor", "rclone",
                "user", "u", "pass", "right",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        yield served
    finally:
        served.stop()


def _synced_library(tmp_path: Path) -> Path:
    """A library with one paper, the active one, synced up through ``wd:lib``."""
    parent = tmp_path / "a"
    parent.mkdir()
    vault = create_vault(parent)
    write_sync_to_config(vault / "lit-config.yaml", SetupPayload(remote="wd", path="lib"))
    _write_paper(vault, "p1")
    registry = add_vault(load_registry(), "main", vault)
    save_registry(mark_health_checked(registry, "main", now_iso()))
    first = _sync(vault)
    assert first.exit_code == 0, first.output
    return vault


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _gui(vault: Path, clock: _Clock) -> SyncScheduler:
    return SyncScheduler(SimpleNamespace(vault=vault.resolve()), clock=clock)


@needs_rclone
def test_a_refused_webdav_password_stops_the_sync_and_says_how_to_get_back_in(
    tmp_path: Path, dav: _Dav
) -> None:
    vault = _synced_library(tmp_path)
    dav.set_password("wrong")

    result = _sync(vault)

    assert result.exit_code == 1
    err = result.exception
    assert isinstance(err, SyncAuthError), result.output
    # litman's words on the terminal; rclone's lines are in the record.
    assert str(err) == f"wd refused the password, so nothing was synced. {_PASSWORD_WAY_BACK}"
    run = read_sync_state(vault).last_run
    assert run is not None
    assert "401 Unauthorized" in (run.error or "")
    assert (run.trigger, run.result, run.reason, run.sign_in) == (
        "terminal", "failed", "auth", "password",
    )
    local = _one_line(_sync(vault, "status", "--local").output)
    assert "`lit sync` in a terminal: failed (the cloud refused the sign-in)." in local
    # The way back once, in the error's own words; no "What to do:" repeats it.
    assert local.count(_PASSWORD_WAY_BACK) == 1
    assert "What to do:" not in local
    at = datetime.fromisoformat(run.at).astimezone()
    listed = CliRunner().invoke(cli, ["list"])
    assert (
        f"Cloud sync stopped at {at:%H:%M} (sign-in refused). "
        "Run `lit sync status` for why and what to do." in listed.stderr.splitlines()
    )

    # The reverse: with the right password the same library syncs again, and
    # the line after commands goes away.
    dav.set_password("right")
    again = _sync(vault)
    assert again.exit_code == 0, again.output
    assert "Cloud sync" not in CliRunner().invoke(cli, ["list"]).output


@needs_rclone
def test_the_gui_stops_on_a_refused_password_and_retries_a_cloud_it_cannot_reach(
    tmp_path: Path, dav: _Dav
) -> None:
    vault = _synced_library(tmp_path)
    dav.set_password("wrong")
    clock = _Clock()
    gui = _gui(vault, clock)

    gui.step()

    status = gui.status()
    assert (status["state"], status["reason"]) == ("error", "auth"), status
    assert status["notice"]["kind"] == "stopped"
    run = read_sync_state(vault).last_run
    assert run is not None
    assert (run.trigger, run.reason, run.sign_in) == ("auto", "auth", "password")
    # Stopped means stopped: an hour later it has not tried again.
    clock.now += 3600
    gui.step()
    assert gui.status()["state"] == "error"

    # The reverse: a cloud that cannot be reached at all is a network error,
    # retried later, never a stop.
    dav.stop()
    other = _gui(vault, clock)
    other.step()
    status = other.status()
    assert (status["state"], status["reason"]) == ("offline", "offline"), status
    assert status["nextTryAt"] is not None


@needs_rclone
def test_a_password_refused_at_the_stamp_check_stops_the_gui_and_says_why(
    tmp_path: Path, dav: _Dav
) -> None:
    vault = _synced_library(tmp_path)
    clock = _Clock()
    gui = _gui(vault, clock)
    gui.step()
    assert gui.status()["state"] == "ok", gui.status()
    dav.set_password("wrong")

    # Nothing changed here: the next look only reads the cloud's stamp.
    clock.now += sched_mod.STAMP_EVERY_S * (1 + sched_mod.STAMP_JITTER) + 1
    gui.step()

    status = gui.status()
    assert (status["state"], status["reason"]) == ("error", "auth"), status
    run = read_sync_state(vault).last_run
    assert run is not None
    assert (run.trigger, run.result, run.reason, run.sign_in) == (
        "auto", "failed", "auth", "password",
    )
    # rclone's own line, kept: it was the stamp it could not read.
    assert ".litman-sync-stamp.json" in (run.error or "")
    assert (run.error or "").startswith(
        f"wd refused the password, so nothing was synced. {_PASSWORD_WAY_BACK}"
    )


@needs_rclone
def test_a_cloud_unreachable_at_the_stamp_check_is_retried_and_not_recorded(
    tmp_path: Path, dav: _Dav
) -> None:
    """The reverse of the test above: no stop, and the record stays the last sync's."""
    vault = _synced_library(tmp_path)
    clock = _Clock()
    gui = _gui(vault, clock)
    gui.step()
    assert gui.status()["state"] == "ok", gui.status()
    before = read_sync_state(vault).last_run
    dav.stop()

    clock.now += sched_mod.STAMP_EVERY_S * (1 + sched_mod.STAMP_JITTER) + 1
    gui.step()

    status = gui.status()
    assert (status["state"], status["reason"]) == ("offline", "offline"), status
    assert read_sync_state(vault).last_run == before
