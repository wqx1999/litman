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

Both paths sit behind the ``_MIN_ASCII_RATIO`` gate. Ids are ASCII by
construction (ADR-005), and both paths tokenize on whitespace — so a script
that does not use spaces arrives as one token and slugs down to whatever Latin
fragment it happened to contain. ``关于化合物A的合成方法`` produced ``2018_Zhang_A``
this way, silently, and a wrong id is permanent in a way a wrong field is not.
Titles below the ratio are refused instead, and ``suggest_id`` offers the
candidate an explicit ``--id`` can start from.

Module API:

- ``derive_keyword(title)``: pick the identifying keyword segment.
- ``derive_keyword_alternatives(title, n)``: generate offset-shifted
  alternatives for the interactive id-collision fallback in ``lit add``.
- ``derive_id(year, family, title)``: assemble the canonical id.
- ``suggest_id(year, family, title)``: a candidate id for a title
  ``derive_id`` refuses — what the CLI error and the GUI's Paper ID field
  offer as a starting point.
- ``is_weak_keyword(keyword)``: shared with the health check's rule for
  keyword segments already sitting in a vault.
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

# Minimum share of a title's non-space characters that must survive slugging
# for the derived keyword to be about the title rather than about whatever
# Latin fragment happened to be embedded in it. English titles sit above 90%;
# a Chinese title with a compound label in it ("关于化合物A的合成方法") sits at 9%
# and a mixed one ("一种新型 PROTAC 分子的设计与合成") at 35%. Both are refused, on
# purpose: "sometimes a Chinese title works" is harder to live with than
# "a Chinese title always needs an id from you", and the id is permanent.
_MIN_ASCII_RATIO = 0.5

# A keyword segment this short or shorter identifies nothing.
_WEAK_KEYWORD_MAX_LEN = 2

# Valid paper id: starts with [A-Za-z0-9_-], then any of [A-Za-z0-9._-].
# Disallows leading dot (no hidden files), spaces, slashes, ".." anywhere —
# and a TRAILING dot: Windows silently strips trailing dots when creating a
# directory, so `papers/2024_Foo./` would land on disk as `papers/2024_Foo/`
# and the id-vs-dirname drift would be born broken (ADR-005).
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?<!\.)$")


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


def _ascii_ratio(title: str) -> float:
    """Share of the title's non-space characters that survive slugging.

    Whitespace is removed rather than counted so the measure does not depend
    on whether the script uses spaces between words — Chinese does not, which
    is the whole reason this measure exists.
    """
    dense = "".join(title.split())
    if not dense:
        return 0.0
    return len(_slug(dense)) / len(dense)


def _derive_keyword_unguarded(title: str) -> str:
    """The two-path heuristic itself, with no judgement about the result."""
    colon = _colon_special_case(title)
    if colon is not None:
        return colon
    return _top_n_keyword(title)


def is_weak_keyword(keyword: str) -> bool:
    """True when a keyword segment is too short to identify anything.

    ``2018_Zhang_A`` names a paper no better than ``2018_Zhang_`` does. Shared
    with ``core/checks.py`` so the ingest guard and the health check agree on
    what counts as a keyword that carries no information.
    """
    return len(keyword) <= _WEAK_KEYWORD_MAX_LEN


def derive_keyword(title: str) -> str:
    """Pick a short, identifying keyword from a paper title.

    See module docstring for the two-path heuristic. Returns ``"untitled"``
    when the title yields no usable token, and — the ``_MIN_ASCII_RATIO``
    gate — when it yields one only by accident.

    The gate exists because tokenizing on whitespace makes a space-less script
    one single token: ``关于化合物A的合成方法`` slugs down to ``A``, and an id of
    ``2018_Zhang_A`` used to be born silently. Refusing is strictly better than
    that. A wrong id is not a transient error — it is the folder name, the
    ``[[wiki-link]]`` target and the project symlink, so only ``lit rename``
    can undo it, and only if someone notices. The way past is an explicit id,
    which both ``lit add --id`` and the GUI's Paper ID field offer alongside
    ``suggest_id``'s candidate.
    """
    if not title or not title.strip():
        return "untitled"
    if _ascii_ratio(title) < _MIN_ASCII_RATIO:
        return "untitled"

    return _derive_keyword_unguarded(title)


def suggest_id(
    year: int | None, first_author_family: str, title: str
) -> str | None:
    """A ready-to-paste id for a title ``derive_id`` refuses, or ``None``.

    Deliberately bypasses the ``_MIN_ASCII_RATIO`` gate: a title like
    ``CRISPR-Cas9 基因编辑技术的研究进展`` is refused as a whole (43% Latin) while
    still carrying a perfectly good keyword, and making the user retype
    ``CRISPR-Cas9`` would be the gate charging rent. What it will not do is
    hand back a keyword that ``is_weak_keyword`` rejects — suggesting
    ``2018_Zhang_A`` is exactly the id the gate just refused to create.

    Returns ``None`` when there is nothing worth offering, which the callers
    render as an empty Paper ID field rather than a bad default.
    """
    if year is None or not isinstance(year, int):
        return None
    family = family_segment(first_author_family)
    if family is None:
        return None
    if not title or not title.strip():
        return None

    keyword = _derive_keyword_unguarded(title)
    if keyword == "untitled" or is_weak_keyword(keyword):
        return None

    return f"{year}_{family}_{keyword}"


def family_segment(first_author_family: str) -> str | None:
    """The ``<Family>`` segment of an id, or ``None`` if nothing ASCII survives.

    The middle third of an id, split out so the two callers that build one
    without going through :func:`derive_id` — ``suggest_id`` and the health
    check's rename hint — capitalise it the same way rather than each
    reimplementing "slug, then upper the first character".
    """
    slug = _slug(first_author_family)
    if not slug:
        return None
    return slug[0].upper() + slug[1:]


def first_author_family(authors: list[str]) -> str:
    """The family name out of the first ``"Family, Given"`` author string.

    Lives here rather than beside its callers because it is the front half of
    :func:`derive_id`'s contract: everything that needs to know which id a
    piece of metadata *would* produce — the ingest paths, the GUI preview, the
    health check's rename hint — has to slice the family name the same way, or
    they disagree about the id while all claiming to derive it.
    """
    if not authors:
        return ""
    return authors[0].split(",", 1)[0].strip()


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
        raise IDError(_no_keyword_message(year, family, title))

    return f"{year}_{family}_{keyword}"


def _no_keyword_message(year: int, family: str, title: str) -> str:
    """Explain a refused title and name the way past it.

    Two different failures land here — a title with no usable token at all,
    and one the ASCII gate refused — and they need different sentences: the
    first is "there is nothing here", the second is "there is something here
    but it cannot be a folder name". Both end on a command the reader can run,
    because the caller is as often an agent working through a batch as it is
    a person, and an error an agent cannot act on stalls the whole batch.
    """
    # First line stands alone: the GUI shows only this one, under a field the
    # title is already visible in, so quoting the title back would be noise
    # there while the CLI reader still needs it. The quote moves to line two.
    lines = ["This title cannot produce a paper id."]
    if _ascii_ratio(title) < _MIN_ASCII_RATIO:
        lines.append(
            f"The id is a folder name on Windows, macOS and Linux alike, so it "
            f"has to be ASCII — and {title!r} is mostly not, which leaves no "
            f"keyword that would actually name the paper."
        )
    else:
        lines.append(f"Title: {title!r}")
    suggestion = suggest_id(year, family, title)
    if suggestion is not None:
        lines.append(
            f"Pass --id {suggestion} to keep the Latin part of the title, or "
            f"--id {year}_{family}_<Keyword> with a keyword you pick."
        )
    else:
        lines.append(
            f"Pass --id {year}_{family}_<Keyword> with a keyword you pick — a "
            "transliteration and the paper's English title both work, and "
            "neither has to match the title you store."
        )
    return "\n".join(lines)
