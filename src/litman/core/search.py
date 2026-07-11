"""Deterministic substring search over per-paper notes / discussion (M33).

``lit search`` is the spine of M33: the content a user hand-writes into
``papers/<id>/notes.md`` and ``papers/<id>/discussion.md`` is the highest-value
context for an agent (identity §"agent 优先用户视角") yet, before this module,
was invisible to every CLI query — agents had to fall back to
``grep papers/*/notes.md``, the exact "bypass the CLI, read vault files
directly" pattern ADR-007 exists to close.

Pure Layer-2 business logic: no CLI rendering, no LLM. The match is a fixed
case-insensitive substring (invariant #5: weak / no LLM still works), not a
vector or semantic search. ``paper.pdf`` full text, ``.trash/``, and the
``views/`` symlink hubs are out of scope — the search corpus is exactly the
two authored markdown files per paper, minus their HTML comments (the seeded
format reminders are scaffolding, not something the user wrote).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from litman.core.notes import enumerate_markdown_files

# HTML comments are scaffolding, not authored content. Both scaffolds seed one
# (notes.md the wikilink reminder, discussion.md the append-format reminder), so
# a comment left in the corpus would return a hit on EVERY paper for a query
# like "wikilink" or "append-only" — noise no user wrote. Same reasoning, same
# regex as core/checks.py's dangling-wikilink scan, which strips comments for
# the same reason.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _mask_html_comments(text: str) -> str:
    """Blank out comment regions, preserving every character position.

    Each commented-out character becomes a space and each newline stays a
    newline, so the masked text has the same line count and the same columns as
    the original — the hit's ``line`` number (which the Web UI uses to scroll
    the reader to the match) keeps pointing at the real line.
    """

    def _blank(match: re.Match[str]) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))

    return _HTML_COMMENT_RE.sub(_blank, text)


def search_notes(
    vault: Path,
    query: str,
    *,
    in_files: tuple[str, ...] = ("notes", "discussion"),
    case_insensitive: bool = True,
) -> list[dict[str, Any]]:
    """Substring-search ``notes.md`` / ``discussion.md`` across the vault.

    Walks the wikilink scope via :func:`enumerate_markdown_files`, which yields
    only ``papers/<id>/{notes,discussion}.md`` — so ``.trash/`` and ``views/``
    are naturally excluded (they are not under ``papers/``). Each line that
    contains ``query`` becomes one hit.

    Args:
        vault: the vault root.
        query: the substring to find. Matched verbatim (no regex).
        in_files: which file stems to search. ``("notes",)`` narrows to
            notes only; ``("discussion",)`` to discussion only. A stem not in
            this tuple is skipped.
        case_insensitive: lowercase both sides before comparing (default).

    Returns:
        A list of ``{id, file, line, snippet}`` dicts, where ``id`` is the
        paper id, ``file`` is the file stem (``"notes"`` / ``"discussion"``),
        ``line`` is the 1-based line number, and ``snippet`` is the WHOLE
        matched line with its trailing newline stripped (no truncation — the
        caller / agent decides how much to keep). Ordered by paper id then
        file then line number (the deterministic order
        :func:`enumerate_markdown_files` yields).
    """
    # An empty / whitespace-only needle would match every line (``"" in s``
    # is always True), turning ``lit search ""`` into a whole-vault dump and
    # breaking the bounded-retrieval contract (ADR-007). Enumeration over the
    # vault is the caller's job (``lit list``), not search's.
    if not query.strip():
        return []
    needle = query.lower() if case_insensitive else query
    hits: list[dict[str, Any]] = []
    for md_path in enumerate_markdown_files(vault):
        file_stem = md_path.stem
        if file_stem not in in_files:
            continue
        paper_id = md_path.parent.name
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # One corrupt / non-UTF-8 / vanished file must not abort the
            # whole search — skip it, mirroring list_papers' tolerance. The
            # corrupt-file finding is owned by `lit health-check`.
            continue
        # Match against the masked text, report the raw line: a line that mixes
        # authored prose and a trailing comment still matches on its prose and
        # still shows the user the line as it really reads on disk.
        raw_lines = text.splitlines()
        masked_lines = _mask_html_comments(text).splitlines()
        for lineno, (raw_line, masked_line) in enumerate(
            zip(raw_lines, masked_lines), start=1
        ):
            haystack = masked_line.lower() if case_insensitive else masked_line
            if needle in haystack:
                hits.append(
                    {
                        "id": paper_id,
                        "file": file_stem,
                        "line": lineno,
                        "snippet": raw_line,
                    }
                )
    return hits
