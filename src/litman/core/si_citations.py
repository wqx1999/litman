"""SI citations in a paper's notes.md / discussion.md.

An agent cites an SI file by its number, with the name the publisher gave it
right after: ``si-01.pdf（ci9b00237_si_001.pdf）``. Numbers are given again
once a file is deleted (:func:`litman.core.supplementary.plan_names`), so a
citation written earlier can come to point at another file. This module finds
the citations and says which no longer match the paper's records; the
``supplementary_cited`` health check and the delete confirmation (``lit rm
--si``, the GUI) share it. Pure text logic: nothing here writes.

What counts as a citation:

* a token ``si-NN`` (two or more digits) with up to two lowercase extension
  segments, not glued to a letter, digit, ``_`` or ``-`` on either side — so
  ``si-1``, ``SI-01`` and ``psi-01`` are not citations, while
  ``supplementary/si-01.pdf`` and ``**si-01.pdf**`` are. A token right after
  ``--si `` / ``--si=`` is a command being quoted, not a citation;
* the original name, when one follows in parentheses — ``（…）`` or ``(…)``,
  after any closing backticks / asterisks and spaces — and the text inside
  looks like a file name (``(p. 12)``, ``（Table S3）`` are not one). Text
  before the first comma or semicolon counts when the whole does not:
  ``（a.pdf，第 12 页）`` names ``a.pdf``.

HTML comments and fenced code blocks are skipped. discussion.md is a dated
log: a ``## YYYY-MM-DD`` heading opens a section, any other level-1 or
level-2 heading closes it, so a citation knows when it was written.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from litman.core.search import _mask_html_comments
from litman.core.supplementary import original_label

NOTE_FILES: tuple[str, ...] = ("notes.md", "discussion.md")

# Possessive extension: "si-01.pdf_old" is no citation at all, rather than one
# of si-01 found by backing off the extension.
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])si-(\d{2,})((?:\.[a-z0-9]++){0,2}+)(?![A-Za-z0-9_])"
)
_COMMAND_PREFIXES = ("--si ", "--si=")
_SI_NAME_RE = re.compile(r"^si-(\d+)((?:\.[a-z0-9]+)*)")
_PARENS = {"(": ")", "（": "）"}
_PAREN_RES = {"(": re.compile(r"[()]"), "（": re.compile(r"[（）]")}
# A file name is at most 255 characters: a closing parenthesis further away
# closes something else, and looking for it on a long line would take long.
_PAREN_SPAN = 260
_NAME_EXT_RE = re.compile(r"(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,12}")
# "Fig.S3", "Tab.S2", "p.S12": a supplementary figure or page, not an extension.
_S_NUMBER_RE = re.compile(r"[Ss]\d+[a-z]?")
_CLAUSE_BREAK_RE = re.compile(r"，|；|, |; ")
_TRIM = " \t`*"

# CommonMark: a heading may be indented up to three spaces.
_HEADING_RE = re.compile(r"^ {0,3}#{1,2}(?:[ \t]|$)")
_DATED_HEADING_RE = re.compile(r"^ {0,3}## +(\d{4}-\d{2}-\d{2})(?!\d)")
# A backtick fence's info string has no backtick: "```code``` …" is inline code.
_FENCE_RE = re.compile(r"^ {0,3}(?:(`{3,}+)(?!.*`)|(~{3,}+))")


@dataclass(frozen=True)
class SiCitation:
    """One citation: where it is, the token as written, and what it names.

    ``section`` is the date of the discussion.md section it sits in; ``None``
    in notes.md and outside any dated section.
    """

    file: str
    line: int
    token: str
    number: int
    original: str | None
    section: date | None


@dataclass(frozen=True)
class CitationProblem:
    """A citation that no longer matches the records.

    ``kind``: ``"gone"`` — no record matches it; ``"renamed"`` — the record
    with that number holds a file under another original name;
    ``"predates"`` — written (with no original name) in a discussion section
    dated before the record was added, so it may mean a file since replaced.
    ``moved_to`` names the record that now holds the cited original, if one
    does; ``added`` is the record's added date (``None`` when unreadable).
    """

    citation: SiCitation
    kind: Literal["gone", "renamed", "predates"]
    record: Mapping[str, Any] | None
    moved_to: str | None
    added: date | None


def _looks_like_file_name(text: str) -> bool:
    stem, dot, ext = text.rpartition(".")
    return (
        bool(dot and stem.strip())
        and "/" not in text
        and "\\" not in text
        and _NAME_EXT_RE.fullmatch(ext) is not None
        and _S_NUMBER_RE.fullmatch(ext) is None
    )


def _original_after(line: str, end: int) -> str | None:
    """The original name in parentheses right after a token ending at ``end``."""
    i = end
    while i < len(line) and line[i] in "`*":
        i += 1
    while i < len(line) and line[i] in " \t":
        i += 1
    if i >= len(line) or line[i] not in _PARENS:
        return None
    opener = line[i]
    depth = 0
    for paren in _PAREN_RES[opener].finditer(line, i, i + _PAREN_SPAN):
        depth += 1 if paren.group() == opener else -1
        if depth == 0:
            inner = line[i + 1 : paren.start()].strip(_TRIM)
            break
    else:
        return None
    if _looks_like_file_name(inner):
        return inner
    head = _CLAUSE_BREAK_RE.split(inner, maxsplit=1)[0].strip(_TRIM)
    return head if _looks_like_file_name(head) else None


def _fence(line: str) -> tuple[str, int] | None:
    """The fence a line opens or closes — its character and length — if any."""
    mark = _FENCE_RE.match(line)
    if mark is None:
        return None
    run = mark.group(1) or mark.group(2)
    return run[0], len(run)


def _calendar_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def scan_si_citations(text: str, file: str, *, dated: bool) -> list[SiCitation]:
    """Every citation in ``text`` (the content of ``file``), in line order.

    Line numbers count ``\\n`` as an editor does. One citation per line,
    token and original name: the same one twice on a line is one. ``dated``
    reads the ``## YYYY-MM-DD`` sections (discussion.md).
    """
    if "si-" not in text:
        return []
    text = text.removeprefix("\ufeff")
    out: list[SiCitation] = []
    seen: set[tuple[int, str, str | None]] = set()
    section: date | None = None
    fence: tuple[str, int] | None = None
    for lineno, line in enumerate(_mask_html_comments(text).split("\n"), start=1):
        mark = _fence(line)
        if fence is not None:
            if mark and mark[0] == fence[0] and mark[1] >= fence[1]:
                fence = None
            continue
        if mark:
            fence = mark
            continue
        if dated and _HEADING_RE.match(line):
            heading = _DATED_HEADING_RE.match(line)
            section = _calendar_date(heading.group(1)) if heading else None
        for token in _TOKEN_RE.finditer(line):
            if line[max(0, token.start() - 5) : token.start()] in _COMMAND_PREFIXES:
                continue
            original = _original_after(line, token.end())
            key = (lineno, token.group(0), original)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                SiCitation(
                    file=file,
                    line=lineno,
                    token=token.group(0),
                    number=int(token.group(1)),
                    original=original,
                    section=section,
                )
            )
    return out


def read_si_citations(
    paper_dir: Path,
) -> tuple[list[SiCitation], list[tuple[str, str]]]:
    """Citations in a paper's notes.md and discussion.md (an absent one has
    none), and ``(file, error class)`` for each that cannot be read as UTF-8
    text — its citations unchecked, never silently skipped."""
    citations: list[SiCitation] = []
    unreadable: list[tuple[str, str]] = []
    for name in NOTE_FILES:
        path = paper_dir / name
        try:
            # Inside the try: a folder that cannot be searched makes even
            # this check raise.
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append((name, exc.__class__.__name__))
            continue
        citations.extend(
            scan_si_citations(text, name, dated=name == "discussion.md")
        )
    return citations, unreadable


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _record_names(record: Mapping[str, Any]) -> set[str]:
    """The record's original name, raw and as printed, folded for comparing."""
    names = {original_label(record)}
    raw = record.get("original")
    if isinstance(raw, str):
        names.add(raw)
    return {_fold(name) for name in names if name}


def _number_and_ext(name: str) -> tuple[int, str] | None:
    m = _SI_NAME_RE.match(name)
    return (int(m.group(1)), m.group(2)) if m else None


def _matches(citation: SiCitation, record: Mapping[str, Any]) -> bool:
    """A token names the number, however many digits it is written with; one
    with an extension names only the file with that extension."""
    have = _number_and_ext(str(record.get("file") or ""))
    want = _number_and_ext(citation.token)
    if have is None or want is None or have[0] != want[0]:
        return False
    return not want[1] or want[1] == have[1]


def refers_to(citation: SiCitation, record: Mapping[str, Any]) -> bool:
    """Whether ``citation`` means ``record``: the token matches it, and the
    original names agree wherever both are known."""
    if not _matches(citation, record):
        return False
    names = _record_names(record)
    return citation.original is None or not names or _fold(citation.original) in names


def count_citations(
    citations: Iterable[SiCitation], record: Mapping[str, Any]
) -> dict[str, int]:
    """``{notes file: lines citing record}``, files without one left out,
    in :data:`NOTE_FILES` order."""
    lines: dict[str, set[int]] = {name: set() for name in NOTE_FILES}
    for citation in citations:
        if refers_to(citation, record):
            lines.setdefault(citation.file, set()).add(citation.line)
    return {name: len(found) for name, found in lines.items() if found}


def added_date(value: Any) -> date | None:
    """The calendar date of a record's ``added`` stamp, as written; ``None``
    when it is missing or not a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def citation_problems(
    citations: Iterable[SiCitation], records: Sequence[Mapping[str, Any]]
) -> list[CitationProblem]:
    """The citations that no longer match ``records``, in the order given.

    One problem per line, file cited and kind: a line citing a file twice,
    in two spellings, is one finding. A bare mention is never "predates" on
    a line that also names that file with its right original.
    """
    matched = [
        (c, next((r for r in records if _matches(c, r)), None)) for c in citations
    ]
    named = {
        (c.file, c.line, str(r["file"]))
        for c, r in matched
        if r is not None and c.original is not None and refers_to(c, r)
    }
    out: list[CitationProblem] = []
    seen: set[tuple[str, int, str, str]] = set()

    def add(problem: CitationProblem) -> None:
        c = problem.citation
        target = (
            str(problem.record["file"]) if problem.record is not None else str(c.number)
        )
        key = (c.file, c.line, target, problem.kind)
        if key not in seen:
            seen.add(key)
            out.append(problem)

    for citation, record in matched:
        moved_to = None
        if citation.original is not None:
            wanted = _fold(citation.original)
            moved_to = next(
                (
                    str(r["file"])
                    for r in records
                    if r is not record and wanted in _record_names(r)
                ),
                None,
            )
        if record is None:
            add(CitationProblem(citation, "gone", None, moved_to, None))
            continue
        if citation.original is not None:
            if not refers_to(citation, record):
                add(CitationProblem(citation, "renamed", record, moved_to, None))
            continue
        if citation.section is None:
            continue
        if (citation.file, citation.line, str(record["file"])) in named:
            continue
        added = added_date(record.get("added"))
        if added is None or citation.section < added:
            add(CitationProblem(citation, "predates", record, None, added))
    return out


__all__ = [
    "NOTE_FILES",
    "CitationProblem",
    "SiCitation",
    "added_date",
    "citation_problems",
    "count_citations",
    "read_si_citations",
    "refers_to",
    "scan_si_citations",
]
