"""One-click self-update endpoint for the litman webUI.

Not a vault write (invariant #16 untouched): this is a server-lifecycle
action, like the agent-launch routes. ``POST /api/self-update`` spawns the
detached upgrade helper (see :mod:`litman.core.self_update_helper`) and then
schedules this server's own exit — the helper waits for the process to
vanish, upgrades through the installer that owns litman, and relaunches the
GUI. The confirmation lives in the SPA; by the time this endpoint is hit the
user has already said yes.

Refusals mirror ``lit self-update`` exactly (same probes, same red line: never
``pip install --upgrade`` into the running interpreter): an editable/dev
install or an install owned by neither uv nor pipx gets a 409 whose ``detail``
is the human hint the SPA shows verbatim.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api")

#: Grace period between answering 202 and asking uvicorn to exit — long enough
#: for the response (and the SPA's overlay paint) to get out of the door.
#: Module-level so tests can shrink it.
EXIT_DELAY_S = 2.0

_EDITABLE_DETAIL = (
    "This litman runs from a development (editable) install — upgrade it the "
    "way you set it up, e.g. `git pull` in the source tree."
)
_NO_INSTALLER_DETAIL = (
    "litman was not installed via uv or pipx, so it cannot update itself. "
    "Upgrade with the tool you used, e.g. `pip install --upgrade litman`."
)
_NO_SESSION_DETAIL = (
    "This server was not started by `lit gui`, so there is no session to "
    "bring back after an update. Run `lit self-update` instead."
)


@router.post("/self-update", status_code=202)
def start_self_update(request: Request) -> dict[str, object]:
    """Spawn the detached upgrade helper and schedule this server's exit.

    Sync ``def`` on purpose: the installer probes shell out (bounded at 15s
    each), and FastAPI runs sync handlers on the threadpool.
    """
    from litman.commands.self_update import (
        _UPGRADE_CMDS,
        _detect_installer,
        _is_editable_install,
    )
    from litman.core import launcher_stubs, self_update_helper

    if _is_editable_install():
        raise HTTPException(status_code=409, detail=_EDITABLE_DETAIL)
    installer = _detect_installer()
    if installer is None:
        raise HTTPException(status_code=409, detail=_NO_INSTALLER_DETAIL)

    relaunch = getattr(request.app.state, "self_update_relaunch", None)
    if not relaunch:
        raise HTTPException(status_code=409, detail=_NO_SESSION_DETAIL)
    port = int(getattr(request.app.state, "self_update_port", 0))

    # The helper runs in a detached process whose PATH is whatever the server
    # inherited (an Explorer double-click on Windows may lack the tool bin
    # dir), so pin the installer binary to its absolute path while we can
    # still resolve it. The stub paths let the Windows script move the
    # launchers aside before upgrading — see litman.core.launcher_stubs.
    upgrade_cmd = list(_UPGRADE_CMDS[installer])
    resolved = shutil.which(upgrade_cmd[0])
    if resolved:
        upgrade_cmd[0] = resolved

    script = self_update_helper.write_and_spawn_helper(
        pid=os.getpid(),
        port=port,
        upgrade_cmd=upgrade_cmd,
        relaunch_cmd=list(relaunch),
        stub_paths=launcher_stubs.installed_stubs(),
    )

    # Bow out AFTER the response is flushed. The window watcher's presence
    # gate may beat us to it (the SPA closes its window) — both paths set the
    # same idempotent flag (`should_exit`), so the race is harmless.
    server = getattr(request.app.state, "uvicorn_server", None)

    def _bow_out() -> None:
        time.sleep(EXIT_DELAY_S)
        if server is not None:
            server.should_exit = True

    threading.Thread(target=_bow_out, daemon=True).start()

    return {"status": "updating", "installer": installer, "helper": str(script)}
