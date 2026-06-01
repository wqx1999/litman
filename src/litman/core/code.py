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

import io
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.document import list_papers, load_yaml_or_raise
from litman.core.views import render_index
from litman.exceptions import CodeError, PaperNotFoundError

# Directory layout constants.
CODES_DIRNAME = "codes"
REPO_DIRNAME = "repo"
REPO_META_FILENAME = "repo-meta.yaml"
NOTES_FILENAME = "notes.md"

DEFAULT_CLONE_DEPTH = 1  # `git clone --depth 1`; `--depth 0` means "no shallow"

# Repo name shape: same chars as paper id (filesystem-safe, shell-friendly),
# but starts with a letter/digit/underscore — leading hyphen would confuse
# `cd -<name>` and shell-flag parsing.
_VALID_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False
_yaml.preserve_quotes = True

_yaml_safe = YAML(typ="safe")


def _now_iso() -> str:
    """Local-timezone ISO 8601 timestamp with seconds precision."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
) -> None:
    """Run ``git clone`` into ``target_path``.

    Args:
        url: Clone URL passed to git verbatim.
        target_path: Destination directory. Must NOT exist (git clone refuses
            an existing non-empty target; we surface the refusal as CodeError).
        depth: Shallow-clone depth. ``0`` (or negative) means "no shallow"
            (full history). Default ``1``.

    Raises:
        CodeError: git is not available, the URL fails to clone, or
            ``target_path`` already exists.
    """
    if target_path.exists():
        raise CodeError(
            f"Clone target already exists: {target_path}. "
            "Remove it first or pick a different --name."
        )
    cmd: list[str] = ["git", "clone"]
    if depth >= 1:
        cmd += ["--depth", str(depth)]
    cmd += [url, str(target_path)]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise CodeError(
            "`git` executable not found on PATH. Install git first."
        ) from e
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip().splitlines()[-5:]
        raise CodeError(
            f"git clone failed (exit {result.returncode}) for {url!r}.\n"
            + "\n".join(stderr_tail)
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
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise CodeError(
            "`git` executable not found on PATH. Install git first."
        ) from e
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

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
        )

    try:
        init = _run(["git", "-C", str(target_dir), "init", "-q"])
    except FileNotFoundError as e:
        raise CodeError(
            "`git` executable not found on PATH. Install git first."
        ) from e
    if init.returncode != 0:
        raise CodeError(
            f"git init failed in {target_dir}: {(init.stderr or '').strip()}"
        )

    add = _run(["git", "-C", str(target_dir), "add", "-A"])
    if add.returncode != 0:
        raise CodeError(
            f"git add failed in {target_dir}: {(add.stderr or '').strip()}"
        )

    commit = _run(
        [
            "git", "-C", str(target_dir),
            "-c", "user.email=litman@localhost",
            "-c", "user.name=litman",
            "commit", "-q", "-m", f"import from {orig_path}",
        ]
    )
    if commit.returncode != 0:
        raise CodeError(
            f"git commit failed in {target_dir}: "
            f"{(commit.stderr or '').strip()}"
        )


def import_local_repo(
    src_path: Path,
    target_repo_root: Path,
    *,
    move: bool = False,
) -> dict[str, Any]:
    """Import a local directory into ``<vault>/codes/<name>/repo/``.

    Three cases, all converging on a ``.git/``-bearing ``target_repo_root``:

    1. ``src_path`` is a git repo (``.git/`` exists, file or dir form) →
       copy / move it whole. ``upstream`` is taken from ``remote.origin.url``
       if set; otherwise ``None``.
    2. ``src_path`` is a non-empty directory without ``.git/`` → copy / move,
       then ``git init && add -A && commit`` inside the target so the result
       is still a normal-looking checkout. ``upstream`` becomes
       ``"local:<absolute-src-path>"`` for provenance tracing.
    3. Anything else (does not exist, is a file, is an empty dir) → raise.

    Args:
        src_path: User-supplied path. Already resolved / expanded by the
            caller. Either a git repo or a non-empty plain directory.
        target_repo_root: Destination ``codes/<name>/repo/`` directory. Must
            NOT exist (caller verifies the surrounding ``codes/<name>/`` is
            free of collisions before invoking).
        move: When ``True``, ``shutil.move`` the source instead of ``cp -r``.
            Caller's responsibility to wrap in try/except and clean up the
            half-built target on failure; this function does not own
            rollback of the surrounding ``codes/<name>/`` directory.

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

    if move:
        shutil.move(str(src_path), str(target_repo_root))
    else:
        shutil.copytree(src_path, target_repo_root, symlinks=True)

    name = target_repo_root.parent.name
    if is_git:
        upstream = _extract_upstream_from_git_dir(target_repo_root)
        return make_repo_meta(name=name, upstream=upstream)

    _init_git_repo_at(target_repo_root, orig_path=src_path)
    return make_repo_meta(name=name, upstream=f"local:{src_path}")


# ---------------------------------------------------------------------------
# repo-meta.yaml + notes.md scaffolding
# ---------------------------------------------------------------------------


def make_repo_meta(
    name: str,
    upstream: str | None,
    papers: list[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Build the initial ``repo-meta.yaml`` dict.

    Field order follows the paper metadata convention: identity layer first
    (machine-maintained), then audit, then relations, then user-fills.

    ``upstream`` may be ``None`` (e.g., a local git repo without an origin
    remote — nothing fetchable to record). ``restore_missing_repos`` treats
    empty or absent upstream as "cannot restore" and reports it as failed.
    """
    timestamp = now or _now_iso()
    return {
        # === identity (machine-maintained) ===
        "name": name,
        "upstream": upstream,
        # === audit (machine-maintained) ===
        "created-at": timestamp,
        "updated-at": timestamp,
        # === relations (machine-maintained back-reference) ===
        "papers": list(papers) if papers else [],
        # === user-fills (annotation layer) ===
        "framework": None,
        "runs-on": None,
        "status": None,
    }


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
        _NOTES_TEMPLATE.format(name=name, upstream=upstream or "(none)"),
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------------------
# Paper ↔ Repo binding
# ---------------------------------------------------------------------------


def bind_paper_to_repo(vault: Path, paper_id: str, repo_name: str) -> bool:
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

    now = _now_iso()
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

    if not (paper_changed or repo_changed):
        return False

    # Build the INDEX.json re-render only if the paper side actually changed;
    # the repo-side change does not affect INDEX.json contents.
    rel_paper_meta = f"papers/{paper_id}/metadata.yaml"
    rel_repo_meta = f"{CODES_DIRNAME}/{repo_name}/{REPO_META_FILENAME}"

    with staged_write(vault, op_id=f"code-bind-{paper_id}-{repo_name}") as stage:
        if paper_changed:
            stage.write_text(rel_paper_meta, _dump_yaml_to_string(paper_meta))
            all_papers = list_papers(vault)
            all_papers = [p for p in all_papers if p.get("id") != paper_id]
            all_papers.append(dict(paper_meta))
            stage.write_text(
                "INDEX.json", render_index(all_papers, _now_iso())
            )
        if repo_changed:
            stage.write_text(rel_repo_meta, _dump_yaml_to_string(repo_meta))
    return True


def unbind_repo_from_all_papers(vault: Path, repo_name: str) -> list[str]:
    """Remove ``<repo_name>`` from every paper's ``code-clones`` list.

    Used by ``lit code rm --cascade``. Single ``staged_write`` covers all
    affected papers + ``INDEX.json``. Does NOT touch the repo's own
    ``repo-meta.yaml`` because the caller is about to delete the repo
    directory.

    Returns:
        Ordered list of paper ids whose metadata.yaml was rewritten.
    """
    affected: list[tuple[str, dict[str, Any]]] = []
    for paper_dir in sorted((vault / "papers").iterdir()):
        if not paper_dir.is_dir():
            continue
        meta_file = paper_dir / "metadata.yaml"
        if not meta_file.is_file():
            continue
        try:
            meta = _yaml.load(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not meta:
            continue
        clones = meta.get("code-clones") or []
        if repo_name not in clones:
            continue
        meta["code-clones"] = [c for c in clones if c != repo_name]
        meta["updated-at"] = _now_iso()
        affected.append((paper_dir.name, meta))

    if not affected:
        return []

    affected_ids = {pid for pid, _ in affected}
    all_papers = list_papers(vault)
    all_papers = [p for p in all_papers if p.get("id") not in affected_ids]
    for _pid, m in affected:
        all_papers.append(dict(m))
    index_json = render_index(all_papers, _now_iso())

    with staged_write(vault, op_id=f"code-unbind-{repo_name}") as stage:
        for pid, m in affected:
            stage.write_text(
                f"papers/{pid}/metadata.yaml", _dump_yaml_to_string(m)
            )
        stage.write_text("INDEX.json", index_json)
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
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        meta["_path"] = child
        out.append(meta)
    return out


# ---------------------------------------------------------------------------
# `lit code update` — git pull, optionally promote shallow → full
# ---------------------------------------------------------------------------


def git_pull(repo_path: Path, unshallow: bool = False) -> dict[str, Any]:
    """Run ``git pull`` (and optionally ``--unshallow``) inside ``repo_path``.

    Returns a small status dict with ``before_sha`` / ``after_sha`` /
    ``unshallow`` flag so the CLI can print a human-readable summary.

    Raises:
        CodeError: ``repo_path`` is not a git checkout, or git fails.
    """
    if not (repo_path / ".git").exists():
        raise CodeError(
            f"{repo_path} is not a git checkout (no .git/). "
            "Re-clone with `lit code add` or restore manually."
        )

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo_path), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    try:
        before = _run(["rev-parse", "HEAD"])
    except FileNotFoundError as e:
        raise CodeError("`git` executable not found on PATH.") from e
    if before.returncode != 0:
        raise CodeError(
            f"git rev-parse failed in {repo_path}: {before.stderr.strip()}"
        )
    before_sha = before.stdout.strip()

    if unshallow:
        # `--unshallow` is a no-op (with a non-zero exit) on already-full
        # clones; tolerate that case so the user can re-run safely.
        shallow_check = _run(["rev-parse", "--is-shallow-repository"])
        is_shallow = shallow_check.stdout.strip() == "true"
        if is_shallow:
            r = _run(["fetch", "--unshallow"])
            if r.returncode != 0:
                raise CodeError(
                    f"git fetch --unshallow failed: {r.stderr.strip()}"
                )

    pull = _run(["pull", "--ff-only"])
    if pull.returncode != 0:
        raise CodeError(
            f"git pull --ff-only failed in {repo_path}: {pull.stderr.strip()}"
        )

    after = _run(["rev-parse", "HEAD"])
    after_sha = after.stdout.strip() if after.returncode == 0 else before_sha

    return {
        "before_sha": before_sha,
        "after_sha": after_sha,
        "changed": before_sha != after_sha,
        "unshallowed": unshallow,
    }


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
    meta["updated-at"] = _now_iso()
    rel = f"{CODES_DIRNAME}/{repo_name}/{REPO_META_FILENAME}"
    with staged_write(vault, op_id=f"code-update-{repo_name}") as stage:
        stage.write_text(rel, _dump_yaml_to_string(meta))


# ---------------------------------------------------------------------------
# `lit code rm` — repo directory deletion (cascade cleanup is separate)
# ---------------------------------------------------------------------------


def delete_repo(vault: Path, repo_name: str) -> None:
    """Permanently delete ``codes/<repo_name>/`` from disk.

    The caller is responsible for running ``unbind_repo_from_all_papers``
    first (or refusing the op outright) — ``delete_repo`` does NOT touch
    paper metadata. Splitting the two operations keeps each one small and
    unit-testable.
    """
    repo_root = vault / CODES_DIRNAME / repo_name
    if not repo_root.is_dir():
        raise CodeError(
            f"No repo with name {repo_name!r} at {repo_root}. "
            "Run `lit code list` to see available repos."
        )
    shutil.rmtree(repo_root)


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
    """

    items: list[RestoreItem] = field(default_factory=list)
    orphan_refs: list[tuple[str, str]] = field(default_factory=list)

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


def restore_missing_repos(
    vault: Path,
    depth: int = DEFAULT_CLONE_DEPTH,
    dry_run: bool = False,
) -> RestoreReport:
    """Re-clone every ``codes/<name>/`` whose ``repo/`` checkout is missing.

    Cross-machine recovery: after a cloud sync that ships
    ``codes/<name>/repo-meta.yaml`` but excludes the bulky ``repo/`` git
    checkouts (recommended pattern, see design doc §5.3 / §14.5), this
    re-creates each ``repo/`` from the ``upstream`` URL recorded in its
    ``repo-meta.yaml``.

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
            clone_repo(upstream, repo_dir, depth=depth)
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

    orphan_refs = find_orphan_code_refs(vault)
    return RestoreReport(items=items, orphan_refs=orphan_refs)
