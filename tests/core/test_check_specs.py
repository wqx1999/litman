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

# The checks present after M30 Phase 2. Phase 1 had 14; Phase 2 added
# ``vault_registry_drift`` (#4) — de-duped out of _drift.py + health.py into the
# single tagged core. Pinned so a dropped or accidentally-renamed check fails
# loudly (Phase 3 will ADD more; this list is updated deliberately).
_EXPECTED_CATEGORIES = (
    "schema",
    "paper_dir_validity",
    "index_vs_disk",
    "views_vs_metadata",
    "project_references",
    "dangling_refs",
    "dangling_wikilinks",
    "relevance_orphan",
    "taxonomy_drift",
    "project_config_consistency",
    "vault_registry_drift",
    "project_path_exists",
    "bidirectional_refs",
    "inbox_staleness",
    "stale_staging",
    "trash_health",
    "pdf_viewer",
    "code_clone_integrity",
)


def test_registry_has_all_checks() -> None:
    assert len(_CHECK_REGISTRY) == 18
    assert tuple(spec.category for spec in _CHECK_REGISTRY) == _EXPECTED_CATEGORIES


def test_every_spec_is_a_checkspec() -> None:
    assert all(isinstance(spec, CheckSpec) for spec in _CHECK_REGISTRY)


def test_every_spec_tags_are_in_valid_sets() -> None:
    for spec in _CHECK_REGISTRY:
        assert spec.tier in TIERS, (spec.category, spec.tier)
        assert spec.klass in KLASSES, (spec.category, spec.klass)
        assert spec.correction in CORRECTIONS, (spec.category, spec.correction)


def test_every_spec_fn_has_check_signature() -> None:
    """Each spec.fn is callable with the ``check_*(vault, papers)`` shape.

    The first two parameters are the positional ``(vault, papers)`` contract
    every spec must honor (``run_all_checks`` calls them that way). Any extra
    parameters MUST be keyword-only WITH a default, so the positional call site
    is unaffected (M30 Phase 5 added an optional ``exists_status`` keyword to the
    two bounded-stat cheap checks to thread the shared probe result).
    """
    for spec in _CHECK_REGISTRY:
        sig = inspect.signature(spec.fn)
        params = list(sig.parameters.values())
        assert len(params) >= 2, spec.category
        assert [p.name for p in params[:2]] == ["vault", "papers"], spec.category
        for extra in params[2:]:
            assert extra.kind == inspect.Parameter.KEYWORD_ONLY, spec.category
            assert extra.default is not inspect.Parameter.empty, spec.category


def test_auto_fixable_categories_unchanged() -> None:
    """Phase 1 must NOT broaden ``--fix`` (that is Phase 2)."""
    assert AUTO_FIXABLE_CATEGORIES == frozenset(
        {"stale_staging", "orphan_trash_sidecar"}
    )


def test_cheap_checks_returns_only_cheap_tier() -> None:
    cheap = cheap_checks()
    assert all(spec.tier == "cheap" for spec in cheap)


def test_cheap_set_is_the_two_b_ext_drifts_plus_index() -> None:
    """Invariant #15: Tier-1 reads only INDEX/registry/listing/bounded-stat.

    After Phase 3 the cheap set is the two B-external drifts the per-command
    hook resolves — ``vault_registry_drift`` (#4) + ``project_path_exists``
    (#5), both bounded-stat — plus ``index_vs_disk`` (#1, klass-A), which reads
    only the INDEX id set + the ``papers/`` directory listing. None of the three
    reads per-paper ``metadata.yaml``. Pinned so no ``full`` check is mistakenly
    promoted into the hot path.
    """
    assert {spec.category for spec in cheap_checks()} == {
        "vault_registry_drift",
        "project_path_exists",
        "index_vs_disk",
    }


def test_index_vs_disk_is_cheap_klass_a_regen() -> None:
    """Ledger #1: INDEX↔papers is a cheap / klass-A / regen check."""
    spec = next(s for s in _CHECK_REGISTRY if s.category == "index_vs_disk")
    assert (spec.tier, spec.klass, spec.correction) == ("cheap", "A", "regen")


def test_klass_a_set_is_the_three_derived_pairs() -> None:
    """klass-A = the derived↔truth pairs: INDEX, views, project refs."""
    assert {spec.category for spec in klass_a_checks()} == {
        "index_vs_disk",
        "views_vs_metadata",
        "project_references",
    }


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


# ---------------------------------------------------------------------------
# M30 Phase 2: vault_registry_drift de-duped into the tagged core
# ---------------------------------------------------------------------------


def _fake_home(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def test_vault_registry_drift_is_registered_cheap_b_ext_resolve() -> None:
    """Ledger #4: registry drift is a registered cheap / B-ext / resolve check."""
    spec = next(
        s for s in _CHECK_REGISTRY if s.category == "vault_registry_drift"
    )
    assert (spec.tier, spec.klass, spec.correction) == ("cheap", "B-ext", "resolve")


def test_vault_registry_drift_detects_dangling_entry(tmp_path, monkeypatch) -> None:
    """A definite-absent registered path is reported as a warning."""
    from litman.core.vault_registry import (
        VaultEntry,
        VaultRegistry,
        save_registry,
    )

    _fake_home(tmp_path, monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    ghost = tmp_path / "ghost"  # never created
    save_registry(
        VaultRegistry(
            vaults=[
                VaultEntry(name="real", path=str(real), is_active=True),
                VaultEntry(name="ghost", path=str(ghost), is_active=False),
            ]
        )
    )
    issues = checks.check_vault_registry_drift(tmp_path, [])
    assert len(issues) == 1
    assert issues[0].category == "vault_registry_drift"
    assert issues[0].severity == "warning"
    assert "ghost" in issues[0].message
    assert issues[0].hint == "lit vault remove ghost"


def test_vault_registry_drift_uses_bounded_stat_not_find_dangling(
    tmp_path, monkeypatch
) -> None:
    """§1.1 divergence gone: detection goes through the mount-safe bounded-stat.

    A None (slow / dropped mount) verdict must NOT be reported as drift —
    which is exactly the behavior a bare ``find_dangling`` stat cannot provide.
    We force the bounded-stat to return None for the dangling entry and assert
    nothing is flagged.
    """
    from litman.commands import _drift
    from litman.core.vault_registry import (
        VaultEntry,
        VaultRegistry,
        save_registry,
    )

    _fake_home(tmp_path, monkeypatch)
    ghost = tmp_path / "ghost"
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="ghost", path=str(ghost), is_active=False)]
        )
    )
    monkeypatch.setattr(
        _drift, "_exists_bounded", lambda paths, budget_s=0.5: {p: None for p in paths}
    )
    assert checks.check_vault_registry_drift(tmp_path, []) == []


def test_vault_registry_drift_corrupt_registry_emits_finding(
    tmp_path, monkeypatch
) -> None:
    """Phase 3 no-silent-skip (invariant #14): a corrupt registry is a finding.

    "I cannot read the registry" means drift detection is blind, which is
    itself reported (not swallowed). The Tier-1 hook surfaces the same case to
    stderr; both sides are consistent now.
    """
    from litman.core.vault_registry import registry_path

    _fake_home(tmp_path, monkeypatch)
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a mapping\n", encoding="utf-8")

    issues = checks.check_vault_registry_drift(tmp_path, [])
    assert len(issues) == 1
    assert issues[0].category == "vault_registry_drift"
    assert issues[0].severity == "error"
    assert "unreadable" in issues[0].message


def test_health_check_no_longer_has_bare_find_dangling_path() -> None:
    """health.py must not import / call the bare-stat ``find_dangling``.

    Phase 2 deletes ``_vault_registry_drift_issues``; the registry drift now
    comes from the unified bounded-stat check. Assert the hang-risk symbol is
    gone from the module namespace.
    """
    from litman.commands import health

    assert not hasattr(health, "_vault_registry_drift_issues")
    assert not hasattr(health, "find_dangling")
