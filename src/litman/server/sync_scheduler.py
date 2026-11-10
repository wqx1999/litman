"""The GUI's auto sync: one thread per ``lit gui`` that keeps the served library synced.

It runs the same two-way sync as ``lit sync`` (:func:`litman.core.sync.two_way_sync`),
one at a time, in its own thread, so a sync needs no page and no focused tab. It
is active only when the library has ``sync:`` set up, this machine has synced
it once with ``lit sync`` (the scheduler never runs a first sync), and rclone
is usable; otherwise its status says why.

It syncs:

* when the server starts, and when the page asks (a click on the status mark);
* once a change here has been still for :data:`SETTLE_S` seconds. A cheap stat
  token (:func:`litman.core.vault_token.synced_token`) notices it; the exact
  comparison with the last sync's record confirms it first;
* when another machine synced: every :data:`STAMP_EVERY_S` seconds it reads
  the small stamp file on the remote, and syncs when it changed;
* once more after a sync during which files changed here;
* before the server exits, when files changed here since the last sync
  (:meth:`SyncScheduler.final_sync`, run by ``lit gui`` itself).

A failure that only the user can settle (a sign-in that expired, rclone's
safety stop, a choice of side) stops it until a click, a successful
``lit sync`` in a terminal, or the next start. Another sync running is retried
after :data:`BUSY_RETRY_S`; anything else is taken as offline and retried
after 2, 4, 8, then every 15 minutes.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from litman.core import sync as core_sync
from litman.core.config import CONFIG_FILENAME, load_config
from litman.core.sync_lock import SharedHold, read_holder
from litman.core.vault_token import fingerprint, synced_token
from litman.exceptions import LitmanError, SyncError, SyncNotSetUpError

#: How often the thread looks at the library, at least. A slow disk stretches
#: it: the stat walk is kept under 2% of the time (:meth:`SyncScheduler._tick`).
TICK_MIN_S = 5.0
#: How long a change must stay still before it is synced.
SETTLE_S = 60.0
#: How often the remote stamp is read, give or take :data:`STAMP_JITTER`.
STAMP_EVERY_S = 120.0
STAMP_JITTER = 0.1
#: When another sync holds the library's lock.
BUSY_RETRY_S = 30.0
#: The pauses after offline failures in a row; the last one repeats.
BACKOFF_S: tuple[float, ...] = (120.0, 240.0, 480.0, 900.0)
#: The exit sync's limits: rclone is asked to stop at the first, killed at the second.
EXIT_INTERRUPT_S = 30.0
EXIT_BUDGET_S = 60.0
#: After a sync is killed, how long it gets to put the library back in order
#: (the read-only lock, its own lock files) before the process ends.
EXIT_GRACE_S = 10.0


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else text


@dataclass
class _Library:
    """What the scheduler knows of the library it serves, and of its remote."""

    vault: Path
    config_fp: str | None = None
    target_url: str | None = None
    remote: str | None = None
    workdir: Path | None = None
    #: rclone checked usable for this remote; None until checked again.
    rclone_ok: bool | None = None
    rclone_error: str | None = None
    active: bool = False
    pending_start: bool = True
    manual: bool = False
    last_token: str | None = None
    token_since: float = 0.0
    settled_token: str | None = None
    #: The last sync's record says files changed here during it.
    dirty: bool = False
    next_stamp_check: float = 0.0
    busy_retry_at: float | None = None
    backoff_step: int = 0
    backoff_until: float | None = None
    #: Stopped on an error that needs the user, and the last sync's record then:
    #: a sync that goes through in a terminal rewrites it.
    stopped: bool = False
    stopped_record: str | None = None

    def reset_remote(self, target_url: str | None, remote: str | None) -> None:
        self.target_url = target_url
        self.remote = remote
        self.workdir = (
            core_sync.sync_workdir(self.vault.resolve(), target_url)
            if target_url is not None
            else None
        )
        self.rclone_ok = None
        self.rclone_error = None
        self.active = False
        self.pending_start = True
        self.settled_token = None
        self.dirty = False
        self.next_stamp_check = 0.0
        self.busy_retry_at = None
        self.backoff_step = 0
        self.backoff_until = None
        self.stopped = False


@dataclass
class _Status:
    state: str = "idle"
    remote: str | None = None
    last_ok_at: str | None = None
    next_try_at: str | None = None
    running_since: str | None = None
    reason: str | None = None
    message: str | None = None
    seq: int = 0
    notice: dict[str, Any] | None = None
    #: Who holds the library's sync lock, while the auto sync waits for it.
    holder: dict[str, str | None] | None = None
    #: How many code repos the last sync found with changes only on this
    #: machine (:func:`_repos_with_work`).
    repos_with_work: int = 0
    vault: Path | None = None


class SyncScheduler:
    """The auto sync of the library ``app_state.vault`` names, in one thread."""

    def __init__(
        self,
        app_state: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._app_state = app_state
        self._clock = clock
        self._wall = wall
        self._library: _Library | None = None
        self._status = _Status()
        self._guard = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._control: core_sync.SyncControl | None = None
        self._exiting = False
        self._scan_s = 0.0
        self._recent: list[str] = []
        #: Set by the self-update route: the relaunched GUI syncs on its start.
        self.self_update_pending = False
        #: The sync itself; tests may wrap it.
        self.sync_fn: Callable[..., core_sync.TwoWayResult] = core_sync.two_way_sync

    # -- the thread ----------------------------------------------------------

    def start(self) -> None:
        """Start the thread (``lit gui``'s lifespan; never a test's bare app)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="litman-auto-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to end after what it is doing; does not wait."""
        self._stop.set()
        self._wake.set()

    @property
    def started(self) -> bool:
        return self._thread is not None

    def _loop(self) -> None:
        # No terminal answers this thread's rclone: an encrypted config's
        # password prompt would block it for good.
        with core_sync.rclone_never_asks():
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    self.step()
                except Exception as err:  # the thread must outlive any one bad look
                    self._set(state="error", reason="internal", message=str(err))
                self._wake.wait(self._tick())

    def _tick(self) -> float:
        return max(TICK_MIN_S, 50 * self._scan_s)

    def request_sync(self) -> dict[str, Any]:
        """Sync now (the page's click): also clears a stop and checks rclone again."""
        with self._guard:
            lib = self._library
            if lib is not None:
                lib.manual = True
                lib.stopped = False
                lib.rclone_ok = None
        self._wake.set()
        return self.status()

    # -- one look --------------------------------------------------------------

    def step(self, now: float | None = None) -> None:
        """Look at the library once, and sync if it is due (in this thread)."""
        now = self._clock() if now is None else now
        vault = getattr(self._app_state, "vault", None)
        lib = self._library
        if lib is None or lib.vault != vault:
            self._switch(vault)
            lib = self._library
        if lib is None or self._exiting:
            return
        if not self._read_config(lib):
            return
        if not self._gate(lib):
            return
        reason = self._due(lib, now)
        if reason is not None:
            self._run(lib, reason)

    def _switch(self, vault: Path | None) -> None:
        old = self._library
        if old is not None and old.active and old.target_url is not None:
            # A change left on the library the page switched away from gets
            # one sync now; nothing else of it is watched any more.
            try:
                if core_sync.local_changes_pending(old.vault, old.target_url):
                    # Through self._control, like any other sync: the exit
                    # sync can then stop it.
                    control = core_sync.SyncControl(own_group=True)
                    if self._exiting:
                        control.skip_git.set()
                    self._control = control
                    try:
                        self.sync_fn(
                            old.vault, old.target_url, lock_wait=0,
                            control=control, by="lit gui", trigger="auto",
                        )
                    finally:
                        self._control = None
            except (LitmanError, OSError):
                pass
        self._library = _Library(vault=vault) if vault is not None else None
        with self._guard:
            seq = self._status.seq
            self._status = _Status(seq=seq, vault=vault)

    def _read_config(self, lib: _Library) -> bool:
        config_fp = fingerprint(lib.vault / CONFIG_FILENAME)
        if config_fp != lib.config_fp:
            lib.config_fp = config_fp
            try:
                configured = load_config(lib.vault).sync
            except (LitmanError, OSError) as err:
                lib.reset_remote(None, None)
                self._set(state="error", reason="config", message=str(err))
                return False
            target = configured.target_url() if configured is not None else None
            remote = configured.remote if configured is not None else None
            if target != lib.target_url:
                lib.reset_remote(target, remote)
        if lib.target_url is None:
            self._set(state="not-configured", reason=None, message=None)
            return False
        return True

    def _gate(self, lib: _Library) -> bool:
        assert lib.target_url is not None and lib.workdir is not None
        if lib.rclone_ok is None:
            try:
                core_sync.require_bisync_rclone()
                if lib.remote is not None and not core_sync.remote_exists(lib.remote):
                    raise SyncError(
                        f"rclone has no remote named {lib.remote!r} any more. "
                        "Set it up again: `lit sync setup`."
                    )
            except SyncError as err:
                lib.rclone_ok, lib.rclone_error = False, str(err)
                # No run starts: the record says why, for `lit sync status`.
                core_sync.record_run(
                    lib.vault, core_sync.run_from_error(err, "auto", reason="rclone")
                )
            else:
                lib.rclone_ok, lib.rclone_error = True, None
        if not lib.rclone_ok:
            lib.active = False
            self._set(state="error", reason="rclone", message=lib.rclone_error)
            return False
        if not core_sync.bisync_initialised(lib.workdir):
            lib.active = False
            self._set(state="not-initialised", reason=None, message=None)
            return False
        if not lib.active:
            lib.active = True
            synced = self._status.state in ("ok", "conflicts")
            self._set(
                state=self._status.state if synced else "idle",
                reason=None,
                message=None,
                last_ok_at=self._last_sync(lib),
            )
        return True

    def _sync_record(self, lib: _Library) -> str:
        """The fingerprint of the last sync's record, which only a sync that went through writes."""
        if lib.workdir is None:
            return "-"
        return fingerprint(lib.workdir / core_sync.SNAPSHOT_NAME)

    def _last_sync(self, lib: _Library) -> str | None:
        try:
            return core_sync.read_sync_state(lib.vault).last_sync
        except (LitmanError, OSError):
            return None

    def _recorded_work(self, lib: _Library) -> int:
        """How many code repos the last run's record found with changes only on this machine."""
        try:
            run = core_sync.read_sync_state(lib.vault).last_run
        except (LitmanError, OSError):
            return 0
        return run.repos_with_work_total if run is not None else 0

    def _due(self, lib: _Library, now: float) -> str | None:
        """Why to sync now, or None."""
        assert lib.target_url is not None and lib.workdir is not None
        if lib.manual:
            return "manual"
        if lib.stopped:
            # A `lit sync` that went through in a terminal settles it too.
            if self._sync_record(lib) == lib.stopped_record:
                return None
            lib.stopped = False
            self._set(
                state="ok",
                reason=None,
                message=None,
                last_ok_at=self._last_sync(lib),
                notice=None,
                repos_with_work=self._recorded_work(lib),
            )
        if lib.busy_retry_at is not None:
            if now < lib.busy_retry_at:
                return None
            lib.busy_retry_at = None
            return "retry"
        if lib.backoff_until is not None:
            if now < lib.backoff_until:
                return None
            lib.backoff_until = None
            return "retry"
        if lib.pending_start:
            return "start"
        started = time.perf_counter()
        token = synced_token(lib.vault)
        self._scan_s = time.perf_counter() - started
        if token != lib.last_token:
            lib.last_token, lib.token_since = token, now
        if (lib.dirty or token != lib.settled_token) and now - lib.token_since >= SETTLE_S:
            if lib.dirty or core_sync.local_changes_pending(lib.vault, lib.target_url):
                return "local"
            lib.settled_token = token
        if now >= lib.next_stamp_check:
            lib.next_stamp_check = now + STAMP_EVERY_S * random.uniform(
                1 - STAMP_JITTER, 1 + STAMP_JITTER
            )
            try:
                check = core_sync.check_remote_stamp(lib.workdir, lib.target_url)
            except SyncError as err:
                refused = core_sync.as_auth_failure(err, lib.target_url)
                if refused is not err:
                    # The cloud refused the sign-in: auto sync stops, and like a
                    # sync it stopped, the record says why (`lit sync status`,
                    # the line after other commands).
                    core_sync.record_run(lib.vault, core_sync.run_from_error(refused, "auto"))
                self._failed(lib, refused, now)
                return None
            if check.moved:
                return "remote"
            if check.owed and not self._write_owed_stamp(lib):
                return "remote"
        return None

    def _write_owed_stamp(self, lib: _Library) -> bool:
        """Write the stamp an earlier sync could not, with nothing else to sync.

        False when the stamp moved since the last sync: its changes come down
        first, and that sync writes the owed stamp.
        """
        assert lib.target_url is not None and lib.workdir is not None
        try:
            with core_sync.hold_sync_lock(lib.workdir, by="lit gui", wait=0):
                core_sync.require_still_set_up(lib.vault, lib.target_url)
                return core_sync.write_owed_stamp(lib.workdir, lib.target_url)
        except (LitmanError, OSError):
            return True

    # -- one sync --------------------------------------------------------------

    def _run(self, lib: _Library, reason: str) -> None:
        assert lib.target_url is not None
        lib.manual = False
        lib.pending_start = False
        control = core_sync.SyncControl(own_group=True)
        if self._exiting:
            control.skip_git.set()
        self._control = control
        self._recent = []
        self._set(
            state="running",
            running_since=_iso(self._wall()),
            reason=reason,
            message=None,
            holder=None,
        )
        try:
            result = self.sync_fn(
                lib.vault,
                lib.target_url,
                on_log=self._log,
                git_progress=False,
                lock_wait=0,
                control=control,
                by="lit gui",
                trigger="auto",
            )
        except Exception as err:
            # Anything unforeseen is retried like a network failure, with the
            # same growing pause: never in a tight loop.
            self._failed(lib, err, self._clock())
        else:
            self._succeeded(lib, result, self._clock())
        finally:
            self._control = None
            with self._guard:
                self._status.running_since = None

    def _log(self, line: str) -> None:
        self._recent = [*self._recent[-4:], line]

    def _failed(self, lib: _Library, err: BaseException, now: float) -> None:
        if isinstance(err, SyncNotSetUpError):
            # `lit sync disconnect` or `lit sync setup` changed the library's
            # `sync:` while this sync waited for the lock: look again now.
            lib.config_fp = None
            self._wake.set()
            return
        kind, reason = core_sync.classify_sync_error(err)
        message = str(err)
        if kind == "busy":
            lib.busy_retry_at = now + BUSY_RETRY_S
            holder = read_holder(lib.workdir / core_sync.SYNC_LOCK_NAME) if lib.workdir else None
            self._set(
                state="running",
                reason="busy",
                message=holder.describe() if holder is not None else None,
                holder={"by": holder.by, "since": holder.since} if holder is not None else None,
            )
            return
        if kind == "needs-user":
            lib.stopped = True
            lib.stopped_record = self._sync_record(lib)
            self._set(
                bump_seq=True,
                state="error",
                reason=reason,
                message=message,
                notice={"kind": "stopped", "reason": reason, "message": message},
            )
            return
        lib.rclone_ok = None
        pause = BACKOFF_S[min(lib.backoff_step, len(BACKOFF_S) - 1)]
        lib.backoff_step += 1
        lib.backoff_until = now + pause
        self._set(
            state="offline",
            reason="offline",
            message=_last_line(message),
            next_try_at=_iso(self._wall() + pause),
        )

    def _succeeded(self, lib: _Library, result: core_sync.TwoWayResult, now: float) -> None:
        assert lib.target_url is not None
        lib.backoff_step = 0
        lib.backoff_until = None
        lib.busy_retry_at = None
        lib.stopped = False
        # The follow-up look: files that changed here while the sync ran.
        token = synced_token(lib.vault)
        lib.last_token, lib.token_since = token, now
        lib.dirty = core_sync.local_changes_pending(lib.vault, lib.target_url)
        lib.settled_token = None if lib.dirty else token
        lib.next_stamp_check = now + STAMP_EVERY_S
        notice = _notice(result)
        self._set(
            bump_seq=True,
            state="conflicts" if result.conflicts or result.deleted_folders else "ok",
            reason=None,
            message=None,
            next_try_at=None,
            last_ok_at=self._last_sync(lib) or _iso(self._wall()),
            notice=notice,
            repos_with_work=_repos_with_work(result),
        )

    # -- the exit sync ----------------------------------------------------------

    def final_sync(
        self,
        *,
        budget: float = EXIT_BUDGET_S,
        interrupt_after: float = EXIT_INTERRUPT_S,
        on_start: Callable[[], None] | None = None,
    ) -> bool:
        """Sync once more before ``lit gui`` exits, when files changed here; at most ``budget`` s.

        Waits for a sync in progress first (telling it to skip its git step),
        then syncs when the exact comparison finds files changed since the last
        sync. rclone is asked to stop at ``interrupt_after`` seconds and killed
        at ``budget``; a sync killed then gets :data:`EXIT_GRACE_S` more to put
        the library back in order. Whatever is left, the next start syncs.
        ``on_start`` is told once, before that wait or the sync, so the
        terminal says why the exit takes a moment. A Ctrl+C during the wait
        stops the sync in progress. Not after a self-update (a sync in
        progress is stopped at once: the update's helper waits for this
        process, and the new GUI syncs on its start), nor while the auto sync
        is stopped on an error that needs the user or waits to retry offline.
        Returns whether it synced.
        """
        self._exiting = True
        self.stop()
        with core_sync.rclone_never_asks():
            return self._final_sync(budget, interrupt_after, on_start)

    def _halt(self) -> None:
        """Stop the sync in progress now: rclone is killed."""
        control = self._control
        if control is not None:
            control.interrupt.set()
            control.kill.set()

    def _join(self, timeout: float, tell: Callable[[], None] | None = None) -> None:
        """Wait up to ``timeout`` s for the thread; ``tell`` once a sync is seen running."""
        thread = self._thread
        if thread is None:
            return
        end = time.monotonic() + timeout
        while thread.is_alive():
            left = end - time.monotonic()
            if left <= 0:
                return
            if tell is not None and self._control is not None:
                tell()
            thread.join(min(0.5, left))

    def _final_sync(
        self,
        budget: float,
        interrupt_after: float,
        on_start: Callable[[], None] | None,
    ) -> bool:
        exit_control = core_sync.SyncControl()
        exit_control.skip_git.set()
        running = self._control
        if running is not None:
            running.skip_git.set()
        if self.self_update_pending:
            self._halt()
            self._join(EXIT_GRACE_S)
            return False
        told = False

        def tell() -> None:
            nonlocal told
            if not told:
                told = True
                if on_start is not None:
                    on_start()

        done = threading.Event()

        def cap() -> None:
            if done.wait(interrupt_after):
                return
            for control in (self._control, exit_control):
                if control is not None:
                    control.interrupt.set()
            if done.wait(max(0.0, budget - interrupt_after)):
                return
            for control in (self._control, exit_control):
                if control is not None:
                    control.kill.set()

        threading.Thread(target=cap, name="litman-exit-sync-cap", daemon=True).start()
        try:
            try:
                self._join(budget + EXIT_GRACE_S, tell)
            except KeyboardInterrupt:
                # The sync in progress runs in a process group of its own, so
                # the Ctrl+C did not reach its rclone: stop it here.
                self._halt()
                self._join(EXIT_GRACE_S)
                return False
            lib = self._library
            if (
                self._thread is None
                or self.self_update_pending
                or lib is None
                or not lib.active
                or lib.stopped
                # Offline at the last try: the next start syncs, rather than
                # this exit waiting a minute on a network that is not there.
                or lib.backoff_step > 0
                or lib.target_url is None
                or exit_control.interrupt.is_set()
            ):
                return False
            try:
                if not core_sync.local_changes_pending(lib.vault, lib.target_url):
                    return False
            except (LitmanError, OSError):
                return False
            tell()
            self._control = exit_control
            try:
                self.sync_fn(
                    lib.vault,
                    lib.target_url,
                    lock_wait=0,
                    control=exit_control,
                    by="lit gui",
                    trigger="exit",
                )
            except (LitmanError, OSError):
                return False
            return True
        finally:
            done.set()
            self._control = None

    # -- status ------------------------------------------------------------------

    def _set(self, *, bump_seq: bool = False, **changes: Any) -> None:
        """Change the status in one hold of the guard: a poll reads all of a change or none.

        ``bump_seq`` counts one more sync or stop for the page, which tells
        the ``notice`` it reads with a new ``seq`` once. Counted apart, a poll
        could read the new count with the state still ``running`` and the
        last sync's notice: the page would tell that one again, never this one.
        """
        with self._guard:
            if bump_seq:
                self._status.seq += 1
            for key, value in changes.items():
                setattr(self._status, key, value)
            lib = self._library
            if lib is not None:
                self._status.remote = lib.target_url

    def status(self) -> dict[str, Any]:
        """The auto sync's status, as ``GET /api/sync/status`` answers it."""
        with self._guard:
            st = self._status
            return {
                "state": st.state,
                "remote": st.remote,
                "lastOkAt": st.last_ok_at,
                "nextTryAt": st.next_try_at,
                "runningSince": st.running_since,
                "reason": st.reason,
                "message": st.message,
                "seq": st.seq,
                "notice": st.notice,
                "holder": st.holder,
                "reposWithWork": st.repos_with_work,
            }

    def status_for(self, vault: Path) -> dict[str, Any] | None:
        """The status for the page's poll: ``None`` when ``vault`` has no ``sync:``.

        Before the thread has looked at ``vault``, its configuration says
        whether there is anything to show.
        """
        lib = self._library
        if lib is None or lib.vault != vault or lib.config_fp is None:
            try:
                configured = load_config(vault).sync
            except (LitmanError, OSError):
                return None
            if configured is None:
                return None
            target = configured.target_url()
            synced_here = core_sync.bisync_initialised(
                core_sync.sync_workdir(vault.resolve(), target)
            )
            status = self.status()
            status.update(
                state="idle" if synced_here else "not-initialised",
                remote=target,
                reason=None,
                message=None,
            )
            return status
        if lib.target_url is None:
            return None
        return self.status()


def _notice(result: core_sync.TwoWayResult) -> dict[str, Any] | None:
    """What a sync has to tell the page once: conflicts, deletions that won, repos left behind."""
    conflicts = [
        {"path": c.path, "kept": c.kept, "backup": str(c.backup)} for c in result.conflicts
    ]
    deleted = [
        {"folder": d.folder, "deletedHere": d.deleted_here, "backup": str(d.backup)}
        for d in result.deleted_folders
    ]
    # `kind` is what the page words its row by; `what` and `wayOut` are the
    # server's own sentences, as `lit sync status` prints them.
    repos = [
        {"name": r.name, "kind": r.kind, "what": r.what, "wayOut": r.way_out}
        for r in core_sync.repo_rows(result)
    ]
    if not (conflicts or deleted or repos):
        return None
    return {
        "kind": "synced",
        "conflicts": conflicts,
        "deletedFolders": deleted,
        "reposNotRestored": repos,
    }


def _repos_with_work(result: core_sync.TwoWayResult) -> int:
    """How many code repos ``result`` found with changes only on this machine, for
    the page's line: each repo once, and none the notice names as not synced
    already (a checkout with only untracked files whose move failed is both).

    Not part of the notice: it is no toast and no activity-log row.
    """
    named = {row.name for row in core_sync.repo_rows(result)}
    return sum(1 for work in core_sync.work_rows(result) if work.name not in named)


class SaveGate:
    """Holds the GUI's saves off the library while rclone runs on it.

    One :class:`~litman.core.sync_lock.SharedHold` per ``pass.lock``, made
    when a library with ``sync:`` set up is first saved to. A library without
    ``sync:``, or never synced on this machine, needs none.
    """

    def __init__(self) -> None:
        self._holds: dict[Path, SharedHold] = {}
        self._targets: dict[Path, tuple[str | None, str | None]] = {}
        self._queues: dict[SharedHold, asyncio.Lock] = {}
        self._guard = threading.Lock()

    def queue_for(self, hold: SharedHold) -> asyncio.Lock:
        """The line the saves waiting for ``hold`` stand in: let in as they came.

        Used on the server's event loop only.
        """
        queue = self._queues.get(hold)
        if queue is None:
            queue = self._queues[hold] = asyncio.Lock()
        return queue

    def hold_for(self, vault: Path) -> SharedHold | None:
        config_fp = fingerprint(vault / CONFIG_FILENAME)
        cached = self._targets.get(vault)
        if cached is None or cached[0] != config_fp:
            try:
                configured = load_config(vault).sync
                target = configured.target_url() if configured is not None else None
            except (LitmanError, OSError):
                target = None
            cached = (config_fp, target)
            self._targets[vault] = cached
        target = cached[1]
        if target is None:
            return None
        lock_path = core_sync.pass_lock_path(vault.resolve(), target)
        if not lock_path.parent.is_dir():
            return None
        with self._guard:
            hold = self._holds.get(lock_path)
            if hold is None:
                hold = self._holds[lock_path] = SharedHold(lock_path)
            return hold
