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
    check_project_config_consistency,
    check_project_path_exists,
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
        "contradicted-by": fields.get("contradicted_by", []),
        "extends": fields.get("extends", []),
        "extended-by": fields.get("extended_by", []),
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


def test_dangling_refs_covers_reverse_fields(vault: Path) -> None:
    # ADR-012: reverse fields (extended-by / contradicted-by) referencing a
    # missing paper must be reported just like forward fields.
    _write_paper(
        vault,
        "A_a_a",
        extended_by=["GHOST_one"],
        contradicted_by=["GHOST_two"],
    )
    issues = check_dangling_refs(vault, list_papers(vault))
    cats = {i.message.split()[0] for i in issues}
    assert cats == {"'extended-by'", "'contradicted-by'"}


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


def test_bidirectional_extends_paired_clean(vault: Path) -> None:
    # ADR-012: extends is symmetric via extended-by. A.extends:[B] +
    # B.extended-by:[A] is a complete pairing → no issue.
    _write_paper(vault, "A_a_a", extends=["B_b_b"])
    _write_paper(vault, "B_b_b", extended_by=["A_a_a"])
    assert check_bidirectional_refs(vault, list_papers(vault)) == []


def test_bidirectional_extends_one_sided_is_error(vault: Path) -> None:
    # A.extends:[B] but B has no extended-by:[A] → reported as error.
    _write_paper(vault, "A_a_a", extends=["B_b_b"])
    _write_paper(vault, "B_b_b")
    issues = check_bidirectional_refs(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "bidirectional_refs"
    assert issues[0].severity == "error"
    assert issues[0].paper_id == "A_a_a"
    assert "extends" in issues[0].message
    assert "extended-by" in issues[0].message


def test_bidirectional_contradicts_paired_clean(vault: Path) -> None:
    _write_paper(vault, "A_a_a", contradicts=["B_b_b"])
    _write_paper(vault, "B_b_b", contradicted_by=["A_a_a"])
    assert check_bidirectional_refs(vault, list_papers(vault)) == []


def test_bidirectional_contradicts_one_sided_is_error(vault: Path) -> None:
    _write_paper(vault, "A_a_a", contradicts=["B_b_b"])
    _write_paper(vault, "B_b_b")
    issues = check_bidirectional_refs(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "contradicts" in issues[0].message
    assert "contradicted-by" in issues[0].message


def test_bidirectional_reverse_field_orphan_is_error(vault: Path) -> None:
    # The residual can also show up on the reverse field: B has
    # extended-by:[A] but A dropped its extends:[B].
    _write_paper(vault, "A_a_a")
    _write_paper(vault, "B_b_b", extended_by=["A_a_a"])
    issues = check_bidirectional_refs(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "extended-by" in issues[0].message


def test_bidirectional_related_still_error_severity(vault: Path) -> None:
    # related residual is now an error too (ADR-012 unified severity).
    _write_paper(vault, "A_a_a", related=["B_b_b"])
    _write_paper(vault, "B_b_b")
    issues = check_bidirectional_refs(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].severity == "error"


# --- dangling_wikilinks -----------------------------------------------------


def test_dangling_wikilinks_clean(vault: Path) -> None:
    _write_paper(vault, "A_a_a", notes="See [[A_a_a]] for context.")
    assert check_dangling_wikilinks(vault, list_papers(vault)) == []


def test_dangling_wikilinks_in_paper_notes(vault: Path) -> None:
    _write_paper(vault, "A_a_a", notes="Related: [[GHOST_x_y]].")
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1
    assert "[[GHOST_x_y]]" in issues[0].message


def test_dangling_wikilinks_dedupes_per_file(vault: Path) -> None:
    """A single file mentioning the same dangling id N times reports only once."""
    _write_paper(vault, "A_a_a", notes="[[ZOMBIE]] [[ZOMBIE]] [[ZOMBIE]]\n")
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1


def test_health_missing_deleted_tag(vault: Path) -> None:
    # M24.2 / AC90 missing-tag: [[X]] at an absent paper with no (deleted)
    # marker → warning (hallucination risk).
    _write_paper(vault, "A_a_a", notes="Related: [[GHOST_x_y]].")
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "not tagged" in issues[0].message
    assert "[[GHOST_x_y]]" in issues[0].message


def test_health_correctly_tagged_deleted_is_clean(vault: Path) -> None:
    # A [[X]] (deleted) whose paper is genuinely absent is the desired
    # post-rm state — no drift, no issue.
    _write_paper(vault, "A_a_a", notes="Gone now: [[GHOST_x_y]] (deleted).\n")
    assert check_dangling_wikilinks(vault, list_papers(vault)) == []


def test_health_missing_tag_after_tagged_same_link(vault: Path) -> None:
    # M24.2 regression: the SAME absent [[X]] appears both (deleted)-tagged
    # AND bare (tagged first). The per-occurrence tag state must not let the
    # tagged occurrence mask the bare one — the bare untagged link is still a
    # missing-tag drift (and a mixed tagged/bare file is exactly the
    # agent-rewrite drift the health-check backstops).
    _write_paper(
        vault,
        "A_a_a",
        notes="Gone: [[GHOST_x_y]] (deleted), but later bare [[GHOST_x_y]].\n",
    )
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "not tagged" in issues[0].message
    assert "[[GHOST_x_y]]" in issues[0].message


def test_health_stale_deleted_tag(vault: Path) -> None:
    # M24.2 / AC90 stale-tag: [[X]] (deleted) but papers/X/ exists (restored)
    # → warning.
    _write_paper(vault, "A_a_a", notes="See [[B_b_b]] (deleted) here.\n")
    _write_paper(vault, "B_b_b")
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "stale deletion tag" in issues[0].message


def test_health_live_link_clean(vault: Path) -> None:
    # A bare [[X]] whose paper exists is clean (no missing-tag false positive).
    _write_paper(vault, "A_a_a", notes="See [[B_b_b]] for context.\n")
    _write_paper(vault, "B_b_b")
    assert check_dangling_wikilinks(vault, list_papers(vault)) == []


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
    # No COMMITTED sentinel → clean abort → info severity (M17 tri-state).
    (vault / ".litman-staging" / "op-crashed").mkdir()
    issues = check_stale_staging(vault, [])
    assert len(issues) == 1
    assert "op-crashed" in issues[0].message
    assert issues[0].severity == "info"
    assert issues[0].category == "stale_staging"


def test_stale_staging_unrecoverable_is_error(vault: Path) -> None:
    """COMMITTED + a manifested relpath missing from both sides → error.

    Not auto-fixable; a human must decide (M17 §M17.2).
    """
    op = vault / ".litman-staging" / "op-torn"
    op.mkdir()
    (op / "MANIFEST.json").write_text(
        '{"op_id": "op-torn", "files": ["papers/2024_X/metadata.yaml"]}',
        encoding="utf-8",
    )
    (op / "COMMITTED").write_bytes(b"")
    issues = check_stale_staging(vault, [])
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].category == "stale_staging_unrecoverable"
    assert "stale_staging_unrecoverable" not in AUTO_FIXABLE_CATEGORIES


def test_stale_staging_unrecoverable_uses_pending_voice(vault: Path) -> None:
    """Read-only check (no --fix): partial tear → pending wording.

    Two manifested relpaths: one already on target (recoverable, --fix
    WOULD roll it forward), one missing from both sides (lost). The
    classifier never promotes, so the message must use conditional/future
    voice with the real recoverable count (1), NOT completed past-tense
    and NOT a hardcoded 0.
    """
    op = vault / ".litman-staging" / "op-partial"
    op.mkdir()
    (op / "MANIFEST.json").write_text(
        '{"op_id": "op-partial", "files": '
        '["papers/2024_R/metadata.yaml", "papers/2024_L/metadata.yaml"]}',
        encoding="utf-8",
    )
    (op / "COMMITTED").write_bytes(b"")
    # papers/2024_R/metadata.yaml already promoted to target → recoverable.
    (vault / "papers" / "2024_R").mkdir(parents=True)
    (vault / "papers" / "2024_R" / "metadata.yaml").write_text(
        "id: 2024_R\n", encoding="utf-8"
    )
    # papers/2024_L/metadata.yaml absent from both staging and target → lost.

    issues = check_stale_staging(vault, [])
    assert len(issues) == 1
    msg = issues[0].message or ""
    assert issues[0].severity == "error"
    assert issues[0].category == "stale_staging_unrecoverable"
    # Pending voice with the real recoverable count (1), not 0, not done.
    assert "可 roll-forward 同 op 内其余 1 个文件" in msg
    assert "（运行 lit health-check --fix 后）" in msg
    assert "已 roll-forward" not in msg
    assert "papers/2024_L/metadata.yaml" in msg
    # No promotion happened — read-only probe must not touch the target.
    assert not (vault / "papers" / "2024_L").exists()
    assert (op / "MANIFEST.json").exists()


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


def test_trash_health_size_warns_above_threshold(vault: Path) -> None:
    """>TRASH_SIZE_WARN entries → info, message aligned to the eviction cap."""
    from litman.core.checks import TRASH_SIZE_WARN
    from litman.core.trash import TRASH_MAX_ENTRIES

    trash = vault / ".trash"
    trash.mkdir()
    for i in range(TRASH_SIZE_WARN + 1):
        (trash / f"2024_E{i:03d}-202601{(i % 28) + 1:02d}T000000Z").mkdir()

    issues = check_trash_health(vault, [])
    size_issues = [i for i in issues if i.category == "trash_size"]
    assert len(size_issues) == 1
    msg = size_issues[0].message
    assert str(TRASH_MAX_ENTRIES) in msg  # message references the cap
    assert "auto-evicted" in msg
    # The retired time-based warning is gone.
    assert all(i.category != "trash_age" for i in issues)


def test_trash_health_no_age_warning(vault: Path) -> None:
    """Old entries no longer raise a `trash_age` info (retired in M22)."""
    trash = vault / ".trash"
    trash.mkdir()
    # An entry dated well over a year ago — would have tripped the old
    # 30-day age warning.
    (trash / "2020_Ancient-20200101T000000Z").mkdir()
    issues = check_trash_health(vault, [])
    assert all(i.category != "trash_age" for i in issues)


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


def test_pdf_viewer_warns_when_xdg_open_headless(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux + xdg-open present + no configured viewer + headless → warn."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_mod.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd == "xdg-open" else None,
    )
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    issues = check_pdf_viewer(vault, [])
    assert len(issues) == 1
    assert issues[0].category == "pdf_viewer"
    assert issues[0].severity == "warning"
    # Distinct from the "not installed" message.
    assert "no graphical display" in issues[0].message
    assert "no platform PDF viewer" not in issues[0].message


def test_pdf_viewer_clean_when_xdg_open_with_display(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux + xdg-open present + DISPLAY set + no configured viewer → clean."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        viewer_mod.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd == "xdg-open" else None,
    )
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
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


def test_health_check_clean_vault_exits_zero(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin platform to macOS so the pdf_viewer probe is deterministically clean
    # regardless of the host's DISPLAY / xdg-open state (CI may be headless).
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
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


def test_health_check_fix_clears_staging(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M17: a no-COMMITTED leftover is rolled back by the vault-open hook
    before checks even run, so health-check sees a clean vault and the
    leftover is gone — no explicit ``--fix`` pass is needed.
    """
    # Pin platform to macOS so the pdf_viewer probe is deterministically clean
    # regardless of the host's DISPLAY / xdg-open state (CI may be headless).
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    op_dir = vault / ".litman-staging" / "op-crashed"
    op_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    assert result.exit_code == 0  # vault self-healed at open
    assert not op_dir.exists()
    assert "All checks passed" in result.output


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


def test_health_check_fix_leaves_klass_b_reported(vault: Path) -> None:
    """M30 Phase 2: --fix never resolves klass-B drift (it needs user judgment).

    A taxonomy_drift finding (klass B-ext, correction=resolve) must survive
    --fix and keep the command at exit 1."""
    _write_paper(vault, "A_a_a", topics=["unregistered-topic"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    assert result.exit_code == 1
    assert "unregistered-topic" in result.output


def test_apply_fixes_regens_klass_a_and_skips_klass_b(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_apply_fixes`` routes klass-A categories through regen, klass-B never.

    Phase 2 registers no klass-A check yet, so we inject a synthetic klass-A
    category into the module's frozenset to exercise the regen branch, and pass
    a klass-B (taxonomy_drift) issue alongside to prove it is left untouched.
    """
    from litman.commands import health
    from litman.core.checks import Issue

    called = {"regen": False}

    def _fake_regen(v: Path, issues: list[Issue]) -> dict[str, int]:
        called["regen"] = True
        return {"index": 1, "views": 0}

    monkeypatch.setattr(health, "regen", _fake_regen)
    monkeypatch.setattr(
        health, "_KLASS_A_CATEGORIES", frozenset({"index_vs_disk"})
    )

    issues = [
        Issue("index_vs_disk", "warning", None, "INDEX has a dead entry"),
        Issue("taxonomy_drift", "warning", "A_a_a", "unregistered topic"),
    ]
    counts = health._apply_fixes(vault, issues)

    # klass-A regen ran and is reported; klass-B (taxonomy_drift) is not.
    assert called["regen"] is True
    assert counts.get("index_vs_disk") == 1
    assert "taxonomy_drift" not in counts


def test_apply_fixes_does_not_regen_for_klass_b_only(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative branch (reviewer Suggestion 1): klass-B-only issues never regen.

    Pins the gate directly — with a known klass-A set that the issue does not
    match, a lone klass-B (taxonomy_drift) finding must leave ``regen``
    uncalled and the klass-B issue left reported (not in the fixed counts).
    """
    from litman.commands import health
    from litman.core.checks import Issue

    called = {"regen": False}

    def _fake_regen(v: Path, issues: list[Issue]) -> dict[str, int]:
        called["regen"] = True
        return {"index": 1, "views": 0}

    monkeypatch.setattr(health, "regen", _fake_regen)
    monkeypatch.setattr(
        health, "_KLASS_A_CATEGORIES", frozenset({"index_vs_disk"})
    )

    issues = [Issue("taxonomy_drift", "warning", "A_a_a", "unregistered topic")]
    counts = health._apply_fixes(vault, issues)

    assert called["regen"] is False
    assert "taxonomy_drift" not in counts


def test_health_check_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--help"])
    assert result.exit_code == 0
    assert "--fix" in result.output
    # M30 Phase 2: --fix help describes the klass-A regen / klass-B report-only
    # policy (it no longer enumerates the two legacy validity categories).
    assert "report-only" in result.output


# ===========================================================================
# M15: project registry health-checks
# ===========================================================================


def _set_taxonomy_projects(vault: Path, names: list[str]) -> None:
    from litman.core.taxonomy import update_user_dict_section

    txt = (vault / "TAXONOMY.md").read_text()
    (vault / "TAXONOMY.md").write_text(
        update_user_dict_section(txt, "projects", names), encoding="utf-8"
    )


def _set_config_projects(vault: Path, mapping: dict[str, str]) -> None:
    lines = [f"library_name: {vault.name}"]
    if mapping:
        lines.append("projects:")
        for k, v in mapping.items():
            lines.append(f"  {k}: {v}")
    (vault / "lit-config.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_project_config_consistency_taxonomy_only_warns(
    vault: Path,
) -> None:
    _set_taxonomy_projects(vault, ["pepforge"])
    _set_config_projects(vault, {})
    issues = check_project_config_consistency(vault, [])
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "TAXONOMY.md" in issues[0].message
    assert issues[0].category == "project_config_consistency"
    assert issues[0].category not in AUTO_FIXABLE_CATEGORIES


def test_project_config_consistency_config_only_warns(
    vault: Path, tmp_path: Path
) -> None:
    d = tmp_path / "proj"
    d.mkdir()
    _set_config_projects(vault, {"pepforge": str(d)})
    # TAXONOMY projects stays empty (seed default).
    issues = check_project_config_consistency(vault, [])
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "lit-config.yaml" in issues[0].message


def test_project_config_consistency_in_sync_no_issue(
    vault: Path, tmp_path: Path
) -> None:
    d = tmp_path / "proj"
    d.mkdir()
    _set_taxonomy_projects(vault, ["pepforge"])
    _set_config_projects(vault, {"pepforge": str(d)})
    assert check_project_config_consistency(vault, []) == []


def test_project_config_consistency_empty_no_issue(vault: Path) -> None:
    assert check_project_config_consistency(vault, []) == []


def test_project_path_exists_missing_warns(
    vault: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "gone"
    _set_config_projects(vault, {"p": str(missing)})
    issues = check_project_path_exists(vault, [])
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "does not exist" in issues[0].message
    assert issues[0].category not in AUTO_FIXABLE_CATEGORIES


def test_project_path_exists_is_file_warns(
    vault: Path, tmp_path: Path
) -> None:
    f = tmp_path / "afile"
    f.write_text("x")
    _set_config_projects(vault, {"p": str(f)})
    issues = check_project_path_exists(vault, [])
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "not a directory" in issues[0].message


def test_project_path_exists_empty_map_no_issue(vault: Path) -> None:
    assert check_project_path_exists(vault, []) == []


def test_project_checks_registered_in_run_all(
    vault: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "gone"
    _set_config_projects(vault, {"p": str(missing)})
    issues = run_all_checks(vault, list_papers(vault))
    cats = {i.category for i in issues}
    assert "project_path_exists" in cats
    assert "project_config_consistency" in cats


# ===========================================================================
# M28: vault registry drift surfaced as a health-check finding
# ===========================================================================


def test_health_check_reports_vault_registry_drift(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dangling vault registry entry must show up as a
    ``vault_registry_drift`` warning in ``lit health-check`` output.

    health-check itself routes through the root-group drift hook too, but
    CliRunner is non-TTY → the hook only emits a single stderr warning
    without mutating the registry, so the dangling entry is still present
    when health-check runs its own probe. Both surfacings coexist by design
    (stderr warning + report finding are two granularities of the same
    information).
    """
    from litman.core.vault_registry import (
        VaultEntry,
        VaultRegistry,
        save_registry,
    )

    # Isolate registry to a tmp HOME so we don't scribble on the dev's real
    # ``~/.config/litman/vaults.yaml``. Local to this test — the rest of
    # test_health.py does not touch the registry, so a module-level fixture
    # would be needless cross-cutting state.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    # Construct a registry with one dangling entry directly (bypassing
    # ``add_vault`` which would reject a non-existent path).
    ghost = tmp_path / "ghost"  # never created on disk
    save_registry(
        VaultRegistry(
            vaults=[
                VaultEntry(name="ghost", path=str(ghost), is_active=False),
            ]
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--library", str(vault)]
    )

    # drift is a warning → counts as an issue → exit 1.
    assert result.exit_code == 1, result.output
    assert "Vault registry drift" in result.output
    assert "ghost" in result.output
    assert "lit vault remove ghost" in result.output
