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

from litman.core.config import CONFIG_FILENAME
from litman.server.routes_agent import router as agent_router
from litman.server.routes_read import router as read_router
from litman.server.routes_structured import router as structured_router
from litman.server.routes_trash import router as trash_router
from litman.server.routes_write import router as write_router

# The vendored SPA build lands here once `frontend/build.sh` has run; it does
# not exist until Phase 1, so the factory guards on its presence.
_WEBUI_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "webui"

# API endpoints that must stay reachable when the server has no usable vault —
# either because none was ever created (welcome page) or because the one it was
# serving vanished mid-session (see ``_guard_vault``). Both states are escaped
# through the same doors: list the registry, register an existing directory,
# create a new one, switch the active entry, drop a stale entry. Everything
# else under ``/api/`` is refused — the SPA + its assets (served off ``/``)
# always pass, so the page that offers those doors still loads.
_VAULTLESS_ALLOWED = frozenset(
    {
        ("GET", "/api/vaults"),
        ("POST", "/api/vaults"),
        ("POST", "/api/vaults/create"),
        ("PUT", "/api/vaults/active"),
        ("GET", "/api/version"),
    }
)


def _vaultless_allowed(method: str, path: str) -> bool:
    """Is this request one of the doors out of the no-vault / gone-vault state?

    ``DELETE /api/vaults/{name}`` carries the name in the path, so it cannot sit
    in the exact-match set above. It belongs with the doors all the same: it is
    a pure registry write (the route never touches the vault, and it refuses the
    served vault itself with its own 409), and the vault manager — the very
    dialog the gone-state banner opens — renders an Unregister button on every
    row. Without this, that button answered with the middleware's complaint
    about a *different* vault than the one clicked.
    """
    if (method, path) in _VAULTLESS_ALLOWED:
        return True
    return method == "DELETE" and path.startswith("/api/vaults/")


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
    starts so the SPA can render the welcome page; the ``_guard_vault``
    middleware then 409s every vault-dependent route until the welcome flow
    creates or opens one (which repoints ``app.state.vault`` in place, no restart).
    """
    app = FastAPI(title="litman webUI", version="0", lifespan=_lifespan)
    app.state.vault = vault

    @app.middleware("http")
    async def _guard_vault(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Refuse vault-dependent routes when the bound vault is absent or gone.

        Two distinct failures, deliberately given two distinct status codes so the
        frontend cannot conflate them:

        * **409** — no vault was ever served (``app.state.vault is None``). The SPA
          renders the welcome page: *create your first library*.
        * **410** — a vault WAS bound, but its ``lit-config.yaml`` is no longer on
          disk: the user moved, renamed or deleted the directory while the GUI was
          open. Telling that user to "create your first library" would invite them
          to create a second one over the top, so it must never reach the welcome
          branch.

        Only 410 needs the extra stat, and it is one per ``/api/`` request. It
        earns it by running BEFORE the route: without this guard a write into a
        vanished vault does not fail, it *rebuilds* — ``staged_write`` mkdirs its
        staging root with ``parents=True`` (core/atomic.py) and the commit mkdirs
        the paper's parent the same way, so the write would silently reconstruct a
        one-paper ghost library at the dead path while the real one sits elsewhere,
        and report success. Reads were no better: a missing ``papers/`` makes
        ``list_papers`` return ``[]``, which the GUI renders as *your library is
        empty*. Both lies die here.

        The ghost directory that a pre-guard write left behind has no
        ``lit-config.yaml`` (``staged_write`` never writes one), so the sentinel
        also refuses to mistake such a carcass for a real vault.
        """
        vault = request.app.state.vault
        if request.url.path.startswith("/api/") and not _vaultless_allowed(
            request.method, request.url.path
        ):
            if vault is None:
                return JSONResponse(
                    status_code=409,
                    content={"detail": "No active vault yet — create or open one first."},
                )
            if not (vault / CONFIG_FILENAME).is_file():
                return JSONResponse(
                    status_code=410,
                    content={
                        "detail": (
                            f"The library is no longer at {vault} — it was moved, "
                            "renamed or deleted while litman was running."
                        ),
                        "path": str(vault),
                    },
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
