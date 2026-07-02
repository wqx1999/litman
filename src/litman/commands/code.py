"""``lit code`` command group (M3).

M3.1: ``add`` subcommand (clone + bind).
M3.2: ``list``, ``link``, ``update``, ``rm`` subcommands (enumeration,
later binding to existing repos, git-pull/unshallow, recursive deletion
with cascade cleanup).
M3.3: ``restore-all`` subcommand (cross-machine recovery — re-clone any
``codes/<name>/repo/`` that's missing locally, using the ``upstream`` URL
preserved in its ``repo-meta.yaml``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from litman.core.code import (
    CODES_DIRNAME,
    REPO_DIRNAME,
    REPO_META_FILENAME,
    bind_paper_to_repo,
    bump_repo_updated_at,
    clone_repo,
    delete_repo,
    derive_repo_name,
    git_pull,
    import_local_repo,
    is_valid_repo_name,
    list_repos,
    make_repo_meta,
    read_repo_meta,
    restore_missing_repos,
    unbind_paper_from_repo,
    unbind_repo_from_all_papers,
    write_notes,
    write_repo_meta,
)
from litman.core.config import load_config
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.locking import rmtree
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input
from litman.exceptions import CodeError, PaperNotFoundError

console = Console()


_URL_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "git@",
    "ssh://",
    "file://",
)


def _is_url(arg: str) -> bool:
    """First-arg discriminator for ``lit code add``.

    Recognises the clone-URL forms ``git`` natively accepts. Everything else
    routes to the local-import branch (the caller verifies it actually points
    at a real directory).
    """
    return arg.startswith(_URL_PREFIXES)


@click.group("code")
def code_group() -> None:
    """Manage code repositories bound to papers in the vault.

    Code repos live under <vault>/codes/<repo-name>/ with the layout
    repo/ (git checkout), repo-meta.yaml (our annotations), and
    notes.md (usage notes). A paper's metadata.yaml references one
    or more repos via the code-clones field; a single repo can be bound
    to multiple papers.
    """


# ---------------------------------------------------------------------------
# lit code add
# ---------------------------------------------------------------------------


@code_group.command("add")
@click.argument("source")
@click.option(
    "--name",
    "repo_name",
    default=None,
    help=(
        "Override the auto-derived repo name. For a URL, default is the "
        "last URL segment minus '.git'; for a local path, default is the "
        "directory's basename. Must match [A-Za-z0-9_][A-Za-z0-9._-]* — "
        "same shape as paper ids, no leading hyphen."
    ),
)
@click.option(
    "--paper",
    "paper_id",
    default=None,
    shell_complete=complete_paper_id,
    help=(
        "Bind the added repo to this paper id (full or unique "
        "case-insensitive substring): appends <repo-name> to the paper's "
        "code-clones list. The paper must exist."
    ),
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Reverse-lookup the paper by DOI instead of supplying --paper. "
        "Mutually exclusive with --paper."
    ),
)
@click.option(
    "--depth",
    type=int,
    default=None,
    help=(
        "git clone --depth N (URL sources only — ignored for local imports). "
        "Use 0 for a full (non-shallow) clone. Defaults to lit-config.yaml's "
        "default_clone_depth (1 unless overridden). Run lit code update "
        "--unshallow later to promote a shallow clone."
    ),
)
@click.option(
    "--move",
    "move_src",
    is_flag=True,
    default=False,
    help=(
        "Local-import only: move the source directory into the vault "
        "instead of copying. The source disappears on success. Useful for "
        "/tmp / Downloads sources you want cleaned up automatically. "
        "Ignored for URL sources."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def code_add_cmd(
    source: str,
    repo_name: str | None,
    paper_id: str | None,
    paper_doi: str | None,
    depth: int | None,
    move_src: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Add a code repository to <vault>/codes/<repo-name>/repo/.

    SOURCE is either a clone URL (http://, https://, git@,
    ssh://, file://) or a path to an existing local directory.

    - URL source → git clone runs against the URL (the default).
    - Local-path source → the directory is copied (default) or moved (with
      --move) into the vault. If the source is already a git repo, its
      remote.origin.url becomes the recorded upstream; otherwise the
      target is initialised as a fresh git repo with a single import commit,
      and upstream is recorded as local:<absolute-source-path> for
      provenance.

    Auto-generates repo-meta.yaml (papers / framework / runs-on / status
    skeleton) and a notes.md placeholder. With --paper <id> (full or
    unique substring) or --paper-doi <DOI>, also appends <repo-name>
    to that paper's code-clones list atomically.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))

    if paper_id is not None or paper_doi is not None:
        paper_id = resolve_paper_input(vault, paper_id, paper_doi)

    use_url = _is_url(source)

    derived_name: str | None = None
    if use_url:
        clone_url = source
        if depth is None:
            depth = load_config(vault).default_clone_depth
        src_path: Path | None = None
        if repo_name is None:
            # Derive only when --name is absent: derive_repo_name RAISES on an
            # underivable URL, so an explicit --name must skip derivation
            # entirely (otherwise --name could never rescue such a URL).
            derived_name = derive_repo_name(clone_url)
    else:
        src_path = Path(source).expanduser().resolve()
        if not src_path.is_dir():
            raise CodeError(
                f"Source is neither a recognised clone URL nor an existing "
                f"local directory: {source!r}."
            )
        if repo_name is None:
            derived_name = src_path.name

    if repo_name is None:
        assert derived_name is not None  # set above whenever repo_name is None
        repo_name = derived_name
        if not is_valid_repo_name(repo_name):
            raise CodeError(
                f"Cannot derive a valid repo name from {source!r}. "
                f"Got {repo_name!r}. Pass --name <repo-name> to override."
            )
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
        # Pre-clone fail-fast: resolve_paper_input already established the paper
        # exists, but re-check here so an absent paper aborts BEFORE the
        # expensive clone/import + mkdir below (bind_paper_to_repo re-validates,
        # but only runs after the clone). Same expensive-op ordering as --move.
        paper_meta = vault / "papers" / paper_id / "metadata.yaml"
        if not paper_meta.is_file():
            raise PaperNotFoundError(
                f"No paper with id {paper_id!r} in vault {vault}. "
                "Run `lit list` to see available ids."
            )

    try:
        repo_root.mkdir(parents=True)
        if use_url:
            clone_repo(clone_url, repo_root / REPO_DIRNAME, depth=depth)
            meta = make_repo_meta(name=repo_name, upstream=clone_url)
        else:
            assert src_path is not None  # mypy/pyright narrowing
            meta = import_local_repo(src_path, repo_root / REPO_DIRNAME)
        write_repo_meta(repo_root, meta)
        write_notes(repo_root, name=repo_name, upstream=meta.get("upstream"))
        bound_now = False
        if paper_id is not None:
            bound_now = bind_paper_to_repo(vault, paper_id, repo_name)
    except Exception:
        if repo_root.exists():
            rmtree(repo_root, ignore_errors=True)
        raise

    # `--move` consumes the source — but only HERE, after the import fully
    # committed (clone/copy + repo-meta + notes + bind all succeeded). The copy
    # above is non-destructive, so the except clause can safely rmtree a
    # half-built vault dir without risking the user's only copy. Deleting the
    # source last is what closes the data-loss window (bug-report 2026-06-01 #7).
    if move_src and not use_url and src_path is not None:
        try:
            rmtree(src_path)
        except OSError as e:
            console.print(
                f"[yellow]warning:[/] imported into the vault, but could not "
                f"remove the original source {escape(str(src_path))}: "
                f"{escape(str(e))}\n  Delete it manually if you no longer "
                "need it."
            )

    binding_line = ""
    if paper_id is not None:
        suffix = (
            "" if bound_now else " [dim](already present, no change)[/]"
        )
        binding_line = f"\n[bold]Bound to paper:[/] {escape(paper_id)}{suffix}"

    upstream_display = meta.get("upstream") or "(none)"
    if use_url:
        depth_label = "full history" if depth < 1 else f"depth {depth}"
        provenance_line = f"[bold]Clone:[/] {depth_label}"
    else:
        verb = "moved" if move_src else "copied"
        provenance_line = (
            f"[bold]Local import:[/] {verb} from "
            f"{escape(str(src_path))}"
        )

    console.print(
        Panel.fit(
            f"[bold green]Code added:[/] {escape(repo_name)}\n"
            f"[dim]Folder:[/] {repo_root}\n\n"
            f"[bold]Upstream:[/] {escape(str(upstream_display))}\n"
            f"{provenance_line}"
            f"{binding_line}\n\n"
            f"[dim]Next:[/] edit {REPO_META_FILENAME} to fill "
            "framework / runs-on / status, then `lit refresh-views` to "
            "update INDEX.json.",
            title="lit code add",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# lit code list
# ---------------------------------------------------------------------------


@code_group.command("list")
@click.option(
    "--paper",
    "paper_id",
    default=None,
    shell_complete=complete_paper_id,
    help=(
        "Show only repos bound to this paper id (full or unique "
        "case-insensitive substring)."
    ),
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Show only repos bound to the paper with this DOI. Mutually "
        "exclusive with --paper."
    ),
)
@click.option(
    "--orphan",
    is_flag=True,
    default=False,
    help="Show only repos with no paper bindings (papers: []).",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def code_list_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    orphan: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """List code repositories in the vault.

    Filters are mutually exclusive: pass at most one of --paper <id> /
    --paper-doi <DOI> / --orphan. --paper accepts a full id or
    a unique case-insensitive substring.
    """
    n_paper_filters = sum(
        1 for v in (paper_id, paper_doi) if v is not None and v != ""
    )
    if n_paper_filters > 1:
        raise CodeError(
            "--paper and --paper-doi are mutually exclusive. "
            "Pick one paper-side filter."
        )
    paper_filter_set = n_paper_filters == 1
    if paper_filter_set and orphan:
        raise CodeError(
            "--paper / --paper-doi and --orphan are mutually exclusive. "
            "Pick one filter or pass neither."
        )

    vault = find_vault(resolve_library_or_vault(library, vault_name))

    if paper_filter_set:
        paper_id = resolve_paper_input(vault, paper_id, paper_doi)

    repos = list_repos(vault)
    if paper_id is not None:
        repos = [r for r in repos if paper_id in (r.get("papers") or [])]
    elif orphan:
        repos = [r for r in repos if not (r.get("papers") or [])]

    if not repos:
        msg_parts = ["No code repos"]
        if paper_id:
            msg_parts.append(f"bound to {paper_id!r}")
        elif orphan:
            msg_parts.append("without paper bindings")
        msg_parts.append(f"in {vault}.")
        console.print(f"[yellow]{' '.join(msg_parts)}[/]")
        return

    table = Table(
        title=f"Code repositories in {vault}",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Name", style="bold")
    table.add_column("Papers", justify="right")
    table.add_column("Framework")
    table.add_column("Status")
    table.add_column("Upstream", overflow="fold")

    for meta in repos:
        papers = meta.get("papers") or []
        papers_cell = (
            "[dim]—[/]" if not papers
            else f"{len(papers)} ({escape(papers[0])}"
                 f"{', ...' if len(papers) > 1 else ''})"
        )
        framework = meta.get("framework") or "[dim]—[/]"
        status = meta.get("status") or "[dim]—[/]"
        upstream = meta.get("upstream") or "[dim]—[/]"
        table.add_row(
            escape(str(meta.get("name", "?"))),
            papers_cell,
            escape(str(framework)) if framework != "[dim]—[/]" else framework,
            escape(str(status)) if status != "[dim]—[/]" else status,
            escape(str(upstream)) if upstream != "[dim]—[/]" else upstream,
        )

    console.print(table)
    console.print(f"[dim]{len(repos)} repo(s)[/]")


# ---------------------------------------------------------------------------
# lit code link
# ---------------------------------------------------------------------------


@code_group.command("link")
@click.argument("repo_name")
@click.option(
    "--paper",
    "paper_id",
    default=None,
    shell_complete=complete_paper_id,
    help=(
        "Paper id to bind <repo-name> to (full or unique case-insensitive "
        "substring). Required unless --paper-doi is given."
    ),
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Reverse-lookup the paper by DOI instead of supplying --paper. "
        "Mutually exclusive with --paper."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def code_link_cmd(
    repo_name: str,
    paper_id: str | None,
    paper_doi: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Bind an existing local repo to a paper.

    Appends <repo-name> to the paper's code-clones list AND
    <paper-id> to the repo's repo-meta.yaml's papers list, both
    atomically. Idempotent: if the binding is already present on both sides,
    no metadata is touched.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    changed = bind_paper_to_repo(vault, paper_id, repo_name)
    if changed:
        console.print(
            f"[bold green]Linked:[/] {escape(paper_id)} ↔ {escape(repo_name)}"
        )
    else:
        console.print(
            f"[yellow]No-op:[/] {escape(paper_id)} ↔ {escape(repo_name)} "
            "already bound on both sides."
        )


# ---------------------------------------------------------------------------
# lit code unlink
# ---------------------------------------------------------------------------


@code_group.command("unlink")
@click.argument("repo_name")
@click.option(
    "--paper",
    "paper_id",
    default=None,
    shell_complete=complete_paper_id,
    help=(
        "Paper id to unbind <repo-name> from (full or unique case-insensitive "
        "substring). Required unless --paper-doi is given."
    ),
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Reverse-lookup the paper by DOI instead of supplying --paper. "
        "Mutually exclusive with --paper."
    ),
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def code_unlink_cmd(
    repo_name: str,
    paper_id: str | None,
    paper_doi: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Unbind a repo from a paper without deleting the clone.

    The inverse of `lit code link`. Removes <repo-name> from the paper's
    code-clones list AND <paper-id> from the repo's repo-meta.yaml's papers
    list, both atomically. The clone directory is kept (use `lit code rm` to
    delete it). For a 1:N repo shared by several papers this drops only the
    named paper's edge. Tolerant of an already-deleted clone: it still cleans
    the paper side, so it also repairs a dangling code-clones reference.
    Idempotent: a no-op if the binding is already absent on both sides.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    changed = unbind_paper_from_repo(vault, paper_id, repo_name)
    if changed:
        console.print(
            f"[bold green]Unlinked:[/] {escape(paper_id)} ✕ {escape(repo_name)}"
        )
    else:
        console.print(
            f"[yellow]No-op:[/] {escape(paper_id)} ✕ {escape(repo_name)} "
            "already unbound on both sides."
        )


# ---------------------------------------------------------------------------
# lit code update
# ---------------------------------------------------------------------------


@code_group.command("update")
@click.argument("repo_name")
@click.option(
    "--unshallow",
    is_flag=True,
    default=False,
    help="Promote a shallow clone to full history (git fetch --unshallow).",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def code_update_cmd(
    repo_name: str,
    unshallow: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Run git pull --ff-only inside codes/<repo-name>/repo/.

    With --unshallow, first promote a shallow clone to full history.
    Bumps the repo's updated-at audit timestamp if anything changed.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    repo_root = vault / CODES_DIRNAME / repo_name
    if not repo_root.is_dir():
        raise CodeError(
            f"No repo with name {repo_name!r} at {repo_root}. "
            "Run `lit code list` to see available repos."
        )

    status = git_pull(repo_root / REPO_DIRNAME, unshallow=unshallow)
    if status["changed"] or status["unshallowed"]:
        bump_repo_updated_at(vault, repo_name)

    if status["changed"]:
        msg = (
            f"[bold green]Updated:[/] {escape(repo_name)}\n"
            f"[dim]HEAD:[/] {status['before_sha'][:8]} → "
            f"{status['after_sha'][:8]}"
        )
    else:
        msg = f"[yellow]Already up to date:[/] {escape(repo_name)} ({status['before_sha'][:8]})"
    if status["unshallowed"]:
        msg += "\n[dim]Unshallow: full history fetched.[/]"
    console.print(msg)


# ---------------------------------------------------------------------------
# lit code rm
# ---------------------------------------------------------------------------


@code_group.command("rm")
@click.argument("repo_name")
@click.option(
    "--cascade",
    is_flag=True,
    default=False,
    help=(
        "Auto-strip <repo-name> from every paper's code-clones list. "
        "Without --cascade, rm refuses when any paper still references this repo."
    ),
)
@click.option(
    "--yes", "-y",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the y/N confirmation prompt.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def code_rm_cmd(
    repo_name: str,
    cascade: bool,
    yes: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Permanently delete codes/<repo-name>/ from the vault.

    Hard delete (there is no trash bin for code repos; the repo is
    re-clonable from the upstream URL preserved in metadata). With
    --cascade, also strip the repo name from every paper's
    code-clones list atomically before the directory is removed.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    repo_root = vault / CODES_DIRNAME / repo_name
    if not repo_root.is_dir():
        raise CodeError(
            f"No repo with name {repo_name!r} at {repo_root}. "
            "Run `lit code list` to see available repos."
        )

    # Find back-references via repo-meta.yaml's papers field. If repo-meta is
    # missing or unreadable we cannot know which papers still bind this repo,
    # so refuse rather than delete blind: a blind delete would silently strand
    # the paper-side code-clones entries (invariant #12a, no link without
    # clone) exactly when the refusal guard below is needed most.
    try:
        meta = read_repo_meta(vault, repo_name)
        bound_papers: list[str] = list(meta.get("papers") or [])
    except CodeError as e:
        raise CodeError(
            f"Cannot read {repo_name!r}'s repo-meta.yaml ({e}). Refusing to "
            "delete: its paper bindings are unknown and deleting blind would "
            "leave dangling code-clones references. Fix the repo-meta.yaml "
            "first, then re-run."
        ) from e

    if bound_papers and not cascade:
        bullet = "\n".join(f"  - {p}" for p in bound_papers)
        raise CodeError(
            f"{repo_name!r} is still bound to {len(bound_papers)} paper(s):\n"
            f"{bullet}\n\n"
            "Re-run with --cascade to auto-strip these bindings, or unbind "
            f"individually with `lit code unlink {repo_name} --paper <id>` "
            "first."
        )

    if not yes:
        plan = f"Delete {repo_root}"
        if bound_papers:
            plan += (
                f" and strip {repo_name!r} from {len(bound_papers)} paper(s) "
                "via cascade"
            )
        click.confirm(f"{plan}?", abort=True, default=False)

    # 1) Update paper sides atomically (only if cascade was requested AND
    #    there's something to update — bound_papers is already empty when
    #    cascade is False, since the refusal above would have fired).
    affected: list[str] = []
    if cascade:
        affected = unbind_repo_from_all_papers(vault, repo_name)

    # 2) Delete the repo directory itself. By the time we reach here, all
    #    paper-side cleanup has committed atomically.
    delete_repo(vault, repo_name)

    summary = (
        f"[bold green]Removed:[/] {escape(repo_name)}\n"
        f"[dim]Folder:[/] {repo_root}"
    )
    if affected:
        summary += (
            f"\n[dim]Unbound from {len(affected)} paper(s):[/] "
            + ", ".join(escape(p) for p in affected)
        )
    console.print(summary)


# ---------------------------------------------------------------------------
# lit code restore-all
# ---------------------------------------------------------------------------


@code_group.command("restore-all")
@click.option(
    "--depth",
    type=int,
    default=None,
    help=(
        "git clone --depth N for every restored repo. Use 0 for full "
        "(non-shallow) clones. Defaults to lit-config.yaml's "
        "default_clone_depth (1 unless overridden). Promote individually "
        "later with lit code update --unshallow."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview which repos would be re-cloned without running git.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml. "
        "Mutually exclusive with --library."
    ),
)
def code_restore_all_cmd(
    depth: int | None,
    dry_run: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Re-clone every code repo whose local repo/ checkout is missing.

    Cross-machine recovery: scans codes/*/repo-meta.yaml and runs
    git clone for any repo whose codes/<name>/repo/ directory is
    absent, using the upstream URL recorded in its repo-meta.yaml.
    Repos already present are skipped. A single failure (network, auth,
    bad URL) does not abort the loop.

    Also reports orphan references — paper code-clones entries that
    point at repos whose repo-meta.yaml is itself missing (broken
    metadata; not recoverable from URL alone).

    Exit code 1 if any clone failed or any orphan reference was found
    (CI/cron-gateable); 0 if every repo is either restored or already
    present.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    if depth is None:
        depth = load_config(vault).default_clone_depth
    report = restore_missing_repos(vault, depth=depth, dry_run=dry_run)

    if not report.items and not report.orphan_refs:
        console.print(
            f"[yellow]No code repos registered in {vault / CODES_DIRNAME}.[/]"
        )
        return

    if report.items:
        title = "lit code restore-all"
        if dry_run:
            title += " (dry-run)"
        table = Table(title=title, header_style="bold", show_lines=False)
        table.add_column("Name", style="bold")
        table.add_column("Status")
        table.add_column("Detail", overflow="fold")
        _STATUS_STYLE = {
            "restored": ("green", "✓ Restored" if not dry_run else "✓ Would restore"),
            "skipped": ("yellow", "○ Skipped"),
            "failed": ("red", "✗ Failed"),
        }
        for item in report.items:
            color, label = _STATUS_STYLE[item.status]
            table.add_row(
                escape(item.name),
                f"[{color}]{label}[/]",
                escape(item.detail),
            )
        console.print(table)

    if report.orphan_refs:
        console.print(
            "\n[bold red]Orphan references "
            f"({len(report.orphan_refs)}):[/] "
            "paper points at a repo with no `repo-meta.yaml`."
        )
        for paper_id, repo_name in report.orphan_refs:
            console.print(
                f"  - paper [bold]{escape(paper_id)}[/] → "
                f"{escape(repo_name)} [dim](no codes/{escape(repo_name)}/{REPO_META_FILENAME})[/]"
            )
        console.print(
            "\n[dim]Restore the missing repo-meta.yaml from backup, or run "
            "`lit code unlink <repo-name> --paper <paper-id>` to drop "
            "the dangling reference.[/]"
        )

    summary = (
        f"\n[bold]{report.restored}[/] restored, "
        f"[bold]{report.skipped}[/] skipped, "
        f"[bold]{report.failed}[/] failed, "
        f"[bold]{len(report.orphan_refs)}[/] orphan ref(s)."
    )
    console.print(summary)

    if not report.is_clean:
        sys.exit(1)
