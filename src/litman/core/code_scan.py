"""Deterministic full-text code-URL recall scanner (M20).

A pure **recall** primitive: given a PDF path, scan every page's extracted
text for code-repository URLs and return the candidates as structured dicts.
A URL the page layout broke across lines is joined back with the start of the
following line(s) of the same page before it is matched.

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
from typing import Any

from pypdf import PdfReader

# Welded-in deterministic host spec. NOT caller-supplied (invariant #5):
# recall quality cannot ride on an LLM remembering to spell this regex.
_CODE_HOST_RE = re.compile(
    r"(github\.com|gitlab\.com|bitbucket\.org|"
    r"huggingface\.co|zenodo\.org|osf\.io|codeocean)",
    re.IGNORECASE,
)

# A URL token: an http(s) scheme followed by any run of non-whitespace,
# non-paren characters. The run may be empty so a bare "https://" left at the
# end of a line is still a token to join; a token that ends up without a code
# host is dropped by the host test. Trailing sentence punctuation is trimmed
# afterwards so "...repo (https://github.com/foo/bar)." normalizes cleanly.
_URL_TOKEN_RE = re.compile(r"https?://[^\s()<>\"']*", re.IGNORECASE)

# The leading run of URL characters on the line that continues a broken URL.
_URL_CONTINUATION_RE = re.compile(r"[^\s()<>\"']+")

# How many following lines a broken URL may be continued over.
_MAX_JOINED_LINES = 3

# An OSF project address is complete at one path segment (osf.io/<id>/);
# elsewhere a complete address is owner + repo.
_ONE_SEGMENT_HOST_RE = re.compile(r"https?://(?:www\.)?osf\.io/", re.IGNORECASE)

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


def _path_depth(token: str) -> int:
    """Count the non-empty path segments after the host of a URL token."""
    rest = token.split("://", 1)[1]
    return sum(1 for segment in rest.split("/")[1:] if segment)


def _needs_join(token: str) -> bool:
    """Whether a URL token that ends its line looks cut off by the line break.

    True for a bare scheme or a ``/`` short of host + owner + repo
    (``https://``, ``github.com/``, ``github.com/owner/``), and for a name split
    at ``-`` / ``_``. A complete address (``owner/repo/``, or ``osf.io/<id>/``)
    does not join, so the next line's prose never lands on a full address; a
    trailing ``.`` never joins because it is almost always the sentence's full
    stop.
    """
    if token.endswith(("-", "_")):
        return True
    if not token.endswith("/"):
        return False
    complete_depth = 1 if _ONE_SEGMENT_HOST_RE.match(token) else 2
    return _path_depth(token) < complete_depth


def _join_broken_url(token: str, next_lines: list[str]) -> str:
    """Continue a URL token that ends its line with the following lines.

    Each step appends the leading run of URL characters of the next line,
    unless that run is a URL of its own; it goes on to a further line only
    while that run was the whole line and the result still looks cut off.
    ``next_lines`` holds at most ``_MAX_JOINED_LINES`` following lines of the
    same page, so a URL is never joined across a page break.
    """
    for next_line in next_lines:
        if not _needs_join(token):
            break
        stripped = next_line.strip()
        match = _URL_CONTINUATION_RE.match(stripped)
        if match is None or _URL_TOKEN_RE.match(match.group(0)):
            break
        token += match.group(0)
        if match.end() < len(stripped):
            break
    return token


def scan_code_urls(pdf_path: Path) -> list[dict[str, Any]]:
    """Scan a PDF's full text for code-repository URLs.

    Opens ``pdf_path`` with :class:`pypdf.PdfReader`, iterates pages calling
    ``extract_text()``, extracts the URL token(s) on every line, joins a token
    that ends its line looking cut off with the next line(s) of the same page,
    and keeps the tokens matching the welded-in host regex.

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

            lines = text.splitlines()
            for line_index, line in enumerate(lines):
                # Prefilter on the scheme, not the host: a line that ends in
                # a bare "https://" carries no host.
                if "http" not in line.lower():
                    continue
                line_end = len(line.rstrip())
                for match in _URL_TOKEN_RE.finditer(line):
                    raw_token = match.group(0)
                    if match.end() == line_end:
                        last = line_index + 1 + _MAX_JOINED_LINES
                        following = lines[line_index + 1 : last]
                        raw_token = _join_broken_url(raw_token, following)
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

    candidates: list[dict[str, Any]] = [
        {"url": url, "page": first_page[url], "count": counts[url]}
        for url in order
    ]
    # Stable sort: count descending; ties keep first-seen order.
    candidates.sort(key=lambda c: c["count"], reverse=True)
    return candidates
