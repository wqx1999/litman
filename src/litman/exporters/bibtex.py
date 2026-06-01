"""Bibtex exporter (M12.1).

Pure functions — no filesystem I/O — that turn metadata.yaml dicts into
a .bib string. The CLI command in ``litman.commands.export`` owns path
resolution, sentinel handling and atomic writing; this module owns
formatting only.

Design choices fixed by the M12 spec:

- ``cite key = paper id``. Paper ids are already filesystem-safe and
  contain only ``[A-Za-z0-9._-]``, which bibtex and biblatex both
  accept as identifiers, so no escaping is needed.
- ``entry type`` is driven by ``venue-type``, NOT by the editorial
  ``type`` field. Missing or unknown ``venue-type`` falls back to
  ``@misc`` (the most permissive entry type).
- UTF-8 output. We do NOT translate accents into LaTeX ``\\"u``-style
  escapes — biblatex + biber (TeX Live 2019+) handle unicode natively.
- The 8 characters that have syntactic meaning inside a bibtex value
  (``{ } \\ & % $ # _``) are escaped.
- Title is wrapped in an extra pair of braces so biblatex's default
  ``casechange=lowercase`` doesn't mangle proper nouns.
- Empty fields are dropped rather than emitted as ``key = {}`` — the
  schema-less invariant means an absent field is genuinely "not
  applicable", not "empty value".
- Page ranges arrive as ``"45-67"`` (one hyphen, the CrossRef raw
  form). Bibtex convention is ``"45--67"`` (two hyphens) so the
  exporter rewrites a single inner hyphen between page numbers.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def entry_type_for(venue_type: str | None) -> str:
    """Map a CrossRef-style ``venue-type`` to a bibtex entry type.

    Unknown or missing values fall back to ``"misc"`` — the most
    permissive entry type, which accepts any combination of fields.
    """
    return _VENUE_TYPE_TO_ENTRY.get((venue_type or "").strip(), "misc")


def escape_bibtex(text: str) -> str:
    """Escape the 8 bibtex-special characters in a field value.

    Handles ``\\``, ``{``, ``}``, ``&``, ``%``, ``$``, ``#``, ``_``.
    Backslash is mapped through a private placeholder so its
    replacement (``\\textbackslash{}``) does not get re-escaped by the
    subsequent brace pass; the placeholder is unmapped at the end.
    """
    if not text:
        return ""
    placeholder = "\x00BS\x00"  # null bytes don't occur in real metadata
    text = text.replace("\\", placeholder)
    for ch in ("{", "}", "&", "%", "$", "#", "_"):
        text = text.replace(ch, "\\" + ch)
    return text.replace(placeholder, "\\textbackslash{}")


def emit_entry(meta: dict[str, Any]) -> str:
    """Render one metadata.yaml dict as a bibtex entry string.

    Field selection matches the M12.0 metadata expansion:

    - title, author, year (always emitted when present)
    - journal, booktitle (only the populated one)
    - volume, issue (renamed to ``number`` in bibtex), pages
    - publisher
    - doi, url (url derived from arxiv-id when no doi)

    Empty / missing fields are dropped, so a sparse preprint produces
    a minimal but valid ``@misc{...}`` block rather than emitting
    ``volume = {}`` etc.

    Raises:
        ValueError: ``meta['id']`` missing — there is no cite key to
            anchor the entry on.
    """
    cite_key = meta.get("id")
    if not cite_key:
        raise ValueError(
            "Cannot render bibtex entry: metadata is missing 'id' (cite key)."
        )

    entry_type = entry_type_for(meta.get("venue-type"))
    fields: list[tuple[str, str]] = []

    # --- title (always wrapped in extra braces to defeat lowercase) ---
    title = (meta.get("title") or "").strip()
    if title:
        fields.append(("title", "{" + escape_bibtex(title) + "}"))

    # --- author (bibtex uses ' and ' as separator) ---
    authors = meta.get("authors") or []
    if authors:
        rendered = " and ".join(escape_bibtex(a) for a in authors)
        fields.append(("author", rendered))

    # --- year ---
    year = meta.get("year")
    if year not in (None, ""):
        fields.append(("year", str(year)))

    # --- venue (mutually exclusive: journal or booktitle) ---
    journal = (meta.get("journal") or "").strip()
    booktitle = (meta.get("booktitle") or "").strip()
    if journal:
        fields.append(("journal", escape_bibtex(journal)))
    if booktitle:
        fields.append(("booktitle", escape_bibtex(booktitle)))

    # --- volume / number / pages / publisher ---
    volume = (meta.get("volume") or "").strip()
    if volume:
        fields.append(("volume", escape_bibtex(volume)))

    issue = (meta.get("issue") or "").strip()
    if issue:
        # Bibtex name for CrossRef's "issue" is "number".
        fields.append(("number", escape_bibtex(issue)))

    pages = (meta.get("pages") or "").strip()
    if pages:
        fields.append(("pages", _normalize_pages(pages)))

    publisher = (meta.get("publisher") or "").strip()
    if publisher:
        fields.append(("publisher", escape_bibtex(publisher)))

    # --- locators: doi + arXiv eprint/url ---
    # A DOI is the canonical locator when present. For an arXiv preprint
    # (arxiv-id, often no DOI) the entry must still carry a locator or it
    # exports as an unresolvable @misc (review F1): emit the biblatex-native
    # eprint/archivePrefix pair, plus a plain abs URL when there's no DOI so
    # even bibtex styles that ignore eprint still have a clickable link.
    doi = (meta.get("doi") or "").strip()
    if doi:
        fields.append(("doi", escape_bibtex(doi)))

    arxiv_id = (meta.get("arxiv-id") or "").strip()
    if arxiv_id:
        fields.append(("eprint", escape_bibtex(arxiv_id)))
        fields.append(("archivePrefix", "arXiv"))
        if not doi:
            fields.append(
                ("url", f"https://arxiv.org/abs/{escape_bibtex(arxiv_id)}")
            )

    return _format_entry(entry_type, cite_key, fields)


def emit_bib(entries: list[dict[str, Any]], sentinel: str) -> str:
    """Render a list of metadata dicts as a full .bib file string.

    The sentinel comment line is the first line; entries follow,
    separated by one blank line. A trailing newline keeps POSIX tools
    happy.

    The order of entries is preserved — callers (the export command)
    decide the sort order; the exporter does not impose one.

    An entry with no ``id`` (cite key) is skipped rather than aborting the
    whole file (review F2): one hand-broken / id-less paper must not make
    ``lit export`` all-or-nothing. The export command reports the count it
    actually wrote and warns about any skipped paper, so the drop is surfaced
    (it filters on the same ``id`` predicate).
    """
    body = "\n\n".join(emit_entry(m) for m in entries if m.get("id"))
    if body:
        return f"{sentinel}\n\n{body}\n"
    return f"{sentinel}\n"


def has_sentinel(text: str) -> bool:
    """Return True if ``text``'s first non-empty line is a litman sentinel.

    Used by the export command to decide whether a target .bib file is
    safe to overwrite. Conservative: any line starting with
    ``% Generated by litman`` (after stripping leading whitespace and
    empty lines) counts as litman-owned.
    """
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        return stripped.startswith(_SENTINEL_PREFIX)
    return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


# Sentinel marker prefix. The full sentinel line is built by the CLI
# command (which knows the version + timestamp). The prefix is shared
# here so ``has_sentinel`` can recognise a previously-written file.
_SENTINEL_PREFIX = "% Generated by litman"


# CrossRef type -> bibtex entry type (the venue-type field on the
# paper drives this; bibtex's editorial-equivalent `type` field of the
# paper is ignored on purpose). Spec: M12.1 entry-type table.
_VENUE_TYPE_TO_ENTRY: dict[str, str] = {
    "journal-article": "article",
    "proceedings-article": "inproceedings",
    "posted-content": "misc",
    "preprint": "misc",
    "book": "book",
    "book-chapter": "incollection",
    "dissertation": "phdthesis",
    "report": "techreport",
}


# Matches a page range like "45-67" or "1234-1250" (one hyphen between
# two page tokens). Used to rewrite to bibtex's "45--67" convention.
# Tokens are kept generous (alnum + dots) to cover supplementary-page
# forms like "S12-S14" or "e1234-e1240".
_PAGE_RANGE_RE = re.compile(r"^([A-Za-z0-9.]+)-([A-Za-z0-9.]+)$")


def _normalize_pages(pages: str) -> str:
    """Convert ``"45-67"`` -> ``"45--67"``; pass other shapes through.

    A page value that does not match the strict single-hyphen pattern
    (e.g. ``"e12345"``, ``"45, 67"``, ``"S12-S14, S20"``) is returned
    escaped but otherwise unchanged — the user can fix it via
    ``lit modify`` if needed.
    """
    m = _PAGE_RANGE_RE.match(pages)
    if m:
        return f"{m.group(1)}--{m.group(2)}"
    return escape_bibtex(pages)


def _format_entry(entry_type: str, cite_key: str, fields: list[tuple[str, str]]) -> str:
    """Render ``@type{cite_key, key = {value}, ...}``.

    Field lines are indented 2 spaces and aligned with a trailing comma
    after every value (including the last — common convention; eases
    diffs). Empty ``fields`` lists still produce a syntactically valid
    entry (``@misc{key,\\n}``), which biber tolerates.
    """
    lines = [f"@{entry_type}{{{cite_key},"]
    for key, value in fields:
        lines.append(f"  {key} = {{{value}}},")
    lines.append("}")
    return "\n".join(lines)
