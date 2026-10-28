"""Library-level checks: the machine's vault registry, leftover staging,
the PDF viewer, installed skill copies, the trash, supplementary files and
their citations, and code clones.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ruamel.yaml.error import YAMLError

from litman.core.checks._base import TRASH_SIZE_WARN, Issue, _yaml
from litman.core.code import CODES_DIRNAME, REPO_DIRNAME, REPO_META_FILENAME
from litman.core.trash import (
    TRASH_DIRNAME,
    TRASH_MAX_ENTRIES,
    count_replaced_folders,
    is_trash_entry_name,
)
from litman.core.undecodable import printable
from litman.exceptions import ConfigError, VaultRegistryError

if TYPE_CHECKING:
    from litman.core.si_citations import CitationProblem


# ---------------------------------------------------------------------------
# Vault-level checks
# ---------------------------------------------------------------------------


def check_vault_registry_drift(
    vault: Path,
    papers: list[dict[str, Any]],
    *,
    exists_status: dict[str, bool | None] | None = None,
) -> list[Issue]:
    """Registered vault entries whose on-disk directory has gone missing.

    Machine-level drift (registry ↔ vault dir, ledger #4). The ``vault`` /
    ``papers`` arguments are ignored — the registry is a user-level truth
    source, not vault-scoped — but the ``(vault, papers)`` signature is kept
    so the check slots into ``_CHECK_REGISTRY`` like any other.

    Uses the mount-safe **bounded-stat** probe (:func:`_drift._exists_bounded`,
    ADR-014) rather than a bare ``stat()``, which hangs or false-reports on a
    dropped HPC mount. This is the
    single-detection-core fix for the M30 §1.1 divergence: registry drift now
    uses bounded-stat in **both** the per-command hook AND ``health-check``.
    Only a definite ``False`` counts as drift; ``None`` (timeout / OSError) is
    "unknown" and never flagged — a slow mount must not look like a deleted
    vault. Reading the registry + bounded-stat is invariant-#15-safe (no
    per-paper ``metadata.yaml`` read).

    A corrupt registry is now an emitted finding (M30 Phase 3 / invariant #14
    no-silent-skip): "I cannot read the registry" is itself drift, not a clean
    state. The Tier-1 hook's ``check_and_prompt_registry_drift`` surfaces the
    same case to stderr, so both sides are consistent.

    ``exists_status`` (M30 Phase 5 / verification task 3): when the Tier-1 cheap
    hook has already bounded-stat'd these paths as part of a single shared 0.5s
    budget, it threads the pre-resolved ``{path: bool|None}`` map in so this
    check does not re-probe. Tier-2 (``run_all_checks``) passes ``None`` →
    falls back to its own :func:`_exists_bounded`, byte-unchanged.
    """
    from litman.commands._drift import _exists_bounded
    from litman.core.vault_registry import load_registry

    try:
        reg = load_registry()
    except VaultRegistryError as exc:
        return [
            Issue(
                category="vault_registry_drift",
                severity="error",
                paper_id=None,
                message=(
                    f"vault registry is unreadable ({exc.__class__.__name__}): "
                    "drift cannot be checked"
                ),
                hint="inspect / repair vaults.yaml (see `lit vault list`)",
            )
        ]

    if not reg.vaults:
        return []

    paths = [v.path for v in reg.vaults]
    if exists_status is None:
        status = _exists_bounded(paths)
    else:
        # Pre-resolved by the shared cheap-hook budget; any path the hook did
        # not include resolves to None (= unknown, never flagged).
        status = {p: exists_status.get(p) for p in paths}
    return [
        Issue(
            category="vault_registry_drift",
            severity="warning",
            paper_id=None,
            message=(
                f"registered vault {entry.name!r} points at "
                f"{entry.path} but that path no longer exists"
            ),
            hint=f"lit vault remove {entry.name}",
        )
        for entry in reg.vaults
        if status.get(entry.path) is False
    ]


def check_stale_staging(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Leftover op directories under ``.litman-staging/`` (M17 tri-state).

    Three outcomes per leftover op directory:

    * No ``COMMITTED`` sentinel → clean abort before the commit decision
      point. Normal, recoverable via roll-back → ``info`` severity, in
      ``AUTO_FIXABLE_CATEGORIES`` (``--fix`` rolls it back). When its
      manifest lists a folder already moved, the finding says the folder
      will be moved back — the roll-back is more than deleting the dir.
    * ``COMMITTED`` present, manifest fully replayable → torn commit that
      roll-forward can finish → ``info`` severity, auto-fixable. (In
      practice the vault-open hook has already healed this before
      health-check runs; this branch covers a direct call / a race.)
    * ``COMMITTED`` present but a manifested relpath is missing from both
      staging and target (or the manifest is unreadable) → unrecoverable
      data loss → ``error`` severity, **not** auto-fixable: a human must
      decide. Reuses :class:`RecoveryResult.message`.

    The probe is read-only — it inspects on-disk state and reuses the
    recovery classifier without mutating anything (``--fix`` performs the
    actual roll-back / roll-forward through ``cleanup_stale_staging``).
    """
    from litman.core.atomic import (
        _SENTINEL_FILENAME,
        RecoveryResult,
        _dir_move_plan,
        _folders_unreadable_message,
        _manifest_unreadable_message,
        _moved_back_message,
        _read_manifest_dir_moves,
    )

    out: list[Issue] = []
    staging = vault / ".litman-staging"
    if not staging.is_dir():
        return out
    for child in sorted(staging.iterdir()):
        if not child.is_dir():
            out.append(
                Issue(
                    category="stale_staging",
                    severity="info",
                    paper_id=None,
                    message=(
                        f".litman-staging/{child.name} — leftover entry"
                    ),
                    hint="run `lit health-check --fix` to clean",
                )
            )
            continue

        committed = (child / _SENTINEL_FILENAME).is_file()
        # Same reading of the folder moves as the recoverer (_recover_one_op).
        moves = _read_manifest_dir_moves(child)
        try:
            back, blocked = (
                _dir_move_plan(child, vault, moves, committed=committed)
                if moves is not None
                else ([], None)
            )
        except OSError as err:
            out.append(
                Issue(
                    category="stale_staging_unrecoverable",
                    severity="error",
                    paper_id=None,
                    message=_folders_unreadable_message(vault, err),
                    hint="the next lit command retries; check again after it",
                )
            )
            continue
        if blocked is not None:
            out.append(
                Issue(
                    category="stale_staging_unrecoverable",
                    severity="error",
                    paper_id=None,
                    message=blocked,
                    hint=(
                        "settle the folders it names by hand, then remove "
                        f".litman-staging/{child.name}/ — NOT auto-fixed"
                    ),
                )
            )
            continue
        if not committed:
            if moves is None:
                out.append(
                    Issue(
                        category="stale_staging_unrecoverable",
                        severity="error",
                        paper_id=None,
                        message=_manifest_unreadable_message(child.name),
                        hint=(
                            f"inspect .litman-staging/{child.name}/ by hand, "
                            "then remove it — NOT auto-fixed"
                        ),
                    )
                )
            elif back:
                out.append(
                    Issue(
                        category="stale_staging",
                        severity="info",
                        paper_id=None,
                        message=_moved_back_message(back, mode="pending"),
                        hint="run `lit health-check --fix` to move it back",
                    )
                )
            else:
                out.append(
                    Issue(
                        category="stale_staging",
                        severity="info",
                        paper_id=None,
                        message=(
                            f".litman-staging/{child.name}/ — leftover op dir "
                            "(clean abort, no commit record)"
                        ),
                        hint="run `lit health-check --fix` to clean",
                    )
                )
            continue

        result = _classify_torn_op(child, vault, RecoveryResult)
        if result.kind == "unrecoverable":
            out.append(
                Issue(
                    category="stale_staging_unrecoverable",
                    severity="error",
                    paper_id=None,
                    message=result.message or (
                        f".litman-staging/{child.name}/ — unrecoverable "
                        "torn commit"
                    ),
                    hint=(
                        "data was lost mid-commit; inspect "
                        f".litman-staging/{child.name}/ manually — "
                        "NOT auto-fixed"
                    ),
                )
            )
        else:
            out.append(
                Issue(
                    category="stale_staging",
                    severity="info",
                    paper_id=None,
                    message=(
                        f".litman-staging/{child.name}/ — torn commit, "
                        "roll-forward pending"
                        + (f" (moves {result.message})" if result.message else "")
                    ),
                    hint="run `lit health-check --fix` to roll forward",
                )
            )
    return out


def _classify_torn_op(
    op_dir: Path,
    vault: Path,
    result_cls: type,
) -> Any:
    """Read-only classification of a ``COMMITTED`` op dir.

    Returns a :class:`RecoveryResult`-shaped object with ``kind`` of
    ``rolled_forward`` (all files replayable; ``message`` names the folder
    moves still to make, or is ``None``) or ``unrecoverable`` (some
    manifested relpath lost from both sides, folders the op cannot settle,
    or manifest unreadable). A file already promoted into a folder that is
    not moved yet is looked for where that folder is now. Performs no
    filesystem mutation — health-check must be able to report without
    changing state.
    """
    # Shared, side-effect-free helpers from the recoverer so the
    # unrecoverable-detection predicate AND the Chinese data-loss-path
    # message strings have a single source of truth and cannot drift
    # between the read-only classifier and the mutating recoverer.
    from litman.core.atomic import (
        _dir_move_plan,
        _folders_unreadable_message,
        _manifest_unreadable_message,
        _read_manifest_dir_moves,
        _read_manifest_relpaths,
        _unrecoverable_message,
    )

    op_id = op_dir.name
    relpaths = _read_manifest_relpaths(op_dir)
    moves = _read_manifest_dir_moves(op_dir)
    if relpaths is None or moves is None:
        return result_cls(
            op_id=op_id,
            kind="unrecoverable",
            n_files=0,
            message=_manifest_unreadable_message(op_id),
        )
    try:
        forward, blocked = _dir_move_plan(op_dir, vault, moves, committed=True)
    except OSError as err:
        blocked = _folders_unreadable_message(vault, err)
    if blocked is not None:
        return result_cls(
            op_id=op_id, kind="unrecoverable", n_files=0, message=blocked
        )

    def promoted_before_the_move(relpath: str) -> bool:
        rel = Path(relpath)
        for src, dst in forward:
            if rel.is_relative_to(dst):
                return (vault / src / rel.relative_to(dst)).exists()
        return False

    recoverable = 0
    unrecoverable: list[str] = []
    for relpath in relpaths:
        staging_path = op_dir / relpath
        target_path = vault / relpath
        if (
            staging_path.exists()
            or target_path.exists()
            or promoted_before_the_move(relpath)
        ):
            recoverable += 1
        else:
            unrecoverable.append(relpath)

    if unrecoverable:
        return result_cls(
            op_id=op_id,
            kind="unrecoverable",
            n_files=recoverable,
            # Read-only probe never promotes: pending voice + the real
            # count of files --fix WOULD roll forward (not 0). The
            # mutating recoverer passes mode="done" + its promoted count.
            message=_unrecoverable_message(
                op_id, unrecoverable, recoverable, mode="pending"
            ),
        )
    return result_cls(
        op_id=op_id,
        kind="rolled_forward",
        n_files=recoverable,
        message=(
            ", ".join(f"{src}/ to {dst}/" for src, dst in forward)
            if forward
            else None
        ),
    )


def check_pdf_viewer(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Probe whether ``lit open`` (M9.1) has a usable PDF viewer.

    Three outcomes:

    * Configured viewer present on PATH, or no config + platform default
      available → no issue (silent ✓ in the health report).
    * Configured ``default_pdf_viewer`` set but not on PATH → warning;
      ``lit open`` would exit 2 with a "install / fix config" hint.
    * No configured viewer and the platform default (``xdg-open`` /
      ``wslview``) is missing on Linux → warning on a desktop (installing
      xdg-utils genuinely fixes it), info on a headless session. (macOS
      always has ``open``; Windows always has ``os.startfile``.)
    * ``xdg-open`` present but no display reachable → info, never warning:
      "this SSH/cron session has no screen" is an environment fact, not
      vault damage, and a warning would make health-check exit 1 forever on
      every headless box — the exact failure mode ``links_unsupported`` was
      demoted for. ``lit open`` there prints the path, which is the correct
      behavior, not a defect.

    Lazy imports avoid pulling pydantic + the viewer module at health-check
    module-load time — both are heavier than checks needs for the unrelated
    schema / id / ref probes.
    """
    from litman.core.config import load_config
    from litman.core.viewer import detect_platform_viewer, is_headless

    try:
        config = load_config(vault)
    except ConfigError:
        # Owned by check_config_readable; narrowed from `except Exception`
        # (review F6) so real bugs propagate rather than reading as clean.
        return []

    out: list[Issue] = []
    configured = config.default_pdf_viewer
    if configured:
        if shutil.which(configured) is None:
            out.append(
                Issue(
                    category="pdf_viewer",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"configured default_pdf_viewer "
                        f"{configured!r} is not on PATH"
                    ),
                    hint=(
                        "install the program or update default_pdf_viewer "
                        "in lit-config.yaml"
                    ),
                )
            )
        return out

    viewer = detect_platform_viewer()
    if viewer is None:
        out.append(
            Issue(
                category="pdf_viewer",
                # Headless: environmental, unfixable from here (info).
                # Desktop: actionable gap — installing xdg-utils fixes it.
                severity="info" if is_headless() else "warning",
                paper_id=None,
                message=(
                    "no platform PDF viewer detected — "
                    "`lit open` will exit 2 and print the path only"
                ),
                hint=(
                    "install xdg-utils (Linux) or wslview (WSL), "
                    "or set default_pdf_viewer in lit-config.yaml"
                ),
            )
        )
    elif viewer == "xdg-open" and is_headless():
        out.append(
            Issue(
                category="pdf_viewer",
                severity="info",
                paper_id=None,
                message=(
                    "xdg-open is installed but no graphical display is "
                    "reachable in this session — `lit open` will exit 2 "
                    "and print the path only"
                ),
                hint=(
                    "set default_pdf_viewer in lit-config.yaml to a viewer "
                    "usable without an X display, or run lit open from a "
                    "session with DISPLAY/WAYLAND_DISPLAY set"
                ),
            )
        )
    return out


def _default_agent_skill_probe() -> tuple[str, Path] | None:
    """The resolved default agent's name + skills dir, or ``None``.

    The single resolution both :func:`check_skill_drift` and the
    ``skill_drift`` arm of :func:`apply_autofix` go through — probe and fix
    must agree on the directory, or a cursor default would have its drift
    detected in the open-standard dir but "fixed" into the claude dir and
    never come clean. ``None`` means the recorded default is not a supported
    catalog agent (hand-edited preferences) — nothing to probe.
    """
    from litman.core import agent_prefs, agents

    name = agent_prefs.load_default_agent() or agents.default_agent_name()
    spec = agents.get_agent(name)
    if spec is None or not spec.supported or spec.skills_dir is None:
        return None
    return name, spec.skills_dir()


def check_skill_drift(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Installed agent skills vs this litman's bundled copies (machine-level).

    The installed skill directory is a deploy artifact of the *package*:
    ``lit install-skill`` copies the bundled files out, and a litman upgrade
    ships new bundled content without touching the installed copy. A stale
    copy fails silently — the agent keeps following an outdated SOP against
    the newer CLI — so it is surfaced here as a warning and refreshed by
    ``--fix`` (a lossless re-copy; files the user added next to SKILL.md are
    never touched).

    Only the *default* agent's skills directory is probed. Sweeping every
    known directory would make the check nag forever about agents the user
    tried once and abandoned — a non-default agent's staleness surfaces at
    the moment it is used (the GUI agent panel computes per-agent state live)
    and the moment it becomes the default. (``--fix`` is wider than the
    probe: it also refreshes stale copies in the other known directories —
    see :func:`apply_autofix` — without widening what is reported here.)
    The issue message names the agent (which pins down the directory) but
    never the path itself: issues flow verbatim into ``GET /api/health``,
    and skills paths stay out of API responses.

    Deliberately NOT flagged:

    * absent skills — never installing one is a respected opt-out (the CLI
      is fully usable without skills), and ``--fix`` installing something the
      user removed would be picking a side (ADR-015);
    * ``linked`` dirs — a symlink/junction points at a copy managed
      elsewhere (a development checkout), where diverging is the point.

    Machine-level, so ``vault`` / ``papers`` are unused — like the pdf-viewer
    probe this reports host state per run, not vault state.
    """
    from litman.core.skill import skill_status

    probe = _default_agent_skill_probe()
    if probe is None:
        return []
    agent_name, parent_dir = probe
    try:
        statuses = skill_status(parent_dir=parent_dir)
    except OSError:
        # Unreadable skills dir / broken package resources — not a vault
        # problem; the install-skill command is where that error belongs.
        return []

    out: list[Issue] = []
    for name in sorted(statuses):
        info = statuses[name]
        if info["state"] != "stale":
            continue
        files = ", ".join(info["stale_files"])
        out.append(
            Issue(
                category="skill_drift",
                severity="warning",
                paper_id=None,
                message=(
                    f"installed agent skill '{name}' (default agent: "
                    f"{agent_name}) is out of date with this litman "
                    f"install ({files})"
                ),
                hint=(
                    "refresh with `lit health-check --fix` or "
                    "`lit install-skill --force`"
                ),
            )
        )
    return out


def check_trash_health(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Orphan sidecars, size warning, and preserved hub folders in ``.trash/``."""
    trash_root = vault / TRASH_DIRNAME
    if not trash_root.is_dir():
        return []

    out: list[Issue] = []
    entry_dirs: dict[str, Path] = {}
    sidecars: dict[str, Path] = {}
    for child in trash_root.iterdir():
        # Filtered at the collection point rather than at the count: the
        # replaced-folders/ container is a directory under .trash/ but is not
        # an entry, and both readers below (orphan-sidecar membership, size
        # warning) would otherwise each have to remember that.
        if child.is_dir() and is_trash_entry_name(child.name):
            entry_dirs[child.name] = child
        elif child.is_file() and child.name.endswith(".meta.yaml"):
            entry_name = child.name[: -len(".meta.yaml")]
            sidecars[entry_name] = child

    for entry_name, sidecar in sidecars.items():
        if entry_name not in entry_dirs:
            out.append(
                Issue(
                    category="orphan_trash_sidecar",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f".trash/{sidecar.name} has no corresponding entry dir"
                    ),
                    hint="run `lit health-check --fix` to remove",
                )
            )

    n_entries = len(entry_dirs)
    if n_entries > TRASH_SIZE_WARN:
        out.append(
            Issue(
                category="trash_size",
                severity="info",
                paper_id=None,
                message=(
                    f".trash/ holds {n_entries} entries; oldest are "
                    f"auto-evicted at {TRASH_MAX_ENTRIES}"
                ),
                hint="run `lit trash empty` to clear it now",
            )
        )

    # Folders `--fix` moved out of a project hub. Unlike a trash entry they
    # have no cap, no eviction and no restore, and a litman_code one is a whole
    # git checkout that `lit sync` pushes to the user's cloud — so the one dim
    # line printed when it happened, which has long since scrolled away, cannot
    # be the only place this is ever visible. Info, not a problem: the same
    # tier as trash_size, and NEVER auto-fixable (decision #3 — the whole point
    # of preserving them is that litman does not delete what it cannot vouch
    # for).
    n_kept = count_replaced_folders(vault)
    if n_kept:
        out.append(
            Issue(
                category="replaced_folders",
                severity="info",
                paper_id=None,
                message=(
                    f".trash/replaced-folders/ holds {n_kept} folder"
                    f"{'s' if n_kept != 1 else ''} kept from project hubs"
                ),
                hint=(
                    "copy anything you still want out of them, then "
                    "`lit trash empty`"
                ),
            )
        )

    return out


def check_supplementary(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Each paper's ``supplementary`` records ↔ its ``supplementary/`` folder.

    Six categories. Two are ``--fix``-able (AUTO_FIXABLE_CATEGORIES):

    * ``supplementary_missing`` (error) — a record whose file is gone.
    * ``supplementary_unrecorded`` (warning) — a plain file in the folder with
      no record (dropped in by hand, or left by a half-finished sync).

    The rest are report-only:

    * ``supplementary_field`` (error) — the list is malformed (not a list, an
      entry that is not a mapping, a bad or repeated ``file``). Nothing else
      is checked for that paper until it is fixed.
    * ``supplementary_changed`` (error) — a recorded file whose content no
      longer matches its checksum: which copy is right is the user's call.
    * ``supplementary_layout`` — what litman will not touch: the folder is a
      link or a file (error, and nothing else is checked for that paper), a
      recorded file was replaced by a link or a folder (error), or the folder
      holds a sub-folder or a link (warning).
    * ``supplementary_unchecked`` (warning) — what cannot be checked: a
      record without ``sha256`` / ``bytes``, or an entry that cannot be read.

    The errors block ``lit sync push``: pushing would delete or overwrite the
    good copy in the cloud (rclone does not follow links either). Content is
    compared by size, and re-hashed only when the file's mtime moved away from
    its ``added`` stamp. Nothing is followed through a link, and an
    unreadable folder is a finding, never an exception. Full tier — it reads
    every paper's metadata (invariant #15).
    """
    # Lazy: this module loads on every `lit` command (the drift hook), the SI
    # module only once a full health check runs.
    from litman.core.locking import SI_DIRNAME
    from litman.core.supplementary import (
        SUPPLEMENTARY_FIELD,
        si_stem,
        supplementary_drift,
    )

    kind_words = {"link": "a link", "dir": "a folder", "other": "a special file"}
    out: list[Issue] = []

    def add(category: str, severity: str, paper_id: str, message: str, hint: str) -> None:
        # A name read off the folder can hold a byte that is not UTF-8; it is
        # shown as U+FFFD, the form the record keeps once the file is added.
        out.append(Issue(category, severity, paper_id, printable(message), printable(hint)))

    for paper in papers:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        paper_id = str(paper_id)
        where = f"papers/{paper_id}/supplementary"
        if SUPPLEMENTARY_FIELD not in paper and not os.path.lexists(
            vault / "papers" / paper_id / SI_DIRNAME
        ):
            continue
        try:
            drift = supplementary_drift(vault, paper_id, paper)
        except OSError as exc:
            add(
                "supplementary_unchecked", "warning", paper_id,
                f"{where} cannot be checked ({exc.__class__.__name__})",
                "fix its permissions, then rerun `lit health-check`",
            )
            continue
        for problem in drift.problems:
            add(
                "supplementary_field", "error", paper_id,
                f"papers/{paper_id}/metadata.yaml: 'supplementary' {problem}",
                "fix it by hand — litman leaves this paper's SI alone until "
                "it parses",
            )
        if drift.folder == "link":
            add(
                "supplementary_layout", "error", paper_id,
                f"{where} is a link; litman does not follow it",
                "replace it with a real folder holding the SI files",
            )
        elif drift.folder == "file":
            add(
                "supplementary_layout", "error", paper_id,
                f"{where} is a file, not a folder",
                "move it out of the paper folder; add SI with `lit add --si`",
            )
        for name in drift.missing:
            add(
                "supplementary_missing", "error", paper_id,
                f"{where}/{name} is recorded but missing",
                "put the file back, or drop the record: "
                f"`lit rm {paper_id} --si {si_stem(name)} --yes`",
            )
        for name in drift.changed:
            add(
                "supplementary_changed", "error", paper_id,
                f"{where}/{name} no longer matches its recorded checksum",
                "put the original back; to keep this one, copy it out, "
                f"`lit rm {paper_id} --si {si_stem(name)} --yes`, then "
                f"`lit add --si {paper_id}` the copy",
            )
        for name, kind in drift.odd_recorded:
            add(
                "supplementary_layout", "error", paper_id,
                f"{where}/{name} is recorded, but is now {kind_words[kind]}",
                "put the recorded file back in its place",
            )
        for name in drift.unrecorded:
            add(
                "supplementary_unrecorded", "warning", paper_id,
                f"{where}/{name} has no record in metadata.yaml",
                "`lit health-check --fix` checks it and records it",
            )
        for name, kind in drift.odd_strays:
            if kind == "dir":
                message = f"{where}/{name}/ is a folder; SI files are single files"
                hint = "move it out; to keep it as SI, zip it and `lit add --si` the zip"
            elif kind == "link":
                message = f"{where}/{name} is a link; litman does not follow it"
                hint = "replace it with a copy of the file itself"
            else:
                message = f"{where}/{name} is not a regular file"
                hint = "move it out of supplementary/"
            add("supplementary_layout", "warning", paper_id, message, hint)
        for name, why in drift.unchecked:
            if why == "no checksum":
                add(
                    "supplementary_unchecked", "warning", paper_id,
                    f"{where}/{name} has no sha256 / bytes on record — its "
                    "content can't be verified",
                    f"copy it out, `lit rm {paper_id} --si {si_stem(name)} "
                    f"--yes`, then `lit add --si {paper_id}` the copy to record "
                    "its checksum",
                )
            else:
                path = f"{where}/{name}" if name else where
                add(
                    "supplementary_unchecked", "warning", paper_id,
                    f"{path} cannot be read ({why}); litman can't check it",
                    "fix its permissions, then rerun `lit health-check`",
                )
    return out


def _cited_finding(paper_id: str, problem: CitationProblem) -> tuple[str, str]:
    """Message and hint for one stale SI citation (see check_supplementary_cited)."""
    from litman.core.supplementary import original_label

    citation = problem.citation
    where = f"{citation.file}:{citation.line}"
    record = problem.record
    if problem.kind == "predates" and record is not None:
        section = citation.section.isoformat() if citation.section else "?"
        when = (
            f"added later ({problem.added.isoformat()})"
            if problem.added is not None
            else "whose added date can't be read"
        )
        message = (
            f"{where} (§{section}) cites {citation.token}, {when} — may refer "
            "to a file since replaced"
        )
        label = original_label(record)
        hint = (
            f"if it means this file, write its name after it: "
            f"{record['file']}（{label}）"
            if label
            else f"check which file it means — `lit show {paper_id}` lists "
            "each SI with its original name"
        )
        return message, hint
    if record is None:
        message = f"{where} cites {citation.token}, which this paper no longer has"
        hint = (
            f"point it at the right file (`lit show {paper_id}` lists the SI), "
            "or remove it"
        )
    else:
        now = original_label(record) or record.get("original")
        # Read from the user's notes: shown through the same filter as a
        # record's name, so a tab or a bidi mark cannot garble the line.
        cited_as = original_label({"original": citation.original}) or "?"
        message = (
            f"{where} cites {citation.token} as {cited_as}, but "
            f"{record['file']} is now {now}"
        )
        hint = (
            f"check which file it means — `lit show {paper_id}` lists each SI "
            "with its original name"
        )
    if problem.moved_to:
        message += f" — that file is now {problem.moved_to}"
        hint = f"change it to {problem.moved_to}"
    return message, hint


def check_supplementary_cited(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """``si-NN`` citations in each paper's notes.md / discussion.md ↔ its
    ``supplementary`` records (``supplementary_cited``, warning).

    A deleted file's number is given again, so a citation can outlive its
    file or come to name another one. Each finding names the file and line:

    * the paper has no file by that number (or by that exact name);
    * the original name written after it is not the one on record — and,
      when another record holds that name, the number it has now;
    * in discussion.md, written without an original name in a section dated
      before the record was added, or the record's ``added`` cannot be read:
      it may mean a file since replaced.

    A notes file that cannot be read is a finding too (invariant #14).
    Report only, never ``--fix``: the text is the user's. Kept out of the
    ``lit sync push`` gate (see _PUSH_GATE_EXCLUDED_CATEGORIES). A paper
    whose ``supplementary`` field is malformed is skipped — it already gets
    ``supplementary_field``. Full tier: it reads every paper's notes
    (invariant #15).
    """
    # Lazy, like check_supplementary: this module loads on every `lit` call.
    from litman.core.si_citations import (
        NOTE_FILES,
        citation_problems,
        read_si_citations,
    )
    from litman.core.supplementary import (
        SUPPLEMENTARY_FIELD,
        field_problems,
        list_supplementary,
    )

    out: list[Issue] = []
    for paper in papers:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        paper_id = str(paper_id)
        if field_problems(paper.get(SUPPLEMENTARY_FIELD)):
            continue
        citations, unreadable = read_si_citations(vault / "papers" / paper_id)
        found: list[tuple[int, int, Issue]] = []
        for name, why in unreadable:
            found.append((
                NOTE_FILES.index(name),
                0,
                Issue(
                    "supplementary_cited", "warning", paper_id,
                    f"{name} cannot be read ({why}); its SI citations went "
                    "unchecked",
                    "save it as UTF-8 (or fix its permissions), then rerun "
                    "`lit health-check`",
                ),
            ))
        for problem in citation_problems(citations, list_supplementary(paper)):
            message, hint = _cited_finding(paper_id, problem)
            citation = problem.citation
            found.append((
                NOTE_FILES.index(citation.file),
                citation.line,
                Issue("supplementary_cited", "warning", paper_id, message, hint),
            ))
        out.extend(issue for _, _, issue in sorted(found, key=lambda f: f[:2]))
    return out


def check_code_clone_integrity(
    vault: Path, papers: list[dict[str, Any]]
) -> list[Issue]:
    """Enforce invariant #12 — paper ``code-clones`` ↔ ``codes/<name>/`` duality.

    Three failure modes, all under the single ``code_clone_integrity``
    category so the report groups them together:

    * **dangling ref** (``error``) — a paper's ``code-clones`` lists a
      ``<name>`` with no ``codes/<name>/`` directory + ``repo-meta.yaml``
      on disk. Active misdirection: the reader follows the link and finds
      nothing. One Issue per ``(paper_id, missing_repo_name)`` pair so
      each can be addressed individually.
    * **dangling clone** (``warning``) — ``codes/<name>/repo-meta.yaml``
      exists but no paper references ``<name>``. Disk hygiene only; the
      clone wastes space but does not mislead.
    * **missing repo-meta** (``error``) — ``codes/<name>/`` is a directory
      but has no ``repo-meta.yaml``. Whatever atomic clone+link op
      created the directory failed to land the metadata file, so the repo
      is unrecoverable and unreferenceable.

    Defensive: silently skips when ``codes/`` does not exist (vault may
    legitimately have no clones) and skips non-directory children of
    ``codes/`` (stray files / symlinks are not in scope here). Per
    invariant #12 "不自动修复" the category is NOT in
    :data:`AUTO_FIXABLE_CATEGORIES`.
    """
    codes_dir = vault / CODES_DIRNAME
    if not codes_dir.is_dir():
        return []

    disk_repos: set[str] = set()
    disk_dirs_no_meta: set[str] = set()
    for child in codes_dir.iterdir():
        if not child.is_dir():
            continue
        if (child / REPO_META_FILENAME).is_file():
            disk_repos.add(child.name)
        else:
            disk_dirs_no_meta.add(child.name)

    # Build referenced_repos plus a reverse map {repo_name: [paper_ids…]}
    # so dangling-ref Issues can pin to the offending paper(s).
    references: dict[str, list[str]] = {}
    for p in papers:
        pid = p.get("id")
        if not pid:
            continue
        for name in p.get("code-clones") or []:
            if not isinstance(name, str) or not name:
                continue
            references.setdefault(name, []).append(str(pid))
    referenced_repos = set(references.keys())

    # Ids of papers whose metadata actually loaded. Used by the #6c symmetric
    # check to distinguish "paper exists but does not list the repo" (a real
    # one-sided binding) from "paper exists but its metadata failed to load"
    # (a corruption already owned by check_paper_dir_validity / check_schema —
    # flagging it here too would be a duplicate, not a silent skip).
    loaded_paper_ids = {str(p.get("id")) for p in papers if p.get("id")}

    out: list[Issue] = []

    # Dangling refs: emit one Issue per (paper, missing repo) pair.
    for repo_name in sorted(referenced_repos - disk_repos):
        for pid in references[repo_name]:
            out.append(
                Issue(
                    category="code_clone_integrity",
                    severity="error",
                    paper_id=pid,
                    message=(
                        f"{pid!r}.code-clones references {repo_name!r} "
                        f"but no codes/{repo_name}/repo-meta.yaml exists"
                    ),
                    hint=(
                        f"`lit code unlink {repo_name} --paper {pid}` "
                        "to drop the broken edge, or `lit code restore-all` "
                        "to re-clone if upstream metadata is recoverable"
                    ),
                )
            )

    # Dangling clones: vault-level finding, no owning paper.
    for repo_name in sorted(disk_repos - referenced_repos):
        out.append(
            Issue(
                category="code_clone_integrity",
                severity="warning",
                paper_id=None,
                message=(
                    f"codes/{repo_name}/ is a dangling clone — "
                    f"no paper references {repo_name!r} in code-clones"
                ),
                hint=(
                    f"`lit code rm {repo_name}` to delete, or "
                    f"`lit code link {repo_name} --paper <id>` to bind it"
                ),
            )
        )

    # Broken clone dirs: codes/<name>/ without repo-meta.yaml. Vault-level.
    for repo_name in sorted(disk_dirs_no_meta):
        out.append(
            Issue(
                category="code_clone_integrity",
                severity="error",
                paper_id=None,
                message=(
                    f"codes/{repo_name}/ exists but has no "
                    f"{REPO_META_FILENAME} (broken clone, likely from a "
                    "failed atomic clone+link op)"
                ),
                hint=(
                    "`lit code restore-all` to re-clone if the upstream "
                    "is known, or remove the directory manually"
                ),
            )
        )

    # #6b: repo-meta.yaml present but the actual codes/<name>/repo/ checkout is
    # missing. The metadata wrapper survived but the git clone did not (e.g. a
    # synced vault that excludes the bulky repo/, or an interrupted clone).
    # `lit code restore-all` re-clones from the recorded upstream.
    for repo_name in sorted(disk_repos):
        if not (codes_dir / repo_name / REPO_DIRNAME).is_dir():
            out.append(
                Issue(
                    category="code_clone_integrity",
                    severity="warning",
                    paper_id=None,
                    message=(
                        f"codes/{repo_name}/ has {REPO_META_FILENAME} but no "
                        f"{REPO_DIRNAME}/ checkout (clone missing)"
                    ),
                    hint=(
                        f"`lit code restore-all` to re-clone {repo_name} from "
                        "its recorded upstream, or `lit code rm "
                        f"{repo_name}` to drop the metadata"
                    ),
                )
            )

    # #6c: repo-meta.yaml's reverse ``papers:`` list ↔ those papers actually
    # exist on disk. A paper id recorded in the back-reference but with no
    # papers/<id>/ directory is a dangling reverse edge (the paper was rm'd
    # without updating the repo's reverse list). Directory enumeration is the
    # truth side (no per-paper metadata read needed for existence).
    existing_paper_dirs = set()
    papers_root = vault / "papers"
    if papers_root.is_dir():
        existing_paper_dirs = {
            c.name for c in papers_root.iterdir() if c.is_dir()
        }
    for repo_name in sorted(disk_repos):
        meta_file = codes_dir / repo_name / REPO_META_FILENAME
        try:
            meta = _yaml.load(meta_file.read_text(encoding="utf-8"))
        except (OSError, YAMLError) as exc:
            out.append(
                Issue(
                    category="code_clone_integrity",
                    severity="error",
                    paper_id=None,
                    message=(
                        f"codes/{repo_name}/{REPO_META_FILENAME} is unparseable "
                        f"({exc.__class__.__name__}) — cannot verify its papers"
                    ),
                    hint="fix the YAML by hand or `lit code rm` + re-clone",
                )
            )
            continue
        if not isinstance(meta, dict):
            continue
        for back_ref in meta.get("papers") or []:
            if not isinstance(back_ref, str) or not back_ref:
                continue
            if back_ref not in existing_paper_dirs:
                out.append(
                    Issue(
                        category="code_clone_integrity",
                        severity="error",
                        paper_id=back_ref,
                        message=(
                            f"codes/{repo_name}/{REPO_META_FILENAME} lists paper "
                            f"{back_ref!r} but papers/{back_ref}/ does not exist"
                        ),
                        hint=(
                            # The paper is gone, so `lit code unlink` (which
                            # needs an existing paper) cannot clear this stale
                            # reverse edge. Remove it by hand, or restore the
                            # paper if it was mis-deleted.
                            f"remove {back_ref!r} from the papers: list in "
                            f"codes/{repo_name}/{REPO_META_FILENAME}, or "
                            f"`lit trash restore {back_ref}` if it was "
                            "mis-deleted"
                        ),
                    )
                )
            elif (
                back_ref in loaded_paper_ids
                and back_ref not in references.get(repo_name, [])
            ):
                # Symmetric drift: the repo's reverse list names a paper that
                # exists and loaded, but that paper's code-clones does NOT
                # include this repo. The forward edge was dropped one-sidedly
                # (e.g. a hand-edit) while the reverse survived. Neither the
                # dangling-ref nor the dangling-reverse check above catches it.
                out.append(
                    Issue(
                        category="code_clone_integrity",
                        severity="error",
                        paper_id=back_ref,
                        message=(
                            f"codes/{repo_name}/{REPO_META_FILENAME} lists paper "
                            f"{back_ref!r} but {back_ref!r}.code-clones does not "
                            f"include {repo_name!r} (one-sided binding)"
                        ),
                        hint=(
                            f"`lit code link {repo_name} --paper {back_ref}` to "
                            f"restore the forward edge, or `lit code unlink "
                            f"{repo_name} --paper {back_ref}` to drop the "
                            "reverse edge"
                        ),
                    )
                )

    return out
