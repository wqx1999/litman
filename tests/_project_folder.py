"""Shared fixtures for the tests of a project folder litman cannot use.

``deny`` does to one hub folder what ``chmod 000`` does on POSIX, on every
platform: the folder can still be seen from its parent, but nothing in it can
be listed, read or changed. ``deny_writes`` is ``chmod 555``: everything in the
folder can be listed and read, nothing can be added, removed or renamed (a file
already there can still be rewritten — unless it is a read-only mount). Both
work at the ``os`` / ``io`` layer every filesystem call in litman goes through,
so the code under test runs unchanged — no litman function is patched.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.checks import check_project_references
from litman.core.code import bind_paper_to_repo
from litman.core.config import load_config
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.project_link import add_project, link_paper_to_project

ACCESS_DENIED = "Access is denied"
HUBS = ["litman_reflib", "litman_code"]

# The one line a command ends with when it could not update a project folder.
LINE = (
    f"warning: could not update a project folder ({ACCESS_DENIED}). "
    "Fix that, then run `lit health-check --fix`."
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod does not take access away from a folder on Windows",
)
not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads and writes a folder whatever its permission bits",
)


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


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def deny_writes(
    monkeypatch: pytest.MonkeyPatch, folder: Path, *, mount: bool = False
) -> None:
    """Refuse every new entry, removal and rename inside ``folder``, as
    ``chmod 555`` does; reads go through, and so does rewriting a file that is
    already there. ``mount=True`` refuses that too, as a read-only mount does.
    """
    root = os.path.normcase(os.path.abspath(folder))

    def inside(arg: object) -> bool:
        if not isinstance(arg, (str, os.PathLike)):
            return False
        path = os.path.normcase(os.path.abspath(os.fspath(arg)))
        return path.startswith(root + os.sep)

    def refuse(arg: Any) -> None:
        raise PermissionError(13, ACCESS_DENIED, os.fspath(arg))

    def guard(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            for arg in args[:2]:
                if inside(arg):
                    refuse(arg)
            return real(*args, **kwargs)

        return call

    for name in (
        "mkdir", "rmdir", "unlink", "remove", "symlink", "link", "rename",
        "replace", "chmod",
    ):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))

    def refused_write(path: Any) -> bool:
        # A new entry is refused either way; an existing file only on a mount.
        return inside(path) and (mount or not os.path.lexists(os.fspath(path)))

    real_os_open = os.open

    def os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & _WRITE_FLAGS and refused_write(path):
            refuse(path)
        return real_os_open(path, flags, *args, **kwargs)

    real_io_open = io.open

    def io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(c in mode for c in "wax+") and refused_write(file):
            refuse(file)
        return real_io_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(io, "open", io_open)
    if sys.platform == "win32":
        # A junction is made through _winapi, not os.
        import _winapi

        monkeypatch.setattr(
            _winapi, "CreateJunction", guard(_winapi.CreateJunction)
        )


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


def run_lit(lib: Library, argv: list[str]) -> tuple[int, str, BaseException | None]:
    """Exit code, output with one space between words (so a line Rich wrapped
    still matches), and the exception the command ended with."""
    result = CliRunner().invoke(cli, [*argv, "--library", str(lib.vault)])
    return result.exit_code, " ".join(result.output.split()), result.exception


def project_issues(lib: Library) -> list[str]:
    return [
        i.message
        for i in check_project_references(lib.vault, list_papers(lib.vault))
    ]


def paper_meta(lib: Library, paper_id: str) -> dict:
    path = lib.vault / "papers" / paper_id / "metadata.yaml"
    return YAML(typ="safe").load(path.read_text(encoding="utf-8")) if path.is_file() else {}


def registered_projects(lib: Library) -> dict:
    return dict(load_config(lib.vault).projects)
