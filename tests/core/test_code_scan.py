"""Unit tests for the M20 full-text code-URL recall scanner.

Covers the spec's enumerated cases: tail-page hit, multi-mention
count-desc ordering, no-URL empty result, and the never-throws defensive
contract (corrupt / non-PDF / encrypted-ish bytes). Also covers URLs the
page layout broke across lines: the ones to join and the ones to leave alone.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# URLs broken across lines. The URL fragments are the ones the spec quotes
# from real papers; the words around them are filler. Each fixture line is one
# extracted line, trailing spaces included.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "full", "truncated"),
    [
        (
            [
                "Lorem ipsum dolor sit amet, available in: https://",
                "github.com/MolecularAI/PepINVENT/. The repository also",
            ],
            "https://github.com/MolecularAI/PepINVENT",
            "https://github.com",
        ),
        (
            [
                "Lorem ipsum dolor sit amet https://github.com/",
                "igashov/DiffLinker. The lorem ipsum dolor sit amet",
            ],
            "https://github.com/igashov/DiffLinker",
            "https://github.com",
        ),
        (
            [
                "https://github.com/dfwlab/",
                "cyclicpeptide and Pypi at lorem ipsum dolor sit amet.",
            ],
            "https://github.com/dfwlab/cyclicpeptide",
            "https://github.com/dfwlab",
        ),
        (
            [
                "Lorem ipsum dolor sit amet repo:https://github.com/Jamson-",
                "Zhong/Graph2Edits. Source lorem ipsum dolor sit amet.",
            ],
            "https://github.com/Jamson-Zhong/Graph2Edits",
            "https://github.com/Jamson-",
        ),
        (
            ["Lorem ipsum https://github.com/dahvida/NP_ ", "Fingerprints (p49) "],
            "https://github.com/dahvida/NP_Fingerprints",
            "https://github.com/dahvida/NP_",
        ),
        (
            ["https://", "github.com/danielgarzonotero/", "PepMNet.git"],
            "https://github.com/danielgarzonotero/PepMNet.git",
            "https://github.com/danielgarzonotero",
        ),
        (
            ["Lorem ipsum https://huggingface.co/ ", "ChatterjeeLab/PepTune (p29) "],
            "https://huggingface.co/ChatterjeeLab/PepTune",
            "https://huggingface.co",
        ),
    ],
    ids=[
        "bare-scheme",
        "bare-host",
        "owner-only",
        "hyphen-split",
        "underscore-split",
        "three-line-chain",
        "huggingface-bare-host",
    ],
)
def test_line_broken_url_is_joined(
    make_text_pdf: Callable[..., Path],
    lines: list[str],
    full: str,
    truncated: str,
) -> None:
    """A URL broken at the end of a line is reported whole, on the page it
    starts on, and the cut-off address is not reported at all."""
    pdf = make_text_pdf([["Introduction without links."], lines])
    out = scan_code_urls(pdf)
    urls = [c["url"] for c in out]
    assert full in urls
    assert truncated not in urls
    assert out == [{"url": full, "page": 2, "count": 1}]


def test_line_broken_url_join_stops_after_three_lines(
    make_text_pdf: Callable[..., Path],
) -> None:
    """A chain that would need a fourth line is cut after three joined lines."""
    pdf = make_text_pdf(
        [["https://", "github.com/", "owner/", "name-", "rest"]]
    )
    urls = [c["url"] for c in scan_code_urls(pdf)]
    assert urls == ["https://github.com/owner/name-"]
    assert "https://github.com/owner/name-rest" not in urls


def test_complete_repo_url_ending_in_slash_is_not_joined(
    make_text_pdf: Callable[..., Path],
) -> None:
    """An owner/repo address ending in ``/`` does not swallow the next line's
    prose."""
    pdf = make_text_pdf(
        [
            [
                "Lorem ipsum dolor sit amet https://github.com/MolecularAI/PepINVENT/",
                "The repository also lorem ipsum dolor sit amet.",
            ]
        ]
    )
    urls = [c["url"] for c in scan_code_urls(pdf)]
    assert urls == ["https://github.com/MolecularAI/PepINVENT"]
    assert "https://github.com/MolecularAI/PepINVENT/The" not in urls


def test_url_ending_in_full_stop_is_not_joined(
    make_text_pdf: Callable[..., Path],
) -> None:
    """A trailing ``.`` is the sentence's full stop, not a broken URL."""
    pdf = make_text_pdf(
        [
            [
                "Lorem ipsum dolor sit amet https://github.com/gcorso/DiffDock.",
                "Hyperparameters. Lorem ipsum dolor sit amet.",
            ]
        ]
    )
    urls = [c["url"] for c in scan_code_urls(pdf)]
    assert urls == ["https://github.com/gcorso/DiffDock"]
    assert "https://github.com/gcorso/DiffDock.Hyperparameters" not in urls


def test_url_starting_the_next_line_is_not_glued_on(
    make_text_pdf: Callable[..., Path],
) -> None:
    """A short URL, or a bare scheme, at the end of a line is not joined with a
    next line that starts a URL of its own."""
    pdf = make_text_pdf(
        [
            [
                "Lorem ipsum https://github.com/IBM/",
                "https://github.com/AstraZeneca/peptide-tools dolor sit amet.",
                "Lorem ipsum https://",
                "https://github.com/team/tool dolor sit amet.",
            ]
        ]
    )
    out = scan_code_urls(pdf)
    assert out == [
        {"url": "https://github.com/IBM", "page": 1, "count": 1},
        {"url": "https://github.com/AstraZeneca/peptide-tools", "page": 1, "count": 1},
        {"url": "https://github.com/team/tool", "page": 1, "count": 1},
    ]
    assert "https://github.com/IBM/https://github.com/AstraZeneca/peptide-tools" not in [
        c["url"] for c in out
    ]


def test_osf_project_address_is_complete_at_one_segment(
    make_text_pdf: Callable[..., Path],
) -> None:
    """``osf.io/<id>/`` at a line end is complete, so the next line's prose is
    not joined; a bare ``osf.io/`` host still is."""
    complete = make_text_pdf(
        [["Lorem ipsum https://osf.io/ab12c/", "Dolor sit amet."]], name="complete.pdf"
    )
    bare = make_text_pdf([["Lorem ipsum https://osf.io/", "ab12c/"]], name="bare.pdf")
    assert [c["url"] for c in scan_code_urls(complete)] == ["https://osf.io/ab12c"]
    assert [c["url"] for c in scan_code_urls(bare)] == ["https://osf.io/ab12c"]


def test_url_not_cut_by_the_line_end_is_unchanged(
    make_text_pdf: Callable[..., Path],
) -> None:
    """A complete URL at the end of a line, and a URL inside a line, are
    reported as they were before line-broken URLs were joined."""
    pdf = make_text_pdf(
        [
            [
                "Code: https://github.com/team/tool",
                "release notes follow on the next line.",
                "See https://github.com/ for the hosting service",
                "owner/repo is not part of that address.",
            ]
        ]
    )
    out = scan_code_urls(pdf)
    assert out == [
        {"url": "https://github.com/team/tool", "page": 1, "count": 1},
        {"url": "https://github.com", "page": 1, "count": 1},
    ]


def test_url_broken_across_a_page_break_is_not_joined(
    make_text_pdf: Callable[..., Path],
) -> None:
    """The last line of one page is never joined with the next page."""
    pdf = make_text_pdf(
        [
            ["Introduction.", "Code availability: https://github.com/"],
            ["owner/repo holds the scripts."],
        ]
    )
    out = scan_code_urls(pdf)
    assert out == [{"url": "https://github.com", "page": 1, "count": 1}]
    assert "https://github.com/owner/repo" not in [c["url"] for c in out]
