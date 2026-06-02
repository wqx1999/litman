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
symlink creation and REFERENCES.md regeneration are post-staging steps
(filesystem-mutating but cheap to redo, recoverable via
``lit link --rebuild-all``).

Cross-platform: symlink creation routes through ``core.portable_link``,
which gracefully degrades on filesystems that refuse symlinks (Windows
without Developer Mode, FAT32, etc.). The metadata side of every
operation succeeds regardless; only the convenience symlinks may be
skipped (ADR-005).
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.dates import now_iso
from litman.core.document import list_papers, read_metadata_or_raise
from litman.core.portable_link import (
    make_relative_symlink,
    remove_link_if_present,
)
from litman.core.project_refs import (
    LITERATURE_SUBDIR,
    write_references_md,
)
from litman.core.views import render_index
from litman.exceptions import LitmanError, PaperNotFoundError

CODE_SUBDIR = "litman_code"

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.default_flow_style = False


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


def link_paper_to_project(
    vault: Path,
    paper_id: str,
    project: str,
    registry: dict[str, str],
    *,
    relevance: str | None = None,
) -> dict[str, Any]:
    """Link a paper to a project (atomic metadata + symlinks + REFERENCES.md).

    Steps:
        1. Resolve project_dir from the registry; refuse on missing.
        2. Load paper's metadata; refuse if paper missing.
        3. Update metadata:
           - Append project to ``projects`` if absent (deduped).
           - Set ``relevance-<project>`` if ``relevance`` was provided
             AND the field isn't already populated by the user.
           - Bump ``updated-at`` if anything actually changed.
        4. Re-render INDEX.json (in-memory splice on the modified copy).
        5. staged_write(metadata + INDEX.json).
        6. Create / refresh ``<project_dir>/litman_reflib/<paper-id>``
           symlink + per-code-clone symlinks under ``<project_dir>/litman_code/``.
        7. Regenerate ``<project_dir>/litman_reflib/REFERENCES.md``.

    Returns:
        A summary dict for the CLI to render.

    Raises:
        LinkError: project unregistered or project_dir missing.
        PaperNotFoundError: paper id has no folder in the vault.
    """
    project_dir = _resolve_project_dir(project, registry)
    paper_meta_path = vault / "papers" / paper_id / "metadata.yaml"
    if not paper_meta_path.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} at {paper_meta_path}. "
            "Run `lit list` to see available ids."
        )

    metadata = read_metadata_or_raise(paper_meta_path)
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

    code_clones = list(metadata.get("code-clones") or [])

    # Idempotent on the metadata side: if nothing changed in projects or
    # relevance, skip the staged write but still refresh symlinks +
    # REFERENCES.md (cheap, defensive — handles partial state).
    metadata_changed = added_to_projects or set_relevance

    if metadata_changed:
        metadata["updated-at"] = now_iso()
        rel_meta = f"papers/{paper_id}/metadata.yaml"
        # Splice the modified metadata into a fresh full paper list to
        # render INDEX.json without depending on disk state.
        all_papers = [
            p for p in list_papers(vault) if p.get("id") != paper_id
        ]
        all_papers.append(dict(metadata))
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
        from litman.core.correctors import reconcile_derived

        reconcile_derived(vault, papers=all_papers, project_refs=False)

    # 6) Symlinks. Created (or refreshed) regardless of metadata change so
    #    that a partial earlier state (e.g. yaml updated by hand without
    #    symlinks) self-heals on the next `lit link`.
    paper_link_path, code_link_paths = _project_link_paths(
        project_dir, paper_id, code_clones
    )
    make_relative_symlink(
        paper_link_path, (vault / "papers" / paper_id).resolve()
    )
    code_links_created: list[str] = []
    code_links_missing_repo: list[str] = []
    code_links_symlink_unsupported: list[str] = []
    for repo_name, link_path in zip(code_clones, code_link_paths, strict=True):
        repo_target = (vault / "codes" / repo_name / "repo").resolve()
        if not repo_target.exists():
            # Repo bound on paper side but not present locally — re-clone via
            # `lit code restore-all`, then `lit link --rebuild-all`.
            code_links_missing_repo.append(repo_name)
            continue
        if make_relative_symlink(link_path, repo_target):
            code_links_created.append(repo_name)
        else:
            # review F31: the repo IS present; the platform refused the symlink
            # (Windows w/o Developer Mode, FAT32/exFAT, ...). This is NOT a
            # missing repo — directing the user to `restore-all` would be a
            # dead end. Track it separately so the CLI gives accurate guidance.
            code_links_symlink_unsupported.append(repo_name)

    # 7) REFERENCES.md
    refs_path = write_references_md(vault, project, project_dir)

    return {
        "paper_id": paper_id,
        "project": project,
        "project_dir": project_dir,
        "added_to_projects": added_to_projects,
        "set_relevance": set_relevance,
        "metadata_changed": metadata_changed,
        "paper_link": paper_link_path,
        "code_links": code_links_created,
        "code_links_skipped_missing_repo": code_links_missing_repo,
        "code_links_skipped_symlink_unsupported": code_links_symlink_unsupported,
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

    code_clones = list(metadata.get("code-clones") or [])
    metadata_changed = was_in_projects or removed_relevance

    if metadata_changed:
        metadata["updated-at"] = now_iso()
        rel_meta = f"papers/{paper_id}/metadata.yaml"
        all_papers = [
            p for p in list_papers(vault) if p.get("id") != paper_id
        ]
        all_papers.append(dict(metadata))
        index_json = render_index(all_papers, now_iso())
        with staged_write(vault, op_id=f"unlink-{paper_id}-{project}") as stage:
            stage.write_text(rel_meta, _dump_yaml_to_string(metadata))
            stage.write_text("INDEX.json", index_json)
        # M30 W3: rebuild INDEX + views/ together through the shared funnel so
        # an unlink drops the stale views/by-project/<name>/<id> symlink, not
        # only the INDEX entry + project-side litman_reflib. project_refs=False
        # — unlink does its own symlink teardown + REFERENCES.md below. Local
        # import avoids a core->commands import-cycle at module load.
        from litman.core.correctors import reconcile_derived

        reconcile_derived(vault, papers=all_papers, project_refs=False)

    # 6) Paper symlink
    paper_link_path = project_dir / LITERATURE_SUBDIR / paper_id
    paper_link_removed = remove_link_if_present(paper_link_path)

    # 7) Code symlinks — keep when another linked paper still uses the repo.
    fresh_papers = list_papers(vault)
    code_links_removed = []
    code_links_kept = []
    for repo_name in code_clones:
        link_path = project_dir / CODE_SUBDIR / repo_name
        if not link_path.is_symlink():
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
        "metadata_changed": metadata_changed,
        "paper_link_removed": paper_link_removed,
        "code_links_removed": code_links_removed,
        "code_links_kept": code_links_kept,
        "references_md": refs_path,
    }


def rebuild_all_project_links(
    vault: Path,
    registry: dict[str, str],
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
    """
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
                "detail": f"project dir not found: {project_dir}",
            }
            continue

        # Wipe the symlink hubs so stale entries from prior runs disappear.
        for sub in (LITERATURE_SUBDIR, CODE_SUBDIR):
            sub_dir = project_dir / sub
            if sub_dir.exists():
                for child in sub_dir.iterdir():
                    if child.is_symlink():
                        child.unlink()
            else:
                sub_dir.mkdir(exist_ok=True)
        # Preserve REFERENCES.md across the wipe — it lives in
        # litman_reflib/ alongside the symlinks but is content, not a link.

        n_paper_links = 0
        n_code_links = 0
        for p in tagged_papers:
            pid = p.get("id")
            if not pid:
                continue
            paper_dir = (vault / "papers" / pid).resolve()
            if not paper_dir.is_dir():
                continue
            if make_relative_symlink(
                project_dir / LITERATURE_SUBDIR / pid, paper_dir
            ):
                n_paper_links += 1
            for repo_name in p.get("code-clones") or []:
                repo_target = (vault / "codes" / repo_name / "repo").resolve()
                if not repo_target.exists():
                    continue
                if make_relative_symlink(
                    project_dir / CODE_SUBDIR / repo_name, repo_target
                ):
                    n_code_links += 1

        write_references_md(vault, project, project_dir)

        out[project] = {
            "status": "rebuilt",
            "n_tagged": n_tagged,
            "n_paper_links": n_paper_links,
            "n_code_links": n_code_links,
            "detail": "",
        }

    return out
