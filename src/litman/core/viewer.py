"""Paper-id resolution + viewer dispatch for ``lit open``.

Two responsibilities, kept here so the CLI command stays a thin wrapper:

* **Id resolution** — exact match plus case-insensitive substring fallback.
  Title and author search are intentionally out of scope: that belongs to
  the ``lit-reading`` Claude Code skill, which has natural-language
  context the CLI doesn't.
* **Viewer dispatch** — open a file with a configured command (for a PDF,
  ``lit-config.yaml``'s ``default_pdf_viewer``) if there is one; otherwise
  with the platform default (``open`` / ``xdg-open`` / ``os.startfile``);
  WSL gets one extra fallback to ``wslview``. The opener is given at most
  :data:`OPEN_WAIT_S` to report that no app took the file — never longer,
  and no output is captured, so the user's terminal never blocks until the
  viewer closes. :func:`open_files_mode` tells the GUI whether opening a
  file here reaches a screen at all.

Pure reads. No state file. Importable from ``core/checks.py`` so the
health-check viewer-availability probe can reuse :func:`detect_platform_viewer`
without duplicating the platform branching.

Design rationale lives in ADR-004 (no embedded viewer, no ``lit focus``
state machine).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from litman.exceptions import AmbiguousPaperIdError, PaperNotFoundError

# Sentinel returned by :func:`detect_platform_viewer` on Windows. The caller
# branches on it because ``os.startfile`` is a function (not a string command)
# and is only defined on Windows.
WINDOWS_STARTFILE_SENTINEL = "__startfile__"

# How long an opener gets to report failure before the file counts as open.
# xdg-open exits 3 at once when no app handles the type, macOS open exits 1;
# without a desktop environment xdg-open runs the app itself in the
# foreground, so an opener still running by then is the app, already open.
OPEN_WAIT_S = 1.5

_PDF_WAY_OUT = "Set default_pdf_viewer in lit-config.yaml to a specific viewer."


def resolve_paper_id(vault: Path, query: str) -> str:
    """Resolve a user-supplied query to a canonical paper id.

    Order:
        1. Exact match against ``<vault>/papers/<query>/`` (which equals
           the metadata.yaml ``id`` field — enforced by
           ``check_paper_dir_validity``).
        2. Case-insensitive substring match against every paper id; unique
           match wins.
        3. Otherwise raise.

    Title / author search is intentionally NOT performed. Letting
    ``lit open peptide`` pick one paper out of fifty peptide-related ones
    surprises the user; substring on id alone keeps the CLI strict and
    pushes natural-language disambiguation onto the ``lit-reading`` skill.

    Args:
        vault: Resolved vault root (must contain a ``papers/`` subdir).
        query: User-supplied id (exact or partial).

    Returns:
        Canonical paper id.

    Raises:
        PaperNotFoundError: zero matches.
        AmbiguousPaperIdError: 2+ matches; carries the candidate list.
    """
    papers_dir = vault / "papers"
    all_ids: list[str] = []
    if papers_dir.is_dir():
        for child in sorted(papers_dir.iterdir()):
            if child.is_dir() and (child / "metadata.yaml").is_file():
                all_ids.append(child.name)

    if query in all_ids:
        return query

    q_lower = query.lower()
    matches = [pid for pid in all_ids if q_lower in pid.lower()]

    if not matches:
        raise PaperNotFoundError(
            f"No paper matching {query!r} in vault {vault.name!r}. "
            "Run `lit list` to see available ids."
        )
    if len(matches) == 1:
        return matches[0]
    raise AmbiguousPaperIdError(query, matches)


# The four seams below — the platform, a PATH lookup, starting an opener,
# os.startfile — are looked up at call time, so a test replaces them on this
# module instead of faking ``sys.platform`` (which is the whole interpreter's).


def _platform() -> str:
    return sys.platform


def _which(name: str) -> str | None:
    return shutil.which(name)


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    """Start an opener sharing nothing with this process: no inherited
    streams, and a session of its own, so the app it starts outlives a
    Ctrl-C here."""
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _startfile(path: str) -> None:
    # os.startfile is Windows-only; mypy can't see it on other platforms.
    os.startfile(path)  # type: ignore[attr-defined]


def is_headless() -> bool:
    """True when no X11/Wayland display is reachable.

    Only meaningful for the xdg-open branch of launch_file: macOS/Windows
    don't consult DISPLAY, and the wslview branch reaches the Windows host
    regardless of any Linux display.
    """
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def detect_platform_viewer() -> str | None:
    """Return the platform default PDF viewer command, or None.

    Returns:
        - ``"open"`` on macOS (always present at ``/usr/bin/open``)
        - :data:`WINDOWS_STARTFILE_SENTINEL` on Windows (caller branches
          to ``os.startfile``)
        - ``"xdg-open"`` on Linux/BSD when present on PATH
        - ``"wslview"`` on Linux when ``xdg-open`` is missing but
          ``wslview`` is present (typical WSL setup without xdg-utils)
        - ``None`` when none of the above is available
    """
    platform = _platform()
    if platform == "darwin":
        return "open"
    if platform == "win32":
        return WINDOWS_STARTFILE_SENTINEL
    # Linux / BSD / WSL.
    if _which("xdg-open"):
        return "xdg-open"
    if _which("wslview"):
        return "wslview"
    return None


def is_ssh_session() -> bool:
    """True in an SSH session, on every platform: a file opened here reaches
    no screen in front of the user. A Mac or a PC reached that way would open
    it on its own unattended screen, and an HPC login node keeps a forwarded
    ``DISPLAY`` long after the X server behind it is gone."""
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))


def open_files_mode() -> Literal["desktop", "none"]:
    """Whether a file opened on this machine shows up on a screen in front of
    the user — what the GUI asks before a click opens an SI file here.

    ``"none"`` in an SSH session (:func:`is_ssh_session`). Otherwise macOS and
    Windows always have a desktop; Linux has one when a display is set and
    ``xdg-open`` is installed, and WSL when ``wslview`` is.
    """
    if is_ssh_session():
        return "none"
    if _platform() in ("darwin", "win32"):
        return "desktop"
    if (not is_headless() and _which("xdg-open")) or _which("wslview"):
        return "desktop"
    return "none"


class OpenFailed(FileNotFoundError):
    """No app opened the file.

    ``reason`` is ``"not-started"`` when no opener could be started (none
    installed, no display for ``xdg-open``, a command that would not run) and
    ``"no-app"`` when the opener ran and reported failure; ``command`` names
    the opener. A FileNotFoundError, which ``lit open`` maps to exit 2 + the
    path.
    """

    def __init__(
        self,
        message: str,
        *,
        command: str,
        reason: Literal["not-started", "no-app"],
    ) -> None:
        super().__init__(message)
        self.command = command
        self.reason = reason


def _install_hint(path: Path) -> str:
    kind = f"{path.suffix.lower()} files" if path.suffix else "this kind of file"
    return f"Install an app for {kind}, or open the path below yourself."


def _wait_for(
    proc: subprocess.Popen[bytes],
    path: Path,
    command: str,
    wait_s: float,
    way_out: str,
) -> None:
    """Raise :class:`OpenFailed` if the opener exits non-zero within ``wait_s``."""
    try:
        code = proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        return
    if code != 0:
        raise OpenFailed(
            f"No app opened {path.name} ({command} exited {code}). {way_out}",
            command=command,
            reason="no-app",
        )


def launch_file(
    path: Path,
    configured_viewer: str | None = None,
    *,
    wait_s: float = OPEN_WAIT_S,
) -> tuple[str, str]:
    """Open ``path`` with ``configured_viewer``, else this machine's default app.

    Priority:
        1. ``configured_viewer`` (``default_pdf_viewer`` from
           ``lit-config.yaml``, which callers pass for a PDF) when non-empty.
        2. Platform default via :func:`detect_platform_viewer`.

    The opener gets the absolute path and up to ``wait_s`` seconds: exiting
    0, or still running by then, means the file is open; any other exit means
    no app took it (``xdg-open`` exits 3 when nothing handles the type, macOS
    ``open`` 1). ``os.startfile`` returns at once and reports a missing
    association by raising.

    Args:
        path: Existing file. Caller verifies existence.
        configured_viewer: The command to open it with, or ``None`` / empty
            string (both treated as "use platform default").
        wait_s: How long the opener may take to report failure.

    Returns:
        ``(command_used, source)`` where ``source`` is one of
        ``"configured"`` / ``"platform"`` / ``"wsl-fallback"``. The CLI
        prints this so the user knows which viewer was actually invoked.

    Raises:
        OpenFailed: Nothing opened the file — no opener in this session
            (missing, or ``xdg-open`` without a display), one that could not
            be started, or one that reported failure. The message says what
            to install or which config field to update; the CLI maps it to
            exit 2 + path print.
    """
    target = str(path.absolute())
    is_pdf = path.suffix.lower() == ".pdf"
    if configured_viewer:
        try:
            proc = _spawn([configured_viewer, target])
        except OSError as e:
            # Broad OSError, not just FileNotFoundError: a configured viewer
            # that is a directory / not executable / permission-denied raises
            # other OSError subclasses.
            raise OpenFailed(
                f"Configured pdf_viewer {configured_viewer!r} could not be "
                "launched (not found on PATH, not executable, or not a file). "
                "Install it, or update default_pdf_viewer in lit-config.yaml.",
                command=configured_viewer,
                reason="not-started",
            ) from e
        _wait_for(
            proc,
            path,
            configured_viewer,
            wait_s,
            "Check default_pdf_viewer in lit-config.yaml.",
        )
        return (configured_viewer, "configured")

    viewer = detect_platform_viewer()
    if viewer is None:
        raise OpenFailed(
            "No platform PDF viewer available (xdg-open / wslview missing). "
            "Install xdg-utils (Linux) or wslview (WSL), or set "
            "default_pdf_viewer in lit-config.yaml."
            if is_pdf
            else "No platform app opener available (xdg-open / wslview "
            "missing). Install xdg-utils (Linux) or wslview (WSL).",
            command="xdg-open",
            reason="not-started",
        )

    if viewer == WINDOWS_STARTFILE_SENTINEL:
        try:
            _startfile(target)
        except OSError as e:
            # No application is associated with the type (WinError 1155), or
            # the shell refused the launch.
            raise OpenFailed(
                f"Windows could not open {path}: no associated PDF "
                f"application, or the launch was refused. {_PDF_WAY_OUT}"
                if is_pdf
                else f"Windows has no app for {path.name}, or refused to start "
                f"one. {_install_hint(path)}",
                command="os.startfile",
                reason="no-app",
            ) from e
        return ("os.startfile", "platform")

    command = viewer
    source = "wsl-fallback" if viewer == "wslview" else "platform"
    if viewer == "xdg-open" and is_headless():
        # On WSL a headless xdg-open is the norm; prefer wslview (which opens
        # the file on the Windows host) before declaring no display available
        # — otherwise this headless guard preempts the documented WSL
        # fallback.
        wslview = _which("wslview")
        if wslview is None:
            # Refused without spawning: with no display, xdg-open can only
            # fail or hand the file to a text-mode program.
            raise OpenFailed(
                "No graphical display detected (DISPLAY and WAYLAND_DISPLAY "
                "both unset); xdg-open cannot launch a viewer in this session. "
                "The PDF path is printed below for manual handling. Set "
                "default_pdf_viewer in lit-config.yaml to a viewer usable "
                "without an X display, or run from a session with a display."
                if is_pdf
                else "No graphical display detected, so xdg-open cannot open "
                f"{path.name}. Run from a session with a display.",
                command="xdg-open",
                reason="not-started",
            )
        command, viewer, source = wslview, "wslview", "wsl-fallback"

    try:
        proc = _spawn([command, target])
    except OSError as e:
        raise OpenFailed(
            f"Platform PDF viewer {viewer!r} could not be launched "
            f"({e.__class__.__name__}) between probe and launch — unusual "
            "environment."
            if is_pdf
            else f"{viewer} was found but could not be started "
            f"({e.__class__.__name__}). Open the path below yourself.",
            command=viewer,
            reason="not-started",
        ) from e
    _wait_for(
        proc, path, viewer, wait_s, _PDF_WAY_OUT if is_pdf else _install_hint(path)
    )
    return (viewer, source)


def launch_pdf(
    pdf_path: Path, configured_viewer: str | None
) -> tuple[str, str]:
    """:func:`launch_file` for a paper's PDF — what ``lit open`` runs.

    Waits up to :data:`OPEN_WAIT_S` for the viewer to report failure, so a
    failed launch exits 2 with the path instead of claiming it opened.
    """
    return launch_file(pdf_path, configured_viewer)
