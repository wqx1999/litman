"""The SI window's requests, held in memory by the running GUI (ADR-027).

A request is "this paper wants its SI": created by ``lit add --si <paper>``
(``source="cli"``) or by the page's own Add SI (``source="page"``), shown by
the page as the SI window, and ended by Done or Cancel. Files dropped into the
window become its rows; nothing reaches the library until Done.

States::

    pending ──confirm──▶ confirming ──ok──▶ added
       ▲                     │
       └──────failed─────────┘
    pending ──cancel──▶ cancelled

``added`` and ``cancelled`` are final. An open request (pending or
confirming) is forgotten after a day without activity — its upload stash is
gone by then too (the 24 h sweep) — and a finished one 30 minutes after it
ended, which is how long a late ``lit add --si`` rerun can still collect it.

Each finished result reaches the CLI once. The CLI marks a request it waits
on (``awaited``); reading its end state with ``collect`` marks it
``delivered``. A rerun for the same paper is handed a finished request only
while it is awaited and not yet delivered — the result the agent never saw —
and otherwise gets a fresh request, so "show it again" after a Cancel really
shows the window again. A request the page made (its own Add SI) is
handed over this way only if a CLI joined it while it was open — that CLI
waited on it, and a later rerun gets the result it missed. One no CLI ever
waited on never reaches the CLI.

Everything here is memory: one lock, held only to read or change state. The
routes do every disk operation — stash writes and deletes, verification, the
library write — outside it. Pruning drops entries from memory only, so a GET
that prunes still writes nothing.

Pure Python on purpose (no fastapi): the state machine is tested on its own.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litman.core.fs_identity import Anchor

# The one clock, replaced by tests to step past the two lifetimes.
_clock = time.monotonic

OPEN_TTL_S = 24 * 3600.0
FINISHED_TTL_S = 30 * 60.0

PENDING = "pending"
CONFIRMING = "confirming"
ADDED = "added"
CANCELLED = "cancelled"
_OPEN = (PENDING, CONFIRMING)


class RequestError(Exception):
    """A request cannot do what was asked; ``str()`` says why."""


class UnknownRequest(RequestError):
    """No such request (never made, or forgotten)."""


class RequestConflict(RequestError):
    """The request's state or library forbids it; ``request`` is its snapshot."""

    def __init__(self, message: str, request: dict[str, Any]) -> None:
        super().__init__(message)
        self.request = request


class BadSelection(RequestError):
    """What Done was asked to add is not addable."""


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class SiRow:
    """One file dropped into the window, stashed and verified."""

    id: str
    name: str
    stash: Path
    size: int
    verdict: str
    check: str | None
    reason: str
    other_id: str | None = None
    sha256: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "bytes": self.size,
            "verdict": self.verdict,
            "check": self.check,
            "reason": self.reason,
            "otherId": self.other_id,
        }


@dataclass
class SiRequest:
    id: str
    vault: Path
    anchor: Anchor
    paper_id: str
    title: str
    link: str | None
    source: str
    touched: float
    status: str = PENDING
    rows: list[SiRow] = field(default_factory=list)
    added: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    finished: float | None = None
    awaited: bool = False
    delivered: bool = False

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN

    def snapshot(self, served: Anchor | None) -> dict[str, Any]:
        return {
            "id": self.id,
            "paperId": self.paper_id,
            "title": self.title,
            "link": self.link,
            "status": self.status,
            "vaultServed": served is not None and self.anchor.same_as(served),
            "rows": [row.to_json() for row in self.rows],
            "added": [dict(entry) for entry in self.added],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ConfirmPlan:
    """What Done will add: each chosen row with the user's "add anyway"."""

    request_id: str
    vault: Path
    paper_id: str
    chosen: list[tuple[SiRow, bool]]
    # "?" rows left unticked: dropped, their stash deleted, once Done succeeds.
    dropped: list[SiRow]


class SiRequestTable:
    """Every request this GUI holds, oldest first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, SiRequest] = {}

    # -- bookkeeping (call with the lock held) ------------------------------

    def _prune(self, now: float) -> None:
        for rid, req in list(self._requests.items()):
            if req.is_open:
                expired = now - req.touched > OPEN_TTL_S
            else:
                expired = req.finished is None or now - req.finished > FINISHED_TTL_S
            if expired:
                del self._requests[rid]

    def _get(self, rid: str, now: float) -> SiRequest:
        self._prune(now)
        req = self._requests.get(rid)
        if req is None:
            raise UnknownRequest(
                "This SI request is unknown or has expired. "
                "Start it again with `lit add --si` or Add SI."
            )
        return req

    @staticmethod
    def _conflict(req: SiRequest, served: Anchor | None, message: str) -> RequestConflict:
        return RequestConflict(message, req.snapshot(served))

    def _require(
        self, req: SiRequest, served: Anchor | None, *, states: tuple[str, ...]
    ) -> None:
        if req.status not in states:
            raise self._conflict(req, served, _STATE_WORDS[req.status])
        if served is None or not req.anchor.same_as(served):
            raise self._conflict(
                req,
                served,
                "This SI request is for another library than the one shown. "
                "Switch back to that library, or cancel the request.",
            )

    # -- creating and reading ------------------------------------------------

    def open(
        self,
        *,
        vault: Path,
        anchor: Anchor,
        paper_id: str,
        title: str,
        link: str | None,
        source: str,
    ) -> dict[str, Any]:
        """The request for ``paper_id`` this caller should follow (snapshot).

        1. One already open for the paper in this library: that one (a CLI
           caller now awaits it, whoever made it).
        2. For the CLI only: one that ended within the last 30 minutes, that
           a CLI awaited and that has not been delivered — the result a timed
           out ``lit add --si`` never saw, page-made ones included once a CLI
           joined them. Handing it over delivers it.
        3. Otherwise a new one.
        """
        now = _clock()
        with self._lock:
            self._prune(now)
            mine = [
                req
                for req in self._requests.values()
                if req.paper_id == paper_id and req.anchor.same_as(anchor)
            ]
            for req in mine:
                if req.is_open:
                    if source == "cli":
                        req.awaited = True
                    req.touched = now
                    return req.snapshot(anchor)
            if source == "cli":
                unseen = [
                    req for req in mine if req.awaited and not req.delivered
                ]
                if unseen:
                    req = max(unseen, key=lambda r: r.finished or 0.0)
                    req.delivered = True
                    return req.snapshot(anchor)
            req = SiRequest(
                id=new_id(),
                vault=vault,
                anchor=anchor,
                paper_id=paper_id,
                title=title,
                link=link,
                source=source,
                touched=now,
                awaited=source == "cli",
            )
            self._requests[req.id] = req
            return req.snapshot(anchor)

    def snapshot(
        self, rid: str, served: Anchor | None, *, collect: bool = False
    ) -> dict[str, Any]:
        """The request's state; ``collect`` delivers a finished one."""
        with self._lock:
            req = self._get(rid, _clock())
            if collect and not req.is_open:
                req.delivered = True
            return req.snapshot(served)

    def has_open(self) -> bool:
        with self._lock:
            self._prune(_clock())
            return any(req.is_open for req in self._requests.values())

    def first_open(self, served: Anchor) -> dict[str, Any] | None:
        """The oldest open request of the library shown, for the page."""
        with self._lock:
            self._prune(_clock())
            for req in self._requests.values():
                if req.is_open and req.anchor.same_as(served):
                    return {
                        "id": req.id,
                        "paperId": req.paper_id,
                        "title": req.title,
                        "link": req.link,
                    }
            return None

    # -- rows ------------------------------------------------------------------

    def upload_target(self, rid: str, served: Anchor | None) -> tuple[Path, str]:
        """``(vault, paper_id)`` a file dropped into the request goes to.

        Raises:
            UnknownRequest, RequestConflict: no such request, not pending, or
                another library is shown.
        """
        with self._lock:
            req = self._get(rid, _clock())
            self._require(req, served, states=(PENDING,))
            return req.vault, req.paper_id

    def add_row(
        self, rid: str, served: Anchor | None, row: SiRow
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Add a verified row; returns ``(row, request)`` snapshots.

        A file identical to one already in the window is rejected here, under
        the lock, so two drops racing each other cannot both pass. The caller
        deletes the stash of a rejected row.
        """
        with self._lock:
            now = _clock()
            req = self._get(rid, now)
            self._require(req, served, states=(PENDING,))
            if row.verdict != "rejected" and row.sha256:
                twin = next(
                    (
                        other
                        for other in req.rows
                        if other.verdict != "rejected" and other.sha256 == row.sha256
                    ),
                    None,
                )
                if twin is not None:
                    row.verdict, row.check = "rejected", None
                    row.reason = f"same file as {twin.name}"
                    row.other_id = None
            req.rows.append(row)
            req.touched = now
            return row.to_json(), req.snapshot(served)

    def remove_row(
        self, rid: str, fid: str, served: Anchor | None
    ) -> tuple[SiRow | None, dict[str, Any]]:
        """Take a row out; ``None`` when there was no such row."""
        with self._lock:
            now = _clock()
            req = self._get(rid, now)
            self._require(req, served, states=(PENDING,))
            row = next((r for r in req.rows if r.id == fid), None)
            if row is not None:
                req.rows.remove(row)
            req.touched = now
            return row, req.snapshot(served)

    # -- Done ------------------------------------------------------------------

    def begin_confirm(
        self, rid: str, served: Anchor | None, add_anyway: list[str]
    ) -> ConfirmPlan | dict[str, Any]:
        """Move to ``confirming`` and say what to add.

        Every ✓ row goes in; a "?" row only when listed in ``add_anyway``.
        Returns the request's snapshot instead when it was added already
        (a repeated Done).

        Raises:
            UnknownRequest, RequestConflict: no such request; confirming,
                cancelled, or another library shown.
            BadSelection: ``add_anyway`` names an unknown or rejected row, or
                nothing would be added.
        """
        with self._lock:
            now = _clock()
            req = self._get(rid, now)
            if req.status == ADDED:
                return req.snapshot(served)
            self._require(req, served, states=(PENDING,))
            rows = {row.id: row for row in req.rows}
            ticked = set(add_anyway)
            for fid in ticked:
                row = rows.get(fid)
                if row is None:
                    raise BadSelection(
                        f"No file {fid!r} in this SI request. Reopen the SI window and try again."
                    )
                if row.verdict == "rejected":
                    raise BadSelection(
                        f"{row.name} was refused ({row.reason}). Untick it, then press Done."
                    )
            chosen: list[tuple[SiRow, bool]] = []
            dropped: list[SiRow] = []
            for row in req.rows:
                if row.verdict == "ok":
                    chosen.append((row, False))
                elif row.verdict == "unverified":
                    if row.id in ticked:
                        chosen.append((row, True))
                    else:
                        dropped.append(row)
            if not chosen:
                raise BadSelection(
                    "Nothing to add yet. Drop a file, "
                    "or tick add anyway on one that can't be confirmed."
                )
            req.status = CONFIRMING
            req.touched = now
            return ConfirmPlan(req.id, req.vault, req.paper_id, chosen, dropped)

    def finish_confirm(
        self,
        rid: str,
        served: Anchor | None,
        added: list[dict[str, Any]],
        warnings: list[str],
    ) -> dict[str, Any]:
        """``confirming`` → ``added``."""
        with self._lock:
            now = _clock()
            req = self._requests[rid]
            req.status = ADDED
            req.added = [dict(entry) for entry in added]
            req.warnings = list(warnings)
            req.finished = now
            req.touched = now
            return req.snapshot(served)

    def abort_confirm(
        self,
        rid: str,
        served: Anchor | None,
        *,
        drop: set[str] | None = None,
        refreshed: dict[str, SiRow] | None = None,
    ) -> dict[str, Any]:
        """``confirming`` → ``pending``, leaving out the rows in ``drop`` and
        replacing those in ``refreshed`` (same ids, new verdicts)."""
        with self._lock:
            req = self._requests[rid]
            if drop or refreshed:
                kept: list[SiRow] = []
                for row in req.rows:
                    if drop and row.id in drop:
                        continue
                    kept.append((refreshed or {}).get(row.id, row))
                req.rows = kept
            req.status = PENDING
            req.touched = _clock()
            return req.snapshot(served)

    # -- Cancel ----------------------------------------------------------------

    def cancel(
        self, rid: str, served: Anchor | None
    ) -> tuple[list[Path], dict[str, Any]]:
        """``pending`` → ``cancelled``; returns the stashes to delete.

        Cancelling twice is fine. Whichever library is shown: a window the
        user closes after switching libraries still ends its request.

        Raises:
            UnknownRequest, RequestConflict: no such request; confirming or
                added.
        """
        with self._lock:
            now = _clock()
            req = self._get(rid, now)
            if req.status == CANCELLED:
                return [], req.snapshot(served)
            if req.status != PENDING:
                raise self._conflict(req, served, _STATE_WORDS[req.status])
            stashes = [row.stash for row in req.rows]
            req.status = CANCELLED
            req.finished = now
            req.touched = now
            return stashes, req.snapshot(served)


_STATE_WORDS = {
    PENDING: "This SI request is still open. Press Done or Cancel first.",
    CONFIRMING: "These SI files are being added right now. Wait a moment, then try again.",
    ADDED: "These SI files were added already. Use Add SI to add more.",
    CANCELLED: "This SI request was cancelled. Use Add SI to start a new one.",
}


__all__ = [
    "ADDED",
    "CANCELLED",
    "CONFIRMING",
    "FINISHED_TTL_S",
    "OPEN_TTL_S",
    "PENDING",
    "BadSelection",
    "ConfirmPlan",
    "RequestConflict",
    "RequestError",
    "SiRequest",
    "SiRequestTable",
    "SiRow",
    "UnknownRequest",
    "new_id",
]
