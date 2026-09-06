"""M-1…M-6: the permanent `--fix` migration off the paper-level `priority`.

`priority` (A/B/C) was a property of the paper; it is now a property of the
paper's link to a project (`priority-<project>`, ADR-025). The retired key is
reported by ``check_schema`` under its own ``retired_priority`` category and
migrated by ``core.ripple.migrate_retired_priority``, wired into
``lit health-check --fix``.

The fixture reproduces the shape of the library the decision was made on:
21 papers — 5 deep-read (A A A A B), 7 skim (C C C C C B... ), 9 inbox with the
key present but null (what `lit add` wrote), 18 linked to one project and 3 to
none — plus two cases the real library does not have yet but the migration must
handle: a paper in TWO projects, and a paper that already carries a
`priority-<project>` grade which must never be overwritten.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.checks import check_schema, run_all_checks
from litman.core.correctors import reconcile_derived
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.locking import ensure_truth_locked
from litman.core.project_link import add_project
from litman.core.project_refs import LITERATURE_SUBDIR, REFERENCES_FILENAME
from litman.core.ripple import migrate_retired_priority

_MAIN = "pepcodec"
# Hyphenated on purpose: the key is the prefix plus the WHOLE name, so
# `priority-binder-design` must never be read as project "binder".
_SECOND = "binder-design"

# (id, status, retired priority value, projects, extra raw YAML lines)
_FIXTURE: tuple[tuple[str, str, str | None, list[str], str], ...] = (
    # --- 5 deep-read -------------------------------------------------------
    (
        "2024_Adams_Diffusion",
        "deep-read",
        "A",
        [_MAIN],
        "relevance-pepcodec: seeded the whole approach\n",
    ),
    ("2024_Baker_Hallucination", "deep-read", "A", [_MAIN], ""),
    # Two projects: the grade must land on BOTH links.
    (
        "2024_Chen_Codebook",
        "deep-read",
        "A",
        [_MAIN, _SECOND],
        "relevance-pepcodec: the tokenizer we copied\n"
        "relevance-binder-design: same trick, other target\n",
    ),
    # Already graded for the project it is linked to: setdefault must not
    # overwrite the C with the retired A.
    ("2024_Dunn_Latent", "deep-read", "A", [_MAIN], "priority-pepcodec: C\n"),
    ("2023_Evans_Folding", "deep-read", "B", [_MAIN], ""),
    # --- 7 skim ------------------------------------------------------------
    ("2023_Foster_Docking", "skim", "C", [_MAIN], ""),
    ("2023_Gupta_Binder", "skim", "C", [_MAIN], ""),
    ("2022_Hall_Sampling", "skim", "C", [_MAIN], ""),
    ("2022_Iyer_Scaffold", "skim", "C", [_MAIN], ""),
    # The three papers in no project: their grade has nowhere to go and is
    # dropped outright (ADR-025 decision 6).
    ("2022_Jones_Motif", "skim", "C", [], ""),
    ("2021_Kim_Rosetta", "skim", "C", [], ""),
    ("2021_Lopez_Assay", "skim", "B", [], ""),
    # --- 9 inbox, key present but NULL (what `lit add` wrote) ---------------
    ("2021_Moore_Screening", "inbox", None, [_MAIN], ""),
    ("2021_Nadar_Affinity", "inbox", None, [_MAIN], ""),
    ("2020_Ortiz_Helix", "inbox", None, [_MAIN], ""),
    ("2020_Patel_Macrocycle", "inbox", None, [_MAIN], ""),
    ("2020_Quinn_Stapled", "inbox", None, [_MAIN], ""),
    ("2019_Rossi_Permeability", "inbox", None, [_MAIN], ""),
    ("2019_Silva_Protease", "inbox", None, [_MAIN], ""),
    ("2019_Tanaka_Display", "inbox", None, [_MAIN], ""),
    ("2018_Usman_Library", "inbox", None, [_MAIN], ""),
)

# Categories this migration owns. The post-fix assertion is scoped to them
# rather than "run_all_checks is empty": a hand-built fixture vault also
# reports host-dependent findings (the pdf_viewer probe), and pinning those
# would make this test about the machine instead of about the migration.
_OWNED = {"retired_priority", "schema", "priority_orphan", "relevance_orphan"}


def _write_paper(
    vault: Path,
    paper_id: str,
    *,
    status: str,
    priority: str | None,
    projects: list[str],
    extra: str,
) -> None:
    """Write one metadata.yaml in the field order and formatting `lit add` uses.

    Raw text, not a YAML dump: M-3 compares the file byte for byte across the
    migration, so the fixture must be the shape a real vault holds — including
    a comment, which is exactly the kind of thing a careless rewrite loses.

    Written with an explicit ``newline="\n"``. ``staged_write`` encodes to
    bytes and never line-translates, so the migration always emits LF; a
    fixture written through the default universal-newline path would be CRLF
    on Windows, and ``read_text`` would then normalise both sides and hide the
    conversion the test exists to detect.
    """
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    year = paper_id[:4]
    projects_block = (
        "projects: []\n"
        if not projects
        else "projects:\n" + "".join(f"  - {p}\n" for p in projects)
    )
    priority_line = (
        "priority:\n" if priority is None else f"priority: {priority}\n"
    )
    with (paper_dir / "metadata.yaml").open(
        "w", encoding="utf-8", newline="\n"
    ) as fh:
        fh.write(
            f"# curated by hand, do not reformat\n"
        f"id: {paper_id}\n"
        f"title: {paper_id.split('_')[2]} study\n"
        f"authors:\n"
        f"  - {paper_id.split('_')[1]}, Jane\n"
        f"year: {year}\n"
        f"journal: Test J.\n"
        f"doi: 10.0/{paper_id}\n"
        f"arxiv-id:\n"
        f"github:\n"
        f"created-at: '2026-04-28T10:00:00+02:00'\n"
        f"updated-at: '2026-04-28T10:00:00+02:00'\n"
        f"{projects_block}"
        f"topics: []\n"
        f"methods: []\n"
        f"data: []\n"
        f"type: research\n"
        f"status: {status}\n"
        f"{priority_line}"
        f"read-date:\n"
        f"last-revisited:\n"
        f"related: []\n"
        f"contradicts: []\n"
        f"extends: []\n"
            f"code-clones: []\n"
            f"{extra}"
        )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4 stub\n")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A 21-paper vault in the pre-migration shape, with derived views built."""
    v = create_vault(tmp_path)
    for name in (_MAIN, _SECOND):
        project_dir = tmp_path / name
        project_dir.mkdir()
        add_project(v, name, project_dir)
    for paper_id, status, priority, projects, extra in _FIXTURE:
        _write_paper(
            v,
            paper_id,
            status=status,
            priority=priority,
            projects=projects,
            extra=extra,
        )
    reconcile_derived(v, project_refs=True)
    return v


def _metadata_texts(vault: Path) -> dict[str, str]:
    """Every metadata.yaml, decoded from RAW BYTES (no newline translation)."""
    return {
        d.name: (d / "metadata.yaml").read_bytes().decode("utf-8")
        for d in sorted((vault / "papers").iterdir())
    }


def _without_priority_lines(text: str) -> str:
    """Drop every `priority`/`priority-<project>` line, keep the rest verbatim."""
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if not line.startswith("priority")
    )


def _references(tmp_path: Path, project: str) -> str:
    return (
        tmp_path / project / LITERATURE_SUBDIR / REFERENCES_FILENAME
    ).read_text(encoding="utf-8")


def _assert_completed(result: Any) -> None:
    """The command ran to the end rather than crashing.

    CliRunner stores an unhandled traceback in ``.exception`` — but
    ``health-check``'s own ``sys.exit(1)`` (it gates on warnings, which this
    fixture always has) lands there too, so ``is None`` is too strict.
    Anything that is NOT a SystemExit is a crash.
    """
    assert result.exception is None or isinstance(
        result.exception, SystemExit
    ), result.exception
    assert "Summary:" in result.output or "All checks passed" in result.output


def _meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return next(p for p in list_papers(vault) if p["id"] == paper_id)


# ---------------------------------------------------------------------------
# M-1 — before the fix: one error per paper, triggered by the KEY
# ---------------------------------------------------------------------------


def test_m1_every_paper_holding_the_key_reports_once(vault: Path) -> None:
    issues = check_schema(vault, list_papers(vault))
    retired = [i for i in issues if i.category == "retired_priority"]

    assert len(retired) == 21
    assert {i.paper_id for i in retired} == {row[0] for row in _FIXTURE}
    assert all(i.severity == "error" for i in retired)
    # The nine `priority:` (null) papers are in there — the trigger is the key
    # being present, not the value being set.
    assert "2018_Usman_Library" in {i.paper_id for i in retired}
    # And nothing else fires: no per-project key exists yet to be out of range.
    assert [i for i in issues if i.category == "schema"] == []


def test_m1_the_finding_is_offered_as_a_one_enter_fix(vault: Path) -> None:
    """The report must say `--fix` handles it — that is the whole UX contract
    (a permanent migration nobody has to hand-edit 21 files for)."""
    from litman.commands.health import _fixable_categories

    assert "retired_priority" in _fixable_categories()
    issues = check_schema(vault, list_papers(vault))
    assert all(
        "--fix" in (i.hint or "")
        for i in issues
        if i.category == "retired_priority"
    )


# ---------------------------------------------------------------------------
# M-2 — after the fix: graded onto every link, null merely dropped, idempotent
# ---------------------------------------------------------------------------


def test_m2_migration_moves_grades_onto_the_links(vault: Path) -> None:
    assert migrate_retired_priority(vault) == 21

    papers = {p["id"]: p for p in list_papers(vault)}
    assert all("priority" not in p for p in papers.values())

    # Graded + linked -> the letter lands on the project key.
    assert papers["2024_Adams_Diffusion"]["priority-pepcodec"] == "A"
    assert papers["2023_Evans_Folding"]["priority-pepcodec"] == "B"
    assert papers["2022_Hall_Sampling"]["priority-pepcodec"] == "C"

    # Ungraded (`priority:` null) -> the key is simply gone. An ungraded link
    # is the ABSENT key: writing `priority-pepcodec: null` would invent a state
    # nothing reads and check_schema rejects.
    assert "priority-pepcodec" not in papers["2018_Usman_Library"]

    # No project -> nowhere to put the grade, so it is dropped outright.
    for pid in ("2022_Jones_Motif", "2021_Kim_Rosetta", "2021_Lopez_Assay"):
        assert not any(k.startswith("priority") for k in papers[pid])

    # Nothing anywhere carries a null grade.
    for paper in papers.values():
        for key, value in paper.items():
            if key.startswith("priority-"):
                assert value in {"A", "B", "C"}, (key, value)


def test_m2_vault_is_clean_afterwards(vault: Path) -> None:
    migrate_retired_priority(vault)
    issues = [
        i for i in run_all_checks(vault, list_papers(vault)) if i.category in _OWNED
    ]
    assert issues == []


def test_m2_second_run_changes_nothing(vault: Path) -> None:
    migrate_retired_priority(vault)
    after_first = _metadata_texts(vault)

    assert migrate_retired_priority(vault) == 0
    assert _metadata_texts(vault) == after_first


def test_m2_fix_through_the_cli_is_idempotent(vault: Path) -> None:
    """The real one-Enter path, not just the core function: `--fix` also
    re-locks TRUTH and regenerates the derived artifacts around the migration."""
    runner = CliRunner()

    first = runner.invoke(cli, ["health-check", "--fix", "--library", str(vault)])
    _assert_completed(first)
    assert "retired_priority: cleaned 21 items" in first.output, first.output
    after_first = _metadata_texts(vault)

    second = runner.invoke(cli, ["health-check", "--fix", "--library", str(vault)])
    # Checked BEFORE the "not in output" assertion below, which a crashed run
    # would satisfy for entirely the wrong reason: CliRunner puts the traceback
    # in .exception, never in .output.
    _assert_completed(second)
    assert "retired_priority" not in second.output, second.output
    assert _metadata_texts(vault) == after_first


# ---------------------------------------------------------------------------
# M-3 — every other byte of every metadata.yaml survives, `updated-at` included
# ---------------------------------------------------------------------------


def test_m3_files_are_untouched_apart_from_the_priority_lines(
    vault: Path,
) -> None:
    before = _metadata_texts(vault)
    migrate_retired_priority(vault)
    after = _metadata_texts(vault)

    assert set(before) == set(after)
    for paper_id, before_text in before.items():
        assert _without_priority_lines(after[paper_id]) == _without_priority_lines(
            before_text
        ), paper_id
    # Control. The loop above strips every priority line from BOTH sides, so
    # it passes just as happily against a migration that did nothing at all.
    # These assert the migration actually acted — and they must look at
    # `after`, which is the half the loop cannot see.
    assert "priority: A\n" in before["2024_Adams_Diffusion"]
    assert "priority: A\n" not in after["2024_Adams_Diffusion"]
    assert "priority-pepcodec: A\n" in after["2024_Adams_Diffusion"]


def test_m3_updated_at_is_not_bumped(vault: Path) -> None:
    """A migration is not a user edit. `updated-at` answers "when did I last
    change this paper" and ranks the browse list; moving a value the tool
    itself retired must not reshuffle the whole library to the top."""
    before = {p["id"]: p["updated-at"] for p in list_papers(vault)}
    migrate_retired_priority(vault)
    after = {p["id"]: p["updated-at"] for p in list_papers(vault)}
    assert after == before
    assert set(before.values()) == {"2026-04-28T10:00:00+02:00"}


# ---------------------------------------------------------------------------
# M-4 is only PARTIALLY covered here, deliberately.
#
# The spec's M-4 is "REFERENCES.md 重建后分组与迁移前完全一致" — the priority
# GROUPING must survive the migration. It cannot yet: `_group_by_priority`
# (core/project_refs.py) still reads the retired paper-level key, so after
# `--fix` every paper falls into the "Unprioritized" bucket. Switching that
# renderer to `priority-<project>` is milestone 3.2, and the grouping
# assertion belongs with it.
#
# What IS asserted below is the half this milestone owns: the reference lists
# are rebuilt by the migration, and rebuilt from FULL metadata. Writing a
# grouping assertion that passes today would mean weakening it to something
# 3.2 must then re-tighten, which is worse than an explicit gap.
# ---------------------------------------------------------------------------


def test_references_are_rebuilt_from_full_metadata(
    vault: Path, tmp_path: Path
) -> None:
    """`reconcile_derived` must reload full metadata: REFERENCES.md renders the
    authored `relevance-<project>` text, which an INDEX projection does not
    carry — handing one in would blank every annotation in the file.
    """
    before_main = _references(tmp_path, _MAIN)
    before_second = _references(tmp_path, _SECOND)

    migrate_retired_priority(vault)

    after_main = _references(tmp_path, _MAIN)
    after_second = _references(tmp_path, _SECOND)

    # Same papers listed, in both projects.
    for row in _FIXTURE:
        paper_id, _status, _pr, projects, _extra = row
        assert (f"[[{paper_id}]]" in after_main) is (_MAIN in projects), paper_id
        assert (f"[[{paper_id}]]" in after_second) is (_SECOND in projects), paper_id

    # Authored relevance prose survives in both files.
    for text in (before_main, after_main):
        assert "seeded the whole approach" in text
        assert "the tokenizer we copied" in text
    for text in (before_second, after_second):
        assert "same trick, other target" in text


# ---------------------------------------------------------------------------
# M-5 — two projects get two keys; an existing grade is never overwritten
# ---------------------------------------------------------------------------


def test_m5_a_paper_in_two_projects_is_graded_for_both(vault: Path) -> None:
    migrate_retired_priority(vault)
    paper = _meta(vault, "2024_Chen_Codebook")
    assert paper["priority-pepcodec"] == "A"
    # The hyphenated name proves the key is built by prefixing the WHOLE
    # project name, and read back by stripping the prefix — never by splitting.
    assert paper["priority-binder-design"] == "A"


def test_m5_an_existing_grade_is_never_overwritten(vault: Path) -> None:
    """setdefault, not assignment: the user already decided this paper is a C
    for pepcodec, and the retired global A must not undo that."""
    before = _meta(vault, "2024_Dunn_Latent")
    assert before["priority"] == "A"
    assert before["priority-pepcodec"] == "C"

    migrate_retired_priority(vault)

    after = _meta(vault, "2024_Dunn_Latent")
    assert after["priority-pepcodec"] == "C"
    assert "priority" not in after


# ---------------------------------------------------------------------------
# The folder IS the identity: `id` is a declared field, not a key
# ---------------------------------------------------------------------------


def _bare_paper(vault: Path, folder: str, declared_id: str, priority: str) -> Path:
    """A paper folder whose declared `id` need not match its folder name."""
    d = vault / "papers" / folder
    d.mkdir(parents=True, exist_ok=True)
    with (d / "metadata.yaml").open("w", encoding="utf-8", newline="\n") as f:
        f.write(
            f"id: {declared_id}\n"
            "title: T\n"
            "year: 2024\n"
            "projects:\n  - pepcodec\n"
            "status: skim\n"
            f"priority: {priority}\n"
            "created-at: '2026-04-28T10:00:00+02:00'\n"
            "updated-at: '2026-04-28T10:00:00+02:00'\n"
        )
    (d / "paper.pdf").write_bytes(b"%PDF-1.4 stub\n")
    return d


def test_two_folders_declaring_one_id_are_both_migrated(vault: Path) -> None:
    """A cloud-sync conflict copy leaves two folders holding the same `id`.

    The vault is an rclone target (invariant #9) and `core/document.py` already
    anticipates conflict copies, so `id` is a declared FIELD, not a key. Keying
    the rewrite by it wrote one folder's file twice, left the other still
    holding the retired key, over-reported the count, and made the second
    `--fix` raise KeyError out of the one-Enter migration.
    """
    _bare_paper(vault, "2024_Zeta_One", "2024_Zeta_One", "A")
    _bare_paper(vault, "2024_Zeta_One-conflict", "2024_Zeta_One", "C")

    n = migrate_retired_priority(vault)
    assert n == 23  # the 21 fixture papers + both conflict folders

    for folder, grade in (
        ("2024_Zeta_One", "A"),
        ("2024_Zeta_One-conflict", "C"),
    ):
        text = (vault / "papers" / folder / "metadata.yaml").read_text(
            encoding="utf-8"
        )
        assert "priority:" not in text, folder
        assert f"priority-pepcodec: {grade}" in text, folder

    # Idempotence (the spec's red line) survives the collision.
    assert migrate_retired_priority(vault) == 0


def test_a_declared_id_that_disagrees_with_its_folder_still_migrates(
    vault: Path,
) -> None:
    """Nothing enforces `id` == folder name at write time; `--fix` must not
    resolve a path through it and then blame the file for being corrupt."""
    _bare_paper(vault, "2024_Yankee_Two", "2024_Wrong_Name", "B")

    assert migrate_retired_priority(vault) == 22

    text = (vault / "papers" / "2024_Yankee_Two" / "metadata.yaml").read_text(
        encoding="utf-8"
    )
    assert "priority:" not in text
    assert "priority-pepcodec: B" in text
    assert not (vault / "papers" / "2024_Wrong_Name").exists()


def test_a_scalar_projects_field_is_never_iterated(vault: Path) -> None:
    """metadata is schema-less (invariant #7), so a hand-edit can leave
    `projects: pepcodec`. Iterating a string yields one key PER CHARACTER
    (`priority-p`, `priority-e`, ...) and check_priority_orphan cannot catch it
    — `set("pepcodec")` contains every one of those letters. Refuse the paper
    instead, mirroring `_ripple_removals`' scalar guard."""
    d = vault / "papers" / "2024_Xray_Three"
    d.mkdir(parents=True)
    with (d / "metadata.yaml").open("w", encoding="utf-8", newline="\n") as f:
        f.write(
            "id: 2024_Xray_Three\ntitle: T\nyear: 2024\n"
            "projects: pepcodec\nstatus: skim\npriority: A\n"
            "created-at: '2026-04-28T10:00:00+02:00'\n"
            "updated-at: '2026-04-28T10:00:00+02:00'\n"
        )
    (d / "paper.pdf").write_bytes(b"%PDF-1.4 stub\n")

    assert migrate_retired_priority(vault) == 21  # the fixture papers only

    text = (d / "metadata.yaml").read_text(encoding="utf-8")
    assert "priority: A" in text  # untouched, so nothing was invented
    assert "priority-p" not in text
    # Not a silent skip (invariant #14): the finding survives, because the
    # paper still holds the retired key and check_schema still reports it.
    assert any(
        i.category == "retired_priority" and i.paper_id == "2024_Xray_Three"
        for i in check_schema(vault, list_papers(vault))
    )


def test_a_leftover_staging_dir_does_not_wedge_every_future_fix(
    vault: Path,
) -> None:
    """A fixed op_id plus `mkdir(exist_ok=False)` meant one unrecoverable
    staging dir made `--fix` raise FileExistsError forever after."""
    stale = vault / ".litman-staging" / "migrate-retired-priority"
    stale.mkdir(parents=True)
    (stale / "leftover").write_text("x", encoding="utf-8")

    assert migrate_retired_priority(vault) == 21


# ---------------------------------------------------------------------------
# M-6 — the write goes through staged_write, so a locked TRUTH file is fine
# ---------------------------------------------------------------------------


def test_m6_migrates_through_the_read_only_truth_lock(vault: Path) -> None:
    """metadata.yaml is chmod read-only (on Windows always, and after any
    Tier-2 run everywhere). A bare `Path.write_text` cannot rewrite it — only
    the staged_write + relock path can.
    """
    assert ensure_truth_locked(vault) > 0
    locked = vault / "papers" / "2024_Adams_Diffusion" / "metadata.yaml"

    # Positive control: prove the lock is actually enforced for this user
    # before concluding anything from the migration succeeding.
    try:
        with locked.open("a", encoding="utf-8"):
            pass
    except PermissionError:
        pass
    else:
        pytest.skip("TRUTH lock not enforced for this user (root?)")

    assert migrate_retired_priority(vault) == 21

    assert "priority-pepcodec: A" in locked.read_text(encoding="utf-8")
    # Still locked afterwards — the migration must not leave TRUTH writable.
    with pytest.raises(PermissionError), locked.open("a", encoding="utf-8"):
        pass


def test_m6_no_staging_leftovers(vault: Path) -> None:
    """One staged_write for all 21 files plus INDEX.json: it commits or it
    rolls back, and it leaves no op directory behind either way."""
    migrate_retired_priority(vault)
    staging = vault / ".litman-staging"
    assert not staging.exists() or list(staging.iterdir()) == []
