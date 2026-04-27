"""Read paper metadata.yaml files.

Pure helpers — no side effects, no CLI rendering. ``commands/`` modules call
these to enumerate or look up papers; tests exercise them directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

from litman.core.id import is_valid_id
from litman.exceptions import PaperNotFoundError

_yaml_safe = YAML(typ="safe")


def read_metadata(metadata_path: Path) -> dict[str, Any]:
    """Load a metadata.yaml file as a plain dict.

    Returns an empty dict for a YAML file whose top-level value is null /
    empty / comment-only — caller decides whether that counts as missing.
    """
    text = metadata_path.read_text(encoding="utf-8")
    data = _yaml_safe.load(text)
    return data if data is not None else {}


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
        except (OSError, YAMLError):
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
    return read_metadata(meta_file)
