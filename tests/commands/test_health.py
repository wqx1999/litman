"""Tests for ``lit health-check`` (M2.8).

Per-check unit tests exercise the pure functions in ``litman.core.checks``.
CLI tests exercise the command via Click ``CliRunner``: rendering, exit
code, and ``--fix`` round-trip.
"""

from __future__ import annotations

import os
import sys
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
    check_inbox_staleness,
    check_discussion_scaffold,
    check_paper_dir_validity,
    check_pdf_viewer,
    check_project_config_consistency,
    check_project_path_exists,
    check_schema,
    check_skill_drift,
    check_stale_staging,
    check_taxonomy_drift,
    check_trash_health,
    run_all_checks,
)
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.notes import WIKILINK_REMINDER, discussion_scaffold

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
    (used for id_consistency tests). Pass ``no_discussion=True`` to leave out
    ``discussion.md`` (the pre-scaffold shape the discussion_scaffold check
    flags).
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
        "read-date": fields.get("read_date"),
        "last-revisited": fields.get("last_revisited"),
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
    # The M30 paper_dir_validity check requires paper.pdf; write a stub so a
    # complete fixture paper does not trip a structural error (tests that want
    # to assert the missing-pdf finding can delete it).
    if not fields.get("no_pdf"):
        (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4 stub\n")
    # Same reasoning for the discussion log: `lit add` scaffolds it, so a
    # fixture paper without one is not "complete" — it is the pre-scaffold shape
    # the discussion_scaffold check exists to flag. Opt out with no_discussion.
    if not fields.get("no_discussion"):
        (paper_dir / "discussion.md").write_text(
            discussion_scaffold(paper_id), encoding="utf-8"
        )


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


def test_schema_clean_with_consistent_dates(vault: Path) -> None:
    """read-date ≤ last-revisited ≤ today → no ordering issue (invariant #11)."""
    _write_paper(
        vault, "2024_Foo_Bar", read_date="2026-05-01", last_revisited="2026-05-10"
    )
    assert check_schema(vault, list_papers(vault)) == []


def test_schema_flags_last_revisited_without_read_date(vault: Path) -> None:
    """A revisit presupposes a first read: last-revisited set + read-date empty
    is an ordering error."""
    _write_paper(vault, "2024_Foo_Bar", last_revisited="2026-05-10")
    issues = check_schema(vault, list_papers(vault))
    assert any(
        i.category == "schema" and "read-date is not" in i.message for i in issues
    )


def test_schema_flags_read_date_after_last_revisited(vault: Path) -> None:
    """read-date later than last-revisited → ordering error."""
    _write_paper(
        vault, "2024_Foo_Bar", read_date="2026-05-20", last_revisited="2026-05-10"
    )
    issues = check_schema(vault, list_papers(vault))
    assert any(
        i.category == "schema" and "after last-revisited" in i.message
        for i in issues
    )


# --- paper_dir_validity (merged id_consistency + invalid_paper_dirs, M30) ---


def test_paper_dir_validity_clean(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    assert check_paper_dir_validity(vault, list_papers(vault)) == []


def test_paper_dir_validity_dir_vs_metadata_mismatch(vault: Path) -> None:
    # Directory is "2024_Foo_Bar" but metadata.id is "2024_Foo_Baz" — looks
    # like a half-finished rename: file content updated, dir not yet renamed.
    _write_paper(vault, "2024_Foo_Bar", override_id="2024_Foo_Baz")
    issues = check_paper_dir_validity(vault, list_papers(vault))
    mismatch = [i for i in issues if "metadata id" in i.message]
    assert len(mismatch) == 1
    assert mismatch[0].category == "paper_dir_validity"
    assert mismatch[0].severity == "error"
    assert "2024_Foo_Bar" in mismatch[0].message
    assert "2024_Foo_Baz" in mismatch[0].message


def test_paper_dir_validity_no_metadata(vault: Path) -> None:
    (vault / "papers" / "2024_Foo_Bar").mkdir(parents=True)
    issues = check_paper_dir_validity(vault, [])
    assert any(
        i.category == "paper_dir_validity" and "no metadata.yaml" in i.message
        for i in issues
    )


def test_paper_dir_validity_bad_id_name(vault: Path) -> None:
    # Spaces are not allowed in paper ids — surfaces as invalid.
    (vault / "papers" / "Bad Dir Name").mkdir(parents=True, exist_ok=True)
    issues = check_paper_dir_validity(vault, [])
    assert any(
        i.category == "paper_dir_validity" and "valid paper id" in i.message
        for i in issues
    )


def test_paper_dir_validity_non_directory_file(vault: Path) -> None:
    (vault / "papers" / "stray.txt").write_text("oops")
    issues = check_paper_dir_validity(vault, [])
    assert any(
        i.category == "paper_dir_validity" and "non-directory" in i.message
        for i in issues
    )


def test_paper_dir_validity_unparseable_metadata_is_an_error(vault: Path) -> None:
    """A corrupt metadata.yaml is an EMITTED error, not a silent drop (#14)."""
    paper_dir = vault / "papers" / "2024_Foo_Bar"
    paper_dir.mkdir(parents=True)
    # Invalid YAML (unterminated flow mapping) — read_metadata would raise.
    (paper_dir / "metadata.yaml").write_text("id: {oops\n", encoding="utf-8")
    (paper_dir / "paper.pdf").write_bytes(b"%PDF stub\n")
    issues = check_paper_dir_validity(vault, list_papers(vault))
    corrupt = [i for i in issues if "unparseable" in i.message]
    assert len(corrupt) == 1
    assert corrupt[0].severity == "error"
    assert corrupt[0].paper_id == "2024_Foo_Bar"


def test_paper_dir_validity_empty_metadata_is_an_error(vault: Path) -> None:
    paper_dir = vault / "papers" / "2024_Foo_Bar"
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text("# only a comment\n", encoding="utf-8")
    (paper_dir / "paper.pdf").write_bytes(b"%PDF stub\n")
    issues = check_paper_dir_validity(vault, list_papers(vault))
    assert any(
        i.category == "paper_dir_validity"
        and "empty" in i.message
        and i.severity == "error"
        for i in issues
    )


def test_paper_dir_validity_missing_pdf_is_an_error(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", no_pdf=True)
    issues = check_paper_dir_validity(vault, list_papers(vault))
    pdf = [i for i in issues if "paper.pdf" in i.message]
    assert len(pdf) == 1
    assert pdf[0].severity == "error"
    assert pdf[0].paper_id == "2024_Foo_Bar"


def test_paper_dir_validity_ignores_authored_markdown(vault: Path) -> None:
    """This check never looks at the authored markdown.

    notes.md absence stays a legitimate state (nothing depends on it existing);
    discussion.md absence IS a finding, but ``check_discussion_scaffold``'s, not
    this one's.
    """
    _write_paper(vault, "2024_Foo_Bar", no_discussion=True)  # metadata + pdf only
    paper_dir = vault / "papers" / "2024_Foo_Bar"
    assert not (paper_dir / "notes.md").exists()
    assert not (paper_dir / "discussion.md").exists()
    assert check_paper_dir_validity(vault, list_papers(vault)) == []


# --- discussion_scaffold -----------------------------------------------------


def test_discussion_scaffold_clean(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")  # the fixture scaffolds discussion.md
    assert check_discussion_scaffold(vault, list_papers(vault)) == []


def test_discussion_scaffold_flags_missing_file(vault: Path) -> None:
    """A paper added before the scaffold landed has no discussion.md at all."""
    _write_paper(vault, "2024_Foo_Bar", no_discussion=True)
    issues = check_discussion_scaffold(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "discussion_scaffold"
    assert issues[0].severity == "info"
    assert issues[0].paper_id == "2024_Foo_Bar"


def test_discussion_scaffold_flags_stripped_header(vault: Path) -> None:
    """The log exists but its append-format header was edited away."""
    _write_paper(vault, "2024_Foo_Bar")
    disc = vault / "papers" / "2024_Foo_Bar" / "discussion.md"
    disc.write_text("# Discussion log\n\n## 2026-07-11 10:00\n\nq\n", encoding="utf-8")
    issues = check_discussion_scaffold(vault, list_papers(vault))
    assert len(issues) == 1
    assert "append-format header" in issues[0].message


def test_fix_backfills_discussion_and_keeps_the_log(vault: Path) -> None:
    """--fix creates the missing log and restores a torn-out header — additively.

    The paper with an existing (header-less) log keeps every dated section it
    already holds: this fix only ever adds the anchor back.
    """
    _write_paper(vault, "2024_Foo_Bar", no_discussion=True)
    _write_paper(vault, "2024_Baz_Qux")
    torn = vault / "papers" / "2024_Baz_Qux" / "discussion.md"
    torn.write_text(
        "# Discussion log for 2024_Baz_Qux\n\n## 2026-06-30 09:27\n\n"
        "**Question:** why does it work?\n",
        encoding="utf-8",
    )

    issues = check_discussion_scaffold(vault, list_papers(vault))
    assert len(issues) == 2
    counts = apply_autofix(vault, issues)
    assert counts["discussion_scaffold"] == 2

    created = (vault / "papers" / "2024_Foo_Bar" / "discussion.md").read_text(
        encoding="utf-8"
    )
    assert created.startswith("# Discussion log for 2024_Foo_Bar")

    healed = torn.read_text(encoding="utf-8")
    assert "## 2026-06-30 09:27" in healed
    assert "**Question:** why does it work?" in healed

    # And the vault is clean afterwards.
    assert check_discussion_scaffold(vault, list_papers(vault)) == []


# --- index_vs_disk (M30 #1) -------------------------------------------------


def _build_index(vault: Path) -> None:
    """Build INDEX.json + views from the on-disk paper set (regen path)."""
    from litman.core.correctors import regen

    regen(vault)


def test_index_vs_disk_clean(vault: Path) -> None:
    from litman.core.checks import check_index_vs_disk

    _write_paper(vault, "2024_Foo_Bar")
    _build_index(vault)
    assert check_index_vs_disk(vault, []) == []


def test_index_vs_disk_vanished_id_is_error(vault: Path) -> None:
    """An id in INDEX whose papers/<id>/ is gone → error (manual rm)."""
    from litman.core.checks import check_index_vs_disk

    _write_paper(vault, "2024_Foo_Bar")
    _build_index(vault)
    # Manual rm of the paper dir, INDEX not rebuilt.
    import shutil

    shutil.rmtree(vault / "papers" / "2024_Foo_Bar")
    issues = check_index_vs_disk(vault, [])
    vanished = [i for i in issues if i.severity == "error"]
    assert len(vanished) == 1
    assert vanished[0].category == "index_vs_disk"
    assert vanished[0].paper_id == "2024_Foo_Bar"


def test_index_vs_disk_unindexed_dir_is_warning(vault: Path) -> None:
    """A dir present but not in INDEX → warning (corrupt metadata / interrupted add)."""
    from litman.core.checks import check_index_vs_disk

    _write_paper(vault, "2024_Foo_Bar")
    _build_index(vault)
    # Add a second paper dir on disk WITHOUT rebuilding INDEX.
    _write_paper(vault, "2025_New_Paper")
    issues = check_index_vs_disk(vault, [])
    warnings = [i for i in issues if i.severity == "warning"]
    assert len(warnings) == 1
    assert warnings[0].category == "index_vs_disk"
    assert warnings[0].paper_id == "2025_New_Paper"
    assert "not indexed" in warnings[0].message


def test_index_vs_disk_no_index_yet_is_clean(vault: Path) -> None:
    """A fresh vault (paper on disk, no INDEX) is not reconcilable → no vanished."""
    from litman.core.checks import check_index_vs_disk

    _write_paper(vault, "2024_Foo_Bar")
    (vault / "INDEX.json").unlink(missing_ok=True)
    issues = check_index_vs_disk(vault, [])
    # Only an un-indexed-dir warning (INDEX empty), no vanished error.
    assert all(i.severity == "warning" for i in issues)


def test_index_vs_disk_does_not_read_metadata(vault: Path, monkeypatch) -> None:
    """Invariant #15: the cheap INDEX↔disk check reads no per-paper metadata."""
    from litman.core.checks import check_index_vs_disk

    _write_paper(vault, "2024_Foo_Bar")
    _build_index(vault)

    real_read_text = Path.read_text

    def _guard(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
        if self.name == "metadata.yaml":
            raise AssertionError(
                f"index_vs_disk read per-paper metadata (invariant #15): {self}"
            )
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _guard)
    assert check_index_vs_disk(vault, []) == []


# --- views_vs_metadata (M30 #2) ---------------------------------------------


def test_views_vs_metadata_clean(vault: Path) -> None:
    from litman.core.checks import check_views_vs_metadata

    _write_paper(vault, "2024_Foo_Bar", topics=["amp"])
    _build_index(vault)
    assert check_views_vs_metadata(vault, list_papers(vault)) == []


def test_views_vs_metadata_missing_symlink_is_error(vault: Path) -> None:
    from litman.core.checks import check_views_vs_metadata

    _write_paper(vault, "2024_Foo_Bar", topics=["amp"])
    # views/ hub exists (create_vault lays it down) but no symlink was built →
    # metadata implies a missing views/by-topic/amp/2024_Foo_Bar symlink.
    (vault / "views" / "by-topic").mkdir(parents=True, exist_ok=True)
    issues = check_views_vs_metadata(vault, list_papers(vault))
    missing = [i for i in issues if "missing" in i.message]
    assert any(i.category == "views_vs_metadata" for i in missing)


def test_views_vs_metadata_stale_symlink_is_error(vault: Path) -> None:
    from litman.core.checks import check_views_vs_metadata

    _write_paper(vault, "2024_Foo_Bar", topics=["amp"])
    _build_index(vault)
    # Hand-edit metadata to drop the topic, but leave the view symlink behind.
    import shutil

    # Simulate: a stale symlink in a bucket the paper no longer belongs to.
    stale_bucket = vault / "views" / "by-topic" / "ghost-topic"
    stale_bucket.mkdir(parents=True, exist_ok=True)
    (stale_bucket / "2024_Foo_Bar").symlink_to(vault / "papers" / "2024_Foo_Bar")
    issues = check_views_vs_metadata(vault, list_papers(vault))
    stale = [i for i in issues if "no matching metadata tag" in i.message]
    assert len(stale) == 1
    assert stale[0].category == "views_vs_metadata"
    del shutil  # keep import used


def test_views_vs_metadata_junction_links_are_seen(
    vault: Path, fake_junction
) -> None:
    """Windows regression (2026-07-14 manual round): view links are junctions
    there, and junctions answer ``is_junction()`` only. With bare
    ``is_symlink()`` detection the on-disk scan was always empty, so a
    perfectly healthy library reported one "link is missing" error PER LINK,
    exited 1, and ``--fix`` rebuilt the same links forever without ever
    converging."""
    from litman.core.checks import check_views_vs_metadata

    _write_paper(vault, "2024_Foo_Bar", topics=["amp"])
    _build_index(vault)
    # Swap the POSIX-built symlinks for junction stand-ins — what the same
    # vault looks like on NTFS.
    for view, bucket in (("by-topic", "amp"), ("by-status", "deep-read")):
        fake_junction(vault / "views" / view / bucket / "2024_Foo_Bar")
    assert check_views_vs_metadata(vault, list_papers(vault)) == []


def test_views_vs_metadata_stale_junction_is_error(
    vault: Path, fake_junction
) -> None:
    """The stale arm needs junction eyes too: with the empty on-disk scan a
    leftover junction in a bucket the paper no longer belongs to was silently
    never reported on Windows."""
    from litman.core.checks import check_views_vs_metadata

    _write_paper(vault, "2024_Foo_Bar", topics=["amp"])
    _build_index(vault)
    fake_junction(
        vault / "views" / "by-topic" / "ghost-topic" / "2024_Foo_Bar"
    )
    issues = check_views_vs_metadata(vault, list_papers(vault))
    stale = [i for i in issues if "no matching metadata tag" in i.message]
    assert len(stale) == 1
    assert stale[0].category == "views_vs_metadata"


# --- relevance_orphan (M30 #11) ---------------------------------------------


def test_relevance_orphan_clean(vault: Path) -> None:
    from litman.core.checks import check_relevance_orphan

    paper_dir = vault / "papers" / "2024_Foo_Bar"
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_Foo_Bar\nprojects:\n  - pep\nrelevance-pep: useful\n",
        encoding="utf-8",
    )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF stub\n")
    assert check_relevance_orphan(vault, list_papers(vault)) == []


def test_relevance_orphan_detected_report_only(vault: Path) -> None:
    from litman.core.checks import check_relevance_orphan

    paper_dir = vault / "papers" / "2024_Foo_Bar"
    paper_dir.mkdir(parents=True)
    # relevance-pep present but projects does NOT contain pep → orphan.
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_Foo_Bar\nprojects: []\nrelevance-pep: stale note\n",
        encoding="utf-8",
    )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF stub\n")
    issues = check_relevance_orphan(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "relevance_orphan"
    assert issues[0].severity == "warning"  # report-only, never auto-delete
    assert issues[0].paper_id == "2024_Foo_Bar"
    assert "relevance-pep" in issues[0].message


# --- project_references (M30 #3) --------------------------------------------


def _configure_project(vault: Path, name: str, project_dir: Path) -> None:
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  {name}: {project_dir}\n",
        encoding="utf-8",
    )


def test_project_references_clean(vault: Path, tmp_path: Path) -> None:
    from litman.core.checks import check_project_references
    from litman.core.correctors import regen

    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    _write_paper(vault, "2024_Foo_Bar", projects=["myproj"])
    regen(vault)  # builds litman_reflib + REFERENCES.md
    assert check_project_references(vault, list_papers(vault)) == []


def test_project_references_missing_symlink_is_error(
    vault: Path, tmp_path: Path
) -> None:
    from litman.core.checks import check_project_references

    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    _write_paper(vault, "2024_Foo_Bar", projects=["myproj"])
    # No reflib built → membership implies a missing symlink + missing REFS.
    issues = check_project_references(vault, list_papers(vault))
    assert any(
        i.category == "project_references" and i.severity == "error"
        for i in issues
    )


def test_project_references_unreachable_dir_skipped(
    vault: Path, tmp_path: Path
) -> None:
    """A project dir that does not exist is left to project_path_exists, not flagged here."""
    from litman.core.checks import check_project_references

    _configure_project(vault, "gone", tmp_path / "nonexistent")
    _write_paper(vault, "2024_Foo_Bar", projects=["gone"])
    assert check_project_references(vault, list_papers(vault)) == []


# --- project_bridge_dangling (#3's cheap arm) --------------------------------


def test_project_bridge_dangling_moved_vault_reported_and_fixed(
    vault: Path, tmp_path: Path
) -> None:
    """The state the name-set check is blind to: the vault moved, every
    bridge dangles, yet every link NAME still matches membership. The cheap
    check reports it and ``--fix``'s klass-A regen re-points the bridges."""
    from litman.core.checks import check_project_bridge_dangling
    from litman.core.correctors import regen

    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    _write_paper(vault, "2024_Foo_Bar", projects=["myproj"])
    regen(vault)  # builds litman_reflib + REFERENCES.md — bridges healthy
    assert check_project_bridge_dangling(vault, []) == []

    moved = tmp_path / "moved_vault"
    vault.rename(moved)
    link = proj / "litman_reflib" / "2024_Foo_Bar"
    assert link.is_symlink() and not link.exists()  # dangling, name intact

    issues = check_project_bridge_dangling(moved, [])
    assert len(issues) == 1
    assert issues[0].category == "project_bridge_dangling"
    assert issues[0].severity == "error"

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--library", str(moved)])
    flat = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "myproj" in flat
    assert "points at nothing" in flat  # n=1 → singular verb

    runner.invoke(cli, ["health-check", "--fix", "--library", str(moved)])
    assert link.is_symlink()
    assert link.resolve() == (moved / "papers" / "2024_Foo_Bar").resolve()
    assert check_project_bridge_dangling(moved, []) == []


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


def test_dangling_wikilinks_present_but_corrupt_paper_not_flagged(
    vault: Path,
) -> None:
    # Review F8: a [[X]] whose papers/X/ exists but has corrupt metadata.yaml
    # (so list_papers drops it) must NOT be flagged as an absent paper —
    # directory presence is the truth (ADR-013). The corrupt paper itself is
    # owned by check_paper_dir_validity, not double-reported here as a dangling
    # link.
    _write_paper(vault, "X_x_x")
    (vault / "papers" / "X_x_x" / "metadata.yaml").write_text(
        ": : [bad yaml", encoding="utf-8"
    )
    _write_paper(vault, "A_a_a", notes="See [[X_x_x]] for context.")
    assert check_dangling_wikilinks(vault, list_papers(vault)) == []


def test_dangling_wikilinks_ignores_seeded_reminder_comment(vault: Path) -> None:
    # Regression: the wikilink-format reminder (core/notes.py WIKILINK_REMINDER)
    # seeded into every notes.md and self-healed on each reading-session close
    # embeds a literal ``[[paper-id]]`` inside an HTML comment to demonstrate the
    # syntax. It is scaffolding, not a real edge, and must NOT be reported as a
    # dangling same-vault link — otherwise every paper in the library trips this
    # warning and drowns the real ones.
    assert "[[paper-id]]" in WIKILINK_REMINDER  # guard: reminder still embeds it
    _write_paper(vault, "A_a_a", notes=f"{WIKILINK_REMINDER}\n\nReal notes.\n")
    assert check_dangling_wikilinks(vault, list_papers(vault)) == []


def test_dangling_wikilinks_comment_stripping_is_scoped(vault: Path) -> None:
    # Comment-stripping must only silence links INSIDE comments: a real dangling
    # ``[[GHOST]]`` in body text is still flagged even when another dangling link
    # is commented out in the same file.
    _write_paper(
        vault,
        "A_a_a",
        notes="<!-- ignore [[HIDDEN_x_y]] -->\nReal: [[GHOST_x_y]].\n",
    )
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    assert len(issues) == 1
    assert "[[GHOST_x_y]]" in issues[0].message
    assert "HIDDEN_x_y" not in issues[0].message


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
    os.chmod(tax, 0o644)  # unlock for hand-edit (M32 locks it at create_vault)
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
    assert "the other 1 file(s) in the same op can be rolled forward" in msg
    assert "(after running lit health-check --fix)" in msg
    assert "rolled the other" not in msg
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
    """Desktop arm: a display is reachable, so 'install xdg-utils' is real
    advice and the finding stays a warning (headless arm is tested below)."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(viewer_mod.shutil, "which", lambda cmd: None)
    monkeypatch.setenv("DISPLAY", ":0")
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


def test_pdf_viewer_headless_is_info_so_health_can_still_exit_zero(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux + xdg-open present + no configured viewer + headless → info.

    "This SSH/cron session has no screen" is an environment fact, not vault
    damage. As a warning it made ``lit health-check`` exit 1 forever on every
    headless box, breaking its documented cron/CI-gate use — the same failure
    mode ``links_unsupported`` was demoted for.
    """
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
    assert issues[0].severity == "info"
    # Distinct from the "not installed" message.
    assert "no graphical display" in issues[0].message
    assert "no platform PDF viewer" not in issues[0].message


def test_pdf_viewer_missing_is_info_headless_but_warning_on_a_desktop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No viewer at all: env-shaped info on a headless box (nothing there to
    install a display for), actionable warning on a desktop (installing
    xdg-utils genuinely fixes `lit open`)."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "linux")
    monkeypatch.setattr(viewer_mod.shutil, "which", lambda cmd: None)

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    headless = check_pdf_viewer(vault, [])
    assert [i.severity for i in headless] == ["info"]

    monkeypatch.setenv("DISPLAY", ":0")
    desktop = check_pdf_viewer(vault, [])
    assert [i.severity for i in desktop] == ["warning"]


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


# --- skill_drift --------------------------------------------------------------
#
# The conftest ``_isolate_skills_dir`` autouse fixture points the call-time
# skills dir at an empty per-test path; tests that need installed skills
# re-patch ``default_skills_parent_dir`` at a dir they populate (a test-body
# patch wins over the fixture's).


def _plant_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    from litman.core.skill import install_all_skills

    parent = tmp_path / "installed-skills"
    install_all_skills(parent_dir=parent)
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir", lambda: parent
    )
    return parent


def test_skill_drift_clean_when_no_skills_installed(vault: Path) -> None:
    """Never installing a skill is a respected opt-out, not drift."""
    assert check_skill_drift(vault, []) == []


def test_skill_drift_clean_when_current(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_skills(tmp_path, monkeypatch)
    assert check_skill_drift(vault, []) == []


def test_skill_drift_stale_is_warning_naming_skill_and_file(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _plant_skills(tmp_path, monkeypatch)
    (parent / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    issues = check_skill_drift(vault, [])
    assert len(issues) == 1
    issue = issues[0]
    assert issue.category == "skill_drift"
    assert issue.severity == "warning"
    assert "lit-library" in issue.message
    assert "SKILL.md" in issue.message
    assert "--fix" in (issue.hint or "")


def test_skill_drift_linked_dir_is_clean(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked skill dir is dev-managed; diverging is the point."""
    parent = tmp_path / "installed-skills"
    parent.mkdir()
    real = tmp_path / "dev-checkout" / "lit-library"
    real.mkdir(parents=True)
    (parent / "lit-library").symlink_to(real)
    monkeypatch.setattr(
        "litman.core.skill.default_skills_parent_dir", lambda: parent
    )
    assert check_skill_drift(vault, []) == []


def test_skill_drift_category_is_auto_fixable(vault: Path) -> None:
    assert "skill_drift" in AUTO_FIXABLE_CATEGORIES


def _plant_standard_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Install the bundle into an open-standard dir stand-in (the gemini /
    cursor location) and point the standard resolver at it."""
    from litman.core.skill import install_all_skills

    parent = tmp_path / "installed-standard-skills"
    install_all_skills(parent_dir=parent)
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: parent
    )
    return parent


def test_skill_drift_probes_only_default_agent_dir(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=claude (nothing recorded): a stale copy in the gemini/cursor
    standard dir is NOT reported — non-default directories are not the
    check's business (they surface in the GUI per-agent panel and the moment
    that agent becomes the default)."""
    standard = _plant_standard_skills(tmp_path, monkeypatch)
    (standard / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    assert check_skill_drift(vault, []) == []


def test_skill_drift_default_gemini_detects_standard_dir(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=gemini: the SAME stale standard-dir copy is now drift, the
    message names the default agent, and a stale claude-dir copy is ignored
    (default-dir semantics both ways). No literal path in the message —
    issues flow into GET /api/health verbatim."""
    from litman.core import agent_prefs

    claude_parent = _plant_skills(tmp_path, monkeypatch)
    standard = _plant_standard_skills(tmp_path, monkeypatch)
    (standard / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    (claude_parent / "lit-reading" / "SKILL.md").write_text(
        "ALSO OUTDATED\n", encoding="utf-8"
    )
    agent_prefs.save_default_agent("gemini")  # registry dir is isolated

    issues = check_skill_drift(vault, [])
    assert len(issues) == 1  # the claude-dir staleness is not reported
    issue = issues[0]
    assert "lit-library" in issue.message
    assert "gemini" in issue.message
    assert str(standard) not in issue.message
    assert str(standard) not in (issue.hint or "")


def test_skill_drift_default_gemini_absent_is_not_drift(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=gemini with nothing installed in the standard dir: absent is
    a respected opt-out, even when the claude dir has (stale) skills."""
    from litman.core import agent_prefs

    parent = _plant_skills(tmp_path, monkeypatch)
    (parent / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    agent_prefs.save_default_agent("gemini")
    assert check_skill_drift(vault, []) == []


def test_apply_autofix_skill_drift_targets_default_agent_dir(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审查 W3 regression: probe and --fix must agree on the directory. With
    default=gemini, the fix refreshes the standard dir and leaves a stale
    claude-dir copy alone — otherwise the check could never come clean."""
    from litman.core import agent_prefs

    claude_parent = _plant_skills(tmp_path, monkeypatch)
    standard = _plant_standard_skills(tmp_path, monkeypatch)
    stale_standard = standard / "lit-library" / "SKILL.md"
    stale_standard.write_text("OUTDATED\n", encoding="utf-8")
    stale_claude = claude_parent / "lit-library" / "SKILL.md"
    stale_claude.write_text("CLAUDE-DIR OUTDATED\n", encoding="utf-8")
    agent_prefs.save_default_agent("gemini")

    issues = check_skill_drift(vault, [])
    counts = apply_autofix(vault, issues)
    assert counts["skill_drift"] == 1
    assert "OUTDATED" not in stale_standard.read_text(encoding="utf-8")
    # The non-default claude dir was NOT touched by the fix.
    assert stale_claude.read_text(encoding="utf-8") == "CLAUDE-DIR OUTDATED\n"
    # Post-fix pass is clean for the default agent.
    assert check_skill_drift(vault, []) == []


def test_health_check_cli_stale_skill_warns_and_fix_refreshes(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the REAL default probe (inject-seam lesson): a
    stale installed skill gates a clean vault at exit 1; ``--fix`` re-copies
    the bundled files, keeps the user's own file, and the post-fix pass is
    clean (exit 0)."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    parent = _plant_skills(tmp_path, monkeypatch)
    stale_md = parent / "lit-library" / "SKILL.md"
    stale_md.write_text("OUTDATED\n", encoding="utf-8")
    user_file = parent / "lit-library" / "my_local_notes.md"
    user_file.write_text("mine\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert result.exit_code == 1
    assert "skill_drift" in result.output or "skill" in result.output.lower()
    assert "fixable via --fix" in result.output

    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "skill_drift" in result.output
    text = stale_md.read_text(encoding="utf-8")
    assert "OUTDATED" not in text
    assert "name: lit-library" in text
    assert user_file.read_text(encoding="utf-8") == "mine\n"


# --- code_clone_integrity ---------------------------------------------------


def _write_repo_meta(vault: Path, repo_name: str, papers: list[str]) -> None:
    """Lay down a minimal ``codes/<repo_name>/repo-meta.yaml``.

    The integrity check only inspects file existence — schema validation
    is out of scope — so the payload need only be a parseable mapping.
    """
    repo_dir = vault / "codes" / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    # Real clones always have codes/<name>/repo/; create it so the M30 #6b
    # check (repo-meta present but repo/ checkout missing) does not fire for
    # tests targeting the other failure modes.
    (repo_dir / "repo").mkdir(exist_ok=True)
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
    assert "lit code unlink Y --paper 2024_Foo_Bar" in issues[0].hint


def test_code_clone_integrity_asymmetric_forward_missing(vault: Path) -> None:
    """repo-meta names a live paper that does NOT list the repo back → 1 error.

    The #6d symmetric check: A is a healthy both-sides binding (so R is not a
    dangling clone), but B exists and loads while its code-clones omits R, yet
    R's reverse ``papers:`` names B. Older checks (dangling ref / dangling
    reverse) miss this one-sided binding.
    """
    _write_paper(vault, "2024_A", code_clones=["R"])
    _write_paper(vault, "2024_B", code_clones=[])
    _write_repo_meta(vault, "R", papers=["2024_A", "2024_B"])
    issues = check_code_clone_integrity(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "code_clone_integrity"
    assert issues[0].severity == "error"
    assert issues[0].paper_id == "2024_B"
    assert "one-sided" in issues[0].message
    assert issues[0].hint is not None
    assert "lit code unlink R --paper 2024_B" in issues[0].hint


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


def test_code_clone_integrity_6b_missing_repo_checkout(vault: Path) -> None:
    """#6b: repo-meta.yaml present but codes/<name>/repo/ checkout missing → warning."""
    _write_paper(vault, "P_p_p", code_clones=["R"])
    _write_repo_meta(vault, "R", papers=["P_p_p"])
    # Remove the repo/ checkout left behind by _write_repo_meta.
    (vault / "codes" / "R" / "repo").rmdir()
    issues = check_code_clone_integrity(vault, list_papers(vault))
    missing_checkout = [i for i in issues if "checkout" in i.message]
    assert len(missing_checkout) == 1
    assert missing_checkout[0].category == "code_clone_integrity"
    assert missing_checkout[0].severity == "warning"
    assert "R" in missing_checkout[0].message


def test_code_clone_integrity_6c_repo_meta_references_missing_paper(
    vault: Path,
) -> None:
    """#6c: repo-meta.papers lists a paper whose papers/<id>/ is gone → error."""
    _write_paper(vault, "P_p_p", code_clones=["R"])
    # repo-meta back-references P_p_p (exists) AND Q_q_q (does NOT exist).
    _write_repo_meta(vault, "R", papers=["P_p_p", "Q_q_q"])
    issues = check_code_clone_integrity(vault, list_papers(vault))
    dangling_back = [i for i in issues if i.paper_id == "Q_q_q"]
    assert len(dangling_back) == 1
    assert dangling_back[0].category == "code_clone_integrity"
    assert dangling_back[0].severity == "error"
    assert "Q_q_q" in dangling_back[0].message


# --- dangling_wikilinks no-silent-skip (M30 #14) ----------------------------


def test_dangling_wikilinks_unreadable_notes_is_a_finding(
    vault: Path, monkeypatch
) -> None:
    """An unreadable notes file is reported, not silently skipped (invariant #14)."""
    _write_paper(vault, "2024_Foo_Bar", notes="see [[ghost]]\n")

    real_read_text = Path.read_text

    def _fail(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
        if self.name == "notes.md":
            raise OSError("permission denied")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _fail)
    issues = check_dangling_wikilinks(vault, list_papers(vault))
    unreadable = [i for i in issues if "could not read" in i.message]
    assert len(unreadable) == 1
    assert unreadable[0].category == "dangling_wikilinks"
    assert unreadable[0].severity == "warning"


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
        {
            "stale_staging",
            "orphan_trash_sidecar",
            "discussion_scaffold",
            "skill_drift",
        }
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
    # A truly clean vault has its derived artifacts (INDEX.json + views/) built;
    # `lit add` does this automatically, but the fixture writes papers directly,
    # so regen here so the M30 index_vs_disk / views_vs_metadata klass-A checks
    # see a vault in sync (otherwise they correctly report the un-built derived
    # state).
    from litman.core.correctors import regen

    regen(vault)
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


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_health_check_relocks_writable_truth_and_reports(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """health-check re-locks a TRUTH file made writable and reports the count (AC#4)."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    _write_paper(vault, "2024_Foo_Bar")
    from litman.core.correctors import regen

    regen(vault)
    # _write_paper writes metadata.yaml writable (bypasses the lock); simulate a
    # writable TRUTH file (post-pull / hand-edit). TAXONOMY.md is also unlocked.
    meta = vault / "papers" / "2024_Foo_Bar" / "metadata.yaml"
    assert os.access(meta, os.W_OK)
    os.chmod(vault / "TAXONOMY.md", 0o644)

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "Re-locked" in result.output
    assert not os.access(meta, os.W_OK)
    assert not os.access(vault / "TAXONOMY.md", os.W_OK)


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_health_check_no_relock_noise_when_clean(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every TRUTH file is already locked, health-check prints no re-lock line."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    _write_paper(vault, "2024_Foo_Bar")
    from litman.core.correctors import regen
    from litman.core.locking import ensure_truth_locked

    regen(vault)
    ensure_truth_locked(vault)  # lock everything up front

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "Re-locked" not in result.output


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


def test_apply_fixes_propagates_project_refs_rebuild_failure(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real project-refs rebuild failure must NOT be reported as a fix.

    Reviewer fix (M30 Phase 3): with regen's except narrowed to the config load
    only, a genuine rebuild failure (permission error / symlink failure)
    propagates out of regen → ``_apply_fixes`` → the caller, instead of being
    swallowed and falsely surfaced as "project_references: 1". This test uses
    the REAL ``regen`` (not a fake) so it pins the swallow narrowing end-to-end.
    """
    from litman.commands import health
    from litman.core.checks import Issue
    import litman.core.project_refs as project_refs

    _write_paper(vault, "2024_A_Foo", title="Foo", projects=["pep"])
    proj_dir = tmp_path / "pep_project"
    proj_dir.mkdir()
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  pep: {proj_dir}\n",
        encoding="utf-8",
    )

    def _boom(*a: object, **kw: object) -> None:
        raise PermissionError("cannot write REFERENCES.md")

    monkeypatch.setattr(project_refs, "rebuild_all_project_refs", _boom)
    # Treat project_references as the fired klass-A category so _apply_fixes
    # routes through the real regen (which now must propagate the failure).
    monkeypatch.setattr(
        health, "_KLASS_A_CATEGORIES", frozenset({"project_references"})
    )

    issues = [
        Issue("project_references", "error", None, "REFERENCES.md is stale"),
    ]

    # The failure surfaces (no false "project_references: 1" success claim).
    with pytest.raises(PermissionError, match="cannot write REFERENCES.md"):
        health._apply_fixes(vault, issues)


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

    tax = vault / "TAXONOMY.md"
    txt = tax.read_text()
    # TAXONOMY.md is locked read-only by create_vault (M32); this helper
    # hand-edits it for test setup, which is exactly the out-of-band write the
    # lock guards, so unlock before writing.
    os.chmod(tax, 0o644)
    tax.write_text(
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


# ===========================================================================
# last_health_check_at refresh (M30 Phase 5)
# ===========================================================================


def _isolate_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect $HOME so the registry lands in tmp; return the home dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def test_health_check_refreshes_timestamp_clean_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful health-check on the ACTIVE vault advances its
    ``last_health_check_at`` even when the vault is clean (exit 0)."""
    from litman.core.correctors import regen
    from litman.core.vault_registry import (
        VaultRegistry,
        add_vault,
        find_active,
        load_registry,
        save_registry,
    )

    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    _isolate_registry(tmp_path, monkeypatch)
    save_registry(add_vault(VaultRegistry(), "main", vault))
    assert find_active(load_registry()).last_health_check_at is None

    _write_paper(vault, "2024_Foo_Bar")
    regen(vault)

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert result.exit_code == 0, result.output

    stamped = find_active(load_registry()).last_health_check_at
    assert stamped is not None


def test_health_check_refreshes_timestamp_dirty_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with findings (exit 1), the timestamp refreshes — the nudge means
    'you haven't looked', not 'your library is clean' (M30 §5)."""
    from litman.core.vault_registry import (
        VaultRegistry,
        add_vault,
        find_active,
        load_registry,
        save_registry,
    )

    _isolate_registry(tmp_path, monkeypatch)
    save_registry(add_vault(VaultRegistry(), "main", vault))

    _write_paper(vault, "A_a_a", related=["GHOST"])  # dangling ref → exit 1

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert result.exit_code == 1, result.output

    stamped = find_active(load_registry()).last_health_check_at
    assert stamped is not None


def test_health_check_unregistered_library_does_not_refresh(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --library override pointing at a vault that is NOT the active
    registered entry must not refresh the active entry, and must not crash."""
    from litman.core.library import create_vault
    from litman.core.vault_registry import (
        VaultRegistry,
        add_vault,
        find_active,
        load_registry,
        save_registry,
    )

    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    _isolate_registry(tmp_path, monkeypatch)
    # Active registered vault is a DIFFERENT directory than the --library target.
    reg_parent = tmp_path / "registered_parent"
    reg_parent.mkdir()
    registered = create_vault(reg_parent)
    save_registry(add_vault(VaultRegistry(), "main", registered))

    from litman.core.correctors import regen

    _write_paper(vault, "2024_Foo_Bar")
    regen(vault)

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert result.exit_code == 0, result.output

    # The active entry (pointing at `registered`, not `vault`) is untouched.
    assert find_active(load_registry()).last_health_check_at is None


# ===========================================================================
# Link-less drive (FAT32 / exFAT sticks, network shares — nowhere to store
# POSIX symlinks or Windows junctions)
#
# The regression these pin: such a drive cannot hold ANY of litman's three
# link kinds, so `views_vs_metadata` + `project_references` used to emit
# ~6 errors per paper that `--fix` could not repair — a 50-paper library showed
# ~300 permanently-red errors and exit 1, forever. That is an environment
# limitation reported as library damage, and it is the thing that makes a new
# user conclude litman is broken and leave.
# ===========================================================================


def _no_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the OS boundary, not litman's helpers — every layer above runs real."""
    from litman.core.portable_link import reset_link_probe_cache

    def boom(self: Path, target: Any, target_is_directory: bool = False) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "symlink_to", boom)
    reset_link_probe_cache()


@pytest.fixture(autouse=True)
def _reset_link_probe_cache() -> Any:
    from litman.core.portable_link import reset_link_probe_cache

    reset_link_probe_cache()
    yield
    reset_link_probe_cache()


def _linkless_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A library built the way it happens on a drive that cannot hold links.

    Order matters and is the whole point: links are dead BEFORE any derived
    state is written, exactly as they are for a real user. So `regen` (what
    `lit add` / `lit link` run) writes INDEX.json and REFERENCES.md — plain files,
    which any filesystem holds — and silently skips every link. Building the
    vault first and disabling links after would leave the links on disk and
    test nothing.
    """
    from litman.core.correctors import regen
    from litman.core.project_link import add_project
    from litman.core.taxonomy import add_taxonomy_values

    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")  # quiet pdf_viewer

    proj = tmp_path / "myproj"
    proj.mkdir()
    add_project(vault, "myproj", proj)
    add_taxonomy_values(vault, "topics", ["peptides"])
    add_taxonomy_values(vault, "methods", ["diffusion"])
    _write_paper(
        vault,
        "2024_Foo_Bar",
        projects=["myproj"],
        topics=["peptides"],
        methods=["diffusion"],
    )

    _no_links(monkeypatch)
    regen(vault)  # the derived write every command performs (INDEX + views + refs)
    return proj


def test_linkless_drive_reports_one_info_and_nothing_else(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a healthy library on a link-less drive is GREEN.

    This paper expects 4 view links + 1 project bridge, none of which can be
    made. Before, that was 5 unfixable errors — and exit 1 — for one paper.
    """
    from litman.core.checks import run_all_checks

    _linkless_vault(vault, tmp_path, monkeypatch)
    issues = run_all_checks(vault, list_papers(vault))

    assert [i for i in issues if i.category == "views_vs_metadata"] == []
    assert [i for i in issues if i.category == "project_references"] == []

    advisories = [i for i in issues if i.category == "links_unsupported"]
    assert len(advisories) == 1
    assert advisories[0].severity == "info"

    # Nothing that gates the exit code — info is advisory by definition.
    assert [i for i in issues if i.severity != "info"] == [], [
        (i.category, i.severity, i.message) for i in issues if i.severity != "info"
    ]


def test_linkless_drive_health_check_cli_is_green(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real command: exit 0, and the user is told why views/ is empty."""
    _linkless_vault(vault, tmp_path, monkeypatch)

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])
    flat = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "folder links cannot be created here" in flat
    assert "your library itself is fine" in flat
    # And NOT a wall of unfixable red.
    assert "but the link is missing" not in flat
    assert "membership implies a litman_reflib" not in flat
    # And never a trip into system settings (the junction tier's whole point).
    assert "Developer Mode" not in result.output
    assert "administrator" not in result.output.lower()


def test_linkless_drive_still_reports_stale_links(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppression is one-directional.

    Removing a link works on any filesystem, so a leftover view is a REAL,
    FIXABLE defect even here. Silencing it too would have been a much worse bug
    than the one being fixed — it would hide damage instead of noise.
    """
    from litman.core.checks import check_views_vs_metadata

    _write_paper(vault, "2024_Foo_Bar", topics=["peptides"])
    stale_bucket = vault / "views" / "by-topic" / "ghost-topic"
    stale_bucket.mkdir(parents=True)
    (stale_bucket / "2024_Foo_Bar").symlink_to("../../../papers/2024_Foo_Bar")

    _no_links(monkeypatch)
    issues = check_views_vs_metadata(vault, list_papers(vault))

    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "has no matching metadata tag" in issues[0].message


def test_vault_can_link_but_project_drive_cannot(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault on an internal drive + a project on an exFAT stick: only the
    project goes quiet.

    Probing once for the whole process would let the vault's verdict speak for a
    filesystem it knows nothing about, and the project's real drift would be
    hidden (or its impossible links reported).
    """
    from litman.core.checks import check_project_references, check_views_vs_metadata
    from litman.core.correctors import regen
    from litman.core.portable_link import (
        _LINK_MECHANISM,
        reset_link_probe_cache,
    )

    proj = tmp_path / "exfat_proj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    _write_paper(vault, "2024_Foo_Bar", projects=["myproj"], topics=["peptides"])
    regen(vault)  # views + bridges all built for real

    # Now pin the verdicts: the vault can, the project cannot.
    reset_link_probe_cache()
    _LINK_MECHANISM[str(vault)] = "symlink"
    _LINK_MECHANISM[str(proj)] = "none"

    # Tear the project's bridge away; the vault's views stay intact.
    for child in (proj / "litman_reflib").iterdir():
        if child.is_symlink():
            child.unlink()

    # The project's missing bridge is suppressed (that drive cannot make it)...
    refs = [
        i
        for i in check_project_references(vault, list_papers(vault))
        if "link for" in i.message
    ]
    assert refs == []
    # ...while the vault's views are still fully checked and still clean.
    assert check_views_vs_metadata(vault, list_papers(vault)) == []
