"""``lit rm`` — remove a paper from the vault (M23.1 unified delete flow).

By default the paper folder is moved into ``<vault>/.trash/`` (recoverable
via ``lit trash restore <id>``); pass ``--purge`` to permanently delete.
Either way INDEX.json and views/ are refreshed.

Unlike the earlier "refuse unless --cascade" design, ``lit rm`` now always
tears down the *external→A* half of A's relationship network in one atomic
step, then seals A (with its own A→external fields intact) into trash. A
single confirmation gates the destruction:

* A has relations (literature ref / code clone / project link) → the prompt
  reports the TOTAL count + points at ``lit show`` for inspection, default
  ``N``.
* A has no relations → the standard delete confirmation.

``-y`` / ``--yes`` is the full non-interactive force-delete entry (script /
weak-LLM / agent path, invariant #5): ``lit rm A -y`` deletes clean with no
prompt.

Cascade teardown (one ``staged_write`` transaction, invariant #9/#12):

* Literature ref: for each opposite paper named in A's own relation fields,
  drop A from that opposite's *paired* field (RELATION_PAIRS lookup). After
  M23.0 symmetry this uniformly removes every "external→A" edge.
* Code: for each repo in A's ``code-clones``, drop A from
  ``codes/<repo>/repo-meta.yaml::papers``. If that empties the repo's binder
  list (1:1, the common case) the orphan ``codes/<repo>/`` dir is
  hard-deleted and ``{name: upstream}`` is recorded in the trash sidecar for
  M23.2 re-clone. A still-bound repo (1:N) only loses the binding.
* Project: for each project in A's ``projects``, the ``litman_reflib/A``
  symlink is removed, the parallel ``litman_code/<repo>`` symlink is removed only
  when no other paper in that project still binds the repo, and
  REFERENCES.md is re-rendered.

A's OWN fields (forward + reverse relation fields, code-clones, projects)
are left one byte unchanged — they ride into trash with the folder. This is
the precondition M23.2 restore depends on.

Same-vault ``[[A]]`` wikilinks in notes/discussion ARE rewritten (M24): the
delete stages a ``[[A]] (deleted)`` annotation on every referencing file so
an agent reading those notes sees the paper is gone (ADR-013). This is the
only prose edit ``lit rm`` performs; the relationship count is still computed
from structured metadata fields only. Cross-vault ``[[v:A]]`` is out of scope
(per-vault, like health-check).

Atomicity layers:

* All metadata.yaml + repo-meta.yaml + INDEX.json writes go through one
  ``staged_write`` so they either all land or all roll back.
* Filesystem-only steps (paper-folder move/rmtree, orphan-repo rmtree,
  project symlink removal, REFERENCES.md re-render, views rebuild) run after
  the staged commit. A failure there leaves INDEX.json claiming the paper is
  gone while the dir is still on disk — ``lit health-check`` flags this.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.code import CODES_DIRNAME, REPO_META_FILENAME
from litman.core.config import load_config
from litman.core.correctors import reconcile_derived
from litman.core.dates import now_iso
from litman.core.document import list_papers, load_yaml_or_raise
from litman.core.id import is_valid_id
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.notes import (
    annotate_deleted_wikilinks,
    enumerate_markdown_files,
)
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input
from litman.core.portable_link import remove_link_if_present
from litman.core.project_link import (
    CODE_SUBDIR,
    _papers_using_repo_in_project,
)
from litman.core.project_refs import LITERATURE_SUBDIR, write_references_md
from litman.core.relations import ALL_REF_FIELDS, RELATION_PAIRS
from litman.core.trash import TRASH_MAX_ENTRIES, enforce_cap, move_to_trash
from litman.core.views import render_index
from litman.exceptions import PaperNotFoundError, RmError

console = Console()

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.default_flow_style = False


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Relationship-network discovery (structured fields only)
# ---------------------------------------------------------------------------


def _opposite_ref_targets(target_meta: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``[(opposite_id, paired_field), ...]`` to clear external→A edges.

    Reads A's *own* relation fields. For each opposite paper A names, the
    edge to remove from that opposite is its RELATION_PAIRS-paired field
    (e.g. A.extends:[B] ⇒ remove A from B.extended-by). After M23.0 symmetry
    this enumerates every "external→A" edge, since every inbound edge is
    mirrored in one of A's own fields.
    """
    out: list[tuple[str, str]] = []
    for field in ALL_REF_FIELDS:
        for opposite_id in target_meta.get(field) or []:
            paired = RELATION_PAIRS[field]
            out.append((str(opposite_id), paired))
    return out


def _build_cascade_ref_updates(
    vault: Path,
    target_meta: dict[str, Any],
    target_id: str,
    now: str,
    safe_papers: list[dict[str, Any]],
) -> tuple[dict[str, str], set[str]]:
    """Build staged metadata.yaml writes that drop ``target_id`` from opposites.

    For each opposite paper named in A's relation fields, remove A from the
    opposite's paired field (RELATION_PAIRS). A single opposite may need
    several of its fields touched (e.g. A both ``related`` and ``extends`` B);
    they are coalesced into one rewrite per opposite paper.

    Mutates ``safe_papers`` in place so the caller can re-render INDEX.json
    from the same in-memory list without re-reading disk.

    Returns ``(staged_writes, opposite_id_set)`` where:
        * ``staged_writes`` maps paper-id → new yaml text
        * ``opposite_id_set`` is the unique set of opposite ids touched
    """
    # opposite_id → set of its own fields that must drop target_id.
    by_opposite: dict[str, set[str]] = {}
    for opposite_id, paired_field in _opposite_ref_targets(target_meta):
        if opposite_id == target_id:
            continue  # self-reference: no separate opposite
        by_opposite.setdefault(opposite_id, set()).add(paired_field)

    staged: dict[str, str] = {}
    touched: set[str] = set()
    for opposite_id, fields in by_opposite.items():
        meta_path = vault / "papers" / opposite_id / "metadata.yaml"
        if not meta_path.is_file():
            # Opposite already gone: nothing to clear. The dangling
            # forward edge rides into trash inside A's own fields.
            continue
        rt = load_yaml_or_raise(meta_path, _yaml)
        if rt is None:
            continue
        changed = False
        for field in fields:
            cur = rt.get(field)
            if cur and target_id in cur:
                rt[field] = [v for v in cur if v != target_id]
                changed = True
        if not changed:
            continue
        rt["updated-at"] = now
        staged[opposite_id] = _dump_yaml_to_string(rt)
        touched.add(opposite_id)
        # Keep the in-memory copy in parity with what we just staged to
        # disk. Relation fields and updated-at are NOT in the INDEX
        # projection (see views.INDEX_PAPER_FIELDS), so this does not affect
        # INDEX.json today; it guards any future consumer of safe_papers
        # against reading stale edges for the opposite paper.
        for paper in safe_papers:
            if paper.get("id") == opposite_id:
                for field in fields:
                    cur = paper.get(field) or []
                    if target_id in cur:
                        paper[field] = [v for v in cur if v != target_id]
                paper["updated-at"] = now
                break
    return staged, touched


def _build_cascade_code_updates(
    vault: Path,
    target_meta: dict[str, Any],
    target_id: str,
    now: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build staged repo-meta.yaml writes that unbind A from each repo.

    Mirrors ``bind_paper_to_repo`` in reverse, single-paper-single-repo:
    drops ``target_id`` from each ``codes/<repo>/repo-meta.yaml::papers``.
    Does NOT use ``unbind_repo_from_all_papers`` (that strips a repo from
    *every* paper and skips repo-meta — the wrong direction here).

    Returns ``(staged_writes, orphan_repos)`` where:
        * ``staged_writes`` maps ``codes/<repo>/repo-meta.yaml`` relpath →
          new yaml text (only for repos that stay, i.e. 1:N survivors)
        * ``orphan_repos`` maps ``repo_name → upstream_url`` for repos whose
          binder list became empty (1:1) — caller hard-deletes the dir and
          records the url in the trash sidecar. Orphan repos are NOT staged
          (their dir is removed wholesale post-stage).
    """
    staged: dict[str, str] = {}
    orphan_repos: dict[str, str] = {}
    for repo_name in target_meta.get("code-clones") or []:
        repo_meta_path = (
            vault / CODES_DIRNAME / str(repo_name) / REPO_META_FILENAME
        )
        if not repo_meta_path.is_file():
            # Binding present on the paper side but repo-meta is missing.
            # Nothing to unbind; health-check surfaces the orphan ref.
            continue
        rt = load_yaml_or_raise(repo_meta_path, _yaml)
        if rt is None:
            continue
        papers = rt.get("papers") or []
        remaining = [p for p in papers if p != target_id]
        if not remaining:
            # 1:1 — A was the last (or only) binder. Orphan the dir.
            orphan_repos[str(repo_name)] = rt.get("upstream") or ""
            continue
        rt["papers"] = remaining
        rt["updated-at"] = now
        staged[f"{CODES_DIRNAME}/{repo_name}/{REPO_META_FILENAME}"] = (
            _dump_yaml_to_string(rt)
        )
    return staged, orphan_repos


def _teardown_project_links(
    vault: Path,
    target_meta: dict[str, Any],
    target_id: str,
    registry: dict[str, str],
    surviving_papers: list[dict[str, Any]],
) -> None:
    """Remove A's project symlinks + re-render REFERENCES.md (post-stage).

    Filesystem-only: A's ``projects`` field is sealed into trash unchanged.
    For each project A was tagged with:
        * remove ``<project>/litman_reflib/A`` symlink;
        * remove ``<project>/litman_code/<repo>`` symlink only when no OTHER paper
          in the project still binds the repo (shared-utility-lib case);
        * re-render REFERENCES.md from the surviving paper list.

    Unregistered or missing project dirs are skipped silently — the metadata
    side of the delete already succeeded; symlinks are a convenience layer.
    """
    code_clones = [str(r) for r in (target_meta.get("code-clones") or [])]
    for project in target_meta.get("projects") or []:
        project = str(project)
        project_dir_str = registry.get(project)
        if not project_dir_str:
            continue
        project_dir = Path(project_dir_str).expanduser()
        if not project_dir.is_dir():
            continue

        paper_link = project_dir / LITERATURE_SUBDIR / target_id
        remove_link_if_present(paper_link)

        for repo_name in code_clones:
            link_path = project_dir / CODE_SUBDIR / repo_name
            if not link_path.is_symlink():
                continue
            still_used = _papers_using_repo_in_project(
                surviving_papers, project, repo_name,
                exclude_paper_id=target_id,
            )
            if not still_used:
                remove_link_if_present(link_path)

        # REFERENCES.md reads list_papers(vault); A is already in trash by
        # the time this runs, so it drops out naturally.
        try:
            write_references_md(vault, project, project_dir)
        except FileNotFoundError:
            continue


@click.command("rm")
@click.argument(
    "paper_id", required=False, shell_complete=complete_paper_id
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Reverse-lookup the paper by DOI instead of supplying the id. "
        "Mutually exclusive with the positional paper id."
    ),
)
@click.option(
    "--purge",
    is_flag=True,
    default=False,
    help="Permanently delete instead of moving to .trash/.",
)
@click.option(
    "--yes",
    "-y",
    "skip_confirm",
    is_flag=True,
    default=False,
    help=(
        "Non-interactive force-delete: skip the confirmation and tear down "
        "all external links in one step (script / agent path)."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview only — list the paper plus every external link that would "
    "be cleared / unbound / orphaned, then exit without deleting anything.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def rm_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    purge: bool,
    skip_confirm: bool,
    dry_run: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Remove a paper from the vault.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass --paper-doi <DOI> instead.

    By default moves papers/<id>/ to <vault>/.trash/ (recoverable via
    lit trash restore <id>). Pass --purge to permanently delete. INDEX.json
    and views/by-*/ are refreshed either way.

    All external links to the paper (other papers' relation fields, repo
    bindings, project symlinks) are torn down atomically; the paper's own
    fields ride into trash so a later lit trash restore can rebuild them.
    A y/N confirmation guards the delete (default N); pass -y to force it
    non-interactively. --dry-run previews the full impact set (the paper plus
    every link that would be cleared / unbound / orphaned) without deleting.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)

    if not is_valid_id(paper_id):
        raise RmError(
            f"Invalid paper id {paper_id!r}. Run `lit list` to see valid ids."
        )

    paper_dir = vault / "papers" / paper_id
    if not paper_dir.is_dir():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )

    meta_path = paper_dir / "metadata.yaml"
    if not meta_path.is_file():
        raise RmError(
            f"Paper folder {paper_dir} has no metadata.yaml — cannot rm "
            "safely. Inspect the directory and remove manually if needed."
        )
    target_meta = load_yaml_or_raise(meta_path, _yaml)
    if target_meta is None:
        target_meta = {}

    # ----- Relationship-network discovery (structured fields only) -----
    ref_opposites = {
        oid for oid, _ in _opposite_ref_targets(target_meta) if oid != paper_id
    }
    code_clones = [str(r) for r in (target_meta.get("code-clones") or [])]
    projects = [str(p) for p in (target_meta.get("projects") or [])]
    n_relations = len(ref_opposites) + len(code_clones) + len(projects)

    # ----- Confirmation -----
    # --dry-run is a preview: skip the destructive confirmation entirely (we
    # print the impact set + exit below before any write).
    title = target_meta.get("title") or "(no title)"
    if not skip_confirm and not dry_run:
        action = "permanently delete" if purge else "move to .trash/"
        if n_relations:
            console.print(
                f"This paper is linked with [bold]{n_relations}[/] "
                f"entr{'y' if n_relations == 1 else 'ies'} in {escape(str(vault))}."
            )
            console.print(
                f"  [dim](run 'lit show {escape(paper_id)}' to inspect them)[/]"
            )
        console.print(f"[bold yellow]About to {action}:[/]")
        console.print(f"  id     : {escape(paper_id)}")
        console.print(f"  title  : {escape(str(title))}")
        if purge:
            console.print(
                f"This permanently removes "
                f"{escape(str(paper_dir.relative_to(vault)))}/ "
                "and updates INDEX/views. [bold red]Not recoverable.[/]"
            )
        else:
            console.print(
                f"This moves {escape(str(paper_dir.relative_to(vault)))}/ "
                "into .trash/ and updates INDEX/views. "
                "Recover with [bold]lit trash restore[/]."
            )
        if not click.confirm("Delete?", default=False):
            console.print("[dim]Aborted. No changes made.[/]")
            return

    now = now_iso()
    registry = load_config(vault).projects

    # ----- Build cascade updates (always; teardown is the default now) -----
    safe_papers = list_papers(vault)
    cascade_ref_updates, touched_ref_ids = _build_cascade_ref_updates(
        vault, target_meta, paper_id, now, safe_papers
    )
    cascade_repo_updates, orphan_repos = _build_cascade_code_updates(
        vault, target_meta, paper_id, now
    )

    # ----- Drop the target from the in-memory paper list (for INDEX) -----
    surviving = [p for p in safe_papers if p.get("id") != paper_id]
    new_index = render_index(surviving, now)

    # ----- Annotate referencing notes/discussion with `(deleted)` (M24) -----
    # Scan every tracked markdown file; stage only those whose text actually
    # changed (mirrors rename's wikilink-rewrite pattern). The deleted paper's
    # own notes ride into trash unchanged — they are filtered out so we never
    # stage a write against a path that's about to move.
    note_updates: dict[str, str] = {}  # vault-relative path → annotated text
    for md_path in enumerate_markdown_files(vault):
        if md_path.parent.name == paper_id:
            continue
        text = md_path.read_text(encoding="utf-8")
        annotated = annotate_deleted_wikilinks(text, paper_id)
        if annotated != text:
            note_updates[str(md_path.relative_to(vault))] = annotated

    # ----- Dry-run: print the full impact set, then exit without writing -----
    if dry_run:
        verb = "permanently delete" if purge else "move to .trash/"
        console.print(
            f"[bold]Would {verb}:[/] {escape(paper_id)} "
            "[dim](dry-run)[/]"
        )
        console.print(f"  title  : {escape(str(title))}")
        if touched_ref_ids:
            console.print(
                "  Would clear references in: "
                f"[dim]{escape(', '.join(sorted(touched_ref_ids)))}[/]"
            )
        if cascade_repo_updates:
            # Keys are vault-relative paths (codes/<repo>/repo-meta.yaml);
            # show the bare repo name to match the post-commit message and the
            # orphan_repos line above.
            repo_names = sorted(k.split("/")[1] for k in cascade_repo_updates)
            console.print(
                "  Would unbind from repos (still bound by others): "
                f"[dim]{escape(', '.join(repo_names))}[/]"
            )
        if orphan_repos:
            console.print(
                "  Would remove orphan repos: "
                f"[dim]{escape(', '.join(sorted(orphan_repos)))}[/]"
            )
        if projects:
            console.print(
                "  Would unlink from projects: "
                f"[dim]{escape(', '.join(sorted(projects)))}[/]"
            )
        if note_updates:
            console.print(
                "  Would tag referencing notes: "
                f"[dim]{escape(', '.join(sorted(note_updates)))}[/]"
            )
        console.print(
            "[dim]Dry-run only — nothing deleted. "
            "Drop --dry-run to remove for real.[/]"
        )
        return

    # ----- Phase 1: staged commit of all file content updates -----
    with staged_write(vault, op_id=f"rm-{paper_id}") as stage:
        for pid, content in cascade_ref_updates.items():
            stage.write_text(f"papers/{pid}/metadata.yaml", content)
        for relpath, content in cascade_repo_updates.items():
            stage.write_text(relpath, content)
        for relpath, content in note_updates.items():
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", new_index)

    # ----- Phase 2: paper-folder removal (purge or trash) -----
    # If this fails partway, INDEX has already been updated; health-check
    # will flag the orphan dir.
    try:
        if purge:
            shutil.rmtree(paper_dir)
        else:
            move_to_trash(vault, paper_id, orphan_repos=orphan_repos)
    except OSError as e:
        # The staged metadata + INDEX write already committed above: the
        # library now considers the paper gone. A filesystem failure here
        # (Windows file lock, permissions) leaves the folder on disk as an
        # orphan — no data loss, but a brief inconsistency. Surface it
        # (invariant #1: never silent) and point at the repair tool instead
        # of crashing with a raw traceback.
        console.print(
            f"[yellow]warning:[/] could not remove "
            f"{escape(str(paper_dir))}: {escape(str(e))}\n"
            "  Metadata + INDEX are already updated; run "
            "`lit health-check --fix` to clear the orphan folder."
        )

    # ----- Phase 3a: orphan-repo hard-delete (1:1 case) -----
    for repo_name in orphan_repos:
        repo_root = vault / CODES_DIRNAME / repo_name
        if repo_root.is_dir():
            try:
                shutil.rmtree(repo_root)
            except OSError as e:
                # Best-effort, post-commit: the repo's binding was already
                # cleared in the staged write. A locked / unremovable repo dir
                # (Windows .git packfiles, antivirus) must not crash a delete
                # that already succeeded on the metadata side (invariant #1).
                console.print(
                    f"[yellow]warning:[/] could not remove orphan repo "
                    f"{escape(str(repo_root))}: {escape(str(e))}\n"
                    "  Its binding is already cleared; run "
                    "`lit health-check --fix` to finish the cleanup."
                )

    # ----- Phase 3b: project symlink teardown + REFERENCES re-render -----
    if projects:
        _teardown_project_links(
            vault, target_meta, paper_id, registry, list_papers(vault)
        )

    # ----- Phase 3c: post-commit derived rebuild via the shared funnel -----
    # INDEX + views recomputed together (M30 Phase 4); the staged INDEX.json
    # above is the crash-safety layer. project_refs=False: the removed paper's
    # project side was already torn down by _teardown_project_links above —
    # behavior unchanged from the pre-funnel command.
    reconcile_derived(vault, papers=list_papers(vault), project_refs=False)

    # ----- Output -----
    if purge:
        console.print(
            f"[bold green]✓ Purged[/] {escape(paper_id)} [dim](permanent)[/]"
        )
    else:
        console.print(
            f"[bold green]✓ Trashed[/] {escape(paper_id)} "
            f"[dim](recover via `lit trash restore {escape(paper_id)}`)[/]"
        )
        # Ring eviction: keep at most TRASH_MAX_ENTRIES; surface what we
        # permanently dropped (never silent — invariant #1).
        evicted = enforce_cap(vault)
        if evicted:
            console.print(
                f"  [yellow]Trash at cap ({TRASH_MAX_ENTRIES}); "
                f"permanently removed oldest: "
                f"{escape(', '.join(evicted))}[/]"
            )
    if touched_ref_ids:
        n = len(touched_ref_ids)
        console.print(
            f"  Cleared references in [bold]{n}[/] paper"
            f"{'s' if n != 1 else ''} "
            f"[dim]({escape(', '.join(sorted(touched_ref_ids)))})[/]"
        )
    if cascade_repo_updates:
        n = len(cascade_repo_updates)
        console.print(
            f"  Unbound from [bold]{n}[/] repo{'s' if n != 1 else ''} "
            "[dim](still bound by other papers)[/]"
        )
    if orphan_repos:
        n = len(orphan_repos)
        console.print(
            f"  Removed [bold]{n}[/] orphan repo{'s' if n != 1 else ''} "
            f"[dim]({escape(', '.join(sorted(orphan_repos)))})[/]"
        )
    if projects:
        console.print(
            f"  Unlinked from [bold]{len(projects)}[/] project"
            f"{'s' if len(projects) != 1 else ''}"
        )
    if note_updates:
        n = len(note_updates)
        console.print(
            f"  Tagged [bold]{n}[/] referencing note{'s' if n != 1 else ''} "
            f"[dim](`[[{escape(paper_id)}]] (deleted)`)[/]"
        )
    console.print("[dim]INDEX.json + views/ refreshed.[/]")
