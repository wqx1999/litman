"""Whether two paths name the same file or folder — the one place that decides.

Identity first: ``(st_dev, st_ino)`` is what the filesystem itself says, and
it sees through links, a different case on a case-insensitive disk and any
other spelling of the same entry. Some Windows volumes (network shares,
virtual disks — bpo-33935) report ``st_ino == 0`` for every file on them;
there identity says nothing, and treating every file as "the same" would make
each path on the disk look like the library itself. Only then do the paths
themselves decide, compared resolved and case-folded the way the platform
folds them.

Standard library only: the CLI's import path (``lit add --si``, ``lit gui``)
reaches this module, and :mod:`litman.core.gui_registry` uses it to match a
running GUI's library.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The one seam tests replace to play a volume without inode numbers.
_stat = os.stat


def identity(st: Any) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of a stat result; ``None`` when ``st_ino`` is 0."""
    if st.st_ino == 0:
        return None
    return (st.st_dev, st.st_ino)


def _identity_of(path: Path) -> tuple[int, int] | None:
    try:
        return identity(_stat(path))
    except (OSError, ValueError):
        return None


def _spelling(path: Path) -> str:
    try:
        resolved = str(Path(path).resolve())
    except (OSError, RuntimeError):
        resolved = os.path.abspath(path)
    return os.path.normcase(resolved)


def same_entry(a: Path, b: Path) -> bool:
    """Whether ``a`` and ``b`` reach the same file or folder.

    Compared by identity when both sides have one, otherwise by their
    resolved, case-folded paths.
    """
    ident_a, ident_b = _identity_of(a), _identity_of(b)
    if ident_a is not None and ident_b is not None:
        return ident_a == ident_b
    return _spelling(a) == _spelling(b)


@dataclass(frozen=True)
class Anchor:
    """A folder, taken once, to test many paths against.

    ``ident`` is ``None`` when the folder has no usable identity (gone, or on
    a volume without inode numbers); ``spelling`` is its resolved, case-folded
    path, the fallback.
    """

    path: Path
    ident: tuple[int, int] | None
    spelling: str

    @classmethod
    def of(cls, root: Path) -> Anchor:
        root = Path(root)
        return cls(root, _identity_of(root), _spelling(root))

    def matches(self, path: Path) -> bool:
        """Whether ``path`` is this folder."""
        if self.ident is not None:
            return _identity_of(path) == self.ident
        return _spelling(path) == self.spelling

    def same_as(self, other: Anchor) -> bool:
        """Whether two anchors were taken of the same folder. No I/O."""
        if self.ident is not None and other.ident is not None:
            return self.ident == other.ident
        return self.spelling == other.spelling


__all__ = ["Anchor", "identity", "same_entry"]
