"""Tests for ``lit health-check`` (M2.8).

Per-check unit tests exercise the pure functions in ``litman.core.checks``.
CLI tests exercise the command via Click ``CliRunner``: rendering, exit
code, and ``--fix`` round-trip.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.commands.health import _CATEGORY_PREVIEW
from litman.core import viewer as viewer_mod
from litman.core.locking import unlock_truth_file
from litman.core.checks import (
    AUTO_FIXABLE_CATEGORIES,
    INBOX_STALE_DAYS,
    apply_autofix,
    check_bidirectional_refs,
    check_code_clone_integrity,
    check_dangling_refs,
    check_dangling_wikilinks,
    check_dirty_metadata,
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
    check_taxonomy_singletons,
    check_taxonomy_similar,
    check_taxonomy_unused,
    check_trash_health,
    run_all_checks,
)
from litman.core.document import list_papers
from litman.core.id import derive_id
from litman.core.ripple import clean_dirty_metadata, drop_dangling_relations
from litman.core.library import create_vault
from litman.core.notes import WIKILINK_REMINDER, discussion_scaffold
from litman.core.portable_link import is_portable_link
from litman.core import locking

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


def _append_metadata(vault: Path, paper_id: str, yaml_text: str) -> None:
    """Append raw YAML lines to a fixture paper's metadata.yaml.

    For keys ``_write_paper`` deliberately does not model: the RETIRED
    paper-level ``priority`` and the variable ``priority-<project>`` keys that
    replaced it (ADR-025). Appending keeps the fixture honest — a real vault
    grew these keys the same way, at the end of the file.
    """
    meta = vault / "papers" / paper_id / "metadata.yaml"
    with meta.open("a", encoding="utf-8") as f:
        f.write(yaml_text)


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


def test_schema_retired_priority_field(vault: Path) -> None:
    """A surviving paper-level `priority` is an error whatever its value —
    the trigger is the key, not the letter (ADR-025)."""
    _write_paper(vault, "2024_Foo_Bar", projects=["pep"])
    _append_metadata(vault, "2024_Foo_Bar", "priority: A\n")
    issues = check_schema(vault, list_papers(vault))
    retired = [i for i in issues if i.category == "retired_priority"]
    assert len(retired) == 1
    assert retired[0].severity == "error"
    assert retired[0].paper_id == "2024_Foo_Bar"
    assert "retired" in retired[0].message
    assert "priority-<project>" in retired[0].message
    assert "--fix" in (retired[0].hint or "")


def test_schema_per_project_priority_out_of_range(vault: Path) -> None:
    """Only the junk value is an error. A null `priority-<other>` is the legal
    "linked, not graded yet" state (ADR-025 decision 5) and stays silent."""
    _write_paper(vault, "2024_Foo_Bar", projects=["pep", "other"])
    _append_metadata(vault, "2024_Foo_Bar", "priority-pep: Z\npriority-other:\n")
    issues = check_schema(vault, list_papers(vault))
    schema = [i for i in issues if i.category == "schema"]
    assert [i.message.split("'")[1] for i in schema] == ["priority-pep"]
    assert schema[0].severity == "error"


def _retired_findings(vault: Path) -> dict[str, Any]:
    return {
        i.paper_id: i
        for i in check_schema(vault, list_papers(vault))
        if i.category == "retired_priority"
    }


def test_schema_retired_priority_discloses_the_lossy_case(vault: Path) -> None:
    """Three cases, and only ONE of them is a warning.

    The loss is disclosed here and nowhere else — the report folds each
    category after five entries, so "the user saw the list first" is not an
    argument that holds. That makes dilution the failure mode to guard: a
    paper with `priority: null` and no projects loses nothing, and `lit add`
    wrote exactly that for every unread paper, so warning about those buries
    the few that really do lose a grade.
    """
    _write_paper(vault, "2024_Linked_Paper", projects=["pep"])
    _append_metadata(vault, "2024_Linked_Paper", "priority: A\n")
    _write_paper(vault, "2024_Loose_Paper", projects=[])
    _append_metadata(vault, "2024_Loose_Paper", "priority: A\n")
    # The common case: added, never triaged, never linked.
    _write_paper(vault, "2024_Unset_Paper", projects=[])
    _append_metadata(vault, "2024_Unset_Paper", "priority:\n")

    found = _retired_findings(vault)
    assert set(found) == {
        "2024_Linked_Paper",
        "2024_Loose_Paper",
        "2024_Unset_Paper",
    }
    linked = found["2024_Linked_Paper"]
    loose = found["2024_Loose_Paper"]
    unset = found["2024_Unset_Paper"]

    # Only the genuinely lossy paper says anything is dropped.
    assert "dropped" in loose.message
    assert "in no project" in loose.message
    # One verdict + one way out, and the way out is the one that KEEPS the
    # grade (link it first), not just the one that deletes it.
    assert "lit link" in (loose.hint or "")

    for issue in (linked, unset):
        assert "dropped" not in issue.message, issue.message
        assert "keep the grade" not in (issue.hint or ""), issue.hint
        assert issue.message != loose.message, "the warning must stand out"

    for issue in (linked, loose, unset):
        assert len(issue.message) < 120, issue.message
        assert len(issue.hint or "") < 120, issue.hint


def test_schema_retired_priority_does_not_warn_an_unset_field(
    vault: Path,
) -> None:
    """The regression this exists for. `lit add` wrote `priority: null`, so on
    a real library most untriaged papers are in this state — 8 of 18 on the
    demo vault. Telling them their value will be dropped is false, and it
    drowns the papers that lose a real grade."""
    _write_paper(vault, "2024_Unset_Paper", projects=[])
    _append_metadata(vault, "2024_Unset_Paper", "priority:\n")

    issue = _retired_findings(vault)["2024_Unset_Paper"]
    assert "dropped" not in issue.message
    assert "no project" not in issue.message
    assert "keep the grade" not in (issue.hint or "")
    # Still an error, still fixable in one Enter — only the wording changes.
    assert issue.severity == "error"
    assert "--fix" in (issue.hint or "")


def test_schema_retired_priority_still_warns_a_real_loss(vault: Path) -> None:
    """The paired control. Without it, a build that never warns at all would
    satisfy the test above."""
    _write_paper(vault, "2024_Loose_Paper", projects=[])
    _append_metadata(vault, "2024_Loose_Paper", "priority: C\n")

    issue = _retired_findings(vault)["2024_Loose_Paper"]
    assert "dropped" in issue.message
    assert "keep the grade" in (issue.hint or "")


def test_schema_retired_priority_warning_tracks_the_migration(
    vault: Path,
) -> None:
    """The warning and the behaviour must not disagree: a paper is told it
    loses something exactly when `migrate_retired_priority` would drop a value
    without copying it anywhere."""
    from litman.core.ripple import migrate_retired_priority

    for name, projects, line in (
        ("2024_Linked_Graded", ["pep"], "priority: A\n"),
        ("2024_Linked_Unset", ["pep"], "priority:\n"),
        ("2024_Loose_Graded", [], "priority: C\n"),
        ("2024_Loose_Unset", [], "priority:\n"),
    ):
        _write_paper(vault, name, projects=projects)
        _append_metadata(vault, name, line)

    warned = {
        pid for pid, i in _retired_findings(vault).items() if "dropped" in i.message
    }
    before = {p["id"]: dict(p) for p in list_papers(vault)}
    migrate_retired_priority(vault)
    after = {p["id"]: dict(p) for p in list_papers(vault)}

    lost = {
        pid
        for pid, old in before.items()
        if old.get("priority") is not None
        and not any(k.startswith("priority-") for k in after[pid])
    }
    assert warned == lost == {"2024_Loose_Graded"}


def test_schema_per_project_priority_in_range_is_clean(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", projects=["binder-design"])
    # A hyphenated project name is the normal case — the key is never split
    # on "-", the prefix is stripped.
    _append_metadata(vault, "2024_Foo_Bar", "priority-binder-design: A\n")
    assert check_schema(vault, list_papers(vault)) == []


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
    assert mismatch[0].category == "unfinished_rename"
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


# --- unfinished_rename: a folder whose name lags its metadata id -----------


def _half_renamed(vault: Path) -> None:
    """A clean `lit rename`, then its folder moved back by hand — the state an
    older `lit rename` left when the folder could not be renamed."""
    from litman.core.correctors import regen

    _write_paper(
        vault, "2024_Foo_Bar", related=["2024_Other"],
        notes="# Foo Bar\n\nSee [[2024_Foo_Bar]].\n",
    )
    _write_paper(
        vault, "2024_Other", related=["2024_Foo_Bar"],
        notes="Compare with [[2024_Foo_Bar]].\n",
    )
    regen(vault)
    result = CliRunner().invoke(
        cli, ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    os.rename(vault / "papers" / "2024_Foo_Baz", vault / "papers" / "2024_Foo_Bar")


def test_fix_renames_a_half_renamed_folder_and_the_library_agrees(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding's own hint, run as written, leaves nothing to report."""
    monkeypatch.setattr(viewer_mod, "detect_platform_viewer", lambda: "open")
    _half_renamed(vault)
    (issue,) = [
        i for i in check_paper_dir_validity(vault, [])
        if "metadata id" in i.message
    ]
    assert issue.category == "unfinished_rename"
    assert issue.severity == "error"
    assert issue.paper_id == "2024_Foo_Bar"
    assert issue.message == (
        "directory name '2024_Foo_Bar' != metadata id '2024_Foo_Baz' "
        "(likely a half-finished rename)"
    )
    assert issue.hint == (
        "`lit health-check --fix` renames the folder to papers/2024_Foo_Baz/"
    )
    command = shlex.split((issue.hint or "").split("`")[1])
    assert command[0] == "lit"

    fixed = CliRunner().invoke(cli, [*command[1:], "--library", str(vault)])

    assert fixed.exit_code == 0, fixed.output
    assert "unfinished_rename: cleaned 1 item" in fixed.output
    again = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])
    assert again.exit_code == 0, again.output
    assert "All checks passed" in again.output

    folder = vault / "papers" / "2024_Foo_Baz"
    assert not (vault / "papers" / "2024_Foo_Bar").exists()
    assert (folder / "paper.pdf").is_file()
    assert _meta(vault, "2024_Foo_Baz")["id"] == "2024_Foo_Baz"
    assert _meta(vault, "2024_Other")["related"] == ["2024_Foo_Baz"]
    assert "[[2024_Foo_Baz]]" in (
        vault / "papers" / "2024_Other" / "notes.md"
    ).read_text(encoding="utf-8")
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert sorted(p["id"] for p in index["papers"]) == ["2024_Foo_Baz", "2024_Other"]
    link = vault / "views" / "by-status" / "deep-read" / "2024_Foo_Baz"
    assert is_portable_link(link)
    assert link.resolve() == folder.resolve()
    assert list((vault / ".litman-staging").iterdir()) == []


@pytest.mark.parametrize(
    ("case", "hint"),
    [
        (
            "target exists",
            "papers/2024_Foo_Baz/ exists too: compare the two folders and keep "
            "one by hand",
        ),
        (
            "case only",
            "the two differ only in case: rename the folder to "
            "papers/2024_Foo_Baz/ by hand",
        ),
        (
            "invalid id",
            "'2024 Foo Baz' is not a valid paper id: set id in metadata.yaml "
            "back to '2024_Foo_Bar' by hand",
        ),
        (
            "case-fold clash",
            "papers/2024_foo_baz/ differs from '2024_Foo_Baz' only in case: keep "
            "one of the two by hand",
        ),
    ],
)
def test_a_mismatch_fix_cannot_settle_stays_for_a_person(
    vault: Path, case: str, hint: str
) -> None:
    """Same verdict on every platform: where the filesystem ignores case,
    papers/2024_Foo_Baz "exists" in the last two cases as well, and the case
    questions are asked before the disk is."""
    from litman.core.checks import finish_unfinished_renames

    folder = "2024_foo_baz" if case == "case only" else "2024_Foo_Bar"
    meta_id = "2024 Foo Baz" if case == "invalid id" else "2024_Foo_Baz"
    _write_paper(vault, folder, override_id=meta_id)
    if case == "target exists":
        _write_paper(vault, "2024_Foo_Baz")
    if case == "case-fold clash":
        _write_paper(vault, "2024_foo_baz")

    (issue,) = [
        i for i in check_paper_dir_validity(vault, [])
        if "metadata id" in i.message
    ]
    assert issue.category == "paper_dir_validity"
    assert issue.severity == "error"
    assert issue.paper_id == folder
    assert issue.hint == hint

    def tree() -> list[tuple[str, bytes]]:
        return [
            (p.relative_to(vault).as_posix(), p.read_bytes())
            for p in sorted((vault / "papers").rglob("*"))
            if p.is_file()
        ]

    before = tree()
    # The mover reads the disk itself and asks the same question.
    assert finish_unfinished_renames(vault) == (0, [])
    assert tree() == before


def test_fix_whose_move_fails_leaves_the_folder_and_says_why(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core import atomic as atomic_mod

    _write_paper(vault, "2024_Foo_Bar", override_id="2024_Foo_Baz")

    def refuse(src: Path, dst: Path) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(atomic_mod, "_rename_dir", refuse)
    result = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )

    assert result.exit_code == 1
    assert (
        "left papers/2024_Foo_Bar/: Access is denied; "
        "close any file open from it and run --fix again"
    ) in " ".join(result.output.split())
    assert (vault / "papers" / "2024_Foo_Bar" / "paper.pdf").is_file()
    assert not (vault / "papers" / "2024_Foo_Baz").exists()
    assert list((vault / ".litman-staging").iterdir()) == []


def test_fix_renames_the_folder_before_any_arm_looks_a_paper_up_by_id(
    vault: Path,
) -> None:
    """Until the folder carries its metadata id, an arm that finds a paper by
    id sees it as missing: the discussion log the scaffold arm would create
    would never be created, and a later run would still report it. Renaming
    first lets every arm act on the moved folder in the same pass."""
    from litman.commands.health import _apply_fixes

    _write_paper(
        vault, "2024_Foo_Bar", override_id="2024_Foo_Baz",
        title="Foo\n   bar", no_discussion=True,
    )
    issues = run_all_checks(vault, list_papers(vault))
    assert {"unfinished_rename", "discussion_scaffold", "dirty_metadata"} <= {
        i.category for i in issues
    }

    counts = _apply_fixes(vault, issues)

    assert counts["unfinished_rename"] == 1
    assert sorted(p.name for p in (vault / "papers").iterdir()) == ["2024_Foo_Baz"]
    folder = vault / "papers" / "2024_Foo_Baz"
    assert (folder / "paper.pdf").is_file()
    assert (folder / "discussion.md").is_file()
    assert _meta(vault, "2024_Foo_Baz")["title"] == "Foo bar"
    after = {i.category for i in run_all_checks(vault, list_papers(vault))}
    assert not after & {"unfinished_rename", "discussion_scaffold", "dirty_metadata"}


def test_fix_renames_the_folder_before_the_derived_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the folder carries its metadata id, the rebuild links views and
    project hubs to a folder that is not there — on Windows that fails and
    reads as "this filesystem cannot hold folder links". So the folder is
    renamed first, and the library is rebuilt once."""
    from litman.commands import health as health_mod

    monkeypatch.setattr(viewer_mod, "detect_platform_viewer", lambda: "open")
    _half_renamed(vault)
    real_regen = health_mod.regen
    calls: list[bool] = []

    def regen_after_the_rename(vault_arg: Path, issues: Any = None) -> Any:
        calls.append((vault / "papers" / "2024_Foo_Baz").is_dir())
        return real_regen(vault_arg, issues)

    monkeypatch.setattr(health_mod, "regen", regen_after_the_rename)
    result = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    assert calls == [True]
    again = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])
    assert again.exit_code == 0, again.output
    assert "All checks passed" in again.output


def _derived_files_name_the_folder(vault: Path) -> None:
    """Half-renamed with INDEX.json and views/ still built for the folder name
    — an id edited by hand, or a rename that stopped before its rebuild."""
    from litman.core.correctors import regen

    _write_paper(vault, "2024_Foo_Bar")
    regen(vault)
    _write_paper(vault, "2024_Foo_Bar", override_id="2024_Foo_Baz")


def test_a_moved_folder_is_rebuilt_even_with_no_derived_finding(
    vault: Path,
) -> None:
    """The views still point at the old folder name after the move, whether or
    not the report happened to name them."""
    from litman.commands.health import _apply_fixes

    _derived_files_name_the_folder(vault)
    only_rename = [
        i for i in run_all_checks(vault, list_papers(vault))
        if i.category == "unfinished_rename"
    ]

    counts = _apply_fixes(vault, only_rename)

    assert counts["unfinished_rename"] == 1
    link = vault / "views" / "by-status" / "deep-read" / "2024_Foo_Baz"
    assert is_portable_link(link)
    assert link.resolve() == (vault / "papers" / "2024_Foo_Baz").resolve()
    after = {i.category for i in run_all_checks(vault, list_papers(vault))}
    assert not after & {"unfinished_rename", "index_vs_disk", "views_vs_metadata"}


def _mismatch(vault: Path, folder: str) -> Any:
    (issue,) = [
        i for i in check_paper_dir_validity(vault, list_papers(vault))
        if i.paper_id == folder and "metadata id" in i.message
    ]
    return issue


@pytest.mark.parametrize(
    ("kind", "where"),
    [
        ("relation", "2021_C_x's related"),
        ("wikilink", "papers/2021_C_x/notes.md"),
        ("repo", "codes/myrepo/repo-meta.yaml"),
    ],
)
def test_a_folder_the_library_still_names_is_left_for_a_person(
    vault: Path, kind: str, where: str
) -> None:
    """An id edited by hand, not a rename that stopped halfway: moving the
    folder would leave the link that still names it dangling."""
    from litman.core.checks import finish_unfinished_renames

    _write_paper(
        vault, "2020_A_x", override_id="2020_B_x",
        code_clones=["myrepo"] if kind == "repo" else [],
    )
    _write_paper(
        vault, "2021_C_x",
        related=["2020_A_x"] if kind == "relation" else [],
        notes="See [[2020_A_x]].\n" if kind == "wikilink" else None,
    )
    if kind == "repo":
        _write_repo_meta(vault, "myrepo", ["2020_A_x"])

    issue = _mismatch(vault, "2020_A_x")

    assert issue.category == "paper_dir_validity"
    assert issue.hint == (
        f"{where} still names '2020_A_x': set id in metadata.yaml back to "
        "'2020_A_x' by hand, then `lit rename 2020_A_x 2020_B_x`"
    )
    assert finish_unfinished_renames(vault) == (0, [])
    assert (vault / "papers" / "2020_A_x").is_dir()


def test_fix_keeps_a_link_to_a_folder_whose_id_was_edited_by_hand(
    vault: Path,
) -> None:
    """Renamed, A's folder would leave C's edge to it dangling, and the same
    --fix drops dangling edges (D's is a real one)."""
    _write_paper(vault, "2020_A_x", override_id="2020_B_x", related=["2021_C_x"])
    _write_paper(vault, "2021_C_x", related=["2020_A_x"])
    _write_paper(vault, "2022_D_x", related=["2019_Ghost_x"])

    for _ in range(2):
        CliRunner().invoke(cli, ["health-check", "--fix", "--library", str(vault)])

    assert _meta(vault, "2021_C_x")["related"] == ["2020_A_x"]
    assert _meta(vault, "2022_D_x")["related"] == []
    assert sorted(p.name for p in (vault / "papers").iterdir()) == [
        "2020_A_x", "2021_C_x", "2022_D_x"
    ]
    assert _mismatch(vault, "2020_A_x").category == "paper_dir_validity"


def test_a_folder_a_leftover_commit_moves_is_left_to_that_commit(
    vault: Path,
) -> None:
    """A rename killed after its commit point: the folder already carries the
    new name, its metadata still the old id, and the commit's own recovery is
    what finishes it. --fix must not move the folder back."""
    from litman.core.atomic import StagedWrite
    from litman.core.checks import finish_unfinished_renames

    _write_paper(vault, "2020_Old_A")
    stage = StagedWrite(vault, op_id="rename-2020_Old_A-to-2024_New_C")
    stage.__enter__()
    stage.move_dir("papers/2020_Old_A", "papers/2024_New_C")
    stage.write_text("papers/2024_New_C/metadata.yaml", "id: 2024_New_C\n")
    stage._fsync_staged_files()
    stage._write_manifest()
    stage._move_dirs()
    stage._write_sentinel()

    issue = _mismatch(vault, "2024_New_C")

    assert issue.category == "paper_dir_validity"
    assert issue.hint == (
        ".litman-staging/rename-2020_Old_A-to-2024_New_C/ still moves this "
        "folder: settle that finding first"
    )
    assert finish_unfinished_renames(vault) == (0, [])
    assert (vault / "papers" / "2024_New_C").is_dir()
    assert not (vault / "papers" / "2020_Old_A").exists()


def test_a_fix_rename_whose_commit_is_still_kept_is_left_with_the_reason(
    vault: Path,
) -> None:
    """A kept op under the id the --fix rename would use: no traceback, the
    folder is left and the reason named."""
    from litman.core.checks import finish_unfinished_renames

    _write_paper(vault, "2024_Foo_Bar", override_id="2024_Foo_Baz")
    op_dir = vault / ".litman-staging" / "fix-rename-2024_Foo_Bar-to-2024_Foo_Baz"
    op_dir.mkdir()
    (op_dir / "MANIFEST.json").write_text(
        json.dumps({"op_id": op_dir.name, "files": ["lost.md"]}), encoding="utf-8"
    )
    (op_dir / "COMMITTED").write_bytes(b"")

    assert finish_unfinished_renames(vault) == (
        0,
        [(
            "2024_Foo_Bar",
            "2024_Foo_Baz",
            "its earlier --fix is kept in .litman-staging/; settle that finding first",
        )],
    )
    assert (vault / "papers" / "2024_Foo_Bar").is_dir()


def test_a_half_renamed_folder_reads_no_other_metadata_without_a_twin(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether another folder claims the id is counted from the paper list;
    the folders are read only to name one."""
    from litman.core import checks as checks_mod

    _write_paper(vault, "2020_A_x", override_id="2020_B_x")
    _write_paper(vault, "2021_C_x")
    _write_paper(vault, "2022_D_x")

    def no_scan(paper_dir: Path) -> object:
        raise AssertionError(f"read {paper_dir} with no twin to name")

    monkeypatch.setattr(checks_mod, "_declared_id", no_scan)

    assert _mismatch(vault, "2020_A_x").category == "unfinished_rename"


def test_two_folders_claiming_one_id_are_both_left_for_a_person(
    vault: Path,
) -> None:
    from litman.core.checks import finish_unfinished_renames

    _write_paper(vault, "2020_A_x", override_id="2020_B_x")
    _write_paper(vault, "2020_C_x", override_id="2020_B_x")

    assert _mismatch(vault, "2020_A_x").hint == (
        "papers/2020_A_x/ and papers/2020_C_x/ both claim '2020_B_x': decide "
        "by hand which one keeps it"
    )
    assert _mismatch(vault, "2020_C_x").hint == (
        "papers/2020_C_x/ and papers/2020_A_x/ both claim '2020_B_x': decide "
        "by hand which one keeps it"
    )
    assert finish_unfinished_renames(vault) == (0, [])
    assert sorted(p.name for p in (vault / "papers").iterdir()) == [
        "2020_A_x", "2020_C_x"
    ]


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

    locking.rmtree(vault / "papers" / "2024_Foo_Bar")
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


# --- priority_orphan (ADR-025) ----------------------------------------------


def test_priority_orphan_clean(vault: Path) -> None:
    from litman.core.checks import check_priority_orphan

    paper_dir = vault / "papers" / "2024_Foo_Bar"
    paper_dir.mkdir(parents=True)
    # Hyphenated project name: the key is the prefix + the WHOLE name.
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_Foo_Bar\nprojects:\n  - binder-design\n"
        "priority-binder-design: A\n",
        encoding="utf-8",
    )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF stub\n")
    assert check_priority_orphan(vault, list_papers(vault)) == []


def test_priority_orphan_detected_report_only(vault: Path) -> None:
    from litman.core.checks import check_priority_orphan

    paper_dir = vault / "papers" / "2024_Foo_Bar"
    paper_dir.mkdir(parents=True)
    # priority-pep present but projects does NOT contain pep -> orphan.
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_Foo_Bar\nprojects: []\npriority-pep: B\n",
        encoding="utf-8",
    )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF stub\n")
    issues = check_priority_orphan(vault, list_papers(vault))
    assert len(issues) == 1
    assert issues[0].category == "priority_orphan"
    assert issues[0].severity == "warning"  # report-only, never auto-deleted
    assert issues[0].paper_id == "2024_Foo_Bar"
    assert "priority-pep" in issues[0].message
    assert "lit unlink" in (issues[0].hint or "")


def test_priority_and_relevance_orphans_stay_separate_categories(
    vault: Path,
) -> None:
    """One shared body, two findings: the repair prose differs (authored text
    vs one letter), so they must not collapse into one category."""
    from litman.core.checks import check_priority_orphan, check_relevance_orphan

    paper_dir = vault / "papers" / "2024_Foo_Bar"
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_Foo_Bar\nprojects: []\n"
        "priority-pep: B\nrelevance-pep: stale note\n",
        encoding="utf-8",
    )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF stub\n")
    papers = list_papers(vault)
    assert [i.category for i in check_priority_orphan(vault, papers)] == [
        "priority_orphan"
    ]
    rel = check_relevance_orphan(vault, papers)
    assert [i.category for i in rel] == ["relevance_orphan"]
    assert "never auto-deleted" in (rel[0].hint or "")


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


def _expand_link_to_copy(link: Path, source: Path) -> None:
    """Do to one link what a copy tool does to a whole project directory."""
    import shutil

    from litman.core.portable_link import remove_link_if_present

    assert remove_link_if_present(link)
    shutil.copytree(source, link)


def test_folder_copy_reported_as_copy_not_missing(
    vault: Path, tmp_path: Path
) -> None:
    """The position is occupied, not empty. "missing" sent the user hunting
    for something that was never lost — and ``--fix`` into a wall it could
    not get past."""
    from litman.core.checks import check_project_references
    from litman.core.correctors import regen

    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    _write_paper(vault, "2024_Foo_Bar", projects=["myproj"])
    regen(vault)
    link = proj / "litman_reflib" / "2024_Foo_Bar"
    _expand_link_to_copy(link, vault / "papers" / "2024_Foo_Bar")

    issues = check_project_references(vault, list_papers(vault))

    assert len(issues) == 1
    assert issues[0].category == "project_references"
    assert issues[0].severity == "error"
    assert issues[0].paper_id == "2024_Foo_Bar"
    assert "is a folder copy, not a litman link" in issues[0].message
    assert "missing" not in issues[0].message
    assert issues[0].hint is not None and ".trash/" in issues[0].hint

    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    assert "project_references" in result.output
    assert is_portable_link(link)
    assert check_project_references(vault, list_papers(vault)) == []


def test_fix_reports_replaced_and_kept_folder_counts(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--fix`` says what it did with the folders — the two outcomes read
    differently, and the one it kept is only useful with its path."""
    from litman.core.correctors import regen

    # Wide enough that Rich does not fold the .trash/ path mid-word.
    monkeypatch.setenv("COLUMNS", "400")
    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    ids = ["2024_A_One", "2024_B_Two", "2024_C_Three"]
    for pid in ids:
        _write_paper(vault, pid, projects=["myproj"])
    regen(vault)
    for pid in ids:
        _expand_link_to_copy(
            proj / "litman_reflib" / pid, vault / "papers" / pid
        )
    (proj / "litman_reflib" / "2024_C_Three" / "notes.md").write_text(
        "written on the other machine\n", encoding="utf-8"
    )

    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    flat = " ".join(result.output.split())

    assert "replaced 2 folder copies with links" in flat
    assert "kept 1 folder that does not match the vault:" in flat
    kept_root = (
        vault / ".trash" / "replaced-folders" / "myproj" / "litman_reflib"
    )
    kept = sorted(kept_root.iterdir())
    assert len(kept) == 1
    assert str(kept[0]) in flat
    assert (kept[0] / "notes.md").read_text(encoding="utf-8") == (
        "written on the other machine\n"
    )
    for pid in ids:
        assert is_portable_link(proj / "litman_reflib" / pid)


def test_fix_keeps_the_kept_folder_path_copyable_at_80_columns(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kept folder's path is only useful if it survives a copy-paste.

    At the default 80 columns it is far wider than the console, and rich's own
    wrapping puts a real newline inside it — the pasted path then points
    nowhere. The line goes out soft-wrapped instead, leaving the folding to
    the terminal. The whole point here is to render at a width the path cannot
    fit into.
    """
    from rich.console import Console

    from litman.commands import health as health_cmd
    from litman.core.correctors import regen

    # Pinned, not set through COLUMNS: rich caches a console's width the first
    # time anything reads it, and `health`'s console is a module global that
    # earlier tests in this file have already read. Patching the console makes
    # the width a property of this test instead of of the run order.
    monkeypatch.setattr(health_cmd, "console", Console(width=80))
    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    pid = "2024_C_Three"
    _write_paper(vault, pid, projects=["myproj"])
    regen(vault)
    _expand_link_to_copy(proj / "litman_reflib" / pid, vault / "papers" / pid)
    (proj / "litman_reflib" / pid / "notes.md").write_text(
        "written on the other machine\n", encoding="utf-8"
    )

    result = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )

    kept_root = (
        vault / ".trash" / "replaced-folders" / "myproj" / "litman_reflib"
    )
    kept = sorted(kept_root.iterdir())
    assert len(kept) == 1
    # Control: the assertion below proves nothing about wrapping unless the
    # path is genuinely wider than the console it was printed to.
    assert len(str(kept[0])) > 80
    # NOT flattened, unlike the sibling test: a fold lands a real newline
    # inside the path and this substring stops existing.
    assert str(kept[0]) in result.output


def test_fix_reports_a_single_replacement_in_the_singular(
    vault: Path, tmp_path: Path
) -> None:
    from litman.core.correctors import regen

    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    _write_paper(vault, "2024_A_One", projects=["myproj"])
    regen(vault)
    _expand_link_to_copy(
        proj / "litman_reflib" / "2024_A_One", vault / "papers" / "2024_A_One"
    )

    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    flat = " ".join(result.output.split())

    assert "replaced 1 folder copy with a link" in flat
    assert "kept 1 folder" not in flat  # nothing differed, nothing preserved


def test_one_unclearable_folder_does_not_abort_the_whole_fix(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked folder costs its own position, not the run.

    Before this, an OSError from the settle escaped through regen into
    ``health_check_cmd``, so the post-fix re-check, the summary and every
    other klass-A category never ran — a stack trace where the old code had
    printed one warning and finished.
    """
    import litman.core.project_link as project_link
    from litman.core.correctors import regen
    from litman.core.locking import rmtree as real_rmtree

    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    _write_paper(vault, "2024_A_One", projects=["myproj"], no_discussion=True)
    _write_paper(vault, "2024_B_Two", projects=["myproj"], no_discussion=True)
    regen(vault)
    for pid in ("2024_A_One", "2024_B_Two"):
        _expand_link_to_copy(proj / "litman_reflib" / pid, vault / "papers" / pid)

    def flaky_rmtree(path: Path, **kwargs: Any) -> None:
        if Path(path).name == "2024_A_One":
            raise OSError(32, "used by another process (mocked)")
        real_rmtree(path, **kwargs)

    monkeypatch.setattr(project_link, "rmtree", flaky_rmtree)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )

    assert result.exception is None or isinstance(result.exception, SystemExit)
    flat = " ".join(result.output.split())
    assert "Summary:" in flat  # the run reached the end
    assert "discussion_scaffold: cleaned 2 items" in flat  # other repairs ran
    assert "replaced 1 folder copy with a link" in flat
    stuck = proj / "litman_reflib" / "2024_A_One"
    assert stuck.is_dir() and not is_portable_link(stuck)
    assert (stuck / "metadata.yaml").is_file()
    assert is_portable_link(proj / "litman_reflib" / "2024_B_Two")


def test_folder_copy_outside_membership_reported_as_stale(
    vault: Path, tmp_path: Path
) -> None:
    """A folder for a paper this project no longer holds. The ``extra`` arm
    only ever counted links, so nothing saw it."""
    from litman.core.checks import check_project_references
    from litman.core.correctors import regen

    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    _write_paper(vault, "2024_Foo_Bar", projects=["myproj"])
    regen(vault)
    orphan = proj / "litman_reflib" / "2019_Old_Paper"
    orphan.mkdir()
    (orphan / "notes.md").write_text("stale\n", encoding="utf-8")

    issues = check_project_references(vault, list_papers(vault))

    assert len(issues) == 1
    assert issues[0].paper_id is None
    assert "is a stale folder copy, not a litman link" in issues[0].message

    runner = CliRunner()
    runner.invoke(cli, ["health-check", "--fix", "--library", str(vault)])
    assert not orphan.exists()
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
    assert is_portable_link(link) and not link.exists()  # dangling, name intact

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
    assert is_portable_link(link)
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


# --- dangling_refs: --fix removes them --------------------------------------
#
# A dangling edge names a paper no longer in the library. A reverse one
# (extended-by / contradicted-by) had no way out at all: `lit modify` refuses
# to name a reverse field, and the forward edge it mirrors lived on the paper
# that is gone. `--fix` now drops every dangling edge, by the same definition
# the check reports by.


def _meta(vault: Path, folder: str) -> dict[str, Any]:
    return YAML(typ="safe").load(
        (vault / "papers" / folder / "metadata.yaml").read_text(encoding="utf-8")
    )


def test_dangling_refs_hint_is_a_command_that_clears_the_finding(
    vault: Path,
) -> None:
    """The finding's own hint, run as written, removes the edge."""
    _write_paper(vault, "B_b_b", extended_by=["A_a_a"])  # A was deleted
    (issue,) = check_dangling_refs(vault, list_papers(vault))
    assert "missing paper 'A_a_a'" in issue.message
    command = (issue.hint or "").split("`")[1]
    argv = shlex.split(command)
    assert argv[0] == "lit", issue.hint

    result = CliRunner().invoke(cli, [*argv[1:], "--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert _meta(vault, "B_b_b")["extended-by"] == []
    assert check_dangling_refs(vault, list_papers(vault)) == []


def test_fix_drops_every_dangling_edge_and_nothing_else(vault: Path) -> None:
    _write_paper(vault, "C_c_c", related=["B_b_b"])
    _write_paper(
        vault,
        "B_b_b",
        related=["C_c_c"],
        extends=["GHOST_one"],
        extended_by=["GHOST_two"],
        contradicted_by=["GHOST_three", "C_c_c"],
    )
    issues = run_all_checks(vault, list_papers(vault))

    counts = apply_autofix(vault, issues)

    assert counts["dangling_refs"] == 3
    meta = _meta(vault, "B_b_b")
    assert meta["related"] == ["C_c_c"]
    assert meta["extends"] == []
    assert meta["extended-by"] == []
    assert meta["contradicted-by"] == ["C_c_c"]  # live, if one-sided: kept
    # The paper's relations changed, so its audit stamp moved — and INDEX,
    # which carries the stamp, moved with it in the same commit.
    assert meta["updated-at"] != "2026-04-28T10:00:00+02:00"
    after = run_all_checks(vault, list_papers(vault))
    assert not [i for i in after if i.category in {"dangling_refs", "index_vs_disk"}]
    # The one-sided live edge is a different finding, still reported.
    assert [i.paper_id for i in after if i.category == "bidirectional_refs"] == [
        "B_b_b"
    ]
    assert drop_dangling_relations(vault) == 0  # idempotent


def _trash_one_side_of(vault: Path) -> dict[str, Any]:
    """B keeps `extended-by: A` with A in the trash; returns A as it was.

    The pair was already one-sided, so `lit rm A` could not see B's half: it
    stays behind pointing into the trash.
    """
    _write_paper(vault, "A_a_a")  # A no longer names B
    _write_paper(vault, "B_b_b", extended_by=["A_a_a"])
    before = _meta(vault, "A_a_a")
    rm = CliRunner().invoke(cli, ["rm", "A_a_a", "-y", "--library", str(vault)])
    assert rm.exit_code == 0, rm.output
    assert _meta(vault, "B_b_b")["extended-by"] == ["A_a_a"]
    return before


def test_fix_treats_a_trashed_paper_as_gone(vault: Path) -> None:
    """--fix removes B's half; A itself comes back from the trash whole."""
    a_before = _trash_one_side_of(vault)
    runner = CliRunner()

    fix = runner.invoke(cli, ["health-check", "--fix", "--library", str(vault)])

    assert "dangling_refs: cleaned 1 item" in fix.output, fix.output
    assert _meta(vault, "B_b_b")["extended-by"] == []
    restore = runner.invoke(
        cli, ["trash", "restore", "A_a_a", "--library", str(vault)]
    )
    assert restore.exit_code == 0, restore.output
    assert _meta(vault, "A_a_a") == a_before


@pytest.mark.parametrize("way_out", [0, 1], ids=["restore", "fix"])
def test_an_edge_into_the_trash_says_so_and_names_both_ways_out(
    vault: Path, way_out: int
) -> None:
    """The one lossy finding names itself: each command in its hint, run as
    written, clears it — restoring keeps the link, --fix drops it."""
    _trash_one_side_of(vault)
    (issue,) = check_dangling_refs(vault, list_papers(vault))
    assert issue.message == "'extended-by' references 'A_a_a', which is in the trash"
    commands = (issue.hint or "").split("`")[1::2]
    assert len(commands) == 2, issue.hint
    argv = shlex.split(commands[way_out])
    assert argv[0] == "lit", issue.hint

    result = CliRunner().invoke(cli, [*argv[1:], "--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert check_dangling_refs(vault, list_papers(vault)) == []
    kept = ["A_a_a"] if way_out == 0 else []
    assert _meta(vault, "B_b_b")["extended-by"] == kept


def test_an_edge_to_a_folder_that_cannot_be_read_is_not_dangling(
    vault: Path,
) -> None:
    """A paper whose metadata.yaml is broken right now is not gone: neither
    the check nor the fix may treat an edge to it as dangling."""
    _write_paper(vault, "B_b_b", extended_by=["A_a_a"])
    broken = vault / "papers" / "A_a_a"
    broken.mkdir()
    (broken / "metadata.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    assert "A_a_a" not in {p["id"] for p in list_papers(vault)}

    assert check_dangling_refs(vault, list_papers(vault)) == []
    assert drop_dangling_relations(vault) == 0
    assert _meta(vault, "B_b_b")["extended-by"] == ["A_a_a"]


def test_fix_rewrites_each_folder_of_a_conflicted_copy(vault: Path) -> None:
    """Two folders declaring one id are both rewritten, and counted as two."""
    _write_paper(vault, "A_a_a", related=["GHOST"])
    _write_paper(vault, "A_a_a_conflict", override_id="A_a_a", related=["GHOST"])
    assert len(check_dangling_refs(vault, list_papers(vault))) == 2

    assert drop_dangling_relations(vault) == 2

    assert _meta(vault, "A_a_a")["related"] == []
    assert _meta(vault, "A_a_a_conflict")["related"] == []


def test_a_ref_that_walks_out_of_papers_is_still_dangling(vault: Path) -> None:
    """`..` names the folder above papers/, which exists: the disk probe for a
    case-insensitive match must not take it for a paper."""
    _write_paper(vault, "B_b_b", related=[".."])

    assert len(check_dangling_refs(vault, list_papers(vault))) == 1
    assert drop_dangling_relations(vault) == 1
    assert _meta(vault, "B_b_b")["related"] == []


def _fs_ignores_case(where: Path) -> bool:
    """Probed, not read off sys.platform: Windows and default macOS ignore
    case, Linux and a case-sensitive APFS volume do not."""
    probe = where / "LitmanCaseProbe"
    probe.mkdir()
    try:
        return (where / "LITMANCASEPROBE").is_dir()
    finally:
        probe.rmdir()


def test_a_case_variant_the_disk_resolves_is_not_dangling(
    vault: Path, tmp_path: Path
) -> None:
    """Where the filesystem ignores case, `extends: a_a_a` opens A_a_a's
    folder — the double-write paired it — so the edge is live: neither the
    check nor the fix may take it for a dangling one."""
    if not _fs_ignores_case(tmp_path):
        pytest.skip(
            "Only where the filesystem itself ignores case does a case "
            "variant name the paper's folder; here it names nothing."
        )
    _write_paper(vault, "A_a_a", extended_by=["X_x_x"])
    _write_paper(vault, "X_x_x", extends=["a_a_a"])

    assert check_dangling_refs(vault, list_papers(vault)) == []
    assert drop_dangling_relations(vault) == 0
    assert _meta(vault, "X_x_x")["extends"] == ["a_a_a"]


def test_a_hand_edited_scalar_relation_is_reported_not_guessed(
    vault: Path,
) -> None:
    """`related: GHOST` (no list) is not read as an edge at all — as one id or
    as five characters. The file is reported, and `--fix` leaves it alone."""
    _write_paper(vault, "A_a_a", related="GHOST")
    meta_file = vault / "papers" / "A_a_a" / "metadata.yaml"
    before = meta_file.read_bytes()

    assert check_dangling_refs(vault, list_papers(vault)) == []
    (issue,) = [
        i for i in check_paper_dir_validity(vault, [])
        if "is not a list" in i.message
    ]
    assert issue.severity == "error"
    assert issue.message == (
        "papers/A_a_a/metadata.yaml: 'related' is not a list — paper invisible "
        "to all checks/INDEX"
    )
    assert issue.hint.startswith("write it as `related: ['GHOST']`")
    assert drop_dangling_relations(vault) == 0
    assert meta_file.read_bytes() == before


def test_every_field_that_is_not_a_list_gets_its_own_finding(vault: Path) -> None:
    """One pass names them all, so fixing the file takes one round."""
    _write_paper(vault, "A_a_a", topics="peptide", methods=42)
    fields = sorted(
        i.message.split("'")[1] for i in check_paper_dir_validity(vault, [])
        if "is not a list" in i.message
    )
    assert fields == ["methods", "topics"]


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


@pytest.mark.parametrize("a_returns_it", [True, False])
def test_bidirectional_makes_no_claim_about_a_scalar_relation(
    vault: Path, a_returns_it: bool
) -> None:
    """B's hand-edited `related: A_a_a` is neither a pairing nor a missing
    one: B is reported as a broken file, and nothing is said about A."""
    _write_paper(vault, "A_a_a", related=["B_b_b"] if a_returns_it else [])
    _write_paper(vault, "B_b_b", related="A_a_a")
    assert check_bidirectional_refs(vault, list_papers(vault)) == []
    assert any(
        i.paper_id == "B_b_b" and "'related' is not a list" in i.message
        for i in check_paper_dir_validity(vault, [])
    )


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
    # TAXONOMY.md is a locked TRUTH file, and Windows refuses os.unlink on a
    # read-only one (WinError 5). Clearing the bit through litman's own helper
    # is what every production deletion path does.
    unlock_truth_file(vault / "TAXONOMY.md")
    (vault / "TAXONOMY.md").unlink()
    issues = check_taxonomy_drift(vault, [])
    assert len(issues) == 1
    assert "TAXONOMY.md" in issues[0].message


# --- taxonomy_similar / taxonomy_unused / taxonomy_singletons --------------

_VOCAB_CATEGORIES = ("taxonomy_similar", "taxonomy_unused", "taxonomy_singletons")

# Twelve methods with nothing alike between any two of them.
_METHODS = [
    "BERT", "GNN", "MCTS", "MD", "MPNN", "RL",
    "VAE", "alphafold", "diffusion", "docking", "random-forest", "transformer",
]


def _vocab_library(vault: Path, *, methods_once: int = 10) -> None:
    """A library with one look-alike pair, one unused value, and a methods
    dict of 12 values of which ``methods_once`` are used by one paper each
    (the rest by two). Its one project has no papers, which is normal for a
    project and must not count as unused vocabulary."""
    from litman.core.project_link import add_project
    from litman.core.taxonomy import add_taxonomy_values

    project_dir = vault.parent / "pepforge"
    project_dir.mkdir()
    add_project(vault, "pepforge", project_dir)
    add_taxonomy_values(vault, "topics", ("peptide", "peptidedrug", "spps"), force=True)
    add_taxonomy_values(vault, "methods", tuple(_METHODS))
    once, twice = _METHODS[:methods_once], _METHODS[methods_once:]
    _write_paper(vault, "2024_A_a", topics=["peptide"], methods=once[0::2] + twice)
    _write_paper(vault, "2024_B_b", topics=["peptide"], methods=once[1::2] + twice)
    _write_paper(vault, "2024_C_c", topics=["peptidedrug"])


def _by_category(issues: list[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {c: [] for c in _VOCAB_CATEGORIES}
    for issue in issues:
        if issue.category in out:
            out[issue.category].append(issue)
    return out


def test_vocabulary_checks_report_each_finding_once(vault: Path) -> None:
    _vocab_library(vault)

    found = _by_category(run_all_checks(vault, list_papers(vault)))

    similar, unused, singletons = (found[c] for c in _VOCAB_CATEGORIES)
    assert len(similar) == 1
    assert similar[0].message == "topics: 'peptidedrug' looks like 'peptide' (prefix)"
    # The less-used value folds into the more-used one.
    assert similar[0].hint == "merge with `lit taxonomy merge topics peptidedrug --into peptide`"
    assert len(unused) == 1
    assert unused[0].message == "topics: 'spps' is used by no paper"
    assert unused[0].hint == "remove with `lit taxonomy rm topics spps`"
    assert len(singletons) == 1
    assert singletons[0].message == "methods: 10 of 12 values are used by one paper"
    assert "`lit taxonomy list methods`" in (singletons[0].hint or "")
    for issue in similar + unused + singletons:
        assert issue.severity == "info"
        assert issue.paper_id is None
        assert len(issue.hint or "") <= 120


def test_similar_keeps_the_value_more_papers_use(vault: Path) -> None:
    """Usage decides before the alphabet: here the kept value sorts last."""
    from litman.core.taxonomy import add_taxonomy_values

    add_taxonomy_values(vault, "topics", ("alpha-model", "alpha-models"), force=True)
    papers = [
        {"id": "2024_A_a", "topics": ["alpha-model"]},
        {"id": "2024_B_b", "topics": ["alpha-models"]},
        {"id": "2024_C_c", "topics": ["alpha-models"]},
    ]

    issues = check_taxonomy_similar(vault, papers)

    assert [(i.message, i.hint) for i in issues] == [
        (
            "topics: 'alpha-model' looks like 'alpha-models' (same after normalisation)",
            "merge with `lit taxonomy merge topics alpha-model --into alpha-models`",
        )
    ]


def test_similar_keeps_the_alphabetically_first_value_on_a_tie(vault: Path) -> None:
    from litman.core.taxonomy import add_taxonomy_values

    add_taxonomy_values(vault, "topics", ("language model", "language-models"), force=True)

    issues = check_taxonomy_similar(vault, [])

    assert [i.hint for i in issues] == [
        "merge with `lit taxonomy merge topics language-models --into 'language model'`"
    ]


def test_singletons_need_ten_values_used_once(vault: Path) -> None:
    _vocab_library(vault, methods_once=9)

    assert check_taxonomy_singletons(vault, list_papers(vault)) == []


@pytest.mark.parametrize(
    ("once", "size", "fires"), [(10, 33, True), (10, 34, False), (12, 40, True)]
)
def test_singletons_need_thirty_percent_of_the_dict(
    vault: Path, once: int, size: int, fires: bool
) -> None:
    """10 of 33 is 30.3 %, 10 of 34 is 29.4 %, and exactly 30 % (12 of 40) fires."""
    from litman.core.taxonomy import add_taxonomy_values

    values = [f"value {chr(ord('a') + i // 26)}{chr(ord('a') + i % 26)}" for i in range(size)]
    add_taxonomy_values(vault, "data", tuple(values), force=True)
    papers = [{"id": f"2024_P_{i}", "data": [v]} for i, v in enumerate(values[:once])]

    issues = check_taxonomy_singletons(vault, papers)

    assert [i.message for i in issues] == (
        [f"data: {once} of {size} values are used by one paper"] if fires else []
    )


@pytest.mark.parametrize("damage", ["missing", "unreadable"])
def test_vocabulary_checks_stay_silent_without_a_taxonomy(
    vault: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """taxonomy_drift owns a missing or unreadable TAXONOMY.md."""
    _vocab_library(vault)
    papers = list_papers(vault)
    # Control: the same library reports all three while TAXONOMY.md reads.
    assert all(_by_category(run_all_checks(vault, papers)).values())
    if damage == "missing":
        unlock_truth_file(vault / "TAXONOMY.md")
        (vault / "TAXONOMY.md").unlink()
    else:
        real_read_text = Path.read_text

        def _refuse(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == "TAXONOMY.md":
                raise PermissionError("simulated unreadable TAXONOMY.md")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _refuse)

    issues = run_all_checks(vault, papers)

    assert _by_category(issues) == {c: [] for c in _VOCAB_CATEGORIES}
    assert [i.category for i in issues if i.category == "taxonomy_drift"]
    for check in (check_taxonomy_similar, check_taxonomy_unused, check_taxonomy_singletons):
        assert check(vault, papers) == []


def _append_latin1_line(vault: Path) -> int:
    """End TAXONOMY.md with a line an editor set to Latin-1 wrote: its é is
    the single byte 0xE9. Returns that line's number."""
    path = vault / "TAXONOMY.md"
    data = path.read_bytes()
    unlock_truth_file(path)
    path.write_bytes(data + b"- caf\xe9\n")
    locking.lock_truth_file(path)
    return data.count(b"\n") + 1


def test_a_taxonomy_that_is_not_utf8_is_one_error_naming_its_line(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drift check reports it; the checks that read the same file stay
    silent; --fix leaves the file as it is."""
    from rich.console import Console

    from litman.commands import health as health_cmd
    from litman.core.correctors import regen

    monkeypatch.setattr(viewer_mod, "detect_platform_viewer", lambda: "open")
    monkeypatch.setattr(health_cmd, "console", Console(width=200))
    _vocab_library(vault)
    regen(vault)
    papers = list_papers(vault)
    # Control: the same library reports all three while TAXONOMY.md reads.
    assert all(_by_category(run_all_checks(vault, papers)).values())
    line = _append_latin1_line(vault)
    data = (vault / "TAXONOMY.md").read_bytes()

    issues = run_all_checks(vault, papers)

    message = (
        f"TAXONOMY.md is present but unreadable (line {line} is not UTF-8): "
        "taxonomy drift cannot be checked"
    )
    # One finding from the five checks that read TAXONOMY.md: the vocabulary
    # checks and project_config_consistency leave it to taxonomy_drift.
    readers = {"taxonomy_drift", *_VOCAB_CATEGORIES, "project_config_consistency"}
    assert [
        (i.category, i.severity, i.message, i.hint) for i in issues if i.category in readers
    ] == [("taxonomy_drift", "error", message, "re-save TAXONOMY.md as UTF-8, then re-run")]

    for args in (["health-check"], ["health-check", "--fix"]):
        result = CliRunner().invoke(cli, [*args, "--library", str(vault)])

        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit), result.exception
        assert message in result.output
        assert "Look-alike vocabulary values" not in result.output
        assert (vault / "TAXONOMY.md").read_bytes() == data


def test_vocabulary_findings_survive_fix_and_do_not_gate_the_exit_code(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    from litman.commands import health as health_cmd
    from litman.core.correctors import regen

    monkeypatch.setattr(viewer_mod, "detect_platform_viewer", lambda: "open")
    monkeypatch.setattr(health_cmd, "console", Console(width=200))
    _vocab_library(vault)
    regen(vault)

    def snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(vault)): p.read_bytes()
            for p in sorted(vault.rglob("*"))
            if p.is_file() and ".litman-staging" not in p.parts
        }

    before = snapshot()
    runner = CliRunner()

    fix = runner.invoke(cli, ["health-check", "--fix", "--library", str(vault)])

    assert fix.exit_code == 0, fix.output
    assert snapshot() == before
    for header in (
        "Look-alike vocabulary values (topics / methods / data) (1 issue)",
        "Vocabulary values no paper uses (1 issue)",
        "Vocabularies mostly made of values used by one paper (1 issue)",
    ):
        assert header in fix.output
    assert "lit taxonomy merge topics peptidedrug --into peptide" in fix.output
    assert "lit taxonomy rm topics spps" in fix.output
    assert "(fixable via --fix)" not in fix.output
    assert "Auto-fix:" not in fix.output


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
    """Install the bundle into an open-standard dir stand-in (the cursor
    location) and point the standard resolver at it."""
    from litman.core.skill import install_all_skills

    parent = tmp_path / "installed-standard-skills"
    install_all_skills(parent_dir=parent)
    monkeypatch.setattr(
        "litman.core.skill.standard_skills_parent_dir", lambda: parent
    )
    return parent


def _plant_antigravity_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Install the bundle into an Antigravity CLI dir stand-in (the agy
    location) and point the antigravity resolver at it."""
    from litman.core.skill import install_all_skills

    parent = tmp_path / "installed-antigravity-skills"
    install_all_skills(parent_dir=parent)
    monkeypatch.setattr(
        "litman.core.skill.antigravity_skills_parent_dir", lambda: parent
    )
    return parent


def test_skill_drift_probes_only_default_agent_dir(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=claude (nothing recorded): a stale copy in the cursor
    standard dir is NOT reported — non-default directories are not the
    check's business (they surface in the GUI per-agent panel and the moment
    that agent becomes the default)."""
    standard = _plant_standard_skills(tmp_path, monkeypatch)
    (standard / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    assert check_skill_drift(vault, []) == []


def test_skill_drift_default_agy_detects_antigravity_dir(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=agy: the SAME stale antigravity-dir copy is now drift, the
    message names the default agent, and a stale claude-dir copy is ignored
    (default-dir semantics both ways). No literal path in the message —
    issues flow into GET /api/health verbatim."""
    from litman.core import agent_prefs

    claude_parent = _plant_skills(tmp_path, monkeypatch)
    antigravity = _plant_antigravity_skills(tmp_path, monkeypatch)
    (antigravity / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    (claude_parent / "lit-reading" / "SKILL.md").write_text(
        "ALSO OUTDATED\n", encoding="utf-8"
    )
    agent_prefs.save_default_agent("agy")  # registry dir is isolated

    issues = check_skill_drift(vault, [])
    assert len(issues) == 1  # the claude-dir staleness is not reported
    issue = issues[0]
    assert "lit-library" in issue.message
    assert "agy" in issue.message
    assert str(antigravity) not in issue.message
    assert str(antigravity) not in (issue.hint or "")


def test_skill_drift_default_agy_absent_is_not_drift(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=agy with nothing installed in the antigravity dir: absent is
    a respected opt-out, even when the claude dir has (stale) skills."""
    from litman.core import agent_prefs

    parent = _plant_skills(tmp_path, monkeypatch)
    (parent / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    agent_prefs.save_default_agent("agy")
    assert check_skill_drift(vault, []) == []


def test_apply_autofix_skill_drift_targets_default_agent_dir(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审查 W3 regression + the cross-directory sweep: probe and --fix agree
    on the default directory (default=agy → the antigravity dir comes
    clean), and the fix ALSO refreshes the stale claude-dir copy via the
    sweep — both counted under skill_drift."""
    from litman.core import agent_prefs

    claude_parent = _plant_skills(tmp_path, monkeypatch)
    antigravity = _plant_antigravity_skills(tmp_path, monkeypatch)
    stale_antigravity = antigravity / "lit-library" / "SKILL.md"
    stale_antigravity.write_text("OUTDATED\n", encoding="utf-8")
    stale_claude = claude_parent / "lit-library" / "SKILL.md"
    stale_claude.write_text("CLAUDE-DIR OUTDATED\n", encoding="utf-8")
    agent_prefs.save_default_agent("agy")

    issues = check_skill_drift(vault, [])
    counts = apply_autofix(vault, issues)
    assert counts["skill_drift"] == 2
    assert "OUTDATED" not in stale_antigravity.read_text(encoding="utf-8")
    # The stale copy in the non-default claude dir was refreshed by the
    # sweep — it may be the copy another agent actually reads.
    text = stale_claude.read_text(encoding="utf-8")
    assert "CLAUDE-DIR OUTDATED" not in text
    assert "name: lit-library" in text
    # Post-fix pass is clean for the default agent.
    assert check_skill_drift(vault, []) == []


def test_apply_autofix_sweep_runs_without_a_skill_drift_finding(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=claude with a clean claude dir: the check reports nothing,
    yet --fix still refreshes a stale copy in the antigravity dir — the fix
    is deliberately wider than the check (the check keeps its
    default-dir-only probe to avoid nagging)."""
    _plant_skills(tmp_path, monkeypatch)  # claude dir current
    antigravity = _plant_antigravity_skills(tmp_path, monkeypatch)
    stale_md = antigravity / "lit-reading" / "SKILL.md"
    stale_md.write_text("OUTDATED\n", encoding="utf-8")

    assert check_skill_drift(vault, []) == []  # nothing reported ...
    counts = apply_autofix(vault, [])  # ... yet the sweep fixes it
    assert counts["skill_drift"] == 1
    text = stale_md.read_text(encoding="utf-8")
    assert "OUTDATED" not in text
    assert "name: lit-reading" in text


def test_apply_autofix_sweep_leaves_absent_and_linked_alone(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep only refreshes stale copies: an absent dir is never
    first-installed and a linked dir (dev checkout) is never written
    through."""
    from litman.core import skill

    _plant_skills(tmp_path, monkeypatch)  # claude dir current
    # antigravity dir: absent (conftest-isolated empty path).
    # standard dir: one linked skill pointing at a divergent checkout.
    real = tmp_path / "dev-checkout" / "lit-library"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("DEV COPY\n", encoding="utf-8")
    standard = skill.standard_skills_parent_dir()
    standard.mkdir(parents=True)
    (standard / "lit-library").symlink_to(real)

    counts = apply_autofix(vault, [])
    assert "skill_drift" not in counts
    assert not skill.antigravity_skills_parent_dir().exists()
    assert (real / "SKILL.md").read_text(encoding="utf-8") == "DEV COPY\n"


def test_health_check_cli_fix_sweeps_with_clean_report(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a clean vault + clean default dir + stale antigravity
    copy → the report shows nothing, `--fix` still refreshes the copy and
    says so, exit 0 both times."""
    monkeypatch.setattr(viewer_mod.sys, "platform", "darwin")
    _plant_skills(tmp_path, monkeypatch)  # claude dir current
    antigravity = _plant_antigravity_skills(tmp_path, monkeypatch)
    stale_md = antigravity / "lit-library" / "SKILL.md"
    stale_md.write_text("OUTDATED\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "skill_drift" in result.output
    assert "OUTDATED" not in stale_md.read_text(encoding="utf-8")


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


def test_a_notes_file_that_is_not_utf8_is_reported_not_a_crash(vault: Path) -> None:
    """A Latin-1 byte in notes.md is a decode error, not an OSError: it must
    land in the same "could not read" warning instead of a traceback."""
    pid = "2024_Foo_Bar"
    _write_paper(vault, pid)
    (vault / "papers" / pid / "notes.md").write_bytes(b"caf\xe9 latin-1 line\n")

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])

    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert result.exit_code == 1, result.output
    notes_rel = str(Path("papers") / pid / "notes.md")
    lines = [
        line
        for line in result.output.splitlines()
        if "could not read notes file" in line
    ]
    assert len(lines) == 1, result.output
    assert "warning" in lines[0]
    assert notes_rel in lines[0]
    assert "UnicodeDecodeError" in lines[0]


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
    _write_paper(vault, "A_a_a", topics=["unregistered-topic"])
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
            "retired_priority",
            "dangling_refs",
            "supplementary_missing",
            "supplementary_unrecorded",
            "dirty_metadata",
            "unfinished_rename",
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
    """--fix leaves a one-sided pairing alone: both papers exist, so which
    side is right is the user's call."""
    _write_paper(vault, "A_a_a", related=["B_b_b"])
    _write_paper(vault, "B_b_b")
    (vault / ".litman-staging" / "op-crashed").mkdir()
    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    # Still one-sided, so exit 1.
    assert result.exit_code == 1
    assert "'A_a_a'.related contains 'B_b_b'" in result.output
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
    """Fake the OS boundary, not litman's helpers — every layer above runs real.

    BOTH mechanisms have to go. Poisoning only ``Path.symlink_to`` left the
    Windows junction constructor untouched, so the probe still said "links
    work" and the linkless-drive scenario quietly stopped being linkless.
    """
    from litman.core import portable_link
    from litman.core.portable_link import reset_link_probe_cache

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "symlink_to", boom)
    monkeypatch.setattr(portable_link, "_create_junction", boom)
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

    # Patch the probe, not sys.platform: ``viewer_mod.sys`` IS the global sys
    # module, so faking "darwin" there also sent core.atomic._fsync_dir down
    # its POSIX arm on a Windows host, where os.O_DIRECTORY does not exist.
    # checks.py imports this function at call time, so the module attribute is
    # the seam. Windows would answer its own sentinel here anyway.
    monkeypatch.setattr(viewer_mod, "detect_platform_viewer", lambda: "open")

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
        if is_portable_link(child):
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


# --- report folding ---------------------------------------------------------


def _plant_placeholder_ids(vault: Path, n: int) -> None:
    """`n` papers whose ids carry a filler — the legacy-library shape.

    Metadata deliberately clean: these report through `placeholder_id` only,
    so the folding assertions below count one category, not two.
    """
    for i in range(n):
        _write_paper(
            vault,
            f"2024_Unknown_Untitled-{i}",
            title=f"Deep learning for protein design {i}",
            authors=["Zhang, San"],
            doi=f"10.0/legacy-{i}",
        )


def test_report_folds_a_flooded_category(vault: Path) -> None:
    """A vault imported before the guard existed must not print 12 screens.

    The count in the header and the summary stay exact — only the listing is
    capped, and the tail line says so and names the way to see the rest.
    """
    _plant_placeholder_ids(vault, 12)

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])
    assert result.exit_code == 1
    assert result.output.count("id carries a placeholder") == _CATEGORY_PREVIEW
    assert "… and 7 more in this category" in result.output
    assert "lit health-check --all" in result.output
    # Folding is a display decision: nothing was dropped from the tally.
    assert "(12 issues)" in result.output


def test_all_prints_every_finding(vault: Path) -> None:
    _plant_placeholder_ids(vault, 12)

    result = CliRunner().invoke(
        cli, ["health-check", "--all", "--library", str(vault)]
    )
    assert result.output.count("id carries a placeholder") == 12
    assert "more in this category" not in result.output


def test_a_category_at_the_cap_grows_no_tail(vault: Path) -> None:
    """Off-by-one guard: exactly `_CATEGORY_PREVIEW` findings fold nothing."""
    _plant_placeholder_ids(vault, _CATEGORY_PREVIEW)

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])
    assert result.output.count("id carries a placeholder") == _CATEGORY_PREVIEW
    assert "more in this category" not in result.output


def test_folding_is_per_category_not_per_report(vault: Path) -> None:
    """Each category keeps its own preview — one flood must not hide another."""
    _plant_placeholder_ids(vault, 8)
    for i in range(8):
        _write_paper(
            vault,
            f"2024_Filler_Metadata-{i}",
            title="Untitled",
            authors=["Zhang, San"],
            doi=f"10.0/filler-{i}",
        )

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])
    assert result.output.count("id carries a placeholder") == _CATEGORY_PREVIEW
    assert result.output.count("title is a placeholder") == _CATEGORY_PREVIEW
    # Both planted categories hold 8, so both fold exactly 3 — counting that
    # phrase rather than every tail line keeps the assertion about these two
    # and not about whatever else an unregenerated fixture vault reports.
    assert result.output.count("… and 3 more in this category") == 2


def _plant_kept_folder(vault: Path, project: str, name: str) -> Path:
    kept = (
        vault
        / ".trash"
        / "replaced-folders"
        / project
        / "litman_reflib"
        / f"{name}-20260908T093800Z"
    )
    kept.mkdir(parents=True)
    (kept / "MY_NOTES.md").write_text("hand-written\n", encoding="utf-8")
    return kept


def test_kept_hub_folders_are_surfaced_as_info(vault: Path) -> None:
    """The dim line at settle time scrolls away; the folders do not.

    No cap, no eviction, no restore — and a litman_code one is a whole git
    checkout that `lit sync` pushes to the user's cloud.
    """
    from litman.core.trash import count_replaced_folders

    assert check_trash_health(vault, list_papers(vault)) == []
    _plant_kept_folder(vault, "myproj", "2024_A_One")
    assert count_replaced_folders(vault) == 1

    issues = check_trash_health(vault, list_papers(vault))

    assert len(issues) == 1
    assert issues[0].category == "replaced_folders"
    assert issues[0].severity == "info"
    assert "holds 1 folder kept from project hubs" in issues[0].message
    assert issues[0].hint is not None and "lit trash empty" in issues[0].hint

    runner = CliRunner()
    result = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert "Folders kept out of project hubs" in result.output


def test_kept_hub_folders_are_never_auto_fixed(vault: Path) -> None:
    """Decision #3: `--fix` must never delete what litman could not vouch for.

    Pinned three ways — the two category sets and the command itself — because
    a future category added to AUTO_FIXABLE_CATEGORIES by name would silently
    turn `--fix` into a delete of the user's only copy.
    """
    from litman.commands import health

    assert "replaced_folders" not in AUTO_FIXABLE_CATEGORIES
    assert "replaced_folders" not in health._KLASS_A_CATEGORIES
    assert "replaced_folders" not in health._fixable_categories()

    kept = _plant_kept_folder(vault, "myproj", "2024_A_One")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )

    assert (kept / "MY_NOTES.md").read_text(encoding="utf-8") == "hand-written\n"
    # Still reported after the fix — it is a notice, not a defect to clear.
    assert "Folders kept out of project hubs" in result.output
    assert "fixable via --fix" not in result.output.split(
        "Folders kept out of project hubs"
    )[1].split("\n")[0]
    assert check_trash_health(vault, list_papers(vault))


# ===========================================================================
# supplementary — SI records ↔ papers/<id>/supplementary/ (ledger #13)
# ===========================================================================

# SI named ci9b00237_* carries this DOI's number, so it verifies by name.
_SI_DOI = "10.1021/acs.jcim.9b00237"
_SI_PAPER = "2019_Yang_Si"


def _si_paper(vault: Path, tmp_path: Path, n: int = 2) -> Path:
    """A paper with ``n`` recorded SI files (si-01.csv …); returns its folder."""
    from litman.core.supplementary import SiItem, add_supplementary

    _write_paper(vault, _SI_PAPER, doi=_SI_DOI)
    items = []
    for i in range(1, n + 1):
        src = tmp_path / f"ci9b00237_si_{i:03d}.csv"
        src.write_text(f"row,{i}\n", encoding="utf-8")
        items.append(SiItem(src, src.name))
    add_supplementary(vault, _SI_PAPER, items, allow_unverified=False)
    return vault / "papers" / _SI_PAPER / "supplementary"


def _si_records(vault: Path) -> list[tuple[str, str]]:
    from litman.core.document import find_paper

    meta = find_paper(vault, _SI_PAPER)
    return [(r["file"], r["check"]) for r in meta.get("supplementary") or []]


def _si_issues(vault: Path) -> list[Any]:
    from litman.core.checks import check_supplementary

    return check_supplementary(vault, list_papers(vault))


def test_supplementary_check_reports_each_drift(vault: Path, tmp_path: Path) -> None:
    si = _si_paper(vault, tmp_path)
    assert _si_issues(vault) == []
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()
    unlock_truth_file(si / "si-02.csv")
    (si / "si-02.csv").write_text("row,2,edited\n", encoding="utf-8")
    (si / "notes from the email.txt").write_text("x", encoding="utf-8")
    (si / ".DS_Store").write_bytes(b"\0")  # clutter is nobody's finding
    _write_paper(vault, "2020_Bad_Field")
    _append_metadata(vault, "2020_Bad_Field", "supplementary: si-01.pdf\n")

    issues = _si_issues(vault)

    assert sorted((i.category, i.severity, i.paper_id) for i in issues) == [
        ("supplementary_changed", "error", _SI_PAPER),
        ("supplementary_field", "error", "2020_Bad_Field"),
        ("supplementary_missing", "error", _SI_PAPER),
        ("supplementary_unrecorded", "warning", _SI_PAPER),
    ]
    by_category = {i.category: i for i in issues}
    assert "si-01.csv is recorded but missing" in by_category["supplementary_missing"].message
    assert by_category["supplementary_missing"].hint.endswith(
        f"`lit rm {_SI_PAPER} --si si-01 --yes`"
    )
    assert "'supplementary' is not a list" in by_category["supplementary_field"].message
    assert "notes from the email.txt" in by_category["supplementary_unrecorded"].message


def test_supplementary_errors_block_push_but_a_stray_does_not(
    vault: Path, tmp_path: Path
) -> None:
    from litman.core.checks import run_push_integrity_errors

    si = _si_paper(vault, tmp_path)
    (si / "stray.txt").write_text("x", encoding="utf-8")

    def si_errors() -> set[str]:
        return {
            i.category
            for i in run_push_integrity_errors(vault, list_papers(vault))
            if i.category.startswith("supplementary")
        }

    assert si_errors() == set()  # a stray is a warning
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()
    assert si_errors() == {"supplementary_missing"}


def test_health_check_reports_si_findings(vault: Path, tmp_path: Path) -> None:
    si = _si_paper(vault, tmp_path)
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])

    assert result.exit_code == 1
    header = next(
        line for line in result.output.splitlines()
        if line.startswith("SI files recorded but missing")
    )
    assert "fixable via --fix" in header


def test_fix_without_a_terminal_records_only_what_verifies(
    vault: Path, tmp_path: Path
) -> None:
    """No one to ask: a stray that verifies is recorded; the dead record, the
    unconfirmable stray and the refused one stay — each with the command that
    would settle it."""
    si = _si_paper(vault, tmp_path, n=1)
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()
    (si / "ci9b00237_si_004.csv").write_text("verified,4\n", encoding="utf-8")
    (si / "Table S2.xlsx").write_bytes(b"PK sheet")
    _write_paper(vault, "2023_Zhong_Other", doi="10.1038/s41467-023-38851-5")
    (si / "41467_2023_38851_MOESM1_ESM.pdf").write_bytes(b"%PDF-1.4 theirs\n")

    result = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )

    out = result.output
    assert f"lit rm {_SI_PAPER} --si si-01 --yes" in out
    assert f'lit add --si {_SI_PAPER} "{si / "Table S2.xlsx"}" --force' in out
    assert "looks like the SI of 2023_Zhong_Other" in out
    assert "supplementary_unrecorded: cleaned 1 item" in out
    assert _si_records(vault) == [("si-01.csv", "filename"), ("si-02.csv", "filename")]
    assert not (si / "ci9b00237_si_004.csv").exists()
    assert (si / "Table S2.xlsx").exists()
    assert (si / "41467_2023_38851_MOESM1_ESM.pdf").exists()
    assert result.exit_code == 1  # what was left is still reported


def test_fix_at_a_terminal_asks_before_dropping_or_recording(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core import confirm as confirm_mod

    monkeypatch.setattr(confirm_mod, "_stdin_is_tty", lambda: True)
    si = _si_paper(vault, tmp_path, n=1)
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()
    # Already si-NN: recorded where it is, keeping a number notes may cite.
    (si / "si-07.csv").write_text("x,7\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)], input="y\ny\n"
    )

    assert "si-01.csv recorded but missing. Drop the record(s)?" in result.output
    assert "can't confirm si-07.csv belongs to it. Record it anyway?" in result.output
    assert _si_records(vault) == [("si-07.csv", "user")]
    assert (si / "si-07.csv").is_file()
    assert _si_issues(vault) == []


def test_fix_at_a_terminal_defaults(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enter twice: the dead record goes (default yes), the unconfirmable
    stray stays out (default no)."""
    from litman.core import confirm as confirm_mod

    monkeypatch.setattr(confirm_mod, "_stdin_is_tty", lambda: True)
    si = _si_paper(vault, tmp_path, n=2)
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()
    (si / "unsure.csv").write_text("u\n", encoding="utf-8")

    CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)], input="\n\n"
    )

    assert _si_records(vault) == [("si-02.csv", "filename")]
    assert (si / "unsure.csv").exists()


def test_apply_autofix_without_a_policy_asks_nobody(
    vault: Path, tmp_path: Path
) -> None:
    si = _si_paper(vault, tmp_path, n=1)
    unlock_truth_file(si / "si-01.csv")
    (si / "si-01.csv").unlink()
    (si / "ci9b00237_si_009.csv").write_text("v\n", encoding="utf-8")
    (si / "unsure.csv").write_text("u\n", encoding="utf-8")

    counts = apply_autofix(vault, run_all_checks(vault, list_papers(vault)))

    assert counts["supplementary_missing"] == 0
    assert counts["supplementary_unrecorded"] == 1
    assert [f for f, _ in _si_records(vault)] == ["si-01.csv", "si-02.csv"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_health_check_relocks_si_files(vault: Path, tmp_path: Path) -> None:
    si = _si_paper(vault, tmp_path, n=1)
    os.chmod(si / "si-01.csv", 0o644)  # e.g. pulled by a sync, mode lost

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])

    # Folded at the console width: compare with the line breaks flattened.
    flat = " ".join(result.output.split())
    assert "Re-locked" in flat
    assert "/ SI files made read-only" in flat
    assert not os.access(si / "si-01.csv", os.W_OK)


def test_layout_findings_are_report_only(vault: Path, tmp_path: Path) -> None:
    """What --fix will never touch — a sub-folder, a supplementary that is a
    file — sits in a category outside AUTO_FIXABLE, whose header and hints
    promise nothing about --fix."""
    si = _si_paper(vault, tmp_path, n=1)
    unzipped = si / "unzipped"
    unzipped.mkdir()
    (unzipped / "table.csv").write_text("t\n", encoding="utf-8")
    _write_paper(vault, "2020_File_Folder")
    placeholder = vault / "papers" / "2020_File_Folder" / "supplementary"
    placeholder.write_text("x", encoding="utf-8")

    layout = [i for i in _si_issues(vault) if i.category == "supplementary_layout"]

    assert sorted((i.paper_id, i.severity) for i in layout) == [
        ("2019_Yang_Si", "warning"),
        ("2020_File_Folder", "error"),
    ]
    assert "supplementary_layout" not in AUTO_FIXABLE_CATEGORIES
    assert all("--fix" not in (i.hint or "") for i in layout)

    result = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    header = next(
        line for line in result.output.splitlines()
        if line.startswith("SI folder layout")
    )
    assert "fixable via --fix" not in header
    assert (unzipped / "table.csv").is_file()
    assert placeholder.read_text(encoding="utf-8") == "x"


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod cannot take a folder's read bit"
)
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the permission bits this takes away",
)
def test_health_check_reports_an_unreadable_si_folder(
    vault: Path, tmp_path: Path
) -> None:
    """A folder litman cannot read is a finding ("can't check X"), never a
    traceback out of the relock sweep or the check."""
    si = _si_paper(vault, tmp_path, n=1)
    si.chmod(0o000)
    try:
        result = CliRunner().invoke(
            cli, ["health-check", "--library", str(vault)]
        )
    finally:
        si.chmod(0o755)
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "SI litman cannot check" in flat
    assert "cannot be read (PermissionError)" in flat


# ===========================================================================
# supplementary_cited — si-NN citations in notes ↔ SI records (ledger #14)
# ===========================================================================


def _cited_issues(vault: Path) -> list[Any]:
    from litman.core.checks import check_supplementary_cited

    return check_supplementary_cited(vault, list_papers(vault))


def _added_day(vault: Path) -> str:
    from litman.core.document import find_paper

    return str(find_paper(vault, _SI_PAPER)["supplementary"][0]["added"])[:10]


def test_supplementary_cited_reports_each_case_with_its_line(
    vault: Path, tmp_path: Path
) -> None:
    _si_paper(vault, tmp_path, n=2)  # si-01.csv, si-02.csv, added today
    paper = vault / "papers" / _SI_PAPER
    (paper / "notes.md").write_text(
        "# Notes\n"
        "si-01.csv（ci9b00237_si_001.csv） is still right\n"
        "si-02.csv（other.csv）\n"
        "si-01.csv（ci9b00237_si_002.csv）\n"
        "si-05\n",
        encoding="utf-8",
    )
    (paper / "discussion.md").write_text(
        f"# Discussion log for {_SI_PAPER}\n\n"
        "## 2026-01-02 10:00\n"
        "si-01 was read before it was added\n"
        "si-02.csv（ci9b00237_si_002.csv） names its file\n",
        encoding="utf-8",
    )

    issues = _cited_issues(vault)

    assert {(i.category, i.severity, i.paper_id) for i in issues} == {
        ("supplementary_cited", "warning", _SI_PAPER)
    }
    assert [(i.message, i.hint) for i in issues] == [
        (
            "notes.md:3 cites si-02.csv as other.csv, but si-02.csv is now "
            "ci9b00237_si_002.csv",
            f"check which file it means — `lit show {_SI_PAPER}` lists each SI "
            "with its original name",
        ),
        (
            "notes.md:4 cites si-01.csv as ci9b00237_si_002.csv, but si-01.csv "
            "is now ci9b00237_si_001.csv — that file is now si-02.csv",
            "change it to si-02.csv",
        ),
        (
            "notes.md:5 cites si-05, which this paper no longer has",
            f"point it at the right file (`lit show {_SI_PAPER}` lists the SI), "
            "or remove it",
        ),
        (
            f"discussion.md:4 (§2026-01-02) cites si-01, added later "
            f"({_added_day(vault)}) — may refer to a file since replaced",
            "if it means this file, write its name after it: "
            "si-01.csv（ci9b00237_si_001.csv）",
        ),
    ]


def test_an_original_from_the_notes_is_shown_printable(
    vault: Path, tmp_path: Path
) -> None:
    _si_paper(vault, tmp_path, n=1)
    (vault / "papers" / _SI_PAPER / "notes.md").write_text(
        "si-01.csv（evil\u202eftp.csv）\n", encoding="utf-8"
    )
    (issue,) = _cited_issues(vault)
    assert issue.message == (
        "notes.md:1 cites si-01.csv as evil ftp.csv, but si-01.csv is now "
        "ci9b00237_si_001.csv"
    )


def test_a_citation_in_a_paper_with_no_si_is_reported(vault: Path) -> None:
    _write_paper(vault, "2020_No_Si", notes="# N\n\nsee si-01.pdf\n")
    (issue,) = _cited_issues(vault)
    assert (issue.paper_id, issue.message) == (
        "2020_No_Si",
        "notes.md:3 cites si-01.pdf, which this paper no longer has",
    )


def test_health_check_lists_stale_citations_under_their_header(
    vault: Path, tmp_path: Path
) -> None:
    _si_paper(vault, tmp_path, n=1)
    (vault / "papers" / _SI_PAPER / "discussion.md").write_text(
        "# Discussion log\n\nsee si-07.csv\n", encoding="utf-8"
    )

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])

    assert result.exit_code == 1
    header = next(
        line for line in result.output.splitlines()
        if line.startswith("SI citations in notes / discussion that no longer match")
    )
    assert "fixable via --fix" not in header
    line = next(
        line for line in result.output.splitlines() if "cites si-07.csv" in line
    )
    assert line.split() == [
        "warning", _SI_PAPER, "discussion.md:3", "cites", "si-07.csv,", "which",
        "this", "paper", "no", "longer", "has",
    ]


def test_a_notes_file_that_is_not_utf8_leaves_its_si_citations_unchecked(
    vault: Path, tmp_path: Path
) -> None:
    _si_paper(vault, tmp_path, n=1)
    (vault / "papers" / _SI_PAPER / "notes.md").write_bytes(b"si-01 caf\xe9\n")

    result = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])

    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    (issue,) = _cited_issues(vault)
    assert (issue.paper_id, issue.message, issue.hint) == (
        _SI_PAPER,
        "notes.md cannot be read (UnicodeDecodeError); its SI citations went "
        "unchecked",
        "save it as UTF-8 (or fix its permissions), then rerun `lit health-check`",
    )
    assert issue.message in result.output


def test_a_malformed_si_field_gets_no_citation_findings(vault: Path) -> None:
    """Which files the paper has is unknown until the field parses: it gets
    supplementary_field, and its citations wait."""
    _write_paper(vault, "2020_Bad_Field", notes="si-01 and si-02.pdf\n")
    _append_metadata(vault, "2020_Bad_Field", "supplementary: si-01.pdf\n")

    assert _cited_issues(vault) == []
    categories = {
        i.category
        for i in run_all_checks(vault, list_papers(vault))
        if i.paper_id == "2020_Bad_Field"
    }
    assert "supplementary_field" in categories
    assert "supplementary_cited" not in categories


def test_fix_never_edits_a_stale_citation(vault: Path, tmp_path: Path) -> None:
    _si_paper(vault, tmp_path, n=1)
    notes = vault / "papers" / _SI_PAPER / "notes.md"
    notes.write_text("# N\n\nsi-09 is gone\n", encoding="utf-8")
    before = notes.read_bytes()

    result = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )

    assert "supplementary_cited" not in AUTO_FIXABLE_CATEGORIES
    assert notes.read_bytes() == before
    assert [i.message for i in _cited_issues(vault)] == [
        "notes.md:3 cites si-09, which this paper no longer has"
    ]
    assert result.exit_code == 1


def test_the_push_gate_never_reads_the_notes(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale citation is a warning, which never blocks a push; the gate
    skips the check rather than read every notes file for nothing."""
    from litman.core.checks import run_push_integrity_errors

    _si_paper(vault, tmp_path, n=1)
    (vault / "papers" / _SI_PAPER / "notes.md").write_text(
        "si-09\n", encoding="utf-8"
    )
    papers = list_papers(vault)
    read: list[str] = []
    real_read_text = Path.read_text

    def spy(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
        read.append(self.name)
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    # Control: the spy does see the check read notes.md.
    assert len(_cited_issues(vault)) == 1
    assert "notes.md" in read
    read.clear()

    assert run_push_integrity_errors(vault, papers) == []
    assert "notes.md" not in read


def test_an_unreadable_added_date_is_a_finding_not_a_skip(vault: Path) -> None:
    """Invariant #14: a record whose ``added`` cannot be read cannot clear a
    dated citation either. A record with no original name cannot be named in
    the hint, which then points at `lit show`."""
    _write_paper(vault, "2020_Hand_Made")
    _append_metadata(
        vault,
        "2020_Hand_Made",
        "supplementary:\n"
        "  - file: si-01.pdf\n"
        "    original: Table_S1.pdf\n"
        "    added: sometime last week\n"
        "  - file: si-02.csv\n"
        "    added: '2026-10-21T10:00:00+02:00'\n",
    )
    (vault / "papers" / "2020_Hand_Made" / "discussion.md").write_text(
        "# Discussion log\n\n## 2026-09-02 10:00\nsi-01 and si-02\n",
        encoding="utf-8",
    )

    assert [(i.message, i.hint) for i in _cited_issues(vault)] == [
        (
            "discussion.md:4 (§2026-09-02) cites si-01, whose added date can't "
            "be read — may refer to a file since replaced",
            "if it means this file, write its name after it: si-01.pdf（Table_S1.pdf）",
        ),
        (
            "discussion.md:4 (§2026-09-02) cites si-02, added later (2026-10-21) — "
            "may refer to a file since replaced",
            "check which file it means — `lit show 2020_Hand_Made` lists each SI "
            "with its original name",
        ),
    ]


# ---------------------------------------------------------------------------
# dirty_metadata: a publisher's line breaks and markup, and what --fix does
#
# Titles and journals as Crossref returned them for 10.1021/jacs.6c11517 (A),
# 10.1021/acs.jcim.6c00587 (B) and 10.1093/bib/bbae714 (C). notes.md goes to
# disk as bytes: the line endings are the point of two of these tests.
# ---------------------------------------------------------------------------

_PP = "\n" + " " * 20
_TITLE_A = (
    f"({_PP}<i>N</i>{_PP}-Hydroxy)amide-Mediated\nRapid Head-to-Tail "
    "Cyclization of Unprotected Peptides in Water"
)
_CLEAN_A = (
    "(N-Hydroxy)amide-Mediated Rapid Head-to-Tail Cyclization of "
    "Unprotected Peptides in Water"
)
_TITLE_B = (
    "Advancing Biochemical\nMolecule Registration, Representation\nand "
    "Search for New Drug Modalities"
)
_CLEAN_B = (
    "Advancing Biochemical Molecule Registration, Representation and "
    "Search for New Drug Modalities"
)
_TITLE_C = "<i>cyclicpeptide</i>: a Python package for cyclic peptide drug design"
_CLEAN_C = "cyclicpeptide: a Python package for cyclic peptide drug design"
_EDITED_C = b"# cyclicpeptide, the package\n\nMy notes on <i>cyclicpeptide</i>.\n"


def _scaffold_notes(title: str, newline: str = "\n") -> bytes:
    """notes.md byte for byte as `lit add` writes it. Text mode, so a library
    written on Windows has CRLF throughout — inside a broken title too."""
    text = f"# {title}\n\n{WIKILINK_REMINDER}\n\n(Personal notes go here.)\n"
    return text.replace("\n", newline).encode("utf-8")


def _notes(vault: Path, paper_id: str) -> Path:
    return vault / "papers" / paper_id / "notes.md"


def _dirty_library(vault: Path) -> dict[str, str]:
    """The three samples as they sit in a library filled before the fix.

    A: id derived from the dirty title, notes written on Windows. B: line
    breaks only, LF notes the user has added to below the heading. C: id given
    by hand, and a notes.md whose heading the user rewrote.
    """
    ids = {
        "A": derive_id(2026, "Zhang", _TITLE_A),
        "B": "2026_Pinto-Gil_Advancing-Biochemical-Molecule",
        "C": "2025_Zhu_CyclicPeptide",
    }
    _write_paper(
        vault, ids["A"], title=_TITLE_A, authors=["Zhang, Yue"], year=2026,
        journal="Journal of the American\nChemical Society",
    )
    _write_paper(
        vault, ids["B"], title=_TITLE_B, authors=["Pinto-Gil, Kevin"], year=2026,
        journal="Journal of Chemical Information\nand Modeling",
    )
    _write_paper(
        vault, ids["C"], title=_TITLE_C, authors=["Yang, Liu", "Zhu, Ruixin"],
        year=2024, journal="Briefings in Bioinformatics",
    )
    _notes(vault, ids["A"]).write_bytes(_scaffold_notes(_TITLE_A, "\r\n"))
    _notes(vault, ids["B"]).write_bytes(_scaffold_notes(_TITLE_B) + b"Mine.\n")
    _notes(vault, ids["C"]).write_bytes(_EDITED_C)
    return ids


def test_dirty_metadata_reported_then_cleaned_by_fix(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    from litman.commands import health as health_cmd

    # The probe, not sys.platform (see the linkless-drive fixture above).
    monkeypatch.setattr(viewer_mod, "detect_platform_viewer", lambda: "open")
    # Wide and pinned: each assertion below reads one whole line.
    monkeypatch.setattr(health_cmd, "console", Console(width=200))
    ids = _dirty_library(vault)
    stamps = {k: _meta(vault, pid)["updated-at"] for k, pid in ids.items()}
    runner = CliRunner()

    report = runner.invoke(cli, ["health-check", "--library", str(vault)])

    assert report.exit_code == 1, report.output
    assert (
        "Line breaks, stray spaces or markup in title / journal / authors"
        in report.output
    )
    assert "(5 issues)" in report.output
    # Only A's id was derived from its broken title.
    clean_id = derive_id(2026, "Zhang", _CLEAN_A)
    assert f"`lit rename {ids['A']} {clean_id}`" in report.output
    assert report.output.count("lit rename") == 1

    fix = runner.invoke(cli, ["health-check", "--fix", "--library", str(vault)])

    assert fix.exit_code == 0, fix.output
    assert "dirty_metadata: cleaned 5 items" in fix.output
    assert (
        f"kept {ids['C']}: notes.md heading differs from the one lit add "
        "wrote, left as is"
    ) in fix.output
    assert fix.output.count("kept ") == 1
    assert (
        f"rename {ids['A']}: `lit rename {ids['A']} {clean_id}` if you want "
        "the id to match the clean title"
    ) in fix.output

    expected = {
        "A": (_CLEAN_A, "Journal of the American Chemical Society"),
        "B": (_CLEAN_B, "Journal of Chemical Information and Modeling"),
        "C": (_CLEAN_C, "Briefings in Bioinformatics"),
    }
    for letter, pid in ids.items():
        meta = _meta(vault, pid)
        assert (meta["title"], meta["journal"]) == expected[letter]
        assert meta["id"] == pid  # never renamed
        assert meta["updated-at"] != stamps[letter]
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert {p["id"]: p["title"] for p in index["papers"]} == {
        pid: expected[letter][0] for letter, pid in ids.items()
    }
    # The heading lit add wrote becomes one line, in the file's own line
    # endings, and nothing after it moves; a heading the user wrote stays.
    assert _notes(vault, ids["A"]).read_bytes() == _scaffold_notes(_CLEAN_A, "\r\n")
    assert _notes(vault, ids["B"]).read_bytes() == (
        _scaffold_notes(_CLEAN_B) + b"Mine.\n"
    )
    assert _notes(vault, ids["C"]).read_bytes() == _EDITED_C

    again = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert again.exit_code == 0, again.output
    assert "Line breaks, stray spaces or markup" not in again.output


def test_clean_dirty_metadata_counts_fields_and_runs_once(vault: Path) -> None:
    """One per finding — A and B two each, C one — and a second run finds
    nothing to do."""
    ids = _dirty_library(vault)
    assert len(check_dirty_metadata(vault, list_papers(vault))) == 5

    clean_id = derive_id(2026, "Zhang", _CLEAN_A)
    assert clean_dirty_metadata(vault) == (5, [ids["C"]], [(ids["A"], clean_id)])

    assert check_dirty_metadata(vault, list_papers(vault)) == []
    assert clean_dirty_metadata(vault) == (0, [], [])


def test_a_notes_heading_that_already_reads_clean_is_left_quietly(
    vault: Path,
) -> None:
    """Someone fixed the heading by hand and not the metadata: nothing to say."""
    _write_paper(vault, "2025_Zhu_CyclicPeptide", title=_TITLE_C, year=2024)
    notes = _scaffold_notes(_CLEAN_C) + b"Mine.\n"
    _notes(vault, "2025_Zhu_CyclicPeptide").write_bytes(notes)

    assert clean_dirty_metadata(vault) == (1, [], [])
    assert _notes(vault, "2025_Zhu_CyclicPeptide").read_bytes() == notes


def test_fix_carries_the_clean_title_into_project_references(
    vault: Path, tmp_path: Path
) -> None:
    from litman.core.correctors import regen

    proj = tmp_path / "myproj"
    proj.mkdir()
    _configure_project(vault, "myproj", proj)
    pid = "2026_Pinto-Gil_Advancing-Biochemical-Molecule"
    _write_paper(
        vault, pid, title=_TITLE_B, authors=["Pinto-Gil, Kevin"], year=2026,
        projects=["myproj"],
    )
    regen(vault)
    refs = proj / "litman_reflib" / "REFERENCES.md"
    assert "Advancing Biochemical\nMolecule" in refs.read_text(encoding="utf-8")

    clean_dirty_metadata(vault)

    assert f"**{_CLEAN_B}**" in refs.read_text(encoding="utf-8")


def test_clean_dirty_metadata_names_the_rename_only_for_an_id_made_from_the_title(
    vault: Path,
) -> None:
    """The same broken title twice: under the id `lit add` derived from it,
    and under one picked by hand. Only the first gets a rename, and neither
    id moves."""
    derived = derive_id(2026, "Zhang", _TITLE_A)
    by_hand = "2026_Zhang_Head-to-tail-cyclization"
    for pid in (derived, by_hand):
        _write_paper(
            vault, pid, title=_TITLE_A, authors=["Zhang, Yue"], year=2026
        )

    n, kept, renames = clean_dirty_metadata(vault)

    assert (n, kept) == (2, [])
    assert renames == [(derived, derive_id(2026, "Zhang", _CLEAN_A))]
    assert {_meta(vault, pid)["id"] for pid in (derived, by_hand)} == {
        derived,
        by_hand,
    }


def _noisy_paper(vault: Path, paper_id: str) -> None:
    """A hand-given id whose every text field is dirty: five findings."""
    _write_paper(
        vault, paper_id, year=2020,
        title="Noisy\n    <i>Peptide</i>\n    Cyclization",
        journal="Journal of\n  Noise",
        authors=["Zhang,\n  Yue", "Wang, Weifeng", "<i>Fu</i>, Hua", "Lu,\xa0Ana"],
    )
    # A JSON string is a YAML double-quoted scalar: the line break goes to
    # disk as an escape and comes back as a line break on read.
    booktitle = json.dumps("Proceedings of\n  <i>NeurIPS</i>")
    publisher = json.dumps("Curran &amp; Associates")
    _append_metadata(
        vault, paper_id, f"booktitle: {booktitle}\npublisher: {publisher}\n"
    )


def test_fix_cleans_authors_booktitle_and_publisher(vault: Path) -> None:
    pid = "2020_Zhang_Noisy-Peptide"
    _noisy_paper(vault, pid)
    clean_authors = ["Zhang, Yue", "Wang, Weifeng", "Fu, Hua", "Lu, Ana"]

    fix = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )

    assert fix.exit_code == 0, fix.output
    assert "dirty_metadata: cleaned 5 items" in fix.output
    meta = _meta(vault, pid)
    assert meta["authors"] == clean_authors  # order kept
    assert meta["booktitle"] == "Proceedings of NeurIPS"
    assert meta["publisher"] == "Curran & Associates"
    assert (meta["title"], meta["journal"]) == (
        "Noisy Peptide Cyclization",
        "Journal of Noise",
    )
    # INDEX.json is what the web UI's paper list shows.
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    (entry,) = index["papers"]
    assert entry["authors"] == clean_authors
    assert entry["title"] == "Noisy Peptide Cyclization"


def test_fix_gives_the_rename_even_when_the_report_folded_the_finding(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A's title finding sorts sixth, past the fold, so the report never shows
    its `lit rename`; the --fix output must, because afterwards nothing can."""
    from rich.console import Console

    from litman.commands import health as health_cmd

    monkeypatch.setattr(viewer_mod, "detect_platform_viewer", lambda: "open")
    monkeypatch.setattr(health_cmd, "console", Console(width=200))
    _noisy_paper(vault, "2020_Zhang_Noisy-Peptide")
    pid = derive_id(2026, "Zhang", _TITLE_A)
    _write_paper(vault, pid, title=_TITLE_A, authors=["Zhang, Yue"], year=2026)
    target = derive_id(2026, "Zhang", _CLEAN_A)
    runner = CliRunner()

    report = runner.invoke(cli, ["health-check", "--library", str(vault)])
    assert "(6 issues)" in report.output
    assert "and 1 more in this category" in report.output
    assert "lit rename" not in report.output

    fix = runner.invoke(cli, ["health-check", "--fix", "--library", str(vault)])

    assert fix.exit_code == 0, fix.output
    assert (
        f"rename {pid}: `lit rename {pid} {target}` if you want the id to "
        "match the clean title"
    ) in fix.output
    assert _meta(vault, pid)["id"] == pid
    assert not (vault / "papers" / target).exists()
