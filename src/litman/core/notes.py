"""Shared helpers for scanning markdown notes that may contain ``[[id]]`` wikilinks.

Used by ``lit rename`` (M2.6) to rewrite wikilinks, ``lit rm`` (M23/M24) to
annotate deleted targets, ``lit trash restore`` (M24) to de-annotate, and
``lit health-check`` (M2.8 + M8.4 + M24.2) to flag dangling / drifted
wikilink targets. Centralising the scope here keeps every caller honest
about which files participate in the wikilink graph.

Wikilink scope (per design doc §5.1):
    * ``papers/<id>/notes.md``      — per-paper notes (always scaffolded)
    * ``papers/<id>/discussion.md`` — per-paper discussion, created
      on-demand (M21), so it is guarded with ``.is_file()`` and simply
      absent for papers that have none.

Anything outside these locations is the user's own thing and is left
untouched by rename / rm / restore.

Wikilink syntax (M8.4 extends the original one-form syntax):
    * ``[[paper-id]]``           — same-vault reference (legacy form)
    * ``[[vault-name:paper-id]]``— cross-vault reference, where
      ``vault-name`` is the handle registered in
      ``~/.config/litman/vaults.yaml``.

The same-vault form is preserved exactly as before, so existing notes
and tests stay valid. The cross-vault form lets agents and humans
write references to papers living in linked fork vaults without
having to first copy them across.

Deletion-status tags (M24): the CLI maintains an inline ``(deleted)``
suffix on same-vault ``[[A]]`` links whenever ``papers/A/`` is absent.
:func:`annotate_deleted_wikilinks` adds it on ``lit rm``,
:func:`deannotate_deleted_wikilinks` strips it on ``lit trash restore``.
Both key off the RESOLVED target id (never the literal prior string), so
a note an agent rewrote still self-heals. The filesystem (``papers/A/``)
is the single source of truth; the tag is only its surfaced projection
(ADR-013).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# Same ``[[...]]`` form health-check uses (core/checks.py): no ``|alias``
# support, no nested brackets, single line. Kept identical so annotate /
# de-annotate / dangling-detection all agree on what a wikilink is.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")

# The status suffix appended right after a ``[[A]]`` whose target is gone.
_DELETED_SUFFIX = " (deleted)"


def enumerate_markdown_files(vault: Path) -> Iterable[Path]:
    """Yield .md files in the wikilink scope (see module docstring).

    Both ``papers/<id>/notes.md`` and ``papers/<id>/discussion.md`` are
    yielded when present. ``discussion.md`` is created on-demand (M21), so a
    paper without one simply contributes only its ``notes.md``.
    """
    papers_dir = vault / "papers"
    if papers_dir.is_dir():
        for child in papers_dir.iterdir():
            if child.is_dir():
                for name in ("notes.md", "discussion.md"):
                    md = child / name
                    if md.is_file():
                        yield md


def parse_wikilink_target(raw: str) -> tuple[str | None, str]:
    """Split the inner text of a ``[[...]]`` wikilink into (vault, paper_id).

    Examples:
        ``"2024_Wang_AMP"``           → ``(None, "2024_Wang_AMP")``
        ``"zhang-shared:2024_Wang_AMP"`` → ``("zhang-shared", "2024_Wang_AMP")``
        ``"  zhang : id  "``         → ``("zhang", "id")``  (whitespace stripped)

    Splits on the FIRST ``:`` only. Paper ids never contain ``:`` per
    :func:`litman.core.id.is_valid_id`, so any colon in the target text
    must be the vault separator.

    Returns:
        ``(None, paper_id)`` for same-vault links; ``(vault_name, paper_id)``
        for cross-vault links. Either ``vault_name`` or ``paper_id`` may
        come back empty when the input is malformed (e.g. ``"vault:"`` or
        ``":id"``); the caller decides how to surface those.
    """
    raw = raw.strip()
    if ":" not in raw:
        return (None, raw)
    vault_name, _, paper_id = raw.partition(":")
    return (vault_name.strip(), paper_id.strip())


def annotate_deleted_wikilinks(text: str, deleted_id: str) -> str:
    """Append ``" (deleted)"`` after each same-vault ``[[deleted_id]]``.

    Only same-vault links whose RESOLVED target equals ``deleted_id`` are
    touched (cross-vault ``[[v:id]]`` is out of scope, ADR-013 / M24). The
    match keys on the parsed target, never on the literal bracket string, so
    an agent-rewritten link still gets tagged. Idempotent: a link already
    followed by ``" (deleted)"`` is left as-is — no ``(deleted) (deleted)``.

    Returns the text unchanged when nothing matched, so callers can stage
    only files whose content actually changed.
    """
    return _retarget(text, deleted_id, annotate=True)


def deannotate_deleted_wikilinks(text: str, restored_id: str) -> str:
    """Strip a trailing ``" (deleted)"`` after each ``[[restored_id]]``.

    Inverse of :func:`annotate_deleted_wikilinks`: same-vault links resolving
    to ``restored_id`` lose the suffix; everything else is untouched.
    Idempotent — a link with no suffix is a no-op.
    """
    return _retarget(text, restored_id, annotate=False)


def _retarget(text: str, target_id: str, *, annotate: bool) -> str:
    """Rewrite the deletion suffix on every ``[[...]]`` resolving to target_id.

    Walks matches left-to-right and rebuilds the string so suffix
    insertion / removal never shifts an index we still need (a plain
    ``re.sub`` cannot peek at the char *after* ``]]`` to enforce
    idempotency). Untouched links and all non-link text are copied verbatim,
    so a file with no matching target comes back byte-identical.
    """
    out: list[str] = []
    pos = 0
    for m in _WIKILINK_RE.finditer(text):
        vault_prefix, paper_id = parse_wikilink_target(m.group(1))
        if vault_prefix is not None or paper_id != target_id:
            continue
        # Copy everything up to and including this link's closing ``]]``.
        out.append(text[pos : m.end()])
        pos = m.end()
        has_suffix = text.startswith(_DELETED_SUFFIX, pos)
        if annotate and not has_suffix:
            out.append(_DELETED_SUFFIX)
        elif not annotate and has_suffix:
            pos += len(_DELETED_SUFFIX)
    out.append(text[pos:])
    return "".join(out)
