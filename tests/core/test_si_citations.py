"""Tests for ``litman.core.si_citations`` — finding ``si-NN`` citations in a
paper's notes and telling which no longer match its SI records.

Pure text: every case is a string in, citations or problems out. The health
check and the delete confirmation that use it are covered in
tests/commands/test_health.py, tests/commands/test_si_cli.py and
tests/server/test_server_si.py.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from litman.core.si_citations import (
    SiCitation,
    added_date,
    citation_problems,
    count_citations,
    read_si_citations,
    refers_to,
    scan_si_citations,
)


def _tokens(text: str) -> list[str]:
    return [c.token for c in scan_si_citations(text, "notes.md", dated=False)]


def _originals(text: str) -> list[str | None]:
    return [c.original for c in scan_si_citations(text, "notes.md", dated=False)]


def _sections(text: str) -> list[date | None]:
    return [c.section for c in scan_si_citations(text, "discussion.md", dated=True)]


def _record(
    file: str, original: str | None = None, added: object = "2026-10-21T14:03:12+08:00"
) -> dict[str, object]:
    record: dict[str, object] = {"file": file, "added": added}
    if original is not None:
        record["original"] = original
    return record


def _cite(
    token: str,
    original: str | None = None,
    section: date | None = None,
    file: str = "discussion.md",
    line: int = 1,
) -> SiCitation:
    number = int(token.split("-", 1)[1].split(".", 1)[0])
    return SiCitation(file, line, token, number, original, section)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("see si-01 here", ["si-01"]),
        ("si-01.pdf and si-02.tar.gz", ["si-01.pdf", "si-02.tar.gz"]),
        ("si-100.csv", ["si-100.csv"]),
        ("supplementary/si-01.pdf", ["si-01.pdf"]),
        ("`si-01.pdf`, **si-02.xlsx**", ["si-01.pdf", "si-02.xlsx"]),
        ("(si-03)", ["si-03"]),
        ("end of a sentence: si-04.", ["si-04"]),
        ("si-05.PDF has an upper-case extension", ["si-05"]),
        # Not citations.
        ("si-1 has one digit", []),
        ("SI-01 is upper case", []),
        ("psi-01 is a word", []),
        ("x-si-01 and si_si-01", []),
        ("si-01a", []),
        ("a backup, si-01.pdf_old", []),
        ("lit rm X --si si-01 --yes", []),
        ("lit open X --si=si-02.xlsx", []),
        ("no citation at all", []),
    ],
)
def test_tokens(text: str, tokens: list[str]) -> None:
    assert _tokens(text) == tokens


def test_the_same_citation_twice_on_a_line_is_one() -> None:
    cites = scan_si_citations(
        "si-01 and si-01 again, si-01.pdf too\nsi-01\n", "notes.md", dated=False
    )
    assert [(c.line, c.token) for c in cites] == [
        (1, "si-01"),
        (1, "si-01.pdf"),
        (2, "si-01"),
    ]


def test_a_citation_carries_its_line_and_number() -> None:
    (cite,) = scan_si_citations("# T\n\nsee si-007.csv\n", "notes.md", dated=False)
    assert (cite.file, cite.line, cite.token, cite.number) == (
        "notes.md", 3, "si-007.csv", 7
    )


# ---------------------------------------------------------------------------
# Original names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "original"),
    [
        ("si-01.pdf（ci9b00237_si_001.pdf）", "ci9b00237_si_001.pdf"),
        ("si-01.pdf (ci9b00237_si_001.pdf)", "ci9b00237_si_001.pdf"),
        ("si-01.pdf  (Table_S2.xlsx)", "Table_S2.xlsx"),
        ("`si-01.pdf` (`a.pdf`)", "a.pdf"),
        ("**si-01.pdf**（**a.pdf**）", "a.pdf"),
        ("si-01.xlsx (Table S2 (final).xlsx)", "Table S2 (final).xlsx"),
        ("si-01.pdf (x (1).pdf) and more (text)", "x (1).pdf"),
        ("si-01.pdf（a.pdf，第 12 页）", "a.pdf"),
        ("si-01.pdf（a.pdf；表 S3）", "a.pdf"),
        ("si-01.pdf (a.pdf, p. 12)", "a.pdf"),
        ("si-01.pdf (a.pdf; Table S3)", "a.pdf"),
        ("si-01.tar.gz (raw data.tar.gz)", "raw data.tar.gz"),
        ("si-01.pdf (A.PDF)", "A.PDF"),
        # Not a file name.
        ("si-01.pdf (p. 12)", None),
        ("si-01.pdf（Table S3）", None),
        ("si-01.pdf (Fig. 3)", None),
        ("si-01.pdf (v.2)", None),
        ("si-01.pdf (see Table S3, p. 4)", None),
        ("si-01.pdf (folder/a.pdf)", None),
        ("si-01.pdf (.pdf)", None),
        ("si-01.pdf (a.toolongextension)", None),
        ("si-01.pdf（Fig.S3）", None),
        ("si-01.pdf (Tab.S2)", None),
        ("si-01.pdf (Eq.S1)", None),
        ("si-01.pdf (p.S12)", None),
        ("si-01.pdf (Fig.S3a)", None),
        ("si-01.pdf (fig.s3)", None),
        # Not right after it, or never closed on the line.
        ("si-01.pdf is (a.pdf)", None),
        ("si-01.pdf (a.pdf", None),
        ("si-01.pdf (x (1).pdf", None),
        ("si-01.pdf（a.pdf)", None),
        ("si-01.pdf", None),
    ],
)
def test_original_names(text: str, original: str | None) -> None:
    assert _originals(text) == [original]


# ---------------------------------------------------------------------------
# What is skipped: comments, fenced code
# ---------------------------------------------------------------------------


def test_a_paren_that_never_closes_nearby_is_no_original() -> None:
    far = "x" * 300
    assert _originals(f"si-01.pdf (a{far}.pdf)") == [None]
    assert _originals(f"si-01.pdf (a.pdf) {far}") == ["a.pdf"]


def test_a_long_line_of_unclosed_parens_is_read_quickly() -> None:
    line = "si-01 (" * 15000  # ~100 000 characters, never closed
    started = time.monotonic()
    cites = scan_si_citations(line, "notes.md", dated=False)
    elapsed = time.monotonic() - started
    assert [(c.token, c.original) for c in cites] == [("si-01", None)]
    assert elapsed < 5, elapsed


def test_comments_are_skipped_and_keep_line_numbers() -> None:
    text = (
        "# T\n"
        "<!-- si-01 in a\n"
        "multi-line comment si-02 -->\n"
        "page one\fsi-03 after a form feed\n"
        "<!-- si-04 --> si-05\n"
    )
    cites = scan_si_citations(text, "notes.md", dated=False)
    assert [(c.line, c.token) for c in cites] == [(4, "si-03"), (5, "si-05")]


def test_fenced_blocks_are_skipped() -> None:
    text = (
        "si-01\n"
        "```bash\n"
        "lit open X si-02\n"
        "~~~\n"
        "si-03 still inside\n"
        "```\n"
        "si-04\n"
        "~~~~\n"
        "si-05\n"
        "```\n"
        "~~~~\n"
        "si-06\n"
    )
    assert _tokens(text) == ["si-01", "si-04", "si-06"]


def test_inline_code_in_triple_backticks_opens_no_fence() -> None:
    text = "```code``` si-01\nsi-02\n````x`` y\nsi-03\n~~~ a `tilde` fence\nsi-04\n~~~\nsi-05\n"
    assert _tokens(text) == ["si-01", "si-02", "si-03", "si-05"]


def test_a_heading_in_a_fence_does_not_open_a_section() -> None:
    text = "```\n## 2026-01-01\n```\nsi-01\n"
    assert _sections(text) == [None]


# ---------------------------------------------------------------------------
# Dated sections (discussion.md)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "section"),
    [
        ("## 2026-09-02", date(2026, 9, 2)),
        ("## 2026-09-02 10:00", date(2026, 9, 2)),
        ("## 2026-09-02 10:00（reading Table S2）", date(2026, 9, 2)),
        ("## 2026-09-02 (a label)", date(2026, 9, 2)),
        ("## 2026-09-02T10:00", date(2026, 9, 2)),
        ("##  2026-09-02 10:00", date(2026, 9, 2)),
        ("   ## 2026-09-02 10:00", date(2026, 9, 2)),  # up to three spaces
        ("    ## 2026-09-02 10:00", None),  # four make it a code block
        ("## 2026-02-30 10:00", None),  # no such day
        ("## 2026-09-021", None),
        ("## Notes on 2026-09-02", None),
        ("## Summary", None),
    ],
)
def test_section_headings(heading: str, section: date | None) -> None:
    assert _sections(f"{heading}\nsi-01\n") == [section]


def test_sections_open_and_close() -> None:
    text = (
        "# Discussion log\n"
        "si-01 before any section\n"
        "2026-09-01 10:00\n"  # a bare date line is no heading
        "si-02\n"
        "## 2026-09-02 10:00\n"
        "si-03\n"
        "### 1. a sub-topic, 2026-01-01\n"
        "si-04\n"
        "## Undated\n"
        "si-05\n"
        "## 2026-10-05\n"
        "si-06\n"
        "# Another top heading\n"
        "si-07\n"
        "##\n"
    )
    assert _sections(text) == [
        None, None, date(2026, 9, 2), date(2026, 9, 2), None, date(2026, 10, 5), None
    ]


def test_a_byte_order_mark_does_not_hide_the_first_heading() -> None:
    cites = scan_si_citations(
        "\ufeff## 2026-09-02\nsi-01\n", "discussion.md", dated=True
    )
    assert [(c.line, c.section) for c in cites] == [(2, date(2026, 9, 2))]


def test_notes_are_never_dated() -> None:
    cites = scan_si_citations("## 2026-09-02\nsi-01\n", "notes.md", dated=False)
    assert [c.section for c in cites] == [None]


# ---------------------------------------------------------------------------
# Reading the files
# ---------------------------------------------------------------------------


def test_read_si_citations_reads_both_files_and_names_an_unreadable_one(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.md").write_bytes(b"si-01 caf\xe9\n")
    (tmp_path / "discussion.md").write_text(
        "## 2026-09-02\nsi-02.pdf\n", encoding="utf-8"
    )
    cites, unreadable = read_si_citations(tmp_path)
    assert [(c.file, c.line, c.token, c.section) for c in cites] == [
        ("discussion.md", 2, "si-02.pdf", date(2026, 9, 2))
    ]
    assert unreadable == [("notes.md", "UnicodeDecodeError")]


def test_read_si_citations_without_notes(tmp_path: Path) -> None:
    assert read_si_citations(tmp_path) == ([], [])


def test_a_notes_file_that_cannot_even_be_looked_at_is_unchecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "notes.md").write_text("si-01\n", encoding="utf-8")
    (tmp_path / "discussion.md").write_text("si-02\n", encoding="utf-8")
    real_is_file = Path.is_file

    def denied(self: Path) -> bool:
        if self.name == "notes.md":
            raise PermissionError(13, "Permission denied (mocked)")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", denied)
    cites, unreadable = read_si_citations(tmp_path)
    assert [c.token for c in cites] == ["si-02"]
    assert unreadable == [("notes.md", "PermissionError")]


# ---------------------------------------------------------------------------
# Matching records, counting
# ---------------------------------------------------------------------------


def test_a_token_with_an_extension_names_exactly_that_file() -> None:
    pdf = _record("si-01.pdf", "a.pdf")
    assert refers_to(_cite("si-01.pdf"), pdf)
    assert refers_to(_cite("si-01"), pdf)
    assert not refers_to(_cite("si-01.xlsx"), pdf)
    assert not refers_to(_cite("si-02"), pdf)
    assert refers_to(_cite("si-001"), pdf)  # the number, not its spelling
    assert refers_to(_cite("si-001.pdf"), pdf)
    assert not refers_to(_cite("si-001.xlsx"), pdf)
    archive = _record("si-02.tar.gz")
    assert refers_to(_cite("si-02.tar.gz"), archive)
    assert not refers_to(_cite("si-02.gz"), archive)


def test_original_names_are_compared_folded() -> None:
    record = _record("si-01.pdf", "Café S1.PDF")
    assert refers_to(_cite("si-01.pdf", "café s1.pdf"), record)
    assert not refers_to(_cite("si-01.pdf", "other.pdf"), record)
    # Either side without an original: the number decides.
    assert refers_to(_cite("si-01.pdf", "other.pdf"), _record("si-01.pdf"))
    assert refers_to(_cite("si-01.pdf"), record)


def test_original_label_counts_as_the_name_too() -> None:
    record = _record("si-01.pdf", "Table\tS1.pdf")  # printed as "Table S1.pdf"
    assert refers_to(_cite("si-01.pdf", "Table S1.pdf"), record)


def test_count_citations_counts_lines_per_file_in_file_order() -> None:
    record = _record("si-02.xlsx", "b.xlsx")
    cites = [
        _cite("si-02", file="discussion.md", line=4),
        _cite("si-02.xlsx", file="discussion.md", line=4),
        _cite("si-02.xlsx", "b.xlsx", file="discussion.md", line=9),
        _cite("si-02.xlsx", "other.xlsx", file="discussion.md", line=10),
        _cite("si-02", file="notes.md", line=1),
        _cite("si-03", file="notes.md", line=2),
    ]
    counts = count_citations(cites, record)
    assert counts == {"notes.md": 1, "discussion.md": 2}
    assert list(counts) == ["notes.md", "discussion.md"]
    assert count_citations([], record) == {}


# ---------------------------------------------------------------------------
# added_date
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-21T14:03:12+08:00", date(2026, 10, 21)),
        ("2026-10-21T23:30:00-05:00", date(2026, 10, 21)),  # the date as written
        ("2026-10-21", date(2026, 10, 21)),
        (datetime(2026, 10, 21, 1, 0, tzinfo=UTC), date(2026, 10, 21)),
        (date(2026, 10, 21), date(2026, 10, 21)),
        ("yesterday", None),
        (None, None),
        (20261021, None),
    ],
)
def test_added_date(value: object, expected: date | None) -> None:
    assert added_date(value) == expected


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


_RECORDS = [
    _record("si-01.pdf", "ci9b00237_si_001.pdf"),
    _record("si-03.xlsx", "Table_S2.xlsx"),
]


def _kinds(cites: list[SiCitation]) -> list[tuple[str, str, str | None]]:
    return [
        (p.citation.token, p.kind, p.moved_to)
        for p in citation_problems(cites, _RECORDS)
    ]


def test_case_1_a_number_the_paper_no_longer_has() -> None:
    assert _kinds([_cite("si-02", line=1), _cite("si-07.xlsx", line=2)]) == [
        ("si-02", "gone", None),
        ("si-07.xlsx", "gone", None),
    ]


def test_one_finding_per_line_file_and_kind() -> None:
    """The same file twice on a line, in two spellings, is one finding."""
    dated = date(2026, 9, 2)
    cites = [
        _cite("si-02", line=4),
        _cite("si-02.xlsx", line=4),
        _cite("si-05", line=4),
        _cite("si-01", section=dated, line=7),
        _cite("si-01.pdf", section=dated, line=7),
        _cite("si-01", section=dated, line=8),
    ]
    assert [
        (p.citation.line, p.citation.token, p.kind)
        for p in citation_problems(cites, _RECORDS)
    ] == [
        (4, "si-02", "gone"),
        (4, "si-05", "gone"),
        (7, "si-01", "predates"),
        (8, "si-01", "predates"),
    ]


def test_no_case_4_beside_the_right_original_on_the_same_line() -> None:
    dated = date(2026, 9, 2)
    cites = [
        _cite("si-01.pdf", "ci9b00237_si_001.pdf", section=dated, line=5),
        _cite("si-01.pdf", section=dated, line=5),
        _cite("si-01.pdf", section=dated, line=6),
    ]
    assert [
        (p.citation.line, p.kind) for p in citation_problems(cites, _RECORDS)
    ] == [(6, "predates")]


def test_a_longer_spelling_with_an_extension_finds_its_record() -> None:
    assert _kinds([_cite("si-001.pdf"), _cite("si-003.pdf")]) == [
        ("si-003.pdf", "gone", None)
    ]


def test_case_1_includes_a_number_whose_file_has_another_extension() -> None:
    assert _kinds([_cite("si-01.xlsx")]) == [("si-01.xlsx", "gone", None)]


def test_case_2_another_original_under_the_number() -> None:
    (problem,) = citation_problems([_cite("si-01.pdf", "other.pdf")], _RECORDS)
    assert problem.kind == "renamed"
    assert problem.record is not None and problem.record["file"] == "si-01.pdf"


def test_case_3_the_cited_file_now_has_another_number() -> None:
    assert _kinds(
        [_cite("si-01.pdf", "Table_S2.xlsx"), _cite("si-02.xlsx", "Table_S2.xlsx")]
    ) == [
        ("si-01.pdf", "renamed", "si-03.xlsx"),
        ("si-02.xlsx", "gone", "si-03.xlsx"),
    ]


def test_case_4_written_before_the_file_was_added() -> None:
    before = _cite("si-01.pdf", section=date(2026, 9, 2))
    (problem,) = citation_problems([before], _RECORDS)
    assert (problem.kind, problem.added) == ("predates", date(2026, 10, 21))


def test_case_4_does_not_fire_on_the_same_day_or_later() -> None:
    cites = [
        _cite("si-01.pdf", section=date(2026, 10, 21)),
        _cite("si-01.pdf", section=date(2026, 10, 22)),
    ]
    assert citation_problems(cites, _RECORDS) == []


def test_case_4_needs_no_original_and_a_dated_section() -> None:
    cites = [
        _cite("si-01.pdf", "ci9b00237_si_001.pdf", section=date(2026, 9, 2)),
        _cite("si-01.pdf", file="notes.md"),
        _cite("si-01.pdf"),  # undated part of discussion.md
    ]
    assert citation_problems(cites, _RECORDS) == []


def test_case_4_with_an_added_date_that_cannot_be_read() -> None:
    records = [_record("si-01.pdf", "a.pdf", added="garbage")]
    (problem,) = citation_problems(
        [_cite("si-01", section=date(2026, 9, 2))], records
    )
    assert (problem.kind, problem.added) == ("predates", None)


def test_matching_citations_are_no_problem() -> None:
    cites = [
        _cite("si-01.pdf", "ci9b00237_si_001.pdf"),
        _cite("si-01"),
        _cite("si-03.xlsx", "table_s2.XLSX"),
    ]
    assert citation_problems(cites, _RECORDS) == []
    assert citation_problems([], _RECORDS) == []
