"""``lit project`` — atomic project registry primitive (M15).

``projects`` is a controlled value just like ``topics`` / ``methods`` /
``data`` (invariant #2), but unlike those it carries a path binding in
``lit-config.yaml``'s ``projects:`` map. Before M15 the only way to register
a project was hand-editing that yaml, which left the two truth sources
(TAXONOMY.md ``## projects`` and the config map) free to drift.

This command group is to ``projects`` what ``lit taxonomy`` is to the other
user dicts: every mutation updates BOTH sides atomically through
:func:`litman.core.atomic.staged_write`.

Five subcommands:

* ``add``      — register a new project (dual-write TAXONOMY + config)
* ``list``     — read-only JOIN of both truth sources, with drift markers
* ``rename``   — rename across TAXONOMY + config key + every paper's
  ``projects`` field + INDEX.json, then rebuild project refs/symlinks
* ``set-path`` — config-only path change (papers store only the name)
* ``rm``       — cascade-with-confirm teardown (untag papers, drop from
  both truth sources, delete project symlinks + REFERENCES.md)

``lit taxonomy {add,rename,rm} projects`` is hard-deprecated (it would be a
half-update footgun); see ``commands/taxonomy.py``.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from ruamel.yaml import YAML

from litman.commands.taxonomy import _ripple_removals, _ripple_replacements
from litman.core.atomic import staged_write
from litman.core.confirm import _confirm_destructive
from litman.core.config import config_to_yaml_dict, load_config
from litman.core.correctors import reconcile_derived
from litman.core.dates import now_iso
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.project_refs import (
    LITERATURE_SUBDIR,
    REFERENCES_FILENAME,
)
from litman.core.taxonomy import (
    find_referencing_papers,
    parse_taxonomy,
    update_user_dict_section,
)
from litman.core.views import render_index
from litman.exceptions import TaxonomyError

console = Console()

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.default_flow_style = False

_PROJECTS_DICT = "projects"


def _dump_config_yaml(config_map: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(config_map, buf)
    return buf.getvalue()


def _config_with_projects(vault: Path, projects: dict[str, str]) -> str:
    """Render lit-config.yaml text with the ``projects`` map replaced.

    Loads + validates the existing config (so an already-broken config is
    surfaced here, not silently overwritten), swaps in ``projects``, and
    dumps the full schema back to yaml text ready for staged_write.
    """
    config = load_config(vault)
    as_dict = config_to_yaml_dict(config)
    as_dict["projects"] = dict(projects)
    return _dump_config_yaml(as_dict)


def _load_taxonomy(vault: Path) -> tuple[str, dict[str, list[str]]]:
    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    return text, parse_taxonomy(text)


# ---------------------------------------------------------------------------
# `lit project` group
# ---------------------------------------------------------------------------


@click.group("project")
def project_group() -> None:
    """Manage the project registry (TAXONOMY.md + lit-config.yaml in sync).

    A project is a controlled projects value bound to an on-disk
    working directory. Both truth sources — TAXONOMY.md's ## projects
    section and lit-config.yaml's projects: map — are kept consistent
    by every subcommand here. Never hand-edit either side for projects.
    """


# Reused option blocks — keep the --library / --vault shape identical to
# every other litman command.
def _library_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--library",
        type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
        default=None,
        envvar="LIT_LIBRARY",
        help="Override the active vault. Discovery order: this flag / $LIT_LIBRARY, then the active registered vault, then cwd-walk.",
    )(fn)


def _vault_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--vault",
        "vault_name",
        default=None,
        help=(
            "Vault name from ~/.config/litman/vaults.yaml. "
            "Mutually exclusive with --library."
        ),
    )(fn)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@project_group.command("add")
@click.argument("name")
@click.option(
    "--path",
    "project_path",
    required=True,
    type=click.Path(
        exists=False,
        file_okay=False,
        resolve_path=True,
        path_type=Path,
    ),
    help=(
        "Absolute on-disk path to the project's working directory. "
        "Must already exist and be a directory (typo defense — no "
        "placeholder registration)."
    ),
)
@_library_option
@_vault_option
def project_add_cmd(
    name: str,
    project_path: Path,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Register a new project (dual-write TAXONOMY.md + lit-config.yaml).

    Both truth sources are updated in a single atomic staged_write so a
    crash never leaves the name in one but not the other.
    """
    name = name.strip()
    if not name:
        raise TaxonomyError("Project name cannot be empty.")
    if not project_path.exists():
        raise TaxonomyError(
            f"Path {str(project_path)!r} does not exist. "
            f"Create it first (e.g. `mkdir -p {project_path}`), "
            "then re-run — placeholder registration is intentionally "
            "not allowed."
        )
    if not project_path.is_dir():
        raise TaxonomyError(
            f"Path {str(project_path)!r} is not a directory."
        )

    vault = find_vault(resolve_library_or_vault(library, vault_name))
    text, parsed = _load_taxonomy(vault)
    config = load_config(vault)

    registered_names = set(parsed[_PROJECTS_DICT]) | set(config.projects)
    if name in registered_names:
        existing_path = config.projects.get(name)
        raise TaxonomyError(
            f"Project {name!r} is already registered"
            + (f" → {existing_path}" if existing_path else "")
            + ". Use `lit project set-path "
            f"{name} <new-path>` to change its path, or "
            f"`lit project rename {name} <new-name>` to rename it."
        )

    new_taxonomy_text = update_user_dict_section(
        text, _PROJECTS_DICT, sorted(parsed[_PROJECTS_DICT] + [name])
    )
    new_projects = dict(config.projects)
    new_projects[name] = str(project_path)
    new_config_text = _config_with_projects(vault, new_projects)

    with staged_write(vault, op_id=f"project-add-{name}") as stage:
        stage.write_text("TAXONOMY.md", new_taxonomy_text)
        stage.write_text("lit-config.yaml", new_config_text)

    console.print(
        f"[bold green]✓ Registered[/] {escape(name)} → "
        f"{escape(str(project_path))}"
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@project_group.command("list")
@_library_option
@_vault_option
def project_list_cmd(
    library: Path | None,
    vault_name: str | None,
) -> None:
    """List every project (a JOIN of TAXONOMY.md's projects section and the
    lit-config.yaml projects map).

    Each row is tagged with one drift marker:

    \b
      ✓                name in BOTH truth sources, path exists on disk
      ⚠ path-missing   name in both, but the config path is absent / not a
                       directory on this machine (likely cross-machine drift)
      ⚠ config-only    in lit-config.yaml but missing from TAXONOMY.md
      ⚠ taxonomy-only  in TAXONOMY.md but missing from the config map
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    _, parsed = _load_taxonomy(vault)
    config = load_config(vault)

    taxonomy_names = set(parsed[_PROJECTS_DICT])
    config_map = config.projects
    config_names = set(config_map)
    all_names = sorted(taxonomy_names | config_names)

    if not all_names:
        console.print("[dim](no projects registered)[/]")
        console.print(
            "[dim]Register one with "
            "`lit project add <name> --path <abs-path>`.[/]"
        )
        return

    table = Table(title="Projects", show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("path")
    table.add_column("status")

    for name in all_names:
        in_tax = name in taxonomy_names
        in_cfg = name in config_names
        path_str = config_map.get(name, "")
        if in_tax and in_cfg:
            path_ok = bool(path_str) and Path(path_str).expanduser().is_dir()
            status = (
                "[green]✓[/]"
                if path_ok
                else "[yellow]⚠ path-missing[/]"
            )
        elif in_cfg:
            status = "[yellow]⚠ config-only[/]"
        else:
            status = "[yellow]⚠ taxonomy-only[/]"
        table.add_row(
            escape(name),
            escape(path_str) if path_str else "[dim]—[/]",
            status,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


@project_group.command("rename")
@click.argument("old")
@click.argument("new")
@_library_option
@_vault_option
def project_rename_cmd(
    old: str,
    new: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Rename a project: TAXONOMY + config key + every paper + INDEX.

    Semantics-preserving (no data loss), so no confirmation prompt — same
    policy as lit taxonomy rename. The project's on-disk path is
    carried over unchanged under the new key.
    """
    old = old.strip()
    new = new.strip()
    if not new:
        raise TaxonomyError("`new` project name cannot be empty.")
    if old == new:
        raise TaxonomyError("`old` and `new` are identical — nothing to do.")

    vault = find_vault(resolve_library_or_vault(library, vault_name))
    text, parsed = _load_taxonomy(vault)
    config = load_config(vault)

    known = set(parsed[_PROJECTS_DICT]) | set(config.projects)
    if old not in known:
        raise TaxonomyError(
            f"Project {old!r} is not registered. "
            "Run `lit project list` to inspect."
        )
    if new in known:
        raise TaxonomyError(
            f"Project {new!r} is already registered. "
            "Pick a different name or `lit project rm` the conflicting one."
        )

    new_taxonomy_values = [
        new if v == old else v for v in parsed[_PROJECTS_DICT]
    ]
    new_taxonomy_text = update_user_dict_section(
        text, _PROJECTS_DICT, new_taxonomy_values
    )

    new_projects = {
        (new if k == old else k): v for k, v in config.projects.items()
    }
    new_config_text = _config_with_projects(vault, new_projects)

    n_changed, staged_meta_paths, all_papers = _ripple_replacements(
        vault, _PROJECTS_DICT, {old: new}, rename_relevance=True
    )
    fresh_index = render_index(all_papers, now_iso())

    with staged_write(vault, op_id=f"project-rename-{old}") as stage:
        stage.write_text("TAXONOMY.md", new_taxonomy_text)
        stage.write_text("lit-config.yaml", new_config_text)
        for relpath, content in staged_meta_paths:
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", fresh_index)

    # Post-commit derived rebuild via the shared funnel (M30 Phase 4):
    # INDEX + views/by-project/ (new name in, old name out) + every project's
    # symlinks + REFERENCES.md, all recomputed together from the committed
    # TRUTH. project_refs=True because a rename touches the project side
    # (mirrors the pre-funnel command's rebuild_all_project_{links,refs}).
    # The staged INDEX.json above is the crash-safety layer; the funnel
    # reloads config (= the just-committed new_projects) for the project side.
    reconcile_derived(vault, project_refs=True)

    console.print(
        f"[bold green]✓ Renamed[/] project {escape(old)} → {escape(new)}"
    )
    console.print(
        f"  Updated [bold]{n_changed}[/] paper"
        f"{'s' if n_changed != 1 else ''}."
    )


# ---------------------------------------------------------------------------
# set-path
# ---------------------------------------------------------------------------


@project_group.command("set-path")
@click.argument("name")
@click.argument(
    "new_path",
    type=click.Path(
        exists=False,
        file_okay=False,
        resolve_path=True,
        path_type=Path,
    ),
)
@_library_option
@_vault_option
def project_set_path_cmd(
    name: str,
    new_path: Path,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Change a project's on-disk path (config-only — papers store names).

    Goes through staged_write (single file, but keeps op-id / rollback
    consistency with the other project commands). Always prints the
    rebuild hint because the registry change does NOT physically move the
    directory.
    """
    name = name.strip()
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    config = load_config(vault)

    if name not in config.projects:
        raise TaxonomyError(
            f"Project {name!r} is not registered in lit-config.yaml. "
            "Run `lit project list` to inspect, or "
            f"`lit project add {name} --path <abs-path>` to register it."
        )
    if not new_path.exists():
        raise TaxonomyError(
            f"Path {str(new_path)!r} does not exist. "
            f"Create it first (e.g. `mkdir -p {new_path}`)."
        )
    if not new_path.is_dir():
        raise TaxonomyError(
            f"Path {str(new_path)!r} is not a directory."
        )

    new_path_str = str(new_path)
    if config.projects[name] == new_path_str:
        console.print(
            f"[yellow]No-op:[/] {escape(name)} already points at "
            f"{escape(new_path_str)}."
        )
        return

    new_projects = dict(config.projects)
    new_projects[name] = new_path_str
    new_config_text = _config_with_projects(vault, new_projects)

    with staged_write(vault, op_id=f"project-set-path-{name}") as stage:
        stage.write_text("lit-config.yaml", new_config_text)

    console.print(
        f"[bold green]✓ Updated[/] {escape(name)} → {escape(new_path_str)}"
    )
    console.print(
        "[dim]ℹ If the project directory wasn't physically moved, run "
        "`lit link --rebuild-all` to recreate symlinks at the new "
        "location.[/]"
    )


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


@project_group.command("rm")
@click.argument("name")
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt (for agents / scripts / CI).",
)
@_library_option
@_vault_option
def project_rm_cmd(
    name: str,
    yes: bool,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Delete a project: cascade-untag papers + drop from both truth sources.

    Cascade-with-confirm: if any paper references the project, a warning
    block lists them and a y/N prompt gates the teardown. --yes / -y
    skips the prompt; a non-tty without --yes aborts cleanly. With no
    references the command executes immediately (nothing to warn about).
    """
    name = name.strip()
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    text, parsed = _load_taxonomy(vault)
    config = load_config(vault)

    known = set(parsed[_PROJECTS_DICT]) | set(config.projects)
    if name not in known:
        raise TaxonomyError(
            f"Project {name!r} is not registered. "
            "Run `lit project list` to inspect."
        )

    papers = list_papers(vault)
    referencing = find_referencing_papers(papers, _PROJECTS_DICT, name)

    project_dir_str = config.projects.get(name)
    project_dir = (
        Path(project_dir_str).expanduser() if project_dir_str else None
    )

    if referencing:
        warning_lines = [
            f"[yellow]⚠[/] '{escape(name)}' is referenced by "
            f"[bold]{len(referencing)}[/] paper(s):",
        ]
        for pid in referencing[:10]:
            warning_lines.append(f"  - {escape(pid)}")
        if len(referencing) > 10:
            warning_lines.append(
                f"  ... and {len(referencing) - 10} more"
            )
        warning_lines.append("")
        warning_lines.append("Removing will:")
        warning_lines.append(
            f"  • Untag these {len(referencing)} paper(s): drop "
            f"'{escape(name)}' from their projects field"
        )
        warning_lines.append(
            "  • Remove from TAXONOMY.md and lit-config.yaml"
        )
        if project_dir is not None:
            warning_lines.append(
                f"  • Delete {escape(str(project_dir))}/"
                f"{LITERATURE_SUBDIR}/ symlinks + {REFERENCES_FILENAME}"
            )
        if not _confirm_destructive(warning_lines, yes=yes):
            console.print("[dim]Aborted. Nothing changed.[/]")
            return

    # Build the post-removal truth sources.
    new_taxonomy_values = [
        v for v in parsed[_PROJECTS_DICT] if v != name
    ]
    new_taxonomy_text = update_user_dict_section(
        text, _PROJECTS_DICT, new_taxonomy_values
    )
    new_projects = {
        k: v for k, v in config.projects.items() if k != name
    }
    new_config_text = _config_with_projects(vault, new_projects)

    n_changed, staged_meta_paths, all_papers = _ripple_removals(
        vault, _PROJECTS_DICT, name, drop_relevance=True
    )
    fresh_index = render_index(all_papers, now_iso())

    with staged_write(vault, op_id=f"project-rm-{name}") as stage:
        stage.write_text("TAXONOMY.md", new_taxonomy_text)
        stage.write_text("lit-config.yaml", new_config_text)
        for relpath, content in staged_meta_paths:
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", fresh_index)

    # Post-commit derived rebuild via the shared funnel (M30 Phase 4):
    # INDEX + views/by-project/ (the removed project drops out) recomputed
    # together. project_refs=False: the removed project's own symlinks +
    # REFERENCES.md are torn down explicitly below, and no other project's
    # membership changed — behavior identical to the pre-funnel command.
    reconcile_derived(vault, papers=list_papers(vault), project_refs=False)

    # Post-commit teardown of the project's on-disk artifacts. Mirrors the
    # unlink pattern: filesystem-mutating, cheap to redo, recoverable.
    if project_dir is not None and project_dir.is_dir():
        literature_dir = project_dir / LITERATURE_SUBDIR
        if literature_dir.is_dir():
            for child in literature_dir.iterdir():
                if child.is_symlink():
                    child.unlink()
            refs = literature_dir / REFERENCES_FILENAME
            if refs.exists():
                refs.unlink()

    console.print(
        f"[bold green]✓ Removed[/] project {escape(name)}."
    )
    console.print(
        f"  Untagged [bold]{n_changed}[/] paper"
        f"{'s' if n_changed != 1 else ''}."
    )
