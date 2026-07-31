"""Pin (pinned-paper) endpoints for the litman webUI (task-gui-pin).

Pins are GUI working state, persisted in the machine-level ``ui-state.json``
beside the vault registry (:mod:`litman.core.ui_state`) — NOT in the vault.
Nothing here touches metadata / INDEX / TAXONOMY / views or any file under
the vault root, so invariant #16's closed direct-write whitelist is not
involved: this is the same class of write as the GUI's default-agent setting.

Contract with the SPA:

* Every write returns the full post-write list — the client renders the
  server's order verbatim (list order IS pin order, oldest first) and never
  splices locally, so front- and backend can't disagree on ordering.
* PUT / DELETE are idempotent: re-pinning keeps the original position,
  unpinning an unpinned id is a 200 no-op. The GUI toggles state; repeating
  a toggle must never error or reshuffle.
* GET prunes dangling pins (papers since removed by ``lit rm``) and writes
  the pruned list back — pruning only ever drops pin ENTRIES, never touches
  ``papers/``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from litman.core.id import is_valid_id
from litman.core.ui_state import load_pins, save_pins

router = APIRouter(prefix="/api")


def _vault(request: Request) -> Path:
    return request.app.state.vault


def _pruned_pins(vault: Path) -> list[str]:
    """The vault's pins with dangling ids dropped (and persisted if any were).

    A pin dangles when ``papers/<id>/`` no longer exists (the paper was
    removed). Pin counts are single-digit, so the per-id stat is noise.
    """
    pins = load_pins(vault)
    alive = [p for p in pins if (vault / "papers" / p).is_dir()]
    if alive != pins:
        save_pins(vault, alive)
    return alive


def _require_valid_id(paper_id: str) -> None:
    """Reject traversal-style ids (mirrors the pdf/write routes) → 404."""
    if not is_valid_id(paper_id):
        raise HTTPException(
            status_code=404, detail=f"Invalid paper id: {paper_id!r}."
        )


@router.get("/pins")
def get_pins(request: Request) -> dict[str, list[str]]:
    """The active vault's pinned paper ids, oldest pin first, pruned."""
    return {"pins": _pruned_pins(_vault(request))}


@router.put("/pins/{paper_id}")
def put_pin(request: Request, paper_id: str) -> dict[str, list[str]]:
    """Pin ``paper_id`` (append to the end; idempotent — re-pinning an
    already-pinned paper keeps its position). The paper must exist."""
    _require_valid_id(paper_id)
    vault = _vault(request)
    if not (vault / "papers" / paper_id).is_dir():
        raise HTTPException(
            status_code=404, detail=f"No paper {paper_id!r} in this vault."
        )
    pins = _pruned_pins(vault)
    if paper_id not in pins:
        pins = [*pins, paper_id]
        save_pins(vault, pins)
    return {"pins": pins}


@router.delete("/pins/{paper_id}")
def delete_pin(request: Request, paper_id: str) -> dict[str, list[str]]:
    """Unpin ``paper_id`` (idempotent — unpinning an unpinned id is a 200)."""
    _require_valid_id(paper_id)
    vault = _vault(request)
    pins = _pruned_pins(vault)
    if paper_id in pins:
        pins = [p for p in pins if p != paper_id]
        save_pins(vault, pins)
    return {"pins": pins}


@router.delete("/pins")
def clear_pins(request: Request) -> dict[str, list[str]]:
    """Clear every pin for the active vault (the Pinned group's Clear all)."""
    save_pins(_vault(request), [])
    return {"pins": []}
