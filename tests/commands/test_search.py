"""Tests for `lit search <query>` (M33)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault


def _seed_paper(
    vault: Path,
    paper_id: str,
    *,
    notes: str | None = None,
    discussion: str | None = None,
) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\ntitle: Title of {paper_id}\n", encoding="utf-8"
    )
    if notes is not None:
        (paper_dir / "notes.md").write_text(notes, encoding="utf-8")
    if discussion is not None:
        (paper_dir / "discussion.md").write_text(discussion, encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2023_A_Foo",
        notes="# Notes\nThis paper proposes a transformer model.\nMore text.\n",
        discussion="We discussed the attention mechanism here.\n",
    )
    _seed_paper(
        v, "2024_B_Bar",
        notes="A GNN approach, no Transformer at all.\n",
    )
    return v


def _invoke(vault: Path, *args: str):
    runner = CliRunner()
    return runner.invoke(cli, ["search", *args, "--library", str(vault)])


def test_search_hits_notes(vault: Path) -> None:
    result = _invoke(vault, "proposes")
    assert result.exit_code == 0, result.output
    hits = json.loads(result.output)
    assert len(hits) == 1
    assert hits[0]["id"] == "2023_A_Foo"
    assert hits[0]["file"] == "notes"


def test_search_hits_discussion(vault: Path) -> None:
    result = _invoke(vault, "attention")
    assert result.exit_code == 0, result.output
    hits = json.loads(result.output)
    assert len(hits) == 1
    assert hits[0]["file"] == "discussion"


def test_search_json_schema(vault: Path) -> None:
    result = _invoke(vault, "proposes")
    hits = json.loads(result.output)
    assert set(hits[0]) == {"id", "file", "line", "snippet"}
    assert hits[0]["line"] == 2  # 1-based; line 1 is "# Notes"
    assert hits[0]["snippet"] == "This paper proposes a transformer model."


def test_search_snippet_is_whole_line(vault: Path) -> None:
    """No truncation — the whole matched line comes back (only the newline
    that splitlines() drops is gone, interior/trailing spaces preserved)."""
    long_line = "x " * 200 + "needle " + "y " * 200
    _seed_paper(vault, "2025_C_Long", notes=long_line + "\n")
    result = _invoke(vault, "needle")
    hits = json.loads(result.output)
    assert hits[0]["snippet"] == long_line


def test_search_case_insensitive(vault: Path) -> None:
    result = _invoke(vault, "TRANSFORMER")
    assert result.exit_code == 0, result.output
    hits = json.loads(result.output)
    ids = {h["id"] for h in hits}
    assert ids == {"2023_A_Foo", "2024_B_Bar"}


def test_search_in_narrowing(vault: Path) -> None:
    result = _invoke(vault, "transformer", "--in", "notes")
    hits = json.loads(result.output)
    assert {h["file"] for h in hits} == {"notes"}
    # The discussion file mentions attention, not transformer, so notes-only
    # still works; narrow to discussion and the transformer hit disappears.
    result2 = _invoke(vault, "transformer", "--in", "discussion")
    assert json.loads(result2.output) == []


def test_search_in_rejects_unknown(vault: Path) -> None:
    result = _invoke(vault, "x", "--in", "metadata")
    assert result.exit_code != 0
    assert "--in accepts only" in result.output


def test_search_cross_paper_aggregation(vault: Path) -> None:
    result = _invoke(vault, "transformer")
    hits = json.loads(result.output)
    assert {h["id"] for h in hits} == {"2023_A_Foo", "2024_B_Bar"}


def test_search_excludes_trash(vault: Path) -> None:
    """A trashed paper's notes must not be searchable."""
    runner = CliRunner()
    # Move 2023_A_Foo into trash.
    runner.invoke(cli, ["rm", "2023_A_Foo", "--yes", "--library", str(vault)])
    result = _invoke(vault, "proposes")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_search_excludes_views(vault: Path) -> None:
    """views/ symlink hubs must not be double-counted."""
    runner = CliRunner()
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    result = _invoke(vault, "transformer")
    hits = json.loads(result.output)
    # Each matching paper appears exactly once (no symlink-induced dupes).
    ids = [h["id"] for h in hits]
    assert sorted(ids) == ["2023_A_Foo", "2024_B_Bar"]


def test_search_no_match(vault: Path) -> None:
    result = _invoke(vault, "zzzznotthere")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_search_table_format(vault: Path) -> None:
    result = _invoke(vault, "proposes", "--format", "table")
    assert result.exit_code == 0, result.output
    assert "2023_A_Foo" in result.output
    assert "notes" in result.output


def test_search_ignores_html_comments(vault: Path) -> None:
    """Scaffold reminders live in HTML comments and are NOT searchable content.

    Both scaffolds seed one (notes.md the wikilink reminder, discussion.md the
    append-format header). Left in the corpus they would return a hit on EVERY
    paper for a word only the scaffold uses — noise no user wrote. This is the
    same reasoning health-check's dangling-wikilink scan already applies.
    """
    _seed_paper(
        vault,
        "2025_C_Baz",
        notes="# Notes\n\n<!-- seeded scaffold: quokka -->\n\nreal body\n",
    )
    assert json.loads(_invoke(vault, "quokka").output) == []
    # The authored line around it is still perfectly findable.
    hits = json.loads(_invoke(vault, "real body").output)
    assert [h["id"] for h in hits] == ["2025_C_Baz"]


def test_search_keeps_line_numbers_across_a_masked_comment(vault: Path) -> None:
    """Masking must not shift the reported line: the Web UI scrolls to it."""
    _seed_paper(
        vault,
        "2025_D_Qux",
        discussion="# Log\n\n<!-- multi\nline\ncomment -->\n\nthe real turn\n",
    )
    hits = json.loads(_invoke(vault, "the real turn").output)
    assert len(hits) == 1
    assert hits[0]["line"] == 7  # the line it really sits on
    assert hits[0]["snippet"] == "the real turn"


def test_search_finds_prose_on_a_line_that_ends_in_a_comment(vault: Path) -> None:
    """Only the commented span is blanked, not the whole line."""
    _seed_paper(vault, "2025_E_Mix", notes="authored words <!-- hidden words -->\n")
    assert [h["id"] for h in json.loads(_invoke(vault, "authored").output)] == [
        "2025_E_Mix"
    ]
    assert json.loads(_invoke(vault, "hidden").output) == []


def test_search_keeps_line_numbers_when_a_comment_holds_a_form_feed(
    vault: Path,
) -> None:
    """splitlines() breaks on \\x0c too — pdftotext page separators pasted
    into notes. A mask that turned it into a space would shift every later
    hit's line number and drop the file's last line from the corpus."""
    _seed_paper(
        vault,
        "2025_F_Feed",
        notes="line one\n<!-- page\x0cbreak -->\nTARGET after comment\n",
    )
    hits = json.loads(_invoke(vault, "TARGET after comment").output)
    assert len(hits) == 1
    assert hits[0]["line"] == 4  # \x0c splits the comment line in two
    assert hits[0]["snippet"] == "TARGET after comment"
