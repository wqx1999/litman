"""Tests for ``lit health-check`` (M2.8).

Per-check unit tests exercise the pure functions in ``litman.core.checks``.
CLI tests exercise the command via Click ``CliRunner``: rendering, exit
code, and ``--fix`` round-trip.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import viewer as viewer_mod
from litman.core.checks import (
    AUTO_FIXABLE_CATEGORIES,
    INBOX_STALE_DAYS,
    apply_autofix,
    check_bidirectional_refs,
    check_code_clone_integrity,
    check_dangling_refs,
    check_dangling_wikilinks,
    check_id_consistency,
    check_inbox_staleness,
    check_invalid_paper_dirs,
    check_pdf_viewer,
    check_schema,
    check_stale_staging,
    check_taxonomy_drift,
    check_trash_health,
    run_all_checks,
)
from litman.core.document import list_papers
from litman.core.library import create_vault

_yaml = YAML(typ="safe")
_yaml_dump = YAML()
_yaml_dump.indent(mapping=2, sequence=4, offset=2)
_yaml_dump.default_flow_style = False


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    """Write a complete metadata.yaml + notes.md skeleton.

    Override individual fields via kwargs. Pass ``override_id=<str>`` to
    write a different value into the ``id`` field than the directory name
    (used for id_consistency tests).
    """
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": fields.get("override_id", paper_id),
        "title": fields.get("title", paper_id),
        "authors": fields.get("authors", ["Doe, Jane"]),
        "year": fields.get("year", 2024),
        "journal": fields.get("journal", "Test J."),
        "doi": fields.get("doi", f"10.0/{paper_id}"),
        "arxiv-id": None,
        "github": None,
        "created-at": fields.get("created_at", "2026-04-28T10:00:00+02:00"),
        "updated-at": fields.get("updated_at", "2026-04-28T10:00:00+02:00"),
        "projects": fields.get("projects", []),
        "topics": fields.get("topics", []),
        "methods": fields.get("methods", []),
        "data": fields.get("data", []),
        "type": fields.get("type", "research"),
        "status": fields.get("status", "deep-read"),
        "priority": fields.get("priority", "B"),
        "read-date": None,
        "last-revisited": None,
        "related": fields.get("related", []),
        "contradicts": fields.get("contradicts", []),
        "extends": fields.get("extends", []),
        "code-clones": fields.get("code_clones", []),
    }
    if "drop_fields" in fields:
        for k in fields["drop_fields"]:
            payload.pop(k, None)
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml_dump.dump(payload, f)
    notes = fields.get("notes")
    if notes is not None:
        (paper_dir / "notes.md").write_text(notes, encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ===========================================================================
# Per-check unit tests
# ===========================================================================


# --- schema ------------------------------------------------------------------


def test_schema_clean_vault(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    assert check_schema(vault, list_papers(vault)) == []


def test_schema_missing_created_at(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", drop_fields={"created-at"})
    issues = check_schema(vault, list_papers(vault))
    assert any("created-at" in i.message for i in issues)
    assert all(i.severity == "error" for i in issues)


def test_schema_invalid_status_value(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", status="reading")  # not in enum
    issues = check_schema(vault, list_papers(vault))
    assert any(
        i.category == "schema" and "'status'" in i.message and "'reading'" in i.message
        for i in issues
    )


def test_schema_invalid_priority(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", priority="X")
    issues = check_schema(vault, list_papers(vault))
    assert any(i.category == "schema" and "'priority'" in i.message for i in issues)


# --- id_consistency ---------------------------------------------------------


def test_id_consistency_clean(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    assert check_id_consistency(vault, list_papers(vault)) == []


def test_id_consistency_dir_vs_metadata_mismatch(vault: Path) -> None:
    # Directory is "2024_Foo_Bar" but metadata.id is "2024_Foo_Baz" — looks
    # like a half-finished rename: file content updated, dir not yet renamed.
    _write_paper(vault, "2024_Foo_Bar", override_id="2024_Foo_Baz")
    issues = check_id_consistency(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "id_consistency"
    assert issues[0].severity == "error"
    assert "2024_Foo_Bar" in issues[0].message
    assert "2024_Foo_Baz" in issues[0].message


# --- invalid_paper_dirs -----------------------------------------------------


def test_invalid_dir_no_metadata(vault: Path) -> None:
    (vault / "papers" / "2024_Foo_Bar").mkdir(parents=True)
    issues = check_invalid_paper_dirs(vault, [])
    assert any(
        i.category == "invalid_paper_dirs" and "no metadata.yaml" in i.message
        for i in issues
    )


def test_invalid_dir_bad_id_name(vault: Path) -> None:
    # Spaces are not allowed in paper ids — surfaces as invalid.
    (vault / "papers" / "Bad Dir Name").mkdir(parents=True, exist_ok=True)
    issues = check_invalid_paper_dirs(vault, [])
    assert any(
        i.category == "invalid_paper_dirs" and "valid paper id" in i.message
        for i in issues
    )


def test_invalid_dir_non_directory_file(vault: Path) -> None:
    (vault / "papers" / "stray.txt").write_text("oops")
    issues = check_invalid_paper_dirs(vault, [])
    assert any(
        i.category == "invalid_paper_dirs" and "non-directory" in i.message
        for i in issues
    )


# --- dangling_refs ----------------------------------------------------------


def test_dangling_refs_clean(vault: Path) -> None:
    _write_paper(vault, "A_a_a", related=["B_b_b"])
    _write_paper(vault, "B_b_b", related=["A_a_a"])
    issues = check_dangling_refs(vault, list_papers(vault))
    assert issues == []


def test_dangling_refs_detects_missing_target(vault: Path) -> None:
    _write_paper(vault, "A_a_a", related=["B_b_b"])
    issues = check_dangling_refs(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "dangling_refs"
    assert issues[0].paper_id == "A_a_a"
    assert "B_b_b" in issues[0].message


def test_dangling_refs_across_all_three_fields(vault: Path) -> None:
    _write_paper(
        vault,
        "A_a_a",
        related=["X1"],
        contradicts=["X2"],
        extends=["X3"],
    )
    issues = check_dangling_refs(vault, list_papers(vault))
    cats = {i.message.split()[0] for i in issues}
    assert cats == {"'related'", "'contradicts'", "'extends'"}


# --- bidirectional_refs -----------------------------------------------------


def test_bidirectional_clean(vault: Path) -> None:
    _write_paper(vault, "A_a_a", related=["B_b_b"])
    _write_paper(vault, "B_b_b", related=["A_a_a"])
    assert check_bidirectional_refs(vault, list_papers(vault)) == []


def test_bidirectional_one_sided(vault: Path) -> None:
    _write_paper(vault, "A_a_a", related=["B_b_b"])
    _write_paper(vault, "B_b_b")
    issues = check_bidirectional_refs(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "bidirectional_refs"
    assert "A_a_a" in issues[0].message
    assert "B_b_b" in issues[0].message


def test_bidirectional_skips_dangling(vault: Path) -> None:
    # Reference to nonexistent paper should NOT trigger bidirectional warning
    # — that's reported by check_dangling_refs instead.
    _write_paper(vault, "A_a_a", related=["GHOST_x_y"])
    issues = check_bidirectional_refs(vault, list_papers(vault))
    assert issues == []


def test_bidirectional_ignores_extends_and_contradicts(vault: Path) -> None:
    _write_paper(vault, "A_a_a", extends=["B_b_b"], contradicts=["B_b_b"])
    _write_paper(vault, "B_b_b")
    issues = check_bidirectional_refs(vault, list_papers(vault))
    assert issues == []  # only `related` is symmetric


# --- dangling_wikilinks -----------------------------------------------------


def test_dangling_wikilinks_clean(vault: Path) -> None:
    _write_paper(vault, "A_a_a", notes="See [[A_a_a]] for context.")
    assert check_dangling_wikilinks(vault, list_papers(vault)) == []


def test_dangling_wikilinks_in_paper_notes(vault: Path) -> None:
    _write_paper(vault, "A_a_a", notes="Related: [[GHOST_x_y]].")
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1
    assert "[[GHOST_x_y]]" in issues[0].message


def test_dangling_wikilinks_in_cross_paper_notes(vault: Path) -> None:
    _write_paper(vault, "A_a_a")
    methods_dir = vault / "notes" / "methods"
    (methods_dir / "deep-learning.md").write_text(
        "Survey: [[A_a_a]] and [[B_missing]].\n", encoding="utf-8"
    )
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1
    assert "B_missing" in issues[0].message


def test_dangling_wikilinks_dedupes_per_file(vault: Path) -> None:
    """A single file mentioning the same dangling id N times reports only once."""
    _write_paper(vault, "A_a_a")
    (vault / "notes" / "ideas" / "x.md").write_text(
        "[[ZOMBIE]] [[ZOMBIE]] [[ZOMBIE]]\n", encoding="utf-8"
    )
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1


# --- taxonomy_drift ---------------------------------------------------------


def test_taxonomy_drift_clean(vault: Path) -> None:
    # Register topic first.
    tax = vault / "TAXONOMY.md"
    text = tax.read_text()
    tax.write_text(text.replace("## topics\n\n(empty)", "## topics\n\n- AMP"))
    _write_paper(vault, "A_a_a", topics=["AMP"])
    assert check_taxonomy_drift(vault, list_papers(vault)) == []


def test_taxonomy_drift_unregistered_value(vault: Path) -> None:
    _write_paper(vault, "A_a_a", topics=["unregistered-topic"])
    issues = check_taxonomy_drift(vault, list_papers(vault))
    assert len(issues) == 1
    assert "unregistered-topic" in issues[0].message
    assert issues[0].severity == "warning"


def test_taxonomy_drift_missing_taxonomy_file(vault: Path) -> None:
    (vault / "TAXONOMY.md").unlink()
    issues = check_taxonomy_drift(vault, [])
    assert len(issues) == 1
    assert "TAXONOMY.md" in issues[0].message


# --- inbox_staleness --------------------------------------------------------


def _iso_days_ago(n: int) -> str:
    """ISO 8601 timestamp `n` days ago (UTC)."""
    return (
        datetime.now(timezone.utc) - timedelta(days=n)
    ).isoformat(timespec="seconds")


def test_inbox_staleness_recent_inbox_is_clean(vault: Path) -> None:
    _write_paper(
        vault,
        "A_a_a",
        status="inbox",
        created_at=_iso_days_ago(1),
    )
    assert check_inbox_staleness(vault, list_papers(vault)) == []


def test_inbox_staleness_old_inbox_flagged(vault: Path) -> None:
    _write_paper(
        vault,
        "A_a_a",
        status="inbox",
        created_at=_iso_days_ago(INBOX_STALE_DAYS + 5),
    )
    issues = check_inbox_staleness(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].paper_id == "A_a_a"


def test_inbox_staleness_skips_non_inbox(vault: Path) -> None:
    _write_paper(
        vault,
        "A_a_a",
        status="deep-read",
        created_at=_iso_days_ago(100),
    )
    assert check_inbox_staleness(vault, list_papers(vault)) == []


# --- stale_staging ----------------------------------------------------------


def test_stale_staging_clean(vault: Path) -> None:
    assert check_stale_staging(vault, []) == []


def test_stale_staging_finds_leftover(vault: Path) -> None:
    (vault / ".litman-staging" / "op-crashed").mkdir()
    issues = check_stale_staging(vault, [])
    assert len(issues) == 1
    assert "op-crashed" in issues[0].message


# --- trash_health -----------------------------------------------------------


def test_trash_health_clean_with_no_trash(vault: Path) -> None:
    assert check_trash_health(vault, []) == []


def test_trash_health_orphan_sidecar(vault: Path) -> None:
    trash = vault / ".trash"
    trash.mkdir()
    (trash / "2024_Ghost-20260101T000000Z.meta.yaml").write_text(
        "paper_id: 2024_Ghost\n", encoding="utf-8"
    )
    issues = check_trash_health(vault, [])
    assert any(i.category == "orphan_trash_sidecar" for i in issues)


# --- pdf_viewer -------------------------------------------------------------


def test_pdf_viewer_clean_when_platform_default_available(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default seed has default_pdf_viewer=null; on macOS `open` is always there."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    assert check_pdf_viewer(vault, []) == []


def test_pdf_viewer_warns_when_no_platform_default(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(viewer_mod.shutil, "which", lambda cmd: None)
    issues = check_pdf_viewer(vault, [])
    assert len(issues) == 1
    assert issues[0].category == "pdf_viewer"
    assert issues[0].severity == "warning"
    assert "no platform PDF viewer" in issues[0].message


def test_pdf_viewer_warns_when_configured_missing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "default_pdf_viewer: null",
            "default_pdf_viewer: nonexistent-viewer-xyz",
        ),
        encoding="utf-8",
    )
    # Plus an unrelated `shutil.which` patch so the configured one is "missing".
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda cmd: None)
    issues = check_pdf_viewer(vault, [])
    assert len(issues) == 1
    assert "nonexistent-viewer-xyz" in issues[0].message


def test_pdf_viewer_clean_when_configured_present(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "default_pdf_viewer: null",
            "default_pdf_viewer: somecmd",
        ),
        encoding="utf-8",
    )
    import shutil as _shutil

    monkeypatch.setattr(
        _shutil, "which", lambda cmd: f"/usr/bin/{cmd}"
    )
    assert check_pdf_viewer(vault, []) == []


# --- code_clone_integrity ---------------------------------------------------


def _write_repo_meta(vault: Path, repo_name: str, papers: list[str]) -> None:
    """Lay down a minimal ``codes/<repo_name>/repo-meta.yaml``.

    The integrity check only inspects file existence — schema validation
    is out of scope — so the payload need only be a parseable mapping.
    """
    repo_dir = vault / "codes" / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    with (repo_dir / "repo-meta.yaml").open("w", encoding="utf-8") as f:
        _yaml_dump.dump({"name": repo_name, "papers": list(papers)}, f)


def test_code_clone_integrity_clean_no_codes_dir(vault: Path) -> None:
    # ``create_vault`` always lays down ``codes/``; remove it to verify the
    # defensive early-return path when the directory is genuinely absent.
    (vault / "codes").rmdir()
    _write_paper(vault, "2024_Foo_Bar")
    assert check_code_clone_integrity(vault, list_papers(vault)) == []


def test_code_clone_integrity_clean_empty_codes_dir(vault: Path) -> None:
    # ``create_vault`` already lays down ``codes/``; this test asserts the
    # check returns clean when the directory exists but contains no entries.
    (vault / "codes").mkdir(exist_ok=True)
    _write_paper(vault, "2024_Foo_Bar")
    assert check_code_clone_integrity(vault, list_papers(vault)) == []


def test_code_clone_integrity_dangling_clone(vault: Path) -> None:
    """Repo on disk that no paper references → 1 warning."""
    _write_paper(vault, "2024_Foo_Bar")
    _write_repo_meta(vault, "X", papers=[])
    issues = check_code_clone_integrity(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "code_clone_integrity"
    assert issues[0].severity == "warning"
    assert issues[0].paper_id is None
    assert "X" in issues[0].message
    assert "dangling" in issues[0].message
    assert issues[0].hint is not None
    assert "lit code rm X" in issues[0].hint


def test_code_clone_integrity_dangling_ref(vault: Path) -> None:
    """Paper references a repo with no codes/ dir → 1 error."""
    _write_paper(vault, "2024_Foo_Bar", code_clones=["Y"])
    issues = check_code_clone_integrity(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "code_clone_integrity"
    assert issues[0].severity == "error"
    assert issues[0].paper_id == "2024_Foo_Bar"
    assert "Y" in issues[0].message
    assert issues[0].hint is not None
    assert "--rm-tag code-clones=Y" in issues[0].hint


def test_code_clone_integrity_missing_repo_meta(vault: Path) -> None:
    """codes/Z/ dir exists but no repo-meta.yaml inside → 1 error."""
    (vault / "codes" / "Z").mkdir(parents=True)
    _write_paper(vault, "2024_Foo_Bar")
    issues = check_code_clone_integrity(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "code_clone_integrity"
    assert issues[0].severity == "error"
    assert issues[0].paper_id is None
    assert "Z" in issues[0].message
    assert issues[0].hint is not None
    assert "lit code restore-all" in issues[0].hint


def test_code_clone_integrity_all_three_failure_modes(vault: Path) -> None:
    """Dangling clone + dangling ref + missing repo-meta + healthy pair."""
    # Healthy pair: paper P references repo R, both sides exist.
    _write_paper(vault, "P_p_p", code_clones=["R"])
    _write_repo_meta(vault, "R", papers=["P_p_p"])
    # Dangling clone: repo X exists, no paper references it.
    _write_repo_meta(vault, "X", papers=[])
    # Dangling ref: paper references Y, no codes/Y/ dir.
    _write_paper(vault, "Q_q_q", code_clones=["Y"])
    # Missing repo-meta: codes/Z/ exists as dir but no repo-meta.yaml.
    (vault / "codes" / "Z").mkdir(parents=True)

    issues = check_code_clone_integrity(vault, list_papers(vault))
    assert len(issues) == 3
    by_severity: dict[str, list[Any]] = {}
    for i in issues:
        by_severity.setdefault(i.severity, []).append(i)
    # 1 warning (dangling clone X) + 2 errors (dangling ref Y, missing meta Z).
    assert len(by_severity["warning"]) == 1
    assert len(by_severity["error"]) == 2

    warning_msg = by_severity["warning"][0].message
    assert "X" in warning_msg and "dangling" in warning_msg

    error_msgs = {i.message for i in by_severity["error"]}
    assert any("Y" in m for m in error_msgs)
    assert any("Z" in m for m in error_msgs)


def test_code_clone_integrity_skips_non_directory_in_codes(vault: Path) -> None:
    """A regular file under codes/ is not in scope — skip silently."""
    # Healthy pair so the check actually runs over the codes/ dir.
    _write_paper(vault, "P_p_p", code_clones=["R"])
    _write_repo_meta(vault, "R", papers=["P_p_p"])
    # Stray file — must not generate a missing_repo_meta or any other issue.
    (vault / "codes" / "W").write_text("not a dir", encoding="utf-8")

    issues = check_code_clone_integrity(vault, list_papers(vault))
    assert issues == []


# ===========================================================================
# Orchestration + autofix
# ===========================================================================


def test_run_all_checks_aggregates_categories(vault: Path) -> None:
    _write_paper(vault, "A_a_a", related=["GHOST"])
    (vault / ".litman-staging" / "op-crashed").mkdir()
    issues = run_all_checks(vault, list_papers(vault))
    cats = {i.category for i in issues}
    assert "dangling_refs" in cats
    assert "stale_staging" in cats


def test_apply_autofix_clears_staging(vault: Path) -> None:
    op_dir = vault / ".litman-staging" / "op-crashed"
    op_dir.mkdir()
    issues = run_all_checks(vault, [])
    counts = apply_autofix(vault, issues)
    assert counts.get("stale_staging") == 1
    assert not op_dir.exists()


def test_apply_autofix_clears_orphan_sidecar(vault: Path) -> None:
    trash = vault / ".trash"
    trash.mkdir()
    sidecar = trash / "2024_Ghost-20260101T000000Z.meta.yaml"
    sidecar.write_text("paper_id: 2024_Ghost\n", encoding="utf-8")
    issues = run_all_checks(vault, [])
    counts = apply_autofix(vault, issues)
    assert counts.get("orphan_trash_sidecar") == 1
    assert not sidecar.exists()


def test_apply_autofix_skips_non_fixable_categories(vault: Path) -> None:
    _write_paper(vault, "A_a_a", related=["GHOST"])
    issues = run_all_checks(vault, list_papers(vault))
    counts = apply_autofix(vault, issues)
    # No fixable categories present.
    assert counts == {}


def test_auto_fixable_categories_constant() -> None:
    assert AUTO_FIXABLE_CATEGORIES == frozenset(
        {"stale_staging", "orphan_trash_sidecar"}
    )


# ===========================================================================
# CLI integration
# ===========================================================================


def test_health_check_clean_vault_exits_zero(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output


def test_health_check_with_issues_exits_one(vault: Path) -> None:
    _write_paper(vault, "A_a_a", related=["GHOST"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--library", str(vault)]
    )
    assert result.exit_code == 1
    assert "Dangling references" in result.output
    assert "GHOST" in result.output


def test_health_check_fix_clears_staging(vault: Path) -> None:
    op_dir = vault / ".litman-staging" / "op-crashed"
    op_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    assert result.exit_code == 0  # post-fix vault is clean
    assert "Auto-fix" in result.output
    assert not op_dir.exists()


def test_health_check_fix_does_not_touch_unfixable(vault: Path) -> None:
    """--fix should leave dangling refs alone."""
    _write_paper(vault, "A_a_a", related=["GHOST"])
    (vault / ".litman-staging" / "op-crashed").mkdir()
    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    # Still has dangling refs, so exit 1.
    assert result.exit_code == 1
    assert "GHOST" in result.output
    # But staging was cleaned.
    assert not (vault / ".litman-staging" / "op-crashed").exists()


def test_health_check_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--help"])
    assert result.exit_code == 0
    assert "--fix" in result.output
    assert "stale_staging" in result.output
