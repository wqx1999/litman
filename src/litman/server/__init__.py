"""FastAPI app factory for the litman webUI (``lit gui``).

The server is a thin read/write surface over the *same* core + command
backends the CLI uses (invariant #16 / ADR-016 / ADR-017). Phase 0 wires
only the read endpoints; structured + whitelist writes land in later phases.

This module imports fastapi at module scope, so it must NEVER be imported by
``litman.cli`` / ``import litman`` at top level (invariant #5 — the CLI's
startup path stays fastapi-free even though fastapi is now a core dependency).
``commands/gui`` imports :func:`create_app` lazily inside the command body.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from litman.server.routes_agent import router as agent_router
from litman.server.routes_read import router as read_router
from litman.server.routes_structured import router as structured_router
from litman.server.routes_trash import router as trash_router
from litman.server.routes_write import router as write_router

# The vendored SPA build lands here once `frontend/build.sh` has run; it does
# not exist until Phase 1, so the factory guards on its presence.
_WEBUI_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "webui"

# API endpoints that must work with NO active vault, so a freshly installed user
# who runs `lit gui` before creating a library lands on the welcome page and can
# create or open one from there (task-gui-welcome). Everything else under
# ``/api/`` returns 409 until a vault is served — the SPA + its assets (served
# off ``/``) always pass, so the welcome page itself loads.
_NO_VAULT_ALLOWED = frozenset(
    {
        ("GET", "/api/vaults"),
        ("POST", "/api/vaults"),
        ("POST", "/api/vaults/create"),
        ("PUT", "/api/vaults/active"),
        ("GET", "/api/version"),
    }
)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """On real server startup, refresh the PyPI update-check cache in the
    background (silent-fail) so a GUI-only user's version badge can populate
    even if they never touch the CLI. The ``GET /api/version`` route only reads
    the cache — the network never touches the request path (task-self-update D4).

    Fires only when the server actually starts (uvicorn / a `with TestClient`
    context), so a bare ``TestClient(create_app(...))`` used in tests never hits
    the network. The refresh helper is itself TTL-gated + opt-out-aware + fully
    silent; the daemon thread just keeps startup non-blocking.
    """

    def _refresh() -> None:
        try:
            from litman.core.update_check import refresh_cache_if_stale

            refresh_cache_if_stale()
        except Exception:
            pass

    threading.Thread(target=_refresh, daemon=True).start()
    yield


def create_app(vault: Path | None) -> FastAPI:
    """Build the FastAPI app bound to one vault (or none yet).

    The vault is stashed on ``app.state.vault`` so route handlers reach it via
    ``request.app.state.vault`` rather than hard-coding any path (invariant #3:
    discovery already happened in ``lit gui`` via ``find_vault``).

    ``vault`` is ``None`` when ``lit gui`` found no vault to serve (fresh install,
    or the active registry entry points at a moved directory). The server still
    starts so the SPA can render the welcome page; the ``_guard_no_vault``
    middleware then 409s every vault-dependent route until the welcome flow
    creates or opens one (which repoints ``app.state.vault`` in place, no restart).
    """
    app = FastAPI(title="litman webUI", version="0", lifespan=_lifespan)
    app.state.vault = vault

    @app.middleware("http")
    async def _guard_no_vault(request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            request.app.state.vault is None
            and request.url.path.startswith("/api/")
            and (request.method, request.url.path) not in _NO_VAULT_ALLOWED
        ):
            return JSONResponse(
                status_code=409,
                content={"detail": "No active vault yet — create or open one first."},
            )
        return await call_next(request)

    app.include_router(read_router)
    app.include_router(write_router)
    app.include_router(structured_router)
    app.include_router(trash_router)
    app.include_router(agent_router)

    if _WEBUI_ASSETS.is_dir():
        # html=True so client-side routes fall back to index.html.
        app.mount("/", StaticFiles(directory=_WEBUI_ASSETS, html=True), name="webui")
    else:

        @app.get("/", response_class=PlainTextResponse)
        def _frontend_not_built() -> str:
            return "frontend not built yet — run frontend/build.sh"

    return app
