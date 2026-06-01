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
from litman.core.correctors import reconcile_derived
from litman.core.document import list_papers, load_yaml_or_raise, read_metadata
from litman.core.library import find_vault, resolve_library_or_vault
from litman.core.paper_lookup import complete_paper_id, resolve_paper_input
from litman.core.relations import RELATION_PAIRS, REVERSE_REF_FIELDS
from litman.core.taxonomy import USER_DICTS, parse_taxonomy
from litman.core.views import render_index
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

# Canonical list-typed fields. The list-semantics primitives
# (``_apply_add_tag`` / ``_apply_rm_tag``) operate on these; attempting
# them on a non-listed field raises so users don't accidentally clobber a
# scalar. Schemaless metadata is preserved for --set, so future custom
# scalar fields work without a registry update. This set INCLUDES the
# ADR-012 reverse fields (``extended-by`` / ``contradicted-by``) so the
# auto double-write can maintain them with correct list semantics — but
# users may NOT name them on the command line (see USER_TAG_FIELDS).
LIST_FIELDS: frozenset[str] = frozenset({
    "authors",
    "projects",
    "topics",
    "methods",
    "data",
    "related",
    "contradicts",
    "contradicted-by",
    "extends",
    "extended-by",
    "code-clones",
})

# Reverse relation fields (ADR-012) are maintained ONLY by the auto
# double-write; a user must never set them directly via --add-tag /
# --rm-tag, or the pairing breaks. The canonical set lives in
# core/relations.py (single de-drift source); imported as REVERSE_REF_FIELDS.

# Relation fields whose forward direction the user MAY drive. Writing any
# of these triggers the paired reverse write on the opposite paper.
RELATION_TAG_FIELDS: frozenset[str] = frozenset(RELATION_PAIRS) - REVERSE_REF_FIELDS

# Fields a user may name in --add-tag / --rm-tag: every list field except
# the reverse relation fields.
USER_TAG_FIELDS: frozenset[str] = LIST_FIELDS - REVERSE_REF_FIELDS


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
    vault: Path, metadata: dict[str, Any], key: str, value: str
) -> tuple[list[Any], list[Any]] | None:
    """Apply --add-tag. Returns (before, after) only when a change happened.

    Register-first validation (M15): for the four USER_DICTS
    (``projects`` / ``topics`` / ``methods`` / ``data``) the value MUST
    already be registered in TAXONOMY.md. This closes the channel by which
    an agent could schema-drift the controlled vocabulary through
    ``--add-tag``. Schemaless scalars, reference fields, and fixed enums
    are NOT validated here (invariant #7 — schemaless is the foundation;
    refs go through dangling-ref health checks; fixed enums are
    hard-coded). ``--rm-tag`` is never validated (clearing a stale value
    is a legitimate cleanup action).
    """
    if key not in LIST_FIELDS:
        raise ModifyError(
            f"--add-tag rejects {key!r}: not a list field. "
            f"Allowed: {', '.join(sorted(USER_TAG_FIELDS))}."
        )
    if not value:
        raise ModifyError(f"--add-tag {key}= got empty value.")
    if key in USER_DICTS:
        registered = parse_taxonomy(
            (vault / "TAXONOMY.md").read_text(encoding="utf-8")
        )[key]
        if value not in registered:
            if key == "projects":
                hint = (
                    f"Run `lit project add {value} --path <abs-path>` "
                    "first."
                )
            else:
                hint = f"Run `lit taxonomy add {key} {value}` first."
            raise ModifyError(
                f"{key!r} value {value!r} is not registered in "
                f"TAXONOMY.md. {hint}"
            )
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
            f"Allowed: {', '.join(sorted(USER_TAG_FIELDS))}."
        )
    current = metadata.get(key)
    before = list(current) if current else []
    if value not in before:
        return None  # absent → already in desired state
    after = [v for v in before if v != value]
    metadata[key] = after
    return before, after


def _reject_reverse_field(key: str, flag_name: str) -> None:
    """Forbid a user from naming a reverse relation field directly.

    Reverse fields (``extended-by`` / ``contradicted-by``) are maintained
    only by the auto double-write (ADR-012). Letting a user set them by
    hand would break the forward/reverse pairing.
    """
    if key in REVERSE_REF_FIELDS:
        forward = RELATION_PAIRS[key]
        raise ModifyError(
            f"{flag_name} {key!r} is not allowed: {key!r} is a reverse "
            f"relation field, maintained automatically when you write its "
            f"forward field {forward!r}. "
            f"Use `{flag_name} {forward}=<other-paper-id>` on the other "
            "paper instead."
        )


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


def _apply_modify(
    vault: Path,
    paper_id: str,
    set_ops: tuple[str, ...] = (),
    add_tag_ops: tuple[str, ...] = (),
    rm_tag_ops: tuple[str, ...] = (),
    skip_set_noop: bool = False,
) -> bool:
    """Apply set / add-tag / rm-tag ops to one paper's metadata.yaml.

    Shared backend for ``lit modify`` and the M13 semantic-sugar commands
    (``lit read`` / ``lit revisit`` / ``lit drop`` / ``lit promote`` /
    ``lit skim``). Handles the full pipeline:

    1. Resolve metadata file path (caller has already resolved ``paper_id``
       through ``resolve_paper_input``).
    2. Load metadata, apply ops, compute diffs.
    3. No-op short-circuit if every op was already in effect — does NOT
       bump ``updated-at`` and skips staged_write entirely.
    4. Otherwise: bump ``updated-at``, render INDEX.json, staged-write
       both files atomically, rebuild views/.
    5. Print human-readable diff summary to the console.

    Args:
        vault: Vault root (already discovered).
        paper_id: Canonical paper id (already resolved through fuzzy /
            DOI channel).
        set_ops: Sequence of ``"key=value"`` --set specs.
        add_tag_ops: Sequence of ``"key=value"`` --add-tag specs.
        rm_tag_ops: Sequence of ``"key=value"`` --rm-tag specs.
        skip_set_noop: If True, --set ops whose new value equals the
            current value are silently dropped from the diff (no
            ``updated-at`` bump if every op turns out to be redundant).
            Default False preserves ``lit modify``'s public contract:
            redundant ``--set`` still bumps ``updated-at`` (since the
            user explicitly asked to write that value). M13 sugar
            commands pass True so that ``lit read X`` twice in one day
            is a true no-op.

    Returns:
        ``True`` when at least one change landed on disk, ``False`` when
        every op was a redundant no-op and metadata.yaml was not touched.

    Raises:
        PaperNotFoundError: ``papers/<id>/metadata.yaml`` does not exist.
        ModifyError: empty metadata file, malformed key=value spec, or
            an op rejected by ``_apply_set`` / ``_apply_add_tag`` /
            ``_apply_rm_tag`` (forbidden field, wrong type, etc.).
    """
    meta_file = vault / "papers" / paper_id / "metadata.yaml"
    if not meta_file.is_file():
        raise PaperNotFoundError(
            f"No paper with id {paper_id!r} in vault {vault}. "
            "Run `lit list` to see available ids."
        )

    # Roundtrip-load preserves comments and quoting in the metadata file.
    metadata = load_yaml_or_raise(meta_file, _yaml)
    if metadata is None:
        raise ModifyError(
            f"metadata.yaml at {meta_file} is empty — refusing to modify. "
            "Restore the file or re-run `lit add`."
        )

    diffs: list[tuple[str, Any, Any]] = []
    # Relation tag ops that landed on the originating paper, recorded as
    # (op_kind, forward_field, opposite_paper_id). After the originating
    # side is settled, each drives a paired write on the opposite paper.
    relation_ops: list[tuple[str, str, str]] = []

    for spec in set_ops:
        key, value = _parse_kv(spec, "--set")
        before, after = _apply_set(metadata, key, value)
        if skip_set_noop and before == after:
            # Sugar-command path (lit read / drop / etc.): same-value set
            # is treated as a true no-op so a repeated `lit read X`
            # doesn't bump updated-at. The default `lit modify --set`
            # path always counts the op (preserves its public contract:
            # explicit --set always bumps updated-at).
            continue
        diffs.append((key, before, after))

    for spec in add_tag_ops:
        key, value = _parse_kv(spec, "--add-tag")
        _reject_reverse_field(key, "--add-tag")
        change = _apply_add_tag(vault, metadata, key, value)
        if change is not None:
            before, after = change
            diffs.append((key, before, after))
        if key in RELATION_TAG_FIELDS:
            relation_ops.append(("add", key, value))

    for spec in rm_tag_ops:
        key, value = _parse_kv(spec, "--rm-tag")
        _reject_reverse_field(key, "--rm-tag")
        change = _apply_rm_tag(metadata, key, value)
        if change is not None:
            before, after = change
            diffs.append((key, before, after))
        if key in RELATION_TAG_FIELDS:
            relation_ops.append(("rm", key, value))

    # ----- ADR-012 auto double-write -----
    # For each relation tag the user wrote, mirror it onto the opposite
    # paper's paired field within the SAME transaction. The originating
    # side's no-op status is irrelevant: even if A.extends:[B] was already
    # present, B.extended-by may be missing in a vault that predates this
    # feature, so we still reconcile the opposite side. Opposite writes are
    # collected as paper_id → (in-memory metadata, relpath); only those
    # whose metadata actually changed are committed.
    opposite_writes: dict[str, dict[str, Any]] = {}
    for op_kind, forward, opposite_id in relation_ops:
        reverse = RELATION_PAIRS[forward]
        if opposite_id == paper_id:
            continue  # self-reference: no separate opposite to write
        opp_file = vault / "papers" / opposite_id / "metadata.yaml"
        if not opp_file.is_file():
            # Opposite paper missing: write the originating side only. The
            # one-directional residual is reported later by
            # check_bidirectional_refs; we never create the opposite.
            continue
        opp_meta = opposite_writes.get(opposite_id)
        if opp_meta is None:
            opp_meta = load_yaml_or_raise(opp_file, _yaml)
            if opp_meta is None:
                continue
        if op_kind == "add":
            # Reuse the dedup primitive: already present → no change.
            opp_change = _apply_add_tag(vault, opp_meta, reverse, paper_id)
        else:
            opp_change = _apply_rm_tag(opp_meta, reverse, paper_id)
        if opp_change is not None:
            opposite_writes[opposite_id] = opp_meta

    # Even if every requested op was a no-op (e.g. --add-tag of an existing
    # value), bumping updated-at is wrong because nothing changed. Detect
    # the all-no-op case and short-circuit. An opposite-only change (the
    # originating side already had the edge, the opposite was missing it)
    # still counts as a real change worth committing.
    if not diffs and not opposite_writes:
        console.print(
            f"[yellow]No-op:[/] every requested change to {paper_id} was "
            "already in effect. metadata.yaml not touched."
        )
        return False

    new_updated_at = _now_iso()
    old_updated_at = metadata.get("updated-at")
    metadata["updated-at"] = new_updated_at

    metadata_yaml = _dump_yaml_to_string(metadata)
    for opp_meta in opposite_writes.values():
        opp_meta["updated-at"] = new_updated_at

    # Re-render INDEX.json from the latest paper list, splicing in our
    # in-memory modified copies (originating + every touched opposite) so
    # the index reflects the staged change without depending on disk state.
    changed_ids = {paper_id} | set(opposite_writes)
    all_papers = list_papers(vault)
    all_papers = [p for p in all_papers if p.get("id") not in changed_ids]
    # ruamel YAML's CommentedMap is dict-compatible for our consumers.
    all_papers.append(dict(metadata))
    for opp_meta in opposite_writes.values():
        all_papers.append(dict(opp_meta))
    index_json = render_index(all_papers, _now_iso())

    rel_meta = f"papers/{paper_id}/metadata.yaml"
    with staged_write(vault, op_id=f"modify-{paper_id}") as stage:
        stage.write_text(rel_meta, metadata_yaml)
        for opposite_id, opp_meta in opposite_writes.items():
            stage.write_text(
                f"papers/{opposite_id}/metadata.yaml",
                _dump_yaml_to_string(opp_meta),
            )
        stage.write_text("INDEX.json", index_json)

    # Post-commit derived rebuild through the single shared funnel
    # (M30 Phase 4): INDEX + views are recomputed together so they can never
    # drift apart. The staged INDEX.json above is the crash-safety layer (it
    # matches metadata atomically even if this rebuild is interrupted); this
    # call re-derives the identical INDEX plus the views/ hubs, which are
    # filesystem-mutating but not text-file-atomic. project_refs=False keeps
    # behavior identical to the pre-funnel command (modify never rebuilt
    # project REFERENCES.md / symlinks).
    fresh_papers = list_papers(vault)
    reconcile_derived(vault, papers=fresh_papers, project_refs=False)

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
    for opposite_id in opposite_writes:
        console.print(
            f"  [dim]↔ paired reverse field written on[/] {escape(opposite_id)}"
        )
    console.print("[dim]INDEX.json + views/ refreshed.[/]")
    return True


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
    or omit it and pass --paper-doi <DOI> instead.

    Updates papers/<id>/metadata.yaml (with a refreshed updated-at
    audit timestamp) and INDEX.json atomically; views/by-*/ symlinks
    are rebuilt afterwards.
    """
    if not (set_ops or add_tag_ops or rm_tag_ops):
        raise ModifyError(
            "lit modify requires at least one of --set / --add-tag / --rm-tag. "
            "Run `lit modify --help` for examples."
        )

    vault = find_vault(resolve_library_or_vault(library, vault_name))
    paper_id = resolve_paper_input(vault, paper_id, paper_doi)
    _apply_modify(
        vault,
        paper_id,
        set_ops=set_ops,
        add_tag_ops=add_tag_ops,
        rm_tag_ops=rm_tag_ops,
    )
