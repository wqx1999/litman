"""No file in the repository carries the maintainer's personal information.

The repository is public and the wheel ships ``src/litman``, so a name, a
machine path or a private mailbox in a comment is as exposed as one in a help
text. The checks below are the kinds of leak the privacy audit found. Words
that would identify someone are kept as SHA-256 digests, so this file does not
publish what it guards; to add one, put ``digest("<lowercase word>")`` in WORDS.
ALLOWED names the few spots where a match is deliberate (the copyright line,
the citation file). New files count before they are added, so a hit fails
with its file:line before it is committed.
"""

from __future__ import annotations

import functools
import hashlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SELF = "tests/test_no_personal_info.py"


@functools.cache
def digest(word: str) -> str:
    return hashlib.sha256(word.encode("utf-8")).hexdigest()[:16]


#: Digest of a lowercase word -> the kind of leak it is.
WORDS: dict[str, str] = {
    "3f3b685181b41ce2": "maintainer handle",
    "a2d923d1f3e4834c": "personal name",
    "135a78ef1553edd0": "personal name",
    "5b0f85b88fad3b00": "personal name",
    "5e1389391b7ba9bc": "research project",
    "666f50bd2dbdfeb9": "research project",
    "6b926f2ef33f882a": "HPC partition",
    "c9e9f3c7f429f8a8": "HPC partition",
    "a17556cc009bd875": "HPC partition",
}
#: Digests of user names that must not appear as a home folder
#: (``C:\\Users\\<name>``, ``/home/<name>``, ``/Users/<name>``).
HOMES: frozenset[str] = frozenset({"2ac8129957801379", "3f3b685181b41ce2"})

#: Kind of leak -> a pattern that names no one (matched on the lowercased line).
PATTERNS: dict[str, str] = {
    "machine path": r"/net/work/|/homes/|/scratch/|miniconda3/envs/",
    "HPC node": r"\bnode\d{3,4}\b",
    "GitHub owner outside a repo link": r"wqx1999(?!/litman)",
}
WORD = re.compile(r"[a-z0-9]+")
HOME = re.compile(r"(?:\b[a-z]:[\\/]+users|/home|/users)[\\/]+([a-z0-9._-]+)")
EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
#: The project's own address, GitHub, and the reserved example domains.
OK_EMAIL = re.compile(
    r"^contact@litman\.dev$|@github\.com$|@(?:[\w-]+\.)*example\.(?:com|org|net)$", re.I
)

#: (path, kind) where a match is deliberate. Each one is the maintainer's call.
ALLOWED: set[tuple[str, str]] = {
    ("CITATION.cff", "personal name"),
    ("README.md", "personal name"),
    ("docs/license.md", "personal name"),
    ("src/litman/__init__.py", "personal name"),
    ("pyproject.toml", "personal name"),
}

#: Third-party files nobody here edits.
VENDOR = (
    "src/litman/assets/webui/wasm/",
    "src/litman/assets/webui/assets/pdf.worker",
    "frontend/package-lock.json",
)


def leaks(
    path: str,
    text: str,
    words: dict[str, str] = WORDS,
    homes: frozenset[str] = HOMES,
) -> list[tuple[int, str, str]]:
    """``(line, kind, match)`` for each leak in ``text``; ``path`` decides what is allowed."""
    found: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        hits = [(words[digest(w)], w) for w in WORD.findall(low) if digest(w) in words]
        hits += [
            ("home folder", m.group(1)) for m in HOME.finditer(low) if digest(m.group(1)) in homes
        ]
        hits += [(kind, m.group(0)) for kind, rx in PATTERNS.items() for m in re.finditer(rx, low)]
        hits += [
            ("e-mail", m.group(0)) for m in EMAIL.finditer(line) if not OK_EMAIL.search(m.group(0))
        ]
        found += [(number, kind, match) for kind, match in hits if (path, kind) not in ALLOWED]
    return found


def repo_files() -> list[str]:
    """Files git tracks, plus new ones it does not ignore."""
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    paths = listing.decode("utf-8").split("\0")
    return [p for p in paths if p and p != SELF and not p.startswith(VENDOR)]


def test_no_file_in_the_repository_carries_personal_information() -> None:
    hits: dict[str, list[tuple[int, str, str]]] = {}
    for rel in repo_files():
        path = ROOT / rel
        if not path.is_file():
            continue  # deleted in the working tree, not yet staged
        raw = path.read_bytes()
        if b"\0" in raw[:8192]:
            continue  # binary
        if found := leaks(rel, raw.decode("utf-8", errors="replace")):
            hits[rel] = found
    assert not hits, f"personal information in the repository: {hits}"


def test_the_scan_sees_each_kind_of_leak() -> None:
    """The control, on stand-in words: each kind planted once is reported; allowed spots are not."""
    words = {
        digest("zzhandle"): "maintainer handle",
        digest("zzname"): "personal name",
        digest("zzproject"): "research project",
        digest("zzpartition"): "HPC partition",
    }
    homes = frozenset({digest("zzuser")})
    planted = "\n".join([
        "# decided by zzhandle, 2026-08-08",
        "author: Zzname Doe",
        "#   zzproject: /net/work/x/Project/ZZProject",
        r'codex = r"C:\Users\ZZUser\AppData\Roaming\npm\codex.CMD"',
        "link = PurePosixPath('/home/zzuser/proj')",
        "measured on node123 (zzpartition)",
        "see https://github.com/wqx1999/other-repo",
        'UA = "litman (mailto:someone@gmail.com)"',
    ])

    kinds = {kind for _, kind, _ in leaks("src/litman/x.py", planted, words, homes)}

    assert kinds == set(words.values()) | set(PATTERNS) | {"home folder", "e-mail"}
    assert leaks("README.md", "Copyright (C) 2026 Zzname Doe.", words, homes) == []
    assert leaks("tests/x.py", r'codex = r"C:\Users\alice\AppData"', words, homes) == []
    assert leaks("README.md", "[Issues](https://github.com/wqx1999/litman/issues)") == []
    assert leaks("tests/x.py", '"-c", "user.email=test@example.com"') == []
    assert leaks("src/x.py", "upstream_web_url('https://me:ghp_x@github.com/o/r.git')") == []


def test_every_kind_of_word_has_a_digest() -> None:
    """The real lists are filled: each word-based kind has an entry, every digest is well formed."""
    assert set(WORDS.values()) == {
        "maintainer handle", "personal name", "research project", "HPC partition"
    }
    assert HOMES
    assert all(re.fullmatch(r"[0-9a-f]{16}", d) for d in (*WORDS, *HOMES))
