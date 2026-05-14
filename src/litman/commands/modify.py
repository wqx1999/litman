"""``lit modify`` — edit fields on an existing paper's metadata.yaml.

Three operation flags, all repeatable:
    --set FIELD=VALUE       set a scalar field
    --add-tag FIELD=VALUE   append a value to a list field (deduped)
    --rm-tag FIELD=VALUE    remove a value from a list field (silent if absent)

Operations within a single invocation apply in the order
(--set, then --add-tag, then --rm-tag), so a single command line can do a
full reclassification:

    lit modify 2023_Pandi_Cell-free \\
        --set priority=A --set status=deep-read \\
        --add-tag topics=peptide --add-tag methods=cell-free

Multi-file atomicity (metadata.yaml + INDEX.json) goes through
``staged_write`` from M2.3. The views/by-*/ symlink hubs are rebuilt
separately in a best-effort step after the staged commit succeeds.

TAXONOMY validation (rejecting --add-tag values that aren't registered)
lands in M2.5 — until then, --add-tag silently records arbitrary values.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from ruamel.yaml import YAML

from litman.core.atomic import staged_write
from litman.core.document import list_papers, read_metadata
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input
from litman.core.views import rebuild_views, render_index
from litman.exceptions import ModifyError, PaperNotFoundError

console = Console()

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True
_yaml.default_flow_style = False

# Fields the user must NOT modify via `lit modify`. id is changed via
# `lit rename` (M2.6) which also renames the paper dir; the audit fields
# are machine-maintained. Trying to --set any of these raises ModifyError.
FORBIDDEN_SET_FIELDS: frozenset[str] = frozenset({
    "id",
    "created-at",
    "updated-at",
})

# Canonical list-typed fields. --add-tag / --rm-tag operate on these only;
# attempting --add-tag on a non-listed field raises so users don't
# accidentally clobber a scalar. Schemaless metadata is preserved for
# --set, so future custom scalar fields work without a registry update.
LIST_FIELDS: frozenset[str] = frozenset({
    "authors",
    "projects",
    "topics",
    "methods",
    "data",
    "related",
    "contradicts",
    "extends",
    "code-clones",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_kv(spec: str, flag_name: str) -> tuple[str, str]:
    """Split a `key=value` pair, raising ModifyError on malformed input."""
    if "=" not in spec:
        raise ModifyError(
            f"{flag_name} expects KEY=VALUE, got {spec!r}. "
            "Example: --set priority=A"
        )
    key, _, value = spec.partition("=")
    key = key.strip()
    if not key:
        raise ModifyError(f"{flag_name} got empty key in {spec!r}.")
    return key, value


def _coerce_scalar(value: str) -> Any:
    """Coerce a CLI string to int when fully numeric, None when empty, else str.

    Predictable rules so users can reason about what `--set year=2023`
    actually writes. Dates, DOIs, paths stay as strings, which round-trip
    cleanly through ruamel.yaml.
    """
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _apply_set(metadata: dict[str, Any], key: str, raw_value: str) -> tuple[Any, Any]:
    """Apply a --set op. Returns (before, after) for the diff summary."""
    if key in FORBIDDEN_SET_FIELDS:
        if key == "id":
            raise ModifyError(
                "Cannot --set id. Use `lit rename <old> <new>` (M2.6) so the "
                "paper directory and back-references stay in sync."
            )
        raise ModifyError(
            f"Cannot --set {key!r}: machine-maintained audit field."
        )
    if key in LIST_FIELDS:
        raise ModifyError(
            f"--set on list field {key!r} would clobber it. "
            f"Use --add-tag {key}=<value> / --rm-tag {key}=<value> instead."
        )
    before = metadata.get(key)
    after = _coerce_scalar(raw_value)
    metadata[key] = after
    return before, after


def _apply_add_tag(
    metadata: dict[str, Any], key: str, value: str
) -> tuple[list[Any], list[Any]] | None:
    """Apply --add-tag. Returns (before, after) only when a change happened."""
    if key not in LIST_FIELDS:
        raise ModifyError(
            f"--add-tag rejects {key!r}: not a list field. "
            f"Allowed: {', '.join(sorted(LIST_FIELDS))}."
        )
    if not value:
        raise ModifyError(f"--add-tag {key}= got empty value.")
    current = metadata.get(key)
    before = list(current) if current else []
    if value in before:
        return None  # idempotent no-op
    after = before + [value]
    metadata[key] = after
    return before, after


def _apply_rm_tag(
    metadata: dict[str, Any], key: str, value: str
) -> tuple[list[Any], list[Any]] | None:
    """Apply --rm-tag. Silent no-op if the value isn't present."""
    if key not in LIST_FIELDS:
        raise ModifyError(
            f"--rm-tag rejects {key!r}: not a list field. "
            f"Allowed: {', '.join(sorted(LIST_FIELDS))}."
        )
    current = metadata.get(key)
    before = list(current) if current else []
    if value not in before:
        return None  # absent → already in desired state
    after = [v for v in before if v != value]
    metadata[key] = after
    return before, after


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _format_diff_value(value: Any) -> str:
    """Compact representation for the before/after summary."""
    if value is None:
        return "—"
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join(str(v) for v in value) + "]"
    return repr(value) if isinstance(value, str) else str(value)


@click.command("modify")
@click.argument(
    "paper_id", required=False, shell_complete=complete_paper_id
)
@click.option(
    "--paper-doi",
    "paper_doi",
    default=None,
    help=(
        "Reverse-lookup the paper by DOI instead of supplying the id. "
        "Mutually exclusive with the positional paper id."
    ),
)
@click.option(
    "--set",
    "set_ops",
    multiple=True,
    metavar="KEY=VALUE",
    help="Set a scalar field. Repeatable. Empty value unsets (writes null).",
)
@click.option(
    "--add-tag",
    "add_tag_ops",
    multiple=True,
    metavar="FIELD=VALUE",
    help="Append a value to a list field (deduped). Repeatable.",
)
@click.option(
    "--rm-tag",
    "rm_tag_ops",
    multiple=True,
    metavar="FIELD=VALUE",
    help="Remove a value from a list field (silent if absent). Repeatable.",
)
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
def modify_cmd(
    paper_id: str | None,
    paper_doi: str | None,
    set_ops: tuple[str, ...],
    add_tag_ops: tuple[str, ...],
    rm_tag_ops: tuple[str, ...],
    library: Path | None,
    vault_name: str | None,
) -> None:
    """Edit fields on an existing paper's metadata.yaml.

    The paper id accepts a full id, a unique case-insensitive substring,
    or omit it and pass ``--paper-doi <DOI>`` instead.

    Updates ``papers/<id>/metadata.yaml`` (with a refreshed ``updated-at``
    audit timestamp) and ``INDEX.json`` atomically; ``views/by-*/`` symlinks
    are rebuilt afterwards.
    """
    if not (set_ops or add_tag_ops or rm_tag_ops):
        raise ModifyError(
            "lit modify requires at least one of --set / --add-tag / --rm-tag. "
            "Run `lit modify --help` for examples."
        )

    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    meta_file = vault / "papers" / paper_id / "metadata.yaml"
    if not meta_file.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )

    # Roundtrip-load preserves comments and quoting in the metadata file.
    metadata = _yaml.load(meta_file.read_text(encoding="utf-8"))
    if metadata is None:
        raise ModifyError(
            f"metadata.yaml at {meta_file} is empty — refusing to modify. "
            "Restore the file or re-run `lit add`."
        )

    diffs: list[tuple[str, Any, Any]] = []

    for spec in set_ops:
        key, value = _parse_kv(spec, "--set")
        before, after = _apply_set(metadata, key, value)
        diffs.append((key, before, after))

    for spec in add_tag_ops:
        key, value = _parse_kv(spec, "--add-tag")
        change = _apply_add_tag(metadata, key, value)
        if change is not None:
            before, after = change
            diffs.append((key, before, after))

    for spec in rm_tag_ops:
        key, value = _parse_kv(spec, "--rm-tag")
        change = _apply_rm_tag(metadata, key, value)
        if change is not None:
            before, after = change
            diffs.append((key, before, after))

    # Even if every requested op was a no-op (e.g. --add-tag of an existing
    # value), bumping updated-at is wrong because nothing changed. Detect
    # the all-no-op case and short-circuit.
    if not diffs:
        console.print(
            f"[yellow]No-op:[/] every requested change to {paper_id} was "
            "already in effect. metadata.yaml not touched."
        )
        return

    new_updated_at = _now_iso()
    old_updated_at = metadata.get("updated-at")
    metadata["updated-at"] = new_updated_at

    metadata_yaml = _dump_yaml_to_string(metadata)

    # Re-render INDEX.json from the latest paper list, splicing in our
    # in-memory modified copy so the index reflects the staged change
    # without depending on disk state.
    all_papers = list_papers(vault)
    all_papers = [p for p in all_papers if p.get("id") != paper_id]
    # ruamel YAML's CommentedMap is dict-compatible for our consumers.
    all_papers.append(dict(metadata))
    index_json = render_index(all_papers, _now_iso())

    rel_meta = f"papers/{paper_id}/metadata.yaml"
    with staged_write(vault, op_id=f"modify-{paper_id}") as stage:
        stage.write_text(rel_meta, metadata_yaml)
        stage.write_text("INDEX.json", index_json)

    # views/ rebuild is filesystem-mutating but not text-file-atomic; do it
    # after the staged commit so a failure here leaves the metadata + index
    # consistent and only views/ stale (recoverable via `lit refresh-views`).
    fresh_papers = list_papers(vault)
    rebuild_views(vault, fresh_papers)

    # ----- Output -----
    console.print(f"[bold green]✓ Modified[/] {paper_id}")
    for key, before, after in diffs:
        # `escape()` keeps literal `[]` from being parsed as Rich markup tags
        # — list-typed values (e.g. "[peptide]") would otherwise vanish.
        console.print(
            f"  {key}: [dim]{escape(_format_diff_value(before))}[/] → "
            f"{escape(_format_diff_value(after))}"
        )
    console.print(
        f"  updated-at: [dim]{escape(str(old_updated_at))}[/] → "
        f"{escape(new_updated_at)}"
    )
    console.print("[dim]INDEX.json + views/ refreshed.[/]")
