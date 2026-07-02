"""FastAPI app factory for the litman webUI (``lit gui``).

The server is a thin read/write surface over the *same* core + command
backends the CLI uses (invariant #16 / ADR-016 / ADR-017). Phase 0 wires
only the read endpoints; structured + whitelist writes land in later phases.

This module imports fastapi at module scope, so it must NEVER be imported by
``litman.cli`` / ``import litman`` at top level (invariant #5). ``commands/gui``
imports :func:`create_app` lazily, behind the extra-installed guard.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from litman.server.routes_read import router as read_router
from litman.server.routes_structured import router as structured_router
from litman.server.routes_trash import router as trash_router
from litman.server.routes_write import router as write_router

# The vendored SPA build lands here once `frontend/build.sh` has run; it does
# not exist until Phase 1, so the factory guards on its presence.
_WEBUI_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "webui"


def create_app(vault: Path) -> FastAPI:
    """Build the FastAPI app bound to one vault.

    The vault is stashed on ``app.state.vault`` so route handlers reach it via
    ``request.app.state.vault`` rather than hard-coding any path (invariant #3:
    discovery already happened in ``lit gui`` via ``find_vault``).
    """
    app = FastAPI(title="litman webUI", version="0")
    app.state.vault = vault

    app.include_router(read_router)
    app.include_router(write_router)
    app.include_router(structured_router)
    app.include_router(trash_router)

    if _WEBUI_ASSETS.is_dir():
        # html=True so client-side routes fall back to index.html.
        app.mount("/", StaticFiles(directory=_WEBUI_ASSETS, html=True), name="webui")
    else:

        @app.get("/", response_class=PlainTextResponse)
        def _frontend_not_built() -> str:
            return "frontend not built yet — run frontend/build.sh"

    return app
