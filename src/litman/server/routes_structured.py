"""Structured-write API endpoints for the litman webUI (Phase 3b).

These are the invariant #16 SECOND-class writes: the cockpit's structured
metadata changes (status / priority / type, topics/methods/data tags,
read/revisit stamps). Unlike the first-class direct writes in
``routes_write.py`` (paper.pdf / notes.md / discussion.md), these NEVER touch
metadata / INDEX / TAXONOMY directly. Every handler imports and calls the same
``lit`` command backend the CLI uses, so the structured write goes through the
identical validation + atomic staged_write + derived-recompute path — the GUI
opens no second write path (invariant #16, extension of invariant #1).

The vault is read from ``request.app.state.vault`` (set by
:func:`litman.server.create_app`), so nothing here assumes a path. Backend
errors are mapped to HTTP status with the raw message preserved as ``detail``
so the GUI toast shows it verbatim (e.g. the "a revisit presupposes a first
read" ModifyError).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from litman.commands.modify import _apply_modify
from litman.commands.read import apply_read
from litman.commands.revisit import apply_revisit
from litman.core.dates import today_iso, validate_iso_date
from litman.core.id import is_valid_id
from litman.exceptions import ModifyError, PaperNotFoundError

router = APIRouter(prefix="/api")


def _require_valid_id(paper_id: str) -> None:
    """Reject a traversal-style id before any backend call (mirrors routes_write)."""
    if not is_valid_id(paper_id):
        raise HTTPException(status_code=404, detail=f"Invalid paper id: {paper_id!r}.")


def _ops_from_tag_map(tag_map: object, flag: str) -> tuple[str, ...]:
    """Flatten an ``{key: [values]}`` map into ``("key=value", ...)`` specs.

    The cockpit sends ``addTag`` / ``rmTag`` as ``{field: [values]}`` (one or
    more chips at once); ``_apply_modify`` consumes a flat tuple of ``key=value``
    strings. A malformed body (not a dict of lists of strings) is a client bug
    → 400.
    """
    if not isinstance(tag_map, dict):
        raise HTTPException(status_code=400, detail=f"{flag} must be an object.")
    ops: list[str] = []
    for key, values in tag_map.items():
        if not isinstance(values, list):
            raise HTTPException(
                status_code=400, detail=f"{flag}[{key!r}] must be a list of values."
            )
        for value in values:
            if not isinstance(value, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"{flag}[{key!r}] values must be strings.",
                )
            ops.append(f"{key}={value}")
    return tuple(ops)


def _resolve_date(payload: object) -> str:
    """Pull an optional ``{date: "YYYY-MM-DD"}`` out of the body, default today.

    Reuses ``core.dates.validate_iso_date`` (the strict ``YYYY-MM-DD`` shape the
    CLI enforces) so a webUI-supplied date can't drift from the CLI's contract.
    An absent / empty body defaults to today, matching ``lit read`` / ``lit
    revisit`` with no ``--date``.
    """
    if not isinstance(payload, dict):
        return today_iso()
    date_str = payload.get("date")
    if not date_str:
        return today_iso()
    if not isinstance(date_str, str):
        raise HTTPException(status_code=400, detail="date must be a string.")
    try:
        return validate_iso_date(date_str)
    except Exception as exc:  # click.BadParameter on a malformed date
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _optional_json(request: Request) -> object:
    """Parse the request body as JSON, treating an empty body as ``None``.

    The read/revisit endpoints take an OPTIONAL body, so a bodyless POST is
    legitimate (default = today). A non-empty body that is not valid JSON is a
    client bug → 400.
    """
    body = await request.body()
    if not body:
        return None
    try:
        return await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc


@router.put("/paper/{paper_id}/metadata")
async def put_metadata(request: Request, paper_id: str) -> dict[str, object]:
    """Apply a structured metadata change through the ``lit modify`` backend.

    Body JSON (all optional, combined in one transaction):
        ``{"set": {field: value}, "addTag": {key: [values]},
           "rmTag": {key: [values]}}``

    Carries the cockpit's status/priority/type dropdown changes (``set``) and
    topics/methods/data chip add/remove (``addTag`` / ``rmTag``). Translated
    into ``_apply_modify``'s tuple-of-``key=value`` arg shape and dispatched
    with ``skip_set_noop=True`` so re-selecting the current value is a true
    no-op (no spurious ``updated-at`` bump). An empty ``value`` in ``set``
    (e.g. ``{"priority": ""}``) unsets the field to None — ``_apply_modify``
    coerces ``""`` to None and the fixed-enum gate allows it for priority/type.

    ``_apply_modify`` does ALL validation (fixed-enum range, TAXONOMY register-
    first for tags, date ordering) and the atomic write + INDEX/views recompute,
    so this handler adds no second write path. A rejected op surfaces its raw
    message: ModifyError → 400, PaperNotFoundError → 404.
    """
    _require_valid_id(paper_id)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    set_block = payload.get("set", {})
    if not isinstance(set_block, dict):
        raise HTTPException(status_code=400, detail="set must be an object.")
    # Stringify scalar values: _apply_modify._parse_kv expects "key=value"
    # strings and re-coerces (int / None) itself. None → "" so the unset path
    # (priority/type → null) works. Only true scalars are accepted — a list /
    # object value would otherwise be written as its Python repr ("[1, 2]"),
    # corrupting a free-text field; reject it at the boundary, symmetric with the
    # per-value type check `_ops_from_tag_map` applies to addTag / rmTag. (bool
    # is a subclass of int, so it is covered by the int branch.)
    set_op_list: list[str] = []
    for field, value in set_block.items():
        if value is not None and not isinstance(value, (str, int, float)):
            raise HTTPException(
                status_code=400,
                detail=f"set[{field!r}] must be a scalar (string, number, or null).",
            )
        set_op_list.append(f"{field}={'' if value is None else value}")
    set_ops = tuple(set_op_list)
    add_tag_ops = _ops_from_tag_map(payload.get("addTag", {}), "addTag")
    rm_tag_ops = _ops_from_tag_map(payload.get("rmTag", {}), "rmTag")

    if not (set_ops or add_tag_ops or rm_tag_ops):
        raise HTTPException(
            status_code=400,
            detail="Body must contain at least one of set / addTag / rmTag.",
        )

    vault = request.app.state.vault
    try:
        changed = _apply_modify(
            vault,
            paper_id,
            set_ops=set_ops,
            add_tag_ops=add_tag_ops,
            rm_tag_ops=rm_tag_ops,
            skip_set_noop=True,
        )
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModifyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "changed": changed}


@router.post("/paper/{paper_id}/read")
async def post_read(request: Request, paper_id: str) -> dict[str, object]:
    """Stamp ``read-date`` through the ``lit read`` backend (idempotent).

    Optional body ``{"date": "YYYY-MM-DD"}`` (default today). Reuses the exact
    ``lit read`` semantics via :func:`litman.commands.read.apply_read`: read-date
    is the immutable first-read stamp, so an already-read paper is a no-op (NOT
    an error) returning the "already read on …" notice. The cockpit's mutually-
    exclusive read/revisit state machine relies on ``changed`` to refresh.
    """
    _require_valid_id(paper_id)
    date_value = _resolve_date(await _optional_json(request))

    vault = request.app.state.vault
    try:
        changed, message = apply_read(vault, paper_id, date_value)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModifyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "changed": changed, "message": message}


@router.post("/paper/{paper_id}/revisit")
async def post_revisit(request: Request, paper_id: str) -> dict[str, object]:
    """Stamp ``last-revisited`` through the ``lit revisit`` backend.

    Optional body ``{"date": "YYYY-MM-DD"}`` (default today). Reuses ``lit
    revisit`` semantics via :func:`litman.commands.revisit.apply_revisit`: a
    revisit presupposes a first read, so a paper with no ``read-date`` raises
    ModifyError → 400 with the raw "a revisit presupposes a first read" message
    (the mutually-exclusive state machine, enforced server-side).
    """
    _require_valid_id(paper_id)
    date_value = _resolve_date(await _optional_json(request))

    vault = request.app.state.vault
    try:
        apply_revisit(vault, paper_id, date_value)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModifyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
