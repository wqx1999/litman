"""FastAPI app factory for the litman webUI (``lit gui``).

The server is a thin read/write surface over the *same* core + command
backends the CLI uses (invariant #16 / ADR-016 / ADR-017): read, write,
structured, trash and agent routers, plus the ``/api/presence`` WebSocket
that lets pages report they are alive (the ``--window`` shutdown gate).

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

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from litman.core.config import CONFIG_FILENAME
from litman.core.presence import PresenceTracker
from litman.server.routes_agent import router as agent_router
from litman.server.routes_presence import router as presence_router
from litman.server.routes_read import router as read_router
from litman.server.routes_structured import router as structured_router
from litman.server.routes_trash import router as trash_router
from litman.server.routes_write import router as write_router

# The vendored SPA build lands here once `frontend/build.sh` has run; it does
# not exist until Phase 1, so the factory guards on its presence.
_WEBUI_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "webui"


class _RevalidatedStaticFiles(StaticFiles):
    """Static files, minus heuristic caching of the mutable filenames.

    Plain ``StaticFiles`` sends ``ETag``/``Last-Modified`` but no
    ``Cache-Control``, which licenses the browser to reuse a cached copy
    *without revalidating* for a stretch it picks itself (RFC 9111 heuristic
    freshness — scaled off the file's age, so an installed wheel's shell can
    be "fresh" for days). That is how a restored window can boot a stale SPA
    build from disk cache and render last week's truth against today's
    server. ``no-cache`` on every mutable name (``index.html``, icons) forces
    the revalidation; the conditional GET still answers 304, so the cache
    keeps doing its job. The content-hashed bundles under ``assets/`` keep
    the default: a stale copy of an immutable name is byte-identical.
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        if not path.replace("\\", "/").startswith("assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

# API endpoints that must stay reachable when the server has no usable vault —
# either because none was ever created (welcome page) or because the one it was
# serving vanished mid-session (see ``_guard_vault``). Both states are escaped
# through the same doors: list the registry, register an existing directory,
# create a new one, switch the active entry, re-point a moved one, drop a stale
# entry. Everything else under ``/api/`` is refused — the SPA + its assets
# (served off ``/``) always pass, so the page that offers those doors still loads.
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

    ``PUT /api/vaults/{name}/path`` (relocate) carries the name in the path too,
    and it is the endpoint that HEALS the gone state — re-pointing the moved
    entry and, when it is the active one, repointing the server in place. Left
    off the whitelist it would be refused by the very 409/410 it exists to
    clear. It ends in ``/path``, which keeps the exact-match ``PUT
    /api/vaults/active`` out of this arm.
    """
    if (method, path) in _VAULTLESS_ALLOWED:
        return True
    if method == "DELETE" and path.startswith("/api/vaults/"):
        return True
    return (
        method == "PUT"
        and path.startswith("/api/vaults/")
        and path.endswith("/path")
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
    starts so the SPA can render the welcome page; the ``_guard_vault``
    middleware then 409s every vault-dependent route until the welcome flow
    creates or opens one (which repoints ``app.state.vault`` in place, no restart).
    """
    app = FastAPI(title="litman webUI", version="0", lifespan=_lifespan)
    app.state.vault = vault
    # Live-page counter behind the /api/presence WebSocket. Created (and the
    # route mounted) unconditionally: in tab/headless mode pages connect too,
    # but nothing consumes the signal — only the --window watcher does, so no
    # mode switch is needed here.
    app.state.presence = PresenceTracker()

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

        Every ``/api/`` response leaves here stamped ``Cache-Control:
        no-store`` — the 410 arm above is WHY. 410 sits on RFC 9111's list of
        heuristically-cacheable status codes, and none of our JSON responses
        carried cache headers, so Chromium would file a gone-state 410 away
        and answer later ``/api/papers`` fetches from disk cache without
        contacting the server at all. The banner it fed could then outlive
        the relocate that healed the vault — and even outlive the server
        itself: a later launch on the same port inherited the cached 410s and
        opened straight onto a stale "library is gone" screen. API responses
        describe live vault state; a cached copy of one is a lie by
        definition, so no-store, not no-cache.
        """
        vault = request.app.state.vault
        is_api = request.url.path.startswith("/api/")
        if is_api and not _vaultless_allowed(request.method, request.url.path):
            if vault is None:
                response: Response = JSONResponse(
                    status_code=409,
                    content={"detail": "No active vault yet — create or open one first."},
                )
                response.headers["Cache-Control"] = "no-store"
                return response
            if not (vault / CONFIG_FILENAME).is_file():
                response = JSONResponse(
                    status_code=410,
                    content={
                        "detail": (
                            f"The library is no longer at {vault} — it was moved, "
                            "renamed or deleted while litman was running."
                        ),
                        "path": str(vault),
                    },
                )
                response.headers["Cache-Control"] = "no-store"
                return response
        response = await call_next(request)
        if is_api:
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(read_router)
    app.include_router(write_router)
    app.include_router(structured_router)
    app.include_router(trash_router)
    app.include_router(agent_router)
    app.include_router(presence_router)

    if _WEBUI_ASSETS.is_dir():
        # html=True so client-side routes fall back to index.html.
        app.mount(
            "/",
            _RevalidatedStaticFiles(directory=_WEBUI_ASSETS, html=True),
            name="webui",
        )
    else:

        @app.get("/", response_class=PlainTextResponse)
        def _frontend_not_built() -> str:
            return "frontend not built yet — run frontend/build.sh"

    return app
