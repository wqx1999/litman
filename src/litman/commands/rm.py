"""``lit rm`` — remove a paper from the vault.

Symmetric to ``lit rename`` (M2.6) but destructive. By default the paper
folder is moved into ``<vault>/.trash/`` (recoverable via
``lit trash restore <id>``); pass ``--purge`` to permanently delete.
Either way, INDEX.json and views/ are refreshed. Two safety nets guard
against silent data loss:

* Reverse-ref scan: refuse if any other paper's
  ``related`` / ``contradicts`` / ``extends`` list contains ``<id>``.
* Wikilink scan: refuse if any ``papers/*/notes.md`` contains
  ``[[<id>]]``.

Both checks are bypassed by ``--cascade``, which auto-clears the references
(deletes the id from ref-list fields and replaces ``[[<id>]]`` with the
bare ``<id>`` text so the surrounding prose stays readable). Cascade
modifications are NOT recorded in trash — only the removed paper itself
is restorable.

A ``y/N`` prompt is shown before destruction, suppressed by ``--yes``.

Atomicity layers (mirrors rename):

* All file content updates (INDEX.json + cascade ref/wikilink edits) go
  through one ``staged_write`` so they either all land or all roll back.
* The paper-folder move-to-trash (or rmtree on ``--purge``) is the final
  step after the file commit. A failure here leaves INDEX.json claiming
  the paper is gone while the dir is still on disk — detectable and
  recoverable (``lit health-check`` will flag this in M2.8).
"""

from __future__ import annotations

import io
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.document import list_papers
from litman.core.id import is_valid_id
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.notes import enumerate_markdown_files
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input
from litman.core.trash import move_to_trash
from litman.core.views import rebuild_views, render_index
from litman.exceptions import PaperNotFoundError, RmError

console = Console()

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.default_flow_style = False

REF_FIELDS: tuple[str, ...] = ("related", "contradicts", "extends")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _scan_ref_holders(
    papers: list[dict[str, Any]], target_id: str
) -> list[tuple[str, str]]:
    """Return ``[(holder_id, ref_field), ...]`` for papers referencing ``target_id``.

    A paper that lists ``target_id`` in two ref fields appears twice (one
    entry per field) so the user sees the full picture.
    """
    out: list[tuple[str, str]] = []
    for paper in papers:
        pid = paper.get("id")
        if not pid or pid == target_id:
            continue
        for field in REF_FIELDS:
            values = paper.get(field) or []
            if target_id in values:
                out.append((str(pid), field))
    return out


def _scan_wikilink_files(
    vault: Path, target_id: str
) -> list[tuple[Path, int]]:
    """Return ``[(path, occurrence_count), ...]`` for notes containing ``[[<id>]]``."""
    needle = f"[[{target_id}]]"
    out: list[tuple[Path, int]] = []
    for md_path in enumerate_markdown_files(vault):
        text = md_path.read_text(encoding="utf-8")
        n = text.count(needle)
        if n > 0:
            out.append((md_path, n))
    return out


def _format_holders_block(holders: list[tuple[str, str]], limit: int = 10) -> str:
    head = holders[:limit]
    lines = [f"  - {pid} ({field})" for pid, field in head]
    if len(holders) > limit:
        lines.append(f"  ... and {len(holders) - limit} more")
    return "\n".join(lines)


def _format_wikilinks_block(
    vault: Path, hits: list[tuple[Path, int]], limit: int = 10
) -> str:
    head = hits[:limit]
    lines = []
    for path, n in head:
        rel = path.relative_to(vault)
        suffix = "" if n == 1 else f" ({n} occurrences)"
        lines.append(f"  - {rel}{suffix}")
    if len(hits) > limit:
        lines.append(f"  ... and {len(hits) - limit} more")
    return "\n".join(lines)


def _build_cascade_ref_updates(
    vault: Path,
    holders: list[tuple[str, str]],
    target_id: str,
    now: str,
    safe_papers: list[dict[str, Any]],
) -> tuple[dict[str, str], set[str]]:
    """Build staged metadata.yaml writes that drop ``target_id`` from ref fields.

    Mutates ``safe_papers`` in place so the caller can re-render INDEX.json
    from the same in-memory list without re-reading disk.

    Returns ``(staged_writes, holder_id_set)`` where:
        * ``staged_writes`` maps paper-id → new yaml text
        * ``holder_id_set`` is the unique set of holder ids touched
    """
    holder_ids = {pid for pid, _ in holders}
    staged: dict[str, str] = {}
    for pid in holder_ids:
        meta_path = vault / "papers" / pid / "metadata.yaml"
        rt = _yaml.load(meta_path.read_text(encoding="utf-8"))
        if rt is None:
            continue
        for field in REF_FIELDS:
            cur = rt.get(field)
            if cur and target_id in cur:
                rt[field] = [v for v in cur if v != target_id]
        rt["updated-at"] = now
        staged[pid] = _dump_yaml_to_string(rt)
        # Mirror into the safe-loaded copy so INDEX.json is correct.
        for paper in safe_papers:
            if paper.get("id") == pid:
                for field in REF_FIELDS:
                    cur = paper.get(field) or []
                    if target_id in cur:
                        paper[field] = [v for v in cur if v != target_id]
                paper["updated-at"] = now
                break
    return staged, holder_ids


def _build_cascade_wikilink_updates(
    vault: Path,
    hits: list[tuple[Path, int]],
    target_id: str,
) -> dict[str, str]:
    """Build staged note rewrites: ``[[<id>]]`` → ``<id>`` (text preserved)."""
    needle = f"[[{target_id}]]"
    replacement = target_id
    out: dict[str, str] = {}
    for path, _ in hits:
        text = path.read_text(encoding="utf-8")
        out[str(path.relative_to(vault))] = text.replace(needle, replacement)
    return out


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
    "--cascade",
    is_flag=True,
    default=False,
    help=(
        "Auto-clear references in other papers and strip [[id]] wikilinks "
        "(text preserved without brackets). Default refuses when refs exist."
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
    help="Skip the y/N confirmation prompt.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
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
    cascade: bool,
    purge: bool,
    skip_confirm: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Remove a paper from the vault.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass ``--paper-doi <DOI>`` instead.

    By default moves ``papers/<id>/`` to ``<vault>/.trash/`` (recoverable
    via ``lit trash restore <id>``). Pass ``--purge`` to permanently delete.
    INDEX.json and ``views/by-*/`` are refreshed either way. Refuses if
    any other paper references it (via ``related`` / ``contradicts`` /
    ``extends``) or any notes file contains ``[[<id>]]``, unless
    ``--cascade`` is given.
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
    target_meta = _yaml.load(meta_path.read_text(encoding="utf-8"))
    if target_meta is None:
        target_meta = {}

    # ----- Reverse-ref scan -----
    safe_papers = list_papers(vault)
    holders = _scan_ref_holders(safe_papers, paper_id)
    if holders and not cascade:
        block = _format_holders_block(holders)
        n_papers = len({pid for pid, _ in holders})
        raise RmError(
            f"Cannot remove {paper_id!r}: {n_papers} paper(s) still "
            f"reference it.\n{block}\n"
            f"Run `lit modify <id> --rm-tag <field>={paper_id}` on each, "
            "or pass --cascade to auto-clear."
        )

    # ----- Wikilink scan -----
    wiki_hits = _scan_wikilink_files(vault, paper_id)
    if wiki_hits and not cascade:
        block = _format_wikilinks_block(vault, wiki_hits)
        raise RmError(
            f"Cannot remove {paper_id!r}: {len(wiki_hits)} notes file(s) "
            f"contain [[{paper_id}]] wikilinks.\n{block}\n"
            "Edit those notes manually, or pass --cascade to strip the "
            "brackets (text preserved)."
        )

    # ----- Confirmation -----
    title = target_meta.get("title") or "(no title)"
    added = target_meta.get("created-at") or "(unknown)"
    if not skip_confirm:
        action = "permanently delete" if purge else "move to .trash/"
        console.print(f"[bold yellow]About to {action}:[/]")
        console.print(f"  id     : {escape(paper_id)}")
        console.print(f"  title  : {escape(str(title))}")
        console.print(f"  added  : {escape(str(added))}")
        if cascade and (holders or wiki_hits):
            n_holders = len({pid for pid, _ in holders})
            extras = []
            if holders:
                extras.append(
                    f"{n_holders} ref-holding paper{'s' if n_holders != 1 else ''}"
                )
            if wiki_hits:
                extras.append(
                    f"{len(wiki_hits)} notes file{'s' if len(wiki_hits) != 1 else ''}"
                )
            console.print(
                f"  [yellow]--cascade:[/] will also touch {' + '.join(extras)}."
            )
            console.print(
                "  [yellow]Note:[/] cascade edits to other papers / notes "
                "are NOT recorded in trash — only this paper is restorable."
            )
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
        if not click.confirm("Continue?", default=False):
            console.print("[dim]Aborted. No changes made.[/]")
            return

    now = _now_iso()

    # ----- Build cascade updates (only if --cascade) -----
    cascade_meta_updates: dict[str, str] = {}
    cascade_holder_ids: set[str] = set()
    cascade_note_updates: dict[str, str] = {}
    if cascade:
        if holders:
            cascade_meta_updates, cascade_holder_ids = _build_cascade_ref_updates(
                vault, holders, paper_id, now, safe_papers
            )
        if wiki_hits:
            cascade_note_updates = _build_cascade_wikilink_updates(
                vault, wiki_hits, paper_id
            )

    # ----- Drop the target from the in-memory paper list (for INDEX) -----
    surviving = [p for p in safe_papers if p.get("id") != paper_id]
    new_index = render_index(surviving, now)

    # ----- Phase 1: staged commit of all file content updates -----
    with staged_write(vault, op_id=f"rm-{paper_id}") as stage:
        for pid, content in cascade_meta_updates.items():
            stage.write_text(f"papers/{pid}/metadata.yaml", content)
        for relpath, content in cascade_note_updates.items():
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", new_index)

    # ----- Phase 2: paper-folder removal (purge or trash) -----
    # If this fails partway, INDEX has already been updated; M2.8
    # health-check will flag the orphan dir.
    if purge:
        shutil.rmtree(paper_dir)
        trash_entry_path = None
    else:
        trash_entry_path = move_to_trash(
            vault, paper_id, cascade_was_used=cascade
        )

    # ----- Phase 3: views/ rebuild (best-effort, recoverable) -----
    rebuild_views(vault, list_papers(vault))

    # ----- Output -----
    if purge:
        console.print(
            f"[bold green]✓ Purged[/] {escape(paper_id)} [dim](permanent)[/]"
        )
    else:
        assert trash_entry_path is not None
        console.print(
            f"[bold green]✓ Trashed[/] {escape(paper_id)} "
            f"[dim](recover via `lit trash restore {escape(paper_id)}`)[/]"
        )
    if cascade_holder_ids:
        n = len(cascade_holder_ids)
        console.print(
            f"  Cleared references in [bold]{n}[/] paper"
            f"{'s' if n != 1 else ''} "
            f"[dim]({escape(', '.join(sorted(cascade_holder_ids)))})[/]"
        )
    if cascade_note_updates:
        n = len(cascade_note_updates)
        console.print(
            f"  Stripped [[{escape(paper_id)}]] from [bold]{n}[/] notes file"
            f"{'s' if n != 1 else ''}"
        )
    console.print("[dim]INDEX.json + views/ refreshed.[/]")
