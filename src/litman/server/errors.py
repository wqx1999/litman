"""Failures the page can name, and the GUI log every one of them lands in.

The page shows a failure as one short line in the person's language; the
agent they ask reads the rest from ``lit gui-log``. So an error answer
carries three things:

* ``detail``: the full text, as the CLI words it, with the way out. The page
  never renders it; it is what the log keeps.
* ``code``: picks the page's line (``doi-taken``, ``path-missing``…). None
  when the person cannot act on more than "it failed": the page then shows
  the failed action's own line.
* ``params``: fill that line. Names, ids, file names, paths inside the
  library; never an absolute path.

:func:`api_error` builds one from a litman error, which carries the code it
was raised with (:func:`litman.exceptions.coded`), or from a sentence.
:func:`install` sets up the handlers that answer for them and write each
failure to the log (:mod:`litman.core.gui_log`), an unexpected exception
with its traceback.
"""

from __future__ import annotations

import errno
import os
import traceback as traceback_module
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from litman.core.atomic import left_for_recovery, refused_file
from litman.core.gui_log import GuiLog, Kind
from litman.core.portable_link import _os_error_reason
from litman.exceptions import CorruptMetadataError

#: The code of a failure the server did not foresee (a 500 with a traceback).
UNEXPECTED = "unexpected"

#: A write that put part of its change on disk; the next open finishes it.
WRITE_PARTIAL = "write-partial"


class ApiError(HTTPException):
    """An error answer with its GUI ``code`` and ``params`` (see the module docstring).

    ``quiet`` keeps it out of the log: an answer the page takes as a normal
    state, not a failure (a notes file not written yet). ``extra`` adds
    fields to the body, such as the SI request a refusal is about.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str | None = None,
        params: Mapping[str, str] | None = None,
        quiet: bool = False,
        extra: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        source: BaseException | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            detail=detail,
            headers=dict(headers) if headers is not None else None,
        )
        self.code = code
        self.params = dict(params or {})
        self.quiet = quiet
        self.extra = dict(extra or {})
        self.source = source


def api_error(
    status: int,
    source: BaseException | str,
    code: str | None = None,
    *,
    quiet: bool = False,
    extra: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    **params: str,
) -> ApiError:
    """The error answer for ``source``: a raised error, or the sentence itself.

    A litman error brings the code and params it was raised with; ``code``
    replaces its code, and ``params`` are added to its own.
    """
    if isinstance(source, BaseException):
        own_code = getattr(source, "code", None)
        own_params = dict(getattr(source, "params", None) or {})
        return ApiError(
            status,
            str(source),
            code=code or own_code,
            params={**own_params, **params},
            quiet=quiet,
            extra=extra,
            headers=headers,
            source=source,
        )
    return ApiError(
        status, source, code=code, params=params, quiet=quiet, extra=extra, headers=headers
    )


def write_refused(
    exc: OSError,
    vault: Path | None,
    rel: str,
    code: str | None = None,
    *,
    again: str = "try again",
) -> ApiError:
    """A write the disk refused, in words: which file, the OS's reason, the way out.

    The file is the one the staged write could not put in place, else the one
    the error names, else ``rel``. The page shows ``code``'s line, or its own
    fixed line; this text is what the log keeps. A write already partly on
    disk (:func:`litman.core.atomic.left_for_recovery`) answers
    ``write-partial`` whatever ``code`` is: the next start that opens the
    library finishes it, so the page must not say that nothing was saved.
    """
    reason = _os_error_reason(exc)
    named = refused_file(exc)
    if named is None and isinstance(exc.filename, str | bytes):
        named = library_path(os.fsdecode(exc.filename), vault)
    target = named or rel
    if left_for_recovery(exc):
        return api_error(
            500,
            f"The change was only partly saved: {target} could not be written ({reason}). "
            "litman completes it the next time it opens this library.",
            WRITE_PARTIAL,
        )
    if exc.errno == errno.ENOSPC:
        return api_error(
            507,
            f"Not enough free disk space to save {target}. Free some space, then {again}.",
            code,
        )
    return api_error(
        500,
        f"{target} could not be saved ({reason}); nothing was changed. Close any program "
        f"that has it open, or make it writable, then {again}.",
        code,
    )


def error_body(
    detail: str,
    code: str | None = None,
    params: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON of an error answer: ``detail``, ``code``, ``params`` (and ``extra``)."""
    return {**(extra or {}), "detail": detail, "code": code, "params": dict(params or {})}


def library_path(path: object, vault: Path | None) -> str:
    """``path`` inside the library (``papers/x/metadata.yaml``); its file name otherwise."""
    target = Path(str(path))
    if vault is not None:
        try:
            return target.resolve().relative_to(vault.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return target.name


def _fill(
    code: str | None, params: dict[str, str], source: object, vault: Path | None
) -> dict[str, str]:
    """Add what only the server knows: the path of a file it could not read."""
    if (
        code == "file-unreadable"
        and "path" not in params
        and isinstance(source, CorruptMetadataError)
    ):
        return {**params, "path": library_path(source.path, vault)}
    return params


def gui_log(request: Request) -> GuiLog | None:
    """The log of the server that answers ``request`` (None for a bare app)."""
    log = getattr(request.app.state, "gui_log", None)
    return log if isinstance(log, GuiLog) else None


def route_of(request: Request) -> str:
    return f"{request.method} {request.url.path}"


def log_entry(
    request: Request,
    *,
    kind: Kind,
    text: str,
    status: int | None = None,
    code: str | None = None,
    params: Mapping[str, object] | None = None,
    traceback: str | None = None,
    route: str | None = None,
) -> None:
    """Write one entry about ``request`` to the GUI log."""
    log = gui_log(request)
    if log is None:
        return
    log.record(
        kind=kind,
        text=text,
        vault=request.app.state.vault,
        route=route if route is not None else route_of(request),
        status=status,
        code=code,
        params=params,
        traceback=traceback,
    )


def log_warnings(
    request: Request, warnings: Iterable[str], code: str | None = None
) -> list[str]:
    """Log what a write that succeeded could not finish (its ``warnings``);
    returns them, for the answer."""
    kept = [str(warning) for warning in warnings if warning]
    for warning in kept:
        log_entry(request, kind="warning", text=warning, status=200, code=code)
    return kept


def error_response(
    request: Request,
    status: int,
    detail: str,
    code: str | None = None,
    params: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """An error answer built outside a route (middleware), logged like the others."""
    log_entry(request, kind="error", text=detail, status=status, code=code, params=params)
    return JSONResponse(
        status_code=status,
        content=error_body(detail, code, params, extra),
        headers=dict(headers) if headers is not None else None,
    )


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    code = getattr(exc, "code", None)
    params = dict(getattr(exc, "params", None) or {})
    params = _fill(code, params, getattr(exc, "source", None), request.app.state.vault)
    extra = getattr(exc, "extra", None)
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    # Only what the page asked for: a static file the browser looked for on
    # its own (a favicon, a source map) is no failure anyone saw.
    if (
        exc.status_code >= 400
        and not getattr(exc, "quiet", False)
        and request.url.path.startswith("/api/")
    ):
        log_entry(
            request, kind="error", text=detail, status=exc.status_code, code=code, params=params
        )
    if exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=exc.headers)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(detail, code, params, extra),
        headers=exc.headers,
    )


async def _unexpected(request: Request, exc: Exception) -> Response:
    """A failure no route foresaw: a 500 the page can name, and its traceback in the log."""
    name = exc.__class__.__name__
    log_entry(
        request,
        kind="unexpected",
        text=f"{name}: {exc}",
        status=500,
        code=UNEXPECTED,
        traceback="".join(traceback_module.format_exception(exc)),
    )
    return JSONResponse(
        status_code=500,
        content=error_body(
            f"litman could not finish this ({name}). `lit gui-log` shows where it failed.",
            UNEXPECTED,
        ),
        headers={"Cache-Control": "no-store"},
    )


def litman_error_response(
    request: Request, status: int, exc: BaseException, detail: str | None = None
) -> JSONResponse:
    """A litman error caught by an app-wide handler, answered and logged with its code."""
    code = getattr(exc, "code", None)
    params = _fill(
        code, dict(getattr(exc, "params", None) or {}), exc, request.app.state.vault
    )
    return error_response(request, status, detail if detail is not None else str(exc), code, params)


def install(app: FastAPI) -> None:
    """Answer every HTTP error with its code, and every unforeseen exception
    with a 500 the page can name; log both."""
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)
