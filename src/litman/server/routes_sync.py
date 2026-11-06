"""The GUI's auto sync, seen from the page: its status, and "sync now".

Both read or poke the scheduler ``lit gui`` runs
(:class:`litman.server.sync_scheduler.SyncScheduler`); neither syncs in the
request. The status also rides along on ``GET /api/vault-version``, so the
page needs no timer of its own.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api")

_NOT_CONFIGURED = (
    "This library has no cloud sync set up. Set it up in a terminal: `lit sync setup`."
)
_NOT_INITIALISED = (
    "This machine hasn't synced this library yet. Run `lit sync` once in a terminal."
)


@router.get("/sync/status")
def get_sync_status(request: Request) -> dict[str, Any]:
    """``{state, remote, lastOkAt, nextTryAt, runningSince, reason, message, seq, notice}``.

    ``state`` is ``not-configured`` for a library with no ``sync:``.
    """
    scheduler = request.app.state.sync_scheduler
    status: dict[str, Any] | None = scheduler.status_for(request.app.state.vault)
    if status is None:
        return {"state": "not-configured", "remote": None, "seq": 0, "notice": None}
    return status


@router.post("/sync", status_code=202)
def post_sync(request: Request) -> dict[str, Any]:
    """Sync now: the click on the status mark. Also clears a stop on an error.

    409 with the way out when there is nothing the auto sync can run yet.
    """
    scheduler = request.app.state.sync_scheduler
    status: dict[str, Any] | None = scheduler.status_for(request.app.state.vault)
    if status is None:
        raise HTTPException(status_code=409, detail=_NOT_CONFIGURED)
    if status.get("state") == "not-initialised":
        raise HTTPException(status_code=409, detail=_NOT_INITIALISED)
    status = scheduler.request_sync()
    return {"queued": True, "state": status["state"]}
