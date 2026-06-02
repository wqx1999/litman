"""Read paper metadata.yaml files.

Pure helpers — no side effects, no CLI rendering. ``commands/`` modules call
these to enumerate or look up papers; tests exercise them directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

from litman.core.id import is_valid_id
from litman.exceptions import CorruptMetadataError, PaperNotFoundError

_yaml_safe = YAML(typ="safe")


def load_yaml_or_raise(path: Path, loader: YAML) -> Any:
    """Load a YAML file with ``loader``, converting read / parse failure into
    a path-carrying :class:`CorruptMetadataError` instead of a raw ``OSError``
    / ``UnicodeDecodeError`` / ruamel ``YAMLError`` traceback.

    Used by the write commands (rename / modify / taxonomy / rm / code) that
    round-trip-load a ``metadata.yaml`` or ``repo-meta.yaml`` before rewriting
    it. A single corrupt or unreadable file — even an *unrelated* one reached
    through a reverse reference — must surface as a friendly error naming the
    file, not abort the command with a traceback. Pass the caller's own
    round-trip ``YAML()`` instance as ``loader`` so comment/quote preservation
    on the subsequent write-back is unchanged.

    Returns the parsed value, which may be ``None`` for an empty /
    comment-only file. Callers that distinguish "empty" from "corrupt" keep
    their existing ``if data is None`` handling — this helper only intercepts
    the *unreadable* / *unparseable* cases.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CorruptMetadataError(path, exc) from exc
    try:
        return loader.load(text)
    except YAMLError as exc:
        raise CorruptMetadataError(path, exc) from exc


def read_metadata(metadata_path: Path) -> dict[str, Any]:
    """Load a metadata.yaml file as a plain dict.

    Returns an empty dict for a YAML file whose top-level value is null /
    empty / comment-only — caller decides whether that counts as missing.
    """
    text = metadata_path.read_text(encoding="utf-8")
    data = _yaml_safe.load(text)
    return data if data is not None else {}


def read_metadata_or_raise(metadata_path: Path) -> dict[str, Any]:
    """Like :func:`read_metadata`, but turn a read / parse failure on a corrupt
    or non-UTF-8 file into a path-naming :class:`CorruptMetadataError` instead
    of a raw ``UnicodeDecodeError`` / ``YAMLError`` traceback.

    For single-paper operations that target one known paper — ``find_paper``,
    ``lit link`` / ``lit unlink`` — where a friendly, actionable error beats a
    stack trace. The tolerant skip-and-continue path for enumerating *many*
    papers stays in :func:`list_papers`, which must not raise on one bad file.
    """
    try:
        return read_metadata(metadata_path)
    except (OSError, UnicodeDecodeError, YAMLError) as exc:
        raise CorruptMetadataError(metadata_path, exc) from exc


def list_papers(vault: Path) -> list[dict[str, Any]]:
    """Enumerate all valid papers under ``vault/papers/``.

    Subdirectories without a ``metadata.yaml`` and files with corrupted YAML
    are silently skipped (the M2 ``lit health-check`` will surface them).
    Returns the list sorted ascending by id (which matches the directory
    traversal order since ids are filesystem-safe).
    """
    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for paper_dir in sorted(papers_dir.iterdir()):
        if not paper_dir.is_dir():
            continue
        meta_file = paper_dir / "metadata.yaml"
        if not meta_file.is_file():
            continue
        try:
            metadata = read_metadata(meta_file)
        except (OSError, YAMLError, UnicodeDecodeError):
            # UnicodeDecodeError (a ValueError subclass, NOT an OSError) is
            # raised by read_text on a metadata.yaml with invalid UTF-8 bytes
            # (cloud-sync conflict copy, external editor). Without it here a
            # single bad file would crash the whole `lit list` / `lit show`.
            # Stays read-only + tolerant by design (M30 OQ4): `list_papers`
            # backs `lit list` / `lit show`, which must never crash on one
            # corrupt paper. This is NOT a silent-skip violation — the
            # corrupt-paper finding is OWNED by
            # `checks.check_paper_dir_validity`, which enumerates `papers/`
            # directly (not via this function) and emits an error for an
            # unparseable / empty metadata.yaml. So a paper dropped here is
            # still reported by `lit health-check`.
            continue
        if not metadata:
            continue
        results.append(metadata)
    return results


def find_paper(vault: Path, paper_id: str) -> dict[str, Any]:
    """Load a single paper's metadata.

    Raises:
        PaperNotFoundError: ``paper_id`` is malformed or no such paper
            exists in the vault.
    """
    if not is_valid_id(paper_id):
        raise PaperNotFoundError(
            f"Invalid paper id: {paper_id!r}. "
            "Ids contain only ASCII letters, digits, dots, underscores, "
            "and hyphens; no leading dot, no slashes, no '..'."
        )

    meta_file = vault / "papers" / paper_id / "metadata.yaml"
    if not meta_file.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )
    return read_metadata_or_raise(meta_file)
