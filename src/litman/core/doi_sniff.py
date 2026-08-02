"""Best-effort DOI sniffing from a PDF's embedded text layer.

Backs the webUI drag-in ingest (task-gui-doi-add): a dropped PDF gets its
first pages' text pulled through :mod:`litman.core.pdf_text` and scanned for
DOI-shaped strings, so the confirm dialog can pre-fill the DOI field. This is
a *hint*, never an authority — the user confirms or corrects the DOI, and the
canonical metadata always comes from CrossRef afterwards.

Recall-only contract (same spirit as ``core.code_scan``): a scanned /
image-only PDF, an unparseable file, or a DOI-free paper all yield ``[]``,
and the dialog degrades to an empty DOI field. Nothing here raises.
"""

from __future__ import annotations

import re
from pathlib import Path

from litman.core.pdf_text import PdfTextError, extract_pdf_text

# DOI shape per the Crossref guidance: "10.<4-9 digit registrant>/<suffix>".
# The suffix charset is deliberately greedy (publishers put nearly anything
# there); trailing sentence punctuation is stripped afterwards instead of
# excluded here, so "doi:10.1021/ja00006a076." still yields the full DOI.
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+")

# Characters that end a DOI in running text but are (almost) never the last
# character of a real DOI: sentence punctuation and closing brackets.
_TRAILING_JUNK = ".,;:)]}»"

# Page 1 carries the DOI for nearly every modern article layout; page 2
# covers the "title page is a cover sheet" case. Reading further mostly
# harvests *references* — other papers' DOIs — which would poison the
# candidate list, so stop at 2.
_SNIFF_PAGES = [1, 2]

_MAX_CANDIDATES = 3


def _dois_from_texts(pages: list[str]) -> list[str]:
    """Pure candidate extraction over page texts (unit-testable core).

    First-seen order (the article's own DOI almost always appears before any
    cited one), case-insensitive dedup (DOIs are case-insensitive by spec),
    trailing sentence punctuation stripped, capped at ``_MAX_CANDIDATES``.
    """
    out: list[str] = []
    seen: set[str] = set()
    for text in pages:
        for match in _DOI_RE.findall(text):
            doi = match.rstrip(_TRAILING_JUNK)
            # A DOI must keep a suffix after the slash once junk is stripped.
            if not doi.partition("/")[2]:
                continue
            key = doi.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(doi)
            if len(out) >= _MAX_CANDIDATES:
                return out
    return out


def sniff_dois(pdf_path: Path) -> list[str]:
    """Return up to 3 DOI candidates from the PDF's first pages, best first.

    Returns ``[]`` on any extraction failure (scanned / image-only /
    unparseable PDF) or when no DOI-shaped string is present.
    """
    try:
        pages = extract_pdf_text(pdf_path, pages=list(_SNIFF_PAGES))
    except PdfTextError:
        return []
    return _dois_from_texts(pages)
