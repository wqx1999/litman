"""Plain-text cleaning for metadata entering litman.

Crossref hands back a title or a journal name the way the publisher deposited
it, and some deposits are pretty-printed XML: line breaks and indentation in
the middle of the string, face markup (``<i>``, ``<sub>``, ``<scp>``) and
MathML around single words. Stored as it comes, that text reaches the paper id
(``<i>N</i>`` slugs to ``INi``), the notes.md heading (a line break splits it),
the browse list and every exported citation. An agent's extraction from a PDF,
or a value pasted into ``lit modify``, can carry the same debris.

:func:`clean_metadata_text` turns such a string into the line a reader sees on
the article page: entities decoded, markup dropped with its text kept
(``CO<sub>2</sub>`` → ``CO2``), every whitespace run one space. It runs where
metadata comes in — ``importers/crossref.py``, ``importers/llm.py``, the web
UI's id preview and ``lit modify`` — and :func:`dirty_text_fields` is how
``lit health-check`` finds papers stored before it did, and what its ``--fix``
writes.

Known limitations, each on the side of keeping text rather than guessing:

* Super- and subscripts keep only their text (``Ca<sup>2+</sup>`` → ``Ca2+``),
  and MathML only its text nodes.
* A line break between a word and a pretty-printed ``<sup>`` / ``<sub>``
  becomes a space (``Ag\\n <sup>+</sup>`` → ``Ag +``): whether the original had
  one is not recoverable from the deposit.
* Markup means the lowercase names below and any namespaced element. ``A<B>C``
  and ``p < 0.05`` stay as they are, and so would an unprefixed MathML child
  such as ``<mi>`` — Crossref always prefixes those (``mml:``).
* An entity needs its semicolon: ``R&D``, or ``&not`` inside a word, is text.
  Double escaping (``&amp;lt;i&amp;gt;``) is decoded until nothing is left to
  decode, so the markup it spells is dropped like any other.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

# Scalar fields that read as prose: what `lit modify --set` cleans and what
# `lit health-check` inspects. `authors` is the list counterpart.
TEXT_FIELDS: tuple[str, ...] = ("title", "journal", "booktitle", "publisher")

_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")

# Crossref's face markup, the inline HTML that turns up beside it, and any
# namespaced element (`mml:*` MathML, `jats:*`). Open, close or self-closing,
# with or without attributes. `<br>` is not in the list: it separates words,
# so it becomes a space before the rest is tokenized.
_MARKUP_NAMES = (
    "b", "i", "u", "ovl", "sup", "sub", "scp", "tt", "font",
    "em", "strong", "span", "sc", "small", "math",
)
_BR_RE = re.compile(r"<br(?:\s[^<>]*)?/?>")
_TAG = (
    r"</?(?:[a-z][a-z0-9-]*:[a-z][a-z0-9-]*|"
    + "|".join(_MARKUP_NAMES)
    + r")(?:\s[^<>]*)?/?>"
)
_TOKEN_RE = re.compile(rf"(?P<tag>{_TAG})|(?P<space>\s+)|(?P<text>[^<\s]+|<)")
_TAG_NAME_RE = re.compile(r"</?([a-z0-9:-]+)")

# A pretty-printed line break sat right after an opener or right before a
# closer in the original, where no space was: `(<i>N</i>-Hydroxy)`.
# Escapes: 2010 hyphen, 2011 non-breaking hyphen, 2013 en dash, and the curly
# quotes, 201C / 2018 opening and 201D / 2019 closing (double / single).
_OPENERS = frozenset("([{/-\u2010\u2011\u2013\u201c\u2018")
_CLOSERS = frozenset(")]}/-\u2010\u2011\u2013,.;:!?\u201d\u2019")


def clean_metadata_text(text: str) -> str:
    """``text`` as one plain line: entities decoded, markup gone, single spaces.

    A whitespace run is pretty-print when it holds a line break and touches a
    tag. It is dropped inside MathML, and where the text it separates is glued
    to an opener on its left or a closer on its right (or there is no text on
    one side); otherwise it is one space, like every other run.

    Pure and idempotent; non-ASCII text passes through, ``""`` stays ``""``.
    """
    # One pass leaves the result of a decoded entity or a dropped tag in
    # place; if that happens to spell another (`&am<i></i>p;`), the next pass
    # takes it. Each pass that changes anything after the first shortens the
    # string, so this ends.
    while True:
        cleaned = _clean_once(text)
        if cleaned == text:
            return cleaned
        text = cleaned


def _clean_once(text: str) -> str:
    text = _BR_RE.sub(" ", _unescape(text))
    tokens = [(m.lastgroup, m.group()) for m in _TOKEN_RE.finditer(text)]

    # First character of the next text token after each position, skipping
    # tags and whitespace: the right-hand side a line break would join.
    next_char: list[str | None] = [None] * len(tokens)
    upcoming: str | None = None
    for k in range(len(tokens) - 1, -1, -1):
        next_char[k] = upcoming
        if tokens[k][0] == "text":
            upcoming = tokens[k][1][0]

    out: list[str] = []
    last_char: str | None = None
    math_depth = 0
    for k, (kind, value) in enumerate(tokens):
        if kind == "text":
            out.append(value)
            last_char = value[-1]
        elif kind == "tag":
            math_depth = _math_depth(value, math_depth)
        elif "\n" in value and _touches_tag(tokens, k) and (
            math_depth
            or last_char is None
            or next_char[k] is None
            or last_char in _OPENERS
            or next_char[k] in _CLOSERS
        ):
            continue
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def _unescape(text: str) -> str:
    # Per entity, never html.unescape on the whole string: that also decodes
    # the legacy semicolon-less names (`&not`, `&para`) inside ordinary words.
    while True:
        decoded = _ENTITY_RE.sub(lambda m: html.unescape(m.group()), text)
        if decoded == text:
            return text
        text = decoded


def _touches_tag(tokens: list[tuple[str | None, str]], k: int) -> bool:
    return (k > 0 and tokens[k - 1][0] == "tag") or (
        k + 1 < len(tokens) and tokens[k + 1][0] == "tag"
    )


def _math_depth(tag: str, depth: int) -> int:
    match = _TAG_NAME_RE.match(tag)
    name = match.group(1) if match else ""
    if name.rsplit(":", 1)[-1] != "math" or tag.endswith("/>"):
        return depth
    return max(depth - 1, 0) if tag.startswith("</") else depth + 1


def dirty_text_fields(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """The text fields cleaning would change, mapped to their clean values.

    Covers :data:`TEXT_FIELDS` (string values only) and ``authors``, which
    comes back whole — every string entry cleaned, anything else as it was —
    so a caller can write it as it stands. The health check that reports
    these and the ``--fix`` that rewrites them both read this, so they cannot
    disagree about what is dirty.
    """
    dirty: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        value = metadata.get(field)
        if isinstance(value, str):
            cleaned = clean_metadata_text(value)
            if cleaned != value:
                dirty[field] = cleaned
    authors = metadata.get("authors")
    if isinstance(authors, list):
        cleaned_authors = [
            clean_metadata_text(a) if isinstance(a, str) else a for a in authors
        ]
        if cleaned_authors != list(authors):
            dirty["authors"] = cleaned_authors
    return dirty
