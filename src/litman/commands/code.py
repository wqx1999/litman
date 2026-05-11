"""``lit code`` command group (M3).

M3.1 ships only the ``add`` subcommand. Future subcommands (link, list,
update, rm, restore-all) plug into the same group without touching the CLI
root.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from litman.core.code import (
    CODES_DIRNAME,
    DEFAULT_CLONE_DEPTH,
    NOTES_FILENAME,
    REPO_DIRNAME,
    REPO_META_FILENAME,
    bind_paper_to_repo,
    clone_repo,
    derive_repo_name,
    is_valid_repo_name,
    make_repo_meta,
    write_notes,
    write_repo_meta,
)
from litman.core.library import find_vault
from litman.exceptions import CodeError, PaperNotFoundError

console = Console()


@click.group("code")
def code_group() -> None:
    """Manage code repositories bound to papers in the vault.

    Code repos live under ``<vault>/codes/<repo-name>/`` with the layout
    ``repo/`` (git checkout), ``repo-meta.yaml`` (our annotations), and
    ``notes.md`` (usage notes). A paper's ``metadata.yaml`` references one
    or more repos via the ``code-clones`` field; a single repo can be bound
    to multiple papers.
    """


@code_group.command("add")
@click.argument("url")
@click.option(
    "--name",
    "repo_name",
    default=None,
    help=(
        "Override the auto-derived repo name (default: last URL segment, "
        "minus '.git'). Must match [A-Za-z0-9_][A-Za-z0-9._-]* — same shape "
        "as paper ids, no leading hyphen."
    ),
)
@click.option(
    "--paper",
    "paper_id",
    default=None,
    help=(
        "Bind the cloned repo to this paper id: appends <repo-name> to the "
        "paper's `code-clones` list. The paper must exist."
    ),
)
@click.option(
    "--depth",
    type=int,
    default=DEFAULT_CLONE_DEPTH,
    show_default=True,
    help=(
        "git clone --depth N. Use 0 for a full (non-shallow) clone. Run "
        "`lit code update --unshallow` later to promote a shallow clone."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
def code_add_cmd(
    url: str,
    repo_name: str | None,
    paper_id: str | None,
    depth: int,
    library: Path | None,
) -> None:
    """Clone a code repository into ``<vault>/codes/<repo-name>/repo/``.

    Auto-generates ``repo-meta.yaml`` (papers / framework / runs-on / status
    skeleton) and a ``notes.md`` placeholder alongside the clone. With
    ``--paper <id>``, also appends ``<repo-name>`` to that paper's
    ``code-clones`` list atomically.
    """
    vault = find_vault(library)

    # ---- Pre-flight validation (cheap, fail fast before any clone) -------
    if repo_name is None:
        repo_name = derive_repo_name(url)
    elif not is_valid_repo_name(repo_name):
        raise CodeError(
            f"Invalid --name {repo_name!r}: must match "
            "[A-Za-z0-9_][A-Za-z0-9._-]* (filesystem-safe, no leading hyphen)."
        )

    repo_root = vault / CODES_DIRNAME / repo_name
    if repo_root.exists():
        raise CodeError(
            f"Repo folder already exists: {repo_root}. "
            "Pick a different --name or remove the existing folder first."
        )

    if paper_id is not None:
        paper_meta = vault / "papers" / paper_id / "metadata.yaml"
        if not paper_meta.is_file():
            raise PaperNotFoundError(
                f"No paper with id {paper_id!r} in vault {vault}. "
                "Run `lit list` to see available ids."
            )

    # ---- Materialize codes/<repo_name>/ ---------------------------------
    # On any failure between mkdir and bind_paper_to_repo, rmtree the whole
    # repo dir so a half-built clone never lingers.
    try:
        repo_root.mkdir(parents=True)
        clone_repo(url, repo_root / REPO_DIRNAME, depth=depth)
        meta = make_repo_meta(
            name=repo_name,
            upstream=url,
            papers=[paper_id] if paper_id else [],
        )
        write_repo_meta(repo_root, meta)
        write_notes(repo_root, name=repo_name, upstream=url)
        bound_now = False
        if paper_id is not None:
            bound_now = bind_paper_to_repo(vault, paper_id, repo_name)
    except Exception:
        if repo_root.exists():
            shutil.rmtree(repo_root, ignore_errors=True)
        raise

    # ---- Summary panel ---------------------------------------------------
    binding_line = ""
    if paper_id is not None:
        if bound_now:
            binding_line = (
                f"\n[bold]Bound to paper:[/] {escape(paper_id)}"
            )
        else:
            binding_line = (
                f"\n[bold]Bound to paper:[/] {escape(paper_id)} "
                "[dim](already present, no change)[/]"
            )

    depth_label = "full history" if depth < 1 else f"depth {depth}"

    console.print(
        Panel.fit(
            f"[bold green]Code added:[/] {escape(repo_name)}\n"
            f"[dim]Folder:[/] {repo_root}\n\n"
            f"[bold]Upstream:[/] {escape(url)}\n"
            f"[bold]Clone:[/] {depth_label}"
            f"{binding_line}\n\n"
            f"[dim]Next:[/] edit {REPO_META_FILENAME} to fill "
            "framework / runs-on / status, then `lit refresh-views` to "
            "update INDEX.json.",
            title="lit code add",
            border_style="green",
        )
    )
