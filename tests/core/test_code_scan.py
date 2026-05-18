"""Unit tests for the M20 full-text code-URL recall scanner.

Covers the spec's enumerated cases: tail-page hit, multi-mention
count-desc ordering, no-URL empty result, and the never-throws defensive
contract (corrupt / non-PDF / encrypted-ish bytes).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from litman.core.code_scan import scan_code_urls


def test_tail_page_hit(make_text_pdf: Callable[..., Path]) -> None:
    """A github URL only on a non-first page is still recalled, with the
    correct 1-based page number (the Nature/Cell availability-statement case)."""
    pdf = make_text_pdf(
        [
            ["Title: Cell-free system paper", "Abstract: lorem ipsum dolor"],
            ["Introduction text with no links here."],
            ["Methods continued, still no links."],
            [
                "Code availability: source at "
                "https://github.com/pandi-lab/cellfree is provided."
            ],
        ]
    )
    out = scan_code_urls(pdf)
    assert len(out) == 1
    assert out[0]["url"] == "https://github.com/pandi-lab/cellfree"
    assert out[0]["page"] == 4
    assert out[0]["count"] == 1


def test_multi_mention_count_desc_ordering(
    make_text_pdf: Callable[..., Path],
) -> None:
    """A URL mentioned >=2 times gets count>=2, and results sort count-desc."""
    pdf = make_text_pdf(
        [
            [
                "We release https://github.com/team/main as the deliverable.",
                "See also https://gitlab.com/other/aux for a helper.",
            ],
            [
                "Reproduce via https://github.com/team/main again.",
                "And once more: https://github.com/team/main here.",
            ],
        ]
    )
    out = scan_code_urls(pdf)
    urls = [c["url"] for c in out]
    assert "https://github.com/team/main" in urls
    assert "https://gitlab.com/other/aux" in urls

    main = next(c for c in out if c["url"] == "https://github.com/team/main")
    aux = next(c for c in out if c["url"] == "https://gitlab.com/other/aux")
    assert main["count"] == 3
    assert main["page"] == 1
    assert aux["count"] == 1
    # Count-descending: the 3-hit URL must come before the 1-hit URL.
    assert out[0]["url"] == "https://github.com/team/main"
    assert out == sorted(out, key=lambda c: c["count"], reverse=True)


def test_no_url_returns_empty(make_text_pdf: Callable[..., Path]) -> None:
    """A body with no code-host URL yields []."""
    pdf = make_text_pdf(
        [
            ["Pure prose with no repository link whatsoever."],
            ["A plain http://example.com/page link is not a code host."],
        ]
    )
    assert scan_code_urls(pdf) == []


def test_dedup_normalizes_trailing_slash_and_punctuation(
    make_text_pdf: Callable[..., Path],
) -> None:
    """The same repo written with a trailing slash and trailing prose
    punctuation collapses to one candidate with count==2."""
    pdf = make_text_pdf(
        [
            ["Code at https://github.com/foo/bar/."],
            ["Mirror: (https://github.com/foo/bar)"],
        ]
    )
    out = scan_code_urls(pdf)
    assert len(out) == 1
    assert out[0]["url"] == "https://github.com/foo/bar"
    assert out[0]["count"] == 2


def test_extra_hosts_recalled(make_text_pdf: Callable[..., Path]) -> None:
    """Non-github welded hosts (huggingface / zenodo / osf / codeocean) hit."""
    pdf = make_text_pdf(
        [
            [
                "Model: https://huggingface.co/org/model",
                "Data: https://zenodo.org/record/12345",
                "Project: https://osf.io/ab12c/",
                "Capsule: https://codeocean.com/capsule/9999",
            ]
        ]
    )
    out = scan_code_urls(pdf)
    urls = {c["url"] for c in out}
    assert urls == {
        "https://huggingface.co/org/model",
        "https://zenodo.org/record/12345",
        "https://osf.io/ab12c",
        "https://codeocean.com/capsule/9999",
    }


def test_corrupt_bytes_does_not_throw(tmp_path: Path) -> None:
    """Garbage masquerading as a PDF returns [] rather than raising."""
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not a real pdf body\n%%EOF\n")
    assert scan_code_urls(bad) == []


def test_non_pdf_file_does_not_throw(tmp_path: Path) -> None:
    """A plain text file (no PDF header) returns [] rather than raising."""
    txt = tmp_path / "notes.txt"
    txt.write_text("just some text, not a pdf at all", encoding="utf-8")
    assert scan_code_urls(txt) == []


def test_missing_file_does_not_throw(tmp_path: Path) -> None:
    """A nonexistent path returns [] rather than raising."""
    assert scan_code_urls(tmp_path / "nope.pdf") == []


def test_empty_file_does_not_throw(tmp_path: Path) -> None:
    """A zero-byte file returns [] rather than raising."""
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert scan_code_urls(empty) == []
