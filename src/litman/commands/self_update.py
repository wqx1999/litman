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
5. Owned by uv or pipx, but installed from a direct URL (git / a local file)
   instead of an index → reject with the reinstall commands. The installer
   re-resolves the source it recorded, so it would never reach the release
   the version check is about to advertise.

Every probe subprocess is timeout-wrapped so a wedged tool can never hang the
command.

The upgrade itself runs differently per platform, and the split is not
cosmetic. POSIX upgrades in place, synchronously, with the installer's output
on the terminal. **Windows cannot**: the command runs from the very launcher
stub the upgrade has to replace, and Windows locks a running executable
outright — it cannot be overwritten and (uv's trampoline holding its own image
without share-delete) cannot even be renamed aside, so ``uv tool upgrade``
dies with os error 32 no matter what this process does first. So Windows hands
the upgrade to the same detached helper the webUI's one-click update uses
(:mod:`litman.core.self_update_helper`): it waits for this process to vanish
and upgrades with nothing locked. The command therefore returns *before* the
upgrade happens, and says so.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import click
from rich.console import Console

from litman import __version__
from litman.core import launcher_stubs, self_update_helper, update_check
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

# Printed, never executed — a shell one-liner reads as something to copy,
# which is the whole point: this process cannot uninstall itself.
_REINSTALL_CMDS = {
    "uv": "uv tool uninstall litman && uv tool install litman",
    "pipx": "pipx uninstall litman && pipx install litman",
}

_NON_RELEASE_HINT = (
    "litman was installed from {origin}, not from a release.\n"
    "Reinstall it:  {command}"
)

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


def _direct_url() -> dict[str, object] | None:
    """The distribution's PEP 610 ``direct_url.json``, or ``None``.

    PEP 610 requires this file for anything installed from a direct URL (git,
    a local wheel, an editable tree) and forbids it for anything resolved from
    an index by name — so its absence *is* the "came from PyPI or a mirror"
    signal. Unreadable / unparsable metadata reads as absent: a probe that
    cannot answer must not block an upgrade that would have worked.
    """
    try:
        import importlib.metadata as importlib_metadata

        raw = importlib_metadata.distribution("litman").read_text("direct_url.json")
        if not raw:
            return None
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _is_editable_install() -> bool:
    """True when the litman distribution is an editable (PEP 660) install."""
    dir_info = (_direct_url() or {}).get("dir_info")
    return bool(isinstance(dir_info, dict) and dir_info.get("editable"))


def _install_origin() -> str | None:
    """Where this litman came from, phrased for a message — or ``None``.

    ``None`` means it was resolved from an index (PyPI or a mirror), the one
    shape ``uv tool upgrade`` / ``pipx upgrade`` can actually move forward: the
    installer re-resolves whatever source it recorded, so a git-installed
    litman upgrades against that git ref, not against the release the version
    check just advertised. Editable installs answer ``None`` here too — they
    have their own branch and their own hint.
    """
    payload = _direct_url()
    if payload is None:
        return None
    dir_info = payload.get("dir_info")
    if isinstance(dir_info, dict) and dir_info.get("editable"):
        return None
    vcs_info = payload.get("vcs_info")
    if isinstance(vcs_info, dict):
        vcs = str(vcs_info.get("vcs") or "a repository")
        revision = vcs_info.get("requested_revision") or str(
            vcs_info.get("commit_id") or ""
        )[:7]
        return f"{vcs} ({revision})" if revision else vcs
    return "a local file"


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


def _spawn_detached_upgrade(cmd: list[str]) -> None:
    """Windows: hand ``cmd`` to the detached helper and let this process die.

    Pins the installer to its absolute path first — the helper inherits
    whatever PATH this shell had, and resolving it here while we still can
    costs nothing.
    """
    upgrade_cmd = list(cmd)
    resolved = shutil.which(upgrade_cmd[0])
    if resolved:
        upgrade_cmd[0] = resolved
    try:
        self_update_helper.write_and_spawn_helper(
            pid=os.getpid(),
            upgrade_cmd=upgrade_cmd,
            stub_paths=launcher_stubs.installed_stubs(),
        )
    except OSError as e:
        raise SelfUpdateError(f"could not start the upgrade helper: {e}") from e


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

    # Heal a launcher an earlier half-failed upgrade lost, before deciding
    # anything else (win32 only; a silent no-op everywhere else).
    for name in launcher_stubs.repair_default():
        console.print(f"[dim]restored missing launcher {name}[/]")

    installer = _detect_installer()
    if installer is None:
        if shutil.which("uv") is None and shutil.which("pipx") is None:
            raise SelfUpdateError(_NO_TOOL_MSG)
        console.print(_MANUAL_HINT)
        return

    # Before the version check, because that is where the promise is made: the
    # advertised release comes from PyPI, while the upgrade would re-resolve
    # the source this copy was installed from. Installer is non-None here.
    origin = _install_origin()
    if origin is not None:
        # markup=False: the origin carries a revision string out of the
        # distribution's own metadata, and Rich would read a bracket in it as a
        # style span — swallowing part of the name, or aborting on an unbalanced
        # one. This hint has no markup of its own to lose.
        console.print(
            _NON_RELEASE_HINT.format(origin=origin, command=_REINSTALL_CMDS[installer]),
            markup=False,
        )
        return

    latest = update_check._fetch_latest_version()
    target = latest or "latest"
    console.print(f"current [bold]{current}[/] → [bold]{target}[/]")
    if not yes:
        click.confirm("Upgrade litman now?", default=False, abort=True)

    cmd = _UPGRADE_CMDS[installer]
    console.print(f"[dim]$ {' '.join(cmd)}[/]")

    # Windows runs the upgrade after this process is gone — see module docstring.
    if sys.platform == "win32":
        _spawn_detached_upgrade(cmd)
        console.print(
            "Windows locks litman's launcher while it runs, so the upgrade "
            "starts the moment this command exits.\n"
            "Give it a few seconds, then check with [bold]lit --version[/].\n"
            f"[dim]log: {self_update_helper.log_path()}[/]"
        )
        return

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
