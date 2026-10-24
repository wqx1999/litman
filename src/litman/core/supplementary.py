"""A paper's supplementary information (SI) — the one write path for it.

SI files live in ``papers/<id>/supplementary/`` as ``si-01.pdf``,
``si-02.xlsx`` …, each recorded in the paper's ``metadata.yaml`` under
``supplementary`` (``file`` / ``original`` / ``sha256`` / ``bytes`` /
``added`` / ``check``) and read-only locked like ``paper.pdf``. Nothing here
fetches anything: the user downloads SI from the publisher and hands the files
over (ADR-027). Every add and remove — ``lit add --si``, ``lit rm --si``,
``lit health-check --fix`` and the GUI — goes through
:func:`add_supplementary` / :func:`remove_supplementary` /
:func:`drop_missing_records` (invariant #16). A removed file is deleted, not
trashed: adding it again is the way back.

Before anything is written, :func:`verify_batch` decides whether each file
belongs to the paper:

* ✓ ``ok`` — its name carries the paper's DOI number, or its first pages
  carry the paper's title;
* ? ``unverified`` — nothing tells either way (a scanned PDF, a spreadsheet
  named by the authors); it goes in only on the user's say-so;
* ✕ ``rejected`` — empty, already in the library, the article itself, or
  another paper's SI. No entry point can add one: :func:`add_supplementary`
  verifies again and refuses, whatever its caller decided.

Import cost: pypdf (through :mod:`litman.core.pdf_text`) and the derived
rebuild are imported inside the functions that use them. ``lit show`` and the
health checks import this module, and the per-command drift hook imports
``checks`` on every ``lit`` invocation.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import os
import re
import stat
import threading
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from litman.core.atomic import staged_write
from litman.core.dates import now_iso
from litman.core.dedup import canonicalize_doi
from litman.core.document import find_paper, list_papers, load_yaml_or_raise
from litman.core.fs_identity import Anchor
from litman.core.locking import (
    SI_DIRNAME,
    is_file_manager_dropping,
    is_si_candidate_name,
    lock_truth_file,
    unlock_truth_file,
)
from litman.core.textclean import clean_metadata_text
from litman.core.undecodable import has_undecodable, printable
from litman.core.views import (
    load_index_papers,
    papers_for_index,
    render_index,
    view_fields_snapshot,
)
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import (
    LitmanError,
    PaperNotFoundError,
    SupplementaryError,
)

SUPPLEMENTARY_FIELD = "supplementary"

# D13: anything bigger is added only after an explicit yes (or --force).
LARGE_FILE_BYTES = 100 * 1024 * 1024

# The GUI's upload stash (server/routes_ingest.py). An SI file staged there is
# one of the three in-library places a source may come from.
_UPLOAD_DIRNAME = ".litman-upload"

# A recorded SI file name: "si-" + at least two digits + up to two lowercase
# alphanumeric extension segments ("si-07.tar.gz").
_SI_NAME_RE = re.compile(r"^si-(\d{2,})((?:\.[a-z0-9]+){0,2})$")
# Lenient on purpose: ANY "si-<n>…" name in the folder holds its number, so a
# hand-copied "si-09.PDF" still keeps 09 from being handed out.
_SI_NUMBER_RE = re.compile(r"^si-(\d+)", re.IGNORECASE)
_SI_ARG_RE = re.compile(r"^si-(\d+)(\..+)?$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_COMPOUND_EXTS: tuple[str, ...] = (".tar.gz", ".tar.bz2", ".tar.xz")
_EXT_RE = re.compile(r"^[a-z0-9]{1,12}$")

# Last suffixes the system's default app would run rather than show: opening
# one with `open` / `os.startfile` / `xdg-open` executes it. A file with no
# suffix at all counts too — that is what a script or a binary looks like.
# Such a file still downloads in the browser.
_RUNNABLE_EXTS: frozenset[str] = frozenset(
    {
        # Windows: programs, installers, scripts, shortcuts
        "exe", "com", "bat", "cmd", "msi", "msp", "scr", "pif", "ps1", "vbs",
        "vbe", "js", "jse", "wsf", "wsh", "hta", "lnk", "reg", "cpl", "msc",
        "mst", "msu", "msix", "msixbundle", "appx", "appxbundle",
        "application", "xbap", "gadget", "diagcab", "vb", "ws", "wsc", "sct",
        "psm1", "psd1", "ps1xml", "psc1", "scf", "inf", "ins", "isp", "url",
        "website", "wsb", "xll", "chm",
        # macOS: apps, packages, scripts, links, configuration profiles
        "app", "command", "tool", "pkg", "scpt", "workflow", "mpkg", "scptd",
        "applescript", "action", "terminal", "webloc", "inetloc", "fileloc",
        "mobileconfig",
        # Linux: scripts, self-running binaries, packages
        "sh", "run", "desktop", "appimage", "bin", "bash", "zsh", "csh",
        "ksh", "tcsh", "fish", "deb", "rpm", "snap", "flatpak", "flatpakref",
        "flatpakrepo",
        # Programs for an interpreter the association starts
        "jar", "py", "pyw", "pyz", "pyzw", "pyc", "pyo", "pl", "rb", "rbw",
        "php", "tcl", "jnlp",
    }
)

# Characters that break or disguise the line a name is printed on: control
# characters (tab, newline), format marks such as U+202E (right-to-left
# override, which makes "fdp.exe" read as "exe.pdf"), the line / paragraph
# separators, and lone surrogates (what an undecodable byte in a POSIX file
# name turns into) — but not the zero-width non-joiner and joiner, ordinary in
# Persian and Indic names and in emoji sequences, and unable to reorder text
# the way the bidi controls can.
_UNPRINTABLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
_JOINERS = frozenset({"\u200c", "\u200d"})

# The publisher's own running number inside a downloaded SI file name, most
# specific pattern first. "-s001" must be exactly three digits so Elsevier's
# "1-s2.0-S0022…" prefix never reads as one.
_PUBLISHER_SEQ_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"_si_(\d+)"),  # ACS: ci9b00237_si_001.pdf
    re.compile(r"-sup-(\d+)"),  # Wiley: psc3666-sup-0001-suppinfo.pdf
    re.compile(r"moesm(\d+)"),  # Springer / Nature / BMC: …_MOESM1_ESM.pdf
    re.compile(r"(?<![a-z])mmc(\d+)"),  # Elsevier / Cell: …-mmc1.pdf
    re.compile(r"media-(\d+)"),  # bioRxiv: media-1.pdf
    re.compile(r"-s(\d{3})(?!\d)"),  # …-s001.pdf
)

_PDF_MAGIC = b"%PDF-"
_PDF_SNIFF_BYTES = 1024

# §核验 0: how the article itself says it has SI. No bare "ESI": chemistry
# papers are full of "ESI-MS"; RSC's "electronic supplementary" covers it.
_MENTION_RE = re.compile(
    r"supporting information|supplementary|supplemental|additional file\s*\d"
    r"|si appendix|electronic supplementary",
    re.IGNORECASE,
)

# Measured on a real 28-paper library (spec §核验 3): a real SI's first page
# shares at most 0.20 Jaccard with the article's first page; another copy of
# the article itself scores 0.92.
_ARTICLE_JACCARD = 0.6
# A title counts only as a phrase (:func:`_title_key`): its words, scattered,
# are on any paper from the same field. A title under this many content words
# is matched by any SI from that field.
_MIN_TITLE_WORDS = 5
# Coarsest common mtime granularity (FAT / some Windows paths): within this,
# an SI file whose size still matches its record is taken as unchanged.
_MTIME_SLACK_S = 2.0

_WORD_RE = re.compile(r"[^\W_]+")
# A title key also skips the spacing modifier letters (U+02B0-02FF): a
# TeX-made PDF sets a circumflex or caron as its own character.
_KEY_RE = re.compile(r"[^\W_\u02b0-\u02ff]+")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_SPRINGER_DOI_RE = re.compile(r"^s(\d{5})-\d{3}-(\d+)-[0-9a-z]$")
_SPRINGER_FILE_RE = re.compile(r"(?<!\d)(\d{5})_\d{4}_(\d+)_moesm\d+")

_STOPWORDS: frozenset[str] = frozenset(
    {
        "about", "across", "after", "against", "all", "along", "among", "and",
        "any", "are", "based", "been", "before", "being", "between", "both",
        "but", "can", "does", "during", "each", "for", "from", "has", "have",
        "how", "into", "its", "may", "more", "not", "novel", "one", "onto",
        "other", "our", "over", "than", "that", "the", "their", "them",
        "then", "there", "these", "this", "those", "through", "toward",
        "towards", "two", "under", "upon", "use", "used", "using", "via",
        "was", "were", "what", "when", "where", "which", "while", "who",
        "why", "with", "within", "without",
    }
)

_yaml_rt = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    preserve_quotes=True,
    default_flow_style=False,
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiItem:
    """One file offered as SI: where it is, and the name to judge it by.

    ``original`` is the name the publisher gave it — the file's own name. A
    lone surrogate in it (an undecodable byte of a POSIX file name) becomes
    U+FFFD, since metadata.yaml's reader rejects one: the name is what the
    user and the record see, while ``path`` still names the real file.

    ``approved`` is the user's "add anyway" for this one file: a file that
    cannot be confirmed goes in when it is set, as with ``allow_unverified``
    for the whole call. It never lets a rejected file in, and a file that
    verified when the user saw it but no longer does still needs it.
    """

    path: Path
    original: str
    approved: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "original", printable(self.original))


@dataclass(frozen=True)
class Verdict:
    """Whether a file belongs to the paper, and why (see the module docstring)."""

    status: Literal["ok", "unverified", "rejected"]
    check: Literal["filename", "title"] | None
    reason: str
    other_id: str | None = None
    sha256: str = ""
    size: int = 0


@dataclass
class SiAddResult:
    """What :func:`add_supplementary` committed."""

    records: list[dict[str, Any]]
    paths: list[Path]
    # Post-commit best-effort hiccups (INDEX lag, a source that could not be
    # removed); the SI is in the library either way.
    warnings: list[str] = field(default_factory=list)


@dataclass
class SiRemoveResult:
    """What :func:`remove_supplementary` did."""

    records: list[dict[str, Any]]
    # Recorded files that were already gone: only their record was dropped.
    gone: list[str] = field(default_factory=list)
    # A derived-view rebuild that failed afterwards; the files are gone anyway.
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SiRemovalPreview:
    """What deleting some SI files would touch (:func:`preview_si_removal`).

    ``cited`` maps each file the notes cite to ``{notes file: lines citing
    it}``; ``unchecked`` holds ``(notes file, why)`` for one that could not be
    read, so its citations went uncounted.
    """

    records: list[dict[str, Any]]
    cited: dict[str, dict[str, int]]
    unchecked: list[tuple[str, str]]


@dataclass
class SiDrift:
    """Where a paper's ``supplementary`` records and folder disagree.

    ``problems`` are the field's structural faults and ``folder`` says why
    ``supplementary`` is no usable folder (``"link"`` / ``"file"``); either
    one stops the rest of the comparison. ``odd_recorded`` / ``odd_strays``
    hold ``(name, kind)`` for entries that are not plain files (``"link"``,
    ``"dir"``, ``"other"``), ``unchecked`` holds ``(name, why)`` for what
    could not be checked — ``why`` is ``"no checksum"`` or the error's class
    name, and an empty ``name`` means the folder itself.
    """

    problems: list[str] = field(default_factory=list)
    folder: str | None = None
    missing: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unrecorded: list[str] = field(default_factory=list)
    odd_recorded: list[tuple[str, str]] = field(default_factory=list)
    odd_strays: list[tuple[str, str]] = field(default_factory=list)
    unchecked: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class SiFixPolicy:
    """The decisions ``health-check --fix`` needs from a person.

    core never prompts; the command layer passes these in (the same shape as
    ``_apply_add``'s ``resolve_collision``). ``confirm_drop`` is asked once per
    paper about its records whose file is gone, ``confirm_unverified`` once per
    stray file that cannot be confirmed; ``on_rejected`` hears about a stray
    that will never be recorded.
    """

    confirm_drop: Callable[[str, list[str]], bool]
    confirm_unverified: Callable[[str, Path, Verdict], bool]
    on_rejected: Callable[[str, Path, Verdict], None] = lambda *_args: None


# No one to ask: record what verifies, leave everything else reported.
NO_PROMPT_POLICY = SiFixPolicy(
    confirm_drop=lambda *_args: False,
    confirm_unverified=lambda *_args: False,
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


_quiet_lock = threading.Lock()
_quiet_depth = 0
_quiet_saved_level = logging.NOTSET


@contextlib.contextmanager
def _quiet_pypdf() -> Iterator[None]:
    """Silence pypdf's log chatter ("Ignoring wrong pointing object") meanwhile.

    Depth-counted under a lock: the GUI server verifies uploads on worker
    threads, and two overlapping save/restore pairs would otherwise leave the
    logger at whatever level the slower one saw.
    """
    global _quiet_depth, _quiet_saved_level
    logger = logging.getLogger("pypdf")
    with _quiet_lock:
        if _quiet_depth == 0:
            _quiet_saved_level = logger.level
            logger.setLevel(logging.ERROR)
        _quiet_depth += 1
    try:
        yield
    finally:
        with _quiet_lock:
            _quiet_depth -= 1
            if _quiet_depth == 0:
                logger.setLevel(_quiet_saved_level)


def _pdf_text(path: Path, pages: list[int] | None) -> list[str] | None:
    """Text of the given pages, or ``None`` when the file does not parse."""
    from litman.core.pdf_text import PdfTextError, extract_pdf_text

    with _quiet_pypdf():
        try:
            return extract_pdf_text(path, pages)
        except PdfTextError:
            return None


def _looks_like_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return _PDF_MAGIC in f.read(_PDF_SNIFF_BYTES)
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _words(text: str) -> set[str]:
    """Distinct lowercase alphanumeric words of 3+ characters."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return {w for w in _WORD_RE.findall(folded) if len(w) >= 3}


def _content_words(text: str) -> frozenset[str]:
    return frozenset(_words(text) - _STOPWORDS)


def _title_key(text: str) -> str:
    """``text`` as one run of lowercase letters and digits, accents dropped.

    A title is on a page when its key is inside the page's: line breaks,
    line-end hyphens, missing spaces and accents (a TeX-made PDF gives
    ``Schr¨odinger``) drop out, while the words must still come in order,
    unbroken.
    """
    decomposed = unicodedata.normalize("NFKD", text).lower()
    return "".join(_KEY_RE.findall(decomposed))


def _is_digit(char: str) -> bool:
    return "0" <= char <= "9"


def _timestamp_of(value: Any) -> float | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return moment.timestamp()


def _number_of(name: str) -> int | None:
    m = _SI_NUMBER_RE.match(name)
    return int(m.group(1)) if m else None


def si_stem(name: str) -> str:
    """``si-02`` for ``si-02.xlsx`` — the short name ``lit rm --si`` takes."""
    return name.split(".", 1)[0]


def shell_path(path: Path | str) -> str:
    """A path as it can be pasted into a shell command.

    One holding a byte that is not UTF-8 text goes in ``$'…'`` quoting with
    that byte as ``\\xNN``, so the shell finds the very file: bash and zsh
    read it, plain ``sh`` and fish do not.
    """
    text = str(path)
    if has_undecodable(text):
        return "$'" + "".join(_ansi_c(c) for c in text) + "'"
    return f'"{text}"' if any(c.isspace() for c in text) else text


def _ansi_c(char: str) -> str:
    """One character of a path inside ``$'…'`` quoting."""
    code = ord(char)
    if 0xDC80 <= code <= 0xDCFF:  # an undecodable byte, as os.fsdecode keeps it
        return f"\\x{code - 0xDC00:02x}"
    if char in "\\'":
        return "\\" + char
    if code < 0x20 or code == 0x7F:
        return f"\\x{code:02x}"
    return char


# ---------------------------------------------------------------------------
# §核验 0 — does the article mention SI at all?
# ---------------------------------------------------------------------------


def mentions_supplementary(pdf: Path) -> bool | None:
    """Whether the article's text says it has supplementary material.

    ``None`` when there is no text to judge — a scanned PDF, or one that does
    not parse — which callers treat as "may well have SI".
    """
    pages = _pdf_text(pdf, None)
    if not pages:
        return None
    text = " ".join(" ".join(pages).split())
    if not text:
        return None
    return _MENTION_RE.search(text) is not None


# ---------------------------------------------------------------------------
# §核验 2 — the DOI number inside a file name
# ---------------------------------------------------------------------------


def _doi_suffix(doi: str) -> str:
    bare = canonicalize_doi(str(doi or "")).lower()
    _prefix, slash, suffix = bare.partition("/")
    return suffix if slash else ""


def doi_tokens(doi: str) -> set[str]:
    """The pieces of a DOI that publishers repeat in SI file names.

    From the suffix (after the first ``/``): split on anything that is not a
    letter or digit, keep pieces of 4+ characters that contain a digit, drop
    bare years. ``10.1021/acs.jcim.9b00237`` → ``{"9b00237"}``.
    """
    return {
        part
        for part in re.split(r"[^a-z0-9]+", _doi_suffix(doi))
        if len(part) >= 4
        and any(_is_digit(c) for c in part)
        and not _YEAR_RE.match(part)
    }


def _springer_key(doi: str) -> tuple[str, int] | None:
    """``("13321", 748)`` for ``10.1186/s13321-023-00748-2``.

    Springer / BMC / Nature name SI ``13321_2023_748_MOESM1_ESM.pdf`` — the
    journal number without its ``s`` and the article number without its
    zero padding — so no plain substring of the DOI appears in it.
    """
    m = _SPRINGER_DOI_RE.match(_doi_suffix(doi))
    return (m.group(1), int(m.group(2))) if m else None


def _token_in_name(token: str, name: str) -> bool:
    """``token`` occurs in ``name`` without running into a neighbouring digit.

    A number-shaped token must not be the middle of a longer number: "3666"
    is not in Elsevier's "1-s2.0-S0022283620301662-mmc1.pdf"-style names just
    because the digits happen to line up.
    """
    start = name.find(token)
    while start != -1:
        end = start + len(token)
        clear_before = not (
            _is_digit(token[0]) and start > 0 and _is_digit(name[start - 1])
        )
        clear_after = not (
            _is_digit(token[-1]) and end < len(name) and _is_digit(name[end])
        )
        if clear_before and clear_after:
            return True
        start = name.find(token, start + 1)
    return False


@dataclass(frozen=True)
class _DoiKey:
    tokens: frozenset[str]
    springer: tuple[str, int] | None

    @classmethod
    def of(cls, doi: Any) -> _DoiKey:
        text = str(doi or "")
        return cls(frozenset(doi_tokens(text)), _springer_key(text))

    def strength(self, filename: str) -> int:
        """How specifically ``filename`` names this DOI (0 = not at all)."""
        name = filename.lower()
        best = max(
            (len(t) for t in self.tokens if _token_in_name(t, name)), default=0
        )
        if self.springer is not None:
            journal, article = self.springer
            for m in _SPRINGER_FILE_RE.finditer(name):
                if m.group(1) == journal and int(m.group(2)) == article:
                    best = max(best, len(m.group(0)))
        return best


def name_matches(filename: str, doi: str) -> bool:
    """Whether ``filename`` carries the number of ``doi`` (rules b and c)."""
    return _DoiKey.of(doi).strength(filename) > 0


# ---------------------------------------------------------------------------
# Records and names
# ---------------------------------------------------------------------------


def list_supplementary(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The paper's well-formed SI records, in file order of the field.

    An entry that is not a mapping, has no valid ``file``, or repeats one is
    left out; :func:`field_problems` is what reports it.
    """
    value = meta.get(SUPPLEMENTARY_FIELD)
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("file")
        if not isinstance(name, str) or not _SI_NAME_RE.match(name):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(dict(entry))
    return out


def field_problems(value: Any) -> list[str]:
    """Structural faults in a ``supplementary`` value, one phrase per fault.

    Absent / null is fine (no SI yet). Not a list, an entry that is not a
    mapping, a missing or invalid ``file``, the same ``file`` twice: with any
    of these, which files the list accounts for is unknown, so litman will
    not rewrite it until it is fixed by hand. An entry whose ``sha256`` /
    ``bytes`` are missing is not a structural fault (see :func:`has_checksum`).
    """
    if value is None:
        return []
    if not isinstance(value, list):
        return ["is not a list"]
    problems: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, Mapping):
            problems.append(f"entry {index} is not a mapping")
            continue
        name = entry.get("file")
        if not isinstance(name, str) or not _SI_NAME_RE.match(name):
            problems.append(f"entry {index} has no valid file name (si-NN.ext)")
            continue
        if name in seen:
            problems.append(f"lists {name} twice")
            continue
        seen.add(name)
    return problems


def has_checksum(record: Mapping[str, Any]) -> bool:
    """Whether a record carries the ``sha256`` / ``bytes`` its file is checked
    against. One without them is kept and carried through every add and
    remove untouched; only its content cannot be verified."""
    sha = record.get("sha256")
    size = record.get("bytes")
    return (
        isinstance(sha, str)
        and _SHA256_RE.match(sha) is not None
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
    )


def si_extension(original: str) -> str:
    """The extension an SI file keeps: lowercase, ``.tar.gz``-style kept whole.

    Only ``[a-z0-9]`` and at most 12 characters; anything else (or no dot at
    all) means no extension.
    """
    lower = original.lower()
    for compound in _COMPOUND_EXTS:
        if lower.endswith(compound) and len(lower) > len(compound):
            return compound
    stem, dot, ext = lower.rpartition(".")
    if not dot or not stem:
        return ""
    return f".{ext}" if _EXT_RE.match(ext) else ""


def is_runnable_si(name: str) -> bool:
    """Whether the system's default app would run the SI file ``name`` rather
    than show it: its last suffix belongs to a program, a script or a
    shortcut (``si-01.pdf.exe`` is an ``.exe``), or it has no suffix."""
    stem, _dot, ext = name.rpartition(".")
    return not stem or not ext or ext.lower() in _RUNNABLE_EXTS


def is_unprintable(char: str) -> bool:
    """Whether ``char`` would break or disguise the line a name is printed on
    (``_UNPRINTABLE_CATEGORIES``). The GUI refuses a dropped file whose name
    has one; :func:`original_label` turns each into a space."""
    return unicodedata.category(char) in _UNPRINTABLE_CATEGORIES and char not in _JOINERS


def original_label(record: Mapping[str, Any]) -> str | None:
    """The record's ``original`` as one printable line; ``None`` when it
    names nothing.

    A control or format character — a tab, a newline, a right-to-left
    override — or a lone surrogate becomes a space, so the name can neither
    break the line it is printed on, nor pass for another, nor fail to print.
    """
    original = record.get("original")
    if not isinstance(original, str):
        return None
    label = "".join(" " if is_unprintable(c) else c for c in original).strip()
    return label or None


def _publisher_seq(original: str) -> int | None:
    lower = original.lower()
    for pattern in _PUBLISHER_SEQ_RES:
        m = pattern.search(lower)
        if m:
            return int(m.group(1))
    return None


def plan_names(taken: Iterable[str], originals: Sequence[str]) -> list[str]:
    """``si-NN.ext`` names for a batch, aligned with ``originals``.

    Each file gets the lowest number from 1 up that no name in ``taken`` —
    recorded, or on disk in any case — holds, so a deleted file's number is
    given again: to the same file added back, or to the next one. Within the
    batch, files carrying the publisher's own running number (``_si_001``,
    ``MOESM1``, ``mmc1``, ``media-1``, ``-s001``, ``-sup-0001``) come first
    in that order; the rest follow in the order given.
    """
    used = _numbers_in(taken)
    seqs = [_publisher_seq(name) for name in originals]
    order = sorted(
        range(len(originals)),
        key=lambda i: (seqs[i] is None, seqs[i] or 0, i),
    )
    names = [""] * len(originals)
    number = 0
    for index in order:
        number += 1
        while number in used:
            number += 1
        names[index] = f"si-{number:02d}{si_extension(originals[index])}"
    return names


def _taken_names(si_dir: Path, records: list[dict[str, Any]]) -> list[str]:
    names = [rec["file"] for rec in records]
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        names.extend(os.listdir(si_dir))
    return names


def _numbers_in(names: Iterable[str]) -> set[int]:
    return {n for n in (_number_of(name) for name in names) if n is not None}


def _sorted_records(entries: Iterable[Any]) -> list[Any]:
    """Records in number order, so si-01 is listed before si-02."""
    return sorted(
        entries, key=lambda e: (_number_of(str(e["file"])) or 0, str(e["file"]))
    )


def resolve_si_records(
    paper_id: str, meta: Mapping[str, Any], names: Iterable[str]
) -> list[dict[str, Any]]:
    """The records ``names`` point at (``si-02`` or ``si-02.xlsx``), in order.

    Raises:
        SupplementaryError: a name matches no record of this paper.
    """
    records = list_supplementary(meta)
    chosen: list[dict[str, Any]] = []
    for raw in names:
        want = raw.strip()
        m = _SI_ARG_RE.match(want)
        match = None
        if m:
            number = int(m.group(1))
            for rec in records:
                if _number_of(rec["file"]) != number:
                    continue
                if m.group(2) and rec["file"].lower() != want.lower():
                    continue
                match = rec
                break
        if match is None:
            raise SupplementaryError(
                f"{paper_id} has no SI file {raw!r}. "
                f"`lit show {paper_id}` lists its SI files."
            )
        if match not in chosen:
            chosen.append(match)
    return chosen


# ---------------------------------------------------------------------------
# §核验 1 — the verdict matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Paper:
    paper_id: str
    key: _DoiKey
    title_words: frozenset[str]
    title_key: str

    @classmethod
    def of(cls, paper_id: str, doi: Any, title: Any) -> _Paper:
        # A stored title may still carry Crossref markup (`<i>…</i>`),
        # which the page does not show.
        text = clean_metadata_text(str(title or ""))
        return cls(
            paper_id, _DoiKey.of(doi), _content_words(text), _title_key(text)
        )


class _Context:
    """Everything verification knows about the library, built once per batch."""

    def __init__(
        self,
        vault: Path,
        paper_id: str,
        meta: Mapping[str, Any],
        index_papers: Sequence[Mapping[str, Any]] | None,
    ) -> None:
        self.paper = _Paper.of(paper_id, meta.get("doi"), meta.get("title"))
        self.recorded_shas = {
            str(rec["sha256"])
            for rec in list_supplementary(meta)
            if has_checksum(rec)
        }
        if index_papers is None:
            index_papers = load_index_papers(vault) or list_papers(vault)
        self.others = [
            _Paper.of(str(p.get("id")), p.get("doi"), p.get("title"))
            for p in index_papers
            if p.get("id") and str(p.get("id")) != paper_id
        ]
        self._article = vault / "papers" / paper_id / "paper.pdf"
        self._article_words: set[str] | None = None
        self._article_words_read = False
        self._article_sha: str | None = None

    def article_size(self) -> int | None:
        try:
            return self._article.stat().st_size
        except OSError:
            return None

    def article_sha(self) -> str | None:
        if self._article_sha is None and self._article.is_file():
            self._article_sha = _sha256_file(self._article)
        return self._article_sha

    def article_words(self) -> set[str] | None:
        """First-page words of the paper's own PDF; None without a text layer."""
        if not self._article_words_read:
            self._article_words_read = True
            if self._article.is_file():
                pages = _pdf_text(self._article, [1])
                words = _words(pages[0]) if pages else set()
                self._article_words = words or None
        return self._article_words


def _verdict_for(
    ctx: _Context, item: SiItem, seen: dict[str, str]
) -> Verdict:
    size = item.path.stat().st_size
    sha = _sha256_file(item.path)

    def rejected(reason: str, other: str | None = None) -> Verdict:
        return Verdict("rejected", None, reason, other, sha, size)

    # Empty: a download that never finished. Before a, so two empty files are
    # each refused as empty, not as copies of each other.
    if size == 0:
        return rejected("empty file")

    # a — already here (recorded, or earlier in this same batch)
    if sha in ctx.recorded_shas:
        return rejected("already in the library")
    if sha in seen:
        return rejected(f"same file as {seen[sha]}")
    seen[sha] = item.original

    # d, byte for byte: the one way to recognise an article that has no text
    # layer (a scan), which the word comparison below has to skip.
    if size == ctx.article_size() and sha == ctx.article_sha():
        return rejected("this is the article itself")

    pages = _pdf_text(item.path, [1, 2]) if _looks_like_pdf(item.path) else None
    page_words = _words(" ".join(pages)) if pages else set()
    page_key = _title_key(" ".join(pages)) if pages else ""

    # d — the article itself. Before the name rules: a downloaded article is
    # usually named after its DOI and would otherwise pass as SI under c.
    article_words = ctx.article_words() if page_words else None
    if pages and article_words:
        first_page = _words(pages[0])
        union = first_page | article_words
        jaccard = len(first_page & article_words) / len(union)
        if first_page and jaccard >= _ARTICLE_JACCARD:
            return rejected("this is the article itself")

    # b / c — the DOI number in the file name. The two conditions exclude
    # each other (b needs this paper's number absent, c needs it present).
    if ctx.paper.key.strength(item.original):
        return Verdict(
            "ok", "filename", "name matches the paper's DOI", None, sha, size
        )
    named_other, best_strength = None, 0
    for other in ctx.others:
        strength = other.key.strength(item.original)
        if strength > best_strength:
            named_other, best_strength = other.paper_id, strength
    if named_other is not None:
        return rejected(f"looks like the SI of {named_other}", named_other)

    # e / f — the title on the first pages. A short title is matched by any SI
    # from the same field, so it decides nothing either way.
    if page_words and len(ctx.paper.title_words) >= _MIN_TITLE_WORDS:
        if ctx.paper.title_key in page_key:
            return Verdict(
                "ok", "title", "first pages carry the paper's title",
                None, sha, size,
            )
        # The longest title found: a title inside a longer one is on the page
        # whenever that one is.
        titled_other, longest = None, 0
        for other in ctx.others:
            if len(other.title_words) < _MIN_TITLE_WORDS:
                continue
            if other.title_key in page_key and len(other.title_key) > longest:
                titled_other, longest = other.paper_id, len(other.title_key)
        if titled_other is not None:
            return rejected(f"looks like the SI of {titled_other}", titled_other)

    # g
    return Verdict(
        "unverified", None, "can't confirm it belongs to this paper",
        None, sha, size,
    )


def verify_batch(
    vault: Path,
    paper_id: str,
    items: Sequence[SiItem],
    *,
    index_papers: Sequence[Mapping[str, Any]] | None = None,
) -> list[Verdict]:
    """One :class:`Verdict` per item, in order (an empty file first, then rules
    a → d → b → c → e → f → g).

    The second of two identical files in one batch is rejected. Other papers'
    DOIs and titles come from ``index_papers`` (INDEX projections suffice);
    ``None`` reads the verified INDEX, falling back to a scan.
    """
    meta = find_paper(vault, paper_id)
    ctx = _Context(vault, paper_id, meta, index_papers)
    seen: dict[str, str] = {}
    return [_verdict_for(ctx, item, seen) for item in items]


def verify_candidate(
    vault: Path,
    paper_id: str,
    path: Path,
    original_name: str,
    *,
    index_papers: Sequence[Mapping[str, Any]] | None = None,
) -> Verdict:
    """:func:`verify_batch` for a single file."""
    return verify_batch(
        vault, paper_id, [SiItem(path, original_name)], index_papers=index_papers
    )[0]


# ---------------------------------------------------------------------------
# Where a source may come from
# ---------------------------------------------------------------------------


def _entry_kind(path: Path) -> str:
    """What sits at ``path``, never following a link.

    ``"missing"``, ``"file"``, ``"dir"``, ``"link"`` (a symlink, or a Windows
    junction — which Python does not call a symlink) or ``"other"``. Raises
    ``OSError`` when the entry cannot even be looked at.
    """
    try:
        mode = os.lstat(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    if stat.S_ISLNK(mode) or path.is_junction():
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def _folder_problem(vault: Path, paper_id: str) -> str | None:
    """``"link"`` / ``"file"`` when ``supplementary`` is no usable folder.

    A link (symlink or junction) leads to some other folder on the machine:
    litman neither reads, locks nor writes through it. A file where the
    folder belongs would make a commit fail halfway, after its decision point.
    """
    kind = _entry_kind(vault / "papers" / paper_id / SI_DIRNAME)
    if kind == "link":
        return "link"
    if kind in ("file", "other"):
        return "file"
    return None


def _refuse_bad_folder(vault: Path, paper_id: str) -> None:
    """Refuse up front — before anything is staged — to use a bad folder."""
    where = f"papers/{paper_id}/supplementary"
    try:
        problem = _folder_problem(vault, paper_id)
    except OSError as exc:
        raise SupplementaryError(
            f"{where} cannot be read ({exc.__class__.__name__}). "
            "Fix its permissions, then retry."
        ) from exc
    if problem == "link":
        raise SupplementaryError(
            f"{where} is a link, and litman does not write through links. "
            "Replace it with a real folder, then retry."
        )
    if problem == "file":
        raise SupplementaryError(
            f"{where} is a file, not a folder. "
            "Move it out of the paper folder, then retry."
        )


def _vault_parts(vault: Anchor, path: Path) -> tuple[str, ...] | None:
    """``path``'s parts below the library root, ``None`` when outside it.

    Decided by identity — each ancestor of the resolved path against the
    library's (:class:`~litman.core.fs_identity.Anchor`) — not by comparing
    strings: on a case-insensitive filesystem a path typed in another case
    reaches the same folder, yet spells it differently. Only on a volume
    without inode numbers do the resolved paths decide. The parts keep the
    spelling found, so the allow-list below refuses a differently-spelled
    path rather than admit it.
    """
    current = path.resolve()
    below: list[str] = []
    while True:
        if vault.matches(current):
            return tuple(reversed(below))
        if current.parent == current:
            return None
        below.append(current.name)
        current = current.parent


def _source_problem(
    vault: Anchor, paper_id: str, path: Path, recorded: set[str]
) -> str | None:
    """Why ``path`` may not be taken in, or ``None``.

    Outside the library anything goes. Inside it, only two places hold files
    that are meant to move into a paper's SI: the GUI's upload stash, and
    this paper's own ``supplementary/`` folder for a file nobody recorded
    yet. Anything else — another paper's PDF, a trashed paper, a sidecar —
    would be deleted by the move. Links are resolved first, so a
    project's ``litman_reflib/<paper>/paper.pdf`` counts as inside.
    """
    if not path.exists():
        return f"No such file: {path}"
    if path.is_dir():
        return f"{path} is a folder — add its files one by one, or zip it first."
    parts = _vault_parts(vault, path)
    if parts is None:
        return None
    if len(parts) == 2 and parts[0] == _UPLOAD_DIRNAME:
        return None
    if (
        len(parts) == 4
        and parts[:3] == ("papers", paper_id, SI_DIRNAME)
        and parts[3] not in recorded
        and is_si_candidate_name(parts[3])
    ):
        return None
    return f"{path} is inside the library — copy it somewhere outside first."


def prepare_items(
    vault: Path, paper_id: str, paths: Sequence[Path]
) -> list[SiItem]:
    """Check that ``paths`` may be added to the paper and name them.

    Raises:
        PaperNotFoundError: no such paper.
        SupplementaryError: the paper's ``supplementary`` is a link or a
            file, or a path is missing, a folder, or sits where a move out of
            it would lose something (see :func:`_source_problem`).
    """
    meta = find_paper(vault, paper_id)
    _refuse_bad_folder(vault, paper_id)
    recorded = {rec["file"] for rec in list_supplementary(meta)}
    anchor = Anchor.of(vault)
    items: list[SiItem] = []
    for path in paths:
        problem = _source_problem(anchor, paper_id, path, recorded)
        if problem:
            raise SupplementaryError(problem)
        items.append(SiItem(path, path.name))
    return items


# ---------------------------------------------------------------------------
# What the GUI's SI window asks before and around a write
# ---------------------------------------------------------------------------


def official_link(meta: Mapping[str, Any]) -> str | None:
    """The publisher's page for the paper, where its SI is downloaded.

    ``https://doi.org/<doi>`` when it has a DOI, else
    ``https://arxiv.org/abs/<id>`` when it has an arXiv id, else ``None``.
    """
    doi = canonicalize_doi(str(meta.get("doi") or ""))
    if doi:
        return f"https://doi.org/{quote(doi, safe='/')}"
    arxiv = str(meta.get("arxiv-id") or "").strip()
    if arxiv:
        return f"https://arxiv.org/abs/{quote(arxiv, safe='/')}"
    return None


def si_write_problem(vault: Path, paper_id: str) -> str | None:
    """Why no SI can be added to or removed from the paper now, or ``None``.

    The words :func:`add_supplementary` would refuse with: a malformed
    ``supplementary`` field, or ``supplementary`` being a link or a file.

    Raises:
        PaperNotFoundError / CorruptMetadataError: no such paper, or its
            metadata does not parse.
    """
    try:
        _load_rt(vault, paper_id)
        _refuse_bad_folder(vault, paper_id)
    except SupplementaryError as exc:
        return str(exc)
    return None


def recorded_si_file(
    vault: Path, paper_id: str, name: str
) -> tuple[Path, dict[str, Any]]:
    """The recorded SI file ``name`` (exactly as recorded) and its record.

    Only a recorded name is ever turned into a path, so nothing else in the
    folder — let alone outside it — can be reached through this.

    Raises:
        PaperNotFoundError / CorruptMetadataError: no such paper, or its
            metadata does not parse.
        SupplementaryError: no record by that name, ``supplementary`` is a
            link or a file, or the file is gone or not a plain file.
    """
    meta = find_paper(vault, paper_id)
    record = None
    if _SI_NAME_RE.match(name):
        record = next(
            (rec for rec in list_supplementary(meta) if rec["file"] == name),
            None,
        )
    if record is None:
        raise SupplementaryError(
            f"{paper_id} has no SI file {name!r}. "
            f"`lit show {paper_id}` lists its SI files."
        )
    _refuse_bad_folder(vault, paper_id)
    path = vault / "papers" / paper_id / SI_DIRNAME / name
    try:
        kind = _entry_kind(path)
    except OSError as exc:
        raise SupplementaryError(
            f"papers/{paper_id}/{SI_DIRNAME}/{name} cannot be read "
            f"({exc.__class__.__name__}). Fix its permissions, then retry."
        ) from exc
    if kind != "file":
        raise SupplementaryError(
            f"papers/{paper_id}/{SI_DIRNAME}/{name} is "
            f"{'missing' if kind == 'missing' else 'not a plain file'}. "
            "`lit health-check` says how to fix it."
        )
    return path, record


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _load_rt(vault: Path, paper_id: str) -> Any:
    meta_path = vault / "papers" / paper_id / "metadata.yaml"
    if not meta_path.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )
    meta = load_yaml_or_raise(meta_path, _yaml_rt)
    if not isinstance(meta, dict):
        raise SupplementaryError(
            f"papers/{paper_id}/metadata.yaml is empty or not a mapping. "
            "Restore it, then retry."
        )
    problems = field_problems(meta.get(SUPPLEMENTARY_FIELD))
    if problems:
        raise SupplementaryError(
            f"papers/{paper_id}/metadata.yaml: 'supplementary' {problems[0]}. "
            "Fix it by hand, then retry."
        )
    return meta


def _dump(meta: Any) -> str:
    buf = io.StringIO()
    _yaml_rt.dump(meta, buf)
    return buf.getvalue()


def _commit_metadata(
    vault: Path,
    paper_id: str,
    meta: Any,
    op_id: str,
    stage_files: Callable[[Any], None] | None = None,
) -> list[dict[str, Any]]:
    """Stage ``meta`` (+ any files) and the INDEX it implies; commit together.

    Returns the paper list the INDEX was rendered from, for the views/INDEX
    funnel afterwards.
    """
    all_papers = papers_for_index(vault, drop_ids={paper_id}, add_metas=(meta,))
    index_json = render_index(all_papers, now_iso())
    with staged_write(vault, op_id=op_id) as stage:
        if stage_files is not None:
            stage_files(stage)
        stage.write_text(f"papers/{paper_id}/metadata.yaml", _dump(meta))
        stage.write_text("INDEX.json", index_json)
    return all_papers


def _reconcile(
    vault: Path, paper_id: str, meta: Any, all_papers: list[dict[str, Any]]
) -> None:
    # Lazy: correctors imports checks, and checks reaches back into this
    # module — at the top of the file the three would be a load-time cycle.
    from litman.core.correctors import reconcile_derived

    # Only updated-at changed among the projected fields; no view bucket moves.
    snapshot = view_fields_snapshot(meta)
    reconcile_derived(
        vault,
        papers=all_papers,
        project_refs=False,
        views_delta=[(paper_id, snapshot, snapshot)],
    )


def _stamp_mtime(path: Path, timestamp: float) -> None:
    """Set ``path``'s mtime to the batch's ``added`` stamp (see _content_changed)."""
    os.utime(path, (timestamp, timestamp))


def _refusal(verdicts: list[tuple[SiItem, Verdict]]) -> str:
    detail = "; ".join(f"{item.original}: {v.reason}" for item, v in verdicts)
    noun = "file" if len(verdicts) == 1 else "files"
    return f"Refused {len(verdicts)} {noun} ({detail}). Nothing was added."


def add_supplementary(
    vault: Path,
    paper_id: str,
    items: Sequence[SiItem],
    *,
    allow_unverified: bool,
) -> SiAddResult:
    """Verify ``items`` and add them to the paper's SI in one staged write.

    All or nothing: the files (as ``supplementary/si-NN.ext``) and the
    metadata records land together with the re-rendered INDEX, or none of
    them do. The promotion locks each SI file read-only. A stray already named
    ``si-NN.*`` in the paper's own folder is recorded where it is, keeping the
    number notes may already cite — unless a record or another stray of the
    batch holds that number. Records are kept
    in number order. Sources are removed only after the commit (``lit add``'s
    mv semantics); one that cannot be is a warning.

    Raises:
        PaperNotFoundError / CorruptMetadataError: no such paper, or its
            metadata does not parse.
        SupplementaryError: the field is malformed, ``supplementary`` is a
            link or a file, a source is not acceptable (see
            :func:`prepare_items`), a file is rejected, or one cannot be
            confirmed and neither ``allow_unverified`` nor its own
            ``approved`` is set. Nothing is written.
    """
    meta = _load_rt(vault, paper_id)
    _refuse_bad_folder(vault, paper_id)
    records = list_supplementary(meta)
    recorded = {rec["file"] for rec in records}
    anchor = Anchor.of(vault)
    for item in items:
        problem = _source_problem(anchor, paper_id, item.path, recorded)
        if problem:
            raise SupplementaryError(problem)
    if not items:
        return SiAddResult([], [])

    verdicts = verify_batch(vault, paper_id, items)
    pairs = list(zip(items, verdicts, strict=True))
    refused = [(i, v) for i, v in pairs if v.status == "rejected"]
    if refused:
        raise SupplementaryError(_refusal(refused))
    unconfirmed = [
        i.original
        for i, v in pairs
        if v.status == "unverified" and not i.approved
    ]
    if unconfirmed and not allow_unverified:
        raise SupplementaryError(
            f"Can't confirm {', '.join(unconfirmed)} belongs to {paper_id}. "
            "Nothing was added; pass --force to add it anyway."
        )

    si_dir = vault / "papers" / paper_id / SI_DIRNAME
    si_dir_resolved = si_dir.resolve()
    taken_numbers = _numbers_in(recorded)
    in_place: dict[int, str] = {}
    for index, item in enumerate(items):
        resolved = item.path.resolve()
        match = _SI_NAME_RE.match(resolved.name)
        if (
            match is None
            or item.path.is_symlink()
            or resolved.parent != si_dir_resolved
            or int(match.group(1)) in taken_numbers
        ):
            continue
        taken_numbers.add(int(match.group(1)))
        in_place[index] = resolved.name
    moving = [i for i in range(len(items)) if i not in in_place]
    planned = plan_names(
        _taken_names(si_dir, records),
        [items[i].original for i in moving],
    )
    names = dict(in_place)
    names.update(zip(moving, planned, strict=True))

    added = now_iso()
    added_ts = datetime.fromisoformat(added).timestamp()
    new_records = _sorted_records(
        {
            "file": names[index],
            "original": item.original,
            "sha256": verdict.sha256,
            "bytes": verdict.size,
            "added": added,
            "check": verdict.check or "user",
        }
        for index, (item, verdict) in enumerate(pairs)
    )
    current = meta.get(SUPPLEMENTARY_FIELD)
    meta[SUPPLEMENTARY_FIELD] = _sorted_records(
        [*(current if isinstance(current, list) else []), *new_records]
    )
    meta["updated-at"] = added

    def stage_files(stage: Any) -> None:
        for index in moving:
            item, verdict = pairs[index]
            staged = stage.copy_file(
                f"papers/{paper_id}/{SI_DIRNAME}/{names[index]}", item.path
            )
            if _sha256_file(staged) != verdict.sha256:
                raise SupplementaryError(
                    f"{item.original} changed while it was being added. "
                    "Nothing was added; try again."
                )
            # The content check in health-check skips re-hashing a file whose
            # mtime still equals its record's `added`; os.replace keeps it.
            _stamp_mtime(staged, added_ts)

    try:
        for name in in_place.values():
            unlock_truth_file(si_dir / name)
            _stamp_mtime(si_dir / name, added_ts)
        all_papers = _commit_metadata(
            vault, paper_id, meta, f"si-add-{paper_id}", stage_files
        )
    finally:
        for name in in_place.values():
            lock_truth_file(si_dir / name)

    warnings: list[str] = []
    try:
        _reconcile(vault, paper_id, meta, all_papers)
    except Exception as exc:
        warnings.append(
            f"INDEX/views rebuild failed ({exc.__class__.__name__}); the SI "
            "was added. Run `lit refresh-views` to reconcile."
        )

    for index in moving:
        source = items[index].path
        try:
            resolved = source.resolve()
            parts = _vault_parts(anchor, source)
            if parts is not None:
                # From inside the library (the upload stash, or a stray in
                # this paper's own folder), possibly through a link: the real
                # file goes, then the link. Removing only the link would leave
                # the file behind as a stray of the very SI just recorded.
                # A stray was locked by the re-lock sweep, and Windows refuses
                # to unlink a read-only file.
                if parts[:3] == ("papers", paper_id, SI_DIRNAME):
                    unlock_truth_file(resolved)
                resolved.unlink()
                if source.is_symlink():
                    source.unlink()
                continue
            source.unlink()
        except OSError as exc:
            warnings.append(
                f"could not remove source file {str(source)!r} "
                f"({exc.__class__.__name__}); the SI was still added. "
                "Delete it manually if no longer needed."
            )

    return SiAddResult(
        records=new_records,
        paths=[si_dir / record["file"] for record in new_records],
        warnings=warnings,
    )


def _refuse_folders(
    paper_id: str, si_dir: Path, records: Iterable[Mapping[str, Any]]
) -> None:
    """Refuse up front to delete a recorded name that now holds a folder.

    Deleting it would take whatever the user put in there; deleting nothing
    but its record would hide it.
    """
    for rec in records:
        where = f"papers/{paper_id}/{SI_DIRNAME}/{rec['file']}"
        try:
            kind = _entry_kind(si_dir / rec["file"])
        except OSError as exc:
            raise SupplementaryError(
                f"{where} cannot be read ({exc.__class__.__name__}). "
                "Fix its permissions, then retry."
            ) from exc
        if kind == "dir":
            raise SupplementaryError(
                f"{where} is a folder, not the recorded file — nothing was "
                "deleted. Move it out of supplementary/, then retry."
            )


def _drop_records(meta: Any, files: set[str]) -> bool:
    """Take the records of ``files`` out of ``meta``; whether any are left."""
    keep = [
        entry
        for entry in meta[SUPPLEMENTARY_FIELD]
        if not (isinstance(entry, Mapping) and entry.get("file") in files)
    ]
    if keep:
        meta[SUPPLEMENTARY_FIELD] = keep
    else:
        del meta[SUPPLEMENTARY_FIELD]
    meta["updated-at"] = now_iso()
    return bool(keep)


def _delete_si_entry(path: Path, kind: str) -> None:
    """Delete one recorded SI entry for good (``kind`` from :func:`_entry_kind`).

    A plain file is unlocked first — Windows refuses to delete a read-only
    file — and locked again when it still cannot go. A link is removed, never
    what it points at; a Windows junction only goes as a directory.
    """
    if kind == "file":
        unlock_truth_file(path)
        try:
            path.unlink()
        except OSError:
            lock_truth_file(path)
            raise
        return
    if kind == "link":
        try:
            path.unlink()
        except OSError as exc:
            # The rmdir is only for a junction; when it fails too, the
            # unlink's error is the one that says why.
            try:
                path.rmdir()
            except OSError:
                raise exc from None
        return
    if kind == "dir":
        raise IsADirectoryError(f"{path} is a folder")
    path.unlink()


def preview_si_removal(
    vault: Path, paper_id: str, names: Iterable[str]
) -> SiRemovalPreview:
    """What :func:`remove_supplementary` would delete, and where the notes cite it.

    Read-only. Refuses as the delete would before writing anything — an
    unknown name, a malformed field, ``supplementary`` being a link or a file,
    a folder at a recorded name — so no confirmation is ever shown for a
    delete that cannot happen.

    Raises:
        PaperNotFoundError / CorruptMetadataError: no such paper, or its
            metadata does not parse.
        SupplementaryError: as above.
    """
    from litman.core.si_citations import count_citations, read_si_citations

    meta = _load_rt(vault, paper_id)
    _refuse_bad_folder(vault, paper_id)
    records = resolve_si_records(paper_id, meta, names)
    paper_dir = vault / "papers" / paper_id
    _refuse_folders(paper_id, paper_dir / SI_DIRNAME, records)
    citations, unchecked = read_si_citations(paper_dir)
    cited: dict[str, dict[str, int]] = {}
    for rec in records:
        counts = count_citations(citations, rec)
        if counts:
            cited[rec["file"]] = counts
    return SiRemovalPreview(records=records, cited=cited, unchecked=unchecked)


def remove_supplementary(
    vault: Path, paper_id: str, names: Iterable[str]
) -> SiRemoveResult:
    """Delete SI files for good and drop their records.

    There is no trash for SI: the way back is adding the file again. The
    records go first, in one staged write with the INDEX; then each file is
    deleted (a link is removed, never followed). A file that is already gone
    just loses its record. If a file cannot be deleted, it is locked again and
    its record written back, so no record ever points at nothing. The folder
    is removed once nothing but file-manager droppings is left in it; any
    other file keeps it.

    Raises:
        SupplementaryError: an unknown name, a malformed field,
            ``supplementary`` being a link or a file, or a folder at a
            recorded name (nothing is written); or a file that could not be
            deleted (its record is restored).
    """
    meta = _load_rt(vault, paper_id)
    _refuse_bad_folder(vault, paper_id)
    chosen = resolve_si_records(paper_id, meta, names)
    if not chosen:
        return SiRemoveResult(records=[])
    si_dir = vault / "papers" / paper_id / SI_DIRNAME
    _refuse_folders(paper_id, si_dir, chosen)
    # Records before files: the likeliest failures — another write holding
    # this paper's staging op, a full disk — hit this commit, before anything
    # is destroyed. A delete that fails after it is undone by writing the
    # record back; only a second failure leaves a file without its record
    # (a warning `--fix` records again), never a record without its file.
    kept = _drop_records(meta, {rec["file"] for rec in chosen})
    all_papers = _commit_metadata(vault, paper_id, meta, f"si-rm-{paper_id}")

    deleted: list[str] = []
    gone: list[str] = []
    failed: list[tuple[dict[str, Any], OSError]] = []
    for rec in chosen:
        path = si_dir / rec["file"]
        try:
            kind = _entry_kind(path)
            if kind == "missing":
                gone.append(rec["file"])
                continue
            _delete_si_entry(path, kind)
            deleted.append(rec["file"])
        except OSError as exc:
            failed.append((rec, exc))

    if failed:
        names_failed = ", ".join(rec["file"] for rec, _ in failed)
        first = failed[0][1]
        one = len(failed) == 1
        try:
            restored = _load_rt(vault, paper_id)
            current = restored.get(SUPPLEMENTARY_FIELD)
            restored[SUPPLEMENTARY_FIELD] = _sorted_records(
                [
                    *(current if isinstance(current, list) else []),
                    *(rec for rec, _ in failed),
                ]
            )
            restored["updated-at"] = now_iso()
            all_papers = _commit_metadata(
                vault, paper_id, restored, f"si-rm-undo-{paper_id}"
            )
            meta = restored
        except Exception as exc:
            raise SupplementaryError(
                f"Could not delete {names_failed} ({first.__class__.__name__}), "
                f"nor put {'its record' if one else 'their records'} back "
                f"({exc.__class__.__name__}): the "
                f"{'file is' if one else 'files are'} still in supplementary/. "
                "Run `lit health-check --fix` to record "
                f"{'it' if one else 'them'} again."
            ) from exc
        went = f" Deleted: {', '.join(deleted)}." if deleted else ""
        try:
            _reconcile(vault, paper_id, meta, all_papers)
        except Exception as exc:
            went += (
                f" INDEX/views rebuild failed ({exc.__class__.__name__}); run "
                "`lit refresh-views` to reconcile."
            )
        raise SupplementaryError(
            f"Could not delete {names_failed} ({first.__class__.__name__}); "
            f"{'its record is' if one else 'their records are'} back. Close "
            f"anything that has {'it' if one else 'them'} open, then retry."
            f"{went}"
        ) from first

    if not kept:
        _remove_empty_folder(si_dir)
    warnings: list[str] = []
    try:
        _reconcile(vault, paper_id, meta, all_papers)
    except Exception as exc:
        what = "file was" if len(chosen) == 1 else "files were"
        warnings.append(
            f"INDEX/views rebuild failed ({exc.__class__.__name__}); the SI "
            f"{what} deleted. Run `lit refresh-views` to reconcile."
        )
    return SiRemoveResult(records=chosen, gone=gone, warnings=warnings)


def drop_missing_records(
    vault: Path, paper_id: str, names: Iterable[str]
) -> list[str]:
    """Drop the records of SI files that are gone; never delete a file.

    ``health-check --fix`` asks before dropping, and a file can come back
    while it waits for the answer, so each one is looked at again just before
    the commit: a record whose file is back is kept. Returns the names
    dropped.
    """
    meta = _load_rt(vault, paper_id)
    _refuse_bad_folder(vault, paper_id)
    si_dir = vault / "papers" / paper_id / SI_DIRNAME
    dead = [
        rec["file"]
        for rec in resolve_si_records(paper_id, meta, names)
        if _entry_kind(si_dir / rec["file"]) == "missing"
    ]
    if not dead:
        return []
    kept = _drop_records(meta, set(dead))
    all_papers = _commit_metadata(vault, paper_id, meta, f"si-rm-{paper_id}")
    if not kept:
        _remove_empty_folder(si_dir)
    _reconcile(vault, paper_id, meta, all_papers)
    return dead


def _remove_empty_folder(si_dir: Path) -> None:
    """Remove ``supplementary/`` once only file-manager droppings are left.

    Those (``.DS_Store``, ``._*``, ``Thumbs.db``, ``desktop.ini``) are the
    only files deleted with it. Anything else — a hidden note of the user's
    included — keeps the folder: what is deleted here cannot be got back.
    """
    try:
        leftovers = os.listdir(si_dir)
        if not all(is_file_manager_dropping(name) for name in leftovers):
            return
        for name in leftovers:
            (si_dir / name).unlink()
        si_dir.rmdir()
    except OSError:
        pass  # a dropping that will not go (or the folder is gone already)


# ---------------------------------------------------------------------------
# Drift: records ↔ folder (health-check)
# ---------------------------------------------------------------------------


def _content_changed(path: Path, record: Mapping[str, Any]) -> bool:
    """Size first; the hash only when the mtime moved away from ``added``."""
    found = path.stat()
    if found.st_size != record["bytes"]:
        return True
    added = _timestamp_of(record.get("added"))
    if added is not None and abs(found.st_mtime - added) <= _MTIME_SLACK_S:
        return False
    return _sha256_file(path) != str(record["sha256"])


def supplementary_drift(
    vault: Path, paper_id: str, meta: Mapping[str, Any]
) -> SiDrift:
    """Compare a paper's ``supplementary`` records with its folder.

    Nothing is followed through a link. With a malformed field, or with
    ``supplementary`` a link or a file, only that is reported: which files
    are accounted for, or where, cannot be known until it is fixed. What
    cannot be read (permissions) is reported as unchecked rather than raised,
    so one locked-down folder never stops a health check or a push.
    """
    problems = field_problems(meta.get(SUPPLEMENTARY_FIELD))
    if problems:
        return SiDrift(problems=problems)
    drift = SiDrift()
    try:
        drift.folder = _folder_problem(vault, paper_id)
    except OSError as exc:
        drift.unchecked.append(("", exc.__class__.__name__))
        return drift
    if drift.folder is not None:
        return drift

    si_dir = vault / "papers" / paper_id / SI_DIRNAME
    records = list_supplementary(meta)
    for rec in records:
        name = rec["file"]
        try:
            kind = _entry_kind(si_dir / name)
            if kind == "missing":
                drift.missing.append(name)
            elif kind != "file":
                drift.odd_recorded.append((name, kind))
            elif not has_checksum(rec):
                drift.unchecked.append((name, "no checksum"))
            elif _content_changed(si_dir / name, rec):
                drift.changed.append(name)
        except OSError as exc:
            drift.unchecked.append((name, exc.__class__.__name__))

    recorded = {rec["file"] for rec in records}
    try:
        entries = sorted(os.listdir(si_dir))
    except (FileNotFoundError, NotADirectoryError):
        entries = []
    except OSError as exc:
        drift.unchecked.append(("", exc.__class__.__name__))
        entries = []
    for name in entries:
        if name in recorded or not is_si_candidate_name(name):
            continue
        try:
            kind = _entry_kind(si_dir / name)
        except OSError as exc:
            drift.unchecked.append((name, exc.__class__.__name__))
            continue
        if kind == "file":
            drift.unrecorded.append(name)
        elif kind != "missing":
            drift.odd_strays.append((name, kind))
    return drift


def fix_supplementary(
    vault: Path, paper_ids: Iterable[str], *, policy: SiFixPolicy
) -> dict[str, int]:
    """``health-check --fix`` for SI: drop dead records, record strays.

    Per paper: records whose file is gone are dropped if
    ``policy.confirm_drop`` agrees — the records only: a file that is back by
    then keeps its record, and no SI file is ever deleted here. Each stray
    plain file is verified — ✓ is recorded, ? only if
    ``policy.confirm_unverified`` agrees, ✕ never (the policy hears about
    it). A paper whose field is malformed, or whose
    ``supplementary`` is a link or a file, is skipped, and so is anything in
    the folder that is not a plain file; their findings stay. Best-effort
    per paper, like the other ``--fix`` arms.
    """
    dropped = recorded = 0
    for paper_id in sorted(set(paper_ids)):
        try:
            meta = find_paper(vault, paper_id)
            drift = supplementary_drift(vault, paper_id, meta)
            if drift.problems or drift.folder is not None:
                continue
            if drift.missing and policy.confirm_drop(paper_id, drift.missing):
                dropped += len(
                    drop_missing_records(vault, paper_id, drift.missing)
                )
            si_dir = vault / "papers" / paper_id / SI_DIRNAME
            strays = [SiItem(si_dir / name, name) for name in drift.unrecorded]
            if not strays:
                continue
            accepted: list[SiItem] = []
            confirmed_by_user = False
            verdicts = verify_batch(vault, paper_id, strays)
            for item, verdict in zip(strays, verdicts, strict=True):
                if verdict.status == "ok":
                    accepted.append(item)
                elif verdict.status == "unverified":
                    if policy.confirm_unverified(paper_id, item.path, verdict):
                        accepted.append(item)
                        confirmed_by_user = True
                else:
                    policy.on_rejected(paper_id, item.path, verdict)
            if accepted:
                add_supplementary(
                    vault, paper_id, accepted, allow_unverified=confirmed_by_user
                )
                recorded += len(accepted)
        except (OSError, LitmanError):
            # The finding stays, and the post-fix report shows it again.
            continue
    return {
        "supplementary_missing": dropped,
        "supplementary_unrecorded": recorded,
    }


__all__ = [
    "LARGE_FILE_BYTES",
    "NO_PROMPT_POLICY",
    "SUPPLEMENTARY_FIELD",
    "SiAddResult",
    "SiDrift",
    "SiFixPolicy",
    "SiItem",
    "SiRemovalPreview",
    "SiRemoveResult",
    "Verdict",
    "add_supplementary",
    "doi_tokens",
    "drop_missing_records",
    "field_problems",
    "fix_supplementary",
    "has_checksum",
    "is_runnable_si",
    "is_unprintable",
    "list_supplementary",
    "mentions_supplementary",
    "name_matches",
    "official_link",
    "original_label",
    "plan_names",
    "prepare_items",
    "preview_si_removal",
    "recorded_si_file",
    "remove_supplementary",
    "resolve_si_records",
    "shell_path",
    "si_extension",
    "si_stem",
    "si_write_problem",
    "supplementary_drift",
    "verify_batch",
    "verify_candidate",
]
