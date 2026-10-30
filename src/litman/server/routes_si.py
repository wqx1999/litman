"""SI window endpoints (task-supplementary-info, ADR-027).

How ``lit add --si <paper>`` reaches the page the user has open, and how the
page's SI window adds files through the same core function the CLI uses
(:func:`litman.core.supplementary.add_supplementary`, invariant #16):

* ``GET /api/si/ping`` — which litman process this is, which library it
  shows, how many pages are open. Answers with or without a library, so a
  CLI can always tell a live GUI from a dead registration.
* ``POST /api/si/requests`` — open (or rejoin) the request for one paper;
  ``GET /api/si/requests/{rid}`` — its state, for the CLI's poll.
* ``POST /api/si/requests/{rid}/files`` — one dropped file, streamed to the
  upload stash and verified; ``DELETE …/files/{fid}`` — take a row out.
* ``POST /api/si/requests/{rid}/confirm`` (Done) / ``…/cancel`` (Cancel).
* ``GET`` / ``DELETE /api/paper/{paper_id}/si/{file}`` — open or delete one
  recorded SI file (the cockpit's Supplementary row); ``POST …/open`` — open
  it with this machine's default app instead, when it has a desktop;
  ``GET …/rm-preview`` — where the paper's notes cite it, for the delete
  dialog.

Nothing reaches the library before Done. A dropped file waits in
``<vault>/.litman-upload/`` as ``si-<rid>-<fid>.<ext>`` — flat, so the 24 h
sweep and ``add_supplementary``'s source check treat it like any other
upload. The request table itself lives in memory
(:mod:`litman.server.si_requests`).

GET never writes: the poll, the ping and serving a file only read. Every
write is a POST or DELETE, so ``LocalPageGuard`` refuses one sent by another
site — and so is starting an app on this machine. Request bodies and answers
are camelCase like the rest of the API.

An SI file is served as itself only when it is a PDF; anything else downloads
(``attachment``, ``application/octet-stream``, ``nosniff``). Opened inline, an
HTML or SVG file would run its scripts as this origin — and this origin can
call the write API.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from litman import __version__
from litman.core import viewer
from litman.core.config import CONFIG_FILENAME, load_config
from litman.core.document import find_paper
from litman.core.fs_identity import Anchor
from litman.core.gui_registry import APP_NAME, host_id
from litman.core.id import is_valid_id
from litman.core.supplementary import (
    SiItem,
    add_supplementary,
    has_checksum,
    is_runnable_si,
    is_unprintable,
    list_supplementary,
    official_link,
    preview_si_removal,
    recorded_si_file,
    remove_supplementary,
    si_extension,
    si_write_problem,
    verify_batch,
    verify_candidate,
)
from litman.exceptions import (
    ConfigError,
    LitmanError,
    PaperNotFoundError,
    SupplementaryError,
)
from litman.server.routes_ingest import _UPLOAD_DIRNAME, sweep_uploads
from litman.server.si_requests import (
    ConfirmPlan,
    RequestConflict,
    RequestError,
    SiRequestTable,
    SiRow,
    UnknownRequest,
    new_id,
)

router = APIRouter(prefix="/api")

# Request and row ids are server-minted uuid4 hex; anything else is unknown
# before it can reach the table or the disk.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# The upload is written in pieces of this size, each off the event loop.
_FLUSH_BYTES = 1 << 20
# Free space has to cover the file twice — this stash, then the staged copy
# Done makes of it — plus this much room for everything else.
_FREE_MARGIN = 64 << 20
# Without a Content-Length the size is unknown up front: free space is
# checked again each time this much more has arrived.
_FREE_CHECK_BYTES = 64 << 20
_NAME_MAX = 255

_NO_SPACE = "Not enough free disk space for this file. Free some space, then drop it again."
_NO_SPACE_DONE = (
    "Not enough free disk space to add these files. Free some space, then press Done again."
)
_NO_SPACE_REMOVE = (
    "Not enough free disk space to update the library. Free some space, then try again."
)
_UNKNOWN = (
    "This SI request is unknown or has expired. Start it again with `lit add --si` or Add SI."
)
_PDF_MAGIC = b"%PDF-"


def _disk_free(path: Path) -> int:
    """Free bytes where ``path`` lives (the seam tests replace)."""
    return shutil.disk_usage(path).free


def _append(handle: BinaryIO, data: bytes) -> None:
    """Write one piece of an upload (the seam tests replace)."""
    handle.write(data)


def _create_stash(stash: Path) -> BinaryIO:
    """Make the upload folder if needed and open a new stash file in it
    (the seam tests replace)."""
    stash.parent.mkdir(exist_ok=True)
    return open(stash, "xb")


def _save_failure(exc: OSError) -> HTTPException:
    """A failed write into the upload stash, as the page should hear it."""
    if exc.errno == errno.ENOSPC:
        return HTTPException(status_code=507, detail=_NO_SPACE)
    return HTTPException(
        status_code=500,
        detail=(
            f"Could not save the file in the library ({exc.__class__.__name__}). "
            "Check that its disk is writable, then drop it again."
        ),
    )


class _NoSpace(Exception):
    pass


def _table(request: Request) -> SiRequestTable:
    table: SiRequestTable = request.app.state.si_requests
    return table


def _served(request: Request) -> Anchor | None:
    vault = request.app.state.vault
    return Anchor.of(vault) if vault is not None else None


def _error(exc: RequestError) -> JSONResponse:
    if isinstance(exc, RequestConflict):
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "request": exc.request}
        )
    status = 404 if isinstance(exc, UnknownRequest) else 400
    return JSONResponse(status_code=status, content={"detail": str(exc)})


def _require_id(value: str) -> None:
    if not _ID_RE.match(value):
        raise HTTPException(status_code=404, detail=_UNKNOWN)


def _discard(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="The request body is not JSON. Send a JSON object."
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="The request body is not a JSON object. Send one."
        )
    return body


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------


@router.get("/si/ping")
def get_si_ping(request: Request) -> dict[str, Any]:
    """Who answers here: the CLI's liveness and same-library check.

    On the vault-less whitelist: with no library, or one that is gone, it
    still answers (``vault: null``) — any answer at all tells the CLI this
    registration is alive. A plain ``def``, so FastAPI runs it in the
    threadpool: the stat and resolve below can hang on a stalled network
    mount, and on the event loop that would stall every other request.
    """
    vault = request.app.state.vault
    served: str | None = None
    if vault is not None and (vault / CONFIG_FILENAME).is_file():
        served = str(Path(vault).resolve())
    return {
        "app": APP_NAME,
        "version": __version__,
        "pid": os.getpid(),
        "host": host_id(),
        "vault": served,
        "pages": request.app.state.presence.count,
    }


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _open_request(
    table: SiRequestTable,
    vault: Path,
    paper_id: str,
    source: str,
    asked_vault: str | None,
    force: bool,
) -> dict[str, Any]:
    served = Anchor.of(vault)
    if asked_vault is not None and not Anchor.of(Path(asked_vault)).same_as(served):
        raise HTTPException(
            status_code=409,
            detail=(
                "The litman GUI shows another library. "
                "Switch it to this library, then run the command again."
            ),
        )
    # Before the read: a Done landing during it must not make its own result
    # look moot for want of files the read could not see yet.
    read_mark = table.mark()
    try:
        meta = find_paper(vault, paper_id)
        problem = si_write_problem(vault, paper_id)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if problem:
        raise HTTPException(status_code=409, detail=problem)
    title = str(meta.get("title") or "").strip() or paper_id
    snapshot = table.open(
        vault=vault,
        anchor=served,
        paper_id=paper_id,
        title=title,
        link=official_link(meta),
        source=source,
        recorded={
            record["file"]: str(record["sha256"]) if has_checksum(record) else None
            for record in list_supplementary(meta)
        },
        read_mark=read_mark,
        force=force,
    )
    return {"request": snapshot}


@router.post("/si/requests")
async def post_si_request(request: Request) -> dict[str, Any]:
    """Open the SI window for a paper, or rejoin the one already open.

    Body ``{paperId, source: "cli"|"page", vault?, force?}``. The CLI passes
    the library it means, and gets 409 when the GUI shows another one; and
    its ``--force`` (absent = false), so a finished result goes back only to
    a run asking the same way.
    """
    body = await _json_object(request)
    paper_id = body.get("paperId")
    source = body.get("source")
    asked_vault = body.get("vault")
    force = body.get("force", False)
    if (
        not isinstance(paper_id, str)
        or source not in ("cli", "page")
        or not (asked_vault is None or isinstance(asked_vault, str))
        or not isinstance(force, bool)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "The request body is malformed. "
                'Send {"paperId": "<id>", "source": "cli" or "page"}.'
            ),
        )
    return await run_in_threadpool(
        _open_request,
        _table(request),
        request.app.state.vault,
        paper_id,
        source,
        asked_vault,
        force,
    )


@router.get("/si/requests/{rid}", response_model=None)
def get_si_request(
    request: Request, rid: str, collect: bool = False
) -> dict[str, Any] | JSONResponse:
    """The request's state. ``?collect=1`` marks a finished one as seen by the
    CLI — memory only, nothing is written."""
    _require_id(rid)
    try:
        snapshot = _table(request).snapshot(rid, _served(request), collect=collect)
    except RequestError as exc:
        return _error(exc)
    return {"request": snapshot}


# ---------------------------------------------------------------------------
# Rows: one dropped file each
# ---------------------------------------------------------------------------


def _dropped_name(request: Request) -> str:
    """The file's own name, from ``X-Litman-Filename`` (UTF-8, %-encoded)."""
    raw = request.headers.get("x-litman-filename")
    unusable = HTTPException(
        status_code=400,
        detail="That file name can't be used. Rename the file, then drop it again.",
    )
    if raw is None:
        raise HTTPException(
            status_code=400,
            detail="The file name is missing. Send it in the X-Litman-Filename header.",
        )
    try:
        name = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise unusable from exc
    name = re.split(r"[\\/]", name)[-1].strip()
    # The name is recorded and becomes the download name: nothing in it may
    # break or disguise the line it is shown on.
    if (
        not name
        or name in (".", "..")
        or len(name) > _NAME_MAX
        or any(is_unprintable(c) for c in name)
    ):
        raise unusable
    return name


async def _receive(request: Request, stash: Path, declared: int) -> int:
    """Stream the body into ``stash``; returns its size.

    Never the whole body in memory: pieces of ``_FLUSH_BYTES`` go to disk as
    they arrive. Any failure — a full disk, a dropped connection, a cancelled
    task — deletes what was written.
    """
    handle: BinaryIO | None = None
    written = checked = 0
    pending = bytearray()
    try:
        handle = await run_in_threadpool(_create_stash, stash)
        async for chunk in request.stream():
            pending += chunk
            if len(pending) < _FLUSH_BYTES:
                continue
            await run_in_threadpool(_append, handle, bytes(pending))
            written += len(pending)
            pending.clear()
            if not declared and written - checked >= _FREE_CHECK_BYTES:
                checked = written
                free = await run_in_threadpool(_disk_free, stash.parent)
                if free < 2 * written + _FREE_MARGIN:
                    raise _NoSpace
        if pending:
            await run_in_threadpool(_append, handle, bytes(pending))
            written += len(pending)
        await run_in_threadpool(handle.close)
    except BaseException as exc:
        if handle is not None:
            with suppress(OSError):
                handle.close()
            _discard(stash)
        if isinstance(exc, ClientDisconnect):
            raise HTTPException(
                status_code=400,
                detail="The upload was interrupted. Drop the file again.",
            ) from exc
        if isinstance(exc, _NoSpace):
            raise HTTPException(status_code=507, detail=_NO_SPACE) from exc
        if isinstance(exc, OSError):
            raise _save_failure(exc) from exc
        raise
    return written


def _paper_exists(vault: Path, paper_id: str) -> bool:
    return is_valid_id(paper_id) and (vault / "papers" / paper_id / "metadata.yaml").is_file()


@router.post("/si/requests/{rid}/files", response_model=None)
async def post_si_file(request: Request, rid: str) -> dict[str, Any] | JSONResponse:
    """One dropped file: raw bytes in, its verified row out.

    Header ``X-Litman-Filename`` carries the file's own name (UTF-8,
    percent-encoded) — the name the DOI-number check reads. No size limit:
    the body streams to disk, and only free space can refuse it (507).
    """
    _require_id(rid)
    name = _dropped_name(request)
    table = _table(request)
    served = await run_in_threadpool(_served, request)
    try:
        vault, paper_id = table.upload_target(rid, served)
    except RequestError as exc:
        return _error(exc)
    if not await run_in_threadpool(_paper_exists, vault, paper_id):
        raise HTTPException(
            status_code=404,
            detail=f"{paper_id} is no longer in the library. Cancel this SI window.",
        )

    declared_text = request.headers.get("content-length", "")
    declared = int(declared_text) if declared_text.isascii() and declared_text.isdigit() else 0
    await run_in_threadpool(sweep_uploads, vault)
    try:
        free = await run_in_threadpool(_disk_free, vault)
    except OSError as exc:
        raise _save_failure(exc) from exc
    if free < 2 * declared + _FREE_MARGIN:
        raise HTTPException(status_code=507, detail=_NO_SPACE)

    fid = new_id()
    stash = vault / _UPLOAD_DIRNAME / f"si-{rid}-{fid}{si_extension(name)}"
    size = await _receive(request, stash, declared)
    try:
        verdict = await run_in_threadpool(verify_candidate, vault, paper_id, stash, name)
    except PaperNotFoundError as exc:
        _discard(stash)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BaseException:
        _discard(stash)
        raise
    row = SiRow(
        id=fid,
        name=name,
        stash=stash,
        size=size,
        verdict=verdict.status,
        check=verdict.check,
        reason=verdict.reason,
        other_id=verdict.other_id,
        sha256=verdict.sha256,
    )
    try:
        row_json, snapshot = table.add_row(rid, served, row)
    except RequestError as exc:
        await run_in_threadpool(_discard, stash)
        return _error(exc)
    if row.verdict == "rejected":
        # A refused file can never be added: nothing to keep it for.
        await run_in_threadpool(_discard, stash)
    return {"row": row_json, "request": snapshot}


@router.delete("/si/requests/{rid}/files/{fid}", response_model=None)
def delete_si_row(request: Request, rid: str, fid: str) -> dict[str, Any] | JSONResponse:
    """Take one row out of the window, and its stash with it. A row that is
    not there answers ``removed: false``."""
    _require_id(rid)
    _require_id(fid)
    try:
        row, snapshot = _table(request).remove_row(rid, fid, _served(request))
    except RequestError as exc:
        return _error(exc)
    if row is not None:
        _discard(row.stash)
    return {"removed": row is not None, "request": snapshot}


# ---------------------------------------------------------------------------
# Done / Cancel
# ---------------------------------------------------------------------------


def _missing_stashes(plan: ConfirmPlan) -> set[str]:
    return {row.id for row, _approved in plan.chosen if not row.stash.is_file()}


def _reverify(plan: ConfirmPlan) -> dict[str, SiRow]:
    """The chosen rows whose verdict is not what the window showed."""
    items = [SiItem(row.stash, row.name) for row, _approved in plan.chosen]
    try:
        verdicts = verify_batch(plan.vault, plan.paper_id, items)
    except (OSError, LitmanError):
        return {}
    changed: dict[str, SiRow] = {}
    for (row, _approved), verdict in zip(plan.chosen, verdicts, strict=True):
        if (verdict.status, verdict.check) != (row.verdict, row.check):
            changed[row.id] = replace(
                row,
                verdict=verdict.status,
                check=verdict.check,
                reason=verdict.reason,
                other_id=verdict.other_id,
                sha256=verdict.sha256,
            )
    return changed


def _added_entries(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "file": record["file"],
            "path": str(path),
            "original": record["original"],
            "bytes": record["bytes"],
            "check": record["check"],
        }
        for record, path in zip(result.records, result.paths, strict=True)
    ]


def _conflict(detail: str, snapshot: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": detail, "request": snapshot})


async def _carry_out(
    table: SiRequestTable, plan: ConfirmPlan, served: Anchor | None
) -> dict[str, Any] | JSONResponse:
    rid = plan.request_id
    settled = False
    try:
        missing = await run_in_threadpool(_missing_stashes, plan)
        if missing:
            snapshot = table.abort_confirm(rid, served, drop=missing)
            settled = True
            return _conflict(
                f"{len(missing)} dropped file(s) were cleaned up before Done. "
                "Drop them again, then press Done.",
                snapshot,
            )
        items = [
            SiItem(row.stash, row.name, approved=approved)
            for row, approved in plan.chosen
        ]
        try:
            result = await run_in_threadpool(
                add_supplementary,
                plan.vault,
                plan.paper_id,
                items,
                allow_unverified=False,
            )
        except SupplementaryError as exc:
            changed = await run_in_threadpool(_reverify, plan)
            for row in changed.values():
                if row.verdict == "rejected":
                    await run_in_threadpool(_discard, row.stash)
            snapshot = table.abort_confirm(rid, served, refreshed=changed)
            settled = True
            if changed:
                return _conflict(
                    f"The check of {len(changed)} file(s) changed since they "
                    "were dropped. Review them, then press Done again.",
                    snapshot,
                )
            return _conflict(str(exc), snapshot)
        except FileExistsError:
            snapshot = table.abort_confirm(rid, served)
            settled = True
            return _conflict(
                "Another change to this paper is being written. Press Done again.",
                snapshot,
            )
        except PaperNotFoundError as exc:
            table.abort_confirm(rid, served)
            settled = True
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OSError as exc:
            table.abort_confirm(rid, served)
            settled = True
            if exc.errno == errno.ENOSPC:
                raise HTTPException(status_code=507, detail=_NO_SPACE_DONE) from exc
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Could not add the SI files ({exc.__class__.__name__}). "
                    "Check that the library's disk is writable, then press Done again."
                ),
            ) from exc
        snapshot = table.finish_confirm(
            rid,
            served,
            _added_entries(result),
            result.warnings,
            sha256s={record["file"]: record["sha256"] for record in result.records},
        )
        settled = True
    finally:
        if not settled:
            table.abort_confirm(rid, served)
    for row in plan.dropped:
        await run_in_threadpool(_discard, row.stash)
    return {"request": snapshot}


@router.post("/si/requests/{rid}/confirm", response_model=None)
async def post_si_confirm(request: Request, rid: str) -> dict[str, Any] | JSONResponse:
    """Done: add every ✓ row and each "?" row listed in ``addAnyway``, in one
    staged write through ``add_supplementary``. A ✕ row can never be added.

    Core verifies again: a file whose check changed since it was dropped is
    refused (409, the rows refreshed) rather than recorded as the user's.
    """
    _require_id(rid)
    body = await _json_object(request)
    add_anyway = body.get("addAnyway", [])
    if not isinstance(add_anyway, list) or not all(
        isinstance(fid, str) for fid in add_anyway
    ):
        raise HTTPException(
            status_code=400,
            detail='The request body is malformed. Send {"addAnyway": ["<file id>"]}.',
        )
    table = _table(request)
    served = await run_in_threadpool(_served, request)
    try:
        plan = table.begin_confirm(rid, served, add_anyway)
    except RequestError as exc:
        return _error(exc)
    if not isinstance(plan, ConfirmPlan):
        return {"request": plan}  # added already: a repeated Done
    return await _carry_out(table, plan, served)


@router.post("/si/requests/{rid}/cancel", response_model=None)
def post_si_cancel(request: Request, rid: str) -> dict[str, Any] | JSONResponse:
    """Cancel: the request ends, every stashed file goes, the library is
    untouched."""
    _require_id(rid)
    try:
        stashes, snapshot = _table(request).cancel(rid, _served(request))
    except RequestError as exc:
        return _error(exc)
    for stash in stashes:
        _discard(stash)
    return {"request": snapshot}


# ---------------------------------------------------------------------------
# A paper's recorded SI files
# ---------------------------------------------------------------------------


def _is_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(len(_PDF_MAGIC)) == _PDF_MAGIC
    except OSError:
        return False


@router.get("/paper/{paper_id}/si/{file}")
def get_si_file(request: Request, paper_id: str, file: str) -> FileResponse:
    """One recorded SI file, downloaded under the name it was added with.

    Only a name recorded in the paper's metadata is ever served, never a link
    and never a folder. A real PDF opens in the browser; everything else is a
    download, sniffing off.
    """
    if not is_valid_id(paper_id):
        raise HTTPException(
            status_code=404,
            detail=f"Invalid paper id: {paper_id!r}. Open the file from the paper's page.",
        )
    try:
        path, record = recorded_si_file(request.app.state.vault, paper_id, file)
    except (PaperNotFoundError, SupplementaryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    inline = file.endswith(".pdf") and _is_pdf(path)
    return FileResponse(
        path,
        media_type="application/pdf" if inline else "application/octet-stream",
        filename=str(record.get("original") or file),
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def config_problem(exc: ConfigError, way_out: str) -> str:
    """lit-config.yaml's fault as one line for the page — the field, when one
    is at fault — then ``way_out``. No path: the page has no use for it."""
    cause = exc.__cause__
    if isinstance(cause, ValidationError) and cause.errors():
        first = cause.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "top level"
        problem = f"{CONFIG_FILENAME} is invalid ({field}: {first['msg']})"
    else:
        problem = f"{CONFIG_FILENAME} cannot be read (lit config show says why)"
    return f"{problem} — {way_out}"


@router.post("/paper/{paper_id}/si/{file}/open")
def post_si_file_open(request: Request, paper_id: str, file: str) -> dict[str, Any]:
    """Open one recorded SI file with this machine's default app — a plain
    click on its chip when ``GET /api/capabilities`` says ``open_files:
    "desktop"``. When it cannot, the answer says to download the file, which
    the page then does.

    Only a recorded name, as for the GET. Never a file the default app would
    run (403). A PDF goes to ``default_pdf_viewer`` when one is set, as in
    ``lit open``. Waits up to ``viewer.OPEN_WAIT_S`` to hear whether an app
    took the file — a plain ``def``, so the wait holds a worker thread, not
    the event loop.
    """
    if not is_valid_id(paper_id):
        raise HTTPException(
            status_code=404,
            detail=f"Invalid paper id: {paper_id!r}. Open the file from the paper's page.",
        )
    vault: Path = request.app.state.vault
    try:
        path, _record = recorded_si_file(vault, paper_id, file)
    except (PaperNotFoundError, SupplementaryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    host = host_id()
    if is_runnable_si(file):
        raise HTTPException(
            status_code=403,
            detail=f"{file} could be run, not viewed — download it instead",
        )
    if viewer.open_files_mode() == "none":
        raise HTTPException(
            status_code=409,
            detail=f"{host} has no desktop to open {file} on — download it instead",
        )
    configured: str | None = None
    if file.endswith(".pdf"):
        try:
            configured = load_config(vault).default_pdf_viewer
        except ConfigError as exc:
            raise HTTPException(
                status_code=500,
                detail=config_problem(exc, "fix it, or download the file instead"),
            ) from exc
    try:
        viewer.launch_file(path, configured)
    except viewer.OpenFailed as exc:
        detail = (
            f"No app on {host} opened {file} — download it instead"
            if exc.reason == "no-app"
            else f"{host} could not start {exc.command} to open {file} — download it instead"
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    return {"opened": True, "file": file, "host": host}


def _recorded_or_404(vault: Path, paper_id: str, file: str) -> None:
    """404 unless ``file`` is exactly a name the paper records."""
    if not is_valid_id(paper_id):
        raise HTTPException(
            status_code=404,
            detail=f"Invalid paper id: {paper_id!r}. Open the file from the paper's page.",
        )
    try:
        meta = find_paper(vault, paper_id)
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if file not in {record["file"] for record in list_supplementary(meta)}:
        raise HTTPException(
            status_code=404,
            detail=f"{paper_id} has no SI file {file!r}. `lit show {paper_id}` lists them.",
        )


@router.get("/paper/{paper_id}/si/{file}/rm-preview")
def get_si_rm_preview(request: Request, paper_id: str, file: str) -> dict[str, Any]:
    """Where the paper's notes cite one SI file, for the delete dialog — the
    counts ``lit rm --si`` prints before it asks. Reads only.

    ``{file, cited: [{name, count}], unchecked: [name]}``: ``count`` is the
    lines of that notes file citing it; ``unchecked`` names a notes file that
    could not be read. 409 when the delete itself would be refused.
    """
    vault: Path = request.app.state.vault
    _recorded_or_404(vault, paper_id, file)
    try:
        preview = preview_si_removal(vault, paper_id, [file])
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SupplementaryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "file": file,
        "cited": [
            {"name": name, "count": count}
            for name, count in preview.cited.get(file, {}).items()
        ],
        "unchecked": [name for name, _why in preview.unchecked],
    }


@router.delete("/paper/{paper_id}/si/{file}")
def delete_si_file(request: Request, paper_id: str, file: str) -> dict[str, Any]:
    """Delete one SI file for good and drop its record — what ``lit rm --si``
    does. ``warnings``: a derived-view rebuild that failed afterwards."""
    vault: Path = request.app.state.vault
    _recorded_or_404(vault, paper_id, file)
    try:
        result = remove_supplementary(vault, paper_id, [file])
    except SupplementaryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail="Another change to this paper is being written. Try again in a moment.",
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise HTTPException(status_code=507, detail=_NO_SPACE_REMOVE) from exc
        # Only the records' commit raises one, before anything was deleted.
        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not delete {file} ({exc.__class__.__name__}); nothing was "
                "changed. Check that the library's disk is writable, then try again."
            ),
        ) from exc
    return {"file": file, "warnings": result.warnings}
