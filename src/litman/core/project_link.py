"""``lit link`` / ``lit unlink`` core logic (M5.2).

Bridges the global vault with project-local working directories via
relative symlinks. A linked paper means three things in concert:

1. The paper's ``metadata.yaml`` lists the project in its ``projects``
   field (single source of truth — derived REFERENCES.md reads it).
2. ``<project_dir>/litman_reflib/<paper-id>`` is a relative symlink into
   ``<vault>/papers/<paper-id>/``, so the user can ``cd`` into the
   paper from the project root.
3. For each repo in the paper's ``code-clones``, a parallel symlink at
   ``<project_dir>/litman_code/<repo-name>`` → ``<vault>/codes/<repo>/repo/``
   (the git checkout, not the metadata wrapper).

Atomicity: the metadata + INDEX.json write goes through ``staged_write``;
link creation and REFERENCES.md regeneration are post-staging steps
(filesystem-mutating but cheap to redo, recoverable via
``lit link --rebuild-all``).

Cross-platform: link creation routes through ``core.portable_link``
(relative symlinks on POSIX, junctions on Windows), which gracefully
degrades on filesystems that cannot hold links (FAT32/exFAT, network
shares). The metadata side of every operation succeeds regardless; only
the convenience links may be skipped (ADR-005).
"""

from __future__ import annotations

import errno
import filecmp
import io
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NamedTuple

from litman.core.atomic import staged_write
from litman.core.config import config_to_yaml_dict, load_config
from litman.core.dates import now_iso
from litman.core.document import list_papers, read_metadata_or_raise
from litman.core.locking import rmtree
from litman.core.portable_link import (
    is_portable_link,
    links_supported,
    make_portable_link,
    remove_link_if_present,
    warn_hub_entry_unmovable,
)
from litman.core.project_refs import (
    LITERATURE_SUBDIR,
    REFERENCES_FILENAME,
    load_project_member_metas,
    write_references_md,
)
from litman.core.taxonomy import (
    find_referencing_papers,
    parse_taxonomy,
    update_user_dict_section,
)
from litman.core.views import (
    papers_for_index,
    render_index,
    view_fields_snapshot,
)
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import LitmanError, PaperNotFoundError, TaxonomyError

_PROJECTS_DICT = "projects"

CODE_SUBDIR = "litman_code"

# Container under <vault>/.trash/ for hub folders moved aside by
# settle_hub_entry. Deliberately NOT shaped like a trash entry
# ("<paper-id>-<UTC timestamp>"), so `lit trash list` / `lit trash restore`
# skip it on the name rule alone — one recycle bin, one restore entity.
REPLACED_FOLDERS_DIRNAME = "replaced-folders"

# Files above this size compare on length alone. A vault entry that big is a
# PDF; re-reading every byte of it on each health-check buys little, and the
# only cost of a false "differs" verdict is that the folder is preserved in
# .trash/ instead of deleted.
_MAX_BYTE_COMPARE = 1024 * 1024

_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    preserve_quotes=True,
    default_flow_style=False,
)


class LinkError(LitmanError):
    """``lit link`` / ``lit unlink`` rejected: project not registered,
    project dir missing on disk, or an invariant was violated."""


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _resolve_project_dir(
    project: str, registry: dict[str, str]
) -> Path:
    """Look up the project's on-disk path; refuse cleanly on miss."""
    if project not in registry:
        raise LinkError(
            f"Project {project!r} is not registered in lit-config.yaml's "
            f"`projects:` map. Add it first, e.g.:\n"
            f"  projects:\n    {project}: /path/to/{project}\n"
            f"Registered: {sorted(registry) or '(none)'}"
        )
    project_dir = Path(registry[project]).expanduser()
    if not project_dir.is_dir():
        raise LinkError(
            f"Project {project!r} resolves to {project_dir}, "
            "but that directory does not exist on this machine. "
            "Either create / sync the project, or fix the path in "
            "lit-config.yaml's `projects:` map."
        )
    return project_dir


def _project_link_paths(
    project_dir: Path, paper_id: str, code_clones: list[str]
) -> tuple[Path, list[Path]]:
    """Compute the paper-link path + per-repo code-link paths under a project."""
    paper_link = project_dir / LITERATURE_SUBDIR / paper_id
    code_links = [project_dir / CODE_SUBDIR / r for r in code_clones]
    return paper_link, code_links


HubVerdict = Literal[
    "clear", "replaced-copy", "moved-aside", "blocked", "failed"
]


class HubSettlement(NamedTuple):
    """What :func:`settle_hub_entry` did with one hub position."""

    verdict: HubVerdict
    moved_to: Path | None  # set iff verdict == 'moved-aside'

    @property
    def position_occupied(self) -> bool:
        """True when a real folder is still sitting there.

        The caller must not attempt the link upsert: it would fail, and the
        user has already been told why in the one case ('failed') where
        something went wrong.
        """
        return self.verdict in ("blocked", "failed")


def _tree_shape(root: Path) -> dict[tuple[str, ...], int | None] | None:
    """Map every entry under ``root`` to its size, ``None`` for directories.

    Keys are ``relative_to(root).parts`` tuples rather than joined strings so
    the comparison never depends on the path separator. Links are skipped both
    as entries and as descent targets — an expanded copy has none, and the
    vault entry's own links (there are none today) would be a difference we
    cannot judge. Any ``OSError`` collapses the whole answer to ``None``:
    unreadable is not provably identical.
    """

    def _reraise(err: OSError) -> None:
        # os.walk's default onerror SWALLOWS a failed scandir and yields the
        # directory as empty. An unreadable subtree would then look equal to
        # anything, and this answer decides whether a folder gets deleted.
        raise err

    shape: dict[tuple[str, ...], int | None] = {}
    try:
        for dirpath, dirnames, filenames in os.walk(
            root, onerror=_reraise, followlinks=False
        ):
            here = Path(dirpath)
            keep: list[str] = []
            for name in dirnames:
                if is_portable_link(here / name):
                    continue
                keep.append(name)
                shape[(here / name).relative_to(root).parts] = None
            dirnames[:] = keep
            for name in filenames:
                child = here / name
                if is_portable_link(child):
                    continue
                shape[child.relative_to(root).parts] = child.stat().st_size
    except OSError:
        return None
    return shape


def _holds_no_files(root: Path) -> bool:
    """True when ``root``'s tree holds no file and no link — nothing to lose.

    Explorer does not copy *through* a junction: it leaves a same-named EMPTY
    directory behind (wangq measured it on Windows 2026-09-09 — ``dir`` on the
    copy reports "0 个文件"), and that is the commonest way a hub position ends
    up occupied. Preserving such a folder under ``.trash/`` buys nothing and
    costs the user a trash entry plus a standing health-check reminder for
    every position, which is the cleanup burden this whole feature exists to
    remove. Deleting it does not weaken decision #3: there is no content.

    Empty-directory scaffolding still counts as empty. A link does NOT — it is
    the one thing in here that names something outside, so it is treated as
    content even though the target survives. An unreadable tree is not empty
    either: unprovable is not proven, and the caller's conservative branch
    keeps it.
    """

    def _reraise(err: OSError) -> None:
        # os.walk's default onerror swallows a failed scandir and yields the
        # directory as empty, which would read here as "nothing to lose".
        raise err

    try:
        for dirpath, dirnames, filenames in os.walk(
            root, onerror=_reraise, followlinks=False
        ):
            if filenames:
                return False
            here = Path(dirpath)
            if any(is_portable_link(here / name) for name in dirnames):
                return False
    except OSError:
        return False
    return True


def _is_verbatim_copy(copy_dir: Path, target_dir: Path) -> bool:
    """True when ``copy_dir`` holds exactly what ``target_dir`` holds.

    Same relative-path set, same kind per path, same size per file, and — up to
    :data:`_MAX_BYTE_COMPARE` — the same bytes.
    """
    if not copy_dir.is_dir() or not target_dir.is_dir():
        return False
    # filecmp memoises verdicts on (path, size, mtime). Two settlements landing
    # inside one mtime tick — the same hub position twice in one test, one
    # health-check over many projects — would otherwise reuse a stale answer.
    filecmp.clear_cache()
    left = _tree_shape(copy_dir)
    right = _tree_shape(target_dir)
    if left is None or right is None or left.keys() != right.keys():
        return False
    for rel, size in left.items():
        if right[rel] != size:
            return False
        if size is None or size > _MAX_BYTE_COMPARE:
            continue
        try:
            if not filecmp.cmp(
                copy_dir.joinpath(*rel), target_dir.joinpath(*rel), shallow=False
            ):
                return False
        except OSError:
            return False
    return True


def _hub_target(vault: Path, hub: str, name: str) -> Path | None:
    """The vault entry a hub position of this name should point at.

    ``None`` when there is none — the paper was deleted, or the clone was
    never restored on this machine. Derived from the position's name because
    the wipe loop settles what it finds on disk, which by definition includes
    names no longer in the project's membership.
    """
    if hub == CODE_SUBDIR:
        candidate = (vault / "codes" / name / "repo").resolve()
    else:
        candidate = (vault / "papers" / name).resolve()
    return candidate if candidate.is_dir() else None


def _move_aside(src: Path, dest_parent: Path, name: str) -> Path:
    """Move ``src`` to ``dest_parent/name``, returning where it landed."""
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest = dest_parent / name
    # lexists, not exists: a broken symlink still occupies the name, and
    # rename() would silently replace an empty directory. Loop rather than
    # retry once — a four-hex suffix can collide too.
    while os.path.lexists(dest):
        dest = dest_parent / f"{name}-{uuid.uuid4().hex[:4]}"
    try:
        src.rename(dest)
    except OSError as err:
        # ONLY a cross-device move earns the copy fallback (Windows maps
        # ERROR_NOT_SAME_DEVICE to EXDEV too). Any other refusal — a locked
        # child, a read-only parent — must stay a failure: copying first would
        # leave a full copy in .trash/ AND the folder still in the hub, and the
        # next run, which our own message asks for, would copy it again under a
        # new timestamp. For a code hub that is a whole checkout each time.
        if err.errno != errno.EXDEV:
            raise
        # NOT shutil.move for the copy: its own cross-device path finishes
        # with a bare shutil.rmtree, which trips over the read-only
        # metadata.yaml the copy inherited from the vault (ADR-005) — and
        # "project on a different drive from the vault" is precisely the
        # situation this whole code path exists for.
        shutil.copytree(src, dest, symlinks=True)
        rmtree(src)
    return dest


def settle_hub_entry(
    link_path: Path,
    target: Path | None,
    *,
    vault: Path,
    project: str,
    hub: str,
) -> HubSettlement:
    """Clear a project hub position so a link can take it.

    Copying a project directory between machines (scp -r, WinSCP, unpacking a
    tar, Explorer, cloud sync) expands every ``litman_reflib`` /
    ``litman_code`` link into a real folder holding a full copy of the vault
    entry. The link upsert then refuses to remove it — correctly, that is user
    data — and the user is left deleting a dozen folders by hand. This settles
    the position instead: a folder that matches the vault byte for byte is
    redundant and goes, and so does one that holds nothing at all
    (Explorer leaves an empty directory rather than copying through the
    junction); anything else is preserved under
    ``<vault>/.trash/replaced-folders/<project>/<hub>/`` first. Unknown content
    is never rmtree'd.

    ``target`` is the vault entry the link should point at, or ``None`` when
    there is none (paper deleted, clone never restored) — a folder with no
    original can never be proven redundant, so it is preserved.

    Returns:
        ``('clear', None)`` — nothing in the way: position empty, already a
        link, or a real file (which the upsert unlinks as it always has);
        ``('blocked', None)`` — a real folder on a filesystem that cannot hold
        links, left alone: deleting it there would take away the only
        browsable copy and give back nothing;
        ``('replaced-copy', None)`` — a redundant copy was deleted, either
        because it matched the vault or because it held nothing;
        ``('moved-aside', dest)`` — the folder now lives at ``dest``;
        ``('failed', None)`` — the filesystem refused to move or delete it
        (a locked child, a read-only parent); warned about, left alone, and
        the rest of the rebuild carries on.
    """
    # is_portable_link BEFORE is_dir: a junction answers is_dir() True, so the
    # other order would file every healthy Windows link as a folder copy.
    if is_portable_link(link_path) or not link_path.is_dir():
        return HubSettlement("clear", None)
    # Probe the PROJECT dir (link_path is <project_dir>/<hub>/<name>), not the
    # hub: same key as check_project_references so both read one cached
    # verdict, and the probe file never lands in the hub the rebuild is
    # iterating over.
    if not links_supported(link_path.parent.parent):
        return HubSettlement("blocked", None)
    if target is not None and _is_verbatim_copy(link_path, target):
        try:
            # locking.rmtree, not shutil's: the copy carries the vault's
            # read-only metadata.yaml / paper.pdf attributes, which stop a
            # plain delete on Windows (ADR-005 dimension F).
            rmtree(link_path)
        except OSError as err:
            warn_hub_entry_unmovable(link_path, err)
            return HubSettlement("failed", None)
        return HubSettlement("replaced-copy", None)
    if _holds_no_files(link_path):
        # Explorer's expansion of a junction: the folder is there, its content
        # is not. Same disposal as a verbatim copy — there is nothing in it to
        # preserve — and it reports as one, so the user is not handed a trash
        # entry per hub position for folders that hold nothing.
        try:
            rmtree(link_path)
        except OSError as err:
            warn_hub_entry_unmovable(link_path, err)
            return HubSettlement("failed", None)
        return HubSettlement("replaced-copy", None)
    # Local import: trash reaches back here for CODE_SUBDIR, so this edge can
    # only run at call time. Mirrors the checks / correctors imports below.
    from litman.core.trash import TRASH_DIRNAME, _utc_compact_now

    try:
        dest = _move_aside(
            link_path,
            vault / TRASH_DIRNAME / REPLACED_FOLDERS_DIRNAME / project / hub,
            f"{link_path.name}-{_utc_compact_now()}",
        )
    except OSError as err:
        # One locked child (a PDF open in a viewer, an indexer, antivirus)
        # must cost this position only. Aborting would take the rest of the
        # rebuild, every other project and every other --fix category with it
        # — strictly worse than the warning-per-folder this replaced. Mirrors
        # empty_trash / enforce_cap, which skip an unremovable entry the same
        # way. A part-written destination may be left under .trash/; the
        # source is only removed once the copy is complete, so nothing is lost.
        warn_hub_entry_unmovable(link_path, err)
        return HubSettlement("failed", None)
    return HubSettlement("moved-aside", dest)


def _papers_using_repo_in_project(
    papers: list[dict[str, Any]],
    project: str,
    repo_name: str,
    *,
    exclude_paper_id: str | None = None,
) -> list[str]:
    """List paper ids tagged with ``project`` that bind ``repo_name``.

    Used by unlink to decide whether a project-level code symlink should
    stay or go: if another linked paper still references the repo, keep
    the symlink; otherwise remove it.
    """
    matched = []
    for p in papers:
        pid = p.get("id")
        if not pid or pid == exclude_paper_id:
            continue
        if project not in (p.get("projects") or []):
            continue
        if repo_name in (p.get("code-clones") or []):
            matched.append(str(pid))
    return matched


def reconcile_project_code_links(
    vault: Path,
    project: str,
    project_dir: Path,
    papers: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Make ``<project_dir>/litman_code/`` match the derived code-clone truth.

    The project-side ``litman_code/<repo>`` symlinks are a pure function of
    ``(membership, code-clones, clone-presence)``: every repo bound (via
    ``code-clones``) by some paper tagged with ``project`` whose
    ``codes/<repo>/repo`` clone is present locally should have exactly one
    symlink, and nothing else should. This reconciles the on-disk symlink set
    to that expected set — creating the missing, removing the orphaned, and
    re-pointing the dangling (a link whose name matches but whose target is
    gone, e.g. after the vault moved, is treated as missing, not present) —
    without the full-vault wipe ``rebuild_all_project_links`` performs.
    Idempotent.

    Clone presence is a same-vault fact (``codes/`` lives under the vault), so a
    missing clone reliably means "not restored locally", not a transient mount
    blip — matching ``link_paper_to_project`` / ``rebuild_all_project_links``,
    which likewise skip symlinks for absent clones (a bound-but-missing clone is
    owned by invariant #12 / ``lit code restore-all``).

    Returns ``{"created": [...], "removed": [...]}`` (sorted repo names).
    """
    expected: dict[str, Path] = {}
    for p in papers:
        if project not in (p.get("projects") or []):
            continue
        for repo_name in p.get("code-clones") or []:
            repo_target = (vault / "codes" / repo_name / "repo").resolve()
            if repo_target.exists():
                expected[repo_name] = repo_target

    code_dir = project_dir / CODE_SUBDIR
    on_disk: set[str] = set()
    resolving: set[str] = set()
    if code_dir.is_dir():
        for child in code_dir.iterdir():
            if is_portable_link(child):
                on_disk.add(child.name)
                # exists() FOLLOWS the link. A dangling bridge (the vault —
                # or the clone — moved out from under it) must count as
                # ABSENT: by name it looks present and the create loop would
                # skip it, leaving reconcile powerless on exactly the state
                # it exists to repair. The upsert re-points it in place.
                if child.exists():
                    resolving.add(child.name)

    created: list[str] = []
    for repo_name, repo_target in expected.items():
        if repo_name in resolving:
            continue
        link_path = code_dir / repo_name
        if settle_hub_entry(
            link_path,
            repo_target,
            vault=vault,
            project=project,
            hub=CODE_SUBDIR,
        ).position_occupied:
            continue
        if make_portable_link(link_path, repo_target):
            created.append(repo_name)
    removed: list[str] = []
    for repo_name in on_disk - set(expected):
        if remove_link_if_present(code_dir / repo_name):
            removed.append(repo_name)
    return {"created": sorted(created), "removed": sorted(removed)}


def find_dangling_bridges(
    projects: dict[str, str],
    dir_status: dict[str, bool | None],
    exists_fn: Callable[[list[str]], dict[str, bool | None]],
) -> dict[str, list[Path]]:
    """Per project: bridge symlinks whose target is definitely gone.

    The one shared detection core behind ``check_project_bridge_dangling``
    (the cheap health check), the Tier-1 ``[Y/n]`` rebuild prompt, and the
    server's switch-time heal — a single collector so the three can never
    disagree about what "dangling" means.

    A bridge is ``<project_dir>/litman_reflib/<id>`` or
    ``<project_dir>/litman_code/<repo>``: a relative symlink that leaves the
    project tree and re-enters the vault at the location the vault had when
    the link was written. Moving the vault dangles every bridge at once while
    the vault itself and each project stay individually healthy — which is
    why nothing else sees it: ``check_project_references`` compares link
    NAMES against membership, and the names still match.

    Args:
        projects: lit-config.yaml ``projects:`` map (name → path, raw).
        dir_status: bounded-stat verdict per expanded project path (``True``
            / ``False`` / ``None``). Only projects definitely present are
            scanned — a missing dir is ``project_path_exists``'s finding, not
            a bridge problem.
        exists_fn: batch target probe keyed by path string
            (``_exists_bounded`` in production — mount-safe per ADR-014).
            ``Path.exists()`` FOLLOWS symlinks, so ``False`` on a just-listed
            link means the TARGET is gone; ``None`` (slow / dropped mount) is
            never counted as dangling.

    Returns:
        ``{project_name: [dangling link paths]}`` for projects with at least
        one definitely-dangling bridge; insertion order follows sorted
        project names, links sorted within each hub.
    """
    links: dict[str, list[Path]] = {}
    for name in sorted(projects):
        project_dir = Path(projects[name]).expanduser()
        if dir_status.get(str(project_dir)) is not True:
            continue
        if not project_dir.is_dir():
            continue
        hub_links: list[Path] = []
        try:
            for sub in (LITERATURE_SUBDIR, CODE_SUBDIR):
                hub = project_dir / sub
                if not hub.is_dir():
                    continue
                for child in sorted(hub.iterdir()):
                    if is_portable_link(child):
                        hub_links.append(child)
        except OSError:
            # A hub that vanishes or refuses listing between the bounded dir
            # probe and this scan (dying mount, chmod'd hub): skip THIS
            # project, keep scanning the rest. On the Tier-1 hot path an
            # exception escaping here would take down the whole drift hook
            # (its outer wrapper swallows everything, registry/project
            # prompts included). Skipping one project's scan is the
            # documented, bounded loss (invariant #14); Tier-2 health-check
            # revisits it.
            continue
        if hub_links:
            links[name] = hub_links

    if not links:
        return {}

    target_status = exists_fn(
        [str(p) for name_links in links.values() for p in name_links]
    )
    out: dict[str, list[Path]] = {}
    for name, name_links in links.items():
        dangling = [
            p for p in name_links if target_status.get(str(p)) is False
        ]
        if dangling:
            out[name] = dangling
    return out


def refresh_project_code_links(
    vault: Path, paper_id: str
) -> dict[str, dict[str, list[str]]]:
    """Re-derive ``litman_code/`` symlinks for every project ``paper_id`` is in.

    The seam that keeps the project-side code symlinks in step with a paper's
    ``code-clones`` after a ``lit code add`` / ``link`` / ``unlink`` (and the
    ``lit code rm`` cascade). Previously only ``lit link`` / ``--rebuild-all``
    materialized these symlinks, so a repo bound *after* the paper was last
    linked never got one (and a repo unbound afterwards never lost it). Reads
    fresh, post-mutation metadata; a pure derived refresh that never mutates
    metadata. Skips projects not registered in ``lit-config.yaml`` or whose
    directory is absent on this machine (those are owned by the
    ``project_path_exists`` health check).

    Returns ``{project: {"created": [...], "removed": [...]}}`` for the
    projects actually reconciled.
    """
    papers = list_papers(vault)
    paper = next((p for p in papers if p.get("id") == paper_id), None)
    if paper is None:
        return {}
    projects = paper.get("projects") or []
    if not projects:
        return {}
    registry = load_config(vault).projects
    out: dict[str, dict[str, list[str]]] = {}
    for project in projects:
        project_dir_str = registry.get(project)
        if not project_dir_str:
            continue
        project_dir = Path(project_dir_str).expanduser()
        if not project_dir.is_dir():
            continue
        out[project] = reconcile_project_code_links(
            vault, project, project_dir, papers
        )
    return out


def link_paper_to_project(
    vault: Path,
    paper_id: str,
    project: str,
    registry: dict[str, str],
    *,
    relevance: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    """Link a paper to a project (atomic metadata + symlinks + REFERENCES.md).

    Steps:
        1. Resolve project_dir from the registry; refuse on missing.
        2. Load paper's metadata; refuse if paper missing.
        3. Update metadata:
           - Append project to ``projects`` if absent (deduped).
           - Set ``relevance-<project>`` when an explicit ``relevance`` is
             provided — this OVERWRITES any existing note (the CLI
             ``--relevance`` flag is "set in one shot"). Left untouched only
             when ``relevance`` is ``None`` (the flag was omitted); the
             ``!= existing_relevance`` check is an idempotency guard, not a
             don't-clobber guard.
           - Set ``priority-<project>`` the same way when an explicit
             ``priority`` (A/B/C) is provided. This is the "link and grade in
             one gesture" path (ADR-025 decision 5); omitting it leaves the
             paper linked but ungraded, which is a legal state — a link
             happens at ingest, a grade after reading.
           - Bump ``updated-at`` if anything actually changed.
        4. Re-render INDEX.json (in-memory splice on the modified copy).
        5. staged_write(metadata + INDEX.json).
        6. Create / refresh ``<project_dir>/litman_reflib/<paper-id>``
           symlink + per-code-clone symlinks under ``<project_dir>/litman_code/``.
        7. Regenerate ``<project_dir>/litman_reflib/REFERENCES.md``.

    Returns:
        A summary dict for the CLI to render.

    Raises:
        LinkError: project unregistered, project_dir missing, or ``priority``
            outside A/B/C.
        PaperNotFoundError: paper id has no folder in the vault.
    """
    # Local import: core.checks reaches back here through core.trash
    # (checks -> trash -> project_link for CODE_SUBDIR), so a module-level
    # import is a load-time cycle. Mirrors the lazy reconcile import below.
    from litman.core.checks import (
        PROJECT_PRIORITY_PREFIX,
        PROJECT_PRIORITY_VALUES,
    )

    # Validated here, not with a click.Choice on the CLI flag: Click would
    # raise UsageError (exit 2, its own formatting) for a metadata value,
    # while every other bad metadata value in this codebase surfaces as a
    # LitmanError (exit 1, Rich panel). One shape for one kind of mistake.
    if priority is not None and priority not in PROJECT_PRIORITY_VALUES:
        raise LinkError(
            f"Invalid priority {priority!r}. Allowed values: "
            f"{', '.join(sorted(PROJECT_PRIORITY_VALUES))}."
        )
    project_dir = _resolve_project_dir(project, registry)
    paper_meta_path = vault / "papers" / paper_id / "metadata.yaml"
    if not paper_meta_path.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} at {paper_meta_path}. "
            "Run `lit list` to see available ids."
        )

    metadata = read_metadata_or_raise(paper_meta_path)
    # Before-state half of the views delta, taken ahead of the mutations
    # below (task-write-perf).
    old_view_fields = view_fields_snapshot(metadata)
    projects_list = list(metadata.get("projects") or [])
    added_to_projects = project not in projects_list
    if added_to_projects:
        projects_list.append(project)
        projects_list.sort()
        metadata["projects"] = projects_list

    relevance_key = f"relevance-{project}"
    existing_relevance = metadata.get(relevance_key)
    set_relevance = (
        relevance is not None and relevance != existing_relevance
    )
    if set_relevance:
        metadata[relevance_key] = relevance

    priority_key = f"{PROJECT_PRIORITY_PREFIX}{project}"
    existing_priority = metadata.get(priority_key)
    set_priority = priority is not None and priority != existing_priority
    if set_priority:
        metadata[priority_key] = priority

    code_clones = list(metadata.get("code-clones") or [])

    # Idempotent on the metadata side: if nothing changed in projects,
    # relevance or priority, skip the staged write but still refresh symlinks
    # + REFERENCES.md (cheap, defensive — handles partial state).
    metadata_changed = added_to_projects or set_relevance or set_priority

    if metadata_changed:
        metadata["updated-at"] = now_iso()
        rel_meta = f"papers/{paper_id}/metadata.yaml"
        # Splice the modified metadata into the paper list to render
        # INDEX.json without depending on disk state — verified INDEX
        # projections when fresh, one scan otherwise (task-write-perf).
        all_papers = papers_for_index(
            vault, drop_ids={paper_id}, add_metas=(metadata,)
        )
        index_json = render_index(all_papers, now_iso())
        with staged_write(vault, op_id=f"link-{paper_id}-{project}") as stage:
            stage.write_text(rel_meta, _dump_yaml_to_string(metadata))
            stage.write_text("INDEX.json", index_json)
        # M30 W3: rebuild INDEX + views/ together through the shared funnel so a
        # link can never leave views/by-project/ stale (the membership change
        # must propagate to the by-project view, not only to INDEX + the
        # project-side litman_reflib). project_refs=False — link does its own
        # symlinks + REFERENCES.md below. Local import avoids any core->commands
        # import-cycle at module load (correctors pulls commands._drift).
        # task-write-perf: only this paper's view buckets change.
        from litman.core.correctors import reconcile_derived

        reconcile_derived(
            vault,
            papers=all_papers,
            project_refs=False,
            views_delta=[
                (paper_id, old_view_fields, view_fields_snapshot(metadata))
            ],
        )

    # 6) Symlinks. Created (or refreshed) regardless of metadata change so
    #    that a partial earlier state (e.g. yaml updated by hand without
    #    symlinks) self-heals on the next `lit link`.
    paper_link_path, code_link_paths = _project_link_paths(
        project_dir, paper_id, code_clones
    )
    paper_target = (vault / "papers" / paper_id).resolve()
    # A folder MOVED out of the user's project dir has to be reported by
    # whatever command moved it: unlike a deleted verbatim copy, whose original
    # is in the vault, this is the only copy of what was in it.
    hub_moved_aside: list[str] = []
    settled = settle_hub_entry(
        paper_link_path,
        paper_target,
        vault=vault,
        project=project,
        hub=LITERATURE_SUBDIR,
    )
    if settled.moved_to is not None:
        hub_moved_aside.append(str(settled.moved_to))
    if not settled.position_occupied:
        make_portable_link(paper_link_path, paper_target)
    code_links_created: list[str] = []
    code_links_missing_repo: list[str] = []
    code_links_unsupported: list[str] = []
    for repo_name, link_path in zip(code_clones, code_link_paths, strict=True):
        repo_target = (vault / "codes" / repo_name / "repo").resolve()
        if not repo_target.exists():
            # Repo bound on paper side but not present locally — re-clone via
            # `lit code restore-all`, then `lit link --rebuild-all`.
            code_links_missing_repo.append(repo_name)
            continue
        settled = settle_hub_entry(
            link_path,
            repo_target,
            vault=vault,
            project=project,
            hub=CODE_SUBDIR,
        )
        if settled.moved_to is not None:
            hub_moved_aside.append(str(settled.moved_to))
        if settled.verdict == "blocked":
            # Same end state as a refused link: the drive cannot hold one.
            code_links_unsupported.append(repo_name)
            continue
        if settled.verdict == "failed":
            # NOT the unsupported bucket: that renders as "this drive cannot
            # hold folder links", which would be a false diagnosis. The folder
            # in the way has already been named on stderr.
            continue
        if make_portable_link(link_path, repo_target):
            code_links_created.append(repo_name)
        else:
            # review F31: the repo IS present; the filesystem refused the link
            # (FAT32/exFAT, network shares). This is NOT a missing repo —
            # directing the user to `restore-all` would be a dead end. Track it
            # separately so the CLI gives accurate guidance.
            code_links_unsupported.append(repo_name)

    # 7) REFERENCES.md
    refs_path = write_references_md(vault, project, project_dir)

    return {
        "paper_id": paper_id,
        "project": project,
        "project_dir": project_dir,
        "added_to_projects": added_to_projects,
        "set_relevance": set_relevance,
        "set_priority": set_priority,
        "metadata_changed": metadata_changed,
        "paper_link": paper_link_path,
        "code_links": code_links_created,
        "code_links_skipped_missing_repo": code_links_missing_repo,
        "code_links_skipped_links_unsupported": code_links_unsupported,
        "hub_moved_aside": hub_moved_aside,
        "references_md": refs_path,
    }


def unlink_paper_from_project(
    vault: Path,
    paper_id: str,
    project: str,
    registry: dict[str, str],
    *,
    purge_relevance: bool = True,
) -> dict[str, Any]:
    """Reverse ``link_paper_to_project``.

    Steps:
        1. Resolve project_dir; refuse on missing.
        2. Load metadata; refuse if paper missing.
        3. Remove project from ``projects`` (no-op if absent).
        4. If ``purge_relevance`` (default), also drop the
           ``relevance-<project>`` field. The previous value is returned
           in the summary so the user sees what was removed.
        4b. ALWAYS drop ``priority-<project>``. Unlike relevance there is no
           opt-out: relevance is authored prose worth offering to keep, a
           grade is one letter that means nothing without the link it grades
           (ADR-025 decision 15). Its previous value is returned too.
        5. staged_write metadata + INDEX.json.
        6. Remove paper symlink under the project.
        7. For each repo in this paper's ``code-clones``, remove the
           project's code symlink ONLY if no other linked paper in the
           project still references that repo.
        8. Regenerate REFERENCES.md.
    """
    project_dir = _resolve_project_dir(project, registry)
    paper_meta_path = vault / "papers" / paper_id / "metadata.yaml"
    if not paper_meta_path.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} at {paper_meta_path}. "
            "Run `lit list` to see available ids."
        )

    metadata = read_metadata_or_raise(paper_meta_path)
    # Before-state half of the views delta, taken ahead of the mutations
    # below (task-write-perf).
    old_view_fields = view_fields_snapshot(metadata)
    projects_list = list(metadata.get("projects") or [])
    was_in_projects = project in projects_list
    if was_in_projects:
        projects_list.remove(project)
        metadata["projects"] = projects_list

    relevance_key = f"relevance-{project}"
    removed_relevance = (
        purge_relevance and relevance_key in metadata
    )
    removed_relevance_value: Any = None
    if removed_relevance:
        removed_relevance_value = metadata.pop(relevance_key)

    # No purge_priority flag by design — see step 4b. Local import for the
    # same checks -> trash -> project_link cycle as in link_paper_to_project.
    from litman.core.checks import PROJECT_PRIORITY_PREFIX

    priority_key = f"{PROJECT_PRIORITY_PREFIX}{project}"
    removed_priority = priority_key in metadata
    removed_priority_value: Any = None
    if removed_priority:
        removed_priority_value = metadata.pop(priority_key)

    code_clones = list(metadata.get("code-clones") or [])
    metadata_changed = (
        was_in_projects or removed_relevance or removed_priority
    )

    if metadata_changed:
        metadata["updated-at"] = now_iso()
        rel_meta = f"papers/{paper_id}/metadata.yaml"
        # Verified INDEX projections when fresh, one scan otherwise
        # (task-write-perf).
        all_papers = papers_for_index(
            vault, drop_ids={paper_id}, add_metas=(metadata,)
        )
        index_json = render_index(all_papers, now_iso())
        with staged_write(vault, op_id=f"unlink-{paper_id}-{project}") as stage:
            stage.write_text(rel_meta, _dump_yaml_to_string(metadata))
            stage.write_text("INDEX.json", index_json)
        # M30 W3: rebuild INDEX + views/ together through the shared funnel so
        # an unlink drops the stale views/by-project/<name>/<id> symlink, not
        # only the INDEX entry + project-side litman_reflib. project_refs=False
        # — unlink does its own symlink teardown + REFERENCES.md below. Local
        # import avoids a core->commands import-cycle at module load.
        # task-write-perf: only this paper's view buckets change.
        from litman.core.correctors import reconcile_derived

        reconcile_derived(
            vault,
            papers=all_papers,
            project_refs=False,
            views_delta=[
                (paper_id, old_view_fields, view_fields_snapshot(metadata))
            ],
        )

    # 6) Paper symlink
    paper_link_path = project_dir / LITERATURE_SUBDIR / paper_id
    paper_link_removed = remove_link_if_present(paper_link_path)

    # 7) Code symlinks — keep when another linked paper still uses the repo.
    # Member-scoped load: the predicate below filters to this project's
    # members and reads code-clones (not projected), so O(project) full
    # metas replace the historical full scan (task-write-perf). The staged
    # INDEX above already reflects the membership change.
    fresh_papers = load_project_member_metas(
        vault, [project], exclude_ids={paper_id}
    )
    code_links_removed = []
    code_links_kept = []
    for repo_name in code_clones:
        link_path = project_dir / CODE_SUBDIR / repo_name
        if not is_portable_link(link_path):
            continue
        # Check fresh papers (excludes the just-unlinked paper). If another
        # paper tagged with this project still binds the repo, KEEP the
        # symlink. The exclude is harmless since fresh_papers already
        # reflects the metadata change above.
        still_in_use = _papers_using_repo_in_project(
            fresh_papers, project, repo_name, exclude_paper_id=paper_id
        )
        if still_in_use:
            code_links_kept.append((repo_name, still_in_use))
        else:
            remove_link_if_present(link_path)
            code_links_removed.append(repo_name)

    # 8) REFERENCES.md
    refs_path = write_references_md(vault, project, project_dir)

    return {
        "paper_id": paper_id,
        "project": project,
        "project_dir": project_dir,
        "was_in_projects": was_in_projects,
        "removed_relevance": removed_relevance,
        "removed_relevance_value": removed_relevance_value,
        "removed_priority": removed_priority,
        "removed_priority_value": removed_priority_value,
        "metadata_changed": metadata_changed,
        "paper_link_removed": paper_link_removed,
        "code_links_removed": code_links_removed,
        "code_links_kept": code_links_kept,
        "references_md": refs_path,
    }


def add_project(vault: Path, name: str, path: Path) -> dict[str, Any]:
    """Register a new project (atomic dual-write TAXONOMY.md + lit-config.yaml).

    The single backend for both ``lit project add`` and the webUI's
    ``POST /api/projects`` (invariant #16: one validate + write path). A project
    is a controlled ``projects`` value bound to an on-disk working directory, so
    both truth sources — TAXONOMY.md's ``## projects`` section and
    lit-config.yaml's ``projects:`` map — are updated in a single staged_write
    so a crash never leaves the name in one but not the other (invariant #2).

    ``path`` must be absolute, already exist, and be a directory (A7 / typo
    defense — no placeholder registration; litman never creates the folder).
    The path is validated as absolute and then ``resolve()``-normalized here,
    so callers may pass a raw (un-resolved) absolute path; the CLI's
    ``click.Path(resolve_path=True)`` pre-resolution is idempotent.

    Returns:
        ``{"name": ..., "path": str(path)}`` summary for the caller to render.

    Raises:
        TaxonomyError: empty name, path not absolute / missing / not a
            directory, or the name is already registered.
    """
    name = name.strip()
    if not name:
        raise TaxonomyError("Project name cannot be empty.")
    if not path.is_absolute():
        raise TaxonomyError(
            f"Path {str(path)!r} is not absolute. "
            "Give the full path to the folder, starting from '/'."
        )
    path = path.resolve()
    if not path.exists():
        raise TaxonomyError(
            f"Path {str(path)!r} does not exist. "
            "Point at an existing folder — litman does not create it."
        )
    if not path.is_dir():
        raise TaxonomyError(f"Path {str(path)!r} is not a directory.")

    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    parsed = parse_taxonomy(text)
    config = load_config(vault)

    registered_names = set(parsed[_PROJECTS_DICT]) | set(config.projects)
    if name in registered_names:
        existing_path = config.projects.get(name)
        raise TaxonomyError(
            f"Project {name!r} is already registered"
            + (f" → {existing_path}" if existing_path else "")
            + ". Use `lit project set-path "
            f"{name} <new-path>` to change its path, or "
            f"`lit project rename {name} <new-name>` to rename it."
        )

    new_taxonomy_text = update_user_dict_section(
        text, _PROJECTS_DICT, sorted(parsed[_PROJECTS_DICT] + [name])
    )
    new_projects = dict(config.projects)
    new_projects[name] = str(path)
    as_dict = config_to_yaml_dict(load_config(vault))
    as_dict["projects"] = new_projects
    new_config_text = _dump_yaml_to_string(as_dict)

    with staged_write(vault, op_id=f"project-add-{name}") as stage:
        stage.write_text("TAXONOMY.md", new_taxonomy_text)
        stage.write_text("lit-config.yaml", new_config_text)

    return {"name": name, "path": str(path)}


def remove_project(vault: Path, name: str) -> tuple[int, list[str]]:
    """Delete a project: cascade-untag papers + drop from both truth sources.

    The single backend for the WRITE half of both ``lit project rm`` and the
    webUI's ``DELETE /api/projects/{name}`` (invariant #16: one validate + write
    path). A project is a controlled ``projects`` value with a lit-config.yaml
    path binding, so removal updates BOTH truth sources (TAXONOMY.md's
    ``## projects`` section and the config map) plus every referencing paper's
    metadata.yaml — including the paired ``relevance-<name>`` /
    ``priority-<name>`` annotations (``drop_project_keys=True``) so no orphan
    is stranded — in one atomic
    staged_write. INDEX + views are then rebuilt through the shared
    ``reconcile_derived`` funnel.

    Post-commit teardown unlinks the project's ``litman_reflib/`` paper symlinks,
    its ``litman_code/`` repo symlinks, and deletes REFERENCES.md. It NEVER
    removes the project directory itself (preserving the CLI's behavior exactly):
    only the litman-managed children inside it are torn down.

    Confirm-free by design: the cascade-with-confirm gate (``_confirm_destructive``)
    and console output stay in the ``lit project rm`` command; the GUI's confirm
    dialog is the confirmation on the server path.

    Returns:
        ``(n_changed, referencing_ids)`` — count of papers whose metadata was
        rewritten and the sorted ids that referenced ``name`` before removal.

    Raises:
        TaxonomyError: ``name`` is not registered in either truth source.
    """
    # Local import avoids a core import-cycle at module load: reconcile_derived
    # → core.checks imports core.taxonomy, and core.ripple imports core.taxonomy
    # too. Mirrors the lazy reconcile import in link/unlink above.
    from litman.core.correctors import reconcile_derived
    from litman.core.ripple import _ripple_removals

    name = name.strip()
    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    parsed = parse_taxonomy(text)
    config = load_config(vault)

    known = set(parsed[_PROJECTS_DICT]) | set(config.projects)
    if name not in known:
        raise TaxonomyError(
            f"Project {name!r} is not registered. "
            "Run `lit project list` to inspect."
        )

    papers = list_papers(vault)
    referencing = find_referencing_papers(papers, _PROJECTS_DICT, name)

    project_dir_str = config.projects.get(name)
    project_dir = (
        Path(project_dir_str).expanduser() if project_dir_str else None
    )

    # Build the post-removal truth sources.
    new_taxonomy_values = [
        v for v in parsed[_PROJECTS_DICT] if v != name
    ]
    new_taxonomy_text = update_user_dict_section(
        text, _PROJECTS_DICT, new_taxonomy_values
    )
    new_projects = {
        k: v for k, v in config.projects.items() if k != name
    }
    as_dict = config_to_yaml_dict(load_config(vault))
    as_dict["projects"] = new_projects
    new_config_text = _dump_yaml_to_string(as_dict)

    n_changed, staged_meta_paths, all_papers = _ripple_removals(
        vault, _PROJECTS_DICT, name, drop_project_keys=True
    )
    fresh_index = render_index(all_papers, now_iso())

    with staged_write(vault, op_id=f"project-rm-{name}") as stage:
        stage.write_text("TAXONOMY.md", new_taxonomy_text)
        stage.write_text("lit-config.yaml", new_config_text)
        for relpath, content in staged_meta_paths:
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", fresh_index)

    # Post-commit derived rebuild via the shared funnel (M30 Phase 4):
    # INDEX + views/by-project/ (the removed project drops out) recomputed
    # together. project_refs=False: the removed project's own symlinks +
    # REFERENCES.md are torn down explicitly below, and no other project's
    # membership changed — behavior identical to the pre-funnel command.
    reconcile_derived(vault, papers=list_papers(vault), project_refs=False)

    # Post-commit teardown of the project's on-disk artifacts. Mirrors the
    # unlink pattern: filesystem-mutating, cheap to redo, recoverable.
    if project_dir is not None and project_dir.is_dir():
        literature_dir = project_dir / LITERATURE_SUBDIR
        if literature_dir.is_dir():
            for child in literature_dir.iterdir():
                remove_link_if_present(child)
            refs = literature_dir / REFERENCES_FILENAME
            if refs.exists():
                refs.unlink()
        # Symmetric teardown of litman_code/ (parallel to rm.py's
        # _teardown_project_links). The project is gone, so every
        # litman_code/<repo> link is an orphan — no shared-lib retention
        # judgment needed. Without this, those links become permanent
        # orphans that no later rebuild_all_project_links revisits (the project
        # is already out of the registry), violating invariant #14.
        code_dir = project_dir / CODE_SUBDIR
        if code_dir.is_dir():
            for child in code_dir.iterdir():
                remove_link_if_present(child)
        # The hubs themselves were litman's litter too: rmdir only when
        # empty, so anything the user parked inside survives untouched.
        for hub in (literature_dir, code_dir):
            try:
                hub.rmdir()
            except OSError:
                pass

    return n_changed, referencing


def rename_project(
    vault: Path, old: str, new: str
) -> tuple[int, list[str], list[str]]:
    """Rename a project across both truth sources + every referencing paper.

    The single backend for both ``lit project rename`` and the webUI's
    ``PUT /api/projects/{name}`` (invariant #16: one validate + write path). A
    project is a controlled ``projects`` value bound to an on-disk path, so the
    rename updates BOTH truth sources (TAXONOMY.md's ``## projects`` section and
    lit-config.yaml's ``projects:`` map key — carrying the path over unchanged
    under the new key), every referencing paper's ``projects`` field, and the
    paired ``relevance-<name>`` / ``priority-<name>`` annotations
    (``rename_project_keys=True``), all in one
    atomic staged_write. INDEX + views/by-project/ + every project's symlinks +
    REFERENCES.md are then rebuilt through the shared ``reconcile_derived`` funnel
    (``project_refs=True`` — a rename touches the project side). Semantics-
    preserving (no data loss), so the CLI runs it confirm-free.

    Returns:
        ``(n_changed, referencing_ids, hub_moved_aside)`` — count of papers
        whose metadata was rewritten, the sorted ids that referenced ``old``
        before the rename, and any project-hub folders the rebuild had to
        preserve under ``.trash/`` (the caller reports those; they are the only
        copy of what was in them).

    Raises:
        TaxonomyError: ``new`` is empty; ``old`` == ``new``; ``old`` is not
            registered; or ``new`` is already registered.
    """
    # Local import avoids a core import-cycle at module load (mirrors
    # remove_project): reconcile_derived → core.checks imports core.taxonomy, and
    # core.ripple imports core.taxonomy too.
    from litman.core.correctors import moved_aside_from, reconcile_derived
    from litman.core.ripple import _ripple_replacements

    old = old.strip()
    new = new.strip()
    if not new:
        raise TaxonomyError("`new` project name cannot be empty.")
    if old == new:
        raise TaxonomyError("`old` and `new` are identical — nothing to do.")

    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    parsed = parse_taxonomy(text)
    config = load_config(vault)

    known = set(parsed[_PROJECTS_DICT]) | set(config.projects)
    if old not in known:
        raise TaxonomyError(
            f"Project {old!r} is not registered. "
            "Run `lit project list` to inspect."
        )
    if new in known:
        raise TaxonomyError(
            f"Project {new!r} is already registered. "
            "Pick a different name or `lit project rm` the conflicting one."
        )

    papers = list_papers(vault)
    referencing = find_referencing_papers(papers, _PROJECTS_DICT, old)

    new_taxonomy_values = [
        new if v == old else v for v in parsed[_PROJECTS_DICT]
    ]
    new_taxonomy_text = update_user_dict_section(
        text, _PROJECTS_DICT, new_taxonomy_values
    )

    new_projects = {
        (new if k == old else k): v for k, v in config.projects.items()
    }
    as_dict = config_to_yaml_dict(load_config(vault))
    as_dict["projects"] = new_projects
    new_config_text = _dump_yaml_to_string(as_dict)

    n_changed, staged_meta_paths, all_papers = _ripple_replacements(
        vault, _PROJECTS_DICT, {old: new}, rename_project_keys=True
    )
    fresh_index = render_index(all_papers, now_iso())

    with staged_write(vault, op_id=f"project-rename-{old}") as stage:
        stage.write_text("TAXONOMY.md", new_taxonomy_text)
        stage.write_text("lit-config.yaml", new_config_text)
        for relpath, content in staged_meta_paths:
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", fresh_index)

    # Post-commit derived rebuild via the shared funnel (M30 Phase 4): INDEX +
    # views/by-project/ (new name in, old name out) + every project's symlinks +
    # REFERENCES.md, all recomputed from the committed TRUTH. project_refs=True
    # because a rename touches the project side; the funnel reloads config (= the
    # just-committed new_projects) for the project side.
    derived = reconcile_derived(vault, project_refs=True)

    return n_changed, referencing, moved_aside_from(derived)


def set_project_path(vault: Path, name: str, new_path: Path) -> dict[str, Any]:
    """Change a project's on-disk path (config-only — papers store names).

    The single backend for both ``lit project set-path`` and the webUI's
    ``PUT /api/projects/{name}/path`` (invariant #16: one validate + write path).
    Papers reference a project by NAME, so only lit-config.yaml's ``projects:``
    map changes — through staged_write (single file, but keeps op-id / rollback
    consistency with the other project commands). It does NOT physically move the
    directory and does NOT rebuild symlinks (those are recreated on demand via
    ``lit link --rebuild-all`` / the GUI's rebuild-views action), matching the CLI
    exactly so both paths behave identically.

    ``new_path`` must be absolute (the server cwd is opaque, so a relative path is
    rejected rather than resolved — mirrors ``add_project``), already exist, and
    be a directory; it is then ``resolve()``-normalized.

    Returns:
        ``{"name": ..., "path": str(new_path), "changed": bool}`` — ``changed``
        is ``False`` when the project already points at that path (no-op).

    Raises:
        TaxonomyError: ``name`` not registered in lit-config.yaml; ``new_path``
            not absolute / missing / not a directory.
    """
    name = name.strip()
    config = load_config(vault)

    if name not in config.projects:
        raise TaxonomyError(
            f"Project {name!r} is not registered in lit-config.yaml. "
            "Run `lit project list` to inspect, or "
            f"`lit project add {name} --path <abs-path>` to register it."
        )
    if not new_path.is_absolute():
        raise TaxonomyError(
            f"Path {str(new_path)!r} is not absolute. "
            "Give the full path to the folder, starting from '/'."
        )
    new_path = new_path.resolve()
    if not new_path.exists():
        raise TaxonomyError(
            f"Path {str(new_path)!r} does not exist. "
            f"Create it first (e.g. `mkdir -p {new_path}`)."
        )
    if not new_path.is_dir():
        raise TaxonomyError(f"Path {str(new_path)!r} is not a directory.")

    new_path_str = str(new_path)
    if config.projects[name] == new_path_str:
        return {"name": name, "path": new_path_str, "changed": False}

    new_projects = dict(config.projects)
    new_projects[name] = new_path_str
    as_dict = config_to_yaml_dict(load_config(vault))
    as_dict["projects"] = new_projects
    new_config_text = _dump_yaml_to_string(as_dict)

    with staged_write(vault, op_id=f"project-set-path-{name}") as stage:
        stage.write_text("lit-config.yaml", new_config_text)

    return {"name": name, "path": new_path_str, "changed": True}


def rebuild_all_project_links(
    vault: Path,
    registry: dict[str, str],
    *,
    papers: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Recreate every project's symlinks + REFERENCES.md from scratch.

    Cross-machine recovery analog of ``lit code restore-all``: scans
    every paper, for each project in its ``projects`` field that is
    registered AND whose project_dir exists, re-creates the literature
    + code symlinks. Per-project failures (missing project_dir) are
    skipped, not aborted.

    Does NOT touch metadata — assumes the vault's metadata.yaml files
    are the source of truth (which they are). Only the on-disk symlinks
    + REFERENCES.md get refreshed.

    ``papers`` reuses an already-loaded FULL metadata list (``list_papers``
    output — the downstream REFERENCES.md render reads
    ``relevance-<project>``, which INDEX projections lack); ``None`` scans.
    """
    if papers is None:
        papers = list_papers(vault)
    out: dict[str, dict[str, Any]] = {}

    for project, project_dir_str in sorted(registry.items()):
        project_dir = Path(project_dir_str).expanduser()
        tagged_papers = [
            p for p in papers if project in (p.get("projects") or [])
        ]
        n_tagged = len(tagged_papers)
        if not project_dir.is_dir():
            out[project] = {
                "status": "skipped",
                "n_tagged": n_tagged,
                "n_paper_links": 0,
                "n_code_links": 0,
                "n_replaced_copies": 0,
                "n_moved_aside": 0,
                "aside_paths": [],
                "detail": f"project dir not found: {project_dir}",
            }
            continue

        # Wipe the symlink hubs so stale entries from prior runs disappear.
        # Whatever is NOT a link gets settled here (an expanded copy is
        # deleted or preserved in .trash/), so the create loop below meets
        # either an empty position or one it must leave alone.
        blocked: set[Path] = set()
        n_replaced_copies = 0
        aside_paths: list[str] = []
        for sub in (LITERATURE_SUBDIR, CODE_SUBDIR):
            sub_dir = project_dir / sub
            if not sub_dir.exists():
                sub_dir.mkdir(exist_ok=True)
                continue
            # sorted() drains the scandir generator before the settling
            # starts removing entries out from under it.
            for child in sorted(sub_dir.iterdir()):
                if remove_link_if_present(child):
                    continue
                settled = settle_hub_entry(
                    child,
                    _hub_target(vault, sub, child.name),
                    vault=vault,
                    project=project,
                    hub=sub,
                )
                if settled.verdict == "replaced-copy":
                    n_replaced_copies += 1
                elif settled.verdict == "moved-aside":
                    aside_paths.append(str(settled.moved_to))
                elif settled.position_occupied:
                    # Still a real folder here. Skipping the upsert keeps the
                    # create loop from adding a second warning about a
                    # position the user has already been told about.
                    blocked.add(child)
        # Preserve REFERENCES.md across the wipe — it lives in
        # litman_reflib/ alongside the symlinks but is content, not a link
        # (settle_hub_entry leaves every real file where it is).

        n_paper_links = 0
        n_code_links = 0
        for p in tagged_papers:
            pid = p.get("id")
            if not pid:
                continue
            paper_dir = (vault / "papers" / pid).resolve()
            if not paper_dir.is_dir():
                continue
            paper_link = project_dir / LITERATURE_SUBDIR / pid
            if paper_link not in blocked and make_portable_link(
                paper_link, paper_dir
            ):
                n_paper_links += 1
            for repo_name in p.get("code-clones") or []:
                repo_target = (vault / "codes" / repo_name / "repo").resolve()
                if not repo_target.exists():
                    continue
                code_link = project_dir / CODE_SUBDIR / repo_name
                if code_link not in blocked and make_portable_link(
                    code_link, repo_target
                ):
                    n_code_links += 1

        write_references_md(vault, project, project_dir)

        out[project] = {
            "status": "rebuilt",
            "n_tagged": n_tagged,
            "n_paper_links": n_paper_links,
            "n_code_links": n_code_links,
            "n_replaced_copies": n_replaced_copies,
            "n_moved_aside": len(aside_paths),
            "aside_paths": aside_paths,
            "detail": "",
        }

    return out
