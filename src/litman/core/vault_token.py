"""Stat-only tokens that tell when a library changed on disk.

Two tokens, one way of making them: a digest of ``mtime_ns:size`` stats. No
file is opened, nothing is parsed.

* :func:`gui_token` answers the page's poll, ``GET /api/vault-version``:
  did anything the page shows change? INDEX.json, TAXONOMY.md,
  lit-config.yaml, the trash folder, the registry, and every paper's
  ``notes.md`` and ``discussion.md``.
* :func:`synced_token` answers the GUI's auto sync: did a file that two-way
  sync carries change here? Every synced file of every paper and code folder,
  plus the folders whose own mtime moves when an entry comes or goes. It reads
  nothing a sync's own rebuild writes (INDEX.json, ``views/``), so a sync
  never sets off the next one.

Each file is statted, not only its folder: a program that rewrites a file in
place (open, truncate, write) moves the file's mtime and not its folder's.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path

from litman.core.config import CONFIG_FILENAME
from litman.core.locking import SI_DIRNAME
from litman.core.trash import TRASH_DIRNAME

#: The library files the page's token stats. INDEX.json covers every paper's
#: metadata (every write command re-derives it), TAXONOMY.md the vocabulary,
#: lit-config.yaml the project map, and the trash folder's own mtime moves
#: whenever an entry goes in or out of it.
GUI_TOKEN_FILES = ("INDEX.json", "TAXONOMY.md", CONFIG_FILENAME, TRASH_DIRNAME)

#: Free-form docs an agent rewrites in place: nothing in INDEX.json moves when
#: it does, so the page's token stats them one by one.
GUI_TOKEN_DOCS = ("notes.md", "discussion.md")

#: What :func:`synced_token` stats at the library's top level: the one synced
#: file there, and the folders whose mtime moves when an entry comes or goes.
_SYNCED_TOP = ("TAXONOMY.md", TRASH_DIRNAME, "papers", "codes")

#: Names two-way sync never carries, at any depth (the anywhere-rules of
#: :func:`litman.core.sync.two_way_filter_rules`), and the conflict copies a
#: sync leaves for an instant. Kept as one pattern so the stat walk stays cheap.
_SKIPPED_NAME_RE = re.compile(
    "|".join(
        fnmatch.translate(pattern)
        for pattern in (
            ".DS_Store",
            "._*",
            "Thumbs.db",
            "desktop.ini",
            "*.partial",
            ".litman-link-probe-*",
            ".litman-write-probe-*",
            "*.sync-conflict[0-9]*",
        )
    )
)

def fingerprint(path: Path | str) -> str:
    """``mtime_ns:size`` for one path, or ``-`` when it is absent or unreadable.

    Absent is a state of its own (no .trash yet, a paper without notes.md) and
    must differ from every present one, so that creating the file moves the
    token.

    Never raises: tokens are computed on a timer, and a path the system refuses
    to stat (a permission, a name the platform rejects, a disconnected network
    drive) reads as not there instead of failing the whole poll.

    Resolution is the filesystem's: far finer than a poll interval on NTFS,
    ext4 and APFS; 2 s on FAT32, where two same-size edits within one bucket
    look like one. The next edit moves it again.
    """
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return "-"
    return f"{st.st_mtime_ns}:{st.st_size}"


def md_version(data: bytes) -> str:
    """The version of a notes or discussion file: the sha256 of its bytes.

    Its content, not its mtime and size: md files are small, and a same-size
    edit inside FAT's 2-second mtime step would otherwise read as no change.
    The GUI sends it back with a save, so a file that changed meanwhile is
    kept before the save replaces it.
    """
    return hashlib.sha256(data).hexdigest()


def _feed(digest: hashlib.blake2b, label: str, value: str) -> None:
    # surrogatepass: a folder whose name is not UTF-8 text still feeds the hash.
    digest.update(f"{label}\0{value}\n".encode("utf-8", "surrogatepass"))


def gui_token(vault: Path, registry: Path) -> str:
    """The token behind ``GET /api/vault-version``: what the page shows changed.

    Cost: one stat per paper document, plus five.
    """
    digest = hashlib.blake2b(digest_size=12)
    for name in GUI_TOKEN_FILES:
        _feed(digest, name, fingerprint(vault / name))
    # The registry lives outside the library and drives the library switcher.
    _feed(digest, "registry", fingerprint(registry))
    papers = vault / "papers"
    try:
        entries = sorted(os.listdir(papers))
    except OSError:
        entries = []
    for paper_id in entries:
        for doc in GUI_TOKEN_DOCS:
            _feed(digest, f"{paper_id}/{doc}", fingerprint(papers / paper_id / doc))
    return digest.hexdigest()


def _every_folder(name: str) -> bool:
    return True


def _feed_folder(
    digest: hashlib.blake2b, label: str, path: str, *, into: Callable[[str], bool]
) -> None:
    """Feed every synced file directly in ``path``, and the folders ``into`` picks in full.

    A folder gone into feeds its own mtime too, which moves when an entry comes
    or goes. Links are skipped, as rclone skips them.
    """
    rows: list[tuple[str, str]] = []
    folders: list[str] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                if _SKIPPED_NAME_RE.match(name):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if into(name):
                            folders.append(name)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rows.append((name, f"{st.st_mtime_ns}:{st.st_size}"))
    except OSError:
        _feed(digest, label, "-")
        return
    rows.sort()
    for name, value in rows:
        _feed(digest, f"{label}/{name}", value)
    for name in sorted(folders):
        sub = os.path.join(path, name)
        _feed(digest, f"{label}/{name}/", fingerprint(sub))
        _feed_folder(digest, f"{label}/{name}", sub, into=_every_folder)


def synced_token(vault: Path) -> str:
    """A token that moves when a file two-way sync carries changes in ``vault``.

    Stats every file directly in each ``papers/<id>/``, the paper's
    ``supplementary/`` folder and everything in it, and every file directly in
    each ``codes/<name>/`` (so never its ``repo/`` checkout); plus TAXONOMY.md and
    the ``.trash``, ``papers`` and ``codes`` folders themselves. A file deeper
    in some other subfolder is not watched; the exact comparison a sync makes
    still finds it.

    Only a trigger: the auto sync confirms a change with the exact snapshot
    comparison before it syncs.
    """
    digest = hashlib.blake2b(digest_size=12)
    root = str(vault)
    for name in _SYNCED_TOP:
        _feed(digest, name, fingerprint(os.path.join(root, name)))
    into_unit: dict[str, Callable[[str], bool]] = {
        "papers": lambda name: name == SI_DIRNAME,
        "codes": lambda name: False,
    }
    for top, into in into_unit.items():
        base = os.path.join(root, top)
        try:
            units = sorted(os.listdir(base))
        except OSError:
            continue
        for unit in units:
            unit_path = os.path.join(base, unit)
            if not os.path.isdir(unit_path) or os.path.islink(unit_path):
                continue
            _feed_folder(digest, f"{top}/{unit}", unit_path, into=into)
    return digest.hexdigest()
