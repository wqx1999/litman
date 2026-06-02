"""rclone-backed vault sync helpers (M6.1).

Pure helpers around the ``rclone`` binary plus the per-machine sync-state
file. The CLI layer in ``litman.commands.sync`` is intentionally thin; all
subprocess invocation, parsing, and state-file IO lives here so it can be
unit-tested without going through Click.

Design choices baked in (per M6 spec + ADR-002, ADR-003):

- **One-way only.** ``push`` and ``pull`` map to ``rclone sync`` in opposite
  directions; both wipe extraneous files on the destination so the two ends
  stay byte-for-byte. No bisync, no conflict resolution.
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

import io
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from litman.core.dates import now_iso
from litman.core.locking import ensure_truth_locked
from litman.exceptions import SyncError

RCLONE_BIN = "rclone"
SYNC_STATE_FILENAME = ".litman-sync-state.yaml"

# Filters applied to every push / pull regardless of user config. All three
# are unconditionally machine-local or derived:
#   - ``.litman-staging/`` holds in-flight atomic writes (must never leak
#     across machines);
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
    "views/**",
    SYNC_STATE_FILENAME,
)

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False


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

    Both timestamps are local-machine ISO 8601 strings (or ``None`` for
    "never"). The file is excluded from sync, so each machine keeps its own
    record without clobbering peers.
    """

    last_push: str | None = None
    last_pull: str | None = None


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
    last_push = data.get("last-push")
    last_pull = data.get("last-pull")
    return SyncState(
        last_push=str(last_push) if last_push else None,
        last_pull=str(last_pull) if last_pull else None,
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
    }
    buf = io.StringIO()
    _yaml.dump(payload, buf)
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    tmp.replace(path)


def stamp_push(vault: Path) -> None:
    """Update ``last-push`` to now, preserving ``last-pull``."""
    state = read_sync_state(vault)
    write_sync_state(
        vault,
        SyncState(last_push=now_iso(), last_pull=state.last_pull),
    )


def stamp_pull(vault: Path) -> None:
    """Update ``last-pull`` to now, preserving ``last-push``."""
    state = read_sync_state(vault)
    write_sync_state(
        vault,
        SyncState(last_push=state.last_push, last_pull=now_iso()),
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
            wrong, mid-edit by another process, etc.).
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
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    tmp.replace(config_path)


# ---------------------------------------------------------------------------
# Status report rendering helpers (CLI consumes these)
# ---------------------------------------------------------------------------


def format_iso(ts: str | None) -> str:
    """Render a stored ISO timestamp or ``"(never)"`` for ``None``."""
    return ts if ts else "(never)"
