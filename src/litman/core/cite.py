"""ACS presentation-style citation formatting (compact, no authors).

Pure functions — no filesystem writes — that turn a metadata.yaml dict into a
short citation string of the form::

    J. Am. Chem. Soc. 2021, 143, 1234-1240.

i.e. ``<journal abbreviation> <year>, <volume>, <pages>.`` This is the compact
"presentation slide" form: no author list, no title. The CLI command in
``litman.commands.cite`` and the webUI ``/api/paper/{id}/cite`` endpoint both
call :func:`format_acs` — one formatting path, no second implementation
(invariant #16).

Journal abbreviation strategy (locked decision): a *shipped* lookup table, not
a user-curated one. The table is a vendored CC0 dataset
(``litman/data/journal_abbrev.csv``, merged from the JabRef abbrv.jabref.org
ACS + life-science lists). Lookups that miss fall back to the verbatim journal
name plus a warning, so the user is told the abbreviation is unverified rather
than handed a silently-wrong string. Single-word journal titles (Nature,
Science, Cell, Bioinformatics) are their own ISO4 abbreviation and never get
flagged.

The fields consumed (``journal`` / ``booktitle`` / ``volume`` / ``issue`` /
``pages`` / ``year`` / ``arxiv-id``) match the names CrossRef import writes and
the bibtex exporter reads — see ``litman.importers.crossref`` and
``litman.exporters.bibtex``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from typing import Any


@dataclass
class Citation:
    """A rendered citation plus any caveats the caller should surface.

    ``warnings`` is empty for a clean, fully-populated journal citation. Each
    entry is a human-readable note (unverified abbreviation, missing volume,
    preprint venue, ...) — the CLI prints them to stderr and the webUI shows
    them next to the copied text, but they are NEVER embedded in ``text`` so the
    citation stays paste-clean.
    """

    text: str
    warnings: list[str] = field(default_factory=list)


def _norm(name: str) -> str:
    """Normalize a journal name to a lookup key: lowercase, whitespace-collapsed,
    leading article stripped. Used both for table keys and for queries so e.g.
    ``"The  Journal Of ..."`` and ``"Journal of ..."`` resolve together."""
    s = " ".join(name.strip().lower().split())
    if s.startswith("the "):
        s = s[4:]
    return s


@lru_cache(maxsize=1)
def _abbrev_table() -> dict[str, str]:
    """Load the vendored full-name -> abbreviation map (parsed once, cached).

    The CSV ships inside the wheel; ``importlib.resources.files`` resolves it
    from either a source checkout or an installed package. ``#`` lines are
    provenance comments and skipped. First occurrence wins (the file is written
    ACS-first so chemistry abbreviations take precedence over any life-science
    duplicate), so the dict is built without overwriting existing keys.
    """
    text = (
        files("litman.data")
        .joinpath("journal_abbrev.csv")
        .read_text(encoding="utf-8")
    )
    table: dict[str, str] = {}
    for row in csv.reader(text.splitlines()):
        if len(row) < 2 or row[0].startswith("#"):
            continue
        full, abbrev = row[0].strip(), row[1].strip()
        if full and abbrev:
            table.setdefault(_norm(full), abbrev)
    return table


def abbreviate_journal(name: str) -> tuple[str, bool]:
    """Return ``(abbreviation, known)`` for a journal name.

    - exact (normalized) table hit -> ``(table value, True)``
    - single-word title not in the table -> ``(title, True)``: ISO4 does not
      abbreviate one-word journal names, so the name *is* its abbreviation.
    - multi-word title not in the table -> ``(title, False)``: genuinely
      unknown; the caller should flag it as unverified.
    """
    name = name.strip()
    if not name:
        return "", False
    hit = _abbrev_table().get(_norm(name))
    if hit:
        return hit, True
    if len(name.split()) == 1:
        return name, True
    return name, False


def format_acs(meta: dict[str, Any]) -> Citation:
    """Render ``meta`` as a compact ACS-style citation with warnings.

    Assembles ``<venue> <year>, <volume>, <pages>.`` from whatever fields are
    present, degrading gracefully:

    - a journal paper with all fields -> the full form;
    - a missing field -> dropped from the string and noted in ``warnings``;
    - a proceedings/book chapter (``booktitle`` but no ``journal``) -> the
      venue name verbatim, flagged (ACS journal style may not apply);
    - a preprint (``arxiv-id``, no journal) -> ``arXiv:<id>``, flagged;
    - nothing usable -> a placeholder string + warning.
    """
    warnings: list[str] = []

    journal = (meta.get("journal") or "").strip()
    booktitle = (meta.get("booktitle") or "").strip()
    arxiv_id = (meta.get("arxiv-id") or "").strip()
    year = meta.get("year")
    volume = (meta.get("volume") or "").strip()
    pages = (meta.get("pages") or "").strip()

    # --- venue label ---
    if journal:
        venue, known = abbreviate_journal(journal)
        if not known:
            warnings.append(
                f"journal abbreviation not in the shipped table — using "
                f"{journal!r} verbatim; verify against ISO4/CASSI before citing"
            )
    elif booktitle:
        venue = booktitle
        warnings.append(
            "non-journal venue (proceedings/book chapter) — ACS journal "
            "abbreviation style may not apply"
        )
    elif arxiv_id:
        venue = f"arXiv:{arxiv_id}"
        warnings.append("preprint — no journal; cited as an arXiv preprint")
    else:
        venue = ""
        warnings.append("no journal or venue in metadata — citation is incomplete")

    # --- missing-field notes (only meaningful for a journal article) ---
    if not year:
        warnings.append("no year in metadata")
    if journal and not volume:
        warnings.append("no volume in metadata")
    if journal and not pages:
        warnings.append("no pages in metadata")

    # --- assemble "<venue> <year>, <volume>, <pages>." ---
    text = venue
    if year:
        text = f"{text} {year}".strip()
    tail = ", ".join(part for part in (volume, pages) if part)
    if tail:
        text = f"{text}, {tail}" if text else tail
    text = text.strip()
    if text and not text.endswith("."):
        text += "."
    if not text:
        text = "(insufficient metadata for a citation)"

    return Citation(text=text, warnings=warnings)
