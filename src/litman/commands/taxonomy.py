"""``lit taxonomy`` — manage controlled vocabulary in TAXONOMY.md.

Five subcommands:

* ``list``   — read-only listing of one or all dicts
* ``add``    — register new value(s) in a user dict
* ``rename`` — rename a value, rippling to all referencing metadata.yaml
* ``merge``  — fold multiple values into one, rippling
* ``rm``     — remove a value (refused if any paper still references it)

The user-extensible dictionaries are ``projects``, ``topics``,
``methods``, ``data``. Fixed-enum dicts (``type``, ``status``,
``priority``) are listable here but write attempts are rejected — those
require a code release because the application's fixed enums must
change in lockstep.

All write subcommands route through :func:`litman.core.atomic.staged_write`
so a TAXONOMY.md edit, the cascade of metadata.yaml rewrites, and the
INDEX.json refresh either all land or none do.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.document import list_papers
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.taxonomy import (
    ALL_DICTS,
    FIXED_DICTS,
    USER_DICTS,
    USER_DICT_TO_METADATA_FIELD,
    find_referencing_papers,
    parse_taxonomy,
    replace_value_in_field,
    update_user_dict_section,
)
from litman.core.views import rebuild_views, render_index
from litman.exceptions import TaxonomyError

console = Console()

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.default_flow_style = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _validate_user_dict(dict_name: str) -> None:
    """Reject unknown dicts and fixed enums (writable subcommands only)."""
    if dict_name in FIXED_DICTS:
        raise TaxonomyError(
            f"Cannot modify fixed-enum dict {dict_name!r}. "
            "Fixed enums (type, status, priority) require a code release "
            "because the app's enum lists must change in lockstep."
        )
    if dict_name not in USER_DICTS:
        raise TaxonomyError(
            f"Unknown dict {dict_name!r}. "
            f"User-extensible dicts: {', '.join(USER_DICTS)}."
        )


def _load_taxonomy(vault: Path) -> tuple[str, dict[str, list[str]]]:
    """Read TAXONOMY.md and return (raw_text, parsed_dict)."""
    path = vault / "TAXONOMY.md"
    text = path.read_text(encoding="utf-8")
    return text, parse_taxonomy(text)


# ---------------------------------------------------------------------------
# `lit taxonomy` group
# ---------------------------------------------------------------------------


@click.group("taxonomy")
def taxonomy_group() -> None:
    """Manage TAXONOMY.md — controlled vocabulary for paper metadata."""


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@taxonomy_group.command("list")
@click.argument("dict_name", required=False)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
    help="Vault path. Defaults to $LIT_LIBRARY or cwd-walk discovery.",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml (M8). "
        "Mutually exclusive with --library."
    ),
)
def taxonomy_list_cmd(
    dict_name: str | None,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """List values in one dict (or all dicts when no name given)."""
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    _, parsed = _load_taxonomy(vault)

    if dict_name is not None:
        if dict_name not in ALL_DICTS:
            raise TaxonomyError(
                f"Unknown dict {dict_name!r}. "
                f"Known dicts: {', '.join(ALL_DICTS)}."
            )
        _print_single_dict(dict_name, parsed.get(dict_name, []))
        return

    table = Table(title="Taxonomy", show_header=True, header_style="bold")
    table.add_column("dict")
    table.add_column("kind")
    table.add_column("count", justify="right")
    table.add_column("values")
    for name in ALL_DICTS:
        kind = "user" if name in USER_DICTS else "fixed"
        values = parsed.get(name, [])
        rendered = ", ".join(values) if values else "[dim]—[/]"
        table.add_row(name, kind, str(len(values)), rendered)
    console.print(table)


def _print_single_dict(name: str, values: list[str]) -> None:
    kind = "user-extensible" if name in USER_DICTS else "fixed enum"
    console.print(f"[bold]{escape(name)}[/] [dim]({kind})[/]")
    if not values:
        console.print("  [dim](empty)[/]")
        return
    for v in values:
        console.print(f"  - {escape(v)}")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@taxonomy_group.command("add")
@click.argument("dict_name")
@click.argument("values", nargs=-1, required=True)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml (M8). "
        "Mutually exclusive with --library."
    ),
)
def taxonomy_add_cmd(
    dict_name: str,
    values: tuple[str, ...],
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Register new value(s) in a user dict.

    Already-present values are silent no-ops. The dict body is rewritten
    in sorted order regardless of the input order.
    """
    _validate_user_dict(dict_name)
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    text, parsed = _load_taxonomy(vault)

    current = parsed[dict_name]
    added: list[str] = []
    skipped: list[str] = []
    new_set = set(current)
    for v in values:
        v = v.strip()
        if not v:
            raise TaxonomyError("Empty value is not allowed.")
        if v in new_set:
            skipped.append(v)
            continue
        new_set.add(v)
        added.append(v)

    if not added:
        console.print(
            f"[yellow]No-op:[/] every value already present in {dict_name}."
        )
        return

    new_body = sorted(new_set)
    new_text = update_user_dict_section(text, dict_name, new_body)

    with staged_write(vault, op_id=f"taxonomy-add-{dict_name}") as stage:
        stage.write_text("TAXONOMY.md", new_text)

    console.print(f"[bold green]✓ Updated[/] {escape(dict_name)}")
    for v in sorted(added):
        console.print(f"  + {escape(v)}")
    for v in sorted(skipped):
        console.print(f"  [dim]= {escape(v)} (already present)[/]")


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


@taxonomy_group.command("rename")
@click.argument("dict_name")
@click.argument("old")
@click.argument("new")
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml (M8). "
        "Mutually exclusive with --library."
    ),
)
def taxonomy_rename_cmd(
    dict_name: str,
    old: str,
    new: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Rename a value in a user dict and ripple to all referencing papers."""
    _validate_user_dict(dict_name)
    if old == new:
        raise TaxonomyError("`old` and `new` are identical — nothing to do.")
    if not new.strip():
        raise TaxonomyError("`new` value cannot be empty.")
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    text, parsed = _load_taxonomy(vault)
    current = parsed[dict_name]
    if old not in current:
        raise TaxonomyError(
            f"{old!r} is not registered in {dict_name}. "
            f"Run `lit taxonomy list {dict_name}` to inspect."
        )
    if new in current:
        raise TaxonomyError(
            f"{new!r} is already in {dict_name}. "
            f"Use `lit taxonomy merge {dict_name} {old} --into {new}` to fold them."
        )

    new_body = [new if v == old else v for v in current]
    new_taxonomy_text = update_user_dict_section(text, dict_name, new_body)

    field = USER_DICT_TO_METADATA_FIELD[dict_name]
    n_changed, staged_meta_paths, all_papers = _ripple_replacements(
        vault, field, {old: new}
    )

    fresh_index = render_index(all_papers, _now_iso())

    with staged_write(vault, op_id=f"taxonomy-rename-{dict_name}") as stage:
        stage.write_text("TAXONOMY.md", new_taxonomy_text)
        for relpath, content in staged_meta_paths:
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", fresh_index)

    if n_changed > 0:
        rebuild_views(vault, list_papers(vault))

    console.print(
        f"[bold green]✓ Renamed[/] {escape(dict_name)}: "
        f"{escape(old)} → {escape(new)}"
    )
    console.print(
        f"  Updated [bold]{n_changed}[/] paper{'s' if n_changed != 1 else ''}."
    )


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


@taxonomy_group.command("merge")
@click.argument("dict_name")
@click.argument("sources", nargs=-1, required=True)
@click.option(
    "--into",
    "dest",
    required=True,
    help="Destination value to merge into. May be one of the sources.",
)
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml (M8). "
        "Mutually exclusive with --library."
    ),
)
def taxonomy_merge_cmd(
    dict_name: str,
    sources: tuple[str, ...],
    dest: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Fold one or more source values into a destination value.

    All sources must already be registered. The dest may be one of the
    sources (in which case the others are removed) or a new value (in
    which case it is added).
    """
    _validate_user_dict(dict_name)
    if not dest.strip():
        raise TaxonomyError("--into value cannot be empty.")
    if not sources:
        raise TaxonomyError("At least one source value is required.")
    sources_unique = list(dict.fromkeys(sources))  # dedupe, preserve order
    # Drop dest from the sources-to-remove set; merging X into X is silly,
    # but tolerating it lets `merge a b --into a` work as "remove b".
    sources_to_remove = [s for s in sources_unique if s != dest]
    if not sources_to_remove:
        raise TaxonomyError(
            "All sources equal the destination — nothing to merge."
        )

    vault = find_vault(resolve_library_or_vault(library, vault_name))
    text, parsed = _load_taxonomy(vault)
    current = parsed[dict_name]
    missing = [s for s in sources_unique if s not in current]
    if missing:
        raise TaxonomyError(
            f"Sources not registered in {dict_name}: {', '.join(missing)}. "
            f"Run `lit taxonomy list {dict_name}` to inspect."
        )

    # Compute the new dict: drop sources_to_remove, ensure dest present.
    remaining = [v for v in current if v not in sources_to_remove]
    if dest not in remaining:
        remaining.append(dest)
    new_taxonomy_text = update_user_dict_section(text, dict_name, remaining)

    field = USER_DICT_TO_METADATA_FIELD[dict_name]
    replacements = {s: dest for s in sources_to_remove}
    n_changed, staged_meta_paths, all_papers = _ripple_replacements(
        vault, field, replacements
    )

    fresh_index = render_index(all_papers, _now_iso())

    with staged_write(vault, op_id=f"taxonomy-merge-{dict_name}") as stage:
        stage.write_text("TAXONOMY.md", new_taxonomy_text)
        for relpath, content in staged_meta_paths:
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", fresh_index)

    if n_changed > 0:
        rebuild_views(vault, list_papers(vault))

    console.print(
        f"[bold green]✓ Merged[/] {escape(dict_name)}: "
        f"{', '.join(escape(s) for s in sources_to_remove)} → {escape(dest)}"
    )
    console.print(
        f"  Updated [bold]{n_changed}[/] paper{'s' if n_changed != 1 else ''}."
    )


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


@taxonomy_group.command("rm")
@click.argument("dict_name")
@click.argument("value")
@click.option(
    "--library",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    envvar="LIT_LIBRARY",
)
@click.option(
    "--vault",
    "vault_name",
    default=None,
    help=(
        "Vault name from ~/.config/litman/vaults.yaml (M8). "
        "Mutually exclusive with --library."
    ),
)
def taxonomy_rm_cmd(
    dict_name: str,
    value: str,
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Remove a value (refused if any paper still references it).

    No automatic ripple — users must clear references via
    ``lit modify --rm-tag <field>=<value>`` first. This is intentional:
    silent deletion of a tag from many papers should be an explicit choice.
    """
    _validate_user_dict(dict_name)
    vault = find_vault(resolve_library_or_vault(library, vault_name))
    text, parsed = _load_taxonomy(vault)
    current = parsed[dict_name]
    if value not in current:
        raise TaxonomyError(
            f"{value!r} is not registered in {dict_name}."
        )

    papers = list_papers(vault)
    referencing = find_referencing_papers(papers, dict_name, value)
    if referencing:
        joined = "\n  - ".join(referencing[:10])
        more = (
            f"\n  ... and {len(referencing) - 10} more"
            if len(referencing) > 10
            else ""
        )
        raise TaxonomyError(
            f"Cannot remove {value!r} from {dict_name}: "
            f"{len(referencing)} paper(s) still reference it.\n  - {joined}{more}\n"
            f"Run `lit modify <id> --rm-tag {dict_name}={value}` "
            "on each paper first."
        )

    new_body = [v for v in current if v != value]
    new_text = update_user_dict_section(text, dict_name, new_body)

    with staged_write(vault, op_id=f"taxonomy-rm-{dict_name}") as stage:
        stage.write_text("TAXONOMY.md", new_text)

    console.print(
        f"[bold green]✓ Removed[/] {escape(value)} from {escape(dict_name)}."
    )


# ---------------------------------------------------------------------------
# Shared helper: ripple replacements across all metadata.yaml
# ---------------------------------------------------------------------------


def _ripple_replacements(
    vault: Path,
    field: str,
    replacements: dict[str, str],
) -> tuple[int, list[tuple[str, str]], list[dict[str, Any]]]:
    """Apply ``replacements`` to ``field`` of every paper that references any source.

    Returns:
        (n_changed, staged_writes, all_papers_with_changes_applied)

        * ``n_changed`` — count of paper metadata files that changed
        * ``staged_writes`` — ``[(relpath, new_yaml_text), ...]`` ready to
          hand to :func:`staged_write`
        * ``all_papers_with_changes_applied`` — full paper list with
          in-memory modifications, suitable for re-rendering INDEX.json
    """
    papers = list_papers(vault)
    staged: list[tuple[str, str]] = []
    n_changed = 0
    sources = set(replacements.keys())
    now = _now_iso()

    # Re-load each touched metadata.yaml in roundtrip mode so we can dump
    # it back preserving formatting. The paper list returned by
    # `list_papers` uses the safe loader and is fine for INDEX rendering,
    # but writing requires the roundtrip representation.
    for paper in papers:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        values = paper.get(field) or []
        if not (sources & set(values)):
            continue
        meta_path = vault / "papers" / str(paper_id) / "metadata.yaml"
        rt_metadata = _yaml.load(meta_path.read_text(encoding="utf-8"))
        if rt_metadata is None:
            continue
        if replace_value_in_field(rt_metadata, field, replacements):
            rt_metadata["updated-at"] = now
            staged.append(
                (
                    f"papers/{paper_id}/metadata.yaml",
                    _dump_yaml_to_string(rt_metadata),
                )
            )
            # Also mutate the safe-loaded copy so the INDEX render reflects
            # the change without a re-read.
            paper[field] = list(rt_metadata[field])
            paper["updated-at"] = now
            n_changed += 1

    return n_changed, staged, papers
