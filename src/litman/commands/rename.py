"""``lit rename`` — change a paper's id and ripple to all back-references.

A paper id is the on-disk handle (``papers/<id>/``), the canonical
metadata.yaml ``id`` field, the value used in other papers'
``related`` / ``contradicts`` / ``extends`` lists, and the body of any
``[[<id>]]`` wikilink scattered across notes. Renaming therefore touches:

1. ``papers/<old>/metadata.yaml`` — ``id`` field, plus any self-references
   in the ref-list fields.
2. Every other ``papers/<other>/metadata.yaml`` whose ref lists contain
   ``<old>``.
3. Every markdown notes file in ``papers/*/notes.md`` containing the
   literal substring ``[[<old>]]``.
4. ``INDEX.json`` (regenerated from the post-rename paper list).
5. The directory itself: ``papers/<old>/`` → ``papers/<new>/``.
6. ``views/by-*/`` symlink hubs (rebuilt afterward).

Atomicity layers:

* All file content updates (1–4) go through one ``staged_write`` so they
  either all land or all roll back.
* The directory rename (5) is a single ``os.rename`` after the file
  commit. A failure here leaves the on-disk metadata pointing at ``<new>``
  while the dir name is still ``<old>`` — detectable and recoverable
  (re-run rename, or rename the dir manually). ``lit health-check`` (M2.8)
  flags the mismatch.

The ``related`` / ``contradicts`` / ``extends`` fields hold plain string
ids (not ``[[id]]`` wikilinks). Wikilinks are a notes-only convention.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.code import CODES_DIRNAME, REPO_META_FILENAME
from litman.core.correctors import reconcile_derived
from litman.core.dates import now_iso
from litman.core.document import list_papers, load_yaml_or_raise
from litman.core.id import find_case_fold_collision, is_valid_id
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.notes import enumerate_markdown_files
from litman.core.paper_lookup import complete_paper_id, resolve_paper_id
from litman.core.relations import ALL_REF_FIELDS
from litman.core.views import render_index
from litman.exceptions import PaperNotFoundError, RenameError

console = Console()

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.default_flow_style = False

# Metadata.yaml list fields that hold paper-id references, forward and
# reverse (ADR-012). Renaming an id must rewrite it inside reverse fields
# (`extended-by` / `contradicted-by`) too, else a dangling reverse ref is
# left behind. Wikilink-formatted refs ([[id]]) live in markdown only and
# are handled separately.
REF_FIELDS: tuple[str, ...] = ALL_REF_FIELDS


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _replace_in_ref_lists(
    metadata: dict[str, Any], old: str, new: str
) -> bool:
    """Replace ``old`` with ``new`` in every ref-field list. Returns True
    iff at least one field changed.
    """
    changed = False
    for field in REF_FIELDS:
        current = metadata.get(field)
        if not current or old not in current:
            continue
        new_list = []
        seen: set[str] = set()
        for v in current:
            replaced = new if v == old else v
            if replaced not in seen:
                new_list.append(replaced)
                seen.add(replaced)
        metadata[field] = new_list
        changed = True
    return changed


def _format_id_list(ids: list[str], limit: int = 5) -> str:
    if not ids:
        return ""
    if len(ids) <= limit:
        return ", ".join(ids)
    return ", ".join(ids[:limit]) + f", ... (+{len(ids) - limit} more)"


@click.command("rename")
@click.argument("old", shell_complete=complete_paper_id)
@click.argument("new")
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
def rename_cmd(
    old: str, new: str, library: Path | None, vault_name: str | None
) -> None:
    """Change a paper id from <old> to <new>, rippling everywhere.

    The <old> argument accepts a full id or a unique case-insensitive
    substring. <new> is the destination id and must be the exact
    target shape — fuzzy resolution does NOT apply to <new> (a paper
    matching that substring would be an unrelated collision, not the
    desired target).

    No --paper-doi option is offered here: rename takes two positional
    arguments and Click's parser cannot reliably tell which positional is
    <old> vs <new> when <old> is omitted. Use lit list to
    look up the id by DOI if needed.

    Touches the renamed paper's metadata + dir, every other paper's
    metadata that references it, every notes.md with a [[<old>]]
    wikilink, INDEX.json, and views/.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    old = resolve_paper_id(vault, old)

    if old == new:
        raise RenameError("`old` and `new` are identical — nothing to do.")
    if not is_valid_id(new):
        raise RenameError(
            f"Invalid new id {new!r}. Ids contain only ASCII letters, "
            "digits, dots, underscores, and hyphens; no leading dot, no "
            "slashes, no '..'."
        )
    if not is_valid_id(old):
        raise RenameError(
            f"Invalid old id {old!r}. Use `lit list` to find the right id."
        )

    old_dir = vault / "papers" / old
    new_dir = vault / "papers" / new

    if not old_dir.is_dir():
        raise PaperNotFoundError(
            f"No paper with id {old!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )
    if new_dir.exists():
        raise RenameError(
            f"A paper with id {new!r} already exists at {new_dir}. "
            "Pick a different id or remove the existing paper first."
        )

    # Case-fold collision (ADR-005 / mirrors add.py): two ids differing only
    # in case coexist on Linux but collapse on Windows / default macOS, so the
    # vault silently loses one paper when moved between OSes. ``old`` is
    # excluded from the candidate set so that recasing a paper's own id
    # (``2023_Pandi`` -> ``2023_pandi``) — a legitimate move, not a duplicate —
    # is still allowed.
    existing_ids = [
        p.name
        for p in (vault / "papers").iterdir()
        if p.is_dir() and p.name != old
    ]
    case_clash = find_case_fold_collision(existing_ids, new)
    if case_clash is not None:
        raise RenameError(
            f"New id {new!r} differs only in case from existing paper "
            f"{case_clash!r}. Two ids that case-fold to the same string "
            "collide on Windows / default macOS filesystems (case-insensitive) "
            "and the vault loses data when moved between OSes. Pick a "
            "substantially different id."
        )

    now = now_iso()

    # ----- Build the renamed paper's new metadata content -----
    renamed_meta_path = old_dir / "metadata.yaml"
    if not renamed_meta_path.is_file():
        raise RenameError(
            f"Renamed paper has no metadata.yaml at {renamed_meta_path}. "
            "Restore the file or remove the directory."
        )
    renamed_meta = load_yaml_or_raise(renamed_meta_path, _yaml)
    if renamed_meta is None:
        raise RenameError(
            f"metadata.yaml at {renamed_meta_path} is empty — cannot rename."
        )
    renamed_meta["id"] = new
    _replace_in_ref_lists(renamed_meta, old, new)  # self-refs, if any
    renamed_meta["updated-at"] = now
    renamed_yaml = _dump_yaml_to_string(renamed_meta)

    # ----- Find and rewrite back-referencing papers -----
    safe_papers = list_papers(vault)  # safe-loaded, used for INDEX rendering
    other_updates: dict[str, str] = {}  # paper_id → new yaml text
    ref_holders: list[str] = []
    for paper in safe_papers:
        pid = paper.get("id")
        if not pid or pid == old:
            continue
        if not any(old in (paper.get(f) or []) for f in REF_FIELDS):
            continue
        meta_path = vault / "papers" / str(pid) / "metadata.yaml"
        rt = load_yaml_or_raise(meta_path, _yaml)
        if rt is None:
            continue
        if _replace_in_ref_lists(rt, old, new):
            rt["updated-at"] = now
            other_updates[str(pid)] = _dump_yaml_to_string(rt)
            ref_holders.append(str(pid))
            # Mirror the change into the safe-loaded copy so the INDEX
            # render reflects it without re-reading from disk.
            for f in REF_FIELDS:
                cur = paper.get(f) or []
                if old in cur:
                    paper[f] = [new if v == old else v for v in cur]
            paper["updated-at"] = now

    # Apply rename to the safe-loaded copy of the renamed paper itself so
    # INDEX.json shows the new id immediately.
    for paper in safe_papers:
        if paper.get("id") == old:
            paper["id"] = new
            for f in REF_FIELDS:
                cur = paper.get(f) or []
                if old in cur:
                    paper[f] = [new if v == old else v for v in cur]
            paper["updated-at"] = now
            break

    # ----- Cascade the id change into bound repos' repo-meta.yaml -----
    # A paper id is mirrored in codes/<repo>/repo-meta.yaml::papers for every
    # repo the paper binds (invariant #12 bidirectional binding). Renaming the
    # paper must rewrite that back-reference too, else `lit code list` /
    # health-check sees a dangling <old> id. Only the renamed paper's own bound
    # repos are touched — code-clones holds repo names, so no OTHER paper's
    # bindings change. repo-meta.yaml is not part of INDEX, so this just adds a
    # few transactional writes; no extra derived rebuild is needed.
    repo_meta_updates: dict[str, str] = {}  # vault-relative path → new yaml
    repo_binders: list[str] = []
    for repo_name in renamed_meta.get("code-clones") or []:
        repo_name = str(repo_name)
        repo_meta_path = vault / CODES_DIRNAME / repo_name / REPO_META_FILENAME
        if not repo_meta_path.is_file():
            # Binding present on the paper side but repo-meta missing: an
            # orphan ref, surfaced by health-check. Nothing to rewrite here.
            continue
        rt = load_yaml_or_raise(repo_meta_path, _yaml)
        if rt is None:
            continue
        papers = rt.get("papers") or []
        if old not in papers:
            continue
        seen: set[str] = set()
        new_papers: list[str] = []
        for p in papers:
            replaced = new if p == old else p
            if replaced not in seen:  # de-dup, preserve order (as REF_FIELDS)
                new_papers.append(replaced)
                seen.add(replaced)
        rt["papers"] = new_papers
        rt["updated-at"] = now
        repo_meta_updates[
            f"{CODES_DIRNAME}/{repo_name}/{REPO_META_FILENAME}"
        ] = _dump_yaml_to_string(rt)
        repo_binders.append(repo_name)

    # ----- Notes.md wikilink rewrites -----
    needle = f"[[{old}]]"
    replacement = f"[[{new}]]"
    note_updates: dict[str, str] = {}  # vault-relative path → new content
    for md_path in enumerate_markdown_files(vault):
        text = md_path.read_text(encoding="utf-8")
        if needle in text:
            note_updates[str(md_path.relative_to(vault))] = text.replace(
                needle, replacement
            )

    # ----- INDEX.json -----
    new_index = render_index(safe_papers, now)

    # ----- Stage everything as one transactional write -----
    rel_renamed_meta = f"papers/{old}/metadata.yaml"
    with staged_write(vault, op_id=f"rename-{old}-to-{new}") as stage:
        stage.write_text(rel_renamed_meta, renamed_yaml)
        for pid, content in other_updates.items():
            stage.write_text(f"papers/{pid}/metadata.yaml", content)
        for relpath, content in repo_meta_updates.items():
            stage.write_text(relpath, content)
        for relpath, content in note_updates.items():
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", new_index)

    # ----- Atomic directory rename -----
    # POSIX guarantees atomicity here: target was pre-validated as
    # non-existent. Failure modes (disk full, permissions) are rare and
    # leave on-disk metadata pointing at <new> while dir is still <old>.
    # `lit health-check` (M2.8) flags this mismatch.
    os.rename(old_dir, new_dir)

    # ----- Post-commit derived rebuild through the shared funnel (M30 Phase 4) -----
    # INDEX + views recomputed together; the staged INDEX.json above remains
    # the crash-safety layer.
    #
    # review F14: a project-linked paper's id is embedded in the project's
    # litman_reflib/<id> symlink and REFERENCES.md, so renaming it leaves a
    # dangling <old> symlink + a stale REFERENCES.md unless the project side is
    # rebuilt. Gate on the renamed paper's own membership (rename never changes
    # any OTHER paper's projects field) so an unlinked paper skips the cost.
    renamed_is_project_member = bool(renamed_meta.get("projects"))
    reconcile_derived(
        vault,
        papers=list_papers(vault),
        project_refs=renamed_is_project_member,
    )

    # ----- Output -----
    console.print(
        f"[bold green]✓ Renamed[/] {escape(old)} → {escape(new)}"
    )
    if ref_holders:
        console.print(
            f"  Updated [bold]{len(ref_holders)}[/] back-referencing "
            f"paper{'s' if len(ref_holders) != 1 else ''} "
            f"[dim]({escape(_format_id_list(sorted(ref_holders)))})[/]"
        )
    if repo_binders:
        n = len(repo_binders)
        console.print(
            f"  Updated [bold]{n}[/] repo binding{'s' if n != 1 else ''} "
            f"[dim]({escape(_format_id_list(sorted(repo_binders)))})[/]"
        )
    if note_updates:
        n = len(note_updates)
        console.print(
            f"  Updated [bold]{n}[/] notes file{'s' if n != 1 else ''}"
        )
    console.print("[dim]INDEX.json + views/ refreshed.[/]")
