"""Tests for ``lit rm`` (M23.1 unified delete flow + M24 deletion tags).

Covers happy-path delete, the relationship-count confirmation, cascade
teardown of external→A edges (literature ref incl. reverse fields, code 1:1
orphan / 1:N keep, project symlink + REFERENCES re-render), the M24
``[[A]] (deleted)`` annotation of referencing notes/discussion (idempotent,
both soft + purge), removal of the M23 deletion log (no ``.deletion-log.jsonl``
generated), ``-y`` non-interactive force-delete, prompt abort, INDEX/views
refresh, and removal of ``--cascade``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import trash as trash_mod
from litman.core.library import create_vault
from litman.core.locking import lock_truth_file
from litman.core.trash import TRASH_DIRNAME, TRASH_MAX_ENTRIES, list_trash
from litman.exceptions import PaperNotFoundError

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": fields.get("title", paper_id),
        "authors": fields.get("authors", ["Doe, Jane"]),
        "year": fields.get("year", 2024),
        "journal": fields.get("journal", "Test J."),
        "doi": fields.get("doi", f"10.0/{paper_id}"),
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": fields.get("projects", []),
        "topics": fields.get("topics", []),
        "methods": fields.get("methods", []),
        "data": fields.get("data", []),
        "type": fields.get("type", "research"),
        "status": fields.get("status", "inbox"),
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
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)
    notes = fields.get("notes")
    if notes is not None:
        (paper_dir / "notes.md").write_text(notes, encoding="utf-8")
    discussion = fields.get("discussion")
    if discussion is not None:
        (paper_dir / "discussion.md").write_text(discussion, encoding="utf-8")


def _make_fake_repo(
    vault: Path, repo_name: str, *, papers: list[str], upstream: str = "file:///fake"
) -> Path:
    """Materialize codes/<repo>/repo/ + repo-meta.yaml for cascade tests."""
    repo_root = vault / "codes" / repo_name
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "repo").mkdir(exist_ok=True)
    (repo_root / "repo" / "README.md").write_text("# fake\n", encoding="utf-8")
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    payload = {
        "name": repo_name,
        "upstream": upstream,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "papers": list(papers),
        "framework": None,
        "runs-on": None,
        "status": None,
    }
    with (repo_root / "repo-meta.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)
    return repo_root


def _read_repo_meta(vault: Path, repo_name: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "codes" / repo_name / "repo-meta.yaml").read_text()
    )


def _read_meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ===========================================================================
# Happy path
# ===========================================================================


def test_rm_simple(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Foo_Bar").exists()
    assert "Trashed 2024_Foo_Bar" in result.output


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_rm_succeeds_on_locked_paper(vault: Path) -> None:
    """`lit rm` whole-dir renames into .trash and ignores file read-only bits (AC#2)."""
    _write_paper(vault, "2024_Foo_Bar")
    paper_dir = vault / "papers" / "2024_Foo_Bar"
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    lock_truth_file(paper_dir / "metadata.yaml")
    lock_truth_file(paper_dir / "paper.pdf")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not paper_dir.exists()


def test_rm_refreshes_index(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", topics=["alpha"])
    _write_paper(vault, "2024_Other", topics=["beta"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output

    payload = json.loads((vault / "INDEX.json").read_text())
    assert payload["n_papers"] == 1
    assert payload["papers"][0]["id"] == "2024_Other"


def test_rm_rebuilds_views(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", topics=["alpha"])
    _write_paper(vault, "2024_Other", topics=["alpha"])
    runner = CliRunner()
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert (vault / "views/by-topic/alpha/2024_Foo_Bar").is_symlink()

    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Stale symlink for the deleted paper is gone.
    assert not (vault / "views/by-topic/alpha/2024_Foo_Bar").exists()
    # The other paper's symlink is still there.
    assert (vault / "views/by-topic/alpha/2024_Other").is_symlink()


def test_rm_does_not_touch_unrelated_papers(vault: Path) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(vault, "2024_Untouched", topics=["x"])
    untouched_meta_path = vault / "papers/2024_Untouched/metadata.yaml"
    before_text = untouched_meta_path.read_text()

    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert untouched_meta_path.read_text() == before_text


# ===========================================================================
# Relationship-count confirmation + cascade teardown (literature ref)
# ===========================================================================


def test_rm_no_relations_deletes_after_confirm(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--library", str(vault)], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Foo_Bar").exists()
    # No relationship banner when there are no links.
    assert "is linked with" not in result.output


def test_rm_has_relations_reports_total_and_pointer(vault: Path) -> None:
    _write_paper(vault, "2024_Target", related=["2024_B"], extends=["2024_C"])
    _write_paper(vault, "2024_B", related=["2024_Target"])
    _write_paper(vault, "2024_C", extended_by=["2024_Target"])
    runner = CliRunner()
    # Default N — refuse, touch nothing.
    result = runner.invoke(
        cli, ["rm", "2024_Target", "--library", str(vault)], input="\n"
    )
    assert result.exit_code == 0, result.output
    # 2 ref opposites, total = 2.
    assert "is linked with 2 entries" in result.output
    assert "lit show 2024_Target" in result.output
    # Refused (default N) → nothing changed.
    assert (vault / "papers" / "2024_Target").is_dir()
    assert _read_meta(vault, "2024_B")["related"] == ["2024_Target"]
    # No entry enumeration — only the total + pointer.
    assert "2024_B" not in result.output
    assert "2024_C" not in result.output


def test_rm_refuse_is_zero_mutation(vault: Path, tmp_path: Path) -> None:
    # Refuse must touch NOTHING across all three link substrates
    # (literature ref, code, project) — spec red line "拒绝则零改动".
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "projects: {}", f"projects:\n  myproj: {project_dir}"
        ),
        encoding="utf-8",
    )
    _write_paper(
        vault,
        "2024_Target",
        related=["2024_Holder"],
        code_clones=["SoloLib"],
        projects=["myproj"],
    )
    _write_paper(vault, "2024_Holder", related=["2024_Target"])
    _make_fake_repo(vault, "SoloLib", papers=["2024_Target"])
    runner = CliRunner()
    link_res = runner.invoke(
        cli, ["link", "2024_Target", "--project", "myproj", "--library", str(vault)]
    )
    assert link_res.exit_code == 0, link_res.output

    holder_before = (vault / "papers/2024_Holder/metadata.yaml").read_text()
    repo_meta_before = _read_repo_meta(vault, "SoloLib")
    refs_before = (project_dir / "litman_reflib" / "REFERENCES.md").read_text()

    result = runner.invoke(
        cli, ["rm", "2024_Target", "--library", str(vault)], input="n\n"
    )
    assert result.exit_code == 0, result.output
    # Paper itself untouched.
    assert (vault / "papers" / "2024_Target").is_dir()
    # Literature ref opposite untouched (byte-identical).
    assert (
        vault / "papers/2024_Holder/metadata.yaml"
    ).read_text() == holder_before
    # Code: repo dir kept, repo-meta.papers unchanged.
    assert (vault / "codes" / "SoloLib").is_dir()
    assert _read_repo_meta(vault, "SoloLib")["papers"] == repo_meta_before["papers"]
    # Project: symlink + REFERENCES untouched.
    assert (project_dir / "litman_reflib" / "2024_Target").is_symlink()
    assert (project_dir / "litman_reflib" / "REFERENCES.md").read_text() == refs_before
    # No log row written.
    assert not (vault / ".deletion-log.jsonl").exists()


def test_rm_cascade_clears_symmetric_related(vault: Path) -> None:
    _write_paper(vault, "2024_Target", related=["2024_Holder", "2022_Other"])
    _write_paper(
        vault, "2024_Holder",
        related=["2024_Target"],
        extends=["2022_Other"],
    )
    _write_paper(vault, "2022_Other")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Target").exists()

    holder = _read_meta(vault, "2024_Holder")
    # Holder.related drops Target; the unrelated extends edge is untouched.
    assert holder["related"] == []
    assert holder["extends"] == ["2022_Other"]
    assert holder["updated-at"] != "2026-04-28T10:00:00+02:00"
    assert "Cleared references in" in result.output


def test_rm_cascade_clears_extends_reverse_field(vault: Path) -> None:
    # Target EXTENDS X (forward on Target). After M23.0 symmetry, X holds
    # extended-by:[Target]; deleting Target must drop it from X.extended-by.
    _write_paper(vault, "2024_Target", extends=["2024_X"])
    _write_paper(vault, "2024_X", extended_by=["2024_Target"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, "2024_X")["extended-by"] == []


def test_rm_cascade_clears_inbound_extends(vault: Path) -> None:
    # Someone EXTENDS Target: X.extends:[Target], Target.extended-by:[X].
    # Deleting Target must drop Target from X.extends (the inbound edge,
    # reachable via Target's own reverse field).
    _write_paper(vault, "2024_Target", extended_by=["2024_X"])
    _write_paper(vault, "2024_X", extends=["2024_Target"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, "2024_X")["extends"] == []


def test_rm_cascade_clears_contradicts_pair(vault: Path) -> None:
    # C contradicts Target: Target.contradicted-by:[C], C.contradicts:[Target].
    _write_paper(vault, "2024_Target", contradicted_by=["2024_C"])
    _write_paper(vault, "2024_C", contradicts=["2024_Target"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, "2024_C")["contradicts"] == []


def test_rm_seals_own_fields_into_trash(vault: Path) -> None:
    # A's own forward+reverse fields are NOT cleared — they ride into trash.
    _write_paper(vault, "2024_Target", related=["2024_B"], extends=["2024_C"])
    _write_paper(vault, "2024_B", related=["2024_Target"])
    _write_paper(vault, "2024_C", extended_by=["2024_Target"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    trash_meta = next(
        (vault / TRASH_DIRNAME).glob("2024_Target-*/metadata.yaml")
    )
    sealed = _yaml.load(trash_meta.read_text())
    assert sealed["related"] == ["2024_B"]
    assert sealed["extends"] == ["2024_C"]


# ===========================================================================
# Cascade teardown — code clones
# ===========================================================================


def test_rm_cascade_code_1to1_hard_deletes_orphan(vault: Path) -> None:
    _write_paper(vault, "2024_Target", code_clones=["RepoX"])
    _make_fake_repo(
        vault, "RepoX", papers=["2024_Target"], upstream="https://x/repo.git"
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Orphan repo dir hard-deleted.
    assert not (vault / "codes" / "RepoX").exists()
    # Sidecar records the upstream url for M23.2 re-clone.
    sidecar = next((vault / TRASH_DIRNAME).glob("2024_Target-*.meta.yaml"))
    data = _yaml.load(sidecar.read_text())
    assert data["orphan_repos"] == {"RepoX": "https://x/repo.git"}
    assert "Removed 1 orphan repo" in result.output


def test_rm_cascade_code_1toN_only_unbinds(vault: Path) -> None:
    _write_paper(vault, "2024_Target", code_clones=["SharedLib"])
    _write_paper(vault, "2024_Keeper", code_clones=["SharedLib"])
    _make_fake_repo(
        vault, "SharedLib", papers=["2024_Target", "2024_Keeper"]
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Repo dir kept (still bound by Keeper).
    assert (vault / "codes" / "SharedLib").is_dir()
    meta = _read_repo_meta(vault, "SharedLib")
    assert meta["papers"] == ["2024_Keeper"]
    # Not recorded as orphan.
    sidecar = next((vault / TRASH_DIRNAME).glob("2024_Target-*.meta.yaml"))
    data = _yaml.load(sidecar.read_text())
    assert data["orphan_repos"] == {}
    assert "Unbound from 1 repo" in result.output


# ===========================================================================
# Cascade teardown — project links
# ===========================================================================


def test_rm_cascade_project_symlink_and_references(
    vault: Path, tmp_path: Path
) -> None:
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    # Register the project in lit-config.yaml (replace the empty default).
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "projects: {}", f"projects:\n  myproj: {project_dir}"
        ),
        encoding="utf-8",
    )
    _write_paper(vault, "2024_Target", projects=["myproj"])
    runner = CliRunner()
    # Link first so the symlink + REFERENCES.md exist.
    link_res = runner.invoke(
        cli, ["link", "2024_Target", "--project", "myproj", "--library", str(vault)]
    )
    assert link_res.exit_code == 0, link_res.output
    assert (project_dir / "litman_reflib" / "2024_Target").is_symlink()
    refs_before = (project_dir / "litman_reflib" / "REFERENCES.md").read_text()
    assert "2024_Target" in refs_before

    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Symlink removed, REFERENCES re-rendered without the paper.
    assert not (project_dir / "litman_reflib" / "2024_Target").exists()
    refs_after = (project_dir / "litman_reflib" / "REFERENCES.md").read_text()
    assert "2024_Target" not in refs_after
    assert "Unlinked from 1 project" in result.output


# ===========================================================================
# Wikilink: M24 annotate referencing notes/discussion with `(deleted)`
# ===========================================================================


def test_rm_annotates_referencing_notes(vault: Path) -> None:
    # AC87: every referencing [[A]] in notes becomes [[A]] (deleted).
    _write_paper(vault, "2024_Target", related=["2024_Other"])
    _write_paper(
        vault, "2024_Other",
        related=["2024_Target"],
        notes="See [[2024_Target]] and [[2024_Target]] here.\n",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    notes_after = (vault / "papers/2024_Other/notes.md").read_text()
    assert (
        notes_after
        == "See [[2024_Target]] (deleted) and [[2024_Target]] (deleted) here.\n"
    )
    assert "Tagged 1 referencing note" in result.output


def test_rm_annotates_referencing_discussion(vault: Path) -> None:
    # Q1 coverage: discussion.md is in scope alongside notes.md.
    _write_paper(vault, "2024_Target")
    _write_paper(
        vault, "2024_Other",
        discussion="Compared against [[2024_Target]] in detail.\n",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert (
        vault / "papers/2024_Other/discussion.md"
    ).read_text() == "Compared against [[2024_Target]] (deleted) in detail.\n"


def test_rm_purge_annotates(vault: Path) -> None:
    # AC87: --purge also tags referencing notes.
    _write_paper(vault, "2024_Target")
    _write_paper(vault, "2024_Other", notes="cf [[2024_Target]].\n")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "--purge", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert (
        vault / "papers/2024_Other/notes.md"
    ).read_text() == "cf [[2024_Target]] (deleted).\n"


def test_rm_annotate_idempotent(vault: Path) -> None:
    # AC87: a note already carrying the (deleted) tag is not double-tagged.
    _write_paper(vault, "2024_Target")
    _write_paper(
        vault, "2024_Other",
        notes="old [[2024_Target]] (deleted) plus fresh [[2024_Target]].\n",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    notes_after = (vault / "papers/2024_Other/notes.md").read_text()
    assert notes_after.count("(deleted)") == 2
    assert "(deleted) (deleted)" not in notes_after


def test_rm_does_not_touch_unreferenced_notes(vault: Path) -> None:
    # A note that does not reference the deleted paper stays byte-identical.
    _write_paper(vault, "2024_Target")
    _write_paper(
        vault, "2024_Other", notes="A note about [[2024_Unrelated]] only.\n"
    )
    notes_before = (vault / "papers/2024_Other/notes.md").read_text()
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert (
        vault / "papers/2024_Other/notes.md"
    ).read_text() == notes_before
    assert "Tagged" not in result.output


# ===========================================================================
# Deletion log removed (M24): no .deletion-log.jsonl is ever generated
# ===========================================================================


def test_rm_does_not_write_deletion_log(vault: Path) -> None:
    # AC89: the M23 log is gone — soft delete writes no log file.
    _write_paper(vault, "2024_Foo_Bar", title="The Foo")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo_Bar", "-y", "--library", str(vault)])
    assert not (vault / ".deletion-log.jsonl").exists()


def test_rm_purge_does_not_write_deletion_log(vault: Path) -> None:
    # AC89: --purge writes no log file either.
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--purge", "-y", "--library", str(vault)]
    )
    assert not (vault / ".deletion-log.jsonl").exists()


# ===========================================================================
# Confirmation prompt
# ===========================================================================


def test_rm_yes_skips_prompt(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    # No stdin provided; -y should skip the prompt entirely.
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Foo_Bar").exists()


def test_rm_yes_non_interactive_with_relations(vault: Path) -> None:
    """-y is a full force-delete: no prompt even when relations exist."""
    _write_paper(vault, "2024_Target", related=["2024_Holder"])
    _write_paper(vault, "2024_Holder", related=["2024_Target"])
    runner = CliRunner()
    # No stdin at all — must not block on a prompt.
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Target").exists()
    assert _read_meta(vault, "2024_Holder")["related"] == []


def test_rm_prompt_y_proceeds(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--library", str(vault)], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Foo_Bar").exists()
    assert "About to move to .trash/" in result.output


def test_rm_prompt_n_aborts(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--library", str(vault)], input="n\n"
    )
    assert result.exit_code == 0, result.output
    # Paper still on disk — abort path leaves vault untouched.
    assert (vault / "papers" / "2024_Foo_Bar").is_dir()
    assert "Aborted" in result.output


def test_rm_prompt_default_is_no(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    # Empty input (just Enter) should treat default=False as "no".
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--library", str(vault)], input="\n"
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_Foo_Bar").is_dir()


# ===========================================================================
# Pre-flight rejection
# ===========================================================================


def test_rm_invalid_id(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "../escape", "--yes", "--library", str(vault)]
    )
    assert result.exit_code != 0
    # M11 routes the positional arg through paper_lookup.resolve_paper_input
    # first, so a path-traversal string that matches no paper surfaces as
    # PaperNotFoundError ("No paper matching ...") rather than the older
    # RmError ("Invalid paper id ..."). The pre-flight refusal is preserved;
    # only the exception class moved.
    assert isinstance(result.exception, PaperNotFoundError)
    assert "No paper matching" in str(result.exception)


def test_rm_paper_not_found(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "9999_Ghost", "--yes", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


# ===========================================================================
# CLI smoke
# ===========================================================================


def test_rm_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["rm", "--help"])
    assert result.exit_code == 0
    assert "rm" in result.output.lower()
    # --cascade fully removed in M23.1.
    assert "--cascade" not in result.output
    assert "--yes" in result.output
    assert "--purge" in result.output
    assert "--paper-doi" in result.output
    # -y now documents non-interactive force-delete, not just "skip prompt".
    assert "non-interactive" in result.output.lower()


def test_rm_accepts_fuzzy_substring(vault: Path) -> None:
    """M11 smoke: substring of the id resolves to the paper correctly."""
    _write_paper(vault, "2024_Pandi_Cellfree")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "Pandi", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Pandi_Cellfree").exists()


def test_rm_accepts_paper_doi(vault: Path) -> None:
    """M11 smoke: --paper-doi reverse-looks-up the id."""
    _write_paper(vault, "2024_X_Foo", doi="10.5555/foo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "rm",
            "--paper-doi",
            "10.5555/foo",
            "--yes",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_X_Foo").exists()


# ===========================================================================
# Trash ring eviction (M22) — `lit rm` triggers enforce_cap
# ===========================================================================


def _prefill_trash(vault: Path, n: int) -> list[str]:
    """Fabricate ``n`` trash entries on disk with timestamps in the past.

    Returns paper ids oldest-first. Timestamps are all on 2020 dates so any
    real `move_to_trash` (uses "now") sorts as newer than every fabricated
    entry — the oldest fabricated one is always the eviction target.
    """
    trash_root = vault / TRASH_DIRNAME
    trash_root.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for i in range(n):
        pid = f"2020_Old{i:03d}"
        # Valid YYYYMMDDTHHMMSSZ in the past (all 2020). Index encoded across
        # minutes/seconds so ordering is strict and every timestamp is real.
        ts = f"20200101T00{i // 60:02d}{i % 60:02d}Z"
        entry = trash_root / f"{pid}-{ts}"
        entry.mkdir()
        (entry / "metadata.yaml").write_text(
            f"id: {pid}\ntitle: Old {pid}\n", encoding="utf-8"
        )
        (trash_root / f"{pid}-{ts}.meta.yaml").write_text(
            f"paper_id: {pid}\ndeleted_at: '2020-01-01T00:00:00+00:00'\n"
            f"cascade_was_used: false\ntitle: Old {pid}\n",
            encoding="utf-8",
        )
        ids.append(pid)
    return ids


def test_rm_at_cap_evicts_oldest_and_exits_zero(vault: Path) -> None:
    old_ids = _prefill_trash(vault, TRASH_MAX_ENTRIES)  # exactly 100
    _write_paper(vault, "2024_New")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_New", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Eviction line printed, naming the oldest fabricated id.
    assert f"Trash at cap ({TRASH_MAX_ENTRIES})" in result.output
    assert "permanently removed oldest" in result.output
    assert old_ids[0] in result.output
    # Oldest gone; trash back at the cap.
    remaining = {e.paper_id for e in list_trash(vault)}
    assert old_ids[0] not in remaining
    assert len(remaining) == TRASH_MAX_ENTRIES


def test_rm_under_cap_evicts_nothing(vault: Path) -> None:
    _prefill_trash(vault, 3)
    _write_paper(vault, "2024_New")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_New", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "permanently removed oldest" not in result.output
    # 3 old + 1 new = 4, none evicted.
    assert len(list_trash(vault)) == 4


def test_rm_eviction_failure_still_exits_zero(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prefill_trash(vault, TRASH_MAX_ENTRIES)
    _write_paper(vault, "2024_New")

    real_rmtree = trash_mod.shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Fail only for the eviction of the oldest fabricated entry; the new
        # paper's move into trash uses shutil.move, not rmtree, so this does
        # not interfere with the rm itself.
        if Path(path).name.startswith("2020_Old000"):
            raise OSError("simulated eviction failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(trash_mod.shutil, "rmtree", flaky_rmtree)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_New", "--yes", "--library", str(vault)]
    )
    # Best-effort housekeeping never hijacks rm.
    assert result.exit_code == 0, result.output
    assert "Trashed 2024_New" in result.output
    # The failed-to-evict entry is still present (swallowed, skipped).
    remaining = {e.paper_id for e in list_trash(vault)}
    assert "2020_Old000" in remaining
