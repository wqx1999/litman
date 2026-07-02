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

from litman.core.config import config_to_yaml_dict, load_config
from litman.core.confirm import _confirm_destructive
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.project_link import (
    add_project,
    remove_project,
    rename_project,
    set_project_path,
)
from litman.core.project_refs import (
    LITERATURE_SUBDIR,
    REFERENCES_FILENAME,
)
from litman.core.taxonomy import (
    find_referencing_papers,
    parse_taxonomy,
)
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import TaxonomyError

console = Console()

_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    preserve_quotes=True,
    default_flow_style=False,
)

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
        "Full path to an existing folder (the folder itself, not its "
        "parent). litman does not create it."
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
    """Link a project NAME to an existing folder (nothing is created).

    NAME is a label papers tag with; it may differ from the folder's own
    name. --path is the full path to that folder (not its parent), which
    must already exist.
    """
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    summary = add_project(vault, name, project_path)

    console.print(
        f"[bold green]✓ Registered[/] {escape(summary['name'])} → "
        f"{escape(summary['path'])}"
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
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    # Single write path (invariant #16): validation + atomic dual-write +
    # derived rebuild all live in core.project_link.rename_project, shared with
    # the webUI's PUT /api/projects/{name}. The command only renders the result.
    n_changed, _ = rename_project(vault, old, new)

    console.print(
        f"[bold green]✓ Renamed[/] project {escape(old.strip())} → "
        f"{escape(new.strip())}"
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
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    # Single write path (invariant #16): validation + atomic config write live in
    # core.project_link.set_project_path, shared with the webUI's
    # PUT /api/projects/{name}/path. The command only renders the result.
    result = set_project_path(vault, name, new_path)
    name_str = result["name"]
    new_path_str = result["path"]

    if not result["changed"]:
        console.print(
            f"[yellow]No-op:[/] {escape(name_str)} already points at "
            f"{escape(new_path_str)}."
        )
        return

    console.print(
        f"[bold green]✓ Updated[/] {escape(name_str)} → {escape(new_path_str)}"
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
    _, parsed = _load_taxonomy(vault)
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

    # The cascade write (dual TAXONOMY + config rewrite, metadata + INDEX, derived
    # rebuild, then symlink/REFERENCES teardown) lives in the core so the webUI
    # DELETE endpoint shares the exact write path (invariant #16). The command
    # keeps only the confirm gate + console output.
    n_changed, _ = remove_project(vault, name)

    console.print(
        f"[bold green]✓ Removed[/] project {escape(name)}."
    )
    console.print(
        f"  Untagged [bold]{n_changed}[/] paper"
        f"{'s' if n_changed != 1 else ''}."
    )
