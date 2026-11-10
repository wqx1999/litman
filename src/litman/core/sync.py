"""rclone-backed vault sync helpers (M6.1, ADR-030).

Pure helpers around the ``rclone`` binary plus the per-machine sync-state
file. The CLI layer in ``litman.commands.sync`` is intentionally thin; all
subprocess invocation, parsing, and state-file IO lives here so it can be
unit-tested without going through Click.

Design choices baked in (per M6 spec + ADR-002, ADR-003, ADR-030):

- **One-way push / pull.** ``push`` and ``pull`` map to ``rclone sync`` in
  opposite directions; both wipe extraneous files on the destination so the
  two ends stay byte-for-byte. They are the backup and restore tools.
- **Two-way sync is ``rclone bisync``** (:func:`two_way_sync`, ADR-030), for
  one library used on several machines. litman fixes the flags, keeps every
  replaced or deleted file in a backup folder on both sides, moves the older
  copy of a conflict out of the vault, and re-locks TRUTH and rebuilds the
  derived files afterwards. Only the true library files travel: derived files,
  machine-local config and ``codes/*/repo/`` checkouts never do.
- **Default excludes are always applied.** ``.litman-staging/`` is transient
  atomic-write scratch and ``.litman-sync-state.yaml`` is per-machine state;
  both are filtered out of every sync direction regardless of user config.
- **State file is local-only.** Last-push / last-pull timestamps live in
  ``<vault>/.litman-sync-state.yaml`` and are themselves on the default
  exclude list, so machine A's clock never overwrites machine B's.
- **rclone errors propagate verbatim.** ``SyncError`` carries rclone's
  stderr tail so the user sees the actual failure (auth, network, quota)
  rather than a litman-flavoured paraphrase.
"""

from __future__ import annotations

import calendar
import contextvars
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from platformdirs import user_data_dir
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark
from ruamel.yaml.tokens import CommentToken

from litman.core.config import CONFIG_FILENAME
from litman.core.dates import now_iso
from litman.core.locking import (
    ensure_truth_locked,
    ensure_truth_unlocked,
    is_file_manager_dropping,
    rmtree,
)
from litman.core.replace import remove_file, replace_file
from litman.core.shell import shell_arg
from litman.core.sync_lock import FileLock, LockHolder, read_holder
from litman.core.terminal import hidden_console_flags
from litman.core.undecodable import config_refusal
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import (
    BisyncLockedError,
    LitmanError,
    SyncAuthError,
    SyncBusyError,
    SyncChoiceRequiredError,
    SyncError,
    SyncLockError,
    SyncNotSetUpError,
    SyncStoppedError,
)

if TYPE_CHECKING:
    from litman.core.code_sync import RepoSyncReport

RCLONE_BIN = "rclone"
SYNC_STATE_FILENAME = ".litman-sync-state.yaml"

# Filters applied to every push / pull regardless of user config. All four
# are unconditionally machine-local or derived:
#   - ``.litman-staging/`` holds in-flight atomic writes (must never leak
#     across machines);
#   - ``.litman-upload/`` holds a dropped PDF between the webUI drag-in and
#     its confirm (server/routes_ingest.py) — scratch by the same argument,
#     and the one abandoned stash a user might leave behind is the last thing
#     that should eat their cloud quota;
#   - ``views/`` is a pure derived projection of metadata, rebuilt on any
#     machine with ``lit refresh-views`` — ADR-003 mandates it in the hard
#     exclude set (review F33). It also holds the vault's only symlinks, so
#     excluding it keeps ``local_vault_size`` counting exactly what rclone
#     transfers, killing the permanent false "not in sync" delta (review F35);
#   - the sync-state file records per-machine timestamps (machine A's
#     last-push time has no meaning on B).
# (``.trash/`` is deliberately NOT excluded — ADR-003 keeps the soft-delete
# buffer backed up to the cloud during its recovery window.)
#
# rclone glob syntax: ``**`` matches across path separators; the bare
# filename matches at any depth (rclone's default behavior for ``--exclude``).
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".litman-staging/**",
    ".litman-upload/**",
    "views/**",
    SYNC_STATE_FILENAME,
)

_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    default_flow_style=False,
)


# ---------------------------------------------------------------------------
# rclone subprocess wrapper
# ---------------------------------------------------------------------------


def rclone_available() -> bool:
    """Return True iff the ``rclone`` binary is on PATH."""
    return shutil.which(RCLONE_BIN) is not None


def _require_rclone() -> None:
    """Raise ``SyncError`` with an install hint when rclone is missing."""
    if not rclone_available():
        raise SyncError(
            "`rclone` executable not found on PATH. Install it from "
            "https://rclone.org/install/ (one-liner: "
            "`curl https://rclone.org/install.sh | sudo bash`) and re-run."
        )


#: Set on the GUI's own threads (:func:`rclone_never_asks`): rclone must never
#: wait there for an answer on a terminal, the password of an encrypted rclone
#: config above all, which would block the auto sync for good.
_NEVER_ASK: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "litman_rclone_never_ask", default=False
)

#: How long rclone may take to tell its version or list its remotes.
_GATE_TIMEOUT = 30.0


@contextmanager
def rclone_never_asks() -> Iterator[None]:
    """In this thread, meanwhile: rclone gets no stdin and ``--ask-password=false``.

    For the GUI, whose rclone runs have no terminal to answer from.
    """
    token = _NEVER_ASK.set(True)
    try:
        yield
    finally:
        _NEVER_ASK.reset(token)


def _never_ask_args() -> list[str]:
    return ["--ask-password=false"] if _NEVER_ASK.get() else []


def run_rclone(
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    timeout: float | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``rclone <args>`` and return the completed process.

    Args:
        args: rclone arguments WITHOUT the leading ``rclone``. E.g.
            ``["sync", "/src", "remote:dst", "--exclude", "x"]``.
        capture: When True (default), stdout/stderr are captured and returned
            as text on the CompletedProcess; when False they inherit the
            parent stdio (used by ``lit sync setup`` to hand the TTY to
            ``rclone config``).
        check: When True (default), a non-zero rclone exit code is wrapped
            into ``SyncError`` carrying the last few stderr lines. When
            False, the caller is responsible for inspecting ``returncode``.
        timeout: Seconds rclone may take; past them it is stopped and
            ``SyncError`` raised.
        input: Text handed to rclone on its stdin (``rclone rcat``).

    Raises:
        SyncError: rclone not installed (re-checked here so unit tests that
            patch PATH don't need to mock both ``rclone_available`` and the
            subprocess), it ran past ``timeout``, or (when ``check=True``) the
            command exited non-zero.
    """
    _require_rclone()
    cmd = [RCLONE_BIN, *_never_ask_args(), *args]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=capture,
            # rclone writes UTF-8 whatever the locale; a name it prints must
            # never fail the decode.
            encoding="utf-8",
            errors="replace",
            input=input,
            stdin=subprocess.DEVNULL if input is None and _NEVER_ASK.get() else None,
            timeout=timeout,
            creationflags=hidden_console_flags(),
        )
    except subprocess.TimeoutExpired as e:
        raise SyncError(
            f"rclone did not answer within {timeout:.0f} seconds for `rclone "
            f"{' '.join(args)}`."
        ) from e
    except FileNotFoundError as e:
        # Race: PATH said rclone existed, but it disappeared. Treat as
        # missing-install for a consistent message.
        raise SyncError(
            "`rclone` executable not found on PATH (race with install?). "
            "See https://rclone.org/install/."
        ) from e

    if check and result.returncode != 0:
        stderr_tail = "\n".join(
            (result.stderr or "").strip().splitlines()[-5:]
        )
        raise SyncError(
            f"rclone exited {result.returncode} for `rclone "
            f"{' '.join(args)}`:\n{stderr_tail}"
        )
    return result


# ---------------------------------------------------------------------------
# Remote registry queries
# ---------------------------------------------------------------------------


def list_remotes() -> list[str]:
    """Return the rclone remote names registered in the active config.

    rclone's ``listremotes`` prints one remote per line with a trailing
    colon (``my-gdrive:``); we strip the colon for ergonomic comparison.
    Empty output (no remotes configured) yields ``[]``.
    """
    result = run_rclone(["listremotes"], timeout=_GATE_TIMEOUT)
    names: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        names.append(line.rstrip(":"))
    return names


def remote_exists(remote_name: str) -> bool:
    """True iff ``remote_name`` is registered in the active rclone config."""
    return remote_name in list_remotes()


# ---------------------------------------------------------------------------
# Size / count queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Size:
    """File count + byte total for a vault or remote."""

    count: int
    bytes: int


def remote_size(target_url: str, *, two_way: bool = False) -> Size:
    """Return ``Size`` of the remote location via ``rclone size --json``.

    Args:
        target_url: rclone destination (``"remote:path/"``).
        two_way: Count only what two-way sync carries
            (:func:`two_way_filter_rules`), for a library that syncs two-way.

    Raises:
        SyncError: rclone fails (path missing, auth error, etc.). A missing
            remote path returns ``Size(0, 0)`` rather than raising — that
            state is the legitimate "never pushed" case.
    """
    # The auto-sync stamp is litman's, not the library's: counted, it would
    # leave `lit sync status` one file out of step for good.
    filters = _two_way_filter_args() if two_way else list(_STAMP_EXCLUDE_ARGS)
    result = run_rclone(
        ["size", target_url, "--json", *filters],
        check=False,
    )
    if result.returncode != 0:
        # rclone exits 3/4 for "directory/file not found" — treat as empty
        # (never pushed yet). Any other non-zero is surfaced.
        if result.returncode in (3, 4):
            return Size(count=0, bytes=0)
        stderr_tail = "\n".join(
            (result.stderr or "").strip().splitlines()[-5:]
        )
        raise SyncError(
            f"rclone size exited {result.returncode} for {target_url!r}:\n"
            f"{stderr_tail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise SyncError(
            f"Could not parse `rclone size --json` output:\n{result.stdout!r}"
        ) from e
    return Size(
        count=int(payload.get("count", 0)),
        bytes=int(payload.get("bytes", 0)),
    )


def _rclone_glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate one rclone ``--exclude`` glob into an anchored full-match regex.

    Mirrors the subset of rclone filter syntax litman emits: ``**`` matches
    across path separators (recursive); ``*`` and ``?`` stay within a single
    path segment; everything else is matched literally. This is the precise
    glob the earlier ``rel.parts[0]`` over-approximation failed to honour for
    multi-segment patterns such as ``codes/*/repo/**`` (where the first
    segment is always ``codes``, never the full glob).
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _compile_exclude_matcher(
    excludes: tuple[str, ...],
) -> Callable[[Path], bool]:
    """Build a predicate `excluded(rel)` for a vault-relative path.

    A bare basename (no ``/``) excludes a file of that name at any depth,
    matching rclone's default ``--exclude`` behaviour. Any pattern containing
    ``/`` is compiled to a full-path glob (via :func:`_rclone_glob_to_regex`)
    so multi-segment globs like ``codes/*/repo/**`` actually match the files
    rclone would skip, instead of silently passing through.
    """
    basenames = frozenset(p for p in excludes if "/" not in p)
    regexes = [_rclone_glob_to_regex(p) for p in excludes if "/" in p]

    def excluded(rel: Path) -> bool:
        if rel.name in basenames:
            return True
        rel_posix = rel.as_posix()
        return any(rx.match(rel_posix) for rx in regexes)

    return excluded


def local_vault_size(
    vault: Path,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
) -> Size:
    """Walk ``vault`` and total up regular files (file count + bytes).

    Args:
        vault: Vault root.
        excludes: rclone-style exclude patterns. Only used to decide which
            files to *count*; the actual transfer applies the same patterns
            via ``rclone --exclude``.

    Excludes are matched against the relative POSIX path with the same glob
    semantics rclone uses (see :func:`_compile_exclude_matcher`), so the count
    stays consistent with what ``rclone --exclude`` transfers — including
    multi-segment globs like ``codes/*/repo/**``.
    """
    excluded = _compile_exclude_matcher(excludes)

    count = 0
    total = 0
    for path in vault.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(vault)
        if excluded(rel):
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
        count += 1
    return Size(count=count, bytes=total)


def two_way_local_size(vault: Path) -> Size:
    """File count + bytes of what two-way sync carries from ``vault``.

    The local half of :func:`remote_size` with ``two_way``: the same rules
    (:func:`_walk_synced`), links left out as rclone leaves them out.
    """
    count = 0
    total = 0
    for _rel, full in _walk_synced(vault):
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        count += 1
        total += st.st_size
    return Size(count=count, bytes=total)


def largest_files(
    vault: Path,
    n: int = 5,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
) -> list[tuple[Path, int]]:
    """Return the ``n`` largest files in ``vault`` after exclude filtering.

    Result is ``[(relative_path, byte_size), ...]`` sorted by size descending.
    Used by ``lit sync push`` to give the user a "here is what we'd transfer
    and the biggest pieces" preview before the first push.

    Walk + exclude semantics match ``local_vault_size`` to keep the two
    views consistent — what gets counted is exactly what gets transferred.
    """
    excluded = _compile_exclude_matcher(excludes)

    found: list[tuple[Path, int]] = []
    for path in vault.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(vault)
        if excluded(rel):
            continue
        try:
            sz = path.stat().st_size
        except OSError:
            continue
        found.append((rel, sz))
    found.sort(key=lambda x: x[1], reverse=True)
    return found[:n]


def codes_ignore_patterns_to_rclone(
    patterns: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Translate ``codes_ignore_patterns`` entries to rclone ``--exclude`` globs.

    The lit-config field is interpreted as relative to ``codes/<name>/``:
    ``repo/`` means "skip the checkout directory of every repo", not "skip
    a top-level repo/ folder". Conversion rules:

    - ``foo/`` (trailing slash, directory) → ``codes/*/foo/**``
    - ``foo`` (no slash, file or glob) → ``codes/*/foo``

    Empty entries are dropped silently.
    """
    out: list[str] = []
    for raw in patterns:
        pat = raw.strip()
        if not pat:
            continue
        if pat.endswith("/"):
            out.append(f"codes/*/{pat}**")
        else:
            out.append(f"codes/*/{pat}")
    return tuple(out)


def humanize_bytes(n: int) -> str:
    """Render a byte count as a human-friendly string (``1.2 MiB``)."""
    if n < 1024:
        return f"{n} B"
    units = ["KiB", "MiB", "GiB", "TiB", "PiB"]
    val = float(n)
    for unit in units:
        val /= 1024.0
        if val < 1024.0:
            return f"{val:.1f} {unit}"
    return f"{val:.1f} EiB"


# ---------------------------------------------------------------------------
# Sync state file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncState:
    """Per-machine sync state recorded in ``.litman-sync-state.yaml``.

    The timestamps are local-machine ISO 8601 strings (or ``None`` for
    "never"). The file is excluded from sync, so each machine keeps its own
    record without clobbering peers.

    ``last_sync`` is the last successful two-way sync; ``last_sync_result`` is
    how the most recent two-way attempt ended: ``ok``, ``conflicts``,
    ``needs-choice`` or ``failed``. ``last_run`` holds that attempt's details
    for ``lit sync status`` (:class:`LastRun`). ``set_up`` is when ``lit sync
    setup`` last ran on this machine: the sync tip counts from it while
    nothing has synced from here yet (None from a setup an older litman made).
    """

    last_push: str | None = None
    last_pull: str | None = None
    last_sync: str | None = None
    last_sync_result: str | None = None
    last_run: LastRun | None = None
    set_up: str | None = None


def read_sync_state(vault: Path) -> SyncState:
    """Load ``<vault>/.litman-sync-state.yaml`` or return an empty state.

    Missing or unreadable file → ``SyncState()`` (both fields None). Errors
    are deliberately swallowed: the state file is purely advisory and the
    user shouldn't hit a hard failure on ``lit sync status`` because of a
    transient permission glitch.
    """
    path = vault / SYNC_STATE_FILENAME
    if not path.is_file():
        return SyncState()
    try:
        data = _yaml.load(path.read_text(encoding="utf-8"))
    except Exception:
        return SyncState()
    if not isinstance(data, dict):
        return SyncState()
    def field(key: str) -> str | None:
        value = data.get(key)
        return str(value) if value else None

    return SyncState(
        last_push=field("last-push"),
        last_pull=field("last-pull"),
        last_sync=field("last-sync"),
        last_sync_result=field("last-sync-result"),
        last_run=_last_run_from(data.get("last-run")),
        set_up=field("set-up"),
    )


def write_sync_state(vault: Path, state: SyncState) -> None:
    """Write ``state`` to ``<vault>/.litman-sync-state.yaml`` atomically.

    Uses a tmp-file + ``os.replace`` rename to avoid leaving a half-written
    file behind. The full ``staged_write`` machinery is overkill here — only
    one file is touched and rclone runs immediately before/after, not in
    the same atomic group.
    """
    path = vault / SYNC_STATE_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload: dict[str, Any] = {
        "last-push": state.last_push,
        "last-pull": state.last_pull,
        "last-sync": state.last_sync,
        "last-sync-result": state.last_sync_result,
    }
    if state.last_run is not None:
        payload["last-run"] = _last_run_to(state.last_run)
    if state.set_up is not None:
        payload["set-up"] = state.set_up
    buf = io.StringIO()
    _yaml.dump(payload, buf)
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    replace_file(tmp, path)


def stamp_set_up(vault: Path) -> None:
    """Record that sync was set up on this machine now, preserving the other fields."""
    state = read_sync_state(vault)
    write_sync_state(vault, replace(state, set_up=now_iso()))


def stamp_push(vault: Path) -> None:
    """Update ``last-push`` to now, preserving the other fields."""
    state = read_sync_state(vault)
    write_sync_state(vault, replace(state, last_push=now_iso()))


def stamp_pull(vault: Path) -> None:
    """Update ``last-pull`` to now, preserving the other fields."""
    state = read_sync_state(vault)
    write_sync_state(vault, replace(state, last_pull=now_iso()))


def stamp_two_way(vault: Path, result: str) -> None:
    """Record how a two-way sync attempt ended, preserving the other fields.

    ``last-sync`` moves only when the sync went through (``ok`` /
    ``conflicts``); a failed or refused attempt records just its result.
    """
    state = read_sync_state(vault)
    if result in ("ok", "conflicts"):
        state = replace(state, last_sync=now_iso())
    write_sync_state(vault, replace(state, last_sync_result=result))


RunTrigger = Literal["terminal", "auto", "exit"]

#: How many conflicts, and how many repos, a :class:`LastRun` lists; the
#: totals count them all.
RUN_RECORD_CAP = 20
#: The most characters of an error a :class:`LastRun` keeps.
RUN_ERROR_CAP = 8000
#: How many of rclone's own lines a :class:`LastRun` keeps, and the most
#: characters of each.
RUN_RCLONE_LINES_CAP = 10
RUN_RCLONE_LINE_CHARS = 500


@dataclass(frozen=True)
class RunConflict:
    """One thing a run had to settle, and where the copy that lost went.

    ``kept`` is ``local`` or ``cloud`` for a file changed on both sides (whose
    copy stayed at ``path``), ``deleted-here`` or ``deleted-there`` for a
    folder deleted on one machine and changed on the other (the deletion won;
    ``path`` is the folder).
    """

    path: str
    kept: str
    backup: str


RepoRowKind = Literal["not-restored", "left-behind", "failed"]


@dataclass(frozen=True)
class RunRepo:
    """A code checkout that did not follow the library, and the way out.

    ``kind`` says which way: ``not-restored`` (not brought to the commit the
    library records), ``left-behind`` (the repo was removed from the library
    and its folder stays on this machine) or ``failed`` (the repo step itself
    failed; ``name`` is empty). ``what`` and ``way_out`` are one sentence
    each, for ``lit sync status``.
    """

    name: str
    what: str
    way_out: str
    kind: str = "not-restored"


@dataclass(frozen=True)
class RunRepoWork:
    """A code checkout holding changes of the user's that no other machine gets.

    ``uncommitted``: changed or untracked files. ``unpushed``: how many
    commits no remote has. The run went through: this is for the user to
    know, not a failure.
    """

    name: str
    uncommitted: bool = False
    unpushed: int = 0


@dataclass(frozen=True)
class LastRun:
    """How this machine's last two-way sync run ended, for ``lit sync status``.

    Written by every run :func:`two_way_sync` starts (from a terminal, the
    GUI's auto sync or its sync on exit: ``trigger``), and by the GUI when it
    cannot start one; not by a run that another sync held off. Kept in the
    per-machine state file, which never syncs.

    ``result`` is ``ok``, ``conflicts``, ``needs-choice`` or ``failed``.
    ``reason`` says why a run stopped, where that is known: ``auth``,
    ``safety-stop``, ``choice``, ``lock`` or ``rclone``. ``error`` is the
    whole error text. ``sign_in``, for ``auth``, says how the user signs in
    again (:attr:`litman.exceptions.SyncAuthError.sign_in`), so that reading
    the record needs no rclone. ``repos_with_work`` names the code checkouts
    holding changes only this machine has (:class:`RunRepoWork`). The lists
    hold the first :data:`RUN_RECORD_CAP` entries; the totals count all of
    them. ``rclone_lines`` are the lines rclone logged at NOTICE and above
    in the run's passes, which ``lit sync`` does not show: each once, in
    order, without its stats lines and without the lines ``error`` already
    quotes, at most :data:`RUN_RCLONE_LINES_CAP`.
    """

    at: str
    trigger: str
    result: str
    reason: str | None = None
    error: str | None = None
    conflicts: tuple[RunConflict, ...] = ()
    conflicts_total: int = 0
    repos: tuple[RunRepo, ...] = ()
    repos_total: int = 0
    sign_in: str | None = None
    repos_with_work: tuple[RunRepoWork, ...] = ()
    repos_with_work_total: int = 0
    rclone_lines: tuple[str, ...] = ()


def repo_rows(result: TwoWayResult) -> list[RunRepo]:
    """The code repos ``result`` left behind, each with its way out."""
    from litman.core.code import left_behind_message

    rows: list[RunRepo] = []
    if result.repos is not None:
        rows += [
            RunRepo(m.name, m.what, m.way_out, "not-restored")
            for m in result.repos.not_restored
        ]
        for name in result.repos.left_behind:
            what, way_out = left_behind_message(name)
            rows.append(RunRepo(name, what, way_out, "left-behind"))
    if result.repos_error is not None:
        rows.append(RunRepo("", result.repos_error, "", "failed"))
    return rows


def work_rows(result: TwoWayResult) -> list[RunRepoWork]:
    """The code checkouts ``result`` found holding changes only this machine has, by name."""
    report = result.repos
    if report is None:
        return []
    return [
        RunRepoWork(name, name in report.uncommitted, report.unpushed.get(name, 0))
        for name in sorted({*report.uncommitted, *report.unpushed})
    ]


def run_from_result(result: TwoWayResult, trigger: RunTrigger) -> LastRun:
    """The record of a run that went through."""
    settled = [RunConflict(c.path, c.kept, str(c.backup)) for c in result.conflicts] + [
        RunConflict(d.folder, "deleted-here" if d.deleted_here else "deleted-there", str(d.backup))
        for d in result.deleted_folders
    ]
    repos = repo_rows(result)
    work = work_rows(result)
    return LastRun(
        at=now_iso(),
        trigger=trigger,
        result="conflicts" if settled else "ok",
        conflicts=tuple(settled[:RUN_RECORD_CAP]),
        conflicts_total=len(settled),
        repos=tuple(repos[:RUN_RECORD_CAP]),
        repos_total=len(repos),
        repos_with_work=tuple(work[:RUN_RECORD_CAP]),
        repos_with_work_total=len(work),
    )


def run_from_error(
    err: BaseException, trigger: RunTrigger, *, reason: str | None = None
) -> LastRun:
    """The record of a run that ``err`` stopped; ``reason`` overrides the guess."""
    kind, guessed = classify_sync_error(err)
    return LastRun(
        at=now_iso(),
        trigger=trigger,
        result="needs-choice" if isinstance(err, SyncChoiceRequiredError) else "failed",
        reason=reason or (guessed if kind == "needs-user" else None),
        error=str(err)[:RUN_ERROR_CAP],
        sign_in=err.sign_in if isinstance(err, SyncAuthError) else None,
    )


def _with_rclone_lines(run: LastRun, heard: Iterable[str]) -> LastRun:
    """``run`` with the lines rclone logged during it (:attr:`LastRun.rclone_lines`)."""
    quoted = {line.strip() for line in (run.error or "").splitlines()}
    kept = dict.fromkeys(
        line[:RUN_RCLONE_LINE_CHARS]
        for line in heard
        if line.strip() and line.strip() not in quoted and not is_rclone_stats(line)
    )
    return replace(run, rclone_lines=tuple(kept)[:RUN_RCLONE_LINES_CAP])


def record_run(vault: Path, run: LastRun) -> None:
    """Keep ``run`` as this machine's last run, preserving the other fields.

    A state-file problem is swallowed: it must not hide how the sync went.
    """
    try:
        state = read_sync_state(vault)
        write_sync_state(vault, replace(state, last_run=run))
    except OSError:
        pass


def _last_run_to(run: LastRun) -> dict[str, Any]:
    out: dict[str, Any] = {"at": run.at, "trigger": run.trigger, "result": run.result}
    if run.reason:
        out["reason"] = run.reason
    if run.sign_in:
        out["sign-in"] = run.sign_in
    if run.error:
        out["error"] = run.error
    if run.conflicts_total:
        out["conflicts-total"] = run.conflicts_total
        out["conflicts"] = [
            {"path": c.path, "kept": c.kept, "backup": c.backup} for c in run.conflicts
        ]
    if run.repos_total:
        out["repos-total"] = run.repos_total
        out["repos"] = [
            {"name": r.name, "kind": r.kind, "what": r.what, "way-out": r.way_out}
            for r in run.repos
        ]
    if run.repos_with_work_total:
        out["repos-with-work-total"] = run.repos_with_work_total
        out["repos-with-work"] = [
            {"name": w.name, "uncommitted": w.uncommitted, "unpushed": w.unpushed}
            for w in run.repos_with_work
        ]
    if run.rclone_lines:
        out["rclone-lines"] = list(run.rclone_lines)
    return out


def _last_run_from(data: object) -> LastRun | None:
    """The record in the state file, or None when it is missing or malformed."""
    if not isinstance(data, dict):
        return None

    def text(source: dict[Any, Any], key: str) -> str | None:
        value = source.get(key)
        return str(value) if value not in (None, "") else None

    def rows(key: str) -> list[dict[Any, Any]]:
        value = data.get(key)
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    def count(key: str, listed: int) -> int:
        value = data.get(key)
        return value if isinstance(value, int) and value >= listed else listed

    def commits(source: dict[Any, Any]) -> int:
        value = source.get("unpushed")
        return value if isinstance(value, int) and value > 0 else 0

    at, trigger, result = text(data, "at"), text(data, "trigger"), text(data, "result")
    if at is None or trigger is None or result is None:
        return None
    conflicts = tuple(
        RunConflict(text(r, "path") or "", text(r, "kept") or "", text(r, "backup") or "")
        for r in rows("conflicts")
    )
    repos = tuple(
        RunRepo(
            text(r, "name") or "",
            text(r, "what") or "",
            text(r, "way-out") or "",
            text(r, "kind") or "not-restored",
        )
        for r in rows("repos")
    )
    work = tuple(
        RunRepoWork(name, r.get("uncommitted") is True, commits(r))
        for r in rows("repos-with-work")
        if (name := text(r, "name")) is not None
    )
    said = data.get("rclone-lines")
    rclone_lines = (
        tuple(line for line in said if isinstance(line, str))[:RUN_RCLONE_LINES_CAP]
        if isinstance(said, list)
        else ()
    )
    return LastRun(
        at=at,
        trigger=trigger,
        result=result,
        reason=text(data, "reason"),
        error=text(data, "error"),
        sign_in=text(data, "sign-in"),
        conflicts=conflicts,
        conflicts_total=count("conflicts-total", len(conflicts)),
        repos=repos,
        repos_total=count("repos-total", len(repos)),
        repos_with_work=work,
        repos_with_work_total=count("repos-with-work-total", len(work)),
        rclone_lines=rclone_lines,
    )


# ---------------------------------------------------------------------------
# Push / pull / status
# ---------------------------------------------------------------------------


def build_exclude_args(extra_excludes: tuple[str, ...] = ()) -> list[str]:
    """Compose ``--exclude PATTERN`` pairs for the default + extra excludes.

    The default set (always applied) is concatenated with any extras the
    caller passes. M6.2 will feed in ``codes/*/repo/**`` via this path.
    """
    args: list[str] = []
    for pat in (*DEFAULT_EXCLUDES, *extra_excludes):
        args += ["--exclude", pat]
    return args


def push(
    vault: Path,
    target_url: str,
    *,
    extra_excludes: tuple[str, ...] = (),
    dry_run: bool = False,
    lock_wait: float = 0.0,
    on_lock_wait: Callable[[LockHolder | None], None] | None = None,
    on_progress: Callable[[SyncProgress | None], None] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Mirror ``vault`` to ``target_url`` (``rclone sync`` one-way).

    ``rclone sync`` deletes destination files that are absent from the
    source — that's the right semantics for "backup to cloud", and it's
    safe because the destination is purely a copy.

    Args:
        vault: Vault root (source).
        target_url: rclone destination (``"<remote>:<path>"``).
        extra_excludes: Additional ``--exclude`` patterns layered on top of
            ``DEFAULT_EXCLUDES``. M6.2 uses this to plumb ``--exclude-repos``.
        dry_run: When True, pass ``--dry-run`` to rclone — preview only,
            no remote modifications. M6.2 surfaces this flag on the CLI.

    On success, updates the per-machine ``last-push`` timestamp (skipped on
    dry-run; we're previewing, not actually transferring).

    One-way and two-way sync do not mix: the caller first checks
    :func:`require_one_way`, which refuses a library that syncs two-way. The
    auto-sync stamp is still left out of the mirror, so that a push started
    just before another machine's first two-way sync does not delete it. A
    real push holds the library's sync lock, waiting up to ``lock_wait``
    seconds for another sync to end (``on_lock_wait`` hears when it starts
    waiting). A dry run shows rclone's output, which is what it is for; a
    real push shows none of it (:func:`_mirror`).

    Returns the dry run's process; None for a real push.

    Raises:
        SyncBusyError: another sync still held the lock after ``lock_wait``.
        SyncNotSetUpError: the library stopped syncing with ``target_url``
            while this waited for the lock.
        SyncError: rclone failed (its last lines are in the message).
    """
    args = [
        "sync", str(vault), target_url,
        *build_exclude_args(extra_excludes), *_STAMP_EXCLUDE_ARGS,
    ]
    if dry_run:
        args.append("--dry-run")
        return run_rclone(args, capture=False)
    workdir = sync_workdir(vault, target_url)
    with hold_sync_lock(workdir, by="lit sync push", wait=lock_wait, on_wait=on_lock_wait):
        require_still_set_up(vault, target_url, "pushed")
        _mirror(args, target_url, "push", on_progress)
        stamp_push(vault)
    return None


def pull(
    vault: Path,
    target_url: str,
    *,
    extra_excludes: tuple[str, ...] = (),
    dry_run: bool = False,
    lock_wait: float = 0.0,
    on_lock_wait: Callable[[LockHolder | None], None] | None = None,
    on_progress: Callable[[SyncProgress | None], None] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Mirror ``target_url`` into ``vault`` (``rclone sync`` one-way).

    Used for cross-machine recovery: a fresh machine runs ``lit sync pull``
    against the cloud-side mirror to materialise the vault. Like ``push``,
    ``rclone sync`` deletes destination files (local) absent from the source
    (cloud) — that's *correct* for the cross-machine restore use case.

    NOTE: the destination's ``.litman-sync-state.yaml`` is preserved because
    it's on the default exclude list, so a local machine's prior pull
    history is never clobbered.

    The caller first checks :func:`require_one_way`, which refuses a library
    that syncs two-way, and a cloud folder holding no ``lit-config.yaml``,
    whose pull would delete this one. The auto-sync stamp stays out of the
    vault all the same. Like :func:`push`, a real pull holds the sync lock,
    and only a dry run shows rclone's output.

    Returns the dry run's process; None for a real pull.

    Raises:
        SyncBusyError: another sync still held the lock after ``lock_wait``.
        SyncNotSetUpError: the library stopped syncing with ``target_url``
            while this waited for the lock.
        SyncError: rclone failed (its last lines are in the message).
    """
    args = [
        "sync", target_url, str(vault),
        *build_exclude_args(extra_excludes), *_STAMP_EXCLUDE_ARGS,
    ]
    if dry_run:
        args.append("--dry-run")
        return run_rclone(args, capture=False)
    workdir = sync_workdir(vault, target_url)
    with hold_sync_lock(workdir, by="lit sync pull", wait=lock_wait, on_wait=on_lock_wait):
        require_still_set_up(vault, target_url, "pulled")
        _mirror(args, target_url, "pull", on_progress)
        stamp_pull(vault)
        # rclone does not round-trip Unix permissions, so pulled TRUTH files
        # land writable (default 0o644). Re-assert the read-only lock locally
        # (M32). Only the pull path needs this — push is unaffected (0o444 is
        # readable, uploads fine).
        ensure_truth_locked(vault)
    return None


def _mirror(
    args: list[str],
    target_url: str,
    verb: OneWay,
    on_progress: Callable[[SyncProgress | None], None] | None,
) -> None:
    """Run the ``rclone sync`` of a push or pull as a two-way pass is run (:func:`run_bisync`).

    None of rclone's lines are shown: its stats lines go to ``on_progress``,
    logged every second for it, and its last other lines end the error of a
    run that failed, a refused sign-in's too (:func:`as_auth_failure`): a push
    or pull keeps no record of its run, so they are all that is left of it.

    Raises:
        SyncAuthError: the cloud refused the sign-in.
        SyncError: rclone exited non-zero for another reason.
    """
    stats = ["--stats", "1s", *_STATS_ARGS] if on_progress is not None else []
    run = run_bisync([*args, *stats], on_progress=on_progress)
    if run.returncode == 0:
        return
    tail = _tail(list(run.lines))
    err = _ending_with_rclone(
        SyncError(
            f"rclone could not finish the {verb} (exit {run.returncode}). Run "
            f"`lit sync {verb}` again; rclone's last lines say why:\n{tail}"
        ),
        tail,
    )
    refused = as_auth_failure(err, target_url, verb)
    if refused is not err:
        raise refused from err
    raise err


# -- one-way and two-way do not mix ------------------------------------------------


OneWay = Literal["push", "pull"]


def list_cloud_root(target_url: str) -> frozenset[str]:
    """The names of the files at the top of the cloud folder (one ``rclone lsf``).

    Empty when the folder does not exist yet.

    Raises:
        SyncError: rclone could not list it (offline, sign-in, provider).
    """
    result = run_rclone(
        ["lsf", "--files-only", target_url, *_STAMP_NET_FLAGS],
        check=False,
        timeout=_STAMP_RUN_TIMEOUT,
    )
    if result.returncode in (3, 4):
        return frozenset()
    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").strip().splitlines()[-3:])
        raise SyncError(
            f"rclone could not list {target_url} (exit {result.returncode}):\n{tail}"
        )
    return frozenset(line for line in (result.stdout or "").splitlines() if line)


@dataclass(frozen=True)
class SyncMode:
    """How a library is synced, from this machine's records and the cloud folder's top."""

    #: This machine has two-way sync records for the library and this cloud folder.
    two_way_here: bool
    #: The cloud folder holds the auto-sync stamp: a machine syncs it two-way.
    stamp_in_cloud: bool
    #: The cloud folder holds a ``lit-config.yaml``, which only a push puts there.
    config_in_cloud: bool

    @property
    def two_way(self) -> bool:
        return self.two_way_here or self.stamp_in_cloud


def read_sync_mode(vault: Path, target_url: str) -> SyncMode:
    """Read :class:`SyncMode`: a look at this machine's records and one listing of the cloud.

    Raises:
        SyncError: rclone could not list the cloud folder.
    """
    names = list_cloud_root(target_url)
    return SyncMode(
        two_way_here=bisync_initialised(sync_workdir(vault, target_url)),
        stamp_in_cloud=SYNC_STAMP_FILENAME in names,
        config_in_cloud=CONFIG_FILENAME in names,
    )


#: How this machine syncs a library, by its own records alone
#: (:func:`local_sync_mode`).
LocalSyncMode = Literal["two-way", "one-way", "unsynced"]


def local_sync_mode(
    vault: Path, target_url: str, state: SyncState | None = None
) -> LocalSyncMode:
    """How this machine syncs ``vault`` with ``target_url``, from its own records only.

    No rclone call, so no network: ``two-way`` once a two-way sync was tried
    here (``last-sync`` or ``last-sync-result`` in the state file) or rclone's
    records of one are in this machine's data folder; else ``one-way`` when a
    push or a pull is on record; else ``unsynced``. Unlike
    :func:`read_sync_mode` it cannot see another machine's two-way sync in the
    cloud: a machine set up to join one reads ``unsynced`` until its first
    ``lit sync``. ``state`` is the state file, when the caller has read it.
    """
    if state is None:
        state = read_sync_state(vault)
    if (
        state.last_sync is not None
        or state.last_sync_result is not None
        or bisync_initialised(sync_workdir(vault, target_url))
    ):
        return "two-way"
    if state.last_push is not None or state.last_pull is not None:
        return "one-way"
    return "unsynced"


def require_one_way(vault: Path, target_url: str, direction: OneWay) -> None:
    """Refuse a ``push`` or ``pull`` that would break a library synced two-way.

    Two-way sync never uploads ``lit-config.yaml``, ``INDEX.json`` or the
    code checkouts, so a pull, which mirrors the cloud with deletion, would
    delete them here; and a push would overwrite what the other machines
    synced. A library that syncs two-way, here or on any machine (the cloud
    holds the stamp), is refused both. A pull is refused as well when the
    cloud folder holds no ``lit-config.yaml``. Nothing is transferred.

    Raises:
        SyncError: the transfer is refused, or rclone could not list the cloud folder.
    """
    mode = read_sync_mode(vault, target_url)
    if direction == "push":
        if mode.two_way_here:
            raise SyncError(
                "This library syncs two-way, so a push would overwrite what your "
                "other machines synced. Run `lit sync` instead."
            )
        if mode.stamp_in_cloud:
            raise SyncError(
                f"{target_url} is synced two-way, so a push would overwrite what "
                "the other machines synced. Run `lit sync` instead."
            )
        return
    if mode.two_way_here:
        raise SyncError(
            "This library syncs two-way, so a pull would delete files the cloud "
            "never holds, like lit-config.yaml. Run `lit sync` instead."
        )
    if mode.stamp_in_cloud:
        raise SyncError(
            f"{target_url} is synced two-way, so a pull would delete files the "
            "cloud never holds. Run `lit sync --first-from cloud` instead."
        )
    if not mode.config_in_cloud:
        raise SyncError(
            f"{target_url} holds no lit-config.yaml, so a pull would delete this "
            "library's. Point `lit sync setup` at the folder you pushed to."
        )


@dataclass(frozen=True)
class StatusReport:
    """Snapshot of local + remote vault state, returned by ``compute_status``."""

    target_url: str
    local: Size
    remote: Size
    state: SyncState
    #: Counted with the two-way rules: the library syncs two-way.
    two_way: bool = False

    @property
    def file_delta(self) -> int:
        """``local.count - remote.count`` (positive = local has more)."""
        return self.local.count - self.remote.count

    @property
    def bytes_delta(self) -> int:
        """``local.bytes - remote.bytes`` (positive = local has more)."""
        return self.local.bytes - self.remote.bytes


def compute_status(
    vault: Path,
    target_url: str,
    *,
    extra_excludes: tuple[str, ...] = (),
    two_way: bool = False,
) -> StatusReport:
    """Gather local + remote sizes + state-file timestamps into one report.

    Two rclone calls (``listremotes`` was already done by the caller; here
    we only do ``size``). Local enumeration is a pure filesystem walk.

    ``two_way`` counts both sides with the two-way rules instead (where
    ``extra_excludes`` has no say): a library that syncs two-way leaves
    ``INDEX.json``, ``lit-config.yaml`` and the code checkouts out of the
    cloud, and reads as in sync once it is.
    """
    if two_way:
        local = two_way_local_size(vault)
    else:
        local = local_vault_size(
            vault,
            excludes=(*DEFAULT_EXCLUDES, *extra_excludes),
        )
    remote = remote_size(target_url, two_way=two_way)
    state = read_sync_state(vault)
    return StatusReport(
        target_url=target_url,
        local=local,
        remote=remote,
        state=state,
        two_way=two_way,
    )


# ---------------------------------------------------------------------------
# Setup helper — persist (remote, path) into lit-config.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SetupPayload:
    """The user-supplied values that ``lit sync setup`` writes into yaml."""

    remote: str
    path: str = ""
    exclude_repos: bool = False


def write_sync_to_config(config_path: Path, payload: SetupPayload) -> None:
    """Insert or replace the ``sync:`` block in ``lit-config.yaml``.

    Uses ruamel.yaml round-trip mode so comments / blank lines elsewhere in
    the file are preserved across the rewrite. Re-running ``lit sync setup``
    after the block already exists overwrites it in place.

    Raises:
        SyncError: file missing or unparseable. The caller (CLI) usually
            guards this with ``find_vault`` + ``load_config`` already, so
            this only fires under truly broken state (manual yaml edit gone
            wrong, mid-edit by another process, etc.). Also when a value
            is not UTF-8 text, which the config could not be read back with.
    """
    data = _config_for_edit(config_path)
    data["sync"] = {
        "remote": payload.remote,
        "path": payload.path,
        "exclude_repos": payload.exclude_repos,
    }
    message = config_refusal(data)
    if message is not None:
        raise SyncError(message)
    _write_config(config_path, data)


def remove_sync_from_config(config_path: Path) -> bool:
    """Take the ``sync:`` block out of ``lit-config.yaml``; False when it has none.

    The round trip of :func:`write_sync_to_config`, so the rest of the file
    stays as it was. ruamel keeps the comment lines that follow a block with
    the block's last entry, the commented-out ``sync:`` example of a new
    library's config among them; they belong to what follows, so they are
    hung after the entry before ``sync:`` (before the one after it, when
    ``sync:`` comes first). The block's own comments go with it.

    Raises:
        SyncError: the file is missing or holds no YAML mapping.
    """
    data = _config_for_edit(config_path)
    if "sync" not in data:
        return False
    keys = list(data)
    at = keys.index("sync")
    lines = _comment_lines_after(data["sync"])
    del data["sync"]
    if lines.strip():
        if at > 0:
            _hang_comment_lines(data, keys[at - 1], lines)
        elif len(keys) > 1:
            slot = data.ca.items.setdefault(keys[1], [None, [], None, None])
            if slot[1] is None:
                slot[1] = []
            slot[1][:0] = [
                CommentToken(line + "\n", CommentMark(0), None) for line in lines.splitlines()
            ]
    _write_config(config_path, data)
    return True


def _config_for_edit(config_path: Path) -> Any:
    """``lit-config.yaml`` read for a round trip (comments kept).

    Raises:
        SyncError: the file is missing, is not YAML, or holds no mapping.
    """
    if not config_path.is_file():
        raise SyncError(
            f"No lit-config.yaml at {config_path}. Run `lit init` first."
        )
    try:
        data = _yaml.load(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise SyncError(
            f"Failed to parse {config_path} as YAML: {e}"
        ) from e
    if data is None or not hasattr(data, "__setitem__"):
        raise SyncError(
            f"{config_path} does not contain a YAML mapping at the top level."
        )
    return data


def _write_config(config_path: Path, data: Any) -> None:
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    replace_file(tmp, config_path)


def _last_key(node: CommentedMap | CommentedSeq) -> Any:
    return list(node)[-1] if isinstance(node, CommentedMap) else len(node) - 1


def _comment_slot(node: CommentedMap | CommentedSeq, key: Any) -> tuple[list[Any], int]:
    """Where ruamel keeps the comment after ``node[key]``'s line: the slot and its index."""
    if isinstance(node, CommentedSeq):
        return node.ca.items.setdefault(key, [None, None]), 0
    return node.ca.items.setdefault(key, [None, None, None, None]), 2


def _comment_lines_after(value: object) -> str:
    """The comment lines ruamel hangs on ``value``'s last entry, after that entry's own line."""
    if not isinstance(value, (CommentedMap, CommentedSeq)) or not value:
        return ""
    key = _last_key(value)
    inner = value[key]
    if isinstance(inner, (CommentedMap, CommentedSeq)) and inner:
        return _comment_lines_after(inner)
    slot, at = _comment_slot(value, key)
    token = slot[at]
    if token is None:
        return ""
    _, newline, rest = str(token.value).partition("\n")
    return rest if newline else ""


def _hang_comment_lines(data: CommentedMap, key: Any, lines: str) -> None:
    """Hang comment ``lines`` after the last line of ``data[key]``."""
    node: CommentedMap | CommentedSeq = data
    while True:
        value = node[key]
        if not (
            isinstance(value, (CommentedMap, CommentedSeq))
            and value
            and not value.fa.flow_style()
        ):
            break
        node, key = value, _last_key(value)
    slot, at = _comment_slot(node, key)
    token = slot[at]
    if token is None:
        slot[at] = CommentToken("\n" + lines, CommentMark(0), None)
    else:
        token.value += lines if token.value.endswith("\n") else "\n" + lines


# ---------------------------------------------------------------------------
# Status report rendering helpers (CLI consumes these)
# ---------------------------------------------------------------------------


def format_iso(ts: str | None) -> str:
    """Render a stored ISO timestamp or ``"(never)"`` for ``None``."""
    return ts if ts else "(never)"


# ---------------------------------------------------------------------------
# Two-way sync (rclone bisync, ADR-030)
# ---------------------------------------------------------------------------

#: The oldest rclone whose ``bisync`` handles the fixed flags below correctly.
#: The conflict flags themselves are older, but before this release
#: ``--conflict-loser pathname`` with ``--conflict-resolve newer`` renamed the
#: losing copy without copying the winner over (rclone PR #9417): the paper on
#: the losing side was left with no ``metadata.yaml`` and rclone still exited 0.
BISYNC_MIN_VERSION: tuple[int, int, int] = (1, 74, 2)

#: The small file on the remote that tells a machine another one has synced
#: (auto sync). Excluded now, so that writing it later is not a filter change,
#: which would make every machine start over with ``--first-from``.
SYNC_STAMP_FILENAME = ".litman-sync-stamp.json"

#: Keeps the stamp out of one-way push / pull and of ``rclone size``. Not in
#: :data:`DEFAULT_EXCLUDES`: that list is part of the two-way filters text,
#: whose every change sends each machine back to ``--first-from``.
_STAMP_EXCLUDE_ARGS: tuple[str, ...] = ("--exclude", f"/{SYNC_STAMP_FILENAME}")

#: Root paths two-way sync never carries, on top of :data:`DEFAULT_EXCLUDES`.
#: INDEX.json is derived and changes on every write, so it would conflict on
#: every sync; lit-config.yaml holds this machine's absolute project paths;
#: a ``codes/*/repo/`` checkout is a git repository, which a file copy breaks
#: (symlinks and exec bits are lost). The ``.tmp`` files are litman's own
#: half-written root files, there for an instant before they replace the real
#: one.
_TWO_WAY_ROOT_EXCLUDES: tuple[str, ...] = (
    *DEFAULT_EXCLUDES,
    "INDEX.json",
    "lit-config.yaml",
    "codes/*/repo/**",
    SYNC_STAMP_FILENAME,
    "INDEX.json.tmp",
    "lit-config.yaml.tmp",
    f"{SYNC_STATE_FILENAME}.tmp",
)

#: Excluded at any depth. File managers drop the first four into every folder
#: they show, and each machine writes its own, so carrying them would only
#: report conflicts nobody made. ``*.partial`` is rclone's name for a file it
#: had not finished writing when it was stopped. The probes are the scratch
#: links and files litman makes for an instant to learn what a folder accepts;
#: on Windows the link probe is a folder, hence its ``/**`` form as well.
_TWO_WAY_ANYWHERE_EXCLUDES: tuple[str, ...] = (
    ".DS_Store",
    "._*",
    "Thumbs.db",
    "desktop.ini",
    "*.partial",
    ".litman-link-probe-*",
    ".litman-link-probe-*/**",
    ".litman-write-probe-*",
)

#: rclone's name for the losing copy of a conflict, with ``--conflict-suffix
#: sync-conflict`` and ``--conflict-loser pathname``: ``1`` is this machine's
#: copy (path1), ``2`` the cloud's (path2).
_CONFLICT_SUFFIX_RE = re.compile(r"\.sync-conflict(\d+)$")

_RCLONE_LOG_PREFIX_RE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")

#: The level that starts an rclone log record, right after the date and time.
_RCLONE_LEVEL_RE = re.compile(
    r"^(DEBUG|INFO|NOTICE|WARNING|ERROR|CRITICAL|ALERT|EMERGENCY)\s*:"
)

#: Records below NOTICE: bisync's per-file report and its progress chatter.
#: litman reads them and does not show them.
_RCLONE_REPORT_LEVELS = frozenset({"DEBUG", "INFO"})

#: What tells one of rclone's ``--stats-one-line`` lines: a size, a slash and a
#: size after NOTICE ("NOTICE:     2.505 MiB / 3.492 MiB, 72%, 102.023 KiB/s,
#: ETA 9s"). Loose, so that a stats line another rclone version words a little
#: differently is still known for one: never shown as rclone's, kept among a
#: run's lines or recorded. :data:`_RCLONE_STATS_RE` reads it.
_RCLONE_STATS_LOOSE_RE = re.compile(r"^NOTICE\s*:\s+\d[\d.]*\s*[A-Za-z]*\s*/\s*\d")

#: A stats line read: the bytes copied, of how many, and rclone's percentage
#: (``-`` while there is nothing to copy).
_RCLONE_STATS_RE = re.compile(
    r"^NOTICE\s*:\s+(?P<done>\d+(?:\.\d+)?) (?P<done_unit>[KMGTPE]i)?B"
    r" / (?P<total>\d+(?:\.\d+)?) (?P<total_unit>[KMGTPE]i)?B, (?P<percent>\d+%|-),"
)

_SIZE_UNITS = {
    "": 1, "Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40, "Pi": 1 << 50, "Ei": 1 << 60,
}

#: How rclone logs its stats when it logs them: on one line, at NOTICE.
_STATS_ARGS = ("--stats-one-line", "--stats-log-level", "NOTICE")

#: Environment variables that change what rclone prints, or where. litman
#: reads bisync's log, so its bisync runs go without them; rclone would even
#: refuse to start with ``RCLONE_VERBOSE`` or ``RCLONE_QUIET`` next to the
#: ``--log-level`` litman passes.
_RCLONE_LOG_ENV_NAMES = frozenset(
    {"RCLONE_VERBOSE", "RCLONE_QUIET", "RCLONE_USE_JSON_LOG", "RCLONE_COLOR"}
)
_RCLONE_LOG_ENV_PREFIXES = (
    "RCLONE_LOG_",
    "RCLONE_STATS",
    "RCLONE_PROGRESS",
    "RCLONE_SYSLOG",
)

_APP_NAME = "litman"

FirstFrom = Literal["cloud", "local"]
TwoWayMode = Literal["sync", "first-local", "first-cloud"]


def two_way_filter_rules() -> str:
    """The ``--filters-file`` text two-way sync hands to ``rclone bisync``.

    Root paths are anchored with ``/``, so a file of the same name deeper in
    the vault (an SI file called ``INDEX.json``) still syncs. rclone records a
    hash of this text at the first sync and refuses to go on once it changes,
    because a changed filter set needs a full resync. Keep the text stable:
    any edit makes every machine start over with ``--first-from``, which
    :func:`two_way_sync` asks the user for rather than crashing.
    """
    lines = ["# Written by litman for rclone bisync. Do not edit."]
    lines += [f"- /{pattern}" for pattern in _TWO_WAY_ROOT_EXCLUDES]
    lines += [f"- {pattern}" for pattern in _TWO_WAY_ANYWHERE_EXCLUDES]
    return "\n".join(lines) + "\n"


def _two_way_filter_args() -> list[str]:
    """:func:`two_way_filter_rules` as ``--filter`` flags, for rclone commands other than bisync."""
    args: list[str] = []
    for line in two_way_filter_rules().splitlines():
        if line and not line.startswith("#"):
            args += ["--filter", line]
    return args


def sync_data_dir() -> Path:
    """litman's per-user data dir, which holds the bisync state and the backups.

    platformdirs, like the registry: ``~/.local/share/litman`` on Linux,
    ``~/Library/Application Support/litman`` on macOS,
    ``%LOCALAPPDATA%\\litman\\litman`` on Windows. Outside every vault, which
    bisync requires of ``--backup-dir1`` (it must not overlap path1).
    """
    return Path(user_data_dir(_APP_NAME))


def vault_key(vault: Path, target_url: str) -> str:
    """The folder name that keys this vault's bisync state and backups.

    The vault folder's name, for a person browsing the data dir, plus a hash
    of the resolved vault path and the remote target. The target is in the
    hash because rclone's records belong to one (vault, remote) pair: after
    ``lit sync setup`` points the vault at another remote folder, the next
    ``lit sync`` is a first sync again, with its own safe first-run rules.
    """
    resolved = vault.resolve()
    seed = f"{os.path.normcase(str(resolved))}\n{target_url}"
    digest = hashlib.sha256(seed.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-.")[:40]
    return f"{name or 'vault'}-{digest}"


#: litman's record of the synced files after the last sync, in the workdir.
SNAPSHOT_NAME = "litman-snapshot.json"


@dataclass(frozen=True)
class BisyncPaths:
    """Where one vault's two-way sync keeps its state and its backups."""

    workdir: Path
    local_backup_root: Path
    remote_backup_root: str

    @property
    def filters_file(self) -> Path:
        return self.workdir / "filters.txt"

    @property
    def snapshot_file(self) -> Path:
        """litman's record of the vault's synced files after the last sync."""
        return self.workdir / SNAPSHOT_NAME

    @property
    def conflicts_root(self) -> Path:
        """Where the older copy of every conflict goes, one folder per sync."""
        return self.local_backup_root / "conflicts"

    @property
    def repo_heads_file(self) -> Path:
        """The commit each code checkout was at when this machine's last sync ended."""
        return self.workdir / "repo-heads.json"

    @property
    def stamp_file(self) -> Path:
        """Which remote stamp this machine last saw and last wrote (:class:`StampRecord`)."""
        return self.workdir / STAMP_RECORD_NAME


def remote_backup_root(target_url: str) -> str:
    """``<remote>:<path>-litman-backup``: the cloud-side backup folder.

    A sibling of the synced folder, so it does not overlap path2 (bisync
    requires that). Syncing the remote's top level would leave no room for a
    sibling, so it is refused.

    Raises:
        SyncError: ``target_url`` names no folder inside the remote.
    """
    remote, sep, path = target_url.partition(":")
    trimmed = path.rstrip("/\\")
    if not sep or not trimmed:
        raise SyncError(
            "Two-way sync needs a folder inside the remote, not its top level. "
            "Set one: `lit sync setup --remote NAME --path litman-vault/`."
        )
    return f"{remote}:{trimmed}-litman-backup"


def sync_workdir(vault: Path, target_url: str) -> Path:
    """The folder of rclone's records for ``vault`` synced with ``target_url``.

    It also holds litman's own: the snapshot of the last sync, the stamp it
    last saw, and the sync locks (:mod:`litman.core.sync_lock`).
    """
    return sync_data_dir() / "bisync" / vault_key(vault, target_url)


def bisync_paths(vault: Path, target_url: str) -> BisyncPaths:
    """The state and backup locations for ``vault`` synced with ``target_url``."""
    key = vault_key(vault, target_url)
    data = sync_data_dir()
    return BisyncPaths(
        workdir=sync_workdir(vault, target_url),
        local_backup_root=data / "sync-backup" / key,
        remote_backup_root=remote_backup_root(target_url),
    )


def bisync_initialised(workdir: Path) -> bool:
    """Whether this machine has synced this (vault, remote) pair before.

    True once rclone has written its listings: the current ones (``.lst``),
    the copy it keeps of the last good ones (``.lst-old``) or the ones it set
    aside after an abort (``.lst-err``). ``.lst-new`` alone is left by a first
    sync that never finished, which is still a first sync.
    """
    try:
        names = os.listdir(workdir)
    except OSError:
        return False
    return any(
        name.endswith((".path1.lst", ".path1.lst-old", ".path1.lst-err"))
        for name in names
    )


# -- the sync locks ----------------------------------------------------------

#: The lock held for a whole sync, and the one held while rclone runs
#: (:mod:`litman.core.sync_lock`), both in the workdir.
SYNC_LOCK_NAME = "sync.lock"
PASS_LOCK_NAME = "pass.lock"

#: How long ``lit sync`` waits for another sync of the same library to end.
#: Under two minutes: an agent's shell tool often stops a command at 120 s,
#: and the agent should see the exit code and its message, not a killed run.
SYNC_LOCK_WAIT = 90.0

#: How long a sync waits for the GUI to finish a save before rclone runs.
PASS_LOCK_WAIT = 60.0


def _busy_message(holder: LockHolder | None) -> str:
    who = holder.describe() if holder is not None else ""
    return (
        f"Another sync of this library is running{f' ({who})' if who else ''}, so "
        "this one did not start. Try again in a minute."
    )


def describe_sync_lock_holder(holder: LockHolder | None) -> str:
    """``Another sync of this library is running (lit gui, since 14:02)``."""
    who = holder.describe() if holder is not None else ""
    return f"Another sync of this library is running{f' ({who})' if who else ''}"


@contextmanager
def hold_sync_lock(
    workdir: Path,
    *,
    by: str,
    wait: float,
    on_wait: Callable[[LockHolder | None], None] | None = None,
) -> Iterator[None]:
    """Hold ``sync.lock`` for one sync, waiting up to ``wait`` seconds for it.

    ``by`` names the holder in the line others read (``lit sync``,
    ``lit gui``). ``on_wait`` hears the current holder once, when the wait
    starts.

    Raises:
        SyncBusyError: the lock was still held after ``wait`` seconds.
    """
    lock = FileLock(workdir / SYNC_LOCK_NAME)

    def waiting() -> None:
        if on_wait is not None:
            on_wait(read_holder(lock.path))

    if not lock.acquire(wait, on_wait=waiting):
        raise SyncBusyError(_busy_message(read_holder(lock.path)))
    try:
        lock.write_holder(by)
        yield
    finally:
        lock.release()


@contextmanager
def hold_pass_lock(workdir: Path) -> Iterator[None]:
    """Hold ``pass.lock`` while rclone runs, after the GUI's saves in progress end.

    Raises:
        SyncBusyError: a GUI save still held it after :data:`PASS_LOCK_WAIT`.
    """
    lock = FileLock(workdir / PASS_LOCK_NAME)
    if not lock.acquire(PASS_LOCK_WAIT):
        raise SyncBusyError(
            "The GUI is still saving to this library, so nothing was synced. "
            "Try again in a minute."
        )
    try:
        yield
    finally:
        lock.release()


def pass_lock_path(vault: Path, target_url: str) -> Path:
    """Where ``pass.lock`` of ``vault`` synced with ``target_url`` lives."""
    return sync_workdir(vault, target_url) / PASS_LOCK_NAME


def require_still_set_up(vault: Path, target_url: str, done: str = "synced") -> None:
    """Refuse to go on once ``vault`` no longer syncs with ``target_url``.

    The first thing a sync does once it holds the sync lock: one that read
    the configuration, then waited while ``lit sync disconnect`` removed
    ``sync:`` or ``lit sync setup`` pointed the library elsewhere, stops here
    before it touches anything. ``done`` is what did not happen (``synced``,
    ``pushed``, ...).

    Raises:
        SyncNotSetUpError: ``sync:`` is gone or names another folder.
    """
    from litman.core.config import load_config

    configured = load_config(vault).sync
    if configured is None or configured.target_url() != target_url:
        raise SyncNotSetUpError(
            f"This library is no longer set up to sync with {target_url}, so "
            f"nothing was {done}."
        )


# -- stopping sync on this machine ----------------------------------------------


@dataclass(frozen=True)
class Disconnected:
    """What :func:`disconnect_sync` left in place, for the user to remove if they want."""

    target_url: str
    remote: str
    #: The cloud folder two-way sync keeps its backups in, which rclone makes
    #: only once a sync has backed something up; ``None`` for a library only
    #: pushed or pulled here, or kept at the remote's top, where none is made.
    cloud_backup: str | None
    #: This machine's sync backups; ``None`` when there are none.
    local_backup: Path | None


def disconnect_sync(
    vault: Path,
    *,
    lock_wait: float = SYNC_LOCK_WAIT,
    on_lock_wait: Callable[[LockHolder | None], None] | None = None,
) -> Disconnected | None:
    """Stop syncing ``vault`` on this machine: what setting it up and syncing left here goes.

    This machine's records of the sync (:func:`sync_workdir`) go first,
    rclone's listings of the last sync before anything else; then the
    sync-state file; the ``sync:`` block of ``lit-config.yaml`` last. Cut off
    anywhere before that, the library is still set up and has no records of
    a sync, so its next sync is a first one, as is the first after a later
    ``lit sync setup``. The library, the backups, the cloud folders, the
    rclone remote and the cloud account's grant stay. Only the configuration
    says what to remove: no rclone, no network.

    Holds the sync lock like a sync, waiting up to ``lock_wait`` seconds for
    one to end (``on_lock_wait`` hears the holder when the wait starts); a
    sync that read the configuration before and takes the lock after stops
    at :func:`require_still_set_up`. The lock files go once the lock is let
    go, where nothing holds one (Windows does not delete an open file): a
    folder left with them alone holds no records. Where the file system
    refuses locks no sync can run, so none is waited for.

    Returns None when ``vault`` has no ``sync:``, and touches nothing.

    Raises:
        SyncBusyError: another sync still held the lock after ``lock_wait``.
        SyncNotSetUpError: ``lit sync setup`` pointed the library elsewhere
            while this waited; nothing was changed.
        ConfigError: ``lit-config.yaml`` could not be read.
        OSError: a record could not be removed; ``sync:`` is still there.
    """
    from litman.core.config import load_config

    configured = load_config(vault).sync
    if configured is None:
        return None
    target_url = configured.target_url()
    workdir = sync_workdir(vault, target_url)
    one_way = local_sync_mode(vault, target_url) == "one-way"
    with ExitStack() as held:
        try:
            held.enter_context(
                hold_sync_lock(
                    workdir, by="lit sync disconnect", wait=lock_wait, on_wait=on_lock_wait
                )
            )
        except SyncBusyError:
            holder = read_holder(workdir / SYNC_LOCK_NAME)
            raise SyncBusyError(
                f"{describe_sync_lock_holder(holder)}, so this computer is still "
                "connected. Try again once it is done."
            ) from None
        except SyncLockError:
            pass
        now = load_config(vault).sync
        if now is not None and now.target_url() != target_url:
            raise SyncNotSetUpError(
                f"This library is no longer set up to sync with {target_url}, so "
                "nothing was changed. Run `lit sync disconnect` again."
            )
        if now is not None:
            _remove_records(workdir)
            for name in (SYNC_STATE_FILENAME, SYNC_STATE_FILENAME + ".tmp"):
                remove_file(vault / name)
            remove_sync_from_config(vault / CONFIG_FILENAME)
    rmtree(workdir, ignore_errors=True)
    if now is None:
        # Another disconnect went first while this one waited.
        return None
    try:
        cloud_backup = None if one_way else remote_backup_root(target_url)
    except SyncError:
        cloud_backup = None
    local_backup = sync_data_dir() / "sync-backup" / vault_key(vault, target_url)
    return Disconnected(
        target_url=target_url,
        remote=configured.remote,
        cloud_backup=cloud_backup,
        local_backup=local_backup if local_backup.is_dir() else None,
    )


def _remove_records(workdir: Path) -> None:
    """Remove everything in ``workdir`` but the two lock files, rclone's path1 listings first.

    Whatever stops it part-way, the folder no longer reads as synced before
    (:func:`bisync_initialised`).
    """
    try:
        names = os.listdir(workdir)
    except FileNotFoundError:
        return
    names = sorted(
        (name for name in names if name not in (SYNC_LOCK_NAME, PASS_LOCK_NAME)),
        key=lambda name: ".path1.lst" not in name,
    )
    for name in names:
        path = workdir / name
        if path.is_dir() and not path.is_symlink():
            rmtree(path)
        else:
            remove_file(path)


# -- the auto-sync stamp -----------------------------------------------------

#: The workdir file that records the stamps this machine saw and wrote.
STAMP_RECORD_NAME = "stamp.json"

#: rclone's network limits for a stamp read or write, and the run's own cap:
#: the stamp is one tiny file, so a provider that does not answer is offline.
_STAMP_NET_FLAGS: tuple[str, ...] = ("--contimeout", "15s", "--timeout", "30s")
_STAMP_RUN_TIMEOUT = 60.0


@dataclass(frozen=True)
class Stamp:
    """The stamp on the remote: written after every sync that changed the cloud.

    ``prev`` is the id the writer read before its sync. A machine whose own
    stamp is neither the current one nor its ``prev`` knows it was replaced by
    one that did not see it, and writes a new one after its next sync.
    """

    id: str
    prev: str | None = None
    at: str | None = None
    machine: str | None = None


@dataclass(frozen=True)
class StampRecord:
    """What this machine knows of the stamp (``<workdir>/stamp.json``).

    ``seen``: the stamp this machine's last sync started from, or wrote.
    ``written``: the last stamp this machine wrote, until a later stamp names
    it as ``prev`` (another machine saw it). ``owed``: a stamp is still to be
    written, because the write failed or this machine's stamp was replaced
    unseen. ``pending``: a sync started and has not reached its stamp yet; left
    set by one that was killed or failed, whose uploads are then owed a stamp.
    """

    seen: str | None = None
    written: str | None = None
    owed: bool = False
    pending: bool = False


def stamp_url(target_url: str) -> str | None:
    """The stamp's rclone path; ``None`` for a remote's top level, which two-way sync refuses."""
    remote, sep, path = target_url.partition(":")
    trimmed = path.rstrip("/\\")
    if not sep or not trimmed:
        return None
    return f"{remote}:{trimmed}/{SYNC_STAMP_FILENAME}"


def read_remote_stamp(target_url: str) -> Stamp | None:
    """The stamp on the remote; ``None`` when there is none.

    Raises:
        SyncError: rclone could not read it (offline, sign-in, provider).
    """
    url = stamp_url(target_url)
    if url is None:
        return None
    result = run_rclone(
        ["cat", url, *_STAMP_NET_FLAGS], check=False, timeout=_STAMP_RUN_TIMEOUT
    )
    if result.returncode in (3, 4):
        return None
    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").strip().splitlines()[-3:])
        raise SyncError(
            f"rclone could not read the sync stamp in {target_url} "
            f"(exit {result.returncode}):\n{tail}"
        )
    text = result.stdout or ""
    try:
        payload = json.loads(text)
        stamp_id = payload["id"]
        if not isinstance(stamp_id, str) or not stamp_id:
            raise ValueError(stamp_id)
    except (ValueError, KeyError, TypeError):
        # Damaged: still a state of its own, which a sync replaces.
        digest = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()
        return Stamp(id=f"{_UNREADABLE_STAMP}{digest[:16]}")

    def text_or_none(key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) else None

    return Stamp(
        id=stamp_id,
        prev=text_or_none("prev"),
        at=text_or_none("at"),
        machine=text_or_none("machine"),
    )


def write_remote_stamp(target_url: str, *, prev: str | None) -> Stamp:
    """Write a new stamp naming ``prev``, and return it.

    Raises:
        SyncError: rclone could not write it.
    """
    url = stamp_url(target_url)
    if url is None:
        raise SyncError(f"{target_url} is a remote's top level, which holds no sync stamp.")
    stamp = Stamp(id=uuid.uuid4().hex, prev=prev, at=now_iso(), machine=socket.gethostname())
    payload = json.dumps(
        {"id": stamp.id, "prev": stamp.prev, "at": stamp.at, "machine": stamp.machine}
    )
    result = run_rclone(
        ["rcat", url, *_STAMP_NET_FLAGS],
        check=False,
        timeout=_STAMP_RUN_TIMEOUT,
        input=payload + "\n",
    )
    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").strip().splitlines()[-3:])
        raise SyncError(
            f"rclone could not write the sync stamp in {target_url} "
            f"(exit {result.returncode}):\n{tail}"
        )
    return stamp


def read_stamp_record(workdir: Path) -> StampRecord:
    """This machine's :class:`StampRecord`; an empty one when there is none to read."""
    try:
        payload = json.loads((workdir / STAMP_RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return StampRecord()
    if not isinstance(payload, dict):
        return StampRecord()
    seen = payload.get("seen")
    written = payload.get("written")
    return StampRecord(
        seen=seen if isinstance(seen, str) else None,
        written=written if isinstance(written, str) else None,
        owed=payload.get("owed") is True,
        pending=payload.get("pending") is True,
    )


def write_stamp_record(workdir: Path, record: StampRecord) -> None:
    path = workdir / STAMP_RECORD_NAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "seen": record.seen,
                "written": record.written,
                "owed": record.owed,
                "pending": record.pending,
            }
        ),
        encoding="utf-8",
    )
    replace_file(tmp, path)


#: The id given to a stamp that cannot be read (see :func:`read_remote_stamp`).
_UNREADABLE_STAMP = "unreadable-"


def stamp_overwritten(record: StampRecord, current: Stamp | None) -> bool:
    """Whether this machine's last stamp was replaced by one that did not see it.

    Then the machine that replaced it may have missed what this one uploaded,
    so this one writes a new stamp after its next sync. A stamp that cannot be
    read (damaged, or two files of one name, which Google Drive allows, read as
    one) tells nothing of who wrote last: it reads as moved when it changes,
    never as replacing this machine's stamp, which would keep both machines
    writing and syncing.
    """
    if record.written is None:
        return False
    if current is None:
        return True
    if current.id.startswith(_UNREADABLE_STAMP):
        return False
    return record.written not in (current.id, current.prev)


def _acknowledged(record: StampRecord, current: Stamp | None) -> StampRecord:
    """Forget ``written`` once a later stamp names it as ``prev``: another machine saw it."""
    if record.written is not None and current is not None and current.prev == record.written:
        return replace(record, written=None)
    return record


@dataclass(frozen=True)
class StampCheck:
    """What a look at the remote stamp found: whether to sync, and whether a stamp is owed."""

    moved: bool
    owed: bool


def check_remote_stamp(workdir: Path, target_url: str) -> StampCheck:
    """Compare the remote stamp with this machine's record: one small read.

    ``moved`` when another machine wrote a stamp since this one's last sync,
    or replaced this one's stamp without seeing it.

    Raises:
        SyncError: rclone could not read the stamp.
    """
    record = read_stamp_record(workdir)
    current = read_remote_stamp(target_url)
    current_id = current.id if current is not None else None
    moved = current_id != record.seen or stamp_overwritten(record, current)
    return StampCheck(moved=moved, owed=record.owed or record.pending)


class StampRound:
    """One sync's dealings with the stamp: read it before the sync, write it after.

    :func:`two_way_sync` goes through it, so a sync from the terminal and the
    GUI's auto sync signal each other. A cloud folder it finds without a stamp
    gets one, even when the sync changed nothing there: the stamp also marks
    the folder as synced two-way, which :func:`require_one_way` reads.
    """

    def __init__(
        self,
        workdir: Path,
        target_url: str,
        record: StampRecord,
        pre: Stamp | None,
        *,
        pre_known: bool,
        overwritten: bool,
        active: bool,
    ) -> None:
        self.workdir = workdir
        self.target_url = target_url
        self.record = record
        self.pre = pre
        self.pre_known = pre_known
        self.overwritten = overwritten
        self.active = active

    @classmethod
    def begin(cls, workdir: Path, target_url: str, *, mark: bool = True) -> StampRound:
        """Read the stamp before a sync. A failed read is not an error here: the sync says.

        With ``mark``, the record on disk says a sync started (``pending``), so
        that one killed before :meth:`finish` still leaves its stamp owed.
        """
        record = read_stamp_record(workdir)
        if stamp_url(target_url) is None:
            return cls(
                workdir, target_url, record, None,
                pre_known=False, overwritten=False, active=False,
            )
        if mark:
            _write_record_quietly(workdir, replace(record, pending=True))
        try:
            pre = read_remote_stamp(target_url)
        except SyncError:
            return cls(
                workdir, target_url, record, None,
                pre_known=False, overwritten=False, active=True,
            )
        return cls(
            workdir, target_url, _acknowledged(record, pre), pre,
            pre_known=True, overwritten=stamp_overwritten(record, pre), active=True,
        )

    def _seen_after(self) -> str | None:
        # The stamp read BEFORE the sync, never one read after: a stamp written
        # while this sync ran may stand for changes its listing missed.
        if self.pre_known:
            return self.pre.id if self.pre is not None else None
        return self.record.seen

    @property
    def moved(self) -> bool:
        """The stamp read at :meth:`begin` is not the one this machine last synced from."""
        if not self.pre_known:
            return False
        current = self.pre.id if self.pre is not None else None
        return current != self.record.seen or self.overwritten

    def mark_pending(self) -> None:
        """Before one more pass after :meth:`finish`: if it is cut off, a stamp is owed."""
        if self.active:
            _write_record_quietly(self.workdir, replace(self.record, pending=True))

    def finish(self, *, remote_changed: bool, may_write: bool = True) -> bool:
        """Write a new stamp when the remote changed (or one is owed); record what was seen.

        ``may_write`` False defers an owed write (a sync cut short at exit) to
        the next sync or auto-sync check. Returns whether a stamp was written.
        It may be called again after another pass: a stamp it wrote is then
        the one the next is written after.
        """
        if not self.active:
            return False
        unmarked = self.pre_known and self.pre is None
        owe = (
            remote_changed
            or self.record.owed
            or self.record.pending
            or self.overwritten
            or unmarked
        )
        written = False
        record = self.record
        if owe and may_write:
            prev = self.pre.id if self.pre_known and self.pre is not None else None
            try:
                new = write_remote_stamp(self.target_url, prev=prev)
            except SyncError:
                record = replace(record, seen=self._seen_after(), owed=True, pending=False)
            else:
                record = StampRecord(seen=new.id, written=new.id)
                written = True
                self.pre, self.pre_known, self.overwritten = new, True, False
        else:
            record = replace(record, seen=self._seen_after(), owed=owe, pending=False)
        self.record = record
        _write_record_quietly(self.workdir, record)
        return written


def _write_record_quietly(workdir: Path, record: StampRecord) -> None:
    try:
        write_stamp_record(workdir, record)
    except OSError:
        pass


def write_owed_stamp(workdir: Path, target_url: str) -> bool:
    """Write the stamp an earlier sync owes, when nothing else is to sync.

    ``False``, with nothing written, when the stamp moved since this machine's
    last sync: a stamp written now would name that one as seen, though its
    changes never came down here. The caller syncs instead, and that sync
    writes the owed stamp. ``True`` when the stamp cannot be read: it stays
    owed, and the next look tries again.
    """
    stamps = StampRound.begin(workdir, target_url, mark=False)
    if not stamps.pre_known:
        return True
    if stamps.moved:
        return False
    stamps.finish(remote_changed=False)
    return True


def rclone_version() -> tuple[int, int, int] | None:
    """The installed rclone's version, or ``None`` if it cannot be read.

    Goes through :func:`run_rclone`, so a missing rclone raises its usual
    install hint and tests can fake the version there.
    """
    result = run_rclone(["version"], check=False, timeout=_GATE_TIMEOUT)
    match = re.search(r"rclone v(\d+)\.(\d+)\.(\d+)", result.stdout or "")
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def _format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def require_bisync_rclone() -> tuple[int, int, int]:
    """Check that the installed rclone can run litman's two-way sync.

    Raises:
        SyncError: rclone is missing, its version cannot be read, or it is
            older than :data:`BISYNC_MIN_VERSION`.
    """
    found = rclone_version()
    needed = _format_version(BISYNC_MIN_VERSION)
    if found is None:
        raise SyncError(
            f"Could not read the installed rclone's version; two-way sync needs "
            f"rclone {needed} or newer. Check it with `rclone version`."
        )
    if found < BISYNC_MIN_VERSION:
        raise SyncError(
            f"rclone {_format_version(found)} is too old for two-way sync, which "
            f"needs {needed} or newer. Upgrade with `rclone selfupdate`, or see "
            "https://rclone.org/install/."
        )
    return found


# -- the names of rclone's records --------------------------------------------

#: The longest name a folder entry may have: 255 bytes on Linux and macOS,
#: 255 UTF-16 units on Windows.
_NAME_MAX = 255

#: The longest ending rclone bisync puts after the session name of a record
#: file; ``.path1.lst-old`` and ``.path1.lst-err`` are as long.
_LONGEST_RECORD_SUFFIX = ".path1.lst-new"

#: What rclone turns into ``_`` in a session name: Go's ``\s``, which is ASCII
#: whitespace only (Python's is not), and ``\ / : ? *``.
_SESSION_NAME_UNSAFE_RE = re.compile(r"[\t\n\f\r \\/:?*]")


def _session_name_half(name: str, root: str, *, windows: bool) -> str:
    """One side of the session name: rclone's ``FsPath``, ``CanonicalPath``, ``StripHexString``."""
    if name == "local":
        path = root.replace("/", "\\").removeprefix("\\\\?\\") if windows else root
    else:
        path = f"{name}:{root}"
    half = _SESSION_NAME_UNSAFE_RE.sub("_", path.strip("\\/"))
    # A remote whose options were set in its name carries a {hash} rclone drops.
    opening, closing = half.find("{"), half.find("}")
    if opening >= 0 and closing > opening:
        half = half[:opening] + half[closing + 1 :]
    return half


def bisync_session_name(
    path1: tuple[str, str], path2: tuple[str, str], *, windows: bool
) -> str:
    """The name rclone bisync gives the records of a sync between ``path1`` and ``path2``.

    Each path is the ``(name, root)`` of its rclone file system, as ``rclone
    backend features`` reports them: ``local`` and the folder for a plain
    path; for a remote, its name and the folder in it (for an alias, those of
    the file system it points at). rclone's rules: on Windows a plain path
    loses its ``\\\\?\\`` and takes backslashes, a remote is written
    ``name:root``, each side is trimmed of slashes at both ends, and spaces
    and ``\\ / : ? *`` become ``_``.
    """
    return (
        _session_name_half(*path1, windows=windows)
        + ".."
        + _session_name_half(*path2, windows=windows)
    )


def name_length(name: str, *, windows: bool) -> int:
    """How long ``name`` is as a folder entry: UTF-16 units on Windows, UTF-8 bytes elsewhere."""
    if windows:
        return len(name.encode("utf-16-le", "surrogatepass")) // 2
    return len(name.encode("utf-8", "surrogatepass"))


def _rclone_fs_info(remote: str) -> tuple[str, str] | None:
    """``(name, root)`` of the rclone file system ``remote`` names; ``None`` if rclone cannot tell.

    ``rclone backend features`` builds the file system as ``rclone bisync``
    does. For a cloud remote that may take the network.
    """
    try:
        result = subprocess.run(
            [RCLONE_BIN, "backend", "features", remote],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_bisync_env(),
            timeout=60,
            check=False,
        )
        info = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    name, root = info.get("Name"), info.get("Root")
    if not isinstance(name, str) or not isinstance(root, str):
        return None
    return name, root


def _cloud_holds_library(target_url: str) -> bool | None:
    """Whether the cloud folder holds a library; ``None`` if rclone cannot tell.

    Runs :func:`remote_holds_files` with the filter rules in a scratch file:
    the sync's state folder is made only once the names are known to fit.
    """
    with tempfile.TemporaryDirectory(prefix="litman-sync-") as scratch:
        filters_file = Path(scratch) / "filters.txt"
        filters_file.write_bytes(two_way_filter_rules().encode("utf-8"))
        try:
            return remote_holds_files(target_url, filters_file)
        except SyncError:
            return None


def require_bisync_names_fit(vault: Path, target_url: str) -> None:
    """Refuse, before rclone runs, a two-way sync whose record names would be too long.

    rclone bisync names its record files after both synced paths
    (:func:`bisync_session_name`). A name the file system refuses stops
    rclone partway, possibly after it copied the library across but before
    it saved its records; every later run then stops the same way. Skipped
    when rclone cannot report a path (a cloud remote while offline, say).

    Raises:
        SyncError: the names would be too long. The message says by how much
            and names the longer of the two paths to shorten, but never a
            cloud folder that already holds a library: other machines may
            sync with it.
    """
    local = _rclone_fs_info(str(vault))
    cloud = _rclone_fs_info(target_url) if local is not None else None
    if local is None or cloud is None:
        return
    windows = sys.platform == "win32"
    session = bisync_session_name(local, cloud, windows=windows)
    over = name_length(session, windows=windows) + len(_LONGEST_RECORD_SUFFIX) - _NAME_MAX
    if over <= 0:
        return
    verdict = (
        f"This library path plus the cloud path is {over} "
        f"character{'' if over == 1 else 's'} too long for rclone."
    )
    library_part, cloud_part = (
        name_length(_session_name_half(*side, windows=windows), windows=windows)
        for side in (local, cloud)
    )
    # When the cloud folder cannot be listed, the lengths decide alone.
    if cloud_part > library_part and _cloud_holds_library(target_url) is not True:
        raise SyncError(f"{verdict} Pick a shorter cloud path: `lit sync setup`.")
    raise SyncError(f"{verdict} Move the library, then run `lit vault set-path`.")


# -- what is synced, seen from the local side --------------------------------


_EXCLUDED_DIR_RES: tuple[re.Pattern[str], ...] = tuple(
    _rclone_glob_to_regex(p[: -len("/**")])
    for p in _TWO_WAY_ROOT_EXCLUDES
    if p.endswith("/**")
)
_EXCLUDED_FILE_RES: tuple[re.Pattern[str], ...] = tuple(
    _rclone_glob_to_regex(p)
    for p in _TWO_WAY_ROOT_EXCLUDES
    if not p.endswith("/**")
)


_ANYWHERE_DIR_PATTERNS: tuple[str, ...] = tuple(
    p[: -len("/**")] for p in _TWO_WAY_ANYWHERE_EXCLUDES if p.endswith("/**")
)
_ANYWHERE_FILE_PATTERNS: tuple[str, ...] = tuple(
    p for p in _TWO_WAY_ANYWHERE_EXCLUDES if not p.endswith("/**")
)


def _two_way_dir_excluded(rel: str, name: str) -> bool:
    if any(fnmatch.fnmatchcase(name, p) for p in _ANYWHERE_DIR_PATTERNS):
        return True
    return any(rx.match(rel) for rx in _EXCLUDED_DIR_RES)


def _two_way_file_excluded(rel: str, name: str) -> bool:
    if any(fnmatch.fnmatchcase(name, p) for p in _ANYWHERE_FILE_PATTERNS):
        return True
    return any(rx.match(rel) for rx in _EXCLUDED_FILE_RES)


def _walk_synced(vault: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(relative POSIX path, absolute path)`` of every file two-way sync carries.

    Applies the same rules as :func:`two_way_filter_rules`. Links are not
    followed and not yielded: rclone skips them too.
    """
    root = str(vault)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        kept = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or os.path.isjunction(full):
                continue
            if _two_way_dir_excluded(prefix + name, name):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            rel = prefix + name
            if _two_way_file_excluded(rel, name):
                continue
            yield rel, os.path.join(dirpath, name)


Snapshot = dict[str, tuple[int, int]]


def local_snapshot(vault: Path) -> Snapshot:
    """``{relative path: (size, mtime_ns)}`` for every synced file, conflict copies aside.

    What bisync compares (size and modtime), from a stat walk: no file is
    opened. Two snapshots tell what a sync changed on this machine.
    """
    out: Snapshot = {}
    for rel, full in _walk_synced(vault):
        if _CONFLICT_SUFFIX_RE.search(rel):
            continue
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        out[rel] = (st.st_size, st.st_mtime_ns)
    return out


@dataclass(frozen=True)
class SavedSnapshot:
    """The record a sync leaves of the library's synced files (``litman-snapshot.json``).

    ``files`` is what the sync left, except that a file changed here while
    it ran, and not by the sync, keeps the state it had before: it still
    reads as a change to take up. ``recheck`` says such files could not be
    told from the ones the sync brought down, so the next look must sync.
    """

    files: Snapshot
    recheck: bool = False


def read_saved_snapshot(path: Path) -> SavedSnapshot | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        files = payload["files"]
        return SavedSnapshot(
            files={str(rel): (int(v[0]), int(v[1])) for rel, v in files.items()},
            recheck=payload.get("recheck") is True,
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None


def _read_snapshot(path: Path) -> Snapshot | None:
    saved = read_saved_snapshot(path)
    return saved.files if saved is not None else None


def _write_snapshot(path: Path, snapshot: Snapshot, *, recheck: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload: dict[str, Any] = {"files": {rel: list(v) for rel, v in sorted(snapshot.items())}}
    if recheck:
        payload["recheck"] = True
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    replace_file(tmp, path)


def local_changes_pending(vault: Path, target_url: str) -> bool:
    """Whether a synced file here changed since the last sync (the exact comparison).

    True with no record of a last sync to compare with, and when that sync
    asked for a second look.
    """
    saved = read_saved_snapshot(sync_workdir(vault.resolve(), target_url) / SNAPSHOT_NAME)
    if saved is None or saved.recheck:
        return True
    return diff_snapshots(saved.files, local_snapshot(vault)).total > 0


@dataclass(frozen=True)
class FileChanges:
    """Files added, changed and deleted between two snapshots (relative paths)."""

    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.added) + len(self.changed) + len(self.deleted)


def diff_snapshots(
    old: Snapshot, new: Snapshot, *, ignore: frozenset[str] = frozenset()
) -> FileChanges:
    """What changed from ``old`` to ``new``, leaving out the paths in ``ignore``."""
    return FileChanges(
        added=tuple(sorted(r for r in new.keys() - old.keys() if r not in ignore)),
        changed=tuple(
            sorted(
                r
                for r in new.keys() & old.keys()
                if new[r] != old[r] and r not in ignore
            )
        ),
        deleted=tuple(sorted(r for r in old.keys() - new.keys() if r not in ignore)),
    )


# -- first sync --------------------------------------------------------------


def remote_holds_files(target_url: str, filters_file: Path) -> bool:
    """Whether the remote folder holds any file two-way sync would carry.

    A missing folder holds none. Empty folders do not count.

    Raises:
        SyncError: rclone could not list the remote (auth, network, quota).
    """
    result = run_rclone(
        [
            "lsf", "-R", "--files-only",
            "--filter-from", str(filters_file),
            target_url,
        ],
        check=False,
    )
    if result.returncode in (3, 4):
        return False
    if result.returncode != 0:
        stderr_tail = "\n".join((result.stderr or "").strip().splitlines()[-5:])
        raise SyncError(
            f"rclone could not list {target_url} (exit {result.returncode}), so "
            f"nothing was synced. Check the remote with `rclone lsd {target_url}`:\n"
            f"{stderr_tail}"
        )
    return any(line.strip() for line in (result.stdout or "").splitlines())


def local_vault_is_fresh(vault: Path) -> bool:
    """A vault with no paper and nothing under ``codes/``, as ``lit init`` leaves it.

    File-manager droppings (``.DS_Store`` and the like) do not count.
    """
    for sub in ("papers", "codes"):
        try:
            with os.scandir(vault / sub) as entries:
                if any(not is_file_manager_dropping(e.name) for e in entries):
                    return False
        except (FileNotFoundError, NotADirectoryError):
            continue
    return True


# -- conflicts ---------------------------------------------------------------


@dataclass(frozen=True)
class SyncConflict:
    """One file both machines changed since the last sync.

    ``kept`` says whose copy is now at ``path`` (``"local"`` = this
    machine's, ``"cloud"`` = the one that came from the cloud); the other copy
    was moved to ``backup``. ``restored`` is True when rclone could not tell
    which copy was newer and kept neither under the original name: litman put
    the newer one back so a paper never loses its ``metadata.yaml``.
    """

    path: str
    paper_id: str | None
    kept: Literal["local", "cloud"]
    kept_mtime: float | None
    moved_mtime: float
    backup: Path
    restored: bool = False


def _side_of(suffix_digit: str) -> Literal["local", "cloud"]:
    return "local" if suffix_digit == "1" else "cloud"


def _other_side(side: Literal["local", "cloud"]) -> Literal["local", "cloud"]:
    return "cloud" if side == "local" else "local"


def _move_out(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst``, across filesystems if need be, never over a file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    stem, n = dst.name, 2
    while dst.exists():
        dst = dst.with_name(f"{stem}.{n}")
        n += 1
    try:
        replace_file(src, dst)
    except OSError:
        # Another filesystem (the data dir on another drive): copy, then delete.
        shutil.copy2(src, dst)
        os.unlink(src)


def sweep_conflicts(vault: Path, dest_root: Path) -> list[SyncConflict]:
    """Move every ``*.sync-conflict<N>`` copy out of the vault into ``dest_root``.

    The relative path is kept under ``dest_root``. When rclone left no file
    under the original name (it could not pick a winner), the newest copy is
    renamed back to it first, so the paper keeps its ``metadata.yaml``.

    Raises:
        SyncError: a copy could not be moved out (it is left in place, and the
            next ``lit sync`` tries again).
    """
    groups: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for rel, full in _walk_synced(vault):
        match = _CONFLICT_SUFFIX_RE.search(rel)
        if match is None:
            continue
        original_rel = rel[: match.start()]
        groups.setdefault((original_rel, full[: len(full) - len(match.group(0))]), []).append(
            (rel, full, match.group(1))
        )

    rows: list[SyncConflict] = []
    for (original_rel, original_full), copies in sorted(groups.items()):
        original = Path(original_full)
        dated = []
        for rel, full, digit in copies:
            try:
                dated.append((os.stat(full).st_mtime, rel, full, digit))
            except OSError:
                continue
        dated.sort(reverse=True)
        restored = False
        kept_side: Literal["local", "cloud"] | None = None
        if not original.exists() and dated:
            _mtime, _rel, full, digit = dated.pop(0)
            replace_file(full, original)
            restored = True
            kept_side = _side_of(digit)
        try:
            kept_mtime: float | None = os.stat(original).st_mtime
        except OSError:
            kept_mtime = None
        parts = original_rel.split("/")
        paper_id = parts[1] if len(parts) >= 3 and parts[0] == "papers" else None
        for mtime, rel, full, digit in dated:
            backup = dest_root / Path(*rel.split("/"))
            try:
                _move_out(Path(full), backup)
            except OSError as err:
                raise SyncError(
                    f"Could not move the conflict copy {rel} out of the library "
                    f"({err.strerror or err}). Close what holds it, then run "
                    "`lit sync` again."
                ) from err
            loser = _side_of(digit)
            rows.append(
                SyncConflict(
                    path=original_rel,
                    paper_id=paper_id,
                    kept=kept_side or _other_side(loser),
                    kept_mtime=kept_mtime,
                    moved_mtime=mtime,
                    backup=backup,
                    restored=restored,
                )
            )
    return rows


# -- a folder deleted on one machine and changed on the other ---------------

#: Folders that hold one paper or one code repository, each a unit: a paper
#: needs its metadata.yaml and paper.pdf, a code folder its repo-meta.yaml.
_UNIT_PARENTS: tuple[str, ...] = ("papers", "codes")


@dataclass(frozen=True)
class DeletedFolderConflict:
    """A paper or code folder deleted (or renamed) on one machine and changed on the other.

    The deletion wins: the changed files were moved out of the vault to
    ``backup``, so the folder is gone everywhere after the sync.
    ``deleted_here`` says whether this machine deleted it (the changes came
    from another machine) or another machine did (the changes were made
    here). ``moved`` lists the moved files, relative to the folder.
    ``in_trash`` says the paper has a trash entry on this machine, which
    ``lit trash restore`` brings back. ``left_behind`` names what stays on
    this machine because sync never touches it (a code folder's ``repo/``).
    """

    folder: str
    deleted_here: bool
    moved: tuple[str, ...]
    backup: Path
    in_trash: bool = False
    left_behind: tuple[str, ...] = ()


def _unit_folder(rel: str) -> str | None:
    parts = rel.split("/", 2)
    if len(parts) == 3 and parts[0] in _UNIT_PARENTS:
        return f"{parts[0]}/{parts[1]}"
    return None


def _by_unit_folder(snapshot: Snapshot) -> dict[str, Snapshot]:
    grouped: dict[str, Snapshot] = {}
    for rel, stamp in snapshot.items():
        folder = _unit_folder(rel)
        if folder is not None:
            grouped.setdefault(folder, {})[rel] = stamp
    return grouped


@dataclass(frozen=True)
class SideDeltas:
    """What bisync found new, changed and deleted on one side since the last sync.

    Paths are relative to the library, with ``/``; folders are among them
    (they are only ever new or deleted).
    """

    new: frozenset[str] = frozenset()
    changed: frozenset[str] = frozenset()
    deleted: frozenset[str] = frozenset()


@dataclass(frozen=True)
class BisyncDeltas:
    """bisync's own report of what changed on each side since the last sync."""

    local: SideDeltas = field(default_factory=SideDeltas)
    cloud: SideDeltas = field(default_factory=SideDeltas)


#: One line of bisync's per-file report, after the date and time:
#: ``INFO  : - Path2             File was deleted                            - papers/x/notes.md``.
#: A path that holds a double quote or a backslash is in Go quotes.
_DELTA_LINE_RE = re.compile(
    r"^INFO\s*: - (Path1|Path2) +(File was deleted|File is new|File changed: .*?) +- (.+)$"
)
#: bisync's count of those lines, printed for each side that has any.
_DELTA_COUNT_RE = re.compile(
    r"^INFO\s*: (Path1|Path2): +\d+ changes: +(\d+) new, +(\d+) modified, +(\d+) deleted\s*$"
)
_NO_DELTAS_RE = re.compile(r"^INFO\s*: No changes found\s*$")
_DELTA_KINDS = {"File was deleted": "deleted", "File is new": "new"}


def _unquote_bisync_path(text: str) -> str | None:
    """A path as bisync's report printed it, unquoted; ``None`` when it cannot be read.

    bisync puts a path in Go quotes (``strconv.Quote``) when it holds a
    double quote or a backslash. JSON reads the escapes such a path has.
    """
    if not (len(text) >= 2 and text.startswith('"') and text.endswith('"')):
        return text
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def parse_bisync_deltas(report: Iterable[str]) -> BisyncDeltas | None:
    """Read bisync's per-file report from the INFO lines of one run.

    The one place that reads it. Returns ``None`` when the report cannot be
    trusted: a side's lines do not add up to the count bisync printed for
    that side, a path cannot be read, or the run printed neither a count nor
    "No changes found" (rclone changed its wording, or the lines went
    elsewhere). The caller then leaves every folder as the run left it.
    """
    found: dict[str, dict[str, list[str]]] = {
        side: {"new": [], "changed": [], "deleted": []} for side in ("Path1", "Path2")
    }
    counted: dict[str, tuple[int, int, int]] = {}
    nothing_changed = False
    for line in report:
        match = _DELTA_LINE_RE.match(line)
        if match is not None:
            side, what, quoted = match.groups()
            path = _unquote_bisync_path(quoted)
            if path is None:
                return None
            found[side][_DELTA_KINDS.get(what, "changed")].append(path)
            continue
        match = _DELTA_COUNT_RE.match(line)
        if match is not None:
            new, changed, deleted = (int(n) for n in match.groups()[1:])
            counted[match.group(1)] = (new, changed, deleted)
        elif _NO_DELTAS_RE.match(line):
            nothing_changed = True
    if not counted and not nothing_changed:
        return None
    sides: dict[str, SideDeltas] = {}
    for side, kinds in found.items():
        lines_per_kind = (len(kinds["new"]), len(kinds["changed"]), len(kinds["deleted"]))
        if lines_per_kind != counted.get(side, (0, 0, 0)):
            return None
        sides[side] = SideDeltas(
            new=frozenset(kinds["new"]),
            changed=frozenset(kinds["changed"]),
            deleted=frozenset(kinds["deleted"]),
        )
    return BisyncDeltas(local=sides["Path1"], cloud=sides["Path2"])


#: One operation bisync queued, after the date and time:
#: ``INFO  : - Path1             Queue copy to Path2       - remote:lib/papers/x/notes.md``
#: (padded with more spaces). A conflict's operations are printed at NOTICE.
_OP_LINE_RE = re.compile(
    r"^(?:INFO|NOTICE)\s*: - (Path1|Path2) +"
    r"(Queue copy to Path1|Queue copy to Path2|Queue delete|Renaming Path1 copy|"
    r"Renaming Path2 copy) +- (.+)$"
)
#: A full comparison (first sync, ``--first-from``): no per-file operations.
_RESYNC_LINE_RE = re.compile(r"^INFO\s*: - Path[12] +Resync is copying files to +- Path[12]\s*$")
#: The line that names the library folder as bisync prints it, operations and all.
#: On Windows: ``INFO  : Synching Path1 "C:\Users\me\lib\" with Path2 "remote:lib/"``,
#: and an operation on it ``- C:\Users\me\lib\papers/x/notes.md``.
_PATH1_ROOT_RE = re.compile(r'^INFO\s*: Synching Path1 (".*") with Path2 ".*"\s*$')
#: The operations that change the cloud copy.
_PATH2_OPS = frozenset({"Queue copy to Path2", "Renaming Path2 copy"})


@dataclass(frozen=True)
class BisyncOps:
    """What one bisync run did to each side, read from its operation lines.

    ``remote_changed``: it may have copied, renamed or deleted something in
    the cloud. ``path1_writes``: the library files it wrote or deleted here,
    relative with ``/``; ``None`` when they cannot be told.
    """

    remote_changed: bool
    path1_writes: frozenset[str] | None


def parse_bisync_ops(run: BisyncRun, *, windows: bool = sys.platform == "win32") -> BisyncOps:
    """Read which side one bisync run changed, from its operation lines.

    Cross-checked with the per-side change counts bisync prints, so a change
    in rclone's wording over-reports instead of going unnoticed: a side with
    changes counted and no operation read for the other side reads as "the
    other side may have changed". Over-reporting costs one sync that finds
    nothing to do. A full comparison, or a run that printed no count at all,
    may have changed both sides. ``windows``: rclone ran on Windows, where
    bisync quotes the library folder without escaping its backslashes.
    """
    lines = [*run.report, *run.lines]
    unknown = BisyncOps(remote_changed=True, path1_writes=None)
    if any(_RESYNC_LINE_RE.match(line) for line in lines):
        return unknown
    root: str | None = None
    counts: dict[str, int] = {}
    nothing_changed = False
    path2_ops = 0
    path1_targets: list[str] = []
    for line in lines:
        match = _OP_LINE_RE.match(line)
        if match is not None:
            side, what, target = match.groups()
            if what in _PATH2_OPS or (what == "Queue delete" and side == "Path2"):
                path2_ops += 1
            else:
                path1_targets.append(target)
            continue
        match = _DELTA_COUNT_RE.match(line)
        if match is not None:
            counts[match.group(1)] = sum(int(n) for n in match.groups()[1:])
            continue
        if _NO_DELTAS_RE.match(line):
            nothing_changed = True
            continue
        match = _PATH1_ROOT_RE.match(line)
        if match is not None and root is None:
            root = _unquote_bisync_root(match.group(1), windows=windows)
    if not counts and not nothing_changed:
        return unknown
    remote_changed = path2_ops > 0 or counts.get("Path1", 0) > 0
    if counts.get("Path2", 0) > 0 and not path1_targets:
        return BisyncOps(remote_changed=remote_changed, path1_writes=None)
    writes: set[str] = set()
    for target in path1_targets:
        rel = _relative_to_root(_unquote_bisync_path(target), root)
        if rel is None:
            return BisyncOps(remote_changed=remote_changed, path1_writes=None)
        if rel:
            writes.add(rel)
    return BisyncOps(remote_changed=remote_changed, path1_writes=frozenset(writes))


def _unquote_bisync_root(text: str, *, windows: bool) -> str | None:
    """The library folder as bisync's "Synching Path1" line printed it, unquoted.

    bisync always quotes it there. On Windows it escapes it (``strconv.Quote``,
    which doubles every backslash) only when it holds something other than a
    backslash to escape; else it only puts quotes around it. A Windows folder
    as bisync prints it always ends in a backslash, and never holds two in a
    row: so one without a doubled backslash was only quoted.
    """
    if windows and len(text) >= 2 and text[0] == text[-1] == '"' and "\\\\" not in text:
        return text[1:-1]
    return _unquote_bisync_path(text)


def _relative_to_root(target: str | None, root: str | None) -> str | None:
    """``target`` relative to the library folder bisync printed (``""`` for the folder itself).

    The folder ends in the system's separator (``\\`` on Windows); the path
    under it is rclone's, with ``/``. ``None`` when it is not under that
    folder, or either cannot be read.
    """
    if target is None or not root:
        return None
    prefix = root if root.endswith(("/", "\\")) else root + "/"
    if target == prefix[:-1]:
        return ""
    if not target.startswith(prefix):
        return None
    return target[len(prefix):].strip("/")


def _by_unit(paths: Iterable[str]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for rel in paths:
        folder = _unit_folder(rel)
        if folder is not None:
            grouped.setdefault(folder, set()).add(rel)
    return grouped


def find_deleted_folder_conflicts(
    deltas: BisyncDeltas, after: Snapshot
) -> list[tuple[str, bool]]:
    """Folders one side deleted whole while the other changed them, as ``(folder, deleted_here)``.

    rclone decides file by file, and a changed file beats a deleted one, so a
    paper deleted (or renamed) on one machine while the other changed some of
    its files comes out of the sync as a folder holding only those files.
    ``deltas`` is bisync's report of what changed on each side since the last
    sync; ``after`` this machine's snapshot after the run.

    A folder counts when one side reported files in it deleted and nothing in
    it new or changed, and every file it still holds is one that side
    reported deleted (the other side changed it, so it came back) or one the
    other side added. The cloud side deleting it means another machine did;
    this side means this machine did (``deleted_here``).

    A file deleted by itself, an SI file say, leaves the folder's other files
    in place on that side, unchanged, so the folder does not count whatever
    the other machine changed. Neither does a new paper partly uploaded by
    another machine: nothing of it was deleted.
    """
    sides = [
        (_by_unit(side.deleted), _by_unit(side.new | side.changed), _by_unit(other.new), here)
        for side, other, here in (
            (deltas.cloud, deltas.local, False),
            (deltas.local, deltas.cloud, True),
        )
    ]
    found: list[tuple[str, bool]] = []
    for folder, files in sorted(_by_unit_folder(after).items()):
        for deleted, touched, added_there, deleted_here in sides:
            gone = deleted.get(folder)
            if (
                gone
                and folder not in touched
                and files.keys() <= gone | added_there.get(folder, set())
            ):
                found.append((folder, deleted_here))
                break
    return found


def _empty_folder_shells(
    vault: Path, resolved: list[DeletedFolderConflict]
) -> list[Path]:
    """The folders of ``resolved`` that exist again here with no file in them."""
    shells: list[Path] = []
    for row in resolved:
        root = vault / Path(*row.folder.split("/"))
        if not root.is_dir():
            continue
        if not any(files for _dirpath, _dirnames, files in os.walk(root)):
            shells.append(root)
    return shells


def _remove_empty_dirs(top: Path) -> None:
    """Remove ``top`` and the folders under it that are empty, deepest first."""
    for dirpath, _dirnames, _filenames in os.walk(top, topdown=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def resolve_deleted_folder_conflicts(
    vault: Path, found: list[tuple[str, bool]], dest_root: Path
) -> list[DeletedFolderConflict]:
    """Let each deletion in ``found`` win: move the folder's files to ``dest_root``.

    Every file under the folder goes, its path kept under ``dest_root``, and
    the emptied folders are removed, so the next bisync pass takes the
    deletion to the cloud. A code folder's ``repo/`` is never touched: it is
    not synced, and it may hold work that exists nowhere else.

    Raises:
        SyncError: a file could not be moved out (the rest of the folder is
            left as it is, and the next ``lit sync`` tries again).
    """
    in_trash: set[str] = set()
    if any(folder.startswith("papers/") for folder, _here in found):
        from litman.core.trash import list_trash

        try:
            in_trash = {entry.paper_id for entry in list_trash(vault)}
        except (LitmanError, OSError):
            in_trash = set()

    rows: list[DeletedFolderConflict] = []
    for folder, deleted_here in found:
        root = vault / Path(*folder.split("/"))
        keep = root / "repo" if folder.startswith("codes/") else None
        moved: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            if keep is not None:
                dirnames[:] = [d for d in dirnames if Path(dirpath) / d != keep]
            for name in filenames:
                src = Path(dirpath) / name
                rel = src.relative_to(vault).as_posix()
                try:
                    _move_out(src, dest_root / Path(*rel.split("/")))
                except OSError as err:
                    raise SyncError(
                        f"Could not move {rel} out of the library ({err.strerror or err}). "
                        "Close what holds it, then run `lit sync` again."
                    ) from err
                if not _two_way_file_excluded(rel, name):
                    moved.append(rel[len(folder) + 1 :])
        _remove_empty_dirs(root)
        left = (f"{folder}/repo/",) if keep is not None and keep.exists() else ()
        rows.append(
            DeletedFolderConflict(
                folder=folder,
                deleted_here=deleted_here,
                moved=tuple(sorted(moved)),
                backup=dest_root / Path(*folder.split("/")),
                in_trash=folder.split("/", 1)[1] in in_trash
                and folder.startswith("papers/"),
                left_behind=left,
            )
        )
    return rows


# -- TAXONOMY.md conflicts: bring back what the older copy had --------------


def restore_dropped_taxonomy_values(
    vault: Path, older_copies: list[Path]
) -> dict[str, tuple[str, ...]]:
    """Add back the values the older TAXONOMY.md copy had and the kept one lacks.

    A TAXONOMY.md conflict keeps the newer copy whole, which would drop every
    value only the other machine had registered while its papers keep the
    tag. Values are only added, never removed, and no paper is touched.
    User dicts go through :func:`litman.core.taxonomy.add_taxonomy_values`,
    the ``lit taxonomy add`` backend. That function refuses ``projects``,
    whose names normally come with a folder; here only the name comes back,
    and this machine gives it a folder with ``lit project set-path``, so the
    projects section is written with the same section helper and staged write.

    Returns ``{dict name: values added}`` for the dicts that changed.
    """
    from litman.core.atomic import staged_write
    from litman.core.taxonomy import (
        USER_DICTS,
        add_taxonomy_values,
        parse_taxonomy,
        read_taxonomy_text,
        update_user_dict_section,
    )

    older: dict[str, set[str]] = {name: set() for name in USER_DICTS}
    for copy in older_copies:
        parsed = parse_taxonomy(copy.read_text(encoding="utf-8"))
        for name in USER_DICTS:
            older[name].update(parsed[name])

    restored: dict[str, tuple[str, ...]] = {}
    for name in USER_DICTS:
        current = parse_taxonomy(read_taxonomy_text(vault))[name]
        missing = tuple(sorted(older[name] - set(current)))
        if not missing:
            continue
        if name == "projects":
            text = read_taxonomy_text(vault)
            new_text = update_user_dict_section(
                text, name, sorted({*current, *missing})
            )
            with staged_write(vault, op_id="sync-restore-projects") as stage:
                stage.write_text("TAXONOMY.md", new_text)
        else:
            add_taxonomy_values(vault, name, missing, force=True)
        restored[name] = missing
    return restored


# -- rclone's half-written files ----------------------------------------------

#: rclone writes a transfer to ``<name>.<8 hex>.partial`` and renames it at the
#: end; a run that was killed leaves the partial file behind.
_RCLONE_PARTIAL_RE = re.compile(r"\.[0-9a-f]{8}\.partial$")


def sweep_partials(vault: Path, dest_root: Path) -> list[str]:
    """Move rclone's leftover half-written files out of the vault into ``dest_root``.

    Excluded from sync, they would otherwise stay in the paper folders. Run
    only after a sync went through, when no rclone of this vault is writing.
    """
    moved: list[str] = []
    root = str(vault)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [
            d for d in dirnames
            if not os.path.islink(os.path.join(dirpath, d))
            and not _two_way_dir_excluded(prefix + d, d)
        ]
        for name in filenames:
            if _RCLONE_PARTIAL_RE.search(name) is None:
                continue
            rel = prefix + name
            try:
                _move_out(Path(dirpath) / name, dest_root / Path(*rel.split("/")))
            except OSError:
                continue
            moved.append(rel)
    return moved


# -- rclone's lock --------------------------------------------------------------


def _pid_running(pid: int) -> bool | None:
    """Whether process ``pid`` runs on this machine; ``None`` when that cannot be told.

    POSIX only: on Windows ``os.kill`` ends the process instead of probing it.
    """
    if sys.platform == "win32":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _lock_message(workdir: Path) -> str:
    """What to tell the user about the bisync lock found in ``workdir``."""
    pid: int | None = None
    expires: datetime | None = None
    for lock in sorted(workdir.glob("*.lck")):
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
            pid = int(payload["PID"])
            expires = datetime.fromisoformat(str(payload["TimeExpires"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        break
    running = _pid_running(pid) if pid is not None else None
    when: str | None = None
    if expires is not None:
        local = expires.astimezone()
        if local - datetime.now().astimezone() < timedelta(days=1):
            when = local.strftime("%H:%M")
    if running:
        return (
            "Another sync of this library is running on this machine, so this one "
            "did not start. Wait for it to finish, then run `lit sync` again."
        )
    if running is False and when is not None:
        return (
            "The last sync of this library was cut off and left its lock behind. "
            f"The lock expires at {when}; run `lit sync` again after that."
        )
    if when is not None:
        return (
            "Another sync of this library holds its lock: one is running, or the "
            f"last one was cut off. If none is running, run `lit sync` again after {when}."
        )
    return (
        "Another sync of this library holds its lock: one is running, or the last "
        f"one was cut off. If none is running, delete the .lck file in {workdir} "
        "and run `lit sync` again."
    )


# -- running rclone bisync ---------------------------------------------------


def bisync_args(
    vault: Path,
    target_url: str,
    paths: BisyncPaths,
    *,
    backup_name: str,
    resync_mode: Literal["path1", "path2"] | None = None,
    force: bool = False,
    stats: str = "30s",
) -> list[str]:
    """The one place the ``rclone bisync`` command line is built.

    Fixed flags (ADR-030), never ``--conflict-resolve none``: rclone's default
    renames both copies of a conflict, which leaves a paper with no
    ``metadata.yaml``. rclone's own safety stops stay on: ``--force`` is passed
    only when the user passed ``lit sync --force``, and a resync
    (``--resync-mode``) only in the first-sync cases :func:`two_way_sync`
    allows, or on the user's ``--first-from``. ``--max-lock`` lets the lock of
    a run that was killed expire instead of blocking every later sync (a
    running sync renews its lock). ``--log-level INFO`` makes bisync report
    each file it found new, changed or deleted on each side
    (:func:`parse_bisync_deltas`); :func:`run_bisync` keeps those lines from
    the user. ``stats`` is how often rclone logs its stats line: every second
    when litman draws its own progress line from it.
    """
    args = [
        "bisync", str(vault), target_url,
        "--conflict-resolve", "newer",
        "--conflict-loser", "pathname",
        "--conflict-suffix", "sync-conflict",
        "--resilient", "--recover",
        "--compare", "size,modtime",
        "--create-empty-src-dirs",
        "--max-lock", "5m",
        "--workdir", str(paths.workdir),
        "--filters-file", str(paths.filters_file),
        "--backup-dir1", str(paths.local_backup_root / backup_name),
        "--backup-dir2", f"{paths.remote_backup_root}/{backup_name}",
        "--color", "NEVER",
        "--log-level", "INFO",
        "--stats", stats, *_STATS_ARGS,
    ]
    if resync_mode is not None:
        args += ["--resync-mode", resync_mode]
    if force:
        args.append("--force")
    return args


@dataclass(frozen=True)
class BisyncRun:
    """One ``rclone bisync`` run that ended (or a push's or pull's ``rclone sync``).

    ``lines`` are its log lines at NOTICE and above but its stats lines
    (:func:`is_rclone_stats`): the ones its error quotes. ``report`` are its
    INFO lines, the per-file report that :func:`parse_bisync_deltas` reads
    among them. rclone's date and time are taken off every line.
    """

    returncode: int
    lines: tuple[str, ...]
    report: tuple[str, ...]


@dataclass(frozen=True)
class SyncProgress:
    """How far one rclone run has copied, from its stats line.

    ``done`` and ``total`` are bytes, as precise as rclone prints them;
    ``percent`` is rclone's own, None while there is nothing to copy.
    """

    done: int
    total: int
    percent: int | None


def is_rclone_stats(line: str) -> bool:
    """True when ``line`` (rclone's date and time taken off) is one of its stats lines."""
    return _RCLONE_STATS_LOOSE_RE.match(line) is not None


def parse_rclone_stats(line: str) -> SyncProgress | None:
    """``line`` read as rclone's stats line; None when it does not read as one."""
    match = _RCLONE_STATS_RE.match(line)
    if match is None:
        return None

    def size(number: str, unit: str | None) -> int:
        return round(float(number) * _SIZE_UNITS[unit or ""])

    percent = match["percent"]
    return SyncProgress(
        done=size(match["done"], match["done_unit"]),
        total=size(match["total"], match["total_unit"]),
        percent=None if percent == "-" else int(percent[:-1]),
    )


@dataclass
class SyncControl:
    """How a sync run by the GUI is steered from outside: stopped, or kept short.

    ``interrupt`` asks rclone to stop and save its state (SIGINT; on Windows a
    windowless process cannot send one, so it waits for ``kill``) and runs no
    further pass. ``kill`` stops rclone at once; the next sync recovers.
    ``skip_git`` skips bringing the code checkouts in. ``own_group`` starts
    rclone in a process group of its own, so a Ctrl+C in the terminal that
    runs ``lit gui`` stops the GUI, not the auto sync's rclone.
    """

    interrupt: threading.Event = field(default_factory=threading.Event)
    kill: threading.Event = field(default_factory=threading.Event)
    skip_git: threading.Event = field(default_factory=threading.Event)
    own_group: bool = False


def _popen_group_kwargs(own_group: bool) -> dict[str, Any]:
    """Popen arguments that start rclone in its own process group, or none."""
    if not own_group:
        return {}
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | hidden_console_flags()}
    return {"start_new_session": True}


def _interrupt(proc: subprocess.Popen[str]) -> None:
    """Ask rclone to stop and save its state; a no-op on Windows (see :class:`SyncControl`)."""
    if sys.platform == "win32" or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except OSError:
        pass


def _watch_control(
    proc: subprocess.Popen[str], control: SyncControl, done: threading.Event
) -> None:
    """Carry ``control``'s requests to ``proc`` until it ends."""
    interrupted = False
    while not done.wait(0.2):
        if control.kill.is_set():
            try:
                proc.kill()
            except OSError:
                pass
            return
        if control.interrupt.is_set() and not interrupted:
            _interrupt(proc)
            interrupted = True


def _bisync_env() -> dict[str, str]:
    """This process's environment, less the variables that change rclone's log."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _RCLONE_LOG_ENV_NAMES
        and not key.upper().startswith(_RCLONE_LOG_ENV_PREFIXES)
    }


def run_bisync(
    args: list[str],
    on_log: Callable[[str], None] | None = None,
    *,
    control: SyncControl | None = None,
    on_progress: Callable[[SyncProgress | None], None] | None = None,
) -> BisyncRun:
    """Run ``rclone <args>``; hand each line the user should see to ``on_log`` as it comes.

    A log record is a line with rclone's date and time, plus the lines after
    it that have none (a JSON dump, say). Records below NOTICE go to the
    run's ``report`` and are not shown, except lines with no date that end
    the output after one: those are not part of it but a crash report, say,
    and are shown once rclone has exited. On Ctrl+C, rclone (same process
    group) gets the interrupt too and saves its state; it is given time for
    that before the interrupt goes on. Any other way out of this function
    (a SIGTERM made into ``SystemExit``, say) reached litman alone, so rclone
    is asked to stop first. ``control`` steers the run from another thread.

    rclone's stats lines (:func:`is_rclone_stats`) are never among the run's
    ``lines``. With ``on_progress``, each one that reads
    (:func:`parse_rclone_stats`) goes to it, and ``None`` once the run is over,
    however it ends; without, they go to ``on_log`` like any other line.
    """
    _require_rclone()
    own_group = control is not None and control.own_group
    popen_kwargs = _popen_group_kwargs(own_group)
    popen_kwargs.setdefault("creationflags", hidden_console_flags())
    try:
        proc = subprocess.Popen(
            [RCLONE_BIN, *_never_ask_args(), *args],
            stdin=subprocess.DEVNULL if _NEVER_ASK.get() else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_bisync_env(),
            **popen_kwargs,
        )
    except FileNotFoundError as e:
        raise SyncError(
            "`rclone` executable not found on PATH (race with install?). "
            "See https://rclone.org/install/."
        ) from e
    lines: list[str] = []
    report: list[str] = []
    held: list[str] = []
    level: str | None = None

    def show(line: str) -> None:
        lines.append(line)
        if on_log is not None and line.strip():
            on_log(line)

    done = threading.Event()
    if control is not None:
        threading.Thread(target=_watch_control, args=(proc, control, done), daemon=True).start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            text = raw.rstrip("\r\n")
            line = _RCLONE_LOG_PREFIX_RE.sub("", text, count=1)
            if line != text:
                match = _RCLONE_LEVEL_RE.match(line)
                level = match.group(1) if match else None
                held = []
            if level in _RCLONE_REPORT_LEVELS:
                report.append(line)
                if line == text:
                    held.append(line)
                continue
            if line != text and is_rclone_stats(line):
                if on_progress is not None:
                    progress = parse_rclone_stats(line)
                    if progress is not None:
                        on_progress(progress)
                elif on_log is not None:
                    on_log(line)
                continue
            show(line)
        returncode = proc.wait()
        for line in held:
            show(line)
    except BaseException as err:
        # A terminal's Ctrl+C reached rclone too, unless it runs in a group of
        # its own; a second SIGINT would make it stop without saving its state.
        if own_group or not isinstance(err, KeyboardInterrupt):
            _interrupt(proc)
        try:
            proc.communicate(timeout=100)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
        raise
    finally:
        done.set()
        if on_progress is not None:
            on_progress(None)
    return BisyncRun(returncode, tuple(lines), tuple(report))


def _choice_message(reason: str) -> str:
    return (
        f"{reason} Pick the winner: `lit sync --first-from cloud` or "
        "`lit sync --first-from local` (files that differ on the other side go "
        "to the backup)."
    )


def _ending_with_rclone[E: SyncError](err: E, tail: str) -> E:
    """``err``, marked when its message ends with rclone's last lines ``tail``.

    :attr:`litman.exceptions.SyncError.rclone_tail`.
    """
    if tail and str(err).endswith("\n" + tail):
        err.rclone_tail = tail
    return err


def _tail(lines: list[str]) -> str:
    """rclone's last five lines, blank ones left out and each said once (where it last came)."""
    latest = dict.fromkeys(line for line in reversed(lines) if line.strip())
    return "\n".join(reversed(list(latest)[:5]))


def _bisync_failure(returncode: int, lines: list[str], workdir: Path) -> SyncError:
    """Turn a failed bisync run into the error the user sees, rclone's last lines marked."""
    tail = _tail(lines)
    return _ending_with_rclone(_bisync_error(returncode, lines, workdir, tail), tail)


def _bisync_error(returncode: int, lines: list[str], workdir: Path, tail: str) -> SyncError:
    text = "\n".join(lines).lower()
    if "too many deletes" in text:
        here, there = (
            ("in the cloud", "on this machine")
            if re.search(r"too many deletes[^\n]*on path2", text)
            else ("on this machine", "in the cloud")
        )
        return SyncStoppedError(
            f"rclone stopped: more than half of the files were deleted {here}, so "
            f"nothing was deleted {there}. If that is intended, run "
            f"`lit sync --force`; the deleted files go to the backup.\n{tail}"
        )
    if "all files were changed" in text:
        return SyncStoppedError(
            "rclone stopped: every file changed on both sides since the last sync, "
            f"so nothing was copied. If that is expected, run `lit sync --force`.\n{tail}"
        )
    if "empty current path2 listing" in text:
        return SyncChoiceRequiredError(
            "The cloud folder is empty, though this machine has synced with it "
            "before, so nothing was synced. To put this library back there, run "
            f"`lit sync --first-from local`.\n{tail}"
        )
    if "empty current path1 listing" in text:
        return SyncChoiceRequiredError(
            "This library is empty, though it has synced before, so nothing was "
            "synced. To bring the cloud's library back here, run "
            f"`lit sync --first-from cloud`.\n{tail}"
        )
    if "lock file found" in text:
        # rclone's last lines end with its own way out (delete the lock by
        # hand); the message names the one to take.
        return BisyncLockedError(_lock_message(workdir))
    if returncode == 7 and "retryable" not in text:
        return SyncChoiceRequiredError(
            _choice_message("rclone cannot continue from its records of the last sync.")
            + f"\n{tail}"
        )
    if returncode == 7:
        return SyncError(
            f"rclone stopped the sync (exit 7). Run `lit sync` again; if the same "
            "error comes back, start over with `lit sync --first-from cloud` or "
            f"`lit sync --first-from local`. rclone's last lines:\n{tail}"
        )
    return SyncError(
        f"rclone could not finish the sync (exit {returncode}). Run `lit sync` "
        f"again; rclone's last lines say why:\n{tail}"
    )


#: How the GUI's auto sync treats a failed sync: ``needs-user`` stops it until
#: the user acts, ``busy`` retries soon, ``offline`` retries with a growing pause.
SyncErrorKind = Literal["needs-user", "busy", "offline"]

#: What rclone says when its OAuth sign-in to a provider has expired or been
#: revoked; it names ``rclone config reconnect`` itself.
_OAUTH_EXPIRED_RE = re.compile(
    r"invalid_grant|token expired|expired_access_token|couldn't fetch token|"
    r"refresh token has expired|config reconnect",
    re.IGNORECASE,
)

#: The server's own answer to a sign-in it turned down, as rclone passes it on:
#: a WebDAV server's to a wrong or revoked password, for one.
_SIGN_IN_REFUSED_RE = re.compile(r"\b401 Unauthorized\b")


def oauth_expired(text: str) -> bool:
    """Whether rclone's ``text`` says an OAuth token expired (``rclone config reconnect``)."""
    return bool(_OAUTH_EXPIRED_RE.search(text))


def sign_in_refused(text: str) -> bool:
    """Whether rclone's ``text`` says the cloud refused its sign-in."""
    return bool(_OAUTH_EXPIRED_RE.search(text) or _SIGN_IN_REFUSED_RE.search(text))


SignIn = Literal["browser", "password"]

#: What a sign-in error says to run again once signed in: ``lit sync``, or its
#: push or pull.
SyncCommand = Literal["sync", "push", "pull"]

#: The rclone backends whose sign-in goes through a browser, by name and by
#: prefix (:func:`_oauth_backends`); ``None`` until rclone has listed them.
_oauth_backend_names: frozenset[str] | None = None


def _oauth_backends() -> frozenset[str] | None:
    """The rclone backends that sign in through a browser (OAuth), or None if rclone cannot say.

    From ``rclone config providers``: rclone gives every OAuth backend the same
    ``token``, ``auth_url`` and ``token_url`` options. A local call, kept for
    the process once it worked.
    """
    global _oauth_backend_names
    if _oauth_backend_names is None:
        result = run_rclone(["config", "providers"], check=False, timeout=_GATE_TIMEOUT)
        if result.returncode != 0:
            return None
        names: set[str] = set()
        for backend in json.loads(result.stdout or "[]"):
            if not isinstance(backend, dict):
                continue
            options = {
                option.get("Name")
                for option in backend.get("Options") or ()
                if isinstance(option, dict)
            }
            if {"token", "auth_url", "token_url"} <= options:
                names.update(
                    str(backend[key]) for key in ("Name", "Prefix") if backend.get(key)
                )
        _oauth_backend_names = frozenset(names)
    return _oauth_backend_names


def remote_sign_in(remote: str) -> SignIn | None:
    """How the user signs in to rclone remote ``remote`` again; None if rclone cannot tell.

    ``browser`` for an OAuth backend (``rclone config reconnect``),
    ``password`` for any other: one typed into ``rclone config``, which is
    the only way back for a WebDAV remote (it refuses ``reconnect``). Reads the
    remote's type from ``rclone listremotes --json``: local calls, no network,
    no prompt.
    """
    try:
        with rclone_never_asks():
            listed = run_rclone(["listremotes", "--json"], check=False, timeout=_GATE_TIMEOUT)
            if listed.returncode != 0:
                return None
            entries = json.loads(listed.stdout or "[]")
            kind = next(
                (
                    entry.get("type")
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("name") == remote
                ),
                None,
            )
            if not isinstance(kind, str) or not kind:
                return None
            oauth = _oauth_backends()
    except (SyncError, OSError, ValueError, TypeError):
        return None
    if oauth is None:
        return None
    return "browser" if kind in oauth else "password"


def _command(verb: SyncCommand) -> str:
    return "lit sync" if verb == "sync" else f"lit sync {verb}"


_DONE = {"sync": "synced", "push": "pushed", "pull": "pulled"}


def sign_in_way_out(remote: str, sign_in: str | None, verb: SyncCommand = "sync") -> str:
    """The sentence that tells the user how to sign in to ``remote`` again, then rerun ``verb``."""
    again = f"then run `{_command(verb)}`."
    if sign_in == "browser":
        return f"Sign in again with `rclone config reconnect {shell_arg(remote + ':')}`, {again}"
    if sign_in == "password":
        return f"Enter the password again with `rclone config` (edit remote {remote}), {again}"
    return f"Sign in again with `rclone config` (edit remote {remote}), {again}"


def auth_failure(target_url: str, detail: str, verb: SyncCommand = "sync") -> SyncAuthError:
    """The error for a ``verb`` the cloud's sign-in stopped: what happened, way out, rclone's lines.

    The way out fits the remote (:func:`remote_sign_in`); rclone's own words
    for an expired OAuth token settle it without asking rclone.
    """
    remote = target_url.partition(":")[0]
    sign_in: SignIn | None = (
        "browser" if _OAUTH_EXPIRED_RE.search(detail) else remote_sign_in(remote)
    )
    nothing = f"so nothing was {_DONE[verb]}."
    if sign_in == "browser":
        verdict = f"The sign-in to {remote} has expired, {nothing}"
    elif sign_in == "password":
        verdict = f"{remote} refused the password, {nothing}"
    else:
        verdict = f"{remote} refused the sign-in, {nothing}"
    return _ending_with_rclone(
        SyncAuthError(
            f"{verdict} {sign_in_way_out(remote, sign_in, verb)}\n{detail}", sign_in=sign_in
        ),
        detail,
    )


def as_auth_failure(
    err: BaseException, target_url: str, verb: SyncCommand = "sync"
) -> BaseException:
    """``err`` made into :func:`auth_failure` when the cloud refused the sign-in; else ``err``.

    Keeps rclone's lines from ``err`` (what follows its first line) and drops
    its own advice, which would say to run ``verb`` again as it was.
    """
    if isinstance(err, SyncAuthError) or classify_sync_error(err) != ("needs-user", "auth"):
        return err
    text = str(err)
    detail = text.split("\n", 1)[1] if "\n" in text else text
    return auth_failure(target_url, detail, verb)


def classify_sync_error(err: BaseException) -> tuple[SyncErrorKind, str]:
    """``(kind, reason)`` for a failed sync, for the GUI's auto sync.

    Needs the user (``reason``): ``choice`` (``--first-from``, a changed
    filter set, records gone, an emptied side), ``safety-stop`` (rclone's
    too-many-deletes or all-changed stop), ``auth`` (the cloud refused the
    sign-in: an expired OAuth token, a password it no longer accepts),
    ``lock`` (the file system refuses the sync locks). Busy: another sync
    holds litman's lock or rclone's. Anything else, the network included, is
    taken as offline.
    """
    if isinstance(err, (SyncBusyError, BisyncLockedError)):
        return "busy", "busy"
    if isinstance(err, SyncLockError):
        return "needs-user", "lock"
    if isinstance(err, SyncChoiceRequiredError):
        return "needs-user", "choice"
    if isinstance(err, SyncStoppedError):
        return "needs-user", "safety-stop"
    if isinstance(err, SyncAuthError) or sign_in_refused(str(err)):
        return "needs-user", "auth"
    return "offline", "offline"


def _reserve_backup_name(local_root: Path, label: str = "") -> str:
    """Name, and create, a fresh local backup folder for one bisync run.

    Created up front because bisync creates it only when it backs something
    up, and the same name is used for the cloud-side folder: two runs within
    one second must not share it. An unused folder is removed afterwards
    (:func:`_drop_if_empty`). The name is the time, then a number when that
    time is taken, then ``label``: :data:`SAVE_BACKUP_LABEL` for a save's.
    """
    base = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name, n = base + label, 2
    while True:
        try:
            (local_root / name).mkdir()
            return name
        except FileExistsError:
            name = f"{base}-{n}{label}"
            n += 1


def conflict_backups_root(vault: Path, target_url: str) -> Path:
    """Where this machine keeps the conflict backups of ``vault`` synced with ``target_url``.

    :attr:`BisyncPaths.conflicts_root`, also for a target two-way sync
    refuses (the remote's top level) or none (``""``): a save keeps its copy
    there all the same.
    """
    return sync_data_dir() / "sync-backup" / vault_key(vault, target_url) / "conflicts"


def keep_replaced_copy(vault: Path, rel: str) -> Path:
    """Copy ``<vault>/<rel>`` to the conflicts backup, before a save replaces it.

    For a GUI save over a file that changed since the page read it (a sync
    brought another machine's copy, an agent rewrote it): the save wins, and
    the copy it replaces goes where a sync puts the older copy of a conflict,
    ``<data dir>/sync-backup/<vault-key>/conflicts/<time>-save/<rel>``: the
    label tells it from a sync's folder (:func:`recent_conflict_backups`).
    Keyed by the configured remote, or by the library alone when it has no
    ``sync:``. Returns the backup's path.

    Raises:
        OSError: the copy could not be made.
    """
    from litman.core.config import load_config

    vault = vault.resolve()
    try:
        configured = load_config(vault).sync
        target = configured.target_url() if configured is not None else ""
    except (LitmanError, OSError):
        target = ""
    root = conflict_backups_root(vault, target)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / _reserve_backup_name(root, SAVE_BACKUP_LABEL) / Path(*rel.split("/"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(vault / Path(*rel.split("/")), dest)
    return dest


#: The end of the name of a folder of conflict backups that a GUI save made
#: (:func:`keep_replaced_copy`), not a sync.
SAVE_BACKUP_LABEL = "-save"
#: How many days back ``lit sync status`` lists the folders of conflict backups.
CONFLICT_BACKUP_DAYS = 7
#: How many files of one such folder it names; the total counts them all.
CONFLICT_BACKUP_FILES_CAP = 5

#: A backup folder's name (:func:`_reserve_backup_name`): its time, the number
#: that tells apart two made in one second, a save's label.
_BACKUP_NAME_RE = re.compile(
    rf"^(\d{{4}}-\d{{2}}-\d{{2}}_\d{{6}})(?:-\d+)?({re.escape(SAVE_BACKUP_LABEL)})?$"
)


@dataclass(frozen=True)
class ConflictBackupFolder:
    """One folder of this machine's conflict backups (:attr:`BisyncPaths.conflicts_root`).

    ``at`` is when it was made, read from its name (this machine's time).
    ``by_save`` says a GUI save made it, to keep the copy it replaced of a file
    that had changed since the page read it. Else a sync made it, for the
    copies that lost its conflicts (``<file>.sync-conflict1``: this machine's,
    ``2``: the cloud's) and the files of a folder deleted on one machine and
    changed on the other. ``files`` are the first
    :data:`CONFLICT_BACKUP_FILES_CAP`, relative to the folder, with ``/``;
    ``files_total`` counts them all.
    """

    path: Path
    at: datetime
    by_save: bool
    files: tuple[str, ...]
    files_total: int


def recent_conflict_backups(
    root: Path, *, now: datetime | None = None, days: int = CONFLICT_BACKUP_DAYS
) -> list[ConflictBackupFolder]:
    """The folders of conflict backups under ``root`` made in the last ``days`` days.

    Newest first. ``root`` is a :attr:`BisyncPaths.conflicts_root`; ``now`` is
    this machine's time, without a zone like the folder names. A folder whose
    name is not a backup folder's, or that holds no file, is left out.
    """
    since = (now or datetime.now()) - timedelta(days=days)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    found: list[ConflictBackupFolder] = []
    for name in names:
        match = _BACKUP_NAME_RE.match(name)
        if match is None:
            continue
        try:
            at = datetime.strptime(match.group(1), "%Y-%m-%d_%H%M%S")
        except ValueError:
            continue
        if at < since:
            continue
        files = sorted(_files_under(root / name))
        if files:
            found.append(
                ConflictBackupFolder(
                    path=root / name,
                    at=at,
                    by_save=match.group(2) is not None,
                    files=tuple(files[:CONFLICT_BACKUP_FILES_CAP]),
                    files_total=len(files),
                )
            )
    found.sort(key=lambda f: (f.at, f.path.name), reverse=True)
    return found


def _files_under(folder: Path) -> list[str]:
    """Every file under ``folder``, relative to it and with ``/``."""
    out: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(folder):
        rel_dir = os.path.relpath(dirpath, folder).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        out.extend(prefix + name for name in filenames)
    return out


def _drop_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _projects_without_folder(vault: Path) -> tuple[str, ...]:
    """Projects in TAXONOMY.md with no folder in this machine's lit-config.yaml."""
    from litman.core.config import load_config
    from litman.core.taxonomy import parse_taxonomy, read_taxonomy_text

    try:
        listed = parse_taxonomy(read_taxonomy_text(vault)).get("projects", [])
        configured = load_config(vault).projects
    except (LitmanError, OSError):
        return ()
    return tuple(sorted(set(listed) - set(configured)))


@dataclass(frozen=True)
class TwoWayResult:
    """What one ``lit sync`` did.

    ``mode`` is ``sync`` for an ordinary two-way run, ``first-local`` /
    ``first-cloud`` for a full comparison where that side won every
    difference: a first sync, or the user's ``--first-from`` (``chosen``).
    ``went_up`` is ``None`` when it was not counted (a full comparison only
    uploads what differs, and the cloud's listing is not read for a count).

    ``remote_changed`` says whether this run may have changed the cloud copy:
    the auto-sync stamp is written only then, so two idle machines never wake
    each other.

    ``conflicts`` and ``deleted_folders`` are what the run had to settle (a
    file changed on both sides; a folder deleted on one and changed on the
    other). ``deletions_unchecked`` says bisync's per-file report could not
    be read, so folders were not checked for the second kind and one may be
    left with only the changed files. ``taxonomy_restored`` lists the values
    a TAXONOMY.md conflict would have dropped and that were added back.

    ``repos`` is what bringing the code checkouts to their recorded commits
    did (:func:`litman.core.code_sync.bring_repos_in`); ``repos_error`` says
    why recording or bringing them failed, the file sync having gone through.
    ``repos_recorded`` names the repos whose commit this sync recorded for the
    first time, and took up for the other machines to follow.

    ``unsynced`` lists the files that changed here while the sync ran, and
    not through it: they may not have gone up, and the next sync takes them.
    ``stamp_written`` says this sync wrote a new auto-sync stamp.
    """

    target_url: str
    mode: TwoWayMode
    chosen: bool
    came_down: FileChanges
    went_up: FileChanges | None
    conflicts: tuple[SyncConflict, ...]
    remote_changed: bool
    local_backups: tuple[Path, ...]
    projects_without_folder: tuple[str, ...]
    rebuild: dict[str, Any] | None = None
    rebuild_error: str | None = None
    deleted_folders: tuple[DeletedFolderConflict, ...] = ()
    taxonomy_restored: dict[str, tuple[str, ...]] = field(default_factory=dict)
    taxonomy_restore_error: str | None = None
    deletions_unchecked: bool = False
    repos: RepoSyncReport | None = None
    repos_error: str | None = None
    repos_recorded: tuple[str, ...] = ()
    unsynced: tuple[str, ...] = ()
    stamp_written: bool = False


def two_way_sync(
    vault: Path,
    target_url: str,
    *,
    first_from: FirstFrom | None = None,
    force: bool = False,
    on_log: Callable[[str], None] | None = None,
    git_progress: bool = False,
    lock_wait: float = SYNC_LOCK_WAIT,
    on_lock_wait: Callable[[LockHolder | None], None] | None = None,
    control: SyncControl | None = None,
    by: str = "lit sync",
    trigger: RunTrigger = "terminal",
    on_progress: Callable[[SyncProgress | None], None] | None = None,
) -> TwoWayResult:
    """Sync ``vault`` with ``target_url`` both ways through ``rclone bisync``.

    1. Check that rclone is new enough, and work out the state and backup
       folders (:func:`bisync_paths`). On a first sync, check that rclone's
       record names fit (:func:`require_bisync_names_fit`).
    2. Decide the run. On the first sync of this (vault, remote) pair: an
       empty or missing remote folder → this machine's library wins; a fresh
       local vault (:func:`local_vault_is_fresh`) → the cloud's wins; both
       holding a library → :class:`SyncChoiceRequiredError`, nothing changed.
       A changed filter set is refused the same way. ``first_from`` is the
       user's explicit choice and overrides all three. Then record in each
       code repo's ``repo-meta.yaml`` the commit of a checkout the user moved
       (:func:`litman.core.code_sync.record_repo_versions`).
    3. Unlock TRUTH (Windows cannot replace a read-only file) and run bisync.
       Then settle what it left: move every conflict's older copy out of the
       vault, let a folder deleted on one machine win over changes made to it
       on the other, and move rclone's leftover half-written files out. Run
       bisync once more so the removals reach the cloud too. After a
       TAXONOMY.md conflict, once a pass settles nothing new, add back the
       values only the older copy had and run one more pass to take them up.
       Re-lock TRUTH in a ``finally``.
    4. Bring each code checkout to the commit its ``repo-meta.yaml``
       records, fetching it from the upstream
       (:func:`litman.core.code_sync.bring_repos_in`; git's progress shows
       with ``git_progress``). A repo that cannot follow is reported, never a
       reason to fail the sync. A ``repo-meta.yaml`` still without a commit
       gets this machine's, and one more pass takes it up.
    5. Record the result in the sync-state file, then rebuild INDEX.json,
       views/ and the project links from TRUTH (none of them travels).
       How the run ended, with its conflicts, repos left behind or error, is
       kept there too (:class:`LastRun`, ``trigger`` saying who ran it), for
       ``lit sync status``, with the repos holding changes only this machine
       has and the lines rclone logged; a run another sync held off leaves it
       as it was.

    Every file a run replaces or deletes goes to a backup folder: on this
    machine under ``<data dir>/sync-backup/<vault-key>/``, in the cloud under
    ``<path>-litman-backup/``, one folder per run.

    The whole sync holds the library's sync lock, waiting up to ``lock_wait``
    seconds for another sync to end (``on_lock_wait`` hears the holder when
    the wait starts), and goes on only if the library still syncs with
    ``target_url`` then (:func:`require_still_set_up`); rclone's passes also
    hold the lock the GUI's saves wait on (:mod:`litman.core.sync_lock`).
    ``by`` names this sync in the lock. Before the passes the auto-sync stamp
    is read, and after a sync that changed the cloud a new one is written
    (:class:`StampRound`).
    ``control`` steers a sync the GUI runs (:class:`SyncControl`).
    ``on_log`` hears each line rclone logs at NOTICE and above; with
    ``on_progress``, its stats lines, logged every second, go there instead
    (:func:`run_bisync`).

    Raises:
        SyncChoiceRequiredError: the run needs the user's ``--first-from``.
        SyncBusyError: another sync of this library held the lock.
        SyncNotSetUpError: the library stopped syncing with ``target_url``
            while this waited for the lock; not recorded.
        SyncError: rclone is missing or too old, its record names would be
            too long, the remote cannot be listed, or bisync failed (its last
            log lines are in the message).
    """
    vault = vault.resolve()
    heard: list[str] = []

    def hear(line: str) -> None:
        heard.append(line)
        if on_log is not None:
            on_log(line)

    try:
        require_bisync_rclone()
        paths = bisync_paths(vault, target_url)
        if not bisync_initialised(paths.workdir):
            # rclone has not written these record names yet; nothing is written
            # before they are known to fit.
            require_bisync_names_fit(vault, target_url)
        paths.workdir.mkdir(parents=True, exist_ok=True)
        with hold_sync_lock(paths.workdir, by=by, wait=lock_wait, on_wait=on_lock_wait):
            require_still_set_up(vault, target_url)
            result = _two_way_sync_held(
                vault,
                target_url,
                paths,
                first_from=first_from,
                force=force,
                on_log=hear,
                on_progress=on_progress,
                git_progress=git_progress,
                control=control or SyncControl(),
            )
            record_run(vault, _with_rclone_lines(run_from_result(result, trigger), heard))
            return result
    except (SyncBusyError, BisyncLockedError, SyncNotSetUpError):
        # Another sync's run, or none of this library's any more: the record
        # stays as it was.
        raise
    except (LitmanError, OSError) as err:
        refused = as_auth_failure(err, target_url)
        record_run(vault, _with_rclone_lines(run_from_error(refused, trigger), heard))
        if refused is not err:
            raise refused from err
        raise


def _two_way_sync_held(
    vault: Path,
    target_url: str,
    paths: BisyncPaths,
    *,
    first_from: FirstFrom | None,
    force: bool,
    on_log: Callable[[str], None] | None,
    on_progress: Callable[[SyncProgress | None], None] | None,
    git_progress: bool,
    control: SyncControl,
) -> TwoWayResult:
    """:func:`two_way_sync`, once it holds the sync lock."""
    initialised = bisync_initialised(paths.workdir)
    paths.local_backup_root.mkdir(parents=True, exist_ok=True)

    # rclone keeps a hash of the filters file from the last full comparison and
    # stops once the file changes; until then the old file must stay as it is.
    rules = two_way_filter_rules().encode("utf-8")
    try:
        rules_unchanged = paths.filters_file.read_bytes() == rules
    except OSError:
        rules_unchanged = False

    mode: TwoWayMode
    if first_from is not None:
        mode = "first-local" if first_from == "local" else "first-cloud"
    elif initialised:
        if not rules_unchanged:
            raise SyncChoiceRequiredError(
                _choice_message(
                    "The set of files litman never syncs has changed since the "
                    "last sync, so rclone must compare both sides afresh."
                )
            )
        mode = "sync"
    else:
        paths.filters_file.write_bytes(rules)
        rules_unchanged = True
        if not remote_holds_files(target_url, paths.filters_file):
            mode = "first-local"
        elif local_vault_is_fresh(vault):
            mode = "first-cloud"
        elif read_sync_state(vault).last_sync is not None:
            raise SyncChoiceRequiredError(
                _choice_message(
                    "This machine's records of its last sync are gone, and both "
                    "this machine and the cloud hold a library."
                )
            )
        else:
            raise SyncChoiceRequiredError(
                _choice_message(
                    "This machine and the cloud both hold a library already."
                )
            )
    if not rules_unchanged:
        paths.filters_file.write_bytes(rules)

    # The commits the user moved checkouts to go up with this sync.
    from litman.core import code_sync

    repos_error: str | None = None
    try:
        code_sync.record_repo_versions(
            vault, code_sync.read_last_heads(paths.repo_heads_file)
        )
    except (LitmanError, OSError) as err:
        repos_error = str(err)

    baseline = _read_snapshot(paths.snapshot_file) if mode == "sync" else None
    stamps = StampRound.begin(paths.workdir, target_url)
    before = local_snapshot(vault)
    ops: list[BisyncOps] = []
    backups: list[str] = []
    conflicts: list[SyncConflict] = []
    deleted_folders: list[DeletedFolderConflict] = []
    deletions_unchecked = False
    restored: dict[str, tuple[str, ...]] = {}
    restore_error: str | None = None

    def run_pass(resync_mode: Literal["path1", "path2"] | None) -> BisyncRun:
        name = _reserve_backup_name(paths.local_backup_root)
        backups.append(name)
        args = bisync_args(
            vault, target_url, paths,
            backup_name=name, resync_mode=resync_mode, force=force,
            stats="30s" if on_progress is None else "1s",
        )
        run = run_bisync(args, on_log, control=control, on_progress=on_progress)
        if run.returncode != 0:
            raise _bisync_failure(run.returncode, list(run.lines), paths.workdir)
        ops.append(parse_bisync_ops(run))
        return run

    repos: RepoSyncReport | None = None
    recorded: list[str] = []
    # litman's own fix-ups made after the last pass (a conflict's copy moved
    # out, TAXONOMY.md values put back): no pass took them up.
    unsent = False
    try:
        with hold_pass_lock(paths.workdir):
            try:
                ensure_truth_unlocked(vault)
                if mode == "sync":
                    first = run_pass(None)
                else:
                    # bisync needs both roots to exist; mkdir is a no-op on one that does.
                    run_rclone(["mkdir", target_url])
                    first = run_pass("path1" if mode == "first-local" else "path2")
                # What a pass leaves to clean up: a conflict's older copy, and the
                # files of a folder deleted on one machine and changed on the
                # other (the deletion wins; bisync's per-file report tells such a
                # folder from a single deleted file, and when it cannot be read
                # nothing is moved). Both are moved out, and the next pass takes
                # the removal to the cloud. A pass only finds new conflicts if
                # someone edits the same file again while it runs; after three
                # such passes the copies are still moved out here, and the next
                # `lit sync` removes them from the cloud. A full comparison
                # (first sync, --first-from) deletes nothing, so it has no
                # deleted folders to settle.
                run_name = backups[-1]
                found = sweep_conflicts(vault, paths.conflicts_root / run_name)
                conflicts.extend(found)
                if mode == "sync":
                    deltas = parse_bisync_deltas(first.report)
                    if deltas is None:
                        deletions_unchecked = True
                    else:
                        deleted_folders = resolve_deleted_folder_conflicts(
                            vault,
                            find_deleted_folder_conflicts(deltas, local_snapshot(vault)),
                            paths.conflicts_root / run_name,
                        )
                sweep_partials(vault, paths.local_backup_root / run_name)
                # Conflicts whose TAXONOMY.md values are not added back yet.
                pending = list(found)
                needs_pass = bool(found or deleted_folders)
                extra_passes = 0
                while extra_passes < 4:
                    if not needs_pass:
                        # Values a TAXONOMY.md conflict dropped are added back only
                        # once a pass settles nothing new. After a conflict where
                        # the cloud's copy won, rclone's listings leave the file
                        # out until a pass finds both copies equal; written back
                        # before that, it differs on both sides and comes back as
                        # a second conflict.
                        needs_pass, error = _restore_taxonomy(vault, pending, restored)
                        restore_error = restore_error or error
                        pending = []
                        if not needs_pass:
                            break
                    if control.interrupt.is_set():
                        # Asked to stop (the GUI is closing): the next sync
                        # takes the rest to the cloud.
                        break
                    # A TAXONOMY.md written back above is locked again; Windows
                    # would refuse to replace it.
                    ensure_truth_unlocked(vault)
                    run_pass(None)
                    extra_passes += 1
                    found = sweep_conflicts(vault, paths.conflicts_root / backups[-1])
                    conflicts.extend(found)
                    pending += found
                    # The folder of a deletion that won comes back empty: in the
                    # pass that met the deletion, rclone recorded the folder as
                    # gone on both sides while it re-created it to hold the changed
                    # file, so the next pass copies it back as new. Remove it again;
                    # one more pass then deletes it everywhere for good.
                    shells = _empty_folder_shells(vault, deleted_folders)
                    for shell in shells:
                        _remove_empty_dirs(shell)
                    needs_pass = bool(found or shells)
                # Still a pass to make: the loop was stopped or ran out.
                unsent = needs_pass
                if pending:
                    # The passes ran out first (someone kept editing): add the
                    # values back anyway; the next `lit sync` takes them up.
                    changed_here, error = _restore_taxonomy(vault, pending, restored)
                    restore_error = restore_error or error
                    unsent = unsent or changed_here
            finally:
                ensure_truth_locked(vault)
                for name in backups:
                    _drop_if_empty(paths.local_backup_root / name)

        # What went up so far is the other machines' to know now: the git
        # step below can take minutes, and a run cut off there (an agent's
        # time limit, Ctrl+C) would otherwise leave no stamp at all.
        stamp_written = stamps.finish(
            remote_changed=any(o.remote_changed for o in ops),
            may_write=not control.interrupt.is_set(),
        )

        # Code checkouts follow the commits their repo-meta.yaml records; git
        # runs with TRUTH locked again, and before the rebuild, which links
        # the checkouts into their projects. Not when the GUI is closing: a
        # clone can take minutes, and the next sync brings them in.
        if not (control.skip_git.is_set() or control.interrupt.is_set()):
            repos, recorded, repos_error = _bring_repos_in(
                vault, paths, progress=git_progress, error=repos_error
            )
        if recorded and not control.interrupt.is_set():
            # A commit recorded here for the first time goes up in this same
            # sync: a machine syncing next finds it and follows, instead of
            # recording its own, which would conflict.
            stamps.mark_pending()
            passes_before = len(ops)
            with hold_pass_lock(paths.workdir):
                try:
                    ensure_truth_unlocked(vault)
                    run_pass(None)
                    found = sweep_conflicts(vault, paths.conflicts_root / backups[-1])
                    conflicts.extend(found)
                    changed_here, error = _restore_taxonomy(vault, found, restored)
                    restore_error = restore_error or error
                    unsent = bool(found) or changed_here
                finally:
                    ensure_truth_locked(vault)
                    _drop_if_empty(paths.local_backup_root / backups[-1])
            stamp_written = stamps.finish(
                remote_changed=any(o.remote_changed for o in ops[passes_before:]),
                may_write=not control.interrupt.is_set(),
            ) or stamp_written
    except SyncChoiceRequiredError:
        _stamp_quietly(vault, "needs-choice")
        raise
    except SyncError:
        _stamp_quietly(vault, "failed")
        raise

    after = local_snapshot(vault)
    # A change made here while a pass ran, which the pass then replaced or
    # deleted: rclone kept it in the pass's backup folder. Reported as a
    # conflict the cloud's copy won.
    conflicts.extend(
        _replaced_during(
            vault, before, [paths.local_backup_root / name for name in backups], ops
        )
    )
    conflict_paths = frozenset(
        [c.path for c in conflicts]
        + [f"{d.folder}/{rel}" for d in deleted_folders for rel in d.moved]
    )
    # A repo-meta.yaml this sync recorded a commit in changed here, not there.
    recorded_paths = frozenset(f"codes/{name}/repo-meta.yaml" for name in recorded)
    # litman's own fix-ups changed here too, and went up in a later pass.
    fixups = conflict_paths | recorded_paths
    unsynced, recheck = _changed_during(
        before, after, ops, fixups, _path1_listing(paths.workdir)
    )
    # Fix-ups no pass took up: the next look syncs once more for them.
    recheck = recheck or unsent
    # What the next sync compares with: what this one left, except that a file
    # changed here meanwhile keeps its state from before, so it still reads as
    # a change to take up.
    saved = dict(after)
    for rel in unsynced:
        if rel in before:
            saved[rel] = before[rel]
        else:
            saved.pop(rel, None)
    try:
        _write_snapshot(paths.snapshot_file, saved, recheck=recheck)
    except OSError:
        # Only the next run's "went up" count and the GUI's auto sync need it;
        # the count is then reported as not counted, and the auto sync syncs.
        pass
    came_down = diff_snapshots(before, after, ignore=fixups | frozenset(unsynced))
    went_up: FileChanges | None
    if mode == "sync":
        went_up = (
            diff_snapshots(baseline, before, ignore=conflict_paths)
            if baseline is not None
            else None
        )
    elif mode == "first-local" and first_from is None:
        went_up = diff_snapshots({}, before)
    else:
        went_up = None
    remote_changed = any(o.remote_changed for o in ops)
    _stamp_quietly(vault, "conflicts" if conflicts or deleted_folders else "ok")

    rebuild: dict[str, Any] | None = None
    rebuild_error: str | None = None
    try:
        from litman.core.correctors import reconcile_derived

        # Held off the GUI's saves: one landing while INDEX.json is rebuilt
        # from a read taken before it would be missing from the new one.
        with hold_pass_lock(paths.workdir):
            rebuild = reconcile_derived(vault, project_refs=True)
    except (LitmanError, OSError) as err:
        rebuild_error = str(err)

    used_backups = tuple(
        paths.local_backup_root / name
        for name in backups
        if _dir_has_entries(paths.local_backup_root / name)
    )
    return TwoWayResult(
        target_url=target_url,
        mode=mode,
        chosen=first_from is not None,
        came_down=came_down,
        went_up=went_up,
        conflicts=tuple(conflicts),
        remote_changed=remote_changed,
        local_backups=used_backups,
        projects_without_folder=_projects_without_folder(vault),
        rebuild=rebuild,
        rebuild_error=rebuild_error,
        deleted_folders=tuple(deleted_folders),
        taxonomy_restored=restored,
        taxonomy_restore_error=restore_error,
        deletions_unchecked=deletions_unchecked,
        repos=repos,
        repos_error=repos_error,
        repos_recorded=tuple(recorded),
        unsynced=unsynced,
        stamp_written=stamp_written,
    )


def _changed_during(
    before: Snapshot,
    after: Snapshot,
    ops: list[BisyncOps],
    fixups: frozenset[str],
    listing: Snapshot | None,
) -> tuple[tuple[str, ...], bool]:
    """The files that changed here during a sync, and not through it.

    Returns them, and whether such files could not be told apart from the
    ones the sync wrote here (a full comparison, or a report that cannot be
    read): then the next look must sync anyway. ``fixups`` are litman's own
    changes, which went up in a later pass. A file the sync wrote here is one
    of them too when it no longer is as rclone's ``listing`` of this side
    records it: something wrote it after rclone did.
    """
    diff = diff_snapshots(before, after, ignore=fixups)
    changed = {*diff.added, *diff.changed, *diff.deleted}
    if not changed:
        return (), False
    writes: set[str] = set()
    for o in ops:
        if o.path1_writes is None:
            return (), True
        writes |= o.path1_writes
    unsynced = changed - writes
    written_here = changed & writes
    if written_here:
        if listing is None:
            return tuple(sorted(unsynced)), True
        unsynced |= {rel for rel in written_here if listing.get(rel) != after.get(rel)}
    return tuple(sorted(unsynced)), False


#: A file's line in rclone bisync's listing of one side: size, hash, ID,
#: modification time, quoted path.
_LISTING_LINE_RE = re.compile(
    r'^- +(\d+) \S+ \S+ '
    r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?"
    r'(Z|[+-]\d\d:?\d\d) (".*"|\S.*)$'
)


def _listing_time_ns(match: re.Match[str]) -> int:
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(2, 8))
    fraction = match.group(8) or ""
    zone = match.group(9)
    offset = 0
    if zone != "Z":
        digits = zone[1:].replace(":", "")
        offset = (int(digits[:2]) * 60 + int(digits[2:])) * 60
        if zone[0] == "-":
            offset = -offset
    seconds = calendar.timegm((year, month, day, hour, minute, second)) - offset
    return seconds * 1_000_000_000 + int(fraction.ljust(9, "0"))


def _path1_listing(workdir: Path) -> Snapshot | None:
    """``{path: (size, mtime_ns)}`` of the files rclone recorded on this side after the run.

    ``None`` when the listing cannot be found or read.
    """
    try:
        names = [n for n in os.listdir(workdir) if n.endswith(".path1.lst")]
    except OSError:
        return None
    if len(names) != 1:
        return None
    out: Snapshot = {}
    try:
        with open(workdir / names[0], encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line.startswith("-"):
                    continue
                match = _LISTING_LINE_RE.match(line)
                if match is None:
                    return None
                rel = _unquote_bisync_path(match.group(10))
                if rel is None:
                    return None
                out[rel] = (int(match.group(1)), _listing_time_ns(match))
    except (OSError, ValueError, OverflowError):
        return None
    return out


def _replaced_during(
    vault: Path, before: Snapshot, backup_dirs: list[Path], ops: list[BisyncOps]
) -> list[SyncConflict]:
    """Changes made here while a pass ran that the pass then replaced or deleted.

    rclone keeps every file it replaces or deletes on this side in the pass's
    backup folder. One that is not as it was before the sync changed in
    between: rclone listed it first, the cloud's copy then went over it. A
    later pass's backup of a file an earlier pass wrote is that pass's own
    copy, and is left out; so are the passes after one whose writes here
    cannot be told.
    """
    rows: list[SyncConflict] = []
    written: set[str] = set()
    for backup_dir, run_ops in zip(backup_dirs, ops, strict=False):
        for dirpath, _dirnames, filenames in os.walk(backup_dir):
            rel_dir = os.path.relpath(dirpath, backup_dir).replace(os.sep, "/")
            prefix = "" if rel_dir == "." else rel_dir + "/"
            for name in filenames:
                rel = prefix + name
                if rel in written or _CONFLICT_SUFFIX_RE.search(rel):
                    continue
                if _two_way_file_excluded(rel, name):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if before.get(rel) == (st.st_size, st.st_mtime_ns):
                    continue
                try:
                    kept_mtime: float | None = os.stat(vault / rel).st_mtime
                except OSError:
                    kept_mtime = None
                parts = rel.split("/")
                rows.append(
                    SyncConflict(
                        path=rel,
                        paper_id=parts[1] if len(parts) >= 3 and parts[0] == "papers" else None,
                        kept="cloud",
                        kept_mtime=kept_mtime,
                        moved_mtime=st.st_mtime,
                        backup=Path(full),
                    )
                )
        if run_ops.path1_writes is None:
            break
        written |= run_ops.path1_writes
    return rows

def _bring_repos_in(
    vault: Path, paths: BisyncPaths, *, progress: bool, error: str | None
) -> tuple[RepoSyncReport | None, list[str], str | None]:
    """Bring the code checkouts to their recorded commits; record the commits missing.

    Returns the report, the repos whose commit was recorded here for the first
    time, and the first error met (``error``, when there was one already).
    """
    from litman.core import code_sync
    from litman.core.code import record_repo_heads

    try:
        repos = code_sync.bring_repos_in(vault, depth=_clone_depth(vault), progress=progress)
        code_sync.write_last_heads(paths.repo_heads_file, repos.heads)
    except (LitmanError, OSError) as err:
        return None, [], error or str(err)
    if not repos.unrecorded:
        return repos, [], error
    try:
        recorded = record_repo_heads(vault, repos.unrecorded, op_id="sync-record-commits")
    except (LitmanError, OSError) as err:
        return repos, [], error or str(err)
    return repos, recorded, error


def _clone_depth(vault: Path) -> int:
    """This vault's ``default_clone_depth``; the default when its config cannot be read."""
    from litman.core.code import DEFAULT_CLONE_DEPTH
    from litman.core.config import load_config

    try:
        return load_config(vault).default_clone_depth
    except (LitmanError, OSError):
        return DEFAULT_CLONE_DEPTH


def _restore_taxonomy(
    vault: Path, found: list[SyncConflict], restored: dict[str, tuple[str, ...]]
) -> tuple[bool, str | None]:
    """Bring back the TAXONOMY.md values ``found``'s conflicts dropped, into ``restored``.

    Returns whether TAXONOMY.md changed, and the error text if that failed.
    """
    older = [c.backup for c in found if c.path == "TAXONOMY.md"]
    if not older:
        return False, None
    try:
        added = restore_dropped_taxonomy_values(vault, older)
    except (LitmanError, OSError) as err:
        return False, str(err)
    for name, values in added.items():
        restored[name] = tuple(sorted({*restored.get(name, ()), *values}))
    return bool(added), None


def _dir_has_entries(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


def _stamp_quietly(vault: Path, result: str) -> None:
    """Record how a sync ended; a state-file problem must not hide the outcome."""
    try:
        stamp_two_way(vault, result)
    except OSError:
        pass
