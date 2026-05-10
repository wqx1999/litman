"""Shared helpers for scanning markdown notes that may contain ``[[id]]`` wikilinks.

Used by ``lit rename`` (M2.6) to rewrite wikilinks and ``lit rm`` (M2.7) to
detect / strip them. Centralising the scope here keeps both commands honest
about which files participate in the wikilink graph.

Wikilink scope (per design doc §5.1):
    * ``papers/<id>/notes.md``                       — per-paper notes
    * ``notes/{methods,ideas,debates}/*.md``         — cross-paper notes

Anything outside these locations is the user's own thing and is left
untouched by both rename and rm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

# Subdirectories under notes/ that may contain wikilink references.
NOTES_SUBDIRS: tuple[str, ...] = ("methods", "ideas", "debates")


def enumerate_markdown_files(vault: Path) -> Iterable[Path]:
    """Yield .md files in the wikilink scope (see module docstring)."""
    papers_dir = vault / "papers"
    if papers_dir.is_dir():
        for child in papers_dir.iterdir():
            if child.is_dir():
                note = child / "notes.md"
                if note.is_file():
                    yield note
    for sub in NOTES_SUBDIRS:
        notes_subdir = vault / "notes" / sub
        if notes_subdir.is_dir():
            for md in sorted(notes_subdir.glob("*.md")):
                yield md
