"""``lit self-update`` — upgrade litman through the tool installer that owns it.

Probes how litman is installed and dispatches to that installer's upgrade
command. RED LINE (task-self-update): this NEVER runs ``pip install --upgrade``
into the current interpreter — that would clobber a conda / editable dev env.
The probe order and their guards:

1. **Editable / development install** (PEP 610 ``direct_url.json`` editable
   flag) → reject with a manual hint, no upgrade. Checked FIRST because a
   machine can carry a separate pipx-installed litman alongside an editable
   conda one: ``pipx list`` would then mention litman even though the *running*
   litman is the editable one, so a text-only pipx probe would upgrade the wrong
   install. The editable flag is the reliable discriminator.
2. ``uv tool list`` mentions litman → ``uv tool upgrade litman``.
3. ``pipx list`` mentions litman → ``pipx upgrade litman``.
4. Otherwise → reject with a manual hint (pip-bare / conda), or, when neither
   ``uv`` nor ``pipx`` is on PATH at all, an error naming the manual command.

Every probe subprocess is timeout-wrapped so a wedged tool can never hang the
command.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import click
from rich.console import Console

from litman import __version__
from litman.core import update_check
from litman.exceptions import SelfUpdateError

console = Console()

# Short cap for the read-only probes (`uv tool list` / `pipx list`); generous
# cap for the actual upgrade (it downloads + reinstalls a wheel).
_PROBE_TIMEOUT_S = 15.0
_UPGRADE_TIMEOUT_S = 300.0

_UPGRADE_CMDS = {
    "uv": ["uv", "tool", "upgrade", "litman"],
    "pipx": ["pipx", "upgrade", "litman"],
}

_EDITABLE_HINT = (
    "litman is running from an editable (development) install.\n"
    "Upgrade it the way you set it up — e.g. `git pull` in the source tree."
)

_MANUAL_HINT = (
    "litman was not installed via uv or pipx.\n"
    "Upgrade with the package manager you used, e.g.:\n"
    "  [bold]pip install --upgrade litman[/]"
)

_NO_TOOL_MSG = (
    "Neither uv nor pipx found on PATH. Upgrade litman manually, e.g. "
    "`pip install --upgrade litman`."
)


def _run_capture(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Run ``cmd`` capturing text output; ``None`` if missing / timed out."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _is_editable_install() -> bool:
    """True when the litman distribution is an editable (PEP 660) install."""
    try:
        import importlib.metadata as importlib_metadata

        raw = importlib_metadata.distribution("litman").read_text("direct_url.json")
        if not raw:
            return False
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:
        return False


def _installer_lists_litman(binary: str, list_cmd: list[str]) -> bool:
    """True when ``binary`` is on PATH and its list output mentions litman."""
    if shutil.which(binary) is None:
        return False
    proc = _run_capture(list_cmd, timeout=_PROBE_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return False
    return "litman" in proc.stdout.lower()


def _detect_installer() -> str | None:
    """Return ``"uv"`` / ``"pipx"`` / ``None`` — which tool manages litman."""
    if _installer_lists_litman("uv", ["uv", "tool", "list"]):
        return "uv"
    if _installer_lists_litman("pipx", ["pipx", "list"]):
        return "pipx"
    return None


def _installed_version() -> str | None:
    """Fresh version of the just-upgraded ``lit`` on PATH (for post-verify)."""
    proc = _run_capture(["lit", "--version"], timeout=_PROBE_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


@click.command("self-update")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
def self_update_cmd(yes: bool) -> None:
    """Upgrade litman to the latest release via uv or pipx.

    Detects how litman was installed and runs that tool's upgrade command after
    a confirmation. A pip-bare, conda, or editable/development install is not
    upgraded — a manual hint is printed instead.
    """
    current = __version__

    if _is_editable_install():
        console.print(_EDITABLE_HINT)
        return

    installer = _detect_installer()
    if installer is None:
        if shutil.which("uv") is None and shutil.which("pipx") is None:
            raise SelfUpdateError(_NO_TOOL_MSG)
        console.print(_MANUAL_HINT)
        return

    latest = update_check._fetch_latest_version()
    target = latest or "latest"
    console.print(f"current [bold]{current}[/] → [bold]{target}[/]")
    if not yes:
        click.confirm("Upgrade litman now?", default=False, abort=True)

    cmd = _UPGRADE_CMDS[installer]
    console.print(f"[dim]$ {' '.join(cmd)}[/]")
    try:
        proc = subprocess.run(cmd, timeout=_UPGRADE_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise SelfUpdateError(
            f"`{' '.join(cmd)}` timed out after {_UPGRADE_TIMEOUT_S:.0f}s."
        ) from e
    except (FileNotFoundError, OSError) as e:
        raise SelfUpdateError(f"`{' '.join(cmd)}` failed to run: {e}") from e
    if proc.returncode != 0:
        raise SelfUpdateError(
            f"`{' '.join(cmd)}` exited with code {proc.returncode}."
        )

    updated = _installed_version()
    if updated:
        console.print(f"[green]now:[/] {updated}")
    else:
        console.print("[green]upgrade complete.[/] Run `lit --version` to verify.")
