"""Tests for `lit list --sort recent` and `--unread` (M25).

The recency signal is ``max(paper.pdf mtime, updated-at)``; the unread
signal is an empty ``read-date``. Default sort stays id-ascending.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault


def _seed_with_meta(vault: Path, paper_id: str, meta: dict[str, object]) -> Path:
    """Seed a paper, writing ``metadata.yaml`` from a dict so hyphenated
    keys (``updated-at``, ``read-date``) can be passed verbatim.

    Returns the paper directory (so callers can create ``paper.pdf``).
    """
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines: list[str] = [f"id: {paper_id}"]
    for key, value in meta.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    (paper_dir / "metadata.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return paper_dir


def _make_pdf(paper_dir: Path, mtime: int) -> None:
    pdf = paper_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    os.utime(pdf, (mtime, mtime))


def _invoke(vault: Path, *args: str):
    runner = CliRunner()
    return runner.invoke(cli, ["list", "--library", str(vault), *args])


def _json_ids(vault: Path, *args: str) -> list[str]:
    result = _invoke(vault, "--format", "json", *args)
    assert result.exit_code == 0, result.output
    return [entry["id"] for entry in json.loads(result.output)]


# ---------------------------------------------------------------------------
# --sort recent
# ---------------------------------------------------------------------------


def test_sort_recent_orders_by_pdf_mtime(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    # id order is a < b < c; mtimes are reversed so recency != id order.
    a = _seed_with_meta(v, "p_a", {"year": 2024, "title": "A"})
    b = _seed_with_meta(v, "p_b", {"year": 2024, "title": "B"})
    c = _seed_with_meta(v, "p_c", {"year": 2024, "title": "C"})
    _make_pdf(a, 1_700_000_100)  # oldest
    _make_pdf(b, 1_700_000_200)
    _make_pdf(c, 1_700_000_300)  # newest

    assert _json_ids(v, "--sort", "recent") == ["p_c", "p_b", "p_a"]


def test_sort_recent_uses_updated_at_when_no_pdf(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    old = _seed_with_meta(
        v, "p_old_pdf", {"year": 2024, "title": "old pdf"}
    )
    _make_pdf(old, 1_600_000_000)  # very old pdf mtime
    # No pdf, but a recent updated-at -> must sort ahead of the old-pdf paper.
    _seed_with_meta(
        v, "p_fresh_meta",
        {"year": 2024, "title": "fresh meta",
         "updated-at": "2025-01-01T00:00:00+00:00"},
    )

    assert _json_ids(v, "--sort", "recent") == ["p_fresh_meta", "p_old_pdf"]


def test_sort_recent_takes_max_of_both(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    # Paper with an OLD updated-at but a NEW pdf mtime: max() must pick the
    # pdf mtime, so it outranks a paper whose only signal is a middling one.
    p_max = _seed_with_meta(
        v, "p_max",
        {"year": 2024, "title": "old meta new pdf",
         "updated-at": "2020-01-01T00:00:00+00:00"},
    )
    _make_pdf(p_max, 1_800_000_000)  # newer than the other paper's signal
    # Other paper: only a middling updated-at, no pdf.
    _seed_with_meta(
        v, "p_mid",
        {"year": 2024, "title": "middling meta",
         "updated-at": "2023-06-01T00:00:00+00:00"},
    )

    assert _json_ids(v, "--sort", "recent") == ["p_max", "p_mid"]


def test_sort_recent_missing_both_sinks(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    # Has a signal.
    with_sig = _seed_with_meta(v, "p_sig", {"year": 2024, "title": "sig"})
    _make_pdf(with_sig, 1_700_000_000)
    # Neither pdf nor updated-at -> key 0.0 -> sinks to bottom.
    _seed_with_meta(v, "p_none", {"year": 2024, "title": "none"})

    result = _invoke(v, "--sort", "recent", "--format", "json")
    assert result.exit_code == 0  # does not raise
    ids = [e["id"] for e in json.loads(result.output)]
    assert ids == ["p_sig", "p_none"]


def test_sort_recent_ties_keep_id_order(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    # Two papers with identical recency signal -> stable sort keeps the
    # incoming id-ascending order (list_papers yields id-asc).
    a = _seed_with_meta(v, "p_aaa", {"year": 2024, "title": "aaa"})
    b = _seed_with_meta(v, "p_bbb", {"year": 2024, "title": "bbb"})
    _make_pdf(a, 1_700_000_500)
    _make_pdf(b, 1_700_000_500)  # equal mtime

    assert _json_ids(v, "--sort", "recent") == ["p_aaa", "p_bbb"]


def test_malformed_updated_at_no_crash(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    _seed_with_meta(
        v, "p_bad",
        {"year": 2024, "title": "bad", "updated-at": "not-a-date"},
    )
    good = _seed_with_meta(v, "p_good", {"year": 2024, "title": "good"})
    _make_pdf(good, 1_700_000_000)

    result = _invoke(v, "--sort", "recent", "--format", "json")
    assert result.exit_code == 0  # malformed updated-at -> 0.0, no crash
    ids = [e["id"] for e in json.loads(result.output)]
    # good (has pdf signal) ranks ahead of bad (signal collapses to 0.0).
    assert ids == ["p_good", "p_bad"]


# ---------------------------------------------------------------------------
# --unread
# ---------------------------------------------------------------------------


def test_unread_excludes_papers_with_read_date(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    _seed_with_meta(
        v, "p_read",
        {"year": 2024, "title": "read", "read-date": "2024-05-01"},
    )
    _seed_with_meta(
        v, "p_unread",
        {"year": 2024, "title": "unread", "read-date": None},
    )

    ids = _json_ids(v, "--unread")
    assert ids == ["p_unread"]


def test_unread_and_sort_recent_compose(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    # Read paper (excluded), even though it has the newest signal.
    read = _seed_with_meta(
        v, "p_read",
        {"year": 2024, "title": "read", "read-date": "2024-05-01"},
    )
    _make_pdf(read, 1_900_000_000)  # newest, but read -> filtered out
    # Two unread papers, ordered by recency.
    old = _seed_with_meta(v, "p_unread_old", {"year": 2024, "title": "uold"})
    _make_pdf(old, 1_700_000_100)
    new = _seed_with_meta(v, "p_unread_new", {"year": 2024, "title": "unew"})
    _make_pdf(new, 1_700_000_900)

    ids = _json_ids(v, "--unread", "--sort", "recent")
    assert ids == ["p_unread_new", "p_unread_old"]
    assert "p_read" not in ids


# ---------------------------------------------------------------------------
# Default-sort regression
# ---------------------------------------------------------------------------


def test_default_sort_is_id_ascending(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    # Seed out of id order with recency that would reorder under --sort recent.
    c = _seed_with_meta(v, "p_c", {"year": 2024, "title": "C"})
    a = _seed_with_meta(v, "p_a", {"year": 2024, "title": "A"})
    b = _seed_with_meta(v, "p_b", {"year": 2024, "title": "B"})
    _make_pdf(a, 1_700_000_100)
    _make_pdf(b, 1_700_000_200)
    _make_pdf(c, 1_700_000_300)

    # No --sort -> id ascending, unaffected by mtime.
    assert _json_ids(v) == ["p_a", "p_b", "p_c"]
