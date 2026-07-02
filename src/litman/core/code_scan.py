"""Deterministic full-text code-URL recall scanner (M20).

A pure **recall** primitive: given a PDF path, scan every page's extracted
text for code-repository URLs and return the candidates as structured dicts.

Why this exists (background, see M20 spec): the lit-reading ingest flow needs
code-repository URLs pulled out of a paper's PDF to feed downstream
clone-link integrity (invariant #12). A first-page-only scan systematically
misses an entire class of journals (Nature / Science / Cell) whose code /
data availability statements are mandated at the *end* of the body, never on
page 1. M20 widens the scan to the full text, deterministically, with zero
LLM involvement.

Design contract:

- **Host list is welded into the package** (``_CODE_HOST_RE``). Callers do
  NOT supply a pattern. Recall quality must not depend on an LLM remembering
  to spell a regex correctly (invariant #5).
- **Never throws.** Any pypdf failure (corrupt / encrypted / non-PDF bytes,
  per-page extraction error) degrades to an empty result. The caller's
  contract is "this function returns a list, always".
- This module is pure recall. Deliverable-vs-dependency precision is a
  downstream agent-side concern; the scanner only reports what it found and
  how often (``count`` is the deliverable strength signal downstream sort
  uses).
"""

from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

# Welded-in deterministic host spec. NOT caller-supplied (invariant #5):
# recall quality cannot ride on an LLM remembering to spell this regex.
_CODE_HOST_RE = re.compile(
    r"(github\.com|gitlab\.com|bitbucket\.org|"
    r"huggingface\.co|zenodo\.org|osf\.io|codeocean)",
    re.IGNORECASE,
)

# A URL token on a matching line: an http(s) scheme followed by any run of
# non-whitespace, non-paren characters. Trailing sentence punctuation is
# trimmed afterwards so "...repo (https://github.com/foo/bar)." normalizes
# cleanly.
_URL_TOKEN_RE = re.compile(r"https?://[^\s()<>\"']+", re.IGNORECASE)

# Punctuation that commonly trails a URL inside running prose but is not part
# of the address.
_URL_TRAILING = ".,;:!?'\")]}>"


def _normalize_url(url: str) -> str:
    """Canonicalize a URL for dedup.

    Lowercase scheme + host, strip a single trailing slash and trailing
    sentence punctuation. The path is case-preserved (GitHub paths are
    case-sensitive) so two genuinely different repos never collapse.
    """
    url = url.strip().rstrip(_URL_TRAILING)
    match = re.match(r"(https?://)([^/]+)(.*)", url, re.IGNORECASE)
    if match is None:
        return url.rstrip("/")
    scheme, host, rest = match.groups()
    return f"{scheme.lower()}{host.lower()}{rest}".rstrip("/")


def scan_code_urls(pdf_path: Path) -> list[dict]:
    """Scan a PDF's full text for code-repository URLs.

    Opens ``pdf_path`` with :class:`pypdf.PdfReader`, iterates pages calling
    ``extract_text()``, and for every line matching the welded-in host regex
    extracts the URL token(s) on that line.

    Returns a list of ``{"url": str, "page": int, "count": int}`` dicts where
    ``page`` is the 1-based page of first occurrence and ``count`` is the
    total number of full-text hits for that normalized URL. The list is
    sorted by ``count`` descending (high-frequency mentions first — the
    deliverable strength signal a downstream consumer ranks on).

    Never raises: any pypdf exception (corrupt / encrypted / non-PDF input,
    per-page extraction failure) yields ``[]``.
    """
    try:
        reader = PdfReader(str(pdf_path))
        pages = reader.pages
    except Exception:
        return []

    # Preserve first-seen order for stable output among equal counts.
    order: list[str] = []
    first_page: dict[str, int] = {}
    counts: dict[str, int] = {}

    try:
        for page_index, page in enumerate(pages):
            page_number = page_index + 1
            try:
                text = page.extract_text() or ""
            except Exception:
                # A single unreadable page must not abort the whole scan, nor
                # leak an exception to the caller.
                continue

            for line in text.splitlines():
                if not _CODE_HOST_RE.search(line):
                    continue
                for raw_token in _URL_TOKEN_RE.findall(line):
                    if not _CODE_HOST_RE.search(raw_token):
                        continue
                    norm = _normalize_url(raw_token)
                    if not norm:
                        continue
                    if norm not in counts:
                        order.append(norm)
                        first_page[norm] = page_number
                        counts[norm] = 0
                    counts[norm] += 1
    except Exception:
        # Honor the documented "never raises" contract: a lazy pypdf failure
        # while iterating the page tree (resolution / decryption) degrades to
        # the partial result gathered so far rather than propagating.
        pass

    candidates = [
        {"url": url, "page": first_page[url], "count": counts[url]}
        for url in order
    ]
    # Stable sort: count descending; ties keep first-seen order.
    candidates.sort(key=lambda c: c["count"], reverse=True)
    return candidates
