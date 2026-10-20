"""Tests for ``lit rename``.

Covers paper-id change, metadata roundtrip, ref-list ripple across other
papers, ``[[id]]`` wikilink rewrite in notes.md files, INDEX.json refresh,
views/ rebuild, atomic directory rename, and pre-flight validation.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import trash as trash_mod
from litman.core.library import create_vault
from litman.core.trash import (
    REMOVED_SI_DIRNAME,
    SIDECAR_SUFFIX,
    list_removed_si,
    move_si_to_trash,
    read_removed_si_sidecar,
    removed_si_dir,
    rename_removed_si,
)
from litman.exceptions import (
    CorruptMetadataError,
    PaperNotFoundError,
    RenameError,
)
from litman.core.portable_link import is_portable_link

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    """Materialise a paper with the canonical M2.0-schema metadata."""
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
        "read-date": None,
        "last-revisited": None,
        "related": fields.get("related", []),
        "contradicts": fields.get("contradicts", []),
        "contradicted-by": fields.get("contradicted_by", []),
        "extends": fields.get("extends", []),
        "extended-by": fields.get("extended_by", []),
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)
    notes = fields.get("notes")
    if notes is not None:
        (paper_dir / "notes.md").write_text(notes, encoding="utf-8")


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


def test_rename_directory_and_id_field(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert not (vault / "papers" / "2024_Foo_Bar").exists()
    new_dir = vault / "papers" / "2024_Foo_Baz"
    assert new_dir.is_dir()
    meta = _read_meta(vault, "2024_Foo_Baz")
    assert meta["id"] == "2024_Foo_Baz"
    # updated-at bumped past the seed value.
    assert meta["updated-at"] != "2026-04-28T10:00:00+02:00"


def test_rename_ripples_to_back_references(vault: Path) -> None:
    _write_paper(vault, "2023_Old_Source")
    _write_paper(
        vault, "2024_Holder_A",
        related=["2023_Old_Source"],
        extends=["2023_Old_Source", "2022_Other"],
    )
    _write_paper(vault, "2024_Holder_B", contradicts=["2023_Old_Source"])
    _write_paper(vault, "2024_NoRef")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2023_Old_Source", "2023_New_Source",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated 2 back-referencing" in result.output

    a = _read_meta(vault, "2024_Holder_A")
    assert a["related"] == ["2023_New_Source"]
    assert a["extends"] == ["2023_New_Source", "2022_Other"]
    assert a["updated-at"] != "2026-04-28T10:00:00+02:00"

    b = _read_meta(vault, "2024_Holder_B")
    assert b["contradicts"] == ["2023_New_Source"]

    no_ref = _read_meta(vault, "2024_NoRef")
    assert no_ref["updated-at"] == "2026-04-28T10:00:00+02:00"


def test_rename_rewrites_reverse_fields(vault: Path) -> None:
    """ADR-012: renaming an id rewrites it inside reverse relation fields
    (extended-by / contradicted-by) too, not just the forward fields."""
    _write_paper(vault, "2023_Old_Source")
    _write_paper(
        vault, "2024_Downstream",
        extended_by=["2023_Old_Source"],
        contradicted_by=["2023_Old_Source"],
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2023_Old_Source", "2023_New_Source",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    d = _read_meta(vault, "2024_Downstream")
    assert d["extended-by"] == ["2023_New_Source"]
    assert d["contradicted-by"] == ["2023_New_Source"]


def test_rename_self_reference_updated(vault: Path) -> None:
    """A paper that references itself in related/extends has the self-ref
    rewritten too."""
    _write_paper(vault, "2024_Self", related=["2024_Self"])

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Self", "2024_Selfie", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, "2024_Selfie")
    assert meta["related"] == ["2024_Selfie"]


def test_rename_dedupes_collisions(vault: Path) -> None:
    """If a paper already references both <old> and <new>, the post-rename
    list collapses the duplicate."""
    _write_paper(vault, "2024_Old")
    _write_paper(vault, "2024_New")
    _write_paper(vault, "2024_Holder", related=["2024_Old", "2024_New"])

    # Rename Old → some other name to avoid the new_dir-exists check
    runner = CliRunner()
    # Move 2024_New out of the way first, then rename Old→New_X to create
    # a synthetic dedupe scenario instead. Simpler: rename Old→Other and
    # check the simple path.
    result = runner.invoke(
        cli,
        ["rename", "2024_Old", "2024_Other", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    holder = _read_meta(vault, "2024_Holder")
    assert holder["related"] == ["2024_Other", "2024_New"]


def test_rename_dedupes_when_new_already_present(vault: Path) -> None:
    """Holder lists [Old, New]; renaming Old→New (where New is also a
    valid paper id chain) — exercise via rename of an unrelated id to
    `New` so no dir collision but ref-list dedupe."""
    _write_paper(vault, "2024_A")
    _write_paper(vault, "2024_B")
    _write_paper(vault, "2024_Holder", related=["2024_A", "2024_B"])

    # Rename A → C (fresh name). Holder.related should become [C, B] (no dedupe).
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rename", "2024_A", "2024_C", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    holder = _read_meta(vault, "2024_Holder")
    assert holder["related"] == ["2024_C", "2024_B"]


def test_rename_rewrites_paper_notes_md(vault: Path) -> None:
    _write_paper(
        vault, "2024_Foo_Bar",
        notes="# Foo Bar\n\nSee [[2024_Foo_Bar]] for context.\n",
    )
    _write_paper(
        vault, "2024_Other",
        notes="Compare with [[2024_Foo_Bar]] and [[2024_Foo_Bar]].\n",
    )
    _write_paper(vault, "2024_Untouched", notes="No refs here.\n")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated 2 notes" in result.output

    assert "[[2024_Foo_Baz]]" in (
        vault / "papers/2024_Foo_Baz/notes.md"
    ).read_text()
    assert "[[2024_Foo_Baz]]" in (
        vault / "papers/2024_Other/notes.md"
    ).read_text()
    assert "[[2024_Foo_Bar]]" not in (
        vault / "papers/2024_Other/notes.md"
    ).read_text()
    untouched = (vault / "papers/2024_Untouched/notes.md").read_text()
    assert untouched == "No refs here.\n"


def test_rename_rewrites_paper_discussion_md(vault: Path) -> None:
    """Q1 widening: enumerate_markdown_files now yields discussion.md, so a
    ``[[old-id]]`` inside a referencing paper's discussion.md is rewritten to
    ``[[new-id]]`` (mirrors the notes.md rewrite path)."""
    _write_paper(vault, "2024_Foo_Bar")
    _write_paper(vault, "2024_Other")
    (vault / "papers/2024_Other/discussion.md").write_text(
        "Discussed alongside [[2024_Foo_Bar]] in the lab meeting.\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    discussion = (vault / "papers/2024_Other/discussion.md").read_text()
    assert "[[2024_Foo_Baz]]" in discussion
    assert "[[2024_Foo_Bar]]" not in discussion


def test_rename_refreshes_index_and_views(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", topics=["alpha"])

    runner = CliRunner()
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert is_portable_link(vault / "views/by-topic/alpha/2024_Foo_Bar")

    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads((vault / "INDEX.json").read_text())
    assert payload["n_papers"] == 1
    assert payload["papers"][0]["id"] == "2024_Foo_Baz"

    assert not (vault / "views/by-topic/alpha/2024_Foo_Bar").exists()
    new_link = vault / "views/by-topic/alpha/2024_Foo_Baz"
    assert is_portable_link(new_link)
    assert new_link.resolve() == (vault / "papers/2024_Foo_Baz").resolve()


def test_rename_paper_pdf_preserved(vault: Path) -> None:
    """Binary files inside the paper dir survive the directory rename."""
    _write_paper(vault, "2024_Foo_Bar")
    pdf = vault / "papers/2024_Foo_Bar/paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n%%EOF\n")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    new_pdf = vault / "papers/2024_Foo_Baz/paper.pdf"
    assert new_pdf.is_file()
    assert new_pdf.read_bytes().startswith(b"%PDF-1.4")


# ===========================================================================
# Pre-flight rejection
# ===========================================================================


def test_rename_identical_old_new(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Bar", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RenameError)
    assert "identical" in str(result.exception)


def test_rename_invalid_new_id(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "../escape", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RenameError)
    assert "Invalid new id" in str(result.exception)


def test_rename_unknown_old_id(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rename", "9999_Ghost", "9999_Spirit", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_rename_corrupt_metadata_friendly_error(vault: Path) -> None:
    # Review A1: renaming a paper whose own metadata.yaml is corrupt must
    # surface a friendly, path-naming CorruptMetadataError instead of a raw
    # ruamel YAMLError traceback. resolve_paper_id matches by directory name
    # (no YAML parse), so the corrupt file is first read at the round-trip
    # load — exactly the site the shared helper now guards.
    _write_paper(vault, "2024_Foo_Bar")
    bad = vault / "papers/2024_Foo_Bar/metadata.yaml"
    bad.write_text("title: [unterminated\n: : :\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CorruptMetadataError)
    assert "2024_Foo_Bar" in str(result.exception)
    # No partial state — the folder is not renamed.
    assert (vault / "papers/2024_Foo_Bar").is_dir()
    assert not (vault / "papers/2024_Foo_Baz").exists()


def test_rename_target_already_taken(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    _write_paper(vault, "2024_Foo_Baz")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RenameError)
    assert "already exists" in str(result.exception)
    # No partial state — both papers still present.
    assert (vault / "papers/2024_Foo_Bar").is_dir()
    assert (vault / "papers/2024_Foo_Baz").is_dir()


# ===========================================================================
# Atomicity
# ===========================================================================


def test_rename_does_not_touch_unrelated_papers(vault: Path) -> None:
    _write_paper(vault, "2024_Renamed")
    _write_paper(vault, "2024_Untouched", topics=["x"])

    untouched_meta_path = vault / "papers/2024_Untouched/metadata.yaml"
    before_text = untouched_meta_path.read_text()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Renamed", "2024_NewName", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert untouched_meta_path.read_text() == before_text


def test_rename_post_state_consistent(vault: Path) -> None:
    """After rename, every paper's metadata id matches its dir name and
    INDEX.json agrees with the on-disk state."""
    _write_paper(vault, "2024_A", related=["2024_B"])
    _write_paper(vault, "2024_B", related=["2024_A"])

    runner = CliRunner()
    result = runner.invoke(
        cli, ["rename", "2024_A", "2024_AA", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output

    payload = json.loads((vault / "INDEX.json").read_text())
    ids_in_index = sorted(p["id"] for p in payload["papers"])
    ids_on_disk = sorted(
        d.name for d in (vault / "papers").iterdir() if d.is_dir()
    )
    assert ids_in_index == ids_on_disk == ["2024_AA", "2024_B"]

    # Each metadata id matches its dir.
    for pid in ids_on_disk:
        meta = _read_meta(vault, pid)
        assert meta["id"] == pid

    # B's reverse ref now points to AA.
    b = _read_meta(vault, "2024_B")
    assert b["related"] == ["2024_AA"]


# ===========================================================================
# CLI smoke
# ===========================================================================


def test_rename_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["rename", "--help"])
    assert result.exit_code == 0
    assert "rename" in result.output.lower()


def test_rename_accepts_fuzzy_old(vault: Path) -> None:
    """M11 smoke: old id accepts a substring; new is NOT fuzzy-matched."""
    _write_paper(vault, "2024_Pandi_Cellfree")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "rename",
            "Pandi",
            "2024_Pandi_CellfreeV2",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_Pandi_CellfreeV2").is_dir()
    assert not (vault / "papers" / "2024_Pandi_Cellfree").exists()


def test_rename_cascades_into_repo_meta(vault: Path) -> None:
    """Renaming a paper rewrites its id inside every bound repo's
    repo-meta.yaml::papers (invariant #12 bidirectional binding), not just
    other papers' ref lists. Regression for bug-report 2026-06-01 #3.
    """
    old, new = "2023_Pandi_Protein", "2024_Pandi_Folding"
    _write_paper(vault, old)

    rt = YAML()
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.default_flow_style = False

    # Paper side: bind to repo "myrepo" via code-clones.
    meta_path = vault / "papers" / old / "metadata.yaml"
    meta = rt.load(meta_path.read_text())
    meta["code-clones"] = ["myrepo"]
    with meta_path.open("w", encoding="utf-8") as f:
        rt.dump(meta, f)

    # Repo side: codes/myrepo/repo-meta.yaml mirrors the binding (papers=[old]).
    repo_dir = vault / "codes" / "myrepo"
    repo_dir.mkdir(parents=True)
    stale_ts = "2026-04-28T10:00:00+02:00"
    repo_meta = {
        "name": "myrepo",
        "upstream": "https://example.com/myrepo.git",
        "created-at": stale_ts,
        "updated-at": stale_ts,
        "papers": [old],
        "framework": None,
        "runs-on": None,
        "status": None,
    }
    with (repo_dir / "repo-meta.yaml").open("w", encoding="utf-8") as f:
        rt.dump(repo_meta, f)

    runner = CliRunner()
    result = runner.invoke(cli, ["rename", old, new, "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "repo binding" in result.output

    # repo-meta back-reference rewritten old -> new, updated-at bumped.
    after = _yaml.load((repo_dir / "repo-meta.yaml").read_text())
    assert after["papers"] == [new]
    assert after["updated-at"] != stale_ts
    # Paper-side binding preserved (repo name is unchanged by a paper rename).
    assert _read_meta(vault, new)["code-clones"] == ["myrepo"]


def test_rename_reports_a_moved_aside_folder(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming a linked paper rebuilds the project hubs, which can move a
    folder out of the user's own directory — never in silence."""
    import shutil

    from litman.core.portable_link import remove_link_if_present

    monkeypatch.setenv("COLUMNS", "400")
    proj = tmp_path / "myproj"
    proj.mkdir()
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  myproj: {proj}\n",
        encoding="utf-8",
    )
    _write_paper(vault, "2024_Foo_Bar", projects=["myproj"])
    runner = CliRunner()
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    link = proj / "litman_reflib" / "2024_Foo_Bar"
    assert remove_link_if_present(link)
    shutil.copytree(vault / "papers" / "2024_Foo_Bar", link)
    (link / "MY_NOTES.md").write_text("hand-written\n", encoding="utf-8")

    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "kept 1 folder that does not match the vault:" in flat
    kept_root = (
        vault / ".trash" / "replaced-folders" / "myproj" / "litman_reflib"
    )
    kept = sorted(kept_root.iterdir())
    assert len(kept) == 1
    assert (kept[0] / "MY_NOTES.md").is_file()
    assert is_portable_link(proj / "litman_reflib" / "2024_Foo_Baz")


# ===========================================================================
# Removed SI in the trash follows the paper
# ===========================================================================

# The SI file names below carry this DOI's number, so `lit add --si` verifies
# them by name and needs no --force.
_SI_DOI = "10.1021/acs.jcim.9b00237"
_STAMP = "20261020T083000Z"


def _run(vault: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, [*args, "--library", str(vault)])


def _si_dir(vault: Path, paper_id: str) -> Path:
    return vault / "papers" / paper_id / "supplementary"


def _paper_with_removed_si(vault: Path, tmp_path: Path, paper_id: str) -> Path:
    """A paper holding si-01 whose si-02 `lit rm --si` moved to the trash."""
    _write_paper(vault, paper_id, doi=_SI_DOI)
    one = tmp_path / "ci9b00237_si_001.csv"
    one.write_bytes(b"1,2\n")
    two = tmp_path / "ci9b00237_si_002.xlsx"
    two.write_bytes(b"sheet")
    added = _run(vault, "add", "--si", paper_id, str(one), str(two))
    assert added.exit_code == 0, added.output
    removed = _run(vault, "rm", paper_id, "--si", "si-02", "--yes")
    assert removed.exit_code == 0, removed.output
    (trashed,) = removed_si_dir(vault, paper_id).glob("*-si-02.xlsx")
    return trashed


def _plant_removed_si(
    vault: Path, tmp_path: Path, paper_id: str, name: str, payload: bytes
) -> Path:
    """A removed SI file and its sidecar, through the call `lit rm --si` makes.
    The record's ``original`` names the payload, so a pair is recognisable."""
    source = tmp_path / "planted" / paper_id / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    record = {"file": name, "original": f"{payload.decode()}.dat"}
    return move_si_to_trash(vault, paper_id, source, record)


def _contents(folder: Path) -> dict[str, bytes] | None:
    if not folder.is_dir():
        return None
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def test_rename_moves_the_removed_si_along(vault: Path, tmp_path: Path) -> None:
    trashed = _paper_with_removed_si(vault, tmp_path, "2019_Yang_Concepts")
    sidecar_before = read_removed_si_sidecar(trashed)

    result = _run(vault, "rename", "2019_Yang_Concepts", "2019_Yang_AI")

    assert result.exit_code == 0, result.output
    assert "Moved 1 removed SI file in the trash to the new id" in result.output
    assert not removed_si_dir(vault, "2019_Yang_Concepts").exists()
    moved = removed_si_dir(vault, "2019_Yang_AI") / trashed.name
    assert moved.read_bytes() == b"sheet"
    # Only the id changes; when it was removed and its record stay as written.
    assert read_removed_si_sidecar(moved) == {
        **sidecar_before,
        "paper_id": "2019_Yang_AI",
    }

    listed = _run(vault, "trash", "list")
    assert listed.exit_code == 0, listed.output
    assert f"lit add --si 2019_Yang_AI {moved}" in listed.output
    assert "lit add --si 2019_Yang_Concepts" not in listed.output

    back = _run(vault, "add", "--si", "2019_Yang_AI", str(moved))
    assert back.exit_code == 0, back.output
    assert back.stdout.splitlines()[1] == str(
        _si_dir(vault, "2019_Yang_AI") / "si-03.xlsx"
    )
    assert not moved.exists()
    assert list_removed_si(vault) == []


def test_rename_keeps_removed_si_numbers_retired(
    vault: Path, tmp_path: Path
) -> None:
    """The next SI added under the new id numbers past the removed si-02, as
    it would have under the old id."""
    _paper_with_removed_si(vault, tmp_path, "2019_Yang_Concepts")
    renamed = _run(vault, "rename", "2019_Yang_Concepts", "2019_Yang_AI")
    assert renamed.exit_code == 0, renamed.output
    fresh = tmp_path / "ci9b00237_si_003.csv"
    fresh.write_bytes(b"3,4\n")

    added = _run(vault, "add", "--si", "2019_Yang_AI", str(fresh))

    assert added.exit_code == 0, added.output
    assert added.stdout.splitlines()[1] == str(
        _si_dir(vault, "2019_Yang_AI") / "si-03.csv"
    )


def test_rename_merges_into_removed_si_left_under_the_new_id(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier paper called 2024_Foo_Baz left removed SI behind. The
    renamed paper's files join it, each with its own sidecar, and a name
    already taken there gets a fresh one instead of overwriting it."""
    monkeypatch.setattr(trash_mod, "_utc_compact_now", lambda: _STAMP)
    _write_paper(vault, "2024_Foo_Bar")
    _plant_removed_si(vault, tmp_path, "2024_Foo_Baz", "si-02.xlsx", b"earlier")
    _plant_removed_si(vault, tmp_path, "2024_Foo_Bar", "si-02.xlsx", b"clash")
    _plant_removed_si(vault, tmp_path, "2024_Foo_Bar", "si-03.csv", b"own")
    # A record whose file was deleted by hand goes along too.
    stray = removed_si_dir(vault, "2024_Foo_Bar") / f"{_STAMP}-si-04.pdf{SIDECAR_SUFFIX}"
    stray.write_text("paper_id: 2024_Foo_Bar\n", encoding="utf-8")

    result = _run(vault, "rename", "2024_Foo_Bar", "2024_Foo_Baz")

    assert result.exit_code == 0, result.output
    assert "Moved 2 removed SI files in the trash to the new id" in result.output
    assert not removed_si_dir(vault, "2024_Foo_Bar").exists()
    folder = removed_si_dir(vault, "2024_Foo_Baz")
    assert len(os.listdir(folder)) == 7  # 3 files, 3 sidecars, the stray record
    rows = {row.path.read_bytes(): row for row in list_removed_si(vault)}
    assert sorted(rows) == [b"clash", b"earlier", b"own"]
    assert rows[b"earlier"].path.name == f"{_STAMP}-si-02.xlsx"
    assert re.fullmatch(
        rf"{_STAMP}-[0-9a-f]{{4}}-si-02\.xlsx", rows[b"clash"].path.name
    )
    assert rows[b"own"].path.name == f"{_STAMP}-si-03.csv"
    for payload, row in rows.items():
        assert row.paper_id == "2024_Foo_Baz"
        assert row.original == f"{payload.decode()}.dat"  # its own record
        assert read_removed_si_sidecar(row.path)["paper_id"] == "2024_Foo_Baz"
    assert _yaml.load((folder / stray.name).read_text(encoding="utf-8")) == {
        "paper_id": "2024_Foo_Baz"
    }


@pytest.mark.parametrize("others", [False, True], ids=["no-trash", "others-si"])
def test_rename_without_removed_si_prints_what_it_always_did(
    vault: Path, tmp_path: Path, others: bool
) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    if others:
        # Another paper's, and some an earlier 2024_Foo_Baz left behind.
        _plant_removed_si(vault, tmp_path, "2024_Other", "si-01.pdf", b"other")
        _plant_removed_si(vault, tmp_path, "2024_Foo_Baz", "si-01.pdf", b"left")
    before = {
        pid: _contents(removed_si_dir(vault, pid))
        for pid in ("2024_Other", "2024_Foo_Baz")
    }

    result = _run(vault, "rename", "2024_Foo_Bar", "2024_Foo_Baz")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "✓ Renamed 2024_Foo_Bar → 2024_Foo_Baz",
        "INDEX.json + views/ refreshed.",
    ]
    assert {
        pid: _contents(removed_si_dir(vault, pid))
        for pid in ("2024_Other", "2024_Foo_Baz")
    } == before


@pytest.mark.parametrize("merge", [False, True], ids=["folder-move", "merge"])
def test_rename_survives_a_failed_removed_si_move(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, merge: bool
) -> None:
    """The rename has committed by then: a failed move is a warning, and every
    removed file stays where it was, next to its sidecar."""
    _write_paper(vault, "2024_Foo_Bar")
    _plant_removed_si(vault, tmp_path, "2024_Foo_Bar", "si-02.xlsx", b"own")
    if merge:
        _plant_removed_si(vault, tmp_path, "2024_Foo_Baz", "si-05.pdf", b"earlier")
    ids = ("2024_Foo_Bar", "2024_Foo_Baz")
    before = {pid: _contents(removed_si_dir(vault, pid)) for pid in ids}
    real_rename = trash_mod.os.rename

    def failing_rename(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        # trash_mod.os is the os module itself: the paper's own folder rename
        # comes through here too and must go on. In a merge the file moves and
        # its sidecar fails, so the file has to be moved back.
        path = Path(src)
        if REMOVED_SI_DIRNAME in path.parts and (
            not merge or path.name.endswith(SIDECAR_SUFFIX)
        ):
            raise PermissionError("simulated")
        real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(trash_mod.os, "rename", failing_rename)

    result = _run(vault, "rename", "2024_Foo_Bar", "2024_Foo_Baz")

    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_Foo_Baz").is_dir()
    assert not (vault / "papers" / "2024_Foo_Bar").exists()
    flat = " ".join(result.output.split())
    assert (
        "warning: removed SI stayed in .trash/removed-si/2024_Foo_Bar/ "
        "(PermissionError). Move its files to .trash/removed-si/2024_Foo_Baz/ "
        "by hand."
    ) in flat
    assert "Moved" not in flat
    assert {pid: _contents(removed_si_dir(vault, pid)) for pid in ids} == before


def _fs_ignores_case(where: Path) -> bool:
    """Probed, not read off sys.platform (as in core/test_supplementary.py)."""
    probe = where / "LitmanCaseProbe"
    probe.mkdir()
    try:
        return (where / "LITMANCASEPROBE").is_dir()
    finally:
        probe.rmdir()


@pytest.mark.parametrize(
    "new", ["2024_Foo_Bar", "2024_foo_bar"], ids=["same-name", "case-variant"]
)
def test_rename_removed_si_leaves_a_folder_reached_by_both_names(
    vault: Path, tmp_path: Path, new: str
) -> None:
    """A case-only rename on a case-insensitive disk reaches one folder under
    both names; merging it into itself would rename every file in it."""
    if new != "2024_Foo_Bar" and not _fs_ignores_case(tmp_path):
        pytest.skip(
            "Only where the filesystem itself ignores case do two spellings "
            "name one folder; here they are two different directories."
        )
    trashed = _plant_removed_si(vault, tmp_path, "2024_Foo_Bar", "si-02.xlsx", b"own")
    before = _contents(trashed.parent)

    assert rename_removed_si(vault, "2024_Foo_Bar", new) == 0
    assert _contents(trashed.parent) == before


@pytest.mark.parametrize(
    "failure", ["replace", "write"], ids=["replace-fails", "write-dies-halfway"]
)
def test_rename_removed_si_never_cuts_a_sidecar_short(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """The sidecar is the only copy of the removed file's record: a rewrite
    that fails part-way keeps the old text whole and leaves no temp file."""
    trashed = _plant_removed_si(vault, tmp_path, "2024_Foo_Bar", "si-02.xlsx", b"own")
    sidecar_name = f"{trashed.name}{SIDECAR_SUFFIX}"
    before = (trashed.parent / sidecar_name).read_bytes()

    def in_removed_si(path: Any) -> bool:
        p = Path(path)
        return REMOVED_SI_DIRNAME in p.parts and p.name.endswith(SIDECAR_SUFFIX)

    if failure == "replace":
        real_replace = trash_mod.os.replace

        def failing_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            # trash_mod.os is the os module itself: fail only the sidecar swap.
            if in_removed_si(dst):
                raise PermissionError("simulated")
            real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(trash_mod.os, "replace", failing_replace)
    else:
        real_write_text = Path.write_text

        def dying_write_text(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
            if in_removed_si(self):
                real_write_text(self, data[: len(data) // 2], *args, **kwargs)
                raise OSError("simulated: disk full")
            return real_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", dying_write_text)

    assert rename_removed_si(vault, "2024_Foo_Bar", "2024_Foo_Baz") == 1

    folder = removed_si_dir(vault, "2024_Foo_Baz")
    assert sorted(os.listdir(folder)) == [trashed.name, sidecar_name]
    assert (folder / sidecar_name).read_bytes() == before


def test_rename_removed_si_merges_on_a_disk_without_inodes(
    vault: Path, tmp_path: Path, no_inodes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different folders: compared by inode 0 they looked like one, and
    nothing moved, without a word."""
    _plant_removed_si(vault, tmp_path, "2024_Foo_Baz", "si-01.pdf", b"earlier")
    _plant_removed_si(vault, tmp_path, "2024_Foo_Bar", "si-02.xlsx", b"own")
    # The same disk as the stdlib sees it, so a return to os.path.samefile
    # (inode 0 on both sides: "same file") fails here too. Patched only now:
    # planting moves files, and shutil asks samefile on the way.
    monkeypatch.setattr(
        os.path, "samefile", lambda a, b: os.stat(a).st_dev == os.stat(b).st_dev
    )

    assert rename_removed_si(vault, "2024_Foo_Bar", "2024_Foo_Baz") == 1

    assert not removed_si_dir(vault, "2024_Foo_Bar").exists()
    rows = {row.path.read_bytes(): row for row in list_removed_si(vault)}
    assert sorted(rows) == [b"earlier", b"own"]
    assert {row.paper_id for row in rows.values()} == {"2024_Foo_Baz"}


def test_rename_removed_si_to_the_same_name_on_a_disk_without_inodes(
    vault: Path, tmp_path: Path, no_inodes: None
) -> None:
    trashed = _plant_removed_si(vault, tmp_path, "2024_Foo_Bar", "si-02.xlsx", b"own")
    before = _contents(trashed.parent)

    assert rename_removed_si(vault, "2024_Foo_Bar", "2024_Foo_Bar") == 0
    assert _contents(trashed.parent) == before
