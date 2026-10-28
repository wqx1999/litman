"""RIS exporter — the format EndNote, Zotero and Mendeley import natively.

Pure functions — no filesystem I/O — that turn metadata.yaml dicts into
a .ris string. The CLI command in ``litman.commands.export`` owns path
resolution, sentinel handling and writing; this module owns formatting.

Why RIS next to bibtex: EndNote's BibTeX import is not dependable (its
filter is not installed by default, and when it is, fields go missing),
while the same records as RIS import whole. Design choices:

- One ``TAG  - value`` line per field (two spaces, hyphen, space), each
  record closed by ``ER  - ``. Lines end in CRLF on every platform.
- ``TY`` is driven by ``venue-type``, like the bibtex entry type;
  missing or unknown falls back to ``GEN`` (generic).
- ``AU`` repeats once per author, in order, as stored (``Family,
  Given``); a family-only name loses its dangling comma.
- The journal (or the proceedings / book title) goes in ``T2``, the
  secondary title, as Zotero writes it. Page ranges ``45-67`` split into
  ``SP`` / ``EP``.
- ``UR`` points at the DOI when there is one, else at the arXiv abstract
  page; an arXiv id also goes in ``N1`` as ``arXiv:<id>``.
- ``LB`` (label) carries the paper id — the bibtex cite key — so a
  reference in EndNote can be matched back to ``\\cite{<paper-id>}``.
- Values are plain text: no escaping, no braces; a line break inside a
  value would end the field early, so whitespace runs collapse to one
  space.
- RIS has no comment syntax. The sentinel is a plain line before the
  first ``TY``; importers skip everything before a record starts.
"""

from __future__ import annotations

import re
from typing import Any

from litman.exporters.authors import clean_author

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ris_type_for(venue_type: str | None) -> str:
    """Map a CrossRef-style ``venue-type`` to a RIS reference type.

    Unknown or missing values fall back to ``"GEN"`` (generic).
    """
    return _VENUE_TYPE_TO_RIS.get((venue_type or "").strip(), "GEN")


def emit_entry(meta: dict[str, Any]) -> str:
    """Render one metadata.yaml dict as a RIS record (CRLF line ends).

    Empty / missing fields are dropped, as in the bibtex exporter.

    Raises:
        ValueError: ``meta['id']`` missing — there is no label to tie the
            record to its paper.
    """
    paper_id = meta.get("id")
    if not paper_id:
        raise ValueError(
            "Cannot render RIS record: metadata is missing 'id'."
        )

    venue_type = (meta.get("venue-type") or "").strip()
    fields: list[tuple[str, str]] = [("TY", ris_type_for(venue_type))]

    _add(fields, "TI", meta.get("title"))
    for author in meta.get("authors") or []:
        _add(fields, "AU", clean_author(str(author)))
    _add(fields, "PY", meta.get("year"))

    # --- secondary title: the journal, else the proceedings / book ---
    _add(fields, "T2", meta.get("journal") or meta.get("booktitle"))

    _add(fields, "VL", meta.get("volume"))
    _add(fields, "IS", meta.get("issue"))

    pages = _clean(meta.get("pages"))
    m = _PAGE_RANGE_RE.match(pages)
    if m:
        fields.append(("SP", m.group(1)))
        fields.append(("EP", m.group(2)))
    else:
        _add(fields, "SP", pages)

    _add(fields, "PB", meta.get("publisher"))

    # --- locators: doi first, arXiv as the fallback link ---
    doi = _clean(meta.get("doi"))
    arxiv_id = _clean(meta.get("arxiv-id"))
    _add(fields, "DO", doi)
    if doi:
        fields.append(("UR", f"https://doi.org/{doi}"))
    elif arxiv_id:
        fields.append(("UR", f"https://arxiv.org/abs/{arxiv_id}"))
    if arxiv_id:
        fields.append(("N1", f"arXiv:{arxiv_id}"))

    fields.append(("LB", str(paper_id)))
    lines = [f"{tag}  - {value}" for tag, value in fields]
    lines.append("ER  - ")
    return _EOL.join(lines)


def emit_ris(entries: list[dict[str, Any]], sentinel: str) -> str:
    """Render a list of metadata dicts as a full .ris file string.

    The sentinel line comes first; records follow, separated by one blank
    line, with a trailing line end. Order is preserved. A paper with no
    ``id`` is skipped, as in ``emit_bib``: the export command counts and
    reports the drop.
    """
    body = (_EOL * 2).join(emit_entry(m) for m in entries if m.get("id"))
    if body:
        return f"{sentinel}{_EOL}{_EOL}{body}{_EOL}"
    return f"{sentinel}{_EOL}"


def has_sentinel(text: str) -> bool:
    """Return True if ``text``'s first non-empty line is a litman sentinel.

    A byte-order mark before it is ignored: the export writes one.
    """
    for line in text.lstrip("﻿").splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        return stripped.startswith(SENTINEL_PREFIX)
    return False


# Shared with the export command, which builds the full sentinel line.
SENTINEL_PREFIX = "Generated by litman"

# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_EOL = "\r\n"

_VENUE_TYPE_TO_RIS: dict[str, str] = {
    "journal-article": "JOUR",
    "proceedings-article": "CPAPER",
    "posted-content": "UNPB",
    "preprint": "UNPB",
    "book": "BOOK",
    "book-chapter": "CHAP",
    "dissertation": "THES",
    "report": "RPRT",
    "patent": "PAT",
}

# Same shape the bibtex exporter rewrites to "45--67".
_PAGE_RANGE_RE = re.compile(r"^([A-Za-z0-9.]+)-([A-Za-z0-9.]+)$")


def _clean(value: object) -> str:
    """Stringify ``value`` and collapse every whitespace run to one space."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _add(fields: list[tuple[str, str]], tag: str, value: object) -> None:
    """Append ``(tag, value)`` when the cleaned value is non-empty."""
    text = _clean(value)
    if text:
        fields.append((tag, text))
