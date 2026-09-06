"""Cascade-rewrite helpers for controlled-vocabulary changes.

When a controlled value (a ``topics`` / ``methods`` / ``data`` tag, or a
``projects`` membership) is renamed, merged, or removed, every paper that
references it must have its ``metadata.yaml`` rewritten in lockstep with
TAXONOMY.md. These two helpers compute that cascade:

* :func:`_ripple_replacements` — apply ``old → new`` substitutions (rename
  one-to-one, merge many-to-one).
* :func:`_ripple_removals` — drop a value entirely (the deletion path).

A third, unrelated helper lives here for the same reason (it needs the same
round-trip YAML machinery and the same "rewrite many metadata.yaml in one
staged commit" shape): :func:`migrate_retired_priority`, the permanent
``lit health-check --fix`` migration off the retired paper-level ``priority``
field (ADR-025).

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

from ruamel.yaml import YAMLError

from litman.core.atomic import _make_op_id, staged_write
from litman.core.dates import now_iso
from litman.core.document import list_papers, load_yaml_or_raise, read_metadata
from litman.core.taxonomy import replace_value_in_field
from litman.core.views import render_index
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import TaxonomyError

_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    preserve_quotes=True,
    default_flow_style=False,
)


def _project_key_prefixes() -> tuple[str, ...]:
    """Prefixes of the per-project annotations that ride on ``projects``.

    ``relevance-<project>`` (authored prose) and ``priority-<project>`` (the
    A/B/C grade, ADR-025). Both are keyed by project name, so both must follow
    a ``lit project rename`` and vanish on a ``lit project rm`` — otherwise
    ``check_relevance_orphan`` / ``check_priority_orphan`` flag what the
    command itself stranded.

    Resolved lazily: ``core.checks`` imports THIS module back (the retired-
    field migration behind ``apply_autofix``), so a module-level import would
    close a load-time cycle.
    """
    from litman.core.checks import PROJECT_PRIORITY_PREFIX

    return ("relevance-", PROJECT_PRIORITY_PREFIX)


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _ripple_replacements(
    vault: Path,
    field: str,
    replacements: dict[str, str],
    *,
    rename_project_keys: bool = False,
    papers: list[dict[str, Any]] | None = None,
) -> tuple[int, list[tuple[str, str]], list[dict[str, Any]]]:
    """Apply ``replacements`` to ``field`` of every paper that references any source.

    ``rename_project_keys`` is set by ``lit project rename`` (M30 Phase 4 /
    verification task 2): when a project is renamed, BOTH paired annotations —
    ``relevance-<old>`` and ``priority-<old>`` (:func:`_project_key_prefixes`)
    — are carried over to the new name with their values preserved, so neither
    is left orphaned (which ``check_relevance_orphan`` / ``check_priority_orphan``
    would otherwise flag from the normal command path). These keys are
    project-specific, so this only applies when ``field == "projects"``;
    taxonomy callers (topics/methods/data) leave it ``False``. A paper whose
    membership did not change but which carries a stray ``<prefix><old>`` key
    (a hand-edit orphan) is still remapped here so a rename does not strand it.

    Returns:
        (n_changed, staged_writes, all_papers_with_changes_applied)

        * ``n_changed`` — count of paper metadata files that changed
        * ``staged_writes`` — ``[(relpath, new_yaml_text), ...]`` ready to
          hand to :func:`staged_write`
        * ``all_papers_with_changes_applied`` — full paper list with
          in-memory modifications, suitable for re-rendering INDEX.json

    ``papers`` lets a caller hand in an already-loaded list (INDEX
    projections via ``views.papers_for_index`` — the membership fields
    topics/methods/data/projects are all projected; task-write-perf).
    FORBIDDEN with ``rename_project_keys``: the stray-key probe reads
    ``relevance-<old>`` / ``priority-<old>`` off every paper, and the
    projection carries neither — that path must keep the full-metadata scan.
    """
    if papers is None:
        papers = list_papers(vault)
    elif rename_project_keys:
        raise ValueError(
            "papers= must not be combined with rename_project_keys: the stray "
            "relevance-/priority-key probe needs full metadata, not INDEX "
            "projections."
        )
    staged: list[tuple[str, str]] = []
    n_changed = 0
    sources = set(replacements.keys())
    now = now_iso()
    # Per-project keys to carry over: <prefix><old> → <prefix><new>.
    project_key_renames = (
        {
            f"{prefix}{old}": f"{prefix}{new}"
            for prefix in _project_key_prefixes()
            for old, new in replacements.items()
        }
        if rename_project_keys
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
        # per-project key that must be remapped (the latter handles a stray
        # <prefix><old> whose membership was already gone — never strand it).
        has_project_key = any(k in (paper or {}) for k in project_key_renames)
        if not (sources & set(values)) and not has_project_key:
            continue
        meta_path = vault / "papers" / str(paper_id) / "metadata.yaml"
        rt_metadata = load_yaml_or_raise(meta_path, _yaml)
        if rt_metadata is None:
            continue
        changed = replace_value_in_field(rt_metadata, field, replacements)
        for old_key, new_key in project_key_renames.items():
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
            # a schema-less paper may carry a stray <prefix><proj> key and
            # be touched via project_key_renames while never having a `projects`
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
    drop_project_keys: bool = False,
    papers: list[dict[str, Any]] | None = None,
) -> tuple[int, list[tuple[str, str]], list[dict[str, Any]]]:
    """Drop ``value`` from ``field`` of every paper that references it.

    The cascade-deletion counterpart of :func:`_ripple_replacements`.
    Kept as a dedicated helper (not ``_ripple_replacements`` with an
    empty-string target) so ``replace_value_in_field`` keeps clean
    replacement-only semantics — a removal is a structurally different
    operation (the value disappears, it is not substituted).

    ``drop_project_keys`` is set by ``lit project rm`` (M30 Phase 4 /
    verification task 2): when a project is removed, BOTH paired annotations —
    ``relevance-<value>`` and ``priority-<value>``
    (:func:`_project_key_prefixes`) — are dropped from each cascaded paper
    alongside the ``projects`` membership, so neither is left orphaned (which
    ``check_relevance_orphan`` / ``check_priority_orphan`` would otherwise flag
    from the normal command path). These keys are project-specific, so this is
    only meaningful when ``field == "projects"``; taxonomy callers
    (topics/methods/data) leave it ``False``.

    Returns the same shape as :func:`_ripple_replacements`:
        (n_changed, staged_writes, all_papers_with_changes_applied)

    ``papers`` mirrors :func:`_ripple_replacements`: an already-loaded list
    (INDEX projections suffice for membership), FORBIDDEN with
    ``drop_project_keys`` for the same stray-key-probe reason.
    """
    if papers is None:
        papers = list_papers(vault)
    elif drop_project_keys:
        raise ValueError(
            "papers= must not be combined with drop_project_keys: the stray "
            "relevance-/priority-key probe needs full metadata, not INDEX "
            "projections."
        )
    staged: list[tuple[str, str]] = []
    n_changed = 0
    now = now_iso()
    project_keys = tuple(
        f"{prefix}{value}" for prefix in _project_key_prefixes()
    )

    for paper in papers:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        values = paper.get(field) or []
        is_member = value in values
        # A paper is touched if it is a member OR (only on the project-rm path)
        # it carries a stray ``<prefix><value>`` whose membership was already
        # gone (a hand-edit orphan). The second clause mirrors
        # _ripple_replacements' has_project_key guard so ``lit project rm X``
        # is symmetric with ``lit project rename``: after rm, neither
        # ``relevance-<X>`` nor ``priority-<X>`` survives anywhere (W1).
        has_stray_project_key = drop_project_keys and any(
            k in (paper or {}) for k in project_keys
        )
        if not is_member and not has_stray_project_key:
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
        if drop_project_keys:
            for key in project_keys:
                if key in rt_metadata:
                    del rt_metadata[key]
                    paper.pop(key, None)
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
        # get-or-[] (mirroring :738): a stray <prefix><proj> drop touches a
        # paper that may have no `projects` key, so a direct subscript would
        # KeyError.
        paper[field] = list(rt_metadata.get(field) or [])
        paper["updated-at"] = now
        n_changed += 1

    return n_changed, staged, papers


def migrate_retired_priority(vault: Path) -> int:
    """Move every paper-level ``priority`` onto its per-project keys. Returns n.

    The permanent ``lit health-check --fix`` migration behind the
    ``retired_priority`` finding (``core/checks.py``, ADR-025). Per paper
    holding the retired key:

    * a graded paper copies its letter onto ``priority-<project>`` for every
      project it is linked to — via ``setdefault``, so a grade the user (or an
      earlier run) already set for that project is NEVER overwritten;
    * ``priority: null`` (what ``lit add`` used to write for an unread paper)
      copies nothing: the migration drops the key instead of inventing a
      ``priority-<project>: null`` for a paper nobody has graded;
    * a paper in no project keeps nothing — there is no project for the grade
      to be "about" (ADR-025 decision 6, and the reason this is the one lossy
      ``--fix`` arm; ``check_schema`` gives those papers a message that says
      so before ``--fix`` is typed).

    Then the retired key is deleted. Idempotent by construction: the second run
    finds no ``priority`` key, stages nothing, and returns 0.

    **Keyed by FOLDER, never by the declared ``id``.** ``id`` is an ordinary
    metadata field: nothing enforces that it is unique or that it matches its
    directory, and the vault is an rclone target (invariant #9) where a sync
    conflict can leave two folders declaring one id. Resolving the path
    through ``id`` wrote one folder twice, left the other still holding the
    retired key, over-reported the count, and made the *second* ``--fix``
    raise ``KeyError`` — which is not a ``LitmanError``, so it escaped the CLI
    handler as a traceback. The returned count is therefore a count of FILES
    rewritten.

    ``updated-at`` is deliberately NOT bumped. A migration is not a user edit —
    the audit stamp answers "when did I last change this paper", and moving a
    value the tool itself retired is not an answer to that. It is also what
    makes the before/after files comparable line by line. This is a property of
    this function, not an option on the shared writers: the other 26 assignment
    sites bump unconditionally and must keep doing so.

    All the rewritten ``metadata.yaml`` files plus ``INDEX.json`` go into ONE
    :func:`staged_write` (mirroring ``project_link.remove_project``), so a crash
    mid-migration leaves the vault either wholly migrated or wholly untouched —
    and TRUTH files are chmod read-only on Windows, which only the staged path
    handles. The post-commit ``reconcile_derived`` re-reads FULL metadata from
    disk: ``retired_priority`` rides the validity-fix path, and
    ``commands/health.py`` only regens for klass-A findings, so this call is the
    migration's own reconciliation rather than a duplicate of one.

    Papers this cannot migrate are left exactly as they are, never guessed at:
    an unreadable metadata.yaml (owned by ``check_paper_dir_validity``) and a
    non-list ``projects`` (a hand-edit; metadata is schema-less by invariant
    #7). Neither is a silent skip in the invariant-#14 sense — the paper keeps
    the retired key, so ``check_schema`` reports it again on the re-run
    ``health-check`` does right after ``--fix``.
    """
    # Local imports: core.checks imports THIS module back (apply_autofix's
    # retired_priority arm) and core.correctors imports core.checks, so
    # keeping both lazy means neither direction is a module-load edge.
    # Mirrors the lazy reconcile import in core/project_link.py.
    from litman.core.checks import PROJECT_PRIORITY_PREFIX
    from litman.core.correctors import reconcile_derived

    papers_dir = vault / "papers"
    if not papers_dir.is_dir():
        return 0

    # Enumerated here rather than through list_papers because the folder name
    # is the identity and that function does not report it. The tolerance
    # below mirrors it exactly, for the reason recorded there.
    index_papers: list[dict[str, Any]] = []
    staged: list[tuple[str, str]] = []
    n_changed = 0

    for paper_dir in sorted(papers_dir.iterdir()):
        if not paper_dir.is_dir():
            continue
        meta_path = paper_dir / "metadata.yaml"
        if not meta_path.is_file():
            continue
        try:
            paper = read_metadata(meta_path)
        except (OSError, YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(paper, dict) or not paper:
            continue
        index_papers.append(paper)

        if "priority" not in paper:
            continue
        rt_metadata = load_yaml_or_raise(meta_path, _yaml)
        if not isinstance(rt_metadata, dict) or "priority" not in rt_metadata:
            continue
        projects = rt_metadata.get("projects")
        if projects is not None and not isinstance(projects, list):
            # `projects: pepcodec` (a hand-edit) would iterate CHARACTER by
            # character into priority-p, priority-e, ... and no check could
            # catch it afterwards: check_priority_orphan matches each letter
            # against set("pepcodec"), which contains them all. Refuse the
            # paper, mirroring _ripple_removals' scalar guard.
            continue

        grade = rt_metadata.get("priority")
        if grade is not None:
            for project in projects or []:
                key = f"{PROJECT_PRIORITY_PREFIX}{project}"
                rt_metadata.setdefault(key, grade)
                paper[key] = rt_metadata[key]
        rt_metadata.pop("priority", None)
        paper.pop("priority", None)
        staged.append(
            (
                f"papers/{paper_dir.name}/metadata.yaml",
                _dump_yaml_to_string(rt_metadata),
            )
        )
        n_changed += 1

    if not n_changed:
        return 0

    fresh_index = render_index(index_papers, now_iso())
    # A generated op id, not a fixed one: a staging dir that recovery could not
    # resolve is preserved as evidence, and a fixed name would then make every
    # later --fix die on FileExistsError.
    with staged_write(
        vault, op_id=_make_op_id("migrate-retired-priority")
    ) as stage:
        for relpath, content in staged:
            stage.write_text(relpath, content)
        stage.write_text("INDEX.json", fresh_index)

    # papers=None on purpose: REFERENCES.md renders relevance-<project> (and
    # groups by the grade), which an INDEX projection does not carry — handing
    # one in would silently rewrite every project's reference list without it.
    reconcile_derived(vault, project_refs=True)
    return n_changed
