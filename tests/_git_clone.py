"""Real git checkouts for tests that delete a paper's repo.

``lit rm`` reads a repo's checkout with git before it deletes it
(:func:`litman.core.code.repo_changes`): a ``repo/`` that is a plain folder is
one it cannot check, so it keeps it. A test that expects the repo to go needs a
checkout git reads as clean — a real clone. These make one, offline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(*args: str, cwd: Path) -> str:
    """Run git in ``cwd`` with a fixed identity; return what it printed."""
    return subprocess.run(
        [
            "git",
            "-c", "user.email=test@example.com",
            "-c", "user.name=test",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_upstream(path: Path) -> Path:
    """A git repo at ``path`` to clone from: ``README.md`` and a ``.gitignore``
    that ignores ``__pycache__/``, in one commit."""
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    (path / "README.md").write_text("# fake\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git("add", "README.md", ".gitignore", cwd=path)
    git("commit", "-q", "-m", "init", cwd=path)
    return path


def clone(upstream: Path, dest: Path) -> Path:
    """``git clone --depth 1`` of ``upstream`` into ``dest``, as ``lit code add``
    clones (a ``file://`` URL: git ignores ``--depth`` for a plain path)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", "--depth", "1", upstream.as_uri(), str(dest), cwd=dest.parent)
    return dest


def clean_clone(vault: Path, dest: Path) -> Path:
    """A clean clone at ``dest``, of one upstream kept next to ``vault``."""
    upstream = vault.parent / "fake-upstream"
    if not upstream.exists():
        make_upstream(upstream)
    return clone(upstream, dest)
