"""Cascade-rewrite helpers for controlled-vocabulary changes.

When a controlled value (a ``topics`` / ``methods`` / ``data`` tag, or a
``projects`` membership) is renamed, merged, or removed, every paper that
references it must have its ``metadata.yaml`` rewritten in lockstep with
TAXONOMY.md. These two helpers compute that cascade:

* :func:`_ripple_replacements` — apply ``old → new`` substitutions (rename
  one-to-one, merge many-to-one).
* :func:`_ripple_removals` — drop a value entirely (the deletion path).

Both return ``(n_changed, staged_writes, all_papers_with_changes_applied)``
so the caller can hand the staged writes to :func:`staged_write` and
re-render INDEX.json from the in-memory paper list without a re-read.

Extracted from ``commands/taxonomy.py`` (M-task web-gui P2): the project-rm
core in ``core/project_link.py`` and the taxonomy-rm core in
``core/taxonomy.py`` both need these, and a core importing from a command
module is a back-dependency (``core → commands``) that the GUI write path
(invariant #16) cannot carry. Keeping the YAML roundtrip helpers here too
makes this module self-contained. ``commands/taxonomy.py`` and
``commands/project.py`` re-import these names so CLI behavior is byte-identical.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from litman.core.dates import now_iso
from litman.core.document import list_papers, load_yaml_or_raise
from litman.core.taxonomy import replace_value_in_field
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import TaxonomyError

_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    preserve_quotes=True,
    default_flow_style=False,
)


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _ripple_replacements(
    vault: Path,
    field: str,
    replacements: dict[str, str],
    *,
    rename_relevance: bool = False,
) -> tuple[int, list[tuple[str, str]], list[dict[str, Any]]]:
    """Apply ``replacements`` to ``field`` of every paper that references any source.

    ``rename_relevance`` is set by ``lit project rename`` (M30 Phase 4 /
    verification task 2): when a project is renamed, the paired
    ``relevance-<old>`` annotation must be carried over to ``relevance-<new>``
    (value preserved) so it is not left orphaned (which ``check_relevance_orphan``
    would otherwise flag from the normal command path). The relevance key is
    project-specific, so this only applies when ``field == "projects"``;
    taxonomy callers (topics/methods/data) leave it ``False``. A paper whose
    membership did not change but which carries a stray ``relevance-<old>`` key
    (a hand-edit orphan) is still remapped here so a rename does not strand it.

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
    now = now_iso()
    # relevance keys to carry over: relevance-<old> → relevance-<new>.
    relevance_renames = (
        {f"relevance-{old}": f"relevance-{new}" for old, new in replacements.items()}
        if rename_relevance
        else {}
    )

    # Re-load each touched metadata.yaml in roundtrip mode so we can dump
    # it back preserving formatting. The paper list returned by
    # `list_papers` uses the safe loader and is fine for INDEX rendering,
    # but writing requires the roundtrip representation.
    for paper in papers:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        values = paper.get(field) or []
        # A paper is touched if its `field` references a source OR it carries a
        # relevance key that must be remapped (the latter handles a stray
        # relevance-<old> whose membership was already gone — never strand it).
        has_relevance_key = any(k in (paper or {}) for k in relevance_renames)
        if not (sources & set(values)) and not has_relevance_key:
            continue
        meta_path = vault / "papers" / str(paper_id) / "metadata.yaml"
        rt_metadata = load_yaml_or_raise(meta_path, _yaml)
        if rt_metadata is None:
            continue
        changed = replace_value_in_field(rt_metadata, field, replacements)
        for old_key, new_key in relevance_renames.items():
            if old_key in rt_metadata:
                # Preserve the value; insert the new key, drop the old one.
                rt_metadata[new_key] = rt_metadata[old_key]
                del rt_metadata[old_key]
                paper[new_key] = paper.pop(old_key, rt_metadata[new_key])
                changed = True
        if changed:
            rt_metadata["updated-at"] = now
            staged.append(
                (
                    f"papers/{paper_id}/metadata.yaml",
                    _dump_yaml_to_string(rt_metadata),
                )
            )
            # Also mutate the safe-loaded copy so the INDEX render reflects
            # the change without a re-read. Use get-or-[] (mirroring :738):
            # a schema-less paper may carry a stray relevance-<proj> key and
            # be touched via relevance_renames while never having a `projects`
            # key, so a direct subscript would KeyError.
            paper[field] = list(rt_metadata.get(field) or [])
            paper["updated-at"] = now
            n_changed += 1

    return n_changed, staged, papers


def _ripple_removals(
    vault: Path,
    field: str,
    value: str,
    *,
    drop_relevance: bool = False,
) -> tuple[int, list[tuple[str, str]], list[dict[str, Any]]]:
    """Drop ``value`` from ``field`` of every paper that references it.

    The cascade-deletion counterpart of :func:`_ripple_replacements`.
    Kept as a dedicated helper (not ``_ripple_replacements`` with an
    empty-string target) so ``replace_value_in_field`` keeps clean
    replacement-only semantics — a removal is a structurally different
    operation (the value disappears, it is not substituted).

    ``drop_relevance`` is set by ``lit project rm`` (M30 Phase 4 / verification
    task 2): when a project is removed, the paired ``relevance-<value>``
    annotation on each cascaded paper is dropped alongside the ``projects``
    membership, so the field is not left orphaned (which ``check_relevance_orphan``
    would otherwise flag from the normal command path). The relevance key is
    project-specific, so this is only meaningful when ``field == "projects"``;
    taxonomy callers (topics/methods/data) leave it ``False``.

    Returns the same shape as :func:`_ripple_replacements`:
        (n_changed, staged_writes, all_papers_with_changes_applied)
    """
    papers = list_papers(vault)
    staged: list[tuple[str, str]] = []
    n_changed = 0
    now = now_iso()
    relevance_key = f"relevance-{value}"

    for paper in papers:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        values = paper.get(field) or []
        is_member = value in values
        # A paper is touched if it is a member OR (only on the project-rm path)
        # it carries a stray ``relevance-<value>`` whose membership was already
        # gone (a hand-edit orphan). The second clause mirrors
        # _ripple_replacements' has_relevance_key guard so ``lit project rm X``
        # is symmetric with ``lit project rename``: after rm, no
        # ``relevance-<X>`` survives anywhere (W1).
        has_stray_relevance = drop_relevance and relevance_key in (paper or {})
        if not is_member and not has_stray_relevance:
            continue
        meta_path = vault / "papers" / str(paper_id) / "metadata.yaml"
        rt_metadata = load_yaml_or_raise(meta_path, _yaml)
        if rt_metadata is None:
            continue
        changed = False
        current = rt_metadata.get(field) or []
        if not isinstance(current, list):
            raise TaxonomyError(
                f"papers/{paper_id}/metadata.yaml field {field!r} is "
                f"{type(current).__name__}, not a list — refusing to ripple "
                "(a scalar value would be corrupted character-by-character). "
                "Fix the field by hand or via `lit modify`."
            )
        if value in current:
            rt_metadata[field] = [v for v in current if v != value]
            changed = True
        if drop_relevance and relevance_key in rt_metadata:
            del rt_metadata[relevance_key]
            paper.pop(relevance_key, None)
            changed = True
        if not changed:
            continue
        rt_metadata["updated-at"] = now
        staged.append(
            (
                f"papers/{paper_id}/metadata.yaml",
                _dump_yaml_to_string(rt_metadata),
            )
        )
        # get-or-[] (mirroring :738): a stray relevance-<proj> drop touches a
        # paper that may have no `projects` key, so a direct subscript would
        # KeyError.
        paper[field] = list(rt_metadata.get(field) or [])
        paper["updated-at"] = now
        n_changed += 1

    return n_changed, staged, papers
