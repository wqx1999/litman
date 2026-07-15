"""PresenceTracker unit tests (task-window-presence-gate AC1 + AC11 support).

The tracker is the window watcher's memory: how many pages hold the
``/api/presence`` socket open, whether any page ever did, and when the count
last dropped to zero. Two threads use it in production — uvicorn's event
loop mutates, the watcher polls — so the concurrency tests hammer it from
many threads and assert the bookkeeping can neither go negative nor feed the
watcher's idle subtraction a ``None`` alongside ``count == 0`` with a
consistent snapshot (the TypeError that would silently kill the watcher).
"""

from __future__ import annotations

import threading
import time

from litman.core.presence import PresenceTracker


def test_tracker_starts_empty_and_never_connected() -> None:
    tracker = PresenceTracker()
    assert tracker.count == 0
    assert tracker.ever_connected is False
    assert tracker.last_zero is None
    assert tracker.snapshot() == (0, False, None)


def test_connect_counts_and_is_remembered_forever() -> None:
    tracker = PresenceTracker()
    tracker.connect()
    assert tracker.count == 1
    assert tracker.ever_connected is True
    tracker.connect()
    assert tracker.count == 2
    tracker.disconnect()
    tracker.disconnect()
    # ever_connected is history, not state: it survives the count going back
    # to zero (it is what routes the watcher to linger instead of grace).
    assert tracker.count == 0
    assert tracker.ever_connected is True


def test_last_disconnect_stamps_the_idle_clock() -> None:
    tracker = PresenceTracker()
    tracker.connect()
    tracker.connect()
    tracker.disconnect()
    # One page still up: not idle.
    assert tracker.last_zero is None
    before = time.monotonic()
    tracker.disconnect()
    after = time.monotonic()
    assert tracker.last_zero is not None
    assert before <= tracker.last_zero <= after


def test_reconnect_clears_the_idle_clock() -> None:
    # F5: the reloaded page reconnects. A tracker holding a live page must
    # not look idle, whatever happened before.
    tracker = PresenceTracker()
    tracker.connect()
    tracker.disconnect()
    assert tracker.last_zero is not None
    tracker.connect()
    assert tracker.snapshot() == (1, True, None)


def test_duplicate_disconnect_clamps_to_zero() -> None:
    # However a connection's teardown gets double-counted, the count must
    # never go negative — a negative count would wedge the gate open forever.
    tracker = PresenceTracker()
    tracker.disconnect()
    assert tracker.count == 0
    tracker.connect()
    tracker.disconnect()
    tracker.disconnect()
    assert tracker.count == 0


def test_concurrent_connects_and_disconnects_balance_out() -> None:
    tracker = PresenceTracker()
    rounds = 200

    def _page() -> None:
        for _ in range(rounds):
            tracker.connect()
            tracker.disconnect()

    threads = [threading.Thread(target=_page) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert tracker.snapshot() == (0, True, tracker.last_zero)
    assert tracker.last_zero is not None


def test_snapshot_never_feeds_the_watcher_a_typeerror() -> None:
    # The watcher's gate expression, evaluated against live snapshots while
    # writers churn: a consistent snapshot may show (0, True, None) only as
    # a one-poll transient, and the None guard makes even that harmless —
    # what must be impossible is `None` reaching the idle subtraction.
    tracker = PresenceTracker()
    stop = threading.Event()
    failures: list[BaseException] = []

    def _watcherlike() -> None:
        try:
            while not stop.is_set():
                count, ever_connected, last_zero = tracker.snapshot()
                assert count >= 0
                if count == 0 and ever_connected and last_zero is not None:
                    time.monotonic() - last_zero  # must never raise
        except BaseException as exc:  # the assertion payload
            failures.append(exc)

    def _page() -> None:
        for _ in range(500):
            tracker.connect()
            tracker.disconnect()

    reader = threading.Thread(target=_watcherlike)
    writers = [threading.Thread(target=_page) for _ in range(4)]
    reader.start()
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    stop.set()
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert failures == []
