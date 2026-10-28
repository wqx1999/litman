"""Author-name cleanup shared by the exporters.

Pure, no I/O. The vault stores authors as ``"Family, Given"``; a record
that only knows the family name can arrive as ``"Yang, "`` — family,
comma, nothing. Copied out verbatim that trailing comma breaks the
reader: bibtex reports ``Name 1 in "Yang," has a comma at the end`` and
a RIS ``AU`` line names an author with an empty given name. The
exporters write such a name as the bare family name instead; the
metadata itself is left as it is.
"""

from __future__ import annotations


def clean_author(name: str) -> str:
    """Return ``name`` with surrounding whitespace and a dangling comma dropped.

    ``"Yang, "`` -> ``"Yang"``; ``"Corso, Gabriele"`` passes through.
    """
    return name.strip().rstrip(",").strip()
