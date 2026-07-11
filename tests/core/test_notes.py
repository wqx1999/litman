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

import os
from pathlib import Path

from litman.core.notes import (
    DISCUSSION_FORMAT_REMINDER,
    WIKILINK_REMINDER,
    annotate_deleted_wikilinks,
    deannotate_deleted_wikilinks,
    discussion_scaffold,
    ensure_discussion_scaffold,
    ensure_wikilink_reminder,
    enumerate_markdown_files,
    has_discussion_reminder,
    heal_discussion_scaffold,
    heal_wikilink_reminder,
)

_MARKER = "not backticks or plain text"  # _WIKILINK_REMINDER_MARKER, kept local


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
    # A pre-scaffold paper has no discussion.md; absent → simply not yielded.
    d = _make_paper(tmp_path, "2024_A")
    (d / "notes.md").write_text("n\n", encoding="utf-8")
    paths = list(enumerate_markdown_files(tmp_path))
    assert [p.name for p in paths] == ["notes.md"]


def test_enumerate_no_papers_dir(tmp_path: Path) -> None:
    assert list(enumerate_markdown_files(tmp_path)) == []


# ---------------------------------------------------------------------------
# ensure_wikilink_reminder (pure transform)
# ---------------------------------------------------------------------------


def test_ensure_present_returns_unchanged() -> None:
    text = f"# Title\n\n{WIKILINK_REMINDER}\n\n(Personal notes go here.)\n"
    assert ensure_wikilink_reminder(text) == text


def test_ensure_detected_by_marker_even_if_reworded() -> None:
    # A reworded reminder still carrying the marker phrase must NOT be
    # duplicated — detection keys on the marker, not the exact string.
    text = f"# Title\n\n<!-- use [[id]], {_MARKER}! -->\n\nbody\n"
    assert ensure_wikilink_reminder(text) == text


def test_ensure_inserts_after_heading_preserving_body() -> None:
    text = "# Some Paper\n\nMy real notes about [[2024_X]].\n"
    out = ensure_wikilink_reminder(text)
    assert _MARKER in out
    assert out.startswith("# Some Paper\n\n<!-- ")
    assert "My real notes about [[2024_X]]." in out
    # The real wikilink is preserved and not mistaken for the reminder.
    assert "[[2024_X]]" in out


def test_ensure_prepends_when_no_heading() -> None:
    text = "Just some notes, no heading.\n"
    out = ensure_wikilink_reminder(text)
    assert out.startswith(WIKILINK_REMINDER)
    assert "Just some notes, no heading." in out


def test_ensure_empty_text() -> None:
    assert ensure_wikilink_reminder("") == WIKILINK_REMINDER + "\n"


def test_ensure_is_idempotent() -> None:
    text = "# T\n\nbody\n"
    once = ensure_wikilink_reminder(text)
    assert ensure_wikilink_reminder(once) == once


# ---------------------------------------------------------------------------
# heal_wikilink_reminder (file-level, atomic)
# ---------------------------------------------------------------------------


def test_heal_rewrites_when_missing(tmp_path: Path) -> None:
    d = _make_paper(tmp_path, "2024_A")
    notes = d / "notes.md"
    # Simulate an agent overwrite that stripped the reminder.
    notes.write_text("# 2024_A\n\nUpdated understanding.\n", encoding="utf-8")
    assert heal_wikilink_reminder(tmp_path, "2024_A") is True
    healed = notes.read_text(encoding="utf-8")
    assert _MARKER in healed
    assert "Updated understanding." in healed


def test_heal_noop_when_present(tmp_path: Path) -> None:
    d = _make_paper(tmp_path, "2024_A")
    notes = d / "notes.md"
    body = f"# 2024_A\n\n{WIKILINK_REMINDER}\n\nkept.\n"
    notes.write_text(body, encoding="utf-8")
    assert heal_wikilink_reminder(tmp_path, "2024_A") is False
    assert notes.read_text(encoding="utf-8") == body  # byte-identical


def test_heal_noop_when_notes_absent(tmp_path: Path) -> None:
    _make_paper(tmp_path, "2024_A")  # dir but no notes.md
    assert heal_wikilink_reminder(tmp_path, "2024_A") is False


def test_heal_keeps_notes_writable(tmp_path: Path) -> None:
    # The whole point of notes.md being unlocked must survive the staged write:
    # the agent has to be able to overwrite it again next session.
    d = _make_paper(tmp_path, "2024_A")
    notes = d / "notes.md"
    notes.write_text("# 2024_A\n\nbody\n", encoding="utf-8")
    heal_wikilink_reminder(tmp_path, "2024_A")
    assert os.access(notes, os.W_OK)


# ---------------------------------------------------------------------------
# discussion scaffold (the append-only log's format anchor)
# ---------------------------------------------------------------------------


def test_scaffold_carries_heading_and_reminder() -> None:
    body = discussion_scaffold("2024_A")
    assert body.startswith("# Discussion log for 2024_A")
    assert DISCUSSION_FORMAT_REMINDER in body
    assert has_discussion_reminder(body)


def test_scaffold_reminder_is_an_html_comment() -> None:
    """The example ``[[paper-id]]`` must stay commented out.

    Uncommented it would read as a real wikilink to a paper that does not exist
    and health-check's dangling scan (which strips comment regions) would report
    one dangling link per paper in the library.
    """
    assert DISCUSSION_FORMAT_REMINDER.startswith("<!--")
    assert DISCUSSION_FORMAT_REMINDER.endswith("-->")
    assert "[[paper-id]]" in DISCUSSION_FORMAT_REMINDER


def test_ensure_discussion_is_idempotent() -> None:
    body = discussion_scaffold("2024_A")
    assert ensure_discussion_scaffold(body, "2024_A") == body


def test_ensure_discussion_reinserts_after_heading() -> None:
    text = "# Discussion log for 2024_A\n\n## 2026-07-11 10:00\n\n**Question:** q\n"
    healed = ensure_discussion_scaffold(text, "2024_A")
    lines = healed.split("\n")
    assert lines[0] == "# Discussion log for 2024_A"
    assert lines[2] == DISCUSSION_FORMAT_REMINDER
    assert "**Question:** q" in healed  # the log itself is untouched


def test_ensure_discussion_prepends_heading_when_absent() -> None:
    healed = ensure_discussion_scaffold("loose prose, no heading\n", "2024_A")
    assert healed.startswith("# Discussion log for 2024_A")
    assert has_discussion_reminder(healed)
    assert "loose prose, no heading" in healed


def test_ensure_discussion_scaffolds_empty_file() -> None:
    assert ensure_discussion_scaffold("", "2024_A") == discussion_scaffold("2024_A")


def test_heal_discussion_creates_when_absent(tmp_path: Path) -> None:
    _make_paper(tmp_path, "2024_A")  # the paper dir, nothing in it
    disc = tmp_path / "papers" / "2024_A" / "discussion.md"
    assert not disc.exists()
    assert heal_discussion_scaffold(tmp_path, "2024_A") is True
    assert has_discussion_reminder(disc.read_text(encoding="utf-8"))


def test_heal_discussion_keeps_existing_sections(tmp_path: Path) -> None:
    """Backfilling a header must never disturb what the user already wrote."""
    _make_paper(tmp_path, "2024_A")
    disc = tmp_path / "papers" / "2024_A" / "discussion.md"
    disc.write_text(
        "# Discussion log for 2024_A\n\n## 2026-06-30 09:27\n\n**Question:** why?\n",
        encoding="utf-8",
    )
    assert heal_discussion_scaffold(tmp_path, "2024_A") is True
    healed = disc.read_text(encoding="utf-8")
    assert has_discussion_reminder(healed)
    assert "## 2026-06-30 09:27" in healed
    assert "**Question:** why?" in healed


def test_heal_discussion_noop_when_scaffolded(tmp_path: Path) -> None:
    _make_paper(tmp_path, "2024_A")
    disc = tmp_path / "papers" / "2024_A" / "discussion.md"
    body = discussion_scaffold("2024_A") + "\n## 2026-07-11 10:00\n\nkept.\n"
    disc.write_text(body, encoding="utf-8")
    assert heal_discussion_scaffold(tmp_path, "2024_A") is False
    assert disc.read_text(encoding="utf-8") == body  # byte-identical


def test_heal_discussion_noop_when_paper_absent(tmp_path: Path) -> None:
    assert heal_discussion_scaffold(tmp_path, "2024_Nope") is False


def test_heal_discussion_keeps_log_writable(tmp_path: Path) -> None:
    # discussion.md is not truth-locked: the agent must be able to append again.
    _make_paper(tmp_path, "2024_A")
    heal_discussion_scaffold(tmp_path, "2024_A")
    assert os.access(tmp_path / "papers" / "2024_A" / "discussion.md", os.W_OK)
