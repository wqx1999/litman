"""Tests for ``litman.core.dedup`` (M2.9 duplicate detection helpers)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from litman.core.dedup import (
    auto_suffix_id,
    canonicalize_doi,
    find_paper_by_doi,
    normalize_doi,
    suggest_alternative_ids,
)
from litman.core.library import create_vault
from litman.exceptions import AddError

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _write_paper(
    vault: Path,
    paper_id: str,
    doi: str = "",
    title: str = "Test paper",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Materialize a minimal paper folder with a metadata.yaml."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": title,
        "doi": doi,
        "year": 2024,
        "created-at": "2026-04-28T15:00:00+02:00",
        "updated-at": "2026-04-28T15:00:00+02:00",
        **(extra or {}),
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)
    return paper_dir


# ---------------------------------------------------------------------------
# normalize_doi
# ---------------------------------------------------------------------------


def test_normalize_doi_lowercases_and_strips() -> None:
    assert normalize_doi("  10.1093/BIOINFORMATICS/btae364  ") == (
        "10.1093/bioinformatics/btae364"
    )


def test_normalize_doi_empty_string_stays_empty() -> None:
    assert normalize_doi("") == ""
    assert normalize_doi("   ") == ""


# ---------------------------------------------------------------------------
# canonicalize_doi (review F10/F11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "10.1093/bioinformatics/btae364",
        "https://doi.org/10.1093/bioinformatics/btae364",
        "http://doi.org/10.1093/bioinformatics/btae364",
        "https://dx.doi.org/10.1093/bioinformatics/btae364",
        "doi:10.1093/bioinformatics/btae364",
        "DOI: 10.1093/bioinformatics/btae364",
        "  https://doi.org/10.1093/bioinformatics/btae364  ",
    ],
)
def test_canonicalize_doi_strips_all_prefix_forms(raw: str) -> None:
    assert canonicalize_doi(raw) == "10.1093/bioinformatics/btae364"


def test_canonicalize_doi_preserves_body_case() -> None:
    # Prefix match is case-insensitive, but the DOI body is untouched.
    assert canonicalize_doi("https://doi.org/10.1/ABC") == "10.1/ABC"


def test_canonicalize_doi_empty() -> None:
    assert canonicalize_doi("") == ""
    assert canonicalize_doi("   ") == ""


def test_find_paper_by_doi_matches_across_prefix_forms(vault: Path) -> None:
    # F10: a paper stored with a bare DOI must be found when the query carries
    # a resolver-URL / doi: prefix (and vice versa) — otherwise the same paper
    # is added twice.
    _write_paper(vault, "2024_Chen_HELM", doi="10.1093/bioinformatics/btae364")
    for query in (
        "https://doi.org/10.1093/bioinformatics/btae364",
        "doi:10.1093/bioinformatics/btae364",
        "https://dx.doi.org/10.1093/BIOINFORMATICS/BTAE364",
    ):
        hit = find_paper_by_doi(vault, query)
        assert hit is not None, query
        assert hit[0] == "2024_Chen_HELM"


# ---------------------------------------------------------------------------
# find_paper_by_doi
# ---------------------------------------------------------------------------


def test_find_paper_by_doi_empty_query_returns_none(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    assert find_paper_by_doi(vault, "") is None


def test_find_paper_by_doi_no_papers_dir_returns_none(tmp_path: Path) -> None:
    # Bare directory, no vault init.
    assert find_paper_by_doi(tmp_path, "10.1/x") is None


def test_find_paper_by_doi_hit(vault: Path) -> None:
    _write_paper(vault, "2024_Chen_HELM", doi="10.1093/bioinformatics/btae364")
    hit = find_paper_by_doi(vault, "10.1093/bioinformatics/btae364")
    assert hit is not None
    pid, meta = hit
    assert pid == "2024_Chen_HELM"
    assert meta["doi"] == "10.1093/bioinformatics/btae364"


def test_find_paper_by_doi_case_insensitive(vault: Path) -> None:
    _write_paper(vault, "2024_Chen_HELM", doi="10.1093/Bioinformatics/btae364")
    hit = find_paper_by_doi(vault, "10.1093/BIOINFORMATICS/BTAE364")
    assert hit is not None
    assert hit[0] == "2024_Chen_HELM"


def test_find_paper_by_doi_no_match_returns_none(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    assert find_paper_by_doi(vault, "10.1/different") is None


def test_find_paper_by_doi_skips_paper_without_metadata(vault: Path) -> None:
    # Paper folder exists, but no metadata.yaml. Should be silently skipped.
    (vault / "papers" / "2024_Orphan").mkdir()
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    hit = find_paper_by_doi(vault, "10.1/x")
    assert hit is not None
    assert hit[0] == "2024_Smith_X"


def test_find_paper_by_doi_skips_corrupt_metadata(vault: Path) -> None:
    # Paper folder with garbage metadata.yaml.
    bad_dir = vault / "papers" / "2024_Corrupt"
    bad_dir.mkdir()
    (bad_dir / "metadata.yaml").write_text("not: : valid: yaml: [", encoding="utf-8")
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    hit = find_paper_by_doi(vault, "10.1/x")
    assert hit is not None
    assert hit[0] == "2024_Smith_X"


def test_find_paper_by_doi_ignores_papers_with_empty_doi(vault: Path) -> None:
    _write_paper(vault, "2024_NoDoi", doi="")
    assert find_paper_by_doi(vault, "10.1/x") is None


def test_find_paper_by_doi_ignores_non_directory_entries(vault: Path) -> None:
    # A stray file under papers/ should be ignored.
    (vault / "papers" / "stray.txt").write_text("not a paper", encoding="utf-8")
    _write_paper(vault, "2024_Smith_X", doi="10.1/x")
    hit = find_paper_by_doi(vault, "10.1/x")
    assert hit is not None
    assert hit[0] == "2024_Smith_X"


# ---------------------------------------------------------------------------
# suggest_alternative_ids
# ---------------------------------------------------------------------------


def test_suggest_alternatives_returns_offset_candidates(vault: Path) -> None:
    title = "Cell-free biosynthesis of antimicrobial peptides at scale"
    alts = suggest_alternative_ids(
        vault,
        primary_id="2024_Smith_Cell-free-biosynthesis-antimicrobial",
        year=2024,
        family="Smith",
        title=title,
        n=3,
    )
    assert len(alts) == 3
    assert all(a.startswith("2024_Smith_") for a in alts)
    # Different alternatives, none equal primary.
    assert len(set(alts)) == 3
    assert "2024_Smith_Cell-free-biosynthesis-antimicrobial" not in alts


def test_suggest_alternatives_skips_existing_disk_collisions(vault: Path) -> None:
    title = "Cell-free biosynthesis of antimicrobial peptides at scale"
    # Pre-create a folder that matches the first alternative offset.
    _write_paper(vault, "2024_Smith_Biosynthesis-antimicrobial-peptides", doi="")

    alts = suggest_alternative_ids(
        vault,
        primary_id="2024_Smith_Cell-free-biosynthesis-antimicrobial",
        year=2024,
        family="Smith",
        title=title,
        n=3,
    )
    assert "2024_Smith_Biosynthesis-antimicrobial-peptides" not in alts


def test_suggest_alternatives_empty_for_short_title(vault: Path) -> None:
    alts = suggest_alternative_ids(
        vault,
        primary_id="2024_Smith_BERT",
        year=2024,
        family="Smith",
        title="BERT",
        n=3,
    )
    assert alts == []


def test_suggest_alternatives_n_zero(vault: Path) -> None:
    alts = suggest_alternative_ids(
        vault,
        primary_id="2024_Smith_Foo",
        year=2024,
        family="Smith",
        title="Foo bar baz qux",
        n=0,
    )
    assert alts == []


# ---------------------------------------------------------------------------
# auto_suffix_id
# ---------------------------------------------------------------------------


def test_auto_suffix_returns_base_when_free(vault: Path) -> None:
    assert auto_suffix_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo"


def test_auto_suffix_returns_b_when_base_taken(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_Foo")
    assert auto_suffix_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo_b"


def test_auto_suffix_returns_c_when_b_also_taken(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_Foo")
    _write_paper(vault, "2024_Smith_Foo_b")
    assert auto_suffix_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo_c"


def test_auto_suffix_skips_taken_slots_in_order(vault: Path) -> None:
    # Hole at _c: base, _b, _d taken → expect _c (smallest free).
    _write_paper(vault, "2024_Smith_Foo")
    _write_paper(vault, "2024_Smith_Foo_b")
    _write_paper(vault, "2024_Smith_Foo_d")
    assert auto_suffix_id(vault, "2024_Smith_Foo") == "2024_Smith_Foo_c"


def test_auto_suffix_exhausted_raises(vault: Path) -> None:
    _write_paper(vault, "2024_Smith_Foo")
    for c in "bcdefghijklmnopqrstuvwxyz":
        _write_paper(vault, f"2024_Smith_Foo_{c}")
    with pytest.raises(AddError, match="exhausted"):
        auto_suffix_id(vault, "2024_Smith_Foo")


# ---------------------------------------------------------------------------
# find_paper_by_doi: the INDEX fast path must not change WHICH paper is found
# ---------------------------------------------------------------------------


def _reconcile(v: Path) -> None:
    from litman.core.correctors import reconcile_derived

    reconcile_derived(v, project_refs=False)


def test_find_by_doi_hits_through_index_without_scanning(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a fresh INDEX the lookup reads INDEX + the one hit's metadata —
    every other metadata.yaml stays shut (booby-trapped read_text proves it)."""
    _write_paper(vault, "2024_A_One", doi="10.1/one")
    _write_paper(vault, "2024_B_Two", doi="10.2/two")
    _write_paper(vault, "2024_C_Three", doi="10.3/three")
    _reconcile(vault)

    real_read = Path.read_text
    opened: list[str] = []

    def _spy(self: Path, *a: Any, **kw: Any) -> str:
        if self.name == "metadata.yaml":
            opened.append(self.parent.name)
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _spy)
    hit = find_paper_by_doi(vault, "10.2/two")
    assert hit is not None
    assert hit[0] == "2024_B_Two"
    assert hit[1]["doi"] == "10.2/two"
    assert opened == ["2024_B_Two"]


def test_find_by_doi_miss_on_fresh_index_is_conclusive(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh INDEX that carries no such DOI answers None without opening a
    single metadata.yaml (the `lit add` dedup check on a new paper)."""
    _write_paper(vault, "2024_A_One", doi="10.1/one")
    _reconcile(vault)

    real_read = Path.read_text

    def _spy(self: Path, *a: Any, **kw: Any) -> str:
        assert self.name != "metadata.yaml", "scanned on a conclusive miss"
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _spy)
    assert find_paper_by_doi(vault, "10.9/absent") is None


def test_find_by_doi_picks_the_same_paper_as_the_scan_on_a_duplicate(
    vault: Path,
) -> None:
    """Red line: in a vault that already holds a duplicate DOI (a health-check
    violation), the fast path must select the SAME paper the scan did —
    `lit rm --paper-doi` deletes what this returns."""
    _write_paper(vault, "2024_Z_Last", doi="10.5/dup")
    _write_paper(vault, "2024_A_First", doi="10.5/dup")
    _reconcile(vault)

    fast = find_paper_by_doi(vault, "10.5/dup")
    (vault / "INDEX.json").unlink()
    scan = find_paper_by_doi(vault, "10.5/dup")

    assert fast is not None and scan is not None
    assert fast[0] == scan[0] == "2024_A_First"  # id-ascending first match


def test_find_by_doi_falls_back_when_index_is_stale(vault: Path) -> None:
    """A paper added behind INDEX's back must still be found (probe fails →
    scan), or `lit add` would ingest a duplicate DOI."""
    _write_paper(vault, "2024_A_One", doi="10.1/one")
    _reconcile(vault)
    _write_paper(vault, "2024_B_Unindexed", doi="10.2/late")

    hit = find_paper_by_doi(vault, "10.2/late")
    assert hit is not None and hit[0] == "2024_B_Unindexed"


def test_find_by_doi_falls_back_when_the_hits_metadata_is_unreadable(
    vault: Path,
) -> None:
    """INDEX says this paper holds the DOI but its metadata.yaml is corrupt:
    the scan (which skips corrupt files) decides — here it finds the other
    paper that also carries the DOI, exactly as before the fast path."""
    _write_paper(vault, "2024_A_Corrupt", doi="10.5/dup")
    _write_paper(vault, "2024_B_Fine", doi="10.5/dup")
    _reconcile(vault)
    (vault / "papers" / "2024_A_Corrupt" / "metadata.yaml").write_bytes(
        b"\xff\xfe not utf-8 at all"
    )

    hit = find_paper_by_doi(vault, "10.5/dup")
    assert hit is not None and hit[0] == "2024_B_Fine"
