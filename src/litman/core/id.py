"""Paper id derivation: ``<year>_<FirstAuthorFamily>_<Keyword>``.

Ids are stable, filesystem-safe handles for papers. They participate in
folder names, ``[[id]]`` wiki-links, and project-level symlinks, so once
chosen they should not change. ``lit rename`` (M2) is the only safe way
to update them.

This module exposes two pure helpers:

- ``derive_keyword(title)``: pick a short, identifying word from the title.
- ``derive_id(year, family, title)``: assemble the canonical id.

Both raise ``IDError`` on inputs that cannot yield a valid id.
"""

from __future__ import annotations

import re

from litman.exceptions import IDError

# Tokens to drop when picking the keyword. Lowercased before comparison.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the",
        "of", "for", "with", "and", "or", "in", "on", "to", "from",
        "is", "are", "by", "as", "via", "using", "towards", "toward",
    }
)

_KEYWORD_MAX_LEN = 30


def _slug(text: str) -> str:
    """Strip everything except ASCII alphanumerics and hyphens."""
    return re.sub(r"[^A-Za-z0-9-]+", "", text)


def derive_keyword(title: str) -> str:
    """Pick a short, identifying keyword from a paper title.

    Heuristic:
        1. Take the part before the first colon ("Model: Description" pattern).
        2. Drop common stop words.
        3. Take the first remaining token, slug-cleaned, capitalized.
        4. Truncate to 30 characters.

    Returns ``"untitled"`` if the title yields no usable token.
    """
    if not title:
        return "untitled"

    main = title.split(":", 1)[0].strip()
    significant = [t for t in main.split() if t.lower() not in _STOP_WORDS]
    if not significant:
        # All stop words — fall back to the raw first token.
        significant = main.split()
    if not significant:
        return "untitled"

    cleaned = _slug(significant[0])
    if not cleaned:
        return "untitled"

    cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned[:_KEYWORD_MAX_LEN]


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
