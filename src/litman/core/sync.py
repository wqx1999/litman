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

import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from platformdirs import user_data_dir

from litman.core.dates import now_iso
from litman.core.locking import (
    ensure_truth_locked,
    ensure_truth_unlocked,
    is_file_manager_dropping,
)
from litman.core.replace import replace_file
from litman.core.undecodable import config_refusal
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import LitmanError, SyncChoiceRequiredError, SyncError

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


def run_rclone(
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
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

    Raises:
        SyncError: rclone not installed (re-checked here so unit tests that
            patch PATH don't need to mock both ``rclone_available`` and the
            subprocess), or (when ``check=True``) the command exited non-zero.
    """
    _require_rclone()
    cmd = [RCLONE_BIN, *args]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=capture,
            text=True,
        )
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
    result = run_rclone(["listremotes"])
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


def remote_size(target_url: str) -> Size:
    """Return ``Size`` of the remote location via ``rclone size --json``.

    Args:
        target_url: rclone destination (``"remote:path/"``).

    Raises:
        SyncError: rclone fails (path missing, auth error, etc.). A missing
            remote path returns ``Size(0, 0)`` rather than raising — that
            state is the legitimate "never pushed" case.
    """
    result = run_rclone(
        ["size", target_url, "--json"],
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
    ``needs-choice`` or ``failed``.
    """

    last_push: str | None = None
    last_pull: str | None = None
    last_sync: str | None = None
    last_sync_result: str | None = None


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
    buf = io.StringIO()
    _yaml.dump(payload, buf)
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    replace_file(tmp, path)


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
) -> subprocess.CompletedProcess[str]:
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
    """
    args = ["sync", str(vault), target_url, *build_exclude_args(extra_excludes)]
    if dry_run:
        args.append("--dry-run")
    result = run_rclone(args, capture=False)
    if not dry_run:
        stamp_push(vault)
    return result


def pull(
    vault: Path,
    target_url: str,
    *,
    extra_excludes: tuple[str, ...] = (),
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Mirror ``target_url`` into ``vault`` (``rclone sync`` one-way).

    Used for cross-machine recovery: a fresh machine runs ``lit sync pull``
    against the cloud-side mirror to materialise the vault. Like ``push``,
    ``rclone sync`` deletes destination files (local) absent from the source
    (cloud) — that's *correct* for the cross-machine restore use case.

    NOTE: the destination's ``.litman-sync-state.yaml`` is preserved because
    it's on the default exclude list, so a local machine's prior pull
    history is never clobbered.
    """
    args = ["sync", target_url, str(vault), *build_exclude_args(extra_excludes)]
    if dry_run:
        args.append("--dry-run")
    result = run_rclone(args, capture=False)
    if not dry_run:
        stamp_pull(vault)
        # rclone does not round-trip Unix permissions, so pulled TRUTH files
        # land writable (default 0o644). Re-assert the read-only lock locally
        # (M32). Only the pull path needs this — push is unaffected (0o444 is
        # readable, uploads fine).
        ensure_truth_locked(vault)
    return result


@dataclass(frozen=True)
class StatusReport:
    """Snapshot of local + remote vault state, returned by ``compute_status``."""

    target_url: str
    local: Size
    remote: Size
    state: SyncState

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
) -> StatusReport:
    """Gather local + remote sizes + state-file timestamps into one report.

    Two rclone calls (``listremotes`` was already done by the caller; here
    we only do ``size``). Local enumeration is a pure filesystem walk.
    """
    local = local_vault_size(
        vault,
        excludes=(*DEFAULT_EXCLUDES, *extra_excludes),
    )
    remote = remote_size(target_url)
    state = read_sync_state(vault)
    return StatusReport(
        target_url=target_url,
        local=local,
        remote=remote,
        state=state,
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
    data["sync"] = {
        "remote": payload.remote,
        "path": payload.path,
        "exclude_repos": payload.exclude_repos,
    }
    message = config_refusal(data)
    if message is not None:
        raise SyncError(message)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    replace_file(tmp, config_path)


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
        return self.workdir / "litman-snapshot.json"

    @property
    def conflicts_root(self) -> Path:
        """Where the older copy of every conflict goes, one folder per sync."""
        return self.local_backup_root / "conflicts"

    @property
    def repo_heads_file(self) -> Path:
        """The commit each code checkout was at when this machine's last sync ended."""
        return self.workdir / "repo-heads.json"


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


def bisync_paths(vault: Path, target_url: str) -> BisyncPaths:
    """The state and backup locations for ``vault`` synced with ``target_url``."""
    key = vault_key(vault, target_url)
    data = sync_data_dir()
    return BisyncPaths(
        workdir=data / "bisync" / key,
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


def rclone_version() -> tuple[int, int, int] | None:
    """The installed rclone's version, or ``None`` if it cannot be read.

    Goes through :func:`run_rclone`, so a missing rclone raises its usual
    install hint and tests can fake the version there.
    """
    result = run_rclone(["version"], check=False)
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


def _read_snapshot(path: Path) -> Snapshot | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        files = payload["files"]
        return {str(rel): (int(v[0]), int(v[1])) for rel, v in files.items()}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def _write_snapshot(path: Path, snapshot: Snapshot) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"files": {rel: list(v) for rel, v in sorted(snapshot.items())}}
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    replace_file(tmp, path)


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
    the user.
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
        "--stats", "30s", "--stats-one-line", "--stats-log-level", "NOTICE",
    ]
    if resync_mode is not None:
        args += ["--resync-mode", resync_mode]
    if force:
        args.append("--force")
    return args


@dataclass(frozen=True)
class BisyncRun:
    """One ``rclone bisync`` run that ended.

    ``lines`` are its log lines at NOTICE and above, the ones the user sees;
    ``report`` its INFO lines, the per-file report that
    :func:`parse_bisync_deltas` reads among them. rclone's date and time are
    taken off every line.
    """

    returncode: int
    lines: tuple[str, ...]
    report: tuple[str, ...]


def _bisync_env() -> dict[str, str]:
    """This process's environment, less the variables that change rclone's log."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _RCLONE_LOG_ENV_NAMES
        and not key.upper().startswith(_RCLONE_LOG_ENV_PREFIXES)
    }


def run_bisync(
    args: list[str], on_log: Callable[[str], None] | None = None
) -> BisyncRun:
    """Run ``rclone <args>``; hand each line the user should see to ``on_log`` as it comes.

    A log record is a line with rclone's date and time, plus the lines after
    it that have none (a JSON dump, say). Records below NOTICE go to the
    run's ``report`` and are not shown, except lines with no date that end
    the output after one: those are not part of it but a crash report, say,
    and are shown once rclone has exited. On Ctrl+C, rclone (same process
    group) gets the interrupt too and saves its state; it is given time for
    that before the interrupt goes on.
    """
    _require_rclone()
    try:
        proc = subprocess.Popen(
            [RCLONE_BIN, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_bisync_env(),
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
            show(line)
        returncode = proc.wait()
        for line in held:
            show(line)
    except BaseException:
        try:
            proc.communicate(timeout=100)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
        raise
    return BisyncRun(returncode, tuple(lines), tuple(report))


def _choice_message(reason: str) -> str:
    return (
        f"{reason} Pick the winner: `lit sync --first-from cloud` or "
        "`lit sync --first-from local` (files that differ on the other side go "
        "to the backup)."
    )


def _bisync_failure(returncode: int, lines: list[str], workdir: Path) -> SyncError:
    """Turn a failed bisync run into the error the user sees."""
    text = "\n".join(lines).lower()
    tail = "\n".join([line for line in lines if line.strip()][-5:])
    if "too many deletes" in text:
        here, there = (
            ("in the cloud", "on this machine")
            if re.search(r"too many deletes[^\n]*on path2", text)
            else ("on this machine", "in the cloud")
        )
        return SyncError(
            f"rclone stopped: more than half of the files were deleted {here}, so "
            f"nothing was deleted {there}. If that is intended, run "
            f"`lit sync --force`; the deleted files go to the backup.\n{tail}"
        )
    if "all files were changed" in text:
        return SyncError(
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
        return SyncError(_lock_message(workdir))
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


def _reserve_backup_name(local_root: Path) -> str:
    """Name, and create, a fresh local backup folder for one bisync run.

    Created up front because bisync creates it only when it backs something
    up, and the same name is used for the cloud-side folder: two runs within
    one second must not share it. An unused folder is removed afterwards
    (:func:`_drop_if_empty`).
    """
    base = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    name, n = base, 2
    while True:
        try:
            (local_root / name).mkdir()
            return name
        except FileExistsError:
            name = f"{base}-{n}"
            n += 1


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


def two_way_sync(
    vault: Path,
    target_url: str,
    *,
    first_from: FirstFrom | None = None,
    force: bool = False,
    on_log: Callable[[str], None] | None = None,
    git_progress: bool = False,
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

    Every file a run replaces or deletes goes to a backup folder: on this
    machine under ``<data dir>/sync-backup/<vault-key>/``, in the cloud under
    ``<path>-litman-backup/``, one folder per run.

    Raises:
        SyncChoiceRequiredError: the run needs the user's ``--first-from``.
        SyncError: rclone is missing or too old, its record names would be
            too long, the remote cannot be listed, or bisync failed (its last
            log lines are in the message).
    """
    vault = vault.resolve()
    require_bisync_rclone()
    paths = bisync_paths(vault, target_url)
    initialised = bisync_initialised(paths.workdir)
    if not initialised:
        # rclone has not written these record names yet; nothing is written
        # before they are known to fit.
        require_bisync_names_fit(vault, target_url)
    paths.workdir.mkdir(parents=True, exist_ok=True)
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
    before = local_snapshot(vault)
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
        )
        run = run_bisync(args, on_log)
        if run.returncode != 0:
            raise _bisync_failure(run.returncode, list(run.lines), paths.workdir)
        return run

    try:
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
            if pending:
                # The passes ran out first (someone kept editing): add the
                # values back anyway; the next `lit sync` takes them up.
                _changed, error = _restore_taxonomy(vault, pending, restored)
                restore_error = restore_error or error
        finally:
            ensure_truth_locked(vault)
            for name in backups:
                _drop_if_empty(paths.local_backup_root / name)

        # Code checkouts follow the commits their repo-meta.yaml records; git
        # runs with TRUTH locked again, and before the rebuild, which links
        # the checkouts into their projects.
        repos, recorded, repos_error = _bring_repos_in(
            vault, paths, progress=git_progress, error=repos_error
        )
        if recorded:
            # A commit recorded here for the first time goes up in this same
            # sync: a machine syncing next finds it and follows, instead of
            # recording its own, which would conflict.
            try:
                ensure_truth_unlocked(vault)
                run_pass(None)
                found = sweep_conflicts(vault, paths.conflicts_root / backups[-1])
                conflicts.extend(found)
                _changed, error = _restore_taxonomy(vault, found, restored)
                restore_error = restore_error or error
            finally:
                ensure_truth_locked(vault)
                _drop_if_empty(paths.local_backup_root / backups[-1])
    except SyncChoiceRequiredError:
        _stamp_quietly(vault, "needs-choice")
        raise
    except SyncError:
        _stamp_quietly(vault, "failed")
        raise

    after = local_snapshot(vault)
    try:
        _write_snapshot(paths.snapshot_file, after)
    except OSError:
        # Only the next run's "went up" count needs it; that count is then
        # reported as not counted.
        pass
    conflict_paths = frozenset(
        [c.path for c in conflicts]
        + [f"{d.folder}/{rel}" for d in deleted_folders for rel in d.moved]
    )
    # A repo-meta.yaml this sync recorded a commit in changed here, not there.
    recorded_paths = frozenset(f"codes/{name}/repo-meta.yaml" for name in recorded)
    came_down = diff_snapshots(before, after, ignore=conflict_paths | recorded_paths)
    went_up: FileChanges | None
    if mode == "sync":
        went_up = (
            diff_snapshots(baseline, before, ignore=conflict_paths)
            if baseline is not None
            else None
        )
        remote_changed = (
            went_up is None or went_up.total > 0 or bool(conflicts) or bool(deleted_folders)
        )
    elif mode == "first-local" and first_from is None:
        went_up = diff_snapshots({}, before)
        remote_changed = bool(before) or bool(conflicts)
    else:
        went_up = None
        remote_changed = True
    remote_changed = remote_changed or bool(recorded)
    _stamp_quietly(vault, "conflicts" if conflicts or deleted_folders else "ok")

    rebuild: dict[str, Any] | None = None
    rebuild_error: str | None = None
    try:
        from litman.core.correctors import reconcile_derived

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
    )


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
