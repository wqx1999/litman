"""Skill installation helper for ``lit install-skill`` (M4.3).

Copies the bundled ``lit-library`` Claude Code skill from the installed
litman package into the user's skill directory (default
``~/.claude/skills/lit-library/``). The skill files live inside the
package at ``src/litman/skills/lit-library/`` and are reachable via
``importlib.resources`` regardless of whether litman was installed
editable, via pipx, or as a regular wheel.

Single-file skills today (just ``SKILL.md``); the loop is written so
that adding supporting files (templates, examples) in the future does
not change the call site.
"""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from litman.exceptions import LitmanError

SKILL_NAME = "lit-library"
DEFAULT_TARGET = Path.home() / ".claude" / "skills" / SKILL_NAME


class SkillInstallError(LitmanError):
    """``lit install-skill`` refused: target collision without --force, or
    bundled resources missing from the installed package."""


def bundled_skill_root() -> Traversable:
    """Return the Traversable pointing at the bundled ``lit-library`` dir.

    Works for editable installs, pipx, regular wheels — anywhere
    ``importlib.resources`` can locate the package's data files.
    """
    return files("litman.skills") / SKILL_NAME


def _iter_skill_files(root: Traversable) -> list[Traversable]:
    """List the files inside the bundled skill dir, sorted by name.

    Flat layout assumed (no nesting). Adjust here if the skill grows
    sub-directories — current call sites iterate over the returned list
    once and copy each entry as a file.
    """
    items = []
    for child in root.iterdir():
        if child.is_file():
            items.append(child)
    items.sort(key=lambda c: c.name)
    return items


def install_skill(
    target: Path = DEFAULT_TARGET,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Copy the bundled skill into ``target``.

    Args:
        target: Destination directory. Created (with parents) if missing.
            Must not exist unless ``overwrite=True``.
        overwrite: When ``True``, an existing ``target`` is replaced
            file-by-file; files in ``target`` that are NOT part of the
            bundled skill are left in place (defensive: the user may
            have added per-machine additions next to SKILL.md).

    Returns:
        A summary dict with keys ``target`` (Path), ``files`` (list of
        copied filenames), ``mode`` ("created" or "overwritten").

    Raises:
        SkillInstallError: target exists and ``overwrite`` is False; or
            the bundled resources cannot be located (broken install).
    """
    root = bundled_skill_root()
    items = _iter_skill_files(root)
    if not items:
        raise SkillInstallError(
            f"No skill files found inside the installed package at "
            f"{root}. Reinstall litman (e.g. `pip install -e .`)."
        )

    target_exists = target.exists()
    if target_exists and not overwrite:
        raise SkillInstallError(
            f"Skill target already exists: {target}. "
            "Pass --force to overwrite, or pick a different --target."
        )

    target.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for item in items:
        dest = target / item.name
        dest.write_bytes(item.read_bytes())
        copied.append(item.name)

    return {
        "target": target,
        "files": copied,
        "mode": "overwritten" if target_exists else "created",
    }
