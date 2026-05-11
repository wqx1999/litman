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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.document import list_papers
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
# repo-meta.yaml + notes.md scaffolding
# ---------------------------------------------------------------------------


def make_repo_meta(
    name: str,
    upstream: str,
    papers: list[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Build the initial ``repo-meta.yaml`` dict.

    Field order follows the paper metadata convention: identity layer first
    (machine-maintained), then audit, then relations, then user-fills.
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


def write_notes(repo_dir: Path, name: str, upstream: str) -> Path:
    """Write the placeholder ``<repo_dir>/notes.md`` and return its path."""
    target = repo_dir / NOTES_FILENAME
    target.write_text(
        _NOTES_TEMPLATE.format(name=name, upstream=upstream),
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------------------
# Paper ↔ Repo binding
# ---------------------------------------------------------------------------


def bind_paper_to_repo(vault: Path, paper_id: str, repo_name: str) -> bool:
    """Append ``repo_name`` to the paper's ``code-clones`` list atomically.

    Idempotent: if ``repo_name`` is already present, returns ``False`` and
    leaves the metadata file untouched (no spurious ``updated-at`` bump).
    Otherwise, the paper's ``metadata.yaml`` and the vault ``INDEX.json``
    are rewritten together via ``staged_write``.

    Returns:
        ``True`` if a write happened, ``False`` for the idempotent no-op.

    Raises:
        PaperNotFoundError: ``papers/<paper_id>/metadata.yaml`` is missing.
    """
    meta_file = vault / "papers" / paper_id / "metadata.yaml"
    if not meta_file.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )

    metadata = _yaml.load(meta_file.read_text(encoding="utf-8"))
    if metadata is None:
        raise CodeError(
            f"metadata.yaml at {meta_file} is empty — refusing to bind. "
            "Restore the file or re-run `lit add`."
        )

    current = metadata.get("code-clones") or []
    if repo_name in current:
        return False
    metadata["code-clones"] = list(current) + [repo_name]
    metadata["updated-at"] = _now_iso()

    new_meta_yaml = _dump_yaml_to_string(metadata)

    # Re-render INDEX.json with the spliced-in updated copy.
    all_papers = list_papers(vault)
    all_papers = [p for p in all_papers if p.get("id") != paper_id]
    all_papers.append(dict(metadata))
    index_json = render_index(all_papers, _now_iso())

    rel_meta = f"papers/{paper_id}/metadata.yaml"
    with staged_write(vault, op_id=f"code-bind-{paper_id}-{repo_name}") as stage:
        stage.write_text(rel_meta, new_meta_yaml)
        stage.write_text("INDEX.json", index_json)
    return True


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
