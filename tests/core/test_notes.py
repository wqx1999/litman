"""Unit tests for the M24 deletion-tag transform helpers in ``core/notes.py``.

``annotate_deleted_wikilinks`` / ``deannotate_deleted_wikilinks`` are the
CLI-side deterministic prose edits ``lit rm`` / ``lit trash restore`` use to
maintain the inline ``[[A]] (deleted)`` marker (ADR-013). They key off the
RESOLVED wikilink target (never the literal string), are idempotent, and
ignore cross-vault ``[[v:id]]`` links.

``enumerate_markdown_files`` is also covered for the Q1 widening (notes.md +
discussion.md, each guarded by ``.is_file()``).
"""

from __future__ import annotations

from pathlib import Path

from litman.core.notes import (
    annotate_deleted_wikilinks,
    deannotate_deleted_wikilinks,
    enumerate_markdown_files,
)


# ---------------------------------------------------------------------------
# annotate_deleted_wikilinks
# ---------------------------------------------------------------------------


def test_annotate_adds_suffix_to_target() -> None:
    text = "See [[2024_A]] for context.\n"
    assert (
        annotate_deleted_wikilinks(text, "2024_A")
        == "See [[2024_A]] (deleted) for context.\n"
    )


def test_annotate_tags_every_occurrence() -> None:
    text = "[[2024_A]] and again [[2024_A]] end.\n"
    assert (
        annotate_deleted_wikilinks(text, "2024_A")
        == "[[2024_A]] (deleted) and again [[2024_A]] (deleted) end.\n"
    )


def test_annotate_only_target_id_keyed() -> None:
    # A different live link must be left byte-identical.
    text = "[[2024_A]] vs [[2024_B]]\n"
    assert (
        annotate_deleted_wikilinks(text, "2024_A")
        == "[[2024_A]] (deleted) vs [[2024_B]]\n"
    )


def test_annotate_is_idempotent() -> None:
    once = annotate_deleted_wikilinks("[[2024_A]]\n", "2024_A")
    twice = annotate_deleted_wikilinks(once, "2024_A")
    assert once == "[[2024_A]] (deleted)\n"
    assert twice == once  # no `(deleted) (deleted)`


def test_annotate_no_match_returns_unchanged() -> None:
    text = "[[2024_B]] only\n"
    assert annotate_deleted_wikilinks(text, "2024_A") == text


def test_annotate_skips_cross_vault() -> None:
    # Cross-vault [[v:id]] is out of scope even when the id half matches.
    text = "[[fork:2024_A]] and [[2024_A]]\n"
    assert (
        annotate_deleted_wikilinks(text, "2024_A")
        == "[[fork:2024_A]] and [[2024_A]] (deleted)\n"
    )


def test_annotate_matches_after_agent_rewrite() -> None:
    # The match keys on the resolved target, so an agent that moved the link
    # around in prose still gets it tagged (not "find last written string").
    text = "Earlier we cited [[2024_A]] but now discuss [[2024_A]] more.\n"
    out = annotate_deleted_wikilinks(text, "2024_A")
    assert out.count("(deleted)") == 2


# ---------------------------------------------------------------------------
# deannotate_deleted_wikilinks
# ---------------------------------------------------------------------------


def test_deannotate_strips_suffix() -> None:
    text = "See [[2024_A]] (deleted) for context.\n"
    assert (
        deannotate_deleted_wikilinks(text, "2024_A")
        == "See [[2024_A]] for context.\n"
    )


def test_deannotate_is_idempotent_when_absent() -> None:
    text = "[[2024_A]] only\n"
    assert deannotate_deleted_wikilinks(text, "2024_A") == text


def test_deannotate_only_target_id() -> None:
    text = "[[2024_A]] (deleted) vs [[2024_B]] (deleted)\n"
    # Only A's tag is removed; B keeps its marker.
    assert (
        deannotate_deleted_wikilinks(text, "2024_A")
        == "[[2024_A]] vs [[2024_B]] (deleted)\n"
    )


def test_annotate_deannotate_round_trip() -> None:
    original = "Cross [[2024_A]] ref and [[2024_B]] kept.\n"
    annotated = annotate_deleted_wikilinks(original, "2024_A")
    restored = deannotate_deleted_wikilinks(annotated, "2024_A")
    assert restored == original


def test_deannotate_skips_cross_vault() -> None:
    text = "[[fork:2024_A]] (deleted) and [[2024_A]] (deleted)\n"
    assert (
        deannotate_deleted_wikilinks(text, "2024_A")
        == "[[fork:2024_A]] (deleted) and [[2024_A]]\n"
    )


# ---------------------------------------------------------------------------
# enumerate_markdown_files (Q1 widening to notes.md + discussion.md)
# ---------------------------------------------------------------------------


def _make_paper(vault: Path, paper_id: str) -> Path:
    d = vault / "papers" / paper_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_enumerate_yields_notes_and_discussion(tmp_path: Path) -> None:
    d = _make_paper(tmp_path, "2024_A")
    (d / "notes.md").write_text("n\n", encoding="utf-8")
    (d / "discussion.md").write_text("d\n", encoding="utf-8")
    names = {p.name for p in enumerate_markdown_files(tmp_path)}
    assert names == {"notes.md", "discussion.md"}


def test_enumerate_skips_absent_discussion(tmp_path: Path) -> None:
    # discussion.md is created on-demand; absent → simply not yielded.
    d = _make_paper(tmp_path, "2024_A")
    (d / "notes.md").write_text("n\n", encoding="utf-8")
    paths = list(enumerate_markdown_files(tmp_path))
    assert [p.name for p in paths] == ["notes.md"]


def test_enumerate_no_papers_dir(tmp_path: Path) -> None:
    assert list(enumerate_markdown_files(tmp_path)) == []
