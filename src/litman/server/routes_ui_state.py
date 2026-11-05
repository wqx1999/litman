"""GUI-state endpoints for the litman webUI: pins, what's-new dismissal, the
UI language preference.

This state is GUI working state, persisted in the machine-level
``ui-state.json`` beside the vault registry (:mod:`litman.core.ui_state`) —
NOT in the vault. Nothing here touches metadata / INDEX / TAXONOMY / views or
any file under the vault root, so invariant #16's closed direct-write
whitelist is not involved: this is the same class of write as the GUI's
default-agent setting.

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

The what's-new endpoint is the odd one out: it needs no vault (the welcome
page can pop the card too) and it takes no body — the server records the
version IT is running. See :func:`put_whatsnew_seen`. The language
preference needs no vault either: the welcome page renders in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Request

from litman.core.id import is_valid_id
from litman.core.ui_state import (
    load_pins,
    load_ui_lang,
    save_pins,
    save_ui_lang,
    save_whatsnew_seen,
)

router = APIRouter(prefix="/api")


def _vault(request: Request) -> Path:
    return cast(Path, request.app.state.vault)


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


@router.put("/whatsnew/seen")
def put_whatsnew_seen() -> dict[str, str]:
    """Mark the RUNNING release's what's-new card as seen. Idempotent.

    Takes no body on purpose: the version recorded is the server's own
    ``__version__``, never one the client supplies. A client that sent the
    wrong string — a stale bundle, a hand-rolled request — would silence the
    card for a release the user never read, or re-pop it forever; making the
    value unspeakable removes that whole class of bug.

    Needs no vault (it is listed in the server's vaultless allowlist): the
    welcome page has no library yet and must still be able to dismiss the card.
    """
    from litman import __version__

    save_whatsnew_seen(__version__)
    return {"seen": __version__}


@router.get("/ui-lang")
def get_ui_lang() -> dict[str, str]:
    """The GUI language preference: ``auto`` (follow the system), ``en``, ``zh``.

    Machine-level, beside the what's-new marker, so the app window and a
    browser tab agree (each has its own ``localStorage``). Needs no vault.
    """
    return {"lang": load_ui_lang()}


@router.put("/ui-lang")
async def put_ui_lang(request: Request) -> dict[str, str]:
    """Record the GUI language preference from ``{"lang": "auto"|"en"|"zh"}``.

    400 for a body that is not that shape, 422 for an unknown value.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    lang = payload.get("lang") if isinstance(payload, dict) else None
    if not isinstance(lang, str):
        raise HTTPException(status_code=400, detail="'lang' must be a string.")
    try:
        save_ui_lang(lang)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"lang": lang}
