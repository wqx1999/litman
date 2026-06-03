"""Tests for `litman.core.checks` schema validation.

Focused on M29's split of fixed-enum fields into required (``status``) and
optional (``priority`` / ``type``): None is a legitimate "not yet evaluated"
state for the optional ones, but still an error for ``status``; non-None
values must still be in the allowed whitelist for all three.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litman.core.checks import (
    check_config_readable,
    check_schema,
    check_taxonomy_drift,
    run_all_checks,
    run_push_integrity_errors,
)
from litman.core.document import list_papers
from litman.core.library import create_vault


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _minimal_paper(**overrides: object) -> dict[str, object]:
    """Return a schema-valid paper dict; pass overrides to mutate fields."""
    base: dict[str, object] = {
        "id": "2024_Test_Paper",
        "created-at": "2024-01-01T00:00:00+00:00",
        "updated-at": "2024-01-01T00:00:00+00:00",
        "type": "research",
        "status": "inbox",
        "priority": "B",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# M29: None is OK for the optional fixed enums
# ---------------------------------------------------------------------------


def test_schema_priority_none_ok(vault: Path) -> None:
    paper = _minimal_paper(priority=None)
    assert check_schema(vault, [paper]) == []


def test_schema_type_none_ok(vault: Path) -> None:
    paper = _minimal_paper(type=None)
    assert check_schema(vault, [paper]) == []


# ---------------------------------------------------------------------------
# M29: status remains required; non-None values still whitelist-checked
# ---------------------------------------------------------------------------


def test_schema_status_none_still_errors(vault: Path) -> None:
    paper = _minimal_paper(status=None)
    issues = check_schema(vault, [paper])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.category == "schema"
    assert "status" in issue.message
    assert "missing" in issue.message


def test_schema_priority_invalid_value_still_errors(vault: Path) -> None:
    paper = _minimal_paper(priority="X")
    issues = check_schema(vault, [paper])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.category == "schema"
    assert "'X'" in issue.message
    assert "['A', 'B', 'C']" in issue.message


def test_schema_type_invalid_value_still_errors(vault: Path) -> None:
    paper = _minimal_paper(type="position-paper")
    issues = check_schema(vault, [paper])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.category == "schema"
    assert "'position-paper'" in issue.message


# ---------------------------------------------------------------------------
# F9: timestamp format validation in check_schema (invariant #11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["created-at", "updated-at"])
def test_schema_malformed_datetime_errors(vault: Path, field: str) -> None:
    paper = _minimal_paper(**{field: "not-a-date"})
    issues = check_schema(vault, [paper])
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].category == "schema"
    assert field in issues[0].message
    assert "ISO 8601 datetime" in issues[0].message


@pytest.mark.parametrize("field", ["read-date", "last-revisited"])
@pytest.mark.parametrize("bad", ["20260530", "2026-W22-1", "2026-5-3", 20260530])
def test_schema_malformed_semantic_date_errors(
    vault: Path, field: str, bad: object
) -> None:
    paper = _minimal_paper(**{field: bad})
    issues = check_schema(vault, [paper])
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "YYYY-MM-DD" in issues[0].message


@pytest.mark.parametrize("field", ["read-date", "last-revisited"])
def test_schema_semantic_date_none_and_valid_ok(vault: Path, field: str) -> None:
    # None (not yet read/revisited) and a strict date both pass.
    assert check_schema(vault, [_minimal_paper(**{field: None})]) == []
    assert check_schema(vault, [_minimal_paper(**{field: "2026-05-30"})]) == []


# ---------------------------------------------------------------------------
# F6/F27: check_config_readable surfaces unparseable lit-config.yaml
# ---------------------------------------------------------------------------


def test_config_readable_clean_vault_ok(vault: Path) -> None:
    assert check_config_readable(vault, []) == []


def test_config_readable_corrupt_config_emits_error(vault: Path) -> None:
    (vault / "lit-config.yaml").write_text(": : not: valid: [", encoding="utf-8")
    issues = check_config_readable(vault, [])
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].category == "config_unreadable"
    assert "unreadable" in issues[0].message


def test_run_all_checks_reports_corrupt_config_once(vault: Path) -> None:
    # F6: a corrupt config must not let health-check report a clean vault, and
    # exactly one check (config_unreadable) owns the finding — the config-keyed
    # checks defer rather than each re-reporting.
    (vault / "lit-config.yaml").write_text("nope: [", encoding="utf-8")
    issues = run_all_checks(vault, [])
    unreadable = [i for i in issues if i.category == "config_unreadable"]
    assert len(unreadable) == 1
    assert unreadable[0].severity == "error"


# ---------------------------------------------------------------------------
# F7/F13: unreadable TAXONOMY.md is a finding, not a clean report
# ---------------------------------------------------------------------------


def test_taxonomy_drift_unreadable_file_emits_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TAXONOMY.md exists (is_file() True) but read raises OSError (permissions,
    # non-UTF-8, dropped mount). Must emit an error, mirroring the missing-file
    # branch — not swallow it and report clean (review F7/F13).
    real_read_text = Path.read_text

    def _raise_for_taxonomy(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "TAXONOMY.md":
            raise PermissionError("simulated unreadable TAXONOMY.md")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _raise_for_taxonomy)
    issues = check_taxonomy_drift(vault, [])
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].category == "taxonomy_drift"
    assert "unreadable" in issues[0].message


# ---------------------------------------------------------------------------
# §6.3: a bare-scalar list-field must not iterate per-character
#
# metadata.yaml is schema-less; `topics: foo` (no `- foo`) loads as the string
# "foo". The pre-fix code did `for value in p.get("topics") or []`, iterating
# the string's CHARACTERS — one phantom drift Issue per char. The shared
# `as_str_list` coercion wraps a scalar into a single-element list so the value
# surfaces once, as itself (no silent mangling, invariant #14).
# ---------------------------------------------------------------------------


def test_taxonomy_drift_scalar_field_not_iterated_per_char(vault: Path) -> None:
    paper = _minimal_paper(topics="zzz_unregistered_topic")
    issues = check_taxonomy_drift(vault, [paper])
    # Exactly one Issue for the whole value, not one per character.
    assert len(issues) == 1
    assert "zzz_unregistered_topic" in issues[0].message
    assert issues[0].category == "taxonomy_drift"


# ---------------------------------------------------------------------------
# C-ops1: run_push_integrity_errors exclusion contract
#
# These pin the load-bearing carve-outs so a future refactor that drops
# `if spec.klass == "A"` or `_PUSH_GATE_EXCLUDED_CATEGORIES` can't silently
# turn regen-fixable / external drift into a backup blocker (the 4 CLI tests
# in test_sync.py only exercise the validity path).
# ---------------------------------------------------------------------------


def _healthy_paper_on_disk(vault: Path, paper_id: str) -> Path:
    """Write a health-check-clean paper folder, return its path."""
    p = vault / "papers" / paper_id
    p.mkdir(parents=True, exist_ok=True)
    (p / "metadata.yaml").write_text(
        f"id: {paper_id}\n"
        "title: T\n"
        "year: 2024\n"
        "status: inbox\n"
        "created-at: '2024-01-01T00:00:00+00:00'\n"
        "updated-at: '2024-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    (p / "paper.pdf").write_bytes(b"%PDF-1.4\n%minimal\n")
    return p


def test_push_gate_excludes_klass_a_index_drift(vault: Path) -> None:
    """A vanished-id index_vs_disk error (klass A) must NOT block a push."""
    (vault / "INDEX.json").write_text(
        json.dumps({"papers": [{"id": "ghost_paper"}]}), encoding="utf-8"
    )
    papers: list[dict[str, object]] = []
    all_issues = run_all_checks(vault, papers)
    assert any(
        i.category == "index_vs_disk" and i.severity == "error" for i in all_issues
    ), "fixture must actually trigger a klass-A error"
    gate = run_push_integrity_errors(vault, papers)
    assert all(i.category != "index_vs_disk" for i in gate)
    assert gate == []  # no other corruption -> nothing blocks the backup


def test_push_gate_returns_validity_error(vault: Path) -> None:
    """A validity error (missing paper.pdf) MUST be returned by the gate."""
    p = vault / "papers" / "p1"
    p.mkdir(parents=True)
    (p / "metadata.yaml").write_text(
        "id: p1\ntitle: T\nyear: 2024\nstatus: inbox\n"
        "created-at: '2024-01-01T00:00:00+00:00'\n"
        "updated-at: '2024-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    # No paper.pdf -> paper_dir_validity error (klass validity, NOT excluded).
    gate = run_push_integrity_errors(vault, list_papers(vault))
    assert any(
        i.category == "paper_dir_validity" and i.severity == "error" for i in gate
    )


def test_push_gate_excludes_cross_vault_wikilink(vault: Path) -> None:
    """A cross-vault dangling-wikilink error must NOT block a push."""
    p = _healthy_paper_on_disk(vault, "p1")
    (p / "notes.md").write_text(
        "see [[no_such_sibling_vault_xyz:some_id]]\n", encoding="utf-8"
    )
    papers = list_papers(vault)
    all_issues = run_all_checks(vault, papers)
    assert any(
        i.category == "dangling_wikilinks" and i.severity == "error"
        for i in all_issues
    ), "fixture must trigger a cross-vault wikilink error"
    gate = run_push_integrity_errors(vault, papers)
    assert all(i.category != "dangling_wikilinks" for i in gate)


def test_push_gate_excludes_unreadable_registry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault_registry_drift error (registry unreadable) must NOT block."""
    from litman.core import vault_registry

    def _boom() -> None:
        raise vault_registry.VaultRegistryError("corrupt registry")

    monkeypatch.setattr(vault_registry, "load_registry", _boom)
    papers: list[dict[str, object]] = []
    all_issues = run_all_checks(vault, papers)
    assert any(
        i.category == "vault_registry_drift" and i.severity == "error"
        for i in all_issues
    ), "fixture must trigger a registry-unreadable error"
    gate = run_push_integrity_errors(vault, papers)
    assert all(i.category != "vault_registry_drift" for i in gate)
