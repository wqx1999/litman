"""``POST /api/gui-log``: the page's own failures, into the GUI log.

Most of what the page shows as a failure is an answer from this server, and
:mod:`litman.server.errors` logs those itself. Some failures never reach
it: pdf.js cannot open a PDF or cannot produce the annotated copy, a save
made before a library switch fails in the browser. The page posts them here
so that the short line it shows still has its full text on disk, where
``lit gui-log`` reads it.

The body is ``{"action": str, "text": str, "code"?: str}``: what the page was
doing, what failed, in its own words, and the code of the line it showed.
Only this user's own page gets here (``LocalPageGuard``); the text is cut
like every entry's, and a repeat within the log's window is written once.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request

from litman.server.errors import api_error, log_entry

router = APIRouter(prefix="/api")

_CODE_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_ACTION_MAX = 120


@router.post("/gui-log")
async def post_gui_log(request: Request) -> dict[str, object]:
    """Log one failure the page met on its own."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise api_error(400, "Body must be JSON.", quiet=True) from exc
    if not isinstance(payload, dict):
        raise api_error(400, "Body must be a JSON object.", quiet=True)
    action = payload.get("action")
    text = payload.get("text")
    code = payload.get("code")
    if not isinstance(action, str) or not action.strip():
        raise api_error(400, "action must be a non-empty string.", quiet=True)
    if not isinstance(text, str) or not text.strip():
        raise api_error(400, "text must be a non-empty string.", quiet=True)
    if code is not None and (not isinstance(code, str) or not _CODE_RE.match(code)):
        raise api_error(400, "code must be a short lowercase code.", quiet=True)
    log_entry(
        request,
        kind="page",
        text=text.strip(),
        code=code,
        route=f"page: {action.strip()[:_ACTION_MAX]}",
    )
    return {"ok": True}
