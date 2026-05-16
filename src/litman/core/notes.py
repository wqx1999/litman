"""Shared helpers for scanning markdown notes that may contain ``[[id]]`` wikilinks.

Used by ``lit rename`` (M2.6) to rewrite wikilinks, ``lit rm`` (M2.7) to
detect / strip them, and ``lit health-check`` (M2.8 + M8.4) to flag
dangling wikilink targets. Centralising the scope here keeps every
caller honest about which files participate in the wikilink graph.

Wikilink scope (per design doc §5.1):
    * ``papers/<id>/notes.md``                       — per-paper notes

Anything outside this location is the user's own thing and is left
untouched by both rename and rm.

Wikilink syntax (M8.4 extends the original one-form syntax):
    * ``[[paper-id]]``           — same-vault reference (legacy form)
    * ``[[vault-name:paper-id]]``— cross-vault reference, where
      ``vault-name`` is the handle registered in
      ``~/.config/litman/vaults.yaml``.

The same-vault form is preserved exactly as before, so existing notes
and tests stay valid. The cross-vault form lets agents and humans
write references to papers living in linked fork vaults without
having to first copy them across.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


def enumerate_markdown_files(vault: Path) -> Iterable[Path]:
    """Yield .md files in the wikilink scope (see module docstring)."""
    papers_dir = vault / "papers"
    if papers_dir.is_dir():
        for child in papers_dir.iterdir():
            if child.is_dir():
                note = child / "notes.md"
                if note.is_file():
                    yield note


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
