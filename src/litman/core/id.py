"""Paper id derivation: ``<year>_<FirstAuthorFamily>_<Keyword>``.

Ids are stable, filesystem-safe handles for papers. They participate in
folder names, ``[[id]]`` wiki-links, and project-level symlinks, so once
chosen they should not change. ``lit rename`` (M2) is the only safe way
to update them.

Keyword heuristic (M2.9):

Two paths produce the keyword segment of the id, depending on the title shape.

1. **Colon special-case** for "Prefix: Description" titles where the prefix
   slugs to fewer than ``_KEYWORD_COLON_PREFIX_MAX`` characters (typical model
   names like ``BERT:``, ``HELM-GPT:``, ``AlphaFold-3:``). The keyword becomes
   ``<Prefix>-<First>`` where ``<First>`` is the first post-colon word whose
   hyphen-stripped slug length is at least ``_KEYWORD_COLON_POST_MIN`` — this
   skips short Latin connectives (``De``, ``novo``, ``ex``, ``vivo``) without
   bloating the stop-word list. The prefix preserves internal hyphens; the
   post-colon word is hyphen-stripped so the join reads as a clean
   ``Model-Concept`` pair (``BERT-Pretraining``, not ``BERT-Pre-training``).

2. **Top-N path** for everything else (no colon, or prefix too long). The
   top ``_KEYWORD_TOP_N`` significant words (stop-words filtered) are
   hyphen-joined with internal hyphens preserved, then truncated at a hyphen
   boundary to ``_KEYWORD_MAX_LEN``.

Module API:

- ``derive_keyword(title)``: pick the identifying keyword segment.
- ``derive_keyword_alternatives(title, n)``: generate offset-shifted
  alternatives for the interactive id-collision fallback in ``lit add``.
- ``derive_id(year, family, title)``: assemble the canonical id.
- ``is_valid_id(id)``: filesystem-safety check used by ``lit add --id``
  override validation and by id-lookup helpers.

All keyword helpers raise ``IDError`` on inputs that cannot yield a valid id.
"""

from __future__ import annotations

import re

from litman.exceptions import IDError

# Tokens to drop when picking significant words. Lowercased before comparison.
# Kept small on purpose — domain-specific short words (e.g. "de", "ex", "in
# silico") are NOT stop words because doing so would distort other domains'
# titles. They are filtered separately, by length threshold, in the
# colon-special-case path only.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the",
        "of", "for", "with", "and", "or", "in", "on", "to", "from",
        "is", "are", "by", "as", "via", "using", "towards", "toward",
    }
)

_KEYWORD_TOP_N = 3
_KEYWORD_MAX_LEN = 40
_KEYWORD_COLON_PREFIX_MAX = 12  # slug(prefix) length cutoff for colon special-case
_KEYWORD_COLON_POST_MIN = 5     # min hyphen-stripped slug length for post-colon first word

# Valid paper id: starts with [A-Za-z0-9_-], then any of [A-Za-z0-9._-].
# Disallows leading dot (no hidden files), spaces, slashes, ".." anywhere.
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


def is_valid_id(paper_id: str) -> bool:
    """Filesystem-safe + shape-correct check for a paper id.

    Used by both ``lit add`` (validating ``--id`` overrides) and
    ``find_paper`` (validating user-supplied lookups). Prevents path
    traversal (``..``) and stray special chars from creating files outside
    the vault.
    """
    if not paper_id:
        return False
    if ".." in paper_id or "/" in paper_id or "\\" in paper_id:
        return False
    return bool(_VALID_ID_RE.match(paper_id))


def find_case_fold_collision(
    existing_ids: list[str], new_id: str
) -> str | None:
    """Return an existing id that case-folds equal to ``new_id``, or ``None``.

    Defends against the gotcha that Linux filesystems are case-sensitive
    while Windows / default macOS are case-insensitive (ADR-005). Two ids
    differing only in case (``2023_Pandi_X`` vs ``2023_pandi_X``) coexist
    on Linux but collide on Windows / macOS — moving the vault between
    machines silently loses one paper.

    Exact matches (``new_id`` already in ``existing_ids`` byte-for-byte)
    are NOT reported here — they belong to the normal collision path
    (``lit add`` already prompts for an alternative). This helper
    specifically surfaces the *case-only* clash.

    Args:
        existing_ids: All paper / vault names currently present.
        new_id: The candidate name being added.

    Returns:
        The first existing id that ``casefold()``-matches ``new_id`` and
        is not byte-identical to it, or ``None`` for no clash.
    """
    target = new_id.casefold()
    for existing in existing_ids:
        if existing == new_id:
            continue
        if existing.casefold() == target:
            return existing
    return None


def _slug(text: str) -> str:
    """Strip everything except ASCII alphanumerics and hyphens."""
    return re.sub(r"[^A-Za-z0-9-]+", "", text)


def _slug_no_hyphen(text: str) -> str:
    """Strip everything except ASCII alphanumerics (hyphens removed too).

    Used for the post-colon word in the colon special-case path so that
    ``Pre-training`` reads as ``Pretraining`` in the assembled keyword.
    """
    return re.sub(r"[^A-Za-z0-9]+", "", text)


def _capitalize_first(s: str) -> str:
    """Uppercase the first character; leave the rest alone."""
    if not s:
        return s
    return s[0].upper() + s[1:]


def _truncate_at_hyphen(s: str, max_len: int) -> str:
    """Truncate ``s`` to at most ``max_len`` chars, preferring a hyphen boundary.

    If the truncation falls inside a word (no hyphen in the truncated prefix),
    cut hard at ``max_len`` rather than dropping further — better to keep more
    of the keyword than to over-shorten.
    """
    if len(s) <= max_len:
        return s
    truncated = s[:max_len]
    last_hyphen = truncated.rfind("-")
    if last_hyphen > 0:
        return truncated[:last_hyphen]
    return truncated


def _significant_tokens(text: str) -> list[str]:
    """Tokenize on whitespace, drop stop words, slug each (preserves hyphens).

    Returns the list of cleaned tokens in original order. Empty / non-ASCII /
    pure-punctuation tokens are dropped.
    """
    out: list[str] = []
    for token in text.split():
        if token.lower() in _STOP_WORDS:
            continue
        slug = _slug(token)
        if slug:
            out.append(slug)
    return out


def _top_n_keyword(title: str) -> str:
    """Build a keyword from the top-N significant words of the full title."""
    words = _significant_tokens(title)
    if not words:
        # All stop words or non-ASCII — fall back to raw first whitespace token.
        raw_tokens = title.split()
        if not raw_tokens:
            return "untitled"
        slug = _slug(raw_tokens[0])
        if not slug:
            return "untitled"
        return _truncate_at_hyphen(_capitalize_first(slug), _KEYWORD_MAX_LEN)
    top = words[:_KEYWORD_TOP_N]
    top[0] = _capitalize_first(top[0])
    return _truncate_at_hyphen("-".join(top), _KEYWORD_MAX_LEN)


def _colon_special_case(title: str) -> str | None:
    """Try the ``<Prefix>-<First>`` colon path; return ``None`` if not applicable.

    Applicable iff the title has a colon AND the slug of the pre-colon portion
    is non-empty AND strictly shorter than ``_KEYWORD_COLON_PREFIX_MAX``.

    Returns ``None`` if the colon path is bypassed OR the post-colon portion
    has no word with hyphen-stripped slug length >= ``_KEYWORD_COLON_POST_MIN``.
    The caller then falls back to the top-N path.
    """
    if ":" not in title:
        return None
    pre, post = title.split(":", 1)
    pre_slug = _slug(pre.strip())
    if not pre_slug or len(pre_slug) >= _KEYWORD_COLON_PREFIX_MAX:
        return None

    for token in post.split():
        if token.lower() in _STOP_WORDS:
            continue
        no_hyphen = _slug_no_hyphen(token)
        if len(no_hyphen) >= _KEYWORD_COLON_POST_MIN:
            combined = (
                f"{_capitalize_first(pre_slug)}-{_capitalize_first(no_hyphen)}"
            )
            return _truncate_at_hyphen(combined, _KEYWORD_MAX_LEN)
    return None


def derive_keyword(title: str) -> str:
    """Pick a short, identifying keyword from a paper title.

    See module docstring for the two-path heuristic. Returns ``"untitled"``
    if the title yields no usable token.
    """
    if not title or not title.strip():
        return "untitled"

    colon = _colon_special_case(title)
    if colon is not None:
        return colon

    return _top_n_keyword(title)


def derive_keyword_alternatives(title: str, n: int = 3) -> list[str]:
    """Generate up to ``n`` alternative keywords by sliding the word window.

    Used by ``lit add`` interactive collision fallback to offer candidates the
    user can pick from. Skips the primary ``derive_keyword(title)`` result and
    deduplicates within the returned list. Always uses the top-N path (ignores
    the colon special-case) because the colon prefix is already locked into
    the collided primary candidate.

    Args:
        title: Paper title.
        n: Maximum alternatives to return.

    Returns:
        List of distinct keyword strings, possibly empty if the title is too
        short to yield alternatives.
    """
    if not title or not title.strip() or n <= 0:
        return []
    primary = derive_keyword(title)
    tokens = _significant_tokens(title)
    if len(tokens) <= 1:
        return []

    seen: set[str] = {primary, "untitled"}
    out: list[str] = []
    for offset in range(1, len(tokens)):
        window = tokens[offset:offset + _KEYWORD_TOP_N]
        window[0] = _capitalize_first(window[0])
        candidate = _truncate_at_hyphen("-".join(window), _KEYWORD_MAX_LEN)
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
        if len(out) >= n:
            break
    return out


def derive_id(year: int | None, first_author_family: str, title: str) -> str:
    """Build the canonical id ``<year>_<Family>_<Keyword>``.

    Raises:
        IDError: ``year`` is missing, ``first_author_family`` slugs to empty,
            or ``title`` is empty (keyword cannot be derived).
    """
    if year is None:
        raise IDError("Cannot derive id without a publication year.")
    if not isinstance(year, int):
        raise IDError(f"Year must be an integer, got {year!r}.")

    family_slug = _slug(first_author_family)
    if not family_slug:
        raise IDError(
            f"First-author family name normalizes to empty: "
            f"{first_author_family!r}."
        )
    family = family_slug[0].upper() + family_slug[1:]

    keyword = derive_keyword(title)
    if keyword == "untitled":
        raise IDError(f"Cannot derive keyword from title: {title!r}.")

    return f"{year}_{family}_{keyword}"
