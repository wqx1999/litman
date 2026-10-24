"""Names that are not UTF-8 text — the one place that decides how they are shown.

On Linux a file name is a string of bytes. One that is not valid UTF-8 (a
folder unpacked from an old GBK or Latin-1 archive) reaches Python with each
byte it cannot decode turned into a lone surrogate: ``b"caf\\xe9"`` becomes
``"caf\\udce9"``. Such a string names the file correctly, but it cannot be
encoded as UTF-8, so it can be written to no YAML or JSON litman keeps and to
no web UI response. macOS (APFS refuses such names) and Windows (names are
UTF-16) never produce one.

What litman does with them:

* a path it must **record** (a project's or a library's folder) is refused
  before anything is written, with :func:`refusal`;
* a name it only **shows** (a message, a list entry, a provenance note) goes
  through :func:`printable`, each such byte becoming U+FFFD — the form SI
  records already store;
* a path it prints for pasting back into a shell goes through
  :func:`litman.core.supplementary.shell_path`;
* any other text holding one (a value typed on the command line, a field of
  ``--from-llm-json``) is stopped at the YAML writer with :func:`encode_error`,
  before the file is written, and ``lit`` reports it with :func:`text_refusal`
  instead of a traceback.

Standard library only.
"""

from __future__ import annotations

import unicodedata
from pathlib import PurePath


def has_undecodable(text: str) -> bool:
    """Whether ``text`` holds a lone surrogate (an undecodable byte)."""
    return not text.isascii() and any(unicodedata.category(c) == "Cs" for c in text)


def printable(text: str) -> str:
    """``text`` with each lone surrogate replaced by U+FFFD."""
    if not has_undecodable(text):
        return text
    return "".join("\ufffd" if unicodedata.category(c) == "Cs" else c for c in text)


def undecodable_part(path: PurePath | str) -> str | None:
    """The first component of ``path`` that is not UTF-8 text, or ``None``."""
    return next((part for part in PurePath(path).parts if has_undecodable(part)), None)


def refusal(path: PurePath | str) -> str | None:
    """The sentence refusing to record ``path``, or ``None`` when it is UTF-8 text."""
    part = undecodable_part(path)
    if part is None:
        return None
    return (
        f"Folder name “{printable(part)}” is not UTF-8 text, so litman cannot "
        "record its path. Rename the folder, then retry."
    )


def config_refusal(data: object, filename: str = "lit-config.yaml") -> str | None:
    """The sentence refusing to write ``filename`` with ``data``, or ``None``.

    The backstop behind every path check: a lone surrogate would be written as
    a ``\\uDCxx`` escape that litman's YAML reader then rejects, leaving every
    command that reads the config unable to start (and no web UI response can
    carry it).
    """
    bad = find_undecodable(data)
    if bad is None:
        return None
    name = undecodable_part(bad) or bad
    return (
        f"{filename} cannot hold “{printable(name)}”: it is not UTF-8 text. "
        "Nothing was written; rename or retype it, then retry."
    )


_WORD_ENDS = frozenset("\"'`,;:()[]{}<>=")


def _in_word(c: str) -> bool:
    return not c.isspace() and c not in _WORD_ENDS


def text_refusal(text: str, at: int) -> str:
    """The sentence for ``text`` that failed to encode at index ``at``: the word
    around that index (``text`` is often already YAML or JSON, so quotes and
    punctuation end it too), or its line when the word is nothing but such
    bytes, shown through :func:`printable`."""
    start = at
    while start > 0 and _in_word(text[start - 1]):
        start -= 1
    end = at
    while end < len(text) and _in_word(text[end]):
        end += 1
    if not printable(text[start:end]).strip("\ufffd"):
        start = text.rfind("\n", 0, at) + 1
        end = text.find("\n", at)
        if end < 0:
            end = len(text)
    if end - start > 60:  # a window that keeps the byte in view
        lo = max(start, min(at - 30, end - 60))
        word = ("…" if lo > start else "") + text[lo:lo + 60] + ("…" if lo + 60 < end else "")
    else:
        word = text[start:end]
    return (
        f"“{printable(word)}” is not UTF-8 text, so litman cannot store it. "
        "Change it, then retry."
    )


def encode_error(text: str) -> UnicodeEncodeError:
    """The error a strict UTF-8 encode of ``text`` raises, built before a byte
    of it is written (``text`` must hold a lone surrogate)."""
    at = next(i for i, c in enumerate(text) if unicodedata.category(c) == "Cs")
    return UnicodeEncodeError("utf-8", text, at, at + 1, "surrogates not allowed")


def find_undecodable(value: object) -> str | None:
    """A string in ``value`` — dict keys and values, lists, tuples, at any
    depth — that holds a lone surrogate, or ``None`` when there is none."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if has_undecodable(item):
                return item
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return None


def printable_tree(value: object) -> object:
    """``value`` with every string in it, keys included, through :func:`printable`."""
    if isinstance(value, str):
        return printable(value)
    if isinstance(value, dict):
        return {printable_tree(k): printable_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [printable_tree(v) for v in value]
    if isinstance(value, tuple):
        return tuple(printable_tree(v) for v in value)
    return value
