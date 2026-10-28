"""Health-check engine for ``lit health-check`` (M2.8).

Each ``check_*`` function is a self-contained probe that returns a list of
:class:`Issue` records. The CLI command in ``commands/health.py`` calls
:func:`run_all_checks` to gather every issue and renders the result.

Design notes:

* Checks are pure functions of ``(vault, papers)`` plus whatever extra disk
  reads they need (TAXONOMY.md, ``.litman-staging/``, ``.trash/``).
  They never mutate the vault — auto-fix lives in :func:`apply_autofix`.
* Severity is one of ``error`` / ``warning`` / ``info``. Errors indicate
  active inconsistency (dangling refs, schema violations, half-finished
  rename). Warnings indicate hygiene concerns (inbox staleness, large trash).
* A single category is set on each issue so the CLI can group + auto-fix.
* Auto-fixable categories are listed in :data:`AUTO_FIXABLE_CATEGORIES`.
  ``--fix`` only operates on those — bidirectional sync, taxonomy drift,
  and schema gaps need user judgment and stay report-only.

Cross-references:

* M2.0 added ``created-at`` / ``updated-at``. M2.8 enforces them.
* M2.3 added ``.litman-staging/``. M2.8 surfaces leftovers.
* ``lit rename`` moves the paper's folder inside its staged_write, so a
  failed rename leaves the library as it was. A dir name that still differs
  from its metadata id (a library an older ``lit rename`` left halfway, or a
  hand edit) is caught by :func:`check_paper_dir_validity`,
  and ``--fix`` renames the folder when that is safe (``unfinished_rename``).
* M2.7 ``lit rm`` is also two-phase; orphan paper dirs (file commit
  succeeded but ``move_to_trash`` failed) surface via
  :func:`check_paper_dir_validity` (structural) and
  :func:`check_index_vs_disk` (the INDEX↔papers cheap diff).
* M2.7+ added ``.trash/`` with sidecar ``<entry>.meta.yaml``. M2.8 surfaces
  orphan sidecars (sidecar without entry dir) and trash bloat.
* invariant #12 bidirectional duality is enforced via
  :func:`check_code_clone_integrity`.

Layout: one package, split by what each check reads. Import from the package
itself; it re-exports every name callers use.

* ``_base`` — :class:`Issue`, :class:`CheckSpec` and its tag vocabularies,
  :data:`AUTO_FIXABLE_CATEGORIES`, report keys, thresholds.
* ``metadata`` — per-paper checks on metadata.yaml and the paper folder.
* ``links`` — relation fields, wikilinks, the views/ link farm.
* ``taxonomy`` — the topics / methods / data vocabulary.
* ``projects`` — lit-config.yaml and the project folders.
* ``maintenance`` — library-level state: vault registry, staging, PDF viewer,
  skill copies, trash, supplementary files, code clones.
* ``registry`` — the check registry, the runs, the push gate and ``--fix``.

The package re-binds the submodules' objects, so a monkeypatch applied here is
invisible to the checks: patch the submodule whose function reads the name.
"""

from __future__ import annotations

from litman.core.checks._base import (
    AUTO_FIXABLE_CATEGORIES,
    CORRECTIONS,
    DIRTY_NOTES_UNREADABLE,
    DIRTY_RENAMES,
    HEALTH_CHECK_STALE_DAYS,
    INBOX_STALE_DAYS,
    KLASSES,
    SYNC_STALE_DAYS,
    TIERS,
    TRASH_SIZE_WARN,
    UNFINISHED_RENAME_LEFT,
    CheckSpec,
    Issue,
)
from litman.core.checks.links import (
    check_bidirectional_refs,
    check_dangling_refs,
    check_dangling_wikilinks,
    check_views_vs_metadata,
)
from litman.core.checks.maintenance import (
    check_code_clone_integrity,
    check_pdf_viewer,
    check_skill_drift,
    check_stale_staging,
    check_supplementary,
    check_supplementary_cited,
    check_trash_health,
    check_vault_registry_drift,
)
from litman.core.checks.metadata import (
    _declared_id,
    check_dirty_metadata,
    check_discussion_scaffold,
    check_duplicate_doi,
    check_inbox_staleness,
    check_index_vs_disk,
    check_paper_dir_validity,
    check_placeholder_id,
    check_placeholder_metadata,
    check_schema,
    check_weak_id_keyword,
)
from litman.core.checks.projects import (
    check_config_readable,
    check_link_support,
    check_priority_orphan,
    check_project_bridge_dangling,
    check_project_config_consistency,
    check_project_path_exists,
    check_project_references,
    check_relevance_orphan,
)
from litman.core.checks.registry import (
    _CHECK_REGISTRY,
    apply_autofix,
    cheap_checks,
    finish_unfinished_renames,
    group_by_category,
    klass_a_checks,
    run_all_checks,
    run_push_integrity_errors,
)
from litman.core.checks.taxonomy import (
    SINGLETON_MIN,
    SINGLETON_PERCENT,
    check_taxonomy_drift,
    check_taxonomy_similar,
    check_taxonomy_singletons,
    check_taxonomy_unused,
)
from litman.core.id import clean_title_rename_target as _clean_title_rename_target
from litman.core.schema import (
    _FIXED_ENUM_VALUES,
    _OPTIONAL_FIXED_ENUMS,
    _REQUIRED_NONEMPTY_FIELDS,
    _RETIRED_FIELDS,
    _STATUS_ORDER,
    PROJECT_PRIORITY_PREFIX,
    PROJECT_PRIORITY_VALUES,
    all_fixed_enums,
    fixed_enum_allows_none,
    fixed_enum_values,
    retired_field_replacement,
)

__all__ = [
    "AUTO_FIXABLE_CATEGORIES",
    "CORRECTIONS",
    "DIRTY_NOTES_UNREADABLE",
    "DIRTY_RENAMES",
    "HEALTH_CHECK_STALE_DAYS",
    "INBOX_STALE_DAYS",
    "KLASSES",
    "PROJECT_PRIORITY_PREFIX",
    "PROJECT_PRIORITY_VALUES",
    "SINGLETON_MIN",
    "SINGLETON_PERCENT",
    "SYNC_STALE_DAYS",
    "TIERS",
    "TRASH_SIZE_WARN",
    "UNFINISHED_RENAME_LEFT",
    "_CHECK_REGISTRY",
    "_FIXED_ENUM_VALUES",
    "_OPTIONAL_FIXED_ENUMS",
    "_REQUIRED_NONEMPTY_FIELDS",
    "_RETIRED_FIELDS",
    "_STATUS_ORDER",
    "CheckSpec",
    "Issue",
    "_clean_title_rename_target",
    "_declared_id",
    "all_fixed_enums",
    "apply_autofix",
    "cheap_checks",
    "check_bidirectional_refs",
    "check_code_clone_integrity",
    "check_config_readable",
    "check_dangling_refs",
    "check_dangling_wikilinks",
    "check_dirty_metadata",
    "check_discussion_scaffold",
    "check_duplicate_doi",
    "check_inbox_staleness",
    "check_index_vs_disk",
    "check_link_support",
    "check_paper_dir_validity",
    "check_pdf_viewer",
    "check_placeholder_id",
    "check_placeholder_metadata",
    "check_priority_orphan",
    "check_project_bridge_dangling",
    "check_project_config_consistency",
    "check_project_path_exists",
    "check_project_references",
    "check_relevance_orphan",
    "check_schema",
    "check_skill_drift",
    "check_stale_staging",
    "check_supplementary",
    "check_supplementary_cited",
    "check_taxonomy_drift",
    "check_taxonomy_similar",
    "check_taxonomy_singletons",
    "check_taxonomy_unused",
    "check_trash_health",
    "check_vault_registry_drift",
    "check_views_vs_metadata",
    "check_weak_id_keyword",
    "finish_unfinished_renames",
    "fixed_enum_allows_none",
    "fixed_enum_values",
    "group_by_category",
    "klass_a_checks",
    "retired_field_replacement",
    "run_all_checks",
    "run_push_integrity_errors",
]
