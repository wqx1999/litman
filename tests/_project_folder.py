"""Shared fixture for the tests of a project folder that cannot be read.

``deny`` does to one hub folder what ``chmod 000`` does on POSIX, on every
platform: the folder can still be seen from its parent, but nothing in it can
be listed, read or changed. It works at the ``os`` / ``io`` layer every
filesystem call in litman goes through, so the code under test runs unchanged
— no litman function is patched.
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from litman.core.code import bind_paper_to_repo
from litman.core.config import load_config
from litman.core.library import create_vault
from litman.core.project_link import add_project, link_paper_to_project

ACCESS_DENIED = "Access is denied"


def deny(monkeypatch: pytest.MonkeyPatch, hub: Path) -> None:
    """Refuse every listing of ``hub`` and every call on a path inside it."""
    root = os.path.normcase(os.path.abspath(hub))

    def refused(arg: object, *, listing: bool) -> bool:
        if not isinstance(arg, (str, os.PathLike)):
            return False
        path = os.path.normcase(os.path.abspath(os.fspath(arg)))
        return path.startswith(root + os.sep) or (listing and path == root)

    def guard(real: Callable[..., Any], *, listing: bool = False) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            for arg in args[:2]:
                if refused(arg, listing=listing):
                    raise PermissionError(13, ACCESS_DENIED, os.fspath(arg))  # type: ignore[arg-type]
            return real(*args, **kwargs)

        return call

    for name in (
        "stat", "lstat", "mkdir", "rmdir", "unlink", "remove", "symlink",
        "rename", "replace", "open", "chmod",
    ):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))
    for name in ("listdir", "scandir"):
        monkeypatch.setattr(os, name, guard(getattr(os, name), listing=True))
    monkeypatch.setattr(io, "open", guard(io.open))


@dataclass
class Library:
    vault: Path
    project_dir: Path

    @property
    def reflib(self) -> Path:
        return self.project_dir / "litman_reflib"

    @property
    def code_dir(self) -> Path:
        return self.project_dir / "litman_code"

    def hub(self, name: str) -> Path:
        return self.project_dir / name


def _write_paper(vault: Path, paper_id: str, code: tuple[str, ...] = ()) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\ntitle: Title {paper_id}\nauthors: [Doe, Jane]\n"
        f"year: 2024\ndoi: 10.0/{paper_id}\nstatus: inbox\ntype: research\n"
        f"projects: []\ntopics: []\nmethods: []\ncode-clones: {list(code)}\n"
        "created-at: '2026-05-11T10:00:00+02:00'\n"
        "updated-at: '2026-05-11T10:00:00+02:00'\n",
        encoding="utf-8",
    )


def make_library(tmp_path: Path) -> Library:
    """Project ``pp`` holding p1 (bound to repo ``repoS``) and p2; p3 is in no
    project. Both hubs and REFERENCES.md exist, everything in step."""
    vault = create_vault(tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "my_own_file.txt").write_text("user data\n", encoding="utf-8")
    add_project(vault, "pp", project_dir)
    for paper_id in ("p1", "p2", "p3"):
        _write_paper(vault, paper_id)
    registry = load_config(vault).projects
    link_paper_to_project(vault, "p1", "pp", registry)
    link_paper_to_project(vault, "p2", "pp", registry)
    repo = vault / "codes" / "repoS"
    (repo / "repo").mkdir(parents=True)
    (repo / "repo" / "README.md").write_text("# s\n", encoding="utf-8")
    (repo / "repo-meta.yaml").write_text(
        "name: repoS\nupstream: file:///fake\npapers: []\n", encoding="utf-8"
    )
    bind_paper_to_repo(vault, "p1", "repoS")
    return Library(vault, project_dir)
