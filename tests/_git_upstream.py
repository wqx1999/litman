"""A bare git upstream for the tests of code repos that follow by version.

Commits go on ``main`` and are pushed there; the upstream lets a fetch ask for
any commit it holds by its id, as GitHub does. Files are written as bytes, so
the commits are the same on every platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tests._git_clone import git


@dataclass
class Upstream:
    """The bare repository at ``bare`` and the clone ``work`` that pushes to it."""

    bare: Path
    work: Path
    commits: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def commit(self, text: str) -> str:
        """Commit ``text`` as ``code.py`` on ``main``, push it, and return its id."""
        (self.work / "code.py").write_bytes(f"print({text!r})\n".encode())
        git("add", "code.py", cwd=self.work)
        git("commit", "-q", "-m", text, cwd=self.work)
        git("push", "-q", "origin", "main", cwd=self.work)
        sha = git("rev-parse", "HEAD", cwd=self.work).strip()
        self.commits.append(sha)
        return sha


def make_bare_upstream(root: Path, n_commits: int = 3) -> Upstream:
    """A bare upstream under ``root`` with ``n_commits`` commits on ``main``."""
    root.mkdir(parents=True, exist_ok=True)
    bare = root / "up.git"
    git("init", "-q", "--bare", "-b", "main", str(bare), cwd=root)
    git("config", "uploadpack.allowReachableSHA1InWant", "true", cwd=bare)
    work = root / "up-work"
    git("init", "-q", "-b", "main", str(work), cwd=root)
    git("remote", "add", "origin", bare.as_uri(), cwd=work)
    upstream = Upstream(bare=bare, work=work)
    for i in range(1, n_commits + 1):
        upstream.commit(f"c{i}")
    return upstream
