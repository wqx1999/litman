"""Tests for the M30 tagged check registry (CheckSpec drift ledger).

Phase 1 is a pure structural refactor: every check now carries tier / klass /
correction metadata (invariant #14). These tests pin the ledger so a future
edit cannot silently drop a check, broaden ``--fix``, or mis-tag a Tier-1
candidate (invariant #15).
"""

from __future__ import annotations

import inspect
from pathlib import Path

from litman.core import checks
from litman.core.library import create_vault
from litman.core.checks import (
    AUTO_FIXABLE_CATEGORIES,
    CORRECTIONS,
    KLASSES,
    TIERS,
    CheckSpec,
    _CHECK_REGISTRY,
    cheap_checks,
    klass_a_checks,
)

# The 14 checks present at the start of M30 Phase 1. Pinned so a dropped or
# accidentally-renamed check fails loudly (Phase 3 will ADD more; this list is
# updated deliberately, not by accident).
_EXPECTED_CATEGORIES = (
    "schema",
    "id_consistency",
    "invalid_paper_dirs",
    "dangling_refs",
    "dangling_wikilinks",
    "taxonomy_drift",
    "project_config_consistency",
    "project_path_exists",
    "bidirectional_refs",
    "inbox_staleness",
    "stale_staging",
    "trash_health",
    "pdf_viewer",
    "code_clone_integrity",
)


def test_registry_has_all_fourteen_checks() -> None:
    assert len(_CHECK_REGISTRY) == 14
    assert tuple(spec.category for spec in _CHECK_REGISTRY) == _EXPECTED_CATEGORIES


def test_every_spec_is_a_checkspec() -> None:
    assert all(isinstance(spec, CheckSpec) for spec in _CHECK_REGISTRY)


def test_every_spec_tags_are_in_valid_sets() -> None:
    for spec in _CHECK_REGISTRY:
        assert spec.tier in TIERS, (spec.category, spec.tier)
        assert spec.klass in KLASSES, (spec.category, spec.klass)
        assert spec.correction in CORRECTIONS, (spec.category, spec.correction)


def test_every_spec_fn_has_check_signature() -> None:
    """Each spec.fn is callable with the ``check_*(vault, papers)`` shape."""
    for spec in _CHECK_REGISTRY:
        sig = inspect.signature(spec.fn)
        assert len(sig.parameters) == 2, spec.category


def test_auto_fixable_categories_unchanged() -> None:
    """Phase 1 must NOT broaden ``--fix`` (that is Phase 2)."""
    assert AUTO_FIXABLE_CATEGORIES == frozenset(
        {"stale_staging", "orphan_trash_sidecar"}
    )


def test_cheap_checks_returns_only_cheap_tier() -> None:
    cheap = cheap_checks()
    assert all(spec.tier == "cheap" for spec in cheap)


def test_cheap_set_is_exactly_project_path_exists() -> None:
    """Invariant #15: Tier-1 reads only INDEX/registry/listing/bounded-stat.

    In Phase 1 the only cheap check is ``project_path_exists`` (it stats the
    config project paths, no per-paper metadata). Phase 3 adds ``index_vs_disk``
    + the cheap paper-dir signal; until then this set is pinned so no ``full``
    check is mistakenly promoted into the per-command hot path.
    """
    assert {spec.category for spec in cheap_checks()} == {"project_path_exists"}


def test_project_path_exists_does_not_read_per_paper_metadata(
    tmp_path, monkeypatch
) -> None:
    """The only cheap check must not open any ``metadata.yaml`` (invariant #15).

    ``check_project_path_exists`` loads ``lit-config.yaml`` and stats project
    paths; it must never touch per-paper metadata. This test exercises the
    *real* working path: a configured project (so the stat loop runs) and a
    paper on disk (so there IS a ``metadata.yaml`` to accidentally read). The
    earlier version pointed at a nonexistent vault, where ``load_config``
    failed and the check early-returned ``[]`` BEFORE the stat loop — proving
    nothing about the hot path. We monkeypatch both ``Path.read_text`` and
    ``Path.open`` to fail loudly if any ``metadata.yaml`` is touched.
    """
    vault = create_vault(tmp_path)

    # One paper on disk: gives the check a metadata.yaml it could wrongly read.
    paper_dir = vault / "papers" / "2024_Foo_Bar"
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_Foo_Bar\ntitle: Foo\n", encoding="utf-8"
    )

    # One configured project whose path is a real directory: the stat loop
    # runs to completion (no early return, no warning), so the access-pattern
    # assertion is about the genuine working path.
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  p: {proj_dir}\n",
        encoding="utf-8",
    )

    real_read_text = Path.read_text
    real_open = Path.open

    def _guarded_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "metadata.yaml":
            raise AssertionError(
                f"cheap check read per-paper metadata (invariant #15): {self}"
            )
        return real_read_text(self, *args, **kwargs)

    def _guarded_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "metadata.yaml":
            raise AssertionError(
                f"cheap check opened per-paper metadata (invariant #15): {self}"
            )
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _guarded_read_text)
    monkeypatch.setattr(Path, "open", _guarded_open)

    # The stat loop runs (project path present + is a dir) → no warnings, and
    # no metadata.yaml was touched. Either guard raising would fail the test.
    issues = checks.check_project_path_exists(vault, [])
    assert issues == []


def test_klass_a_checks_returns_only_klass_a() -> None:
    """Accessor filters by klass A (none registered yet in Phase 1)."""
    # Vacuous (klass_a_checks() is empty) until Phase 3 registers a klass-A
    # check (e.g. index_vs_disk); kept now to pin the accessor's contract.
    assert all(spec.klass == "A" for spec in klass_a_checks())
