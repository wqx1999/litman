"""Skill installation helpers for ``lit install-skill`` (M4.3 + M9.2).

Copies every bundled agent skill from the installed litman package into a
per-user skills directory: ``~/.claude/skills/<name>/`` for Claude Code, or
the Agent Skills open-standard directory ``~/.agents/skills/<name>/`` that
Gemini CLI and Cursor both discover. Which directory a caller targets is
decided by the agent catalog (:mod:`litman.core.agents`); every helper here
is directory-neutral and just takes ``parent_dir``.

The skill files live inside the package at
``src/litman/skills/<skill-name>/`` and are reachable via
``importlib.resources`` regardless of whether litman was installed
editable, via pipx, or as a regular wheel. Each top-level directory
under ``src/litman/skills/`` is one skill; add a new skill by creating
a new subdirectory with at least a ``SKILL.md``.

API split:

* :func:`install_skill` installs **one** named skill — used by tests and
  by callers that want fine-grained control.
* :func:`install_all_skills` enumerates the bundle and loops over each
  skill — used by ``lit install-skill`` with no ``--skill`` flag, so
  users adding lit-reading later can re-run the same command to pick
  up new bundled skills.
* :func:`skill_status` / :func:`aggregate_skill_state` compare installed
  copies byte-for-byte against the bundle — the freshness probes behind
  the health-check ``skill_drift`` arm, the GUI agent-status endpoint and
  ``lit install-skill``'s re-run behaviour.
"""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from litman.core.portable_link import is_portable_link
from litman.exceptions import LitmanError

def default_skills_parent_dir() -> Path:
    """The per-user skills dir Claude Code auto-discovers, at call time.

    ``DEFAULT_PARENT_DIR`` below freezes ``Path.home()`` at import; every
    runtime probe (``skill_status`` / ``installed_skill_names`` and the
    health-check / GUI callers behind them) resolves through this helper
    instead, so a redirected ``$HOME`` is honored and the test suite can
    isolate itself from the developer's real skills dir by patching one seam.
    """
    return Path.home() / ".claude" / "skills"


def standard_skills_parent_dir() -> Path:
    """The shared skills dir of the Agent Skills open standard
    (``~/.agents/skills``) — Gemini CLI and Cursor both discover it.

    Same call-time seam contract as :func:`default_skills_parent_dir`: a
    redirected ``$HOME`` is honored, and the test suite isolates itself from
    the developer's real ``~/.agents/skills`` by patching this one function.
    """
    return Path.home() / ".agents" / "skills"


# Default parent dir under which each skill gets its own subdir.
# Claude Code auto-discovers user-level skills here. Import-time snapshot,
# kept as a pinned value for tests and back-compat imports; runtime
# probes use :func:`default_skills_parent_dir`.
DEFAULT_PARENT_DIR = default_skills_parent_dir()

# Backwards-compatibility re-exports — some early tests / scripts import these.
# ``SKILL_NAME`` historically pointed at the only bundled skill; now there are
# multiple, so it points at the first one for ergonomics but new code should
# prefer ``list_bundled_skills`` + per-skill names.
SKILL_NAME = "lit-library"
DEFAULT_TARGET = DEFAULT_PARENT_DIR / SKILL_NAME


class SkillInstallError(LitmanError):
    """``lit install-skill`` refused: target collision without ``--force``,
    bundled resources missing from the installed package, or an unknown
    skill name was passed."""


def _skills_root() -> Traversable:
    """Traversable pointing at ``litman.skills`` — the parent namespace
    that contains every bundled skill subdir."""
    return files("litman.skills")


def list_bundled_skills() -> list[str]:
    """Return the names of every skill bundled with this litman install.

    A skill is any subdirectory of ``litman.skills`` that contains at
    least a ``SKILL.md``. Order is stable (sorted by name) so reruns of
    ``lit install-skill`` install skills in the same order.
    """
    out: list[str] = []
    for child in _skills_root().iterdir():
        if not child.is_dir():
            continue
        # A directory only counts as a skill if SKILL.md is present.
        has_skill_md = any(
            grandchild.is_file() and grandchild.name == "SKILL.md"
            for grandchild in child.iterdir()
        )
        if has_skill_md:
            out.append(child.name)
    out.sort()
    return out


def bundled_skill_root(name: str = SKILL_NAME) -> Traversable:
    """Return the Traversable pointing at one bundled skill's directory.

    Args:
        name: Skill subdirectory name (e.g. ``"lit-library"``,
            ``"lit-reading"``). Defaults to ``SKILL_NAME`` for legacy
            callers that predate multi-skill support.

    Raises:
        SkillInstallError: ``name`` does not match any directory under
            ``litman.skills``.
    """
    if name not in list_bundled_skills():
        available = ", ".join(list_bundled_skills()) or "(none)"
        raise SkillInstallError(
            f"No bundled skill named {name!r}. Available: {available}."
        )
    return _skills_root() / name


def _iter_skill_files(root: Traversable) -> list[Traversable]:
    """List the files inside one skill's directory, sorted by name.

    Flat layout assumed (no nesting). Adjust here if a skill grows
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
    target: Path | None = None,
    overwrite: bool = False,
    name: str = SKILL_NAME,
) -> dict[str, Any]:
    """Copy one bundled skill into ``target``.

    Args:
        target: Destination directory for the skill (e.g.
            ``~/.claude/skills/lit-reading``). Created with parents if
            missing. When ``None`` (the default), uses
            ``default_skills_parent_dir() / name`` so a fresh install lands
            where Claude Code auto-discovers it. Must not exist unless
            ``overwrite=True``.
        overwrite: When ``True``, an existing ``target`` is replaced
            file-by-file; files in ``target`` that are NOT part of the
            bundled skill are left in place (defensive: the user may
            have added per-machine additions next to SKILL.md).
        name: Which bundled skill to install. Defaults to
            ``SKILL_NAME`` (``"lit-library"``) for legacy callers.

    Returns:
        A summary dict with keys ``name`` (str), ``target`` (Path),
        ``files`` (list of copied filenames), ``mode``
        (``"created"`` | ``"overwritten"`` | ``"linked"`` = target is a
        symlink/junction, left entirely untouched).

    Raises:
        SkillInstallError: target exists and ``overwrite`` is False; or
            ``name`` is unknown; or the bundled resources cannot be
            located (broken install).
    """
    root = bundled_skill_root(name)
    items = _iter_skill_files(root)
    if not items:
        raise SkillInstallError(
            f"No skill files found inside the installed package at "
            f"{root}. Reinstall litman (e.g. `pip install -e .`)."
        )

    if target is None:
        target = default_skills_parent_dir() / name

    if is_portable_link(target):
        # A linked skill dir (symlink or Windows junction) points at a copy
        # managed elsewhere — typically a development checkout. Copying
        # "through" the link would overwrite those source files in place, so
        # it is refused even with ``overwrite=True`` and reported instead.
        return {
            "name": name,
            "target": target,
            "files": [],
            "mode": "linked",
        }

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
        "name": name,
        "target": target,
        "files": copied,
        "mode": "overwritten" if target_exists else "created",
    }


def installed_skill_names(
    parent_dir: Path | None = None,
) -> set[str]:
    """Return the subset of bundled skill names whose target directory
    already exists under ``parent_dir`` (default: the call-time skills dir).

    Mirrors :func:`litman.commands.install_completion.completion_installed`
    so wizards / setup flows can detect a prior install and skip rather than
    crash on the first ``SkillInstallError`` from :func:`install_skill`.
    Presence of the directory — not its contents — is the signal: a
    half-populated dir still counts as installed, because :func:`install_skill`
    would still refuse without ``overwrite=True``. For a content-level
    verdict (up to date vs stale) use :func:`skill_status`.
    """
    if parent_dir is None:
        parent_dir = default_skills_parent_dir()
    return {
        name
        for name in list_bundled_skills()
        if (parent_dir / name).exists()
    }


def skill_status(
    parent_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Compare every bundled skill against its installed copy.

    The installed skill directory is a deploy artifact of the package: a
    litman upgrade ships new bundled content but never touches the installed
    copy, so presence alone says nothing about freshness. This is the single
    content-level probe behind the health-check ``skill_drift`` arm, the GUI
    agent-status endpoint and ``lit install-skill``'s re-run behaviour.

    Returns ``{skill_name: {"state": ..., "stale_files": [...]}}`` with one
    entry per bundled skill:

    * ``"absent"``  — nothing at ``parent_dir/<name>``.
    * ``"linked"``  — the directory is a symlink / Windows junction. A linked
      skill points at a copy managed elsewhere (e.g. a development checkout);
      its content is deliberately not compared and it is never stale.
    * ``"stale"``   — directory exists but at least one bundled file is
      missing or differs byte-for-byte; ``stale_files`` names them.
    * ``"current"`` — every bundled file is present and byte-identical.

    Files the user added next to ``SKILL.md`` never affect the state (they
    are also never touched by an overwrite — same contract as
    :func:`install_skill`).
    """
    if parent_dir is None:
        parent_dir = default_skills_parent_dir()
    out: dict[str, dict[str, Any]] = {}
    for name in list_bundled_skills():
        target = parent_dir / name
        if is_portable_link(target):
            out[name] = {"state": "linked", "stale_files": []}
            continue
        if not target.exists():
            out[name] = {"state": "absent", "stale_files": []}
            continue
        stale_files: list[str] = []
        for item in _iter_skill_files(bundled_skill_root(name)):
            dest = target / item.name
            try:
                same = dest.read_bytes() == item.read_bytes()
            except OSError:
                # Missing or unreadable installed file — either way the
                # installed copy no longer matches the bundle.
                same = False
            if not same:
                stale_files.append(item.name)
        out[name] = {
            "state": "stale" if stale_files else "current",
            "stale_files": stale_files,
        }
    return out


def aggregate_skill_state(parent_dir: Path | None = None) -> str:
    """One-word skill state for the agent-onboarding status endpoint.

    ``"stale"`` if any installed skill is out of date, ``"absent"`` if no
    bundled skill is installed at all, else ``"current"``. A partial install
    (some skills present, others never installed) counts as ``"current"``:
    installing only one bundled skill is a deliberate choice
    (``lit install-skill --skill``), not drift. ``linked`` dirs count as
    current — they are dev-managed and never nagged about.
    """
    states = [
        info["state"] for info in skill_status(parent_dir).values()
    ]
    if "stale" in states:
        return "stale"
    if all(state == "absent" for state in states):
        return "absent"
    return "current"


def uninstall_skill(
    name: str,
    parent_dir: Path | None = None,
) -> dict[str, Any]:
    """Remove one bundled skill's files from ``parent_dir/name/``.

    Symmetric with :func:`install_skill`: only files that belong to the
    bundled skill are deleted; any file the user added next to ``SKILL.md``
    is left in place. The directory is removed only if it ends up empty;
    otherwise it is kept and the surviving files are reported.

    Args:
        name: Bundled skill subdirectory name (e.g. ``"lit-library"``).
        parent_dir: Directory that holds the skill's own subdir. ``None``
            (the default) resolves the call-time
            :func:`default_skills_parent_dir`.

    Returns:
        A summary dict with keys ``name`` (str), ``target`` (Path),
        ``removed`` (list of deleted filenames), ``mode``
        (``"removed"`` = dir gone | ``"kept"`` = dir kept with leftovers |
        ``"skipped"`` = target is a symlink, left untouched |
        ``"absent"`` = nothing was there), ``leftover`` (list of filenames
        left behind).
    """
    if parent_dir is None:
        parent_dir = default_skills_parent_dir()
    target = parent_dir / name
    if is_portable_link(target):
        # A linked skill dir (symlink or Windows junction) points outside
        # the tree we manage: deleting "through" it could reach files
        # elsewhere, and rmdir on a link errors. Leave it entirely untouched.
        return {
            "name": name,
            "target": target,
            "removed": [],
            "mode": "skipped",
            "leftover": [],
        }
    if not target.exists():
        return {
            "name": name,
            "target": target,
            "removed": [],
            "mode": "absent",
            "leftover": [],
        }

    # Which filenames belong to the bundled skill? Only those get deleted.
    # If the bundled resources cannot be located (unknown name / broken
    # install) fall back to deleting nothing so we never over-remove.
    try:
        bundled = {item.name for item in _iter_skill_files(bundled_skill_root(name))}
    except SkillInstallError:
        bundled = set()

    removed: list[str] = []
    for child in sorted(target.iterdir(), key=lambda c: c.name):
        if child.is_file() and child.name in bundled:
            child.unlink()
            removed.append(child.name)

    leftover = sorted(c.name for c in target.iterdir())
    if not leftover:
        target.rmdir()
        return {
            "name": name,
            "target": target,
            "removed": removed,
            "mode": "removed",
            "leftover": [],
        }
    return {
        "name": name,
        "target": target,
        "removed": removed,
        "mode": "kept",
        "leftover": leftover,
    }


def install_all_skills(
    parent_dir: Path | None = None,
    overwrite: bool = False,
) -> list[dict[str, Any]]:
    """Install every bundled skill into ``parent_dir/<name>/``.

    Each skill is installed independently — if one already exists and
    ``overwrite`` is False, the call raises before any further skill is
    touched. This keeps the operation atomic-from-the-user's-view: you
    either get a clean batch install, or you get told why it stopped.

    Args:
        parent_dir: Directory under which each skill gets its own
            subdirectory. ``None`` (the default) resolves the call-time
            :func:`default_skills_parent_dir`.
        overwrite: Forwarded to :func:`install_skill`.

    Returns:
        A list of the summary dicts returned by :func:`install_skill`,
        in the same order as :func:`list_bundled_skills`.
    """
    if parent_dir is None:
        parent_dir = default_skills_parent_dir()
    results: list[dict[str, Any]] = []
    for skill_name in list_bundled_skills():
        result = install_skill(
            target=parent_dir / skill_name,
            overwrite=overwrite,
            name=skill_name,
        )
        results.append(result)
    return results
