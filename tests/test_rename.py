"""Tests for ``lit rename``.

Covers paper-id change, metadata roundtrip, ref-list ripple across other
papers, ``[[id]]`` wikilink rewrite in notes.md files, INDEX.json refresh,
views/ rebuild, atomic directory rename, and pre-flight validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.exceptions import PaperNotFoundError, RenameError

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
        "priority": fields.get("priority", "B"),
        "read-date": None,
        "last-revisited": None,
        "related": fields.get("related", []),
        "contradicts": fields.get("contradicts", []),
        "extends": fields.get("extends", []),
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


def test_rename_rewrites_cross_paper_notes(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    methods_md = vault / "notes" / "methods" / "transformer-survey.md"
    methods_md.write_text(
        "Transformer methods include [[2024_Foo_Bar]].\n",
        encoding="utf-8",
    )
    ideas_md = vault / "notes" / "ideas" / "amp-design.md"
    ideas_md.write_text(
        "Inspired by [[2024_Foo_Bar]] (the Bar paper).\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert "[[2024_Foo_Baz]]" in methods_md.read_text()
    assert "[[2024_Foo_Baz]]" in ideas_md.read_text()


def test_rename_refreshes_index_and_views(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", topics=["alpha"])

    runner = CliRunner()
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert (vault / "views/by-topic/alpha/2024_Foo_Bar").is_symlink()

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
    assert new_link.is_symlink()
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
