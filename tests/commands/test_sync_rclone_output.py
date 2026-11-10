"""What `lit sync`, push and pull show of rclone: litman's own progress line, no rclone line.

rclone's stats lines become one line of litman's, redrawn in place on a
terminal and blanked once rclone is done; an agent or a pipe gets none of it.
rclone's other lines are not shown either: a two-way run keeps them in the
record ``lit sync status`` shows, an error litman recognises says it in its own
words, and any other error, a push's or pull's included, ends with rclone's
last lines. The GUI's auto sync hears rclone's lines as before, and records no
stats line either.

The rclone runs against the local remote are real; what a cloud remote adds
(Google Drive's notice, stats lines of an older rclone, an error) is printed
ahead of them by a stand-in that then runs rclone itself
(:func:`_rclone_saying`).
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from litman.commands import sync as sync_cmd
from litman.core import sync as core_sync
from litman.core.library import create_vault
from litman.core.sync import (
    RUN_RCLONE_LINE_CHARS,
    RUN_RCLONE_LINES_CAP,
    LastRun,
    SetupPayload,
    SyncProgress,
    is_rclone_stats,
    parse_rclone_stats,
    read_sync_state,
    record_run,
    write_sync_to_config,
)
from litman.exceptions import SyncAuthError, SyncStoppedError
from litman.server.sync_scheduler import SyncScheduler
from tests.commands.test_sync_bisync import (
    _fake_rclone,
    _machine,
    _sync,
    _write_paper,
    cloud,
    needs_rclone,
    tmp_path,
)

__all__ = ["cloud", "tmp_path"]  # fixtures, used by name

_DATE = "2026/11/09 10:14:46 "
# What rclone says on every run against a Google Drive remote with its shared client id.
_CLIENT_ID = (
    "NOTICE: gdrive: This remote uses rclone's shared Google Drive client_id, which "
    "is being retired and will stop working during 2026. Create your own client_id "
    "to avoid interruption: https://rclone.org/drive/#making-your-own-client-id"
)
_STATS = "NOTICE:     1.000 MiB / 4.000 MiB, 25%, 1.000 MiB/s, ETA 3s"
# As an rclone older than litman supports words it: known for stats, not read.
_OLD_STATS = "NOTICE:      2.505M / 3.492 MBytes, 72%, 102.023 kBytes/s, ETA 9s"
_NOTHING_YET = "NOTICE:           0 B / 0 B, -, 0 B/s, ETA -"
# A Google Drive remote's run, before rclone's own lines.
_DRIVE = "".join(f"{_DATE}{line}\n" for line in (_CLIENT_ID, _CLIENT_ID, _STATS, _OLD_STATS))
_DRAWN = "Copying 1.0 MiB of 4.0 MiB (25%)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Terminal(io.StringIO):
    """What a console writes to, standing for a terminal."""

    def isatty(self) -> bool:
        return True


def _terminal(width: int = 200) -> tuple[Console, _Terminal]:
    screen = _Terminal()
    console = Console(
        file=screen, force_terminal=True, color_system=None, width=width, legacy_windows=False
    )
    return console, screen


def _visible(out: str) -> list[str]:
    """The rows ``out`` leaves on a terminal: a carriage return writes over its row."""
    rows, col = [""], 0
    for part in re.split(r"(\r|\n)", out):
        if part == "\n":
            rows.append("")
            col = 0
        elif part == "\r":
            col = 0
        elif part:
            row = rows[-1].ljust(col)
            rows[-1] = row[:col] + part + row[col + len(part) :]
            col += len(part)
    return [row.rstrip() for row in rows]


def _rclone_saying(
    monkeypatch: pytest.MonkeyPatch, said: str, *, exit_code: int | None = None
) -> list[dict[str, Any]]:
    """Make every rclone run litman streams print ``said`` first, then run rclone.

    With ``exit_code`` it exits with that instead of running rclone. Returns
    each run's arguments and ``on_progress``, as litman passed them.
    """
    rclone = shutil.which("rclone")
    then = (
        f"sys.exit({exit_code})"
        if exit_code is not None
        else f"sys.exit(subprocess.call([{rclone!r}, *sys.argv[1:]]))"
    )
    script = (
        f"import subprocess, sys\nsys.stdout.write({said!r})\nsys.stdout.flush()\n{then}\n"
    )
    real_run, real_never_ask = core_sync.run_bisync, core_sync._never_ask_args
    calls: list[dict[str, Any]] = []

    def run(
        args: list[str], on_log: Any = None, **kwargs: Any
    ) -> core_sync.BisyncRun:
        calls.append({"args": list(args), "on_progress": kwargs.get("on_progress")})
        # rclone's own options go after the script, where Python leaves them be.
        ask = real_never_ask()
        with monkeypatch.context() as m:
            m.setattr(core_sync, "RCLONE_BIN", sys.executable)
            m.setattr(core_sync, "_never_ask_args", lambda: [])
            return real_run(["-c", script, *ask, *args], on_log, **kwargs)

    monkeypatch.setattr(core_sync, "run_bisync", run)
    return calls


def _stats_every(call: dict[str, Any]) -> str:
    args: list[str] = call["args"]
    return args[args.index("--stats") + 1]


def _synced_once(tmp_path: Path, cloud: Path) -> Path:
    """A library synced once, with a change made since."""
    vault = _machine(tmp_path, cloud, "a")
    _write_paper(vault, "p1")
    assert _sync(vault).exit_code == 0
    _write_paper(vault, "p2")
    return vault


def _last_run(vault: Path) -> LastRun:
    run = read_sync_state(vault).last_run
    assert run is not None
    return run


# ---------------------------------------------------------------------------
# Telling rclone's stats lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "progress"),
    [
        pytest.param(_STATS, SyncProgress(1 << 20, 4 << 20, 25), id="mid-copy"),
        pytest.param(
            "NOTICE:     2.505 MiB / 3.492 MiB, 72%, 102.023 KiB/s, ETA 9s",
            SyncProgress(round(2.505 * 2**20), round(3.492 * 2**20), 72),
            id="fractions",
        ),
        pytest.param(
            "NOTICE:    7.621 MiB / 28.610 MiB, 27%, 0 B/s, ETA -",
            SyncProgress(round(7.621 * 2**20), round(28.610 * 2**20), 27),
            id="no-speed-yet",
        ),
        pytest.param(
            "NOTICE:         593 B / 593 B, 100%, 0 B/s, ETA -",
            SyncProgress(593, 593, 100),
            id="bytes",
        ),
        pytest.param(_NOTHING_YET, SyncProgress(0, 0, None), id="nothing-to-copy"),
        pytest.param(
            "NOTICE: 2.505 MiB / 3.492 MiB, -, 0 B/s, ETA -",
            SyncProgress(round(2.505 * 2**20), round(3.492 * 2**20), None),
            id="no-percentage",
        ),
        pytest.param(
            "NOTICE:    1.250 GiB / 2.500 GiB, 50%, 12.500 MiB/s, ETA 1m42s (xfr#3/7)",
            SyncProgress(round(1.25 * 2**30), round(2.5 * 2**30), 50),
            id="with-transfers",
        ),
    ],
)
def test_a_stats_line_is_known_and_read(line: str, progress: SyncProgress) -> None:
    assert is_rclone_stats(line)
    assert parse_rclone_stats(line) == progress


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(_OLD_STATS, id="older-rclone"),
        pytest.param("NOTICE:     2.505 MB / 3.492 MB, 72%, 102 kB/s, ETA 9s", id="decimal-units"),
        pytest.param("NOTICE:     2.505 MiB / 3.492 MiB", id="cut-short"),
    ],
)
def test_a_stats_line_worded_otherwise_is_known_but_not_read(line: str) -> None:
    assert is_rclone_stats(line)
    assert parse_rclone_stats(line) is None


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(_CLIENT_ID, id="a-notice"),
        pytest.param("NOTICE: 2 files were left out", id="a-count"),
        pytest.param("ERROR : 1.000 MiB / 4.000 MiB, 25%, 0 B/s, ETA -", id="an-error"),
        pytest.param("INFO  : Path1 checking for diffs", id="info"),
    ],
)
def test_no_other_line_is_taken_for_stats(line: str) -> None:
    assert not is_rclone_stats(line)


# ---------------------------------------------------------------------------
# Where rclone's lines go
# ---------------------------------------------------------------------------


_A_FAILED_RUN = "".join(
    f"{_DATE}{line}\n"
    for line in (_CLIENT_ID, _STATS, _OLD_STATS, _NOTHING_YET, "ERROR : an error")
)


def test_stats_lines_go_to_the_progress_line_and_nowhere_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _fake_rclone(monkeypatch, _A_FAILED_RUN, code=1)
    shown: list[str] = []
    progress: list[SyncProgress | None] = []

    run = core_sync.run_bisync(args, shown.append, on_progress=progress.append)
    error = core_sync._bisync_failure(run.returncode, list(run.lines), Path("."))

    assert run.returncode == 1
    assert list(run.lines) == shown == [_CLIENT_ID, "ERROR : an error"]
    # The older wording is neither read nor shown; the end comes as None.
    assert progress == [SyncProgress(1 << 20, 4 << 20, 25), SyncProgress(0, 0, None), None]
    assert str(error).endswith(f"\n{_CLIENT_ID}\nERROR : an error")
    assert error.rclone_tail == f"{_CLIENT_ID}\nERROR : an error"


def test_rclones_last_lines_say_each_line_once() -> None:
    """Google Drive's notice comes twice a run; the error quotes it once, where it last came."""
    lines = [_CLIENT_ID, _CLIENT_ID, "ERROR : one", "", _CLIENT_ID, "NOTICE: two", "ERROR : one"]

    error = core_sync._bisync_failure(2, lines, Path("."))

    assert error.rclone_tail == f"{_CLIENT_ID}\nNOTICE: two\nERROR : one"
    assert str(error).endswith(f"\n{error.rclone_tail}")


def test_without_a_progress_line_stats_lines_reach_on_log_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUI's auto sync: every line rclone logs, as it logs it, still to on_log."""
    args = _fake_rclone(monkeypatch, _A_FAILED_RUN, code=1)
    shown: list[str] = []

    run = core_sync.run_bisync(args, shown.append)

    assert shown == [_CLIENT_ID, _STATS, _OLD_STATS, _NOTHING_YET, "ERROR : an error"]
    assert list(run.lines) == [_CLIENT_ID, "ERROR : an error"]


def test_the_progress_line_hears_the_end_however_the_run_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _fake_rclone(monkeypatch, f"{_DATE}{_STATS}\n{_DATE}{_CLIENT_ID}\n")
    progress: list[SyncProgress | None] = []

    def gone(line: str) -> None:
        raise RuntimeError("the screen went away")

    with pytest.raises(RuntimeError):
        core_sync.run_bisync(args, gone, on_progress=progress.append)

    assert progress == [SyncProgress(1 << 20, 4 << 20, 25), None]


def test_rclone_logs_its_stats_every_30_seconds_unless_litman_draws_them(
    tmp_path: Path,
) -> None:
    vault = create_vault(tmp_path)
    paths = core_sync.bisync_paths(vault, "fake-cloud:lib")

    slow = core_sync.bisync_args(vault, "fake-cloud:lib", paths, backup_name="b")
    fast = core_sync.bisync_args(vault, "fake-cloud:lib", paths, backup_name="b", stats="1s")

    assert slow[slow.index("--stats") + 1] == "30s"
    assert fast[fast.index("--stats") + 1] == "1s"


# ---------------------------------------------------------------------------
# The progress line
# ---------------------------------------------------------------------------


def test_the_progress_line_is_redrawn_in_place_and_blanked_at_the_end() -> None:
    console, screen = _terminal()
    line = sync_cmd._ProgressLine(console)

    line(SyncProgress(0, 0, None))  # nothing to copy yet: nothing drawn
    line(SyncProgress(1023 << 10, 4 << 20, 24))
    line(SyncProgress(1 << 20, 4 << 20, 25))
    line(SyncProgress(4 << 20, 4 << 20, 100))
    line(None)

    first, second, third = (
        "Copying 1023.0 KiB of 4.0 MiB (24%)",
        "Copying 1.0 MiB of 4.0 MiB (25%)",
        "Copying 4.0 MiB of 4.0 MiB (100%)",
    )
    assert screen.getvalue() == (
        f"\r{first}\r{second}{' ' * (len(first) - len(second))}"
        f"\r{third}\r{' ' * len(third)}\r"
    )
    assert _visible(screen.getvalue()) == [""]


def test_the_progress_line_never_wraps() -> None:
    console, screen = _terminal(width=20)
    line = sync_cmd._ProgressLine(console)

    line(SyncProgress(1 << 20, 4 << 20, 25))
    line(None)

    drawn = [part for part in screen.getvalue().split("\r") if part.strip()]
    assert drawn == ["Copying 1.0 MiB of "]
    assert _visible(screen.getvalue()) == [""]


def test_the_progress_line_works_out_a_missing_percentage() -> None:
    console, screen = _terminal()

    sync_cmd._ProgressLine(console)(SyncProgress(1 << 20, 4 << 20, None))

    assert screen.getvalue() == f"\r{_DRAWN}"


def test_nothing_is_drawn_but_on_a_terminal() -> None:
    piped = io.StringIO()
    # FORCE_COLOR makes Rich take a pipe for a terminal; the file is not one.
    forced = io.StringIO()
    for console in (
        Console(file=piped, width=80),
        Console(file=forced, force_terminal=True, width=80),
    ):
        line = sync_cmd._ProgressLine(console)
        line(SyncProgress(1 << 20, 4 << 20, 25))
        line(None)
    assert piped.getvalue() == forced.getvalue() == ""

    console, screen = _terminal()
    sync_cmd._ProgressLine(console)(None)
    assert screen.getvalue() == ""


# ---------------------------------------------------------------------------
# The last-run record
# ---------------------------------------------------------------------------


def test_the_record_keeps_rclones_lines_once_without_stats_or_what_the_error_quotes() -> None:
    run = LastRun(
        at="2026-11-10T12:00:00+01:00",
        trigger="terminal",
        result="failed",
        error="The sign-in to gdrive has expired, so nothing was synced.\nERROR : invalid_grant",
    )
    long = "NOTICE: " + "x" * 900
    heard = [
        _CLIENT_ID, _STATS, _OLD_STATS, _NOTHING_YET, _CLIENT_ID, "ERROR : invalid_grant",
        "", long, *(f"NOTICE: line {i}" for i in range(20)),
    ]

    kept = core_sync._with_rclone_lines(run, heard).rclone_lines

    assert len(kept) == RUN_RCLONE_LINES_CAP
    assert kept[:2] == (_CLIENT_ID, long[:RUN_RCLONE_LINE_CHARS])
    assert kept[2:] == tuple(f"NOTICE: line {i}" for i in range(RUN_RCLONE_LINES_CAP - 2))


def test_rclones_lines_survive_the_state_file_and_an_older_record_still_reads(
    tmp_path: Path,
) -> None:
    vault = create_vault(tmp_path)
    run = LastRun(
        at="2026-11-10T12:00:00+01:00",
        trigger="terminal",
        result="ok",
        rclone_lines=(_CLIENT_ID, "NOTICE: another"),
    )

    record_run(vault, run)

    assert _last_run(vault) == run
    head = "last-run:\n  at: '2026-11-10T12:00:00+01:00'\n  trigger: terminal\n  result: ok\n"
    for lines, kept in (
        ("", ()),  # an older litman's record
        ("  rclone-lines: one line\n", ()),
        (f"  rclone-lines:\n  - {json.dumps(_CLIENT_ID)}\n  - 3\n  - null\n", (_CLIENT_ID,)),
    ):
        (vault / ".litman-sync-state.yaml").write_text(head + lines, encoding="utf-8")
        assert _last_run(vault).rclone_lines == kept


def test_status_lists_what_rclone_said_under_the_last_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_cmd, "console", Console(width=400))
    vault = create_vault(tmp_path)
    write_sync_to_config(
        vault / "lit-config.yaml", SetupPayload(remote="fake-cloud", path="lib")
    )
    record_run(
        vault,
        LastRun(
            at="2026-11-10T12:00:00+01:00",
            trigger="terminal",
            result="ok",
            rclone_lines=(_CLIENT_ID, "NOTICE: another"),
        ),
    )

    result = _sync(vault, "status", "--local")

    assert result.exit_code == 0, result.output
    rows = result.output.splitlines()
    said = rows.index("  rclone said:")
    assert rows[said + 1 : said + 3] == [f"    {_CLIENT_ID}", "    NOTICE: another"]


# ---------------------------------------------------------------------------
# lit sync, end to end
# ---------------------------------------------------------------------------


@needs_rclone
def test_a_terminal_sees_litmans_progress_line_then_the_result(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _synced_once(tmp_path, cloud)
    calls = _rclone_saying(monkeypatch, _DRIVE)
    console, screen = _terminal()
    monkeypatch.setattr(sync_cmd, "console", console)

    result = _sync(vault)

    out = screen.getvalue()
    assert result.exit_code == 0, out
    assert f"\r{_DRAWN}" in out
    assert "NOTICE" not in out + result.output
    assert "MBytes" not in out
    rows = _visible(out)
    # The banner, then the result: no progress left, no blank row.
    assert rows[0].startswith("Syncing ")
    assert rows[1].startswith("╭")
    assert not any("Copying" in row for row in rows)
    assert calls and all(_stats_every(call) == "1s" for call in calls)


@needs_rclone
def test_an_agent_sees_neither_progress_nor_rclones_lines_and_status_has_them(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _synced_once(tmp_path, cloud)
    _rclone_saying(monkeypatch, _DRIVE)
    monkeypatch.setattr(sync_cmd, "console", Console(width=400))

    result = _sync(vault)

    assert result.exit_code == 0, result.output
    for unseen in ("\r", "Copying", "NOTICE", "MiB"):
        assert unseen not in result.output
    rclone_lines = _last_run(vault).rclone_lines
    assert rclone_lines.count(_CLIENT_ID) == 1
    assert not any(is_rclone_stats(line) for line in rclone_lines)
    status = _sync(vault, "status", "--local")
    assert status.exit_code == 0, status.output
    assert "rclone said:" in status.output
    assert status.output.count(_CLIENT_ID) == 1


_EXPIRED = (
    "ERROR : Bisync critical error: couldn't fetch token: invalid_grant: maybe "
    'token expired? - try refreshing with "rclone config reconnect fake-cloud:"'
)
_TOO_MANY = (
    'ERROR : Safety abort: too many deletes (>50%, 3 of 4) on Path1 "lib". '
    "Run with --force if desired."
)


@needs_rclone
@pytest.mark.parametrize(
    ("said", "kind", "words"),
    [
        pytest.param(
            _EXPIRED,
            SyncAuthError,
            "The sign-in to fake-cloud has expired, so nothing was synced. Sign in "
            "again with `rclone config reconnect fake-cloud:`, then run `lit sync`.",
            id="expired-sign-in",
        ),
        pytest.param(
            _TOO_MANY,
            SyncStoppedError,
            "rclone stopped: more than half of the files were deleted on this machine, "
            "so nothing was deleted in the cloud. If that is intended, run "
            "`lit sync --force`; the deleted files go to the backup.",
            id="safety-stop",
        ),
    ],
)
def test_an_error_litman_knows_is_said_in_its_own_words_and_recorded_whole(
    tmp_path: Path,
    cloud: Path,
    monkeypatch: pytest.MonkeyPatch,
    said: str,
    kind: type[Exception],
    words: str,
) -> None:
    vault = _synced_once(tmp_path, cloud)
    _rclone_saying(monkeypatch, f"{_DATE}{_STATS}\n{_DATE}{said}\n", exit_code=1)
    monkeypatch.setattr(sync_cmd, "console", Console(width=400))

    result = _sync(vault)

    assert result.exit_code == 1
    assert isinstance(result.exception, kind), result.output
    assert str(result.exception) == words
    assert "ERROR" not in result.output
    run = _last_run(vault)
    assert said in (run.error or "")
    assert "MiB" not in (run.error or "")
    assert run.rclone_lines == ()  # the error quotes rclone's only line


@needs_rclone
def test_an_error_litman_does_not_know_keeps_rclones_last_lines(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _synced_once(tmp_path, cloud)
    odd = "ERROR : Bisync critical error: something no one has seen before"
    _rclone_saying(monkeypatch, f"{_DATE}{_STATS}\n{_DATE}{odd}\n", exit_code=2)

    result = _sync(vault)

    assert result.exit_code == 1
    message = str(result.exception)
    assert message.startswith("rclone could not finish the sync (exit 2).")
    assert message.endswith(f"rclone's last lines say why:\n{odd}")


@needs_rclone
def test_the_first_from_panel_leaves_rclones_lines_to_the_record(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _synced_once(tmp_path, cloud)
    empty = (
        "ERROR : Bisync critical error: empty current Path2 listing. Cannot sync "
        "to an empty directory: lib"
    )
    _rclone_saying(monkeypatch, f"{_DATE}{empty}\n", exit_code=2)
    monkeypatch.setattr(sync_cmd, "console", Console(width=400))

    result = _sync(vault)

    assert result.exit_code == sync_cmd.EXIT_SYNC_NEEDS_CHOICE, result.output
    assert "The cloud folder is empty" in result.output
    assert "Cannot sync to an empty directory" not in result.output
    assert empty in (_last_run(vault).error or "")


# ---------------------------------------------------------------------------
# push and pull
# ---------------------------------------------------------------------------


@needs_rclone
def test_push_and_pull_show_the_same_progress_line_and_no_rclone_line(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    b = _machine(tmp_path, cloud, "b")
    calls = _rclone_saying(monkeypatch, _DRIVE)

    # A first push shows its size first and asks; --yes answers.
    for vault, verb, banner in ((a, ("push", "--yes"), "Pushing "), (b, ("pull",), "Pulling ")):
        console, screen = _terminal()
        monkeypatch.setattr(sync_cmd, "console", console)

        result = _sync(vault, *verb)

        out = screen.getvalue()
        assert result.exit_code == 0, out
        assert f"\r{_DRAWN}" in out
        assert "NOTICE" not in out + result.output
        rows = _visible(out)
        at = next(i for i, row in enumerate(rows) if row.startswith(banner))
        assert rows[at + 1].startswith("╭")
        assert not any("Copying" in row for row in rows)
    assert (b / "papers" / "p1" / "metadata.yaml").is_file()
    assert [_stats_every(call) for call in calls] == ["1s", "1s"]


@needs_rclone
def test_a_failed_push_ends_with_rclones_last_lines(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _machine(tmp_path, cloud, "a")
    _write_paper(vault, "p1")
    failed = (
        "ERROR : papers/p1/paper.pdf: Failed to copy: quota exceeded",
        "ERROR : Failed to sync: quota exceeded",
    )
    said = "".join(f"{_DATE}{line}\n" for line in (_STATS, _CLIENT_ID, *failed))
    _rclone_saying(monkeypatch, said, exit_code=7)

    result = _sync(vault, "push", "--yes")

    assert result.exit_code == 1
    assert str(result.exception) == (
        "rclone could not finish the push (exit 7). Run `lit sync push` again; "
        "rclone's last lines say why:\n" + "\n".join((_CLIENT_ID, *failed))
    )
    assert read_sync_state(vault).last_push is None


@needs_rclone
def test_a_push_or_pull_the_cloud_refused_says_how_to_sign_in_and_keeps_rclones_lines(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push or pull keeps no record, so rclone's lines stay in its error."""
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a, "push", "--yes").exit_code == 0  # a library in the cloud to pull
    b = _machine(tmp_path, cloud, "b")
    _rclone_saying(monkeypatch, f"{_DATE}{_STATS}\n{_DATE}{_EXPIRED}\n", exit_code=1)

    for vault, verb, done in ((a, "push", "pushed"), (b, "pull", "pulled")):
        result = _sync(vault, verb)

        assert result.exit_code == 1
        err = result.exception
        assert isinstance(err, SyncAuthError), result.output
        assert err.sign_in == "browser"
        assert str(err) == (
            f"The sign-in to fake-cloud has expired, so nothing was {done}. Sign in again "
            f"with `rclone config reconnect fake-cloud:`, then run `lit sync {verb}`."
            f"\n{_EXPIRED}"
        )


@pytest.mark.parametrize("sign_in", ["browser", "password", None])
def test_the_way_back_names_the_command_to_run_again(sign_in: str | None) -> None:
    two_way = core_sync.sign_in_way_out("gdrive", sign_in)

    assert two_way.endswith(", then run `lit sync`.")
    for verb in ("push", "pull"):
        assert core_sync.sign_in_way_out("gdrive", sign_in, verb) == two_way.replace(
            "`lit sync`", f"`lit sync {verb}`"
        )


# ---------------------------------------------------------------------------
# The GUI's auto sync
# ---------------------------------------------------------------------------


@needs_rclone
def test_the_guis_auto_sync_hears_rclone_as_before_and_records_no_stats(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _synced_once(tmp_path, cloud)
    calls = _rclone_saying(monkeypatch, _DRIVE)
    scheduler = SyncScheduler(SimpleNamespace(vault=vault.resolve()), clock=lambda: 1000.0)
    heard: list[str] = []
    monkeypatch.setattr(scheduler, "_log", heard.append)

    scheduler.step()

    assert scheduler.status()["state"] == "ok", scheduler.status()
    assert calls and all(call["on_progress"] is None for call in calls)
    assert all(_stats_every(call) == "30s" for call in calls)
    assert heard[:4] == [_CLIENT_ID, _CLIENT_ID, _STATS, _OLD_STATS]
    rclone_lines = _last_run(vault).rclone_lines
    assert rclone_lines.count(_CLIENT_ID) == 1
    assert not any(is_rclone_stats(line) for line in rclone_lines)
