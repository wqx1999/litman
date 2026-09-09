"""The cascade rewrites are keyed by FOLDER, never by the declared ``id``.

``id`` is an ordinary metadata field. Nothing enforces that it is unique or
that it matches its directory, and the vault is an rclone target (invariant
#9) where a sync conflict leaves two folders holding one id — a case
``core/document.py`` already anticipates. Building the write path out of
``id`` meant one of those folders was rewritten twice while the other kept
the old value, and the reported count came from the number of dicts rather
than the number of files.

``migrate_retired_priority`` was fixed for exactly this (its own duplicate-id
tests live in test_priority_migration.py); these are the same tests for the
two ripple helpers behind ``lit taxonomy merge/rename/rm`` and ``lit project
rename/rm``, plus the derived-artifact equivalence that a duplicate id must
not break — ``test_write_op_equivalence.py`` pins that invariant, but its
fixture has no id/folder mismatch, so this shape never reaches it.

Note on the projection: ``load_index_papers`` refuses an INDEX whose id SET
is smaller than its paper list (``core/views.py``), so any duplicate-id vault
fails the freshness probe and ``papers_for_index`` falls back to a full scan.
That is why nothing here can exercise the no-read fast path — the healthy
vault owns that branch, and it is pinned in ``test_write_op_equivalence.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.correctors import reconcile_derived
from litman.core.library import create_vault
from litman.core.portable_link import is_portable_link


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _paper_folder(
    vault: Path,
    folder: str,
    declared_id: str,
    *,
    topics: list[str] | None = None,
    projects: list[str] | None = None,
    raw_topics: str | None = None,
    extra: dict[str, str] | None = None,
) -> Path:
    """A paper folder whose declared ``id`` need not match its folder name."""
    paper_dir = vault / "papers" / folder
    paper_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"id: {declared_id}",
        f"title: T {folder}",
        "year: 2024",
        "status: skim",
        "type: research",
    ]
    if raw_topics is not None:
        lines.append(f"topics: {raw_topics}")
    else:
        lines.append("topics:")
        lines += [f"  - {t}" for t in topics or []]
    lines.append("projects:")
    lines += [f"  - {p}" for p in projects or []]
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value}")
    lines += [
        "created-at: '2026-04-28T10:00:00+02:00'",
        "updated-at: '2026-04-28T10:00:00+02:00'",
    ]
    with (paper_dir / "metadata.yaml").open(
        "w", encoding="utf-8", newline="\n"
    ) as f:
        f.write("\n".join(lines) + "\n")
    return paper_dir


def _meta(vault: Path, folder: str) -> str:
    return (vault / "papers" / folder / "metadata.yaml").read_text(
        encoding="utf-8"
    )


_CONFLICT = ("2024_Zeta_One", "2024_Zeta_One-conflict")


def _seed_conflict_pair(vault: Path, **fields: Any) -> None:
    """Two folders, one declared id — a cloud-sync conflict copy."""
    for folder in _CONFLICT:
        _paper_folder(vault, folder, "2024_Zeta_One", **fields)


def _ok(result: Any) -> Any:
    assert result.exit_code == 0, result.output
    return result


def _derived_state(vault: Path) -> dict[str, Any]:
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    index.pop("generated_at", None)
    views: dict[str, str] = {}
    root = vault / "views"
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if is_portable_link(p):
            views[rel] = p.resolve().name
        elif p.is_dir():
            views[rel] = "<dir>"
    return {"index": index, "views": views}


def _assert_derived_equals_full_rebuild(vault: Path) -> None:
    """What the command left behind must equal a wholesale rebuild of TRUTH.

    Mirrors ``test_write_op_equivalence.py``'s assertion. The incremental
    views update is driven by a per-paper before/after snapshot, and a folder
    the projection cannot account for is one the ripple reads itself — so the
    "after" side has to come from what the ripple touched, not from the
    projection the caller still holds.
    """
    incremental = _derived_state(vault)
    reconcile_derived(vault)  # papers=None → full scan + full views rebuild
    assert incremental == _derived_state(vault)


# ---------------------------------------------------------------------------
# _ripple_replacements
# ---------------------------------------------------------------------------


def test_taxonomy_merge_rewrites_both_folders_of_a_conflict_pair(
    vault: Path,
) -> None:
    _seed_conflict_pair(vault, topics=["AMP"])
    runner = CliRunner()
    _ok(runner.invoke(
        cli, ["taxonomy", "add", "topics", "AMP", "--library", str(vault)]
    ))

    result = _ok(runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "AMP", "--into", "AMP-design",
         "--yes", "--library", str(vault)],
    ))

    # Two files, not one dict counted twice.
    assert "Updated 2 paper" in result.output
    for folder in _CONFLICT:
        assert "AMP-design" in _meta(vault, folder), folder
        assert "- AMP\n" not in _meta(vault, folder), folder


def test_taxonomy_rename_rewrites_both_folders_of_a_conflict_pair(
    vault: Path,
) -> None:
    _seed_conflict_pair(vault, topics=["AMP"])
    runner = CliRunner()
    _ok(runner.invoke(
        cli, ["taxonomy", "add", "topics", "AMP", "--library", str(vault)]
    ))

    result = _ok(runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "AMP", "antimicrobial",
         "--library", str(vault)],
    ))

    assert "Updated 2 paper" in result.output
    for folder in _CONFLICT:
        assert "antimicrobial" in _meta(vault, folder), folder
        assert "- AMP\n" not in _meta(vault, folder), folder


def test_project_rename_carries_both_folders_per_project_keys(
    vault: Path, tmp_path: Path
) -> None:
    """``lit project rename`` also moves ``relevance-<old>`` and
    ``priority-<old>``. Missing the second folder stranded both keys, which
    ``check_relevance_orphan`` / ``check_priority_orphan`` then flagged."""
    proj = tmp_path / "pepforge_dir"
    proj.mkdir()
    runner = CliRunner()
    _ok(runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj),
         "--library", str(vault)],
    ))
    _seed_conflict_pair(
        vault,
        projects=["pepforge"],
        extra={"relevance-pepforge": "core", "priority-pepforge": "A"},
    )

    result = _ok(runner.invoke(
        cli,
        ["project", "rename", "pepforge", "pepcodec",
         "--library", str(vault)],
    ))

    assert "Updated 2 paper" in result.output
    for folder in _CONFLICT:
        text = _meta(vault, folder)
        assert "- pepcodec\n" in text, folder
        assert "relevance-pepcodec: core" in text, folder
        assert "priority-pepcodec: A" in text, folder
        assert "pepforge" not in text, folder


# ---------------------------------------------------------------------------
# _ripple_removals
# ---------------------------------------------------------------------------


def test_taxonomy_rm_clears_both_folders_of_a_conflict_pair(
    vault: Path,
) -> None:
    _seed_conflict_pair(vault, topics=["AMP"])
    runner = CliRunner()
    _ok(runner.invoke(
        cli, ["taxonomy", "add", "topics", "AMP", "--library", str(vault)]
    ))

    result = _ok(runner.invoke(
        cli,
        ["taxonomy", "rm", "topics", "AMP", "--yes", "--library", str(vault)],
    ))

    assert "Untagged 2 paper" in result.output
    for folder in _CONFLICT:
        assert "- AMP\n" not in _meta(vault, folder), folder


def test_project_rm_untags_both_folders_and_drops_their_keys(
    vault: Path, tmp_path: Path
) -> None:
    proj = tmp_path / "pepforge_dir"
    proj.mkdir()
    runner = CliRunner()
    _ok(runner.invoke(
        cli,
        ["project", "add", "pepforge", "--path", str(proj),
         "--library", str(vault)],
    ))
    _seed_conflict_pair(
        vault,
        projects=["pepforge"],
        extra={"relevance-pepforge": "core", "priority-pepforge": "A"},
    )

    result = _ok(runner.invoke(
        cli,
        ["project", "rm", "pepforge", "--yes", "--library", str(vault)],
    ))

    assert "Untagged 2 paper" in result.output
    for folder in _CONFLICT:
        assert "pepforge" not in _meta(vault, folder), folder


# ---------------------------------------------------------------------------
# The derived artifacts have to survive it too
# ---------------------------------------------------------------------------


def test_taxonomy_rename_on_a_conflict_pair_keeps_views_rebuilt(
    vault: Path,
) -> None:
    """Rewriting the right files is half the job: ``views/`` has to follow.

    The incremental update is a per-paper before/after delta, and the "after"
    dict for a conflict copy is one the ripple read itself. Taking it from the
    caller's projection instead compares a snapshot with itself, so
    ``by-topic/<old>`` survived and ``by-topic/<new>`` was never created —
    klass-A drift written by a blessed command.
    """
    _seed_conflict_pair(vault, topics=["AMP"])
    runner = CliRunner()
    _ok(runner.invoke(
        cli, ["taxonomy", "add", "topics", "AMP", "--library", str(vault)]
    ))
    reconcile_derived(vault, project_refs=False)

    _ok(runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "AMP", "antimicrobial",
         "--library", str(vault)],
    ))

    assert (vault / "views" / "by-topic" / "antimicrobial").is_dir()
    assert not (vault / "views" / "by-topic" / "AMP").exists()
    _assert_derived_equals_full_rebuild(vault)


def test_taxonomy_rm_on_a_conflict_pair_keeps_views_rebuilt(
    vault: Path,
) -> None:
    _seed_conflict_pair(vault, topics=["AMP"])
    runner = CliRunner()
    _ok(runner.invoke(
        cli, ["taxonomy", "add", "topics", "AMP", "--library", str(vault)]
    ))
    reconcile_derived(vault, project_refs=False)

    _ok(runner.invoke(
        cli,
        ["taxonomy", "rm", "topics", "AMP", "--yes", "--library", str(vault)],
    ))

    assert not (vault / "views" / "by-topic" / "AMP").exists()
    _assert_derived_equals_full_rebuild(vault)


def test_only_one_half_of_a_conflict_pair_tagged_still_rebuilds_views(
    vault: Path,
) -> None:
    """The asymmetric pair, which is where a per-paper delta cannot work.

    One views symlink, two truths: only the tagged half is rewritten, so
    picking an "after" dict by id picks whichever folder sorts last — right
    when that is the tagged one, wrong when it is not, i.e. a coin flip on a
    real vault. The delta is therefore skipped whenever the walk came back
    with anything other than the caller's own objects, and views/ is rebuilt
    wholesale instead. Seeded so the tagged half sorts FIRST, the arrangement
    that a last-wins delta gets wrong.
    """
    _paper_folder(vault, "2024_Zeta_One", "2024_Zeta_One", topics=["AMP"])
    _paper_folder(vault, "2024_Zeta_One-conflict", "2024_Zeta_One", topics=[])
    runner = CliRunner()
    _ok(runner.invoke(
        cli, ["taxonomy", "add", "topics", "AMP", "--library", str(vault)]
    ))
    reconcile_derived(vault, project_refs=False)
    assert (vault / "views" / "by-topic" / "AMP").is_dir()

    _ok(runner.invoke(
        cli,
        ["taxonomy", "rm", "topics", "AMP", "--yes", "--library", str(vault)],
    ))

    assert not (vault / "views" / "by-topic" / "AMP").exists()
    _assert_derived_equals_full_rebuild(vault)


def test_a_paper_dir_whose_metadata_vanished_does_not_crash_the_cascade(
    vault: Path,
) -> None:
    """An id the walk never yields is the other way the delta goes wrong.

    ``load_index_papers``' freshness probe counts paper DIRECTORIES, so a
    directory whose ``metadata.yaml`` disappeared after INDEX was rendered
    still looks fresh: the projection carries the id, the walk skips the
    folder. The delta would then carry a paper that is no longer there,
    leaving its view links standing — so this is the second thing the
    wholesale fallback covers.
    """
    _paper_folder(vault, "2024_Alpha_One", "2024_Alpha_One", topics=["AMP"])
    _paper_folder(vault, "2024_Beta_Two", "2024_Beta_Two", topics=["AMP"])
    runner = CliRunner()
    _ok(runner.invoke(
        cli, ["taxonomy", "add", "topics", "AMP", "--library", str(vault)]
    ))
    reconcile_derived(vault, project_refs=False)
    (vault / "papers" / "2024_Beta_Two" / "metadata.yaml").unlink()

    result = runner.invoke(
        cli,
        ["taxonomy", "rm", "topics", "AMP", "--yes", "--library", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "- AMP\n" not in _meta(vault, "2024_Alpha_One")
    _assert_derived_equals_full_rebuild(vault)


# ---------------------------------------------------------------------------
# The refusal names the folder too
# ---------------------------------------------------------------------------


def test_scalar_field_refusal_names_the_folder_not_the_declared_id(
    vault: Path,
) -> None:
    """metadata is schema-less (invariant #7), so a hand-edit can leave
    ``topics: AMP``. The ripple refuses that paper rather than rippling a
    string character by character — and the message has to name the folder it
    actually read, since resolving through the declared id used to point the
    user at a directory that does not exist.
    """
    _paper_folder(vault, "2024_Bad_Folder", "2024_Somewhere_Else",
                  raw_topics="AMP")
    runner = CliRunner()
    _ok(runner.invoke(
        cli, ["taxonomy", "add", "topics", "AMP", "--library", str(vault)]
    ))

    result = runner.invoke(
        cli,
        ["taxonomy", "rm", "topics", "AMP", "--yes", "--library", str(vault)],
    )

    assert result.exit_code != 0
    message = re.sub(r"\s+", " ", str(result.exception))
    assert "papers/2024_Bad_Folder/metadata.yaml" in message
    assert "2024_Somewhere_Else" not in message
    assert not (vault / "papers" / "2024_Somewhere_Else").exists()
