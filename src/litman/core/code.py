"""``codes/<repo-name>/`` helpers — clone + bind + repo-meta scaffolding.

Layout per design §5.3::

    codes/<repo-name>/
    ├── repo/                   # actual `git clone` checkout (keeps upstream .git/)
    ├── repo-meta.yaml          # our annotation file
    └── notes.md                # usage notes (setup, gotchas, custom scripts)

Default `git clone --depth 1` to save disk; promotable to full history later
via ``lit code update --unshallow``. A paper can bind to N repos and a repo
can be referenced by N papers (utility libs); the binding is the
``code-clones: [<repo-name>, ...]`` field in ``papers/<id>/metadata.yaml``,
mirrored back by the ``papers: [<id>, ...]`` field in ``repo-meta.yaml``.

This module exposes pure helpers so the CLI command stays thin and the
binding logic is testable without spinning up a real git clone every time.
"""

from __future__ import annotations

import errno
import io
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from ruamel.yaml.error import YAMLError

from litman.core.atomic import op_is_decided, staged_write
from litman.core.dates import now_iso
from litman.core.document import list_papers, load_yaml_or_raise
from litman.core.git import GitNotFoundError, output_tail, run_git
from litman.core.locking import is_file_manager_dropping, rmtree
from litman.core.undecodable import printable
from litman.core.views import render_index
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import (
    CodeError,
    CorruptMetadataError,
    PaperNotFoundError,
)

# Directory layout constants.
CODES_DIRNAME = "codes"
REPO_DIRNAME = "repo"
REPO_META_FILENAME = "repo-meta.yaml"
NOTES_FILENAME = "notes.md"

DEFAULT_CLONE_DEPTH = 1  # `git clone --depth 1`; `--depth 0` means "no shallow"

#: The clone URL forms ``lit code add`` takes, and two-way sync fetches from.
CLONE_URL_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "git@",
    "ssh://",
    "file://",
)

#: A commit id as git prints it: 40 hex digits (SHA-1) or 64 (SHA-256).
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

#: Where a checkout keeps the commit litman last fetched by its id: a fetch by
#: id leaves no remote branch naming it, and without this ref the commit would
#: look like one of the user's that no remote has.
FETCHED_REF = "refs/litman/fetched"
_FETCHED_GLOB = "refs/litman/*"

# Repo name shape: same chars as paper id (filesystem-safe, shell-friendly),
# but starts with a letter/digit/underscore — leading hyphen would confuse
# `cd -<name>` and shell-flag parsing.
_VALID_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def missing_code_clones(vault: Path, names: list[str]) -> list[str]:
    """Return the ``code-clones`` names whose clone is gone from disk.

    A name is "missing" when ``codes/<name>/repo-meta.yaml`` does not exist —
    the same "dangling ref" criterion
    :func:`litman.core.checks.check_code_clone_integrity` enforces for
    invariant #12. Shared so the CLI (``lit show``), the webUI cockpit, and
    ``lit health-check`` all agree on what a dangling code-clone link is.
    Order-preserving; empty / non-string names are skipped (never valid repos).
    """
    codes_dir = vault / CODES_DIRNAME
    return [
        name
        for name in names
        if isinstance(name, str)
        and name
        and not (codes_dir / name / REPO_META_FILENAME).is_file()
    ]


# git@host:owner/repo and ssh://[user@]host[:port]/owner/repo — the SSH forms
# of an upstream that also has a web page at https://host/owner/repo.
_SCP_UPSTREAM_RE = re.compile(r"^[^@/\s]+@([^:/\s]+):(?!/)(\S+)$")
_SSH_UPSTREAM_RE = re.compile(r"^ssh://(?:[^@/\s]+@)?([^:/\s]+)(?::\d+)?/(\S+)$")


def upstream_web_url(upstream: object) -> str | None:
    """The web page of a clone's ``upstream``, or None when it has none.

    An http(s) URL loses any user / token and a trailing ``.git``; the SSH
    forms become the https page on the same host. A ``local:<path>`` import
    or anything else returns None, so only an http(s) link ever leaves here.
    """
    if not isinstance(upstream, str):
        return None
    text = upstream.strip()
    m = _SCP_UPSTREAM_RE.match(text) or _SSH_UPSTREAM_RE.match(text)
    if m:
        text = f"https://{m.group(1)}/{m.group(2)}"
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    path = parts.path.rstrip("/").removesuffix(".git").rstrip("/")
    if ":" in host:  # IPv6 literal: urlsplit drops the brackets
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    return f"{parts.scheme.lower()}://{netloc}{path}"


def code_clone_upstreams(vault: Path, names: list[str]) -> dict[str, str]:
    """Map each clone in ``names`` to the web page of its upstream.

    Read from ``codes/<name>/repo-meta.yaml``; a missing or unreadable one,
    or an upstream :func:`upstream_web_url` turns down, leaves the name out.
    """
    out: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or not is_valid_repo_name(name):
            continue
        try:
            url = upstream_web_url(read_repo_meta(vault, name).get("upstream"))
        except CodeError:
            continue
        if url:
            out[name] = url
    return out

_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    default_flow_style=False,
    preserve_quotes=True,
)

_yaml_safe = ThreadLocalYAML(typ="safe")


# ---------------------------------------------------------------------------
# Repo name handling
# ---------------------------------------------------------------------------


def is_valid_repo_name(name: str) -> bool:
    """Filesystem-safe + shell-safe shape check.

    Rejects empty, ``..`` traversal, slashes, leading dot/hyphen. Same shape
    as paper ids (see ``core.id.is_valid_id``) except the leading hyphen is
    additionally forbidden so ``cd -name`` does not parse as a flag.
    """
    if not name:
        return False
    if ".." in name or "/" in name or "\\" in name:
        return False
    return bool(_VALID_REPO_NAME_RE.match(name))


def derive_repo_name(url: str) -> str:
    """Infer the repo name from a clone URL.

    Strategy: take the last ``/`` or ``:`` separated segment, then drop a
    trailing ``.git`` if present. Examples:

    ============================================== ===========
    Input URL                                      Result
    ============================================== ===========
    https://github.com/molecularsets/HELM-GPT      HELM-GPT
    https://github.com/molecularsets/HELM-GPT.git  HELM-GPT
    git@github.com:foo/bar.git                     bar
    file:///tmp/some/repo                          repo
    ssh://user@host/path/to/X                      X
    ============================================== ===========

    Raises:
        CodeError: URL is empty or yields an invalid (non-filesystem-safe)
            name after derivation. Caller can pass ``--name`` to override.
    """
    if not url or not url.strip():
        raise CodeError("Clone URL is empty.")
    cleaned = url.strip().rstrip("/")
    # Last segment after either '/' or ':' (handles git@host:user/repo form).
    tail = re.split(r"[/:]", cleaned)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    if not is_valid_repo_name(tail):
        raise CodeError(
            f"Cannot derive a valid repo name from URL {url!r}. "
            f"Got {tail!r}. Pass --name <repo-name> to override."
        )
    return tail


# ---------------------------------------------------------------------------
# git clone wrapper
# ---------------------------------------------------------------------------


def clone_repo(
    url: str,
    target_path: Path,
    depth: int = DEFAULT_CLONE_DEPTH,
    *,
    progress: bool = False,
) -> None:
    """Run ``git clone`` into ``target_path``.

    Args:
        url: Clone URL passed to git verbatim.
        target_path: Destination directory. Must NOT exist (git clone refuses
            an existing non-empty target; we surface the refusal as CodeError).
        depth: Shallow-clone depth. ``0`` (or negative) means "no shallow"
            (full history). Default ``1``.
        progress: Show git's progress on the terminal as it clones.

    Raises:
        CodeError: git is not available, the URL fails to clone, or
            ``target_path`` already exists.
    """
    if target_path.exists():
        raise CodeError(
            f"Clone target already exists: {target_path}. "
            "Remove it first or pick a different --name."
        )
    args: list[str] = ["clone"]
    if progress:
        args.append("--progress")
    if depth >= 1:
        args += ["--depth", str(depth)]
    # `--` terminates option parsing so a url beginning with `-` is treated as
    # the repository positional, not an injected git flag (review A5). `lit
    # code add` whitelists url prefixes, but restore_missing_repos clones from
    # the repo-meta.yaml upstream, which is NOT whitelisted — a cloud-sync
    # conflict copy could leave it starting with `-`.
    args += ["--", url, str(target_path)]
    result = run_git(args, progress=progress)
    if result.returncode != 0:
        raise CodeError(
            f"git clone failed (exit {result.returncode}) for {url!r}.\n"
            + output_tail(result.stderr)
        )


# ---------------------------------------------------------------------------
# Local import (M3.4) — bring an already-downloaded repo into the vault
# ---------------------------------------------------------------------------


def _extract_upstream_from_git_dir(repo_dir: Path) -> str | None:
    """Read ``remote.origin.url`` from ``<repo_dir>/.git/config``.

    Uses ``git -C <repo_dir> config --get remote.origin.url`` so it works for
    both real ``.git/`` directories and the ``.git`` file form (worktrees /
    submodules). Returns ``None`` when no origin remote is configured.
    """
    if not (repo_dir / ".git").exists():
        return None
    result = run_git(["config", "--get", "remote.origin.url"], repo=repo_dir)
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _init_git_repo_at(target_dir: Path, orig_path: Path) -> None:
    """``git init && git add -A && git commit`` inside ``target_dir``.

    Used when importing a non-git directory: we want every imported repo to
    have a ``.git/`` so ``lit code update`` and downstream tooling can treat
    it uniformly. The commit captures the import provenance in its message.

    ``user.email`` / ``user.name`` are injected per-command so the operation
    succeeds even when the host has no git global identity configured (CI,
    fresh machines, containers).
    """

    init = run_git(["init", "-q"], repo=target_dir)
    if init.returncode != 0:
        raise CodeError(
            f"git init failed in {target_dir}: {(init.stderr or '').strip()}"
        )

    add = run_git(["add", "-A"], repo=target_dir)
    if add.returncode != 0:
        raise CodeError(
            f"git add failed in {target_dir}: {(add.stderr or '').strip()}"
        )

    commit = run_git(
        [
            "-c", "user.email=litman@localhost",
            "-c", "user.name=litman",
            "commit", "-q", "-m", f"import from {orig_path}",
        ],
        repo=target_dir,
    )
    if commit.returncode != 0:
        raise CodeError(
            f"git commit failed in {target_dir}: "
            f"{(commit.stderr or '').strip()}"
        )


# A local source can be written to while it is copied — git's background
# maintenance takes and drops locks and temp packs inside `.git/` on its own
# schedule — so a file listed by `copytree` may be gone by the time it is
# opened (CI, macOS, 2026-09-06: `.git/objects/maintenance.lock`).
_COPY_ATTEMPTS = 3
_COPY_RETRY_DELAY_S = 1.0
# `copytree` hands each failure back as text, `str(OSError)`, which starts
# "[Errno N]" — or "[WinError N]" from a Windows API call, where 2 and 3 are
# file and path not found.
_NOT_FOUND = re.compile(rf"^\[(?:Errno {errno.ENOENT}|WinError [23])\]")


def _skip_git_locks(src_root: Path) -> Callable[[str, list[str]], set[str]]:
    """``copytree`` ignore callback: no ``*.lock`` from inside a ``.git/``.

    Every lock git takes is ``<file>.lock`` beside the file it guards, and git
    refuses ref names ending in ``.lock``, so inside a git directory the suffix
    names nothing else. A copied lock is stale in the copy, where git would
    then refuse to write ("Unable to create '….lock': File exists"). Outside
    ``.git/`` the suffix is the project's own (``uv.lock``, ``Cargo.lock``) and
    is copied like any other file.
    """

    def ignore(directory: str, names: list[str]) -> set[str]:
        if ".git" not in Path(directory).relative_to(src_root).parts:
            return set()
        return {name for name in names if name.endswith(".lock")}

    return ignore


def _only_vanished(errors: object) -> bool:
    """Whether every failure ``copytree`` collected is a source that went away.

    A failure counts if it says not-found, or if its source is gone now. Not
    only the second: git deletes an emptied ``objects/<xx>/`` and recreates it
    with the next object it writes, so a folder that vanished under the walk
    can be back by the time anyone looks.
    """
    if not isinstance(errors, list) or not errors:
        return False
    for entry in errors:
        if not (isinstance(entry, tuple) and len(entry) == 3):
            return False
        src, _dst, why = entry
        if not (_NOT_FOUND.match(str(why)) or not os.path.lexists(src)):
            return False
    return True


def _copy_source_tree(src_path: Path, target: Path) -> None:
    """``copytree`` that restarts when files vanish under it mid-walk.

    A file that went away is not simply skipped: git repacks by writing the
    new pack before deleting the old ones, so a walk that listed only the old
    pack and then lost it would yield a copy missing objects, with no error.
    The half copy is removed instead and the walk starts again. Any other
    failure (a file the user cannot read) is
    raised as before, at once: a second walk would fail the same way, after
    copying the whole tree again.
    """
    error: shutil.Error | None = None
    for attempt in range(_COPY_ATTEMPTS):
        if attempt:
            if os.path.lexists(target):
                rmtree(target)
            time.sleep(_COPY_RETRY_DELAY_S * attempt)
        try:
            shutil.copytree(
                src_path, target, symlinks=True, ignore=_skip_git_locks(src_path)
            )
            return
        except shutil.Error as e:
            if not _only_vanished(e.args[0] if e.args else None):
                raise
            error = e
    raise CodeError(
        f"Files under {src_path} kept disappearing while they were copied. "
        "Let whatever is writing there (e.g. git) finish, then re-run."
    ) from error


def import_local_repo(
    src_path: Path,
    target_repo_root: Path,
) -> dict[str, Any]:
    """Import a local directory into ``<vault>/codes/<name>/repo/``.

    Three cases, all converging on a ``.git/``-bearing ``target_repo_root``:

    1. ``src_path`` is a git repo (``.git/`` exists, file or dir form) →
       copy it whole. ``upstream`` is taken from ``remote.origin.url``
       if set; otherwise ``None``.
    2. ``src_path`` is a non-empty directory without ``.git/`` → copy, then
       ``git init && add -A && commit`` inside the target so the result is
       still a normal-looking checkout. ``upstream`` becomes
       ``"local:<absolute-src-path>"`` for provenance tracing.
    3. Anything else (does not exist, is a file, is an empty dir) → raise.

    This import is **always a non-destructive copy**, even for ``lit code add
    --move``. The source is never touched here: ``--move`` is implemented by
    the caller deleting the source only AFTER the whole import (repo-meta +
    notes + paper binding) has committed. Copying first means a failure at any
    later step can never destroy the user's only copy — the half-built vault
    dir is rolled back while the source stays intact (bug-report 2026-06-01 #7).

    Args:
        src_path: User-supplied path. Already resolved / expanded by the
            caller. Either a git repo or a non-empty plain directory.
        target_repo_root: Destination ``codes/<name>/repo/`` directory. Must
            NOT exist (caller verifies the surrounding ``codes/<name>/`` is
            free of collisions before invoking).

    Returns:
        A dict suitable for ``write_repo_meta``: the same shape ``make_repo_meta``
        produces, with ``upstream`` filled appropriately for the source case.
        The caller still owns ``name`` selection, paper binding, and writing
        the file to disk.

    Raises:
        CodeError: ``src_path`` is invalid (missing, not a directory, empty),
            ``target_repo_root`` already exists, or any git subprocess fails.
    """
    if not src_path.exists():
        raise CodeError(
            f"Source path does not exist: {src_path}. "
            "Pass a real local directory or a clone URL."
        )
    if not src_path.is_dir():
        raise CodeError(
            f"Source path is not a directory: {src_path}. "
            "Local import expects a folder (clone / extracted source / etc.)."
        )

    is_git = (src_path / ".git").exists()
    if not is_git:
        has_content = any(src_path.iterdir())
        if not has_content:
            raise CodeError(
                f"Source directory is empty: {src_path}. "
                "Nothing to import."
            )

    if target_repo_root.exists():
        raise CodeError(
            f"Import target already exists: {target_repo_root}. "
            "Remove it first or pick a different --name."
        )

    target_repo_root.parent.mkdir(parents=True, exist_ok=True)

    # Always a non-destructive copy — see the docstring. `--move` source
    # removal is owned by the caller and happens only after the full import
    # commits, so a mid-import failure can never leave the user with neither
    # the source nor the vault copy.
    _copy_source_tree(src_path, target_repo_root)

    name = target_repo_root.parent.name
    upstream: str | None
    if is_git:
        upstream = _extract_upstream_from_git_dir(target_repo_root)
    else:
        _init_git_repo_at(target_repo_root, orig_path=src_path)
        upstream = f"local:{src_path}"
    return make_repo_meta(name=name, upstream=upstream, head=read_head(target_repo_root))


# ---------------------------------------------------------------------------
# repo-meta.yaml + notes.md scaffolding
# ---------------------------------------------------------------------------


def make_repo_meta(
    name: str,
    upstream: str | None,
    papers: list[str] | None = None,
    now: str | None = None,
    head: tuple[str, str | None] | None = None,
) -> dict[str, Any]:
    """Build the initial ``repo-meta.yaml`` dict.

    Field order follows the paper metadata convention: identity layer first
    (machine-maintained), then audit, then relations, then user-fills.

    ``upstream`` may be ``None`` (e.g., a local git repo without an origin
    remote — nothing fetchable to record). ``restore_missing_repos`` treats
    empty or absent upstream as "cannot restore" and reports it as failed.
    A byte of it that is not UTF-8 is kept as U+FFFD, since the note has to be
    YAML; ``restore_missing_repos`` then reports that clone as failed.

    ``head`` is the checkout's ``(commit, branch)`` (:func:`read_head`). It is
    recorded as ``commit`` and ``branch`` (``None`` when HEAD is detached),
    which two-way ``lit sync`` brings the checkout on every machine to.
    """
    timestamp = now or now_iso()
    meta: dict[str, Any] = {
        # === identity (machine-maintained) ===
        "name": name,
        "upstream": printable(upstream) if upstream is not None else None,
    }
    if head is not None:
        meta["commit"], meta["branch"] = head
    meta.update({
        # === audit (machine-maintained) ===
        "created-at": timestamp,
        "updated-at": timestamp,
        # === relations (machine-maintained back-reference) ===
        "papers": list(papers) if papers else [],
        # === user-fills (annotation layer) ===
        "framework": None,
        "runs-on": None,
        "status": None,
    })
    return meta


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    """Serialize a dict to YAML using the module-level configured writer."""
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def write_repo_meta(repo_dir: Path, meta: dict[str, Any]) -> Path:
    """Write ``<repo_dir>/repo-meta.yaml`` and return its path."""
    target = repo_dir / REPO_META_FILENAME
    with target.open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)
    return target


def repo_meta_text(meta: dict[str, Any]) -> str:
    """``meta`` as :func:`write_repo_meta` writes it, for a staged write."""
    buf = io.StringIO()
    _yaml.dump(meta, buf)
    return buf.getvalue()


_NOTES_TEMPLATE = """\
# {name}

Upstream: {upstream}

(Setup, env, custom scripts, gotchas — anything that helps re-run this repo
later. Long-form. The structured fields live in `repo-meta.yaml`.)
"""


def write_notes(repo_dir: Path, name: str, upstream: str | None) -> Path:
    """Write the placeholder ``<repo_dir>/notes.md`` and return its path."""
    target = repo_dir / NOTES_FILENAME
    target.write_text(
        _NOTES_TEMPLATE.format(name=name, upstream=printable(upstream or "(none)")),
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------------------
# Paper ↔ Repo binding
# ---------------------------------------------------------------------------


def _bind_op_id(paper_id: str, repo_name: str) -> str:
    return f"code-bind-{paper_id}-{repo_name}"


def bind_is_decided(vault: Path, paper_id: str, repo_name: str) -> bool:
    """Did a failed bind already pass its decision point?

    Then the next ``lit`` finishes it, so the clone it points at must stay.
    """
    return op_is_decided(vault, _bind_op_id(paper_id, repo_name))


def bind_paper_to_repo(
    vault: Path,
    paper_id: str,
    repo_name: str,
    *,
    link_errors: list[OSError] | None = None,
) -> bool:
    """Atomically bind a paper ↔ repo on BOTH sides.

    Writes both ``papers/<paper_id>/metadata.yaml`` (appends ``<repo_name>`` to
    ``code-clones``) and ``codes/<repo_name>/repo-meta.yaml`` (appends
    ``<paper_id>`` to ``papers``) inside a single ``staged_write`` so the two
    files cannot drift apart. ``INDEX.json`` is also re-rendered to reflect
    the updated paper. Each side's ``updated-at`` is bumped only when that
    side actually changed.

    Idempotent on both sides: if both sides already record the binding,
    returns ``False`` and no file is touched. If only one side is missing,
    only that side is rewritten.

    The project folders' ``litman_code/`` links are refreshed after the bind
    commits; an ``OSError`` there does not undo it, and is appended to
    ``link_errors`` (when given) for the command to report.

    Returns:
        ``True`` if any side was written, ``False`` for the no-op.

    Raises:
        PaperNotFoundError: paper missing.
        CodeError: repo missing, or either metadata file is empty.
    """
    paper_meta_file = vault / "papers" / paper_id / "metadata.yaml"
    repo_meta_file = vault / CODES_DIRNAME / repo_name / REPO_META_FILENAME

    if not paper_meta_file.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )
    if not repo_meta_file.is_file():
        raise CodeError(
            f"No repo with name {repo_name!r} in vault {vault}. "
            "Run `lit code list` to see available repos."
        )

    paper_meta = load_yaml_or_raise(paper_meta_file, _yaml)
    if paper_meta is None:
        raise CodeError(
            f"metadata.yaml at {paper_meta_file} is empty — refusing to bind. "
            "Restore the file or re-run `lit add`."
        )
    repo_meta = load_yaml_or_raise(repo_meta_file, _yaml)
    if repo_meta is None:
        raise CodeError(
            f"repo-meta.yaml at {repo_meta_file} is empty — refusing to bind. "
            "Restore the file or re-run `lit code add`."
        )

    now = now_iso()
    paper_changed = False
    repo_changed = False

    paper_clones = paper_meta.get("code-clones") or []
    if repo_name not in paper_clones:
        paper_meta["code-clones"] = list(paper_clones) + [repo_name]
        paper_meta["updated-at"] = now
        paper_changed = True

    repo_papers = repo_meta.get("papers") or []
    if paper_id not in repo_papers:
        repo_meta["papers"] = list(repo_papers) + [paper_id]
        repo_meta["updated-at"] = now
        repo_changed = True

    changed = paper_changed or repo_changed
    if changed:
        # Build the INDEX.json re-render only if the paper side actually
        # changed; the repo-side change does not affect INDEX.json contents.
        rel_paper_meta = f"papers/{paper_id}/metadata.yaml"
        rel_repo_meta = f"{CODES_DIRNAME}/{repo_name}/{REPO_META_FILENAME}"

        with staged_write(vault, op_id=_bind_op_id(paper_id, repo_name)) as stage:
            if paper_changed:
                stage.write_text(
                    rel_paper_meta, _dump_yaml_to_string(paper_meta)
                )
                all_papers = list_papers(vault)
                all_papers = [p for p in all_papers if p.get("id") != paper_id]
                all_papers.append(dict(paper_meta))
                stage.write_text(
                    "INDEX.json", render_index(all_papers, now_iso())
                )
            if repo_changed:
                stage.write_text(rel_repo_meta, _dump_yaml_to_string(repo_meta))

    # Keep the project-side litman_code/ symlinks in sync with the (possibly
    # just-updated) code-clones — otherwise a repo bound after the paper was
    # last `lit link`ed never gets its symlink. Runs even on the idempotent
    # no-op so a previously-stale symlink self-heals (mirrors
    # link_paper_to_project, which refreshes symlinks regardless of whether
    # metadata changed). Lazy import: project_link pulls config/views, so
    # importing it at module load would widen core.code's import graph.
    from litman.core.project_link import refresh_project_code_links

    try:
        refresh_project_code_links(vault, paper_id)
    except OSError as err:
        # Best-effort convenience symlinks: the paper↔repo bind is already
        # committed (staged_write above). A symlink fs fault must NOT propagate
        # — code_add's except would otherwise roll back by rmtree'ing the
        # just-cloned repo, leaving the committed code-clones entry dangling. A
        # stale/missing symlink self-heals on the next bind/link, and
        # `lit health-check` surfaces any project-symlink drift; the command
        # says so in one line (link_errors).
        if link_errors is not None:
            link_errors.append(err)
    return changed


def unbind_paper_from_repo(
    vault: Path,
    paper_id: str,
    repo_name: str,
    *,
    link_errors: list[OSError] | None = None,
) -> bool:
    """Atomically drop a single paper ↔ repo binding from BOTH sides.

    The inverse of :func:`bind_paper_to_repo`: removes ``<repo_name>`` from the
    paper's ``code-clones`` AND ``<paper_id>`` from the repo's
    ``repo-meta.yaml`` ``papers`` list, inside one ``staged_write`` so the two
    sides cannot drift apart. ``INDEX.json`` is re-rendered when the paper side
    changed. Each side's ``updated-at`` is bumped only when that side actually
    changed.

    Tolerant of a missing repo: if ``codes/<repo_name>/repo-meta.yaml`` is gone
    (e.g. the clone was deleted out of band), only the paper side is cleaned.
    This makes ``lit code unlink`` the single tool for dropping a binding
    whether or not the clone still exists, including repairing a dangling
    forward edge (``code-clones`` lists a repo that no longer exists).

    Idempotent on both sides: if the paper does not list the repo AND the
    repo-meta (when present) does not list the paper, returns ``False`` and no
    file is touched.

    ``link_errors`` collects an ``OSError`` from the ``litman_code/`` refresh
    that follows, as in :func:`bind_paper_to_repo`.

    Raises:
        PaperNotFoundError: paper missing.
        CodeError: repo-meta.yaml present but empty / unreadable.
    """
    paper_meta_file = vault / "papers" / paper_id / "metadata.yaml"
    if not paper_meta_file.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )
    paper_meta = load_yaml_or_raise(paper_meta_file, _yaml)
    if paper_meta is None:
        raise CodeError(
            f"metadata.yaml at {paper_meta_file} is empty — refusing to "
            "unbind. Restore the file or re-run `lit add`."
        )

    # Repo side is optional: a dangling forward edge (paper lists a repo whose
    # codes/<name>/ is gone) is cleaned paper-side only.
    repo_meta_file = vault / CODES_DIRNAME / repo_name / REPO_META_FILENAME
    repo_meta: dict[str, Any] | None = None
    if repo_meta_file.is_file():
        repo_meta = load_yaml_or_raise(repo_meta_file, _yaml)
        if repo_meta is None:
            raise CodeError(
                f"repo-meta.yaml at {repo_meta_file} is empty — refusing to "
                "unbind. Restore the file or `lit code rm` + re-clone."
            )

    now = now_iso()
    paper_changed = False
    repo_changed = False

    paper_clones = paper_meta.get("code-clones") or []
    if repo_name in paper_clones:
        paper_meta["code-clones"] = [c for c in paper_clones if c != repo_name]
        paper_meta["updated-at"] = now
        paper_changed = True

    if repo_meta is not None:
        repo_papers = repo_meta.get("papers") or []
        if paper_id in repo_papers:
            repo_meta["papers"] = [p for p in repo_papers if p != paper_id]
            repo_meta["updated-at"] = now
            repo_changed = True

    changed = paper_changed or repo_changed
    if changed:
        rel_paper_meta = f"papers/{paper_id}/metadata.yaml"
        rel_repo_meta = f"{CODES_DIRNAME}/{repo_name}/{REPO_META_FILENAME}"

        with staged_write(
            vault, op_id=f"code-unbind-{paper_id}-{repo_name}"
        ) as stage:
            if paper_changed:
                stage.write_text(
                    rel_paper_meta, _dump_yaml_to_string(paper_meta)
                )
                all_papers = list_papers(vault)
                all_papers = [p for p in all_papers if p.get("id") != paper_id]
                all_papers.append(dict(paper_meta))
                stage.write_text(
                    "INDEX.json", render_index(all_papers, now_iso())
                )
            if repo_changed:
                stage.write_text(
                    rel_repo_meta, _dump_yaml_to_string(cast(dict[str, Any], repo_meta))
                )

    # Tear down the now-orphaned project-side litman_code/ symlink(s): the repo
    # is gone from this paper's code-clones, so for each project the paper is in
    # the symlink must go unless another linked paper still binds the repo
    # (reconcile keys off live membership, so the keep/remove decision is made
    # for us). Same derived-refresh seam as bind. See refresh_project_code_links.
    from litman.core.project_link import refresh_project_code_links

    try:
        refresh_project_code_links(vault, paper_id)
    except OSError as err:
        # Best-effort symlink teardown: the unbind is already committed
        # (staged_write above). A symlink fs fault must not propagate — the
        # metadata change stands and a stale symlink self-heals / is surfaced
        # by `lit health-check`; the command says so (link_errors).
        if link_errors is not None:
            link_errors.append(err)
    return changed


def unbind_repo_from_all_papers(
    vault: Path, repo_name: str, *, link_errors: list[OSError] | None = None
) -> list[str]:
    """Remove ``<repo_name>`` from every paper's ``code-clones`` list.

    Used by ``lit code rm --cascade``. Single ``staged_write`` covers all
    affected papers + ``INDEX.json``. Does NOT touch the repo's own
    ``repo-meta.yaml`` because the caller is about to delete the repo
    directory. ``link_errors`` collects an ``OSError`` from the
    ``litman_code/`` refresh that follows, as in :func:`bind_paper_to_repo`.

    Returns:
        Ordered list of paper ids whose metadata.yaml was rewritten.
    """
    affected: list[tuple[str, dict[str, Any]]] = []
    papers_root = vault / "papers"
    if not papers_root.is_dir():
        # `papers/` may not exist yet (e.g. `lit code add` ran before any
        # `lit add`). iterdir() on a missing dir raises FileNotFoundError;
        # guard it the same way every other papers/ walker does.
        return []
    for paper_dir in sorted(papers_root.iterdir()):
        if not paper_dir.is_dir():
            continue
        meta_file = paper_dir / "metadata.yaml"
        if not meta_file.is_file():
            continue
        try:
            meta = load_yaml_or_raise(meta_file, _yaml)
        except CorruptMetadataError:
            # Unreadable, unparseable, or a list field that is not a list
            # (`code-clones: myrepo-fork` would match as a substring and be
            # saved back as its characters): skip it like list_papers does —
            # health-check names the file.
            continue
        if not isinstance(meta, dict):
            # Empty/None or a non-mapping top-level YAML (list/scalar): skip like
            # every other metadata reader (corruption is surfaced by
            # health-check). Guards the .get below from an AttributeError on a
            # truthy non-dict.
            continue
        clones = meta.get("code-clones") or []
        if repo_name not in clones:
            continue
        meta["code-clones"] = [c for c in clones if c != repo_name]
        meta["updated-at"] = now_iso()
        affected.append((paper_dir.name, meta))

    if not affected:
        return []

    affected_ids = {pid for pid, _ in affected}
    all_papers = list_papers(vault)
    all_papers = [p for p in all_papers if p.get("id") not in affected_ids]
    for _pid, m in affected:
        all_papers.append(dict(m))
    index_json = render_index(all_papers, now_iso())

    with staged_write(vault, op_id=f"code-unbind-{repo_name}") as stage:
        for pid, m in affected:
            stage.write_text(
                f"papers/{pid}/metadata.yaml", _dump_yaml_to_string(m)
            )
        stage.write_text("INDEX.json", index_json)

    # Drop the project-side litman_code/ symlink for every paper we just
    # stripped (the caller is about to delete the clone, so the symlinks would
    # otherwise dangle). reconcile keys off the freshly written membership —
    # no paper binds the repo anymore — so the symlink is removed regardless of
    # whether the clone dir still exists at this point.
    from litman.core.project_link import refresh_project_code_links

    for pid, _ in affected:
        try:
            refresh_project_code_links(vault, pid)
        except OSError as err:
            # Best-effort, as in bind/unbind above: the strip is already
            # committed, and propagating would stop `lit code rm` before it
            # deletes the clone. A symlink left behind is surfaced by
            # `lit health-check`, and the command says so (link_errors).
            if link_errors is not None:
                link_errors.append(err)
    return [pid for pid, _ in affected]


def read_repo_meta(vault: Path, repo_name: str) -> dict[str, Any]:
    """Load ``codes/<repo_name>/repo-meta.yaml`` as a dict.

    Raises:
        CodeError: file missing or unparseable.
    """
    meta_file = vault / CODES_DIRNAME / repo_name / REPO_META_FILENAME
    if not meta_file.is_file():
        raise CodeError(
            f"No repo-meta.yaml at {meta_file}. "
            f"Is {repo_name!r} registered? `lit code list` to check."
        )
    try:
        meta = _yaml_safe.load(meta_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise CodeError(
            f"Failed to parse {meta_file}: {e}"
        ) from e
    if not isinstance(meta, dict):
        raise CodeError(
            f"{meta_file} does not contain a YAML mapping."
        )
    return meta


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def list_repos(vault: Path) -> list[dict[str, Any]]:
    """Enumerate ``codes/*`` and load each ``repo-meta.yaml``.

    Returns a list sorted by repo name. Each entry is the parsed metadata
    dict with an extra synthetic ``_path`` key pointing at the repo root
    (``codes/<name>/``). Repos without a ``repo-meta.yaml`` are skipped
    silently — health-check is the right surface for those orphans.
    """
    codes_dir = vault / CODES_DIRNAME
    if not codes_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(codes_dir.iterdir()):
        if not child.is_dir():
            continue
        meta_file = child / REPO_META_FILENAME
        if not meta_file.is_file():
            continue
        try:
            meta = _yaml_safe.load(meta_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, YAMLError):
            # The try only reads + parses, so these are the only failures
            # possible — let any other (programming) error propagate.
            continue
        if not isinstance(meta, dict):
            continue
        meta["_path"] = child
        out.append(meta)
    return out


# ---------------------------------------------------------------------------
# `lit code update` — git pull, optionally promote shallow → full
# ---------------------------------------------------------------------------


def git_pull(
    repo_path: Path,
    unshallow: bool = False,
    *,
    depth: int = DEFAULT_CLONE_DEPTH,
    progress: bool = False,
) -> dict[str, Any]:
    """Run ``git pull --ff-only`` (and optionally ``--unshallow``) inside ``repo_path``.

    Returns a small status dict with ``before_sha`` / ``after_sha`` /
    ``unshallow`` flag so the CLI can print a human-readable summary, and
    ``head``: the checkout's ``(commit, branch)`` afterwards (``None`` when
    git cannot tell).

    A shallow clone that two-way sync brought to an older commit holds that
    commit and its branch's tip as two pieces of history git cannot connect,
    so ``--ff-only`` fails on it. When it does and the clone holds no work of
    the user's, the branch is moved to its upstream branch's tip, fetched
    ``depth`` deep: what the pull would have done.

    Raises:
        CodeError: ``repo_path`` is not a git checkout, or git fails.
    """
    if not (repo_path / ".git").exists():
        raise CodeError(
            f"{repo_path} is not a git checkout (no .git/). "
            "Re-clone with `lit code add` or restore manually."
        )

    before = run_git(["rev-parse", "HEAD"], repo=repo_path)
    if before.returncode != 0:
        raise CodeError(
            f"git rev-parse failed in {repo_path}: {before.stderr.strip()}"
        )
    before_sha = before.stdout.strip()
    show = ["--progress"] if progress else []

    did_unshallow = False
    # `--unshallow` is a no-op (with a non-zero exit) on already-full clones;
    # tolerate that case so the user can re-run safely.
    if unshallow and _is_shallow(repo_path):
        r = run_git(["fetch", *show, "--unshallow"], repo=repo_path, progress=progress)
        if r.returncode != 0:
            raise CodeError(f"git fetch --unshallow failed: {output_tail(r.stderr)}")
        did_unshallow = True

    pull = run_git(["pull", *show, "--ff-only"], repo=repo_path, progress=progress)
    if pull.returncode != 0 and not _follow_upstream_tip(
        repo_path, depth=depth, progress=progress
    ):
        raise CodeError(
            f"git pull --ff-only failed in {repo_path}: {output_tail(pull.stderr)}"
        )

    head = read_head(repo_path)
    after_sha = head[0] if head is not None else before_sha

    return {
        "before_sha": before_sha,
        "after_sha": after_sha,
        "changed": before_sha != after_sha,
        # Reflect whether a shallow→full fetch ACTUALLY ran, not merely whether
        # the user passed --unshallow. Passing it to an already-full clone is a
        # no-op and must not bump updated-at or print "full history fetched".
        "unshallowed": did_unshallow,
        "head": head,
    }


def _follow_upstream_tip(repo: Path, *, depth: int, progress: bool) -> bool:
    """Move a shallow clone's branch to its upstream branch's tip; whether it did.

    For :func:`git_pull`, once ``--ff-only`` failed. Only a shallow clone on a
    branch that follows a remote branch, holding no work of the user's, is
    moved.
    """
    if not _is_shallow(repo):
        return False
    state = checkout_state(repo)
    head = read_head(repo)
    if state is None or state.has_work or head is None or head[1] is None:
        return False
    branch = head[1]
    remote = _git_config(repo, f"branch.{branch}.remote")
    merge = _git_config(repo, f"branch.{branch}.merge")
    if not remote or remote == "." or not merge or not merge.startswith("refs/heads/"):
        return False
    tracking = f"refs/remotes/{remote}/{merge.removeprefix('refs/heads/')}"
    fetch = ["fetch", "--no-tags"]
    if progress:
        fetch.append("--progress")
    if depth >= 1:
        fetch += ["--depth", str(depth)]
    fetched = run_git(
        [*fetch, remote, f"+{merge}:{tracking}"], repo=repo, progress=progress
    )
    if fetched.returncode != 0:
        return False
    return run_git(["checkout", "-q", "-B", branch, tracking], repo=repo).returncode == 0


def bump_repo_updated_at(vault: Path, repo_name: str) -> None:
    """Refresh ``updated-at`` on the repo's ``repo-meta.yaml``.

    Standalone helper because ``git pull`` doesn't pass through the
    bidirectional bind path — its metadata change is purely repo-side and
    has no paper-side mirror.
    """
    meta_file = vault / CODES_DIRNAME / repo_name / REPO_META_FILENAME
    if not meta_file.is_file():
        raise CodeError(f"No repo-meta.yaml at {meta_file}.")
    meta = load_yaml_or_raise(meta_file, _yaml)
    if meta is None:
        raise CodeError(f"repo-meta.yaml at {meta_file} is empty.")
    meta["updated-at"] = now_iso()
    rel = f"{CODES_DIRNAME}/{repo_name}/{REPO_META_FILENAME}"
    with staged_write(vault, op_id=f"code-update-{repo_name}") as stage:
        stage.write_text(rel, _dump_yaml_to_string(meta))


def record_repo_heads(
    vault: Path, heads: Mapping[str, tuple[str, str | None]], *, op_id: str
) -> list[str]:
    """Write each repo's ``(commit, branch)`` into its ``repo-meta.yaml``, in one staged write.

    A file that already records that commit and branch is left as it is; one
    that changes gets ``updated-at`` moved as well. Returns the repos written.

    Raises:
        CodeError: a repo has no ``repo-meta.yaml``, or it holds no mapping.
        CorruptMetadataError: one cannot be read or parsed.
    """
    writes: dict[str, str] = {}
    for name, (commit, branch) in sorted(heads.items()):
        meta_file = vault / CODES_DIRNAME / name / REPO_META_FILENAME
        if not meta_file.is_file():
            raise CodeError(f"No repo-meta.yaml at {meta_file}.")
        meta = load_yaml_or_raise(meta_file, _yaml)
        if not isinstance(meta, dict):
            raise CodeError(f"{meta_file} does not contain a YAML mapping.")
        if meta.get("commit") == commit and "branch" in meta and meta["branch"] == branch:
            continue
        _set_after(meta, "commit", commit, after="upstream")
        _set_after(meta, "branch", branch, after="commit")
        meta["updated-at"] = now_iso()
        writes[f"{CODES_DIRNAME}/{name}/{REPO_META_FILENAME}"] = _dump_yaml_to_string(meta)
    if writes:
        with staged_write(vault, op_id=op_id) as stage:
            for rel, text in writes.items():
                stage.write_text(rel, text)
    return [rel.split("/")[1] for rel in writes]


def _set_after(meta: Any, key: str, value: object, *, after: str) -> None:
    """Set ``meta[key]``; a key it lacks goes right after ``after``, when it has that."""
    if key in meta or after not in meta or not hasattr(meta, "insert"):
        meta[key] = value
        return
    meta.insert(list(meta).index(after) + 1, key, value)


# ---------------------------------------------------------------------------
# `lit code rm` — repo directory deletion (cascade cleanup is separate)
# ---------------------------------------------------------------------------


def delete_repo(vault: Path, repo_name: str) -> None:
    """Permanently delete ``codes/<repo_name>/`` from disk.

    The caller is responsible for running ``unbind_repo_from_all_papers``
    first (or refusing the op outright) — ``delete_repo`` does NOT touch
    paper metadata. Splitting the two operations keeps each one small and
    unit-testable.

    The paper-side ``code-clones`` bindings are stripped (and committed)
    *before* this runs, so a torn ``rmtree`` would leave a dangling clone
    (a ``codes/<name>/`` no paper references — violating invariant #12).
    We retry read-only ``.git`` objects via ``onexc`` and, if the removal
    still cannot complete, raise a ``CodeError`` that points the user at
    ``lit health-check`` rather than letting a half-deleted directory pass
    silently.
    """
    repo_root = vault / CODES_DIRNAME / repo_name
    if not repo_root.is_dir():
        raise CodeError(
            f"No repo with name {repo_name!r} at {repo_root}. "
            "Run `lit code list` to see available repos."
        )
    try:
        rmtree(repo_root)
    except OSError as err:
        raise CodeError(
            f"Failed to fully delete {repo_root}: {err}. The directory may "
            "be partially removed while the paper-side code-clones bindings "
            "were already stripped — run `lit health-check` to detect and "
            "reconcile the dangling clone."
        ) from err


# ---------------------------------------------------------------------------
# `lit code restore-all` — cross-machine recovery
# ---------------------------------------------------------------------------

RestoreStatus = Literal["restored", "skipped", "failed"]


@dataclass(frozen=True)
class RestoreItem:
    """One repo's outcome in a restore run."""

    name: str
    upstream: str
    status: RestoreStatus
    detail: str = ""


@dataclass
class RestoreReport:
    """Aggregated result of ``restore_missing_repos``.

    ``items`` covers every repo with a readable ``repo-meta.yaml`` under
    ``codes/``. ``orphan_refs`` covers a separate failure mode: a paper's
    ``code-clones`` field names a repo whose ``codes/<name>/repo-meta.yaml``
    is itself missing — we have no upstream URL to clone from, so the user
    must restore the metadata from backup or drop the dangling reference.
    ``link_errors`` holds each ``OSError`` from linking a restored repo into
    its papers' project ``litman_code/`` folders, for the command to report.
    """

    items: list[RestoreItem] = field(default_factory=list)
    orphan_refs: list[tuple[str, str]] = field(default_factory=list)
    link_errors: list[OSError] = field(default_factory=list)

    @property
    def restored(self) -> int:
        return sum(1 for it in self.items if it.status == "restored")

    @property
    def skipped(self) -> int:
        return sum(1 for it in self.items if it.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for it in self.items if it.status == "failed")

    @property
    def is_clean(self) -> bool:
        return self.failed == 0 and not self.orphan_refs


def find_orphan_code_refs(vault: Path) -> list[tuple[str, str]]:
    """Find paper ``code-clones`` entries pointing at non-existent repo-meta.

    A reference is orphan when the paper says ``code-clones: [<name>]`` but
    no ``codes/<name>/repo-meta.yaml`` exists on disk — we cannot restore
    the repo because the upstream URL is unknown.

    Returns a sorted, deduplicated list of ``(paper_id, repo_name)``.
    """
    codes_dir = vault / CODES_DIRNAME
    seen: set[tuple[str, str]] = set()
    for paper in list_papers(vault):
        refs = paper.get("code-clones") or []
        if not isinstance(refs, list):
            continue
        paper_id = str(paper.get("id") or "?")
        for name in refs:
            if not isinstance(name, str) or not name:
                continue
            meta_file = codes_dir / name / REPO_META_FILENAME
            if not meta_file.is_file():
                seen.add((paper_id, name))
    return sorted(seen)


def fetch_repo(
    upstream: str,
    target_path: Path,
    depth: int = DEFAULT_CLONE_DEPTH,
    *,
    commit: str | None = None,
    branch: str | None = None,
    progress: bool = False,
) -> None:
    """Re-create a ``repo/`` checkout from its recorded ``upstream``.

    Fetch the recorded ``commit`` (on ``branch``) when there is one
    (:func:`restore_at_commit`), else ``git clone`` it; or, for a folder that
    was imported without git and so recorded as ``local:<path>``, copy that
    folder again, as ``lit code add`` did the first time.

    Raises:
        CodeError: the clone or the copy failed, or ``target_path`` exists.
    """
    if upstream.startswith("local:"):
        import_local_repo(Path(upstream.removeprefix("local:")), target_path)
        return
    if commit is not None:
        restore_at_commit(
            upstream, target_path, commit, branch, depth=depth, progress=progress
        )
        return
    clone_repo(upstream, target_path, depth=depth, progress=progress)


def can_fetch_again(upstream: str) -> bool:
    """Whether :func:`fetch_repo` has a source to re-create a checkout from.

    A clone URL has one (when the network allows); ``local:<path>`` only while
    that folder exists — ``lit code add --move`` consumed it, so the vault holds
    the only copy; an empty upstream never has one.
    """
    if not upstream:
        return False
    if upstream.startswith("local:"):
        return Path(upstream.removeprefix("local:")).is_dir()
    return True


# What `lit rm` reads before it deletes a repo it could clone again.
RepoState = Literal["clean", "changed", "unchecked"]

# A git call that takes longer than this counts as "could not check": the
# repo is kept, and a large checkout cannot hold up the delete.
_CHANGES_TIMEOUT_S = 15.0
# Who commits the folder _init_git_repo_at imports without git.
_IMPORT_IDENTITY = "litman@localhost"


def _git_read(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only git command in ``repo_dir``, bounded in time.

    ``GIT_OPTIONAL_LOCKS=0`` keeps ``git status`` from refreshing (and
    locking) the index: the check writes nothing.
    """
    return run_git(
        args, repo=repo_dir, timeout=_CHANGES_TIMEOUT_S, env={"GIT_OPTIONAL_LOCKS": "0"}
    )


def _has_own_files(status: str) -> bool:
    """Whether ``git status --porcelain -z`` lists a change of the user's.

    Anything but an untracked file a file manager left (``.DS_Store``, ...).
    """
    for entry in status.split("\0"):
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if code != "??" or not is_file_manager_dropping(path.rsplit("/", 1)[-1]):
            return True
    return False


def repo_changes(repo_root: Path) -> tuple[RepoState, str]:
    """Whether ``codes/<repo>/`` holds anything a fresh clone would not bring back.

    ``("changed", "")`` when it does: a file next to ``repo/`` other than
    ``repo-meta.yaml`` and ``notes.md`` (and what a file manager leaves), a
    stash, a commit on no remote, or a changed, staged or untracked file in
    the checkout (ignored files and a file manager's do not count).
    ``("clean", "")`` when it does not, or when there is no ``repo/`` at all
    (a sync that leaves checkouts out). ``("unchecked", why)`` when it cannot
    tell: ``repo/`` is not a git checkout, or git is missing, fails or takes
    too long — ``why`` says which.
    """
    try:
        names = [child.name for child in repo_root.iterdir()]
    except OSError:
        return "unchecked", "cannot read the folder"
    own = {REPO_DIRNAME, REPO_META_FILENAME, NOTES_FILENAME}
    if any(n not in own and not is_file_manager_dropping(n) for n in names):
        return "changed", ""
    repo_dir = repo_root / REPO_DIRNAME
    if not os.path.lexists(repo_dir):
        return "clean", ""
    # Before any git call: without its own .git, git would walk up and
    # answer for an enclosing repository (a vault kept in git).
    if not os.path.lexists(repo_dir / ".git"):
        return "unchecked", "not a git checkout"
    try:
        stash = _git_read(repo_dir, "rev-parse", "--verify", "--quiet", "refs/stash")
        if stash.returncode == 0:
            return "changed", ""
        # Commits on no remote, by committer: --branches as well as HEAD (a
        # commit on another local branch). The one litman makes importing a
        # folder without git is not the user's — a re-import makes it again —
        # unless they amended it, which makes them its committer. One litman
        # fetched by its id (FETCHED_REF) is on a remote.
        log = _git_read(
            repo_dir, "log", "--format=%ce", "HEAD", "--branches",
            "--not", "--remotes", f"--glob={_FETCHED_GLOB}",
        )
        if log.returncode != 0:
            return "unchecked", "git failed"
        if any(line.lower() != _IMPORT_IDENTITY for line in log.stdout.splitlines()):
            return "changed", ""
        # Every untracked file, explicitly: a repo set to hide them still
        # shows them, and each is named, so what a file manager leaves (in
        # any folder it was shown) can be told apart.
        status = _git_read(repo_dir, "status", "--porcelain", "-z", "--untracked-files=all")
        if status.returncode != 0:
            return "unchecked", "git failed"
        return ("changed", "") if _has_own_files(status.stdout) else ("clean", "")
    except GitNotFoundError:
        return "unchecked", "git not found"
    except subprocess.TimeoutExpired:
        return "unchecked", "git took too long"
    except OSError:
        return "unchecked", "git failed"


# ---------------------------------------------------------------------------
# Checkouts at a recorded commit (two-way `lit sync`, restore)
# ---------------------------------------------------------------------------

#: What git refuses in a branch name, or what would read as an option.
_BAD_BRANCH_RE = re.compile(
    r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//|^-|^/|/$|\.$|\.lock(?:/|$)|(?:^|/)\."
)


def valid_commit(value: object) -> str | None:
    """``value`` when it is a full commit id, else ``None``."""
    return value if isinstance(value, str) and _COMMIT_RE.fullmatch(value) else None


def valid_branch(value: object) -> str | None:
    """``value`` when it is a branch name git takes, else ``None``.

    ``repo-meta.yaml`` comes from other machines, so a name is checked before
    it reaches a git command line.
    """
    if not isinstance(value, str) or value in ("", "HEAD", "@"):
        return None
    return None if _BAD_BRANCH_RE.search(value) else value


class GitStepError(CodeError):
    """A git step failed. ``tail`` holds git's last lines of output."""

    def __init__(self, step: str, result: subprocess.CompletedProcess[str]) -> None:
        self.step = step
        self.tail = output_tail(result.stderr)
        lines = self.tail.splitlines()
        reason = next(
            (line for line in lines if line.startswith(("fatal:", "error:"))),
            lines[-1] if lines else f"exit {result.returncode}",
        )
        super().__init__(f"git could not {step}: {reason}")


def _git_step(step: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        raise GitStepError(step, result)


def _git_config(repo: Path, key: str) -> str | None:
    try:
        result = _git_read(repo, "config", "--get", key)
    except (GitNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _is_shallow(repo: Path) -> bool:
    try:
        result = _git_read(repo, "rev-parse", "--is-shallow-repository")
    except (GitNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.stdout.strip() == "true"


def read_head(repo_dir: Path) -> tuple[str, str | None] | None:
    """The commit ``repo_dir``'s checkout is at, and its branch (``None`` when detached).

    ``None`` when ``repo_dir`` has no ``.git`` of its own (git would answer for
    an enclosing repository, a vault kept in git), nothing is checked out yet,
    or git is missing, fails or takes too long.
    """
    if not os.path.lexists(repo_dir / ".git"):
        return None
    try:
        rev = _git_read(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if rev.returncode != 0:
            return None
        ref = _git_read(repo_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    except (GitNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    commit = valid_commit(rev.stdout.strip())
    if commit is None:
        return None
    branch = ref.stdout.strip() if ref.returncode == 0 else ""
    return commit, branch or None


@dataclass(frozen=True)
class CheckoutState:
    """What a checkout holds that no other machine can fetch."""

    #: A tracked file changed, staged or deleted.
    changed: bool
    #: A file git does not track (what a file manager leaves aside).
    untracked: bool
    #: A commit on HEAD or a local branch that no remote has.
    unpushed: bool

    @property
    def has_work(self) -> bool:
        """Whether moving the checkout to another commit could lose the user's work.

        Untracked files do not count: they stay, and git refuses a move that
        would overwrite one.
        """
        return self.changed or self.unpushed


def checkout_state(repo_dir: Path) -> CheckoutState | None:
    """What ``repo_dir``'s checkout holds of the user's own; ``None`` when git cannot tell.

    A commit litman fetched by its id (:data:`FETCHED_REF`) is on a remote,
    and so is one at the edge of a shallow clone's history: only a fetch
    makes one. Without the second rule, a commit the user fetched by its id
    and checked out would look like theirs.
    """
    try:
        status = _git_read(repo_dir, "status", "--porcelain", "-z", "--untracked-files=all")
        if status.returncode != 0:
            return None
        log = _git_read(
            repo_dir, "log", "--format=%H", "HEAD", "--branches",
            "--not", "--remotes", f"--glob={_FETCHED_GLOB}",
        )
        if log.returncode != 0:
            return None
        local = set(log.stdout.split())
        if local:
            local -= _shallow_edge(repo_dir)
    except (GitNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    changed = untracked = False
    for entry in status.stdout.split("\0"):
        if not entry:
            continue
        code, rel = entry[:2], entry[3:]
        if code != "??":
            changed = True
        elif not is_file_manager_dropping(rel.rsplit("/", 1)[-1]):
            untracked = True
    return CheckoutState(changed=changed, untracked=untracked, unpushed=bool(local))


def _shallow_edge(repo: Path) -> set[str]:
    """The commits at the edge of a shallow clone's history (its ``shallow`` file)."""
    where = _git_read(repo, "rev-parse", "--git-path", "shallow")
    if where.returncode != 0:
        return set()
    path = Path(where.stdout.strip())
    try:
        return set((path if path.is_absolute() else repo / path).read_text("ascii").split())
    except (OSError, UnicodeDecodeError):
        return set()


def _fetch_commit(repo: Path, commit: str, *, depth: int | None, progress: bool) -> None:
    """Fetch ``commit`` from ``origin`` into :data:`FETCHED_REF`, ``depth`` deep."""
    args = ["fetch", "--no-tags"]
    if progress:
        args.append("--progress")
    if depth is not None:
        args += ["--depth", str(depth)]
    args += ["origin", f"+{commit}:{FETCHED_REF}"]
    _git_step(f"fetch commit {commit[:7]}", run_git(args, repo=repo, progress=progress))


def _checkout_commit(repo: Path, commit: str, branch: str | None) -> None:
    """Check out ``commit``: on ``branch``, which follows ``origin``'s, or detached."""
    if branch is None:
        _git_step(
            f"check out {commit[:7]}",
            run_git(["checkout", "-q", "--detach", commit], repo=repo),
        )
        return
    _git_step(
        f"check out {commit[:7]} on {branch}",
        run_git(["checkout", "-q", "-B", branch, commit], repo=repo),
    )
    # As after a clone, the branch follows origin's branch of that name, so
    # `git pull` and `lit code update` work on it. One that already follows a
    # branch keeps it.
    if _git_config(repo, f"branch.{branch}.merge"):
        return
    tracking = f"refs/remotes/origin/{branch}"
    if run_git(["rev-parse", "--verify", "--quiet", tracking], repo=repo).returncode != 0:
        run_git(["update-ref", tracking, commit], repo=repo)
    run_git(["branch", "-q", f"--set-upstream-to=origin/{branch}", branch], repo=repo)


def restore_at_commit(
    upstream: str,
    target_path: Path,
    commit: str,
    branch: str | None,
    *,
    depth: int = DEFAULT_CLONE_DEPTH,
    progress: bool = False,
) -> None:
    """Make ``target_path`` a checkout of ``upstream`` at ``commit``, on ``branch``.

    Only that commit is fetched, ``depth`` deep (``0``: with its whole
    history). A clone of the branch tip followed by a fetch of an older commit
    would hold two pieces of history git cannot connect, and a later ``git
    pull --ff-only`` would fail. ``branch`` follows ``origin``'s branch of that
    name, as after a clone; ``None`` leaves HEAD detached at ``commit``.

    Raises:
        CodeError: ``target_path`` exists, or git is missing or failed
            (:class:`GitStepError`). A half-made checkout is removed.
    """
    if os.path.lexists(target_path):
        raise CodeError(
            f"Clone target already exists: {target_path}. "
            "Remove it first or pick a different --name."
        )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git_step("make the checkout", run_git(["init", "-q", "--", str(target_path)]))
        follows = ["-t", branch] if branch is not None else []
        _git_step(
            "add its upstream",
            run_git(["remote", "add", *follows, "--", "origin", upstream], repo=target_path),
        )
        _fetch_commit(
            target_path, commit, depth=depth if depth >= 1 else None, progress=progress
        )
        _checkout_commit(target_path, commit, branch)
    except BaseException:
        rmtree(target_path, ignore_errors=True)
        raise


def move_to_commit(
    repo_dir: Path,
    commit: str,
    branch: str | None,
    *,
    depth: int = DEFAULT_CLONE_DEPTH,
    progress: bool = False,
) -> None:
    """Bring the checkout in ``repo_dir`` to ``commit``, on ``branch`` (or detached).

    For a checkout holding no work of the user's (:func:`checkout_state`). The
    commit is fetched from ``origin``: ``depth`` deep into a shallow clone,
    which stays shallow, and whole into a full one. Untracked files stay; git
    refuses the move when the commit would overwrite one.

    Raises:
        CodeError: git is missing or failed (:class:`GitStepError`).
    """
    deep = depth if depth >= 1 and _is_shallow(repo_dir) else None
    _fetch_commit(repo_dir, commit, depth=deep, progress=progress)
    _checkout_commit(repo_dir, commit, branch)


#: What a ``codes/<name>/`` folder holds (:func:`code_folder_kind`).
CodeFolderKind = Literal["repo", "left-behind", "empty", "broken"]


def code_folder_kind(folder: Path) -> CodeFolderKind:
    """What a ``codes/<name>/`` folder holds, as the checks and two-way sync see it.

    * ``repo``: it has a ``repo-meta.yaml``.
    * ``left-behind``: only a ``repo/`` checkout with files in it. The repo
      was removed from the library (two-way sync brings a removal made on
      another machine), and its checkout, which sync never touches, stayed.
    * ``empty``: no files, only empty folders and what a file manager leaves.
      While a machine keeps a left-behind checkout, two-way sync brings its
      empty folder to every other machine.
    * ``broken``: anything else.
    """
    if (folder / REPO_META_FILENAME).is_file():
        return "repo"
    try:
        names = [n for n in os.listdir(folder) if not is_file_manager_dropping(n)]
        repo = folder / REPO_DIRNAME
        if (
            names == [REPO_DIRNAME]
            and repo.is_dir()
            and not os.path.islink(repo)
            and _holds_files(repo)
        ):
            return "left-behind"
        return "broken" if _holds_files(folder) else "empty"
    except OSError:
        return "broken"


def _holds_files(top: Path) -> bool:
    """Whether ``top`` holds a file or a link, other than what a file manager leaves."""

    def fail(err: OSError) -> None:
        raise err

    for dirpath, dirnames, filenames in os.walk(top, onerror=fail):
        if any(not is_file_manager_dropping(n) for n in filenames):
            return True
        if any(os.path.islink(os.path.join(dirpath, d)) for d in dirnames):
            return True
    return False


def is_clone_url(value: str) -> bool:
    """Whether ``value`` is a clone URL in one of the forms of :data:`CLONE_URL_PREFIXES`."""
    return value.startswith(CLONE_URL_PREFIXES)


def left_behind_message(name: str) -> tuple[str, str]:
    """What a left-behind checkout (:func:`code_folder_kind`) is, and the way out.

    One wording for the ``lit sync`` summary and ``lit health-check``.
    """
    return (
        f"codes/{name}/repo/ stays on this machine: {name} was removed from the library",
        "Move that folder out of the library, or delete it.",
    )


def remove_empty_subfolders(folder: Path) -> None:
    """Remove the empty folders inside ``folder``, deepest first; ``folder`` stays."""
    for dirpath, _dirnames, _filenames in os.walk(folder, topdown=False):
        if Path(dirpath) != folder:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


def restore_missing_repos(
    vault: Path,
    depth: int = DEFAULT_CLONE_DEPTH,
    dry_run: bool = False,
    *,
    progress: bool = False,
) -> RestoreReport:
    """Re-clone every ``codes/<name>/`` whose ``repo/`` checkout is missing.

    Cross-machine recovery: after a cloud sync that ships
    ``codes/<name>/repo-meta.yaml`` but excludes the bulky ``repo/`` git
    checkouts (recommended pattern, see design doc §5.3 / §14.5), this
    re-creates each ``repo/`` from the ``upstream`` URL recorded in its
    ``repo-meta.yaml``, at the recorded ``commit`` when there is one.

    Failures are isolated per repo: one clone failure (network, auth, bad
    URL, missing ``upstream``) does not abort the loop. The returned
    ``RestoreReport`` lists every repo's outcome so the caller can render a
    human-friendly summary and pick the exit code.

    Args:
        vault: Vault root.
        depth: Shallow-clone depth forwarded to ``clone_repo``. ``0`` means
            full history. Default ``1`` matches ``lit code add``.
        dry_run: When ``True``, log what *would* be cloned without executing
            any ``git clone``. Useful for previewing in CI before commit.

    Returns:
        A ``RestoreReport`` with per-repo items plus any orphan paper
        references.
    """
    items: list[RestoreItem] = []
    link_errors: list[OSError] = []
    for meta in list_repos(vault):
        name = str(meta.get("name") or meta["_path"].name)
        upstream = str(meta.get("upstream") or "")
        repo_dir = meta["_path"] / REPO_DIRNAME

        if repo_dir.exists():
            items.append(
                RestoreItem(
                    name=name,
                    upstream=upstream,
                    status="skipped",
                    detail="repo/ already present",
                )
            )
            continue

        if not upstream:
            items.append(
                RestoreItem(
                    name=name,
                    upstream="",
                    status="failed",
                    detail="repo-meta.yaml has empty 'upstream' field",
                )
            )
            continue

        if dry_run:
            items.append(
                RestoreItem(
                    name=name,
                    upstream=upstream,
                    status="restored",
                    detail="(dry-run) would clone",
                )
            )
            continue

        try:
            fetch_repo(
                upstream,
                repo_dir,
                depth=depth,
                commit=valid_commit(meta.get("commit")),
                branch=valid_branch(meta.get("branch")),
                progress=progress,
            )
        except CodeError as e:
            first_line = str(e).splitlines()[0] if str(e) else "git clone failed"
            items.append(
                RestoreItem(
                    name=name,
                    upstream=upstream,
                    status="failed",
                    detail=first_line,
                )
            )
            continue
        items.append(
            RestoreItem(
                name=name,
                upstream=upstream,
                status="restored",
                detail=f"cloned from {upstream}",
            )
        )
        # The checkout is back, so its papers' projects can link to it again
        # (a project's litman_code/ skips a repo with no repo/). Lazy import,
        # as in bind_paper_to_repo.
        from litman.core.project_link import refresh_project_code_links

        for paper_id in meta.get("papers") or []:
            try:
                refresh_project_code_links(vault, str(paper_id))
            except OSError as err:
                link_errors.append(err)

    orphan_refs = find_orphan_code_refs(vault)
    return RestoreReport(
        items=items, orphan_refs=orphan_refs, link_errors=link_errors
    )
