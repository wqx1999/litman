"""Tests for ``lit rm``.

Covers happy-path delete, reverse-ref pre-flight, wikilink pre-flight,
``--cascade`` ref clearing + wikilink stripping, ``--yes`` prompt skip,
prompt abort, INDEX/views refresh, and pre-flight rejection.
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
from litman.exceptions import PaperNotFoundError, RmError

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


def test_rm_simple(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Foo_Bar").exists()
    assert "Trashed 2024_Foo_Bar" in result.output


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
# Reverse-ref pre-flight
# ===========================================================================


def test_rm_refuses_when_referenced(vault: Path) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(vault, "2024_Holder", related=["2024_Target"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "--yes", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RmError)
    assert "still reference" in str(result.exception)
    assert "2024_Holder (related)" in str(result.exception)
    # Target dir still on disk — pre-flight rejected before any destruction.
    assert (vault / "papers" / "2024_Target").is_dir()


def test_rm_refuses_listing_multiple_holders(vault: Path) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(vault, "2024_HolderA", related=["2024_Target"])
    _write_paper(vault, "2024_HolderB", contradicts=["2024_Target"])
    _write_paper(vault, "2024_HolderC", extends=["2024_Target"])
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "--yes", "--library", str(vault)]
    )
    assert result.exit_code != 0
    msg = str(result.exception)
    assert "3 paper(s) still reference" in msg
    assert "2024_HolderA (related)" in msg
    assert "2024_HolderB (contradicts)" in msg
    assert "2024_HolderC (extends)" in msg


# ===========================================================================
# Wikilink pre-flight
# ===========================================================================


def test_rm_refuses_when_wikilinked_in_paper_notes(vault: Path) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(
        vault, "2024_Other",
        notes="See [[2024_Target]] for context.\n",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Target", "--yes", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RmError)
    assert "wikilinks" in str(result.exception)
    assert "papers/2024_Other/notes.md" in str(result.exception)
    assert (vault / "papers" / "2024_Target").is_dir()


# ===========================================================================
# --cascade
# ===========================================================================


def test_rm_cascade_clears_refs(vault: Path) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(
        vault, "2024_Holder",
        related=["2024_Target", "2022_Other"],
        extends=["2024_Target"],
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rm", "2024_Target", "--cascade", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Target").exists()

    holder = _read_meta(vault, "2024_Holder")
    assert holder["related"] == ["2022_Other"]
    assert holder["extends"] == []
    assert holder["updated-at"] != "2026-04-28T10:00:00+02:00"
    assert "Cleared references in 1 paper" in result.output
    assert "2024_Holder" in result.output


def test_rm_cascade_strips_wikilinks(vault: Path) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(
        vault, "2024_Other",
        notes="Compare [[2024_Target]] and [[2024_Target]] here.\n",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rm", "2024_Target", "--cascade", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    # Bracket-stripped: "[[id]]" → "id" (text preserved).
    other_notes = (vault / "papers/2024_Other/notes.md").read_text()
    assert "[[2024_Target]]" not in other_notes
    assert "Compare 2024_Target and 2024_Target here." in other_notes

    assert "Stripped" in result.output


def test_rm_cascade_handles_both_refs_and_wikilinks(vault: Path) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(
        vault, "2024_Holder",
        related=["2024_Target"],
        notes="[[2024_Target]] is referenced.\n",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rm", "2024_Target", "--cascade", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    holder = _read_meta(vault, "2024_Holder")
    assert holder["related"] == []
    holder_notes = (vault / "papers/2024_Holder/notes.md").read_text()
    assert "[[2024_Target]]" not in holder_notes
    assert "2024_Target is referenced." in holder_notes


# ===========================================================================
# Confirmation prompt
# ===========================================================================


def test_rm_yes_skips_prompt(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    # No stdin provided; --yes should skip the prompt entirely.
    result = runner.invoke(
        cli, ["rm", "2024_Foo_Bar", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / "2024_Foo_Bar").exists()


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
    assert "--cascade" in result.output
    assert "--yes" in result.output
    assert "--paper-doi" in result.output


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
