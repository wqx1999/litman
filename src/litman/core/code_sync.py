"""Code repos follow the library by version, for two-way ``lit sync``.

A ``codes/<name>/repo/`` checkout never travels as files: a copied git
repository is a broken one (links and exec bits are lost). Its
``repo-meta.yaml`` records the commit it is at (``commit``, ``branch``); that
file travels like any other, and each machine brings its own checkout to the
recorded commit from the upstream.

* Before the file sync, :func:`record_repo_versions` writes into
  ``repo-meta.yaml`` the commit of each checkout the user moved since this
  machine's last sync (a pull, a commit, a checkout).
* After it, :func:`bring_repos_in` fetches each missing checkout at the
  recorded commit and moves each one that is at another, unless it holds work
  of the user's. It reports what it could not do and why, which checkouts hold
  changes no other machine gets, and which were left behind by a repo removed
  from the library. It deletes nothing.
* A ``repo-meta.yaml`` that still records no commit after the file sync gets
  this machine's (:attr:`RepoSyncReport.unrecorded`), and the sync takes it up
  at once: the first machine to sync records the commit and the others follow
  it. Written before the file sync, by each machine for itself, the commits
  would meet as a conflict.

A checkout that could not follow (offline, say) is still at its old commit at
the next sync, which must not take that for a move of the user's and write
the old commit back: the commit each checkout was at when this machine's last
sync ended is kept in the sync's state folder (:func:`read_last_heads`), and
only a checkout that moved since then has its commit written.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from litman.core.code import (
    CODES_DIRNAME,
    REPO_DIRNAME,
    GitStepError,
    checkout_state,
    code_folder_kind,
    fetch_repo,
    is_clone_url,
    list_repos,
    move_to_commit,
    read_head,
    record_repo_heads,
    valid_branch,
    valid_commit,
)
from litman.core.git import GitNotFoundError
from litman.core.replace import replace_file
from litman.exceptions import CodeError

#: What git says when the upstream does not have the commit asked for: it was
#: never pushed, or the branch holding it was rewritten.
_NOT_ON_UPSTREAM = (
    "not our ref",
    "couldn't find remote ref",
    "no such remote ref",
    "unadvertised object",
)

_RETRY = "The next `lit sync` tries again."


@dataclass(frozen=True)
class RepoRestored:
    """A checkout this sync fetched (``cloned``) or ``moved`` to the recorded commit."""

    name: str
    action: Literal["cloned", "moved"]
    commit: str
    branch: str | None


@dataclass(frozen=True)
class RepoNotRestored:
    """A checkout this sync could not bring to the recorded commit.

    ``what`` says what happened and ``way_out`` what to do, one sentence each.
    """

    name: str
    what: str
    way_out: str


@dataclass(frozen=True)
class RepoSyncReport:
    """What :func:`bring_repos_in` did, for the ``lit sync`` summary."""

    restored: tuple[RepoRestored, ...] = ()
    not_restored: tuple[RepoNotRestored, ...] = ()
    #: Checkouts with changed or untracked files: they stay on this machine.
    uncommitted: tuple[str, ...] = ()
    #: Checkouts with commits no remote has, and how many: no other machine
    #: can fetch them.
    unpushed: dict[str, int] = field(default_factory=dict)
    #: ``codes/<name>/`` folders holding only a checkout: the repo was removed.
    left_behind: tuple[str, ...] = ()
    #: The commit each checkout is at now, for :func:`write_last_heads`.
    heads: dict[str, str] = field(default_factory=dict)
    #: Checkouts whose ``repo-meta.yaml`` records no commit yet, with the
    #: ``(commit, branch)`` they are at: for the sync to record and take up.
    unrecorded: dict[str, tuple[str, str | None]] = field(default_factory=dict)

    @property
    def mismatched(self) -> bool:
        """Whether a checkout here does not match what the library records."""
        return bool(self.not_restored or self.left_behind)


def read_last_heads(path: Path) -> dict[str, str] | None:
    """The commit each checkout was at when this machine's last sync ended.

    ``None`` when no sync has recorded them yet (or the file cannot be read).
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(name): str(commit) for name, commit in payload["heads"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_last_heads(path: Path, heads: Mapping[str, str]) -> None:
    """Keep ``heads`` for the next sync's :func:`record_repo_versions`."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"heads": dict(sorted(heads.items()))}), encoding="utf-8")
    replace_file(tmp, path)


def record_repo_versions(vault: Path, last_heads: Mapping[str, str] | None) -> list[str]:
    """Write into ``repo-meta.yaml`` the commit of each checkout the user moved here.

    A checkout counts as moved when it is at another commit than when this
    machine's last sync ended (``last_heads``), or was not there then. With
    ``last_heads`` ``None`` no checkout counts as moved: one that differs from
    its record is brought to it after the file sync instead. A checkout that
    did not move is never written here, even when its ``repo-meta.yaml``
    records no commit yet: that waits until after the file sync
    (:attr:`RepoSyncReport.unrecorded`), when another machine may have
    recorded one.

    Returns the repos written.

    Raises:
        LitmanError: a ``repo-meta.yaml`` could not be read or written.
    """
    heads: dict[str, tuple[str, str | None]] = {}
    for meta in list_repos(vault):
        folder: Path = meta["_path"]
        head = read_head(folder / REPO_DIRNAME)
        if head is None or last_heads is None or last_heads.get(folder.name) == head[0]:
            continue
        heads[folder.name] = head
    if not heads:
        return []
    return record_repo_heads(vault, heads, op_id="sync-record-commits")


def bring_repos_in(vault: Path, *, depth: int, progress: bool = False) -> RepoSyncReport:
    """Bring each checkout to the commit its ``repo-meta.yaml`` records.

    For each ``codes/<name>/`` with a ``repo-meta.yaml``:

    * no ``repo/``: fetch it from the upstream at the recorded commit, ``depth``
      deep (a plain clone when no commit is recorded). An upstream that is a
      folder on another machine (``local:``), or none, is not tried.
    * ``repo/`` at another commit: move it there, unless it holds work of the
      user's (a tracked change, or a commit no remote has).

    A failure is reported and the next repo tried; nothing is deleted.
    ``progress`` shows git's progress on the terminal. A checkout whose
    ``repo-meta.yaml`` records no commit is listed in ``unrecorded``.
    """
    restored: list[RepoRestored] = []
    not_restored: list[RepoNotRestored] = []
    uncommitted: list[str] = []
    unpushed: dict[str, int] = {}
    heads: dict[str, str] = {}
    unrecorded: dict[str, tuple[str, str | None]] = {}
    for meta in list_repos(vault):
        folder: Path = meta["_path"]
        name = folder.name
        repo_dir = folder / REPO_DIRNAME
        commit = valid_commit(meta.get("commit"))
        branch = valid_branch(meta.get("branch"))

        if not os.path.lexists(repo_dir):
            outcome = _fetch(
                name, repo_dir, str(meta.get("upstream") or ""), commit, branch,
                depth=depth, progress=progress,
            )
            if isinstance(outcome, RepoNotRestored):
                not_restored.append(outcome)
                continue
            restored.append(outcome)
            heads[name] = outcome.commit
            if commit is None and outcome.commit:
                unrecorded[name] = (outcome.commit, outcome.branch)
            continue

        head = read_head(repo_dir)
        if head is None:
            if commit is not None:
                not_restored.append(
                    RepoNotRestored(
                        name,
                        f"its repo/ is not a git checkout litman can read, so it is not "
                        f"at {commit[:7]} as the library records",
                        "Move that folder aside; the next `lit sync` fetches it again.",
                    )
                )
            continue
        heads[name] = head[0]
        if commit is None:
            unrecorded[name] = head
        state = checkout_state(repo_dir)
        if commit is not None and head[0] != commit:
            if state is None or state.has_work:
                not_restored.append(_kept(name, head[0], commit, checked=state is not None))
                continue
            try:
                move_to_commit(repo_dir, commit, branch, depth=depth, progress=progress)
            except GitNotFoundError:
                not_restored.append(_no_git(name))
            except CodeError as err:
                not_restored.append(
                    RepoNotRestored(
                        name, f"not moved to {commit[:7]}: {_reason(err)}", _way_out(err, commit)
                    )
                )
            else:
                restored.append(RepoRestored(name, "moved", commit, branch))
                heads[name] = commit
        if state is not None:
            if state.changed or state.untracked:
                uncommitted.append(name)
            if state.unpushed:
                unpushed[name] = state.unpushed

    return RepoSyncReport(
        restored=tuple(restored),
        not_restored=tuple(not_restored),
        uncommitted=tuple(uncommitted),
        unpushed=unpushed,
        left_behind=tuple(_left_behind(vault)),
        heads=heads,
        unrecorded=unrecorded,
    )


def _fetch(
    name: str,
    repo_dir: Path,
    upstream: str,
    commit: str | None,
    branch: str | None,
    *,
    depth: int,
    progress: bool,
) -> RepoRestored | RepoNotRestored:
    """Fetch the missing checkout ``repo_dir`` from ``upstream``, at ``commit``."""
    if not upstream:
        return RepoNotRestored(
            name,
            "it has no upstream to fetch it from",
            "Copy its repo/ folder here from a machine that has it.",
        )
    if upstream.startswith("local:"):
        return RepoNotRestored(
            name,
            "it was imported from a folder on another machine",
            f"Copy that folder to codes/{name}/repo here.",
        )
    if not is_clone_url(upstream):
        return RepoNotRestored(
            name,
            f"its upstream is not a clone URL: {upstream}",
            "Fix `upstream` in its repo-meta.yaml; the next `lit sync` fetches it.",
        )
    try:
        fetch_repo(upstream, repo_dir, depth, commit=commit, branch=branch, progress=progress)
    except GitNotFoundError:
        return _no_git(name)
    except CodeError as err:
        return RepoNotRestored(name, f"not fetched: {_reason(err)}", _way_out(err, commit))
    head = read_head(repo_dir)
    if head is None:
        return RepoRestored(name, "cloned", commit or "", branch)
    return RepoRestored(name, "cloned", head[0], head[1])


def _kept(name: str, head: str, commit: str, *, checked: bool) -> RepoNotRestored:
    """A checkout left where it is because it may hold work of the user's."""
    if not checked:
        return RepoNotRestored(
            name,
            f"git could not check it for changes, so it stays at {head[:7]}; "
            f"the library records {commit[:7]}",
            _RETRY,
        )
    return RepoNotRestored(
        name,
        f"it has changes here, so it stays at {head[:7]}; the library records {commit[:7]}",
        "Commit and push them, or undo them; the next `lit sync` moves it.",
    )


def _no_git(name: str) -> RepoNotRestored:
    return RepoNotRestored(
        name, "git is not installed", "Install git; the next `lit sync` fetches it."
    )


def _reason(err: CodeError) -> str:
    """One line on why git failed: a step's own summary, or git's last line."""
    if isinstance(err, GitStepError):
        return str(err).splitlines()[0]
    lines = [line for line in str(err).splitlines() if line.strip()]
    return lines[-1] if lines else "git failed"


def _way_out(err: CodeError, commit: str | None) -> str:
    """What to do after a failed fetch: push a commit the upstream lacks, or wait."""
    text = (err.tail if isinstance(err, GitStepError) else str(err)).lower()
    if commit is not None and any(marker in text for marker in _NOT_ON_UPSTREAM):
        return f"Push {commit[:7]} from the machine that made it; the next `lit sync` fetches it."
    return _RETRY


def _left_behind(vault: Path) -> list[str]:
    codes = vault / CODES_DIRNAME
    try:
        children = sorted(codes.iterdir()) if codes.is_dir() else []
    except OSError:
        return []
    return [
        child.name
        for child in children
        if child.is_dir() and not child.is_symlink() and code_folder_kind(child) == "left-behind"
    ]
