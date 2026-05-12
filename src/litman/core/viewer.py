"""Paper-id resolution + PDF viewer dispatch for ``lit open`` (M9.1).

Two responsibilities, kept here so the CLI command stays a thin wrapper:

* **Id resolution** — exact match plus case-insensitive substring fallback.
  Title and author search are intentionally out of scope: that belongs to
  the ``lit-reading`` Claude Code skill (M9.2), which has natural-language
  context the CLI doesn't.
* **Viewer dispatch** — pick a launch command from ``lit-config.yaml``'s
  ``default_pdf_viewer`` field if set; otherwise fall back to the platform
  default (``open`` / ``xdg-open`` / ``os.startfile``); WSL gets one extra
  fallback to ``wslview``. Launches are fire-and-forget — no wait, no
  output capture, otherwise the user's terminal blocks until the GUI
  viewer closes.

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

from litman.exceptions import AmbiguousPaperIdError, PaperNotFoundError

# Sentinel returned by :func:`detect_platform_viewer` on Windows. The caller
# branches on it because ``os.startfile`` is a function (not a string command)
# and is only defined on Windows.
WINDOWS_STARTFILE_SENTINEL = "__startfile__"


def resolve_paper_id(vault: Path, query: str) -> str:
    """Resolve a user-supplied query to a canonical paper id.

    Order:
        1. Exact match against ``<vault>/papers/<query>/`` (which equals
           the metadata.yaml ``id`` field — enforced by
           ``check_id_consistency``).
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
    if sys.platform == "darwin":
        return "open"
    if sys.platform == "win32":
        return WINDOWS_STARTFILE_SENTINEL
    # Linux / BSD / WSL.
    if shutil.which("xdg-open"):
        return "xdg-open"
    if shutil.which("wslview"):
        return "wslview"
    return None


def launch_pdf(
    pdf_path: Path, configured_viewer: str | None
) -> tuple[str, str]:
    """Spawn a viewer process for ``pdf_path``. Fire-and-forget.

    Priority:
        1. ``configured_viewer`` (from ``lit-config.yaml`` ``default_pdf_viewer``)
           when non-empty.
        2. Platform default via :func:`detect_platform_viewer`.

    Args:
        pdf_path: Existing PDF file. Caller verifies existence.
        configured_viewer: Value of ``default_pdf_viewer`` from config, or
            ``None`` / empty string (both treated as "use platform default").

    Returns:
        ``(command_used, source)`` where ``source`` is one of
        ``"configured"`` / ``"platform"`` / ``"wsl-fallback"``. The CLI
        prints this so the user knows which viewer was actually invoked.

    Raises:
        FileNotFoundError: No usable viewer. Message tells the user what
            to install or which config field to update.
    """
    if configured_viewer:
        try:
            subprocess.Popen(
                [configured_viewer, str(pdf_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            return (configured_viewer, "configured")
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"Configured pdf_viewer {configured_viewer!r} not found on "
                "PATH. Install it, or update default_pdf_viewer in "
                "lit-config.yaml."
            ) from e

    viewer = detect_platform_viewer()
    if viewer is None:
        raise FileNotFoundError(
            "No platform PDF viewer available (xdg-open / wslview missing). "
            "Install xdg-utils (Linux) or wslview (WSL), or set "
            "default_pdf_viewer in lit-config.yaml."
        )

    if viewer == WINDOWS_STARTFILE_SENTINEL:
        # os.startfile is Windows-only; mypy can't see it on other platforms.
        os.startfile(str(pdf_path))  # type: ignore[attr-defined]
        return ("os.startfile", "platform")

    source = "wsl-fallback" if viewer == "wslview" else "platform"
    try:
        subprocess.Popen(
            [viewer, str(pdf_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return (viewer, source)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"Platform PDF viewer {viewer!r} disappeared between probe "
            "and launch — unusual environment."
        ) from e
