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

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from litman.commands.modify import _apply_modify
from litman.commands.read import apply_read, apply_unread
from litman.commands.revisit import apply_revisit
from litman.core.config import load_config
from litman.core.dates import today_iso, validate_iso_date
from litman.core.id import is_valid_id
from litman.core.project_link import (
    LinkError,
    add_project,
    link_paper_to_project,
    remove_project,
    unlink_paper_from_project,
)
from litman.core.taxonomy import add_taxonomy_values, remove_taxonomy_value
from litman.core.vault_registry import apply_vault_use
from litman.exceptions import (
    ModifyError,
    PaperNotFoundError,
    TaxonomyError,
    VaultRegistryError,
)

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
        # `projects` is a USER_DICT that `_apply_modify` would happily append to,
        # but a project membership is more than a metadata list entry: it owns the
        # `litman_reflib/<id>` symlink + REFERENCES.md that only
        # link_paper_to_project / unlink_paper_from_project keep consistent. Let
        # the generic metadata endpoint write `projects` and you get a half-linked
        # paper (metadata says linked, no symlink) — a drift the GUI must not be
        # able to create. Route projects through the dedicated link endpoints.
        if key == "projects":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{flag} cannot write 'projects'. Use POST/DELETE "
                    "/api/paper/{id}/project so the symlink + REFERENCES.md stay "
                    "consistent."
                ),
            )
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


@router.post("/paper/{paper_id}/unread")
async def post_unread(request: Request, paper_id: str) -> dict[str, object]:
    """Clear read-date (+ dependent last-revisited) through the modify backend.

    The guarded reversal of ``POST /read`` — the "I mis-clicked Mark read"
    repair. Reuses :func:`litman.commands.read.apply_unread`, which clears both
    stamps in ONE atomic ``_apply_modify`` (the date-ordering guard forbids a
    lone ``last-revisited``, so the two must clear together; any revisit record
    is discarded — the cockpit's confirm dialog, default-No, warns about that
    loss). read-date is immutable-by-default (invariant #11); this constrained
    undo is the GUI's front-door repair, while the CLI's stays ``lit modify``.

    No body. An already-unread paper is a no-op (``changed: False``), not an
    error. ModifyError → 400, PaperNotFoundError → 404.
    """
    _require_valid_id(paper_id)
    vault = request.app.state.vault
    try:
        changed, message = apply_unread(vault, paper_id)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModifyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "changed": changed, "message": message}


@router.post("/paper/{paper_id}/project")
async def post_paper_project(request: Request, paper_id: str) -> dict[str, object]:
    """Link a paper to a registered project through the ``lit link`` backend.

    Body JSON ``{"project": str, "relevance"?: str}``. Reaches the filesystem
    only through :func:`litman.core.project_link.link_paper_to_project`, which
    resolves the project from the config registry map, updates the paper's
    ``projects`` field, recreates the ``litman_reflib`` / ``litman_code``
    symlinks, regenerates REFERENCES.md, and rebuilds INDEX + views atomically
    (invariant #16: no second write path). The registry is the same
    ``load_config(vault).projects`` map ``GET /api/projects`` reads from.

    An unregistered project / missing project dir surfaces as LinkError → 400;
    an unknown paper as PaperNotFoundError → 404.
    """
    _require_valid_id(paper_id)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    project = payload.get("project")
    if not isinstance(project, str) or not project.strip():
        raise HTTPException(status_code=400, detail="project must be a non-empty string.")
    relevance = payload.get("relevance")
    if relevance is not None and not isinstance(relevance, str):
        raise HTTPException(status_code=400, detail="relevance must be a string.")

    vault = request.app.state.vault
    registry = load_config(vault).projects
    try:
        link_paper_to_project(
            vault, paper_id, project.strip(), registry, relevance=relevance
        )
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/paper/{paper_id}/project/{project}")
async def delete_paper_project(
    request: Request, paper_id: str, project: str
) -> dict[str, object]:
    """Unlink a paper from a project through the ``lit unlink`` backend.

    Reaches the filesystem only through
    :func:`litman.core.project_link.unlink_paper_from_project`, the reverse of
    the link path: drops the project from the paper's ``projects`` field, removes
    the project-side symlinks (keeping shared code symlinks still used by another
    linked paper), regenerates REFERENCES.md, and rebuilds INDEX + views
    atomically. Same config registry map as the link endpoint.
    """
    _require_valid_id(paper_id)

    vault = request.app.state.vault
    registry = load_config(vault).projects
    try:
        unlink_paper_from_project(vault, paper_id, project, registry)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/projects")
async def post_projects(request: Request) -> dict[str, object]:
    """Register a new project through the ``lit project add`` backend.

    Body JSON ``{"name": str, "path": str}``. Reaches the filesystem only through
    :func:`litman.core.project_link.add_project` (the same core ``lit project
    add`` calls), which validates the path exists and is a directory (A7), then
    dual-writes TAXONOMY.md + lit-config.yaml atomically (invariant #2). The path
    is resolved to an absolute path here, mirroring the CLI's
    ``click.Path(resolve_path=True)``. Empty name / missing path / duplicate name
    surface as TaxonomyError → 400.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    name = payload.get("name")
    path = payload.get("path")
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="name must be a string.")
    if not isinstance(path, str) or not path.strip():
        raise HTTPException(status_code=400, detail="path must be a non-empty string.")

    vault = request.app.state.vault
    try:
        summary = add_project(vault, name, Path(path).expanduser().resolve())
    except TaxonomyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "name": summary["name"], "path": summary["path"]}


@router.post("/taxonomy/{key}")
async def post_taxonomy(request: Request, key: str) -> dict[str, object]:
    """Register a new controlled-vocabulary value through the ``lit taxonomy add``
    backend (register-first per invariant #2).

    Body JSON ``{"value": str}``. Reaches the filesystem only through
    :func:`litman.core.taxonomy.add_taxonomy_values` (the same core the CLI
    calls), which registers the value in TAXONOMY.md atomically — it does NOT
    attach the value to any paper. The frontend's inline-create then makes the
    separate ``PUT /metadata`` addTag call to attach it (two steps: register,
    then tag).

    ``key`` must be a user-extensible dict (topics / methods / data); an unknown
    key, a fixed-enum key, ``projects`` (path-bound, use ``POST /api/projects``),
    or an empty value surface as TaxonomyError → 400.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    value = payload.get("value")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="value must be a non-empty string.")

    vault = request.app.state.vault
    try:
        added, skipped = add_taxonomy_values(vault, key, (value,))
    except TaxonomyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "added": added, "skipped": skipped}


@router.delete("/taxonomy/{key}")
async def delete_taxonomy(
    request: Request, key: str, value: str | None = None
) -> dict[str, object]:
    """Remove a controlled-vocabulary value through the ``lit taxonomy rm`` backend.

    The value is a QUERY param (``?value=X``) rather than a path segment because a
    controlled value may contain ``/`` (it would break path routing). Reaches the
    filesystem only through :func:`litman.core.taxonomy.remove_taxonomy_value`
    (the same core the CLI's WRITE half calls), which drops the value from
    TAXONOMY.md and cascades the removal to every referencing paper's metadata in
    one atomic staged_write, then rebuilds INDEX + views (invariant #16: no second
    write path). Confirm-free — the GUI's confirm dialog is the confirmation, so
    the server skips the CLI's ``_confirm_destructive`` gate.

    ``key`` must be a user-extensible dict (topics / methods / data); an unknown
    key, a fixed-enum key, ``projects`` (path-bound, use ``DELETE /api/projects``),
    or an unregistered value surface as TaxonomyError → 400. A missing ``value``
    query param is a client bug → 400.
    """
    if not value or not value.strip():
        raise HTTPException(
            status_code=400, detail="value query parameter is required."
        )

    vault = request.app.state.vault
    try:
        n_changed, _ = remove_taxonomy_value(vault, key, value)
    except TaxonomyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "changed": n_changed}


@router.delete("/projects/{name}")
async def delete_project(request: Request, name: str) -> dict[str, object]:
    """Delete a project through the ``lit project rm`` backend.

    Reaches the filesystem only through
    :func:`litman.core.project_link.remove_project` (the same core the CLI's WRITE
    half calls): drops the project from both truth sources (TAXONOMY.md +
    lit-config.yaml), cascades the untag (and the paired ``relevance-<name>``) to
    every referencing paper, rebuilds INDEX + views, and tears down the project's
    ``litman_reflib`` / ``litman_code`` symlinks + REFERENCES.md — without removing
    the project directory itself (invariant #16: no second write path). Confirm-
    free — the GUI's confirm dialog is the confirmation.

    An unregistered project surfaces as TaxonomyError → 400.
    """
    vault = request.app.state.vault
    try:
        n_changed, _ = remove_project(vault, name)
    except TaxonomyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "changed": n_changed}


@router.put("/vaults/active")
async def put_active_vault(request: Request) -> dict[str, object]:
    """Switch the active vault through the ``lit vault use`` backend (3c-2).

    Body JSON ``{"name": str}``. This is a GLOBAL switch: it sets the registry's
    active vault via :func:`litman.core.vault_registry.apply_vault_use` (the same
    set_active + save_registry the CLI's ``lit vault use`` calls), so subsequent
    ``lit`` commands in ANY terminal without ``--library`` / ``$LIT_LIBRARY``
    resolve to the new vault — not just this GUI. The running server is also
    repointed in place: ``app.state.vault`` is updated so every later request
    hits the new vault without a restart.

    ``require_path=True`` rejects a stale registry entry (vault moved or deleted)
    with a 400 BEFORE the switch is persisted, so the global active is never left
    pointing at a dead path. An unknown name also surfaces as VaultRegistryError
    → 400.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="name must be a non-empty string.")

    try:
        entry = apply_vault_use(name, require_path=True)
    except VaultRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    target = Path(entry.path).expanduser()
    request.app.state.vault = target
    return {"ok": True, "active": name, "path": str(target)}
