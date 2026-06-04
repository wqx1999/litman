"""Deterministic tests for the seed builder (Phase B-seed).

Builds the small seeds for real (fast: empty / 1-paper / 2-paper) and asserts
their content + idempotency + the litman-fingerprint cache-invalidation. The
5-paper seed is exercised in one slower test. Everything runs offline (no agent,
no network) using the committed fixture PDFs + golden JSON.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import seeds
from harness.seeds import SEED_SPECS, build_seed, ensure_seeds, litman_fingerprint


def _papers(vault: Path) -> list[str]:
    return sorted(p.name for p in (vault / "papers").iterdir() if p.is_dir())


def _health_errors(vault: Path) -> list:
    from litman.core.checks import run_all_checks
    from litman.core.document import list_papers

    return [
        i
        for i in run_all_checks(vault, list_papers(vault))
        if i.severity == "error"
    ]


def test_seed_specs_defined() -> None:
    assert set(SEED_SPECS) == {
        "seed-empty",
        "seed-1paper-diffdock",
        "seed-1paper-diffdock-read",
        "seed-2papers-peptide",
        "seed-5papers-tagged",
    }
    # The model is scale-agnostic: each spec is an ordered tuple of steps,
    # the first of which is always `init`.
    for spec in SEED_SPECS.values():
        assert spec.steps[0].op == "init"


def test_fixtures_present() -> None:
    for fid in (1, 2, 4, 5, 9):
        assert (seeds.PDFS_DIR / f"{fid}.pdf").is_file(), f"fixture {fid}.pdf missing"
        assert (seeds.GOLDEN_DIR / f"{fid}.json").is_file(), f"golden {fid}.json missing"


def test_build_empty_seed() -> None:
    vault = build_seed("seed-empty")
    assert (vault / "lit-config.yaml").is_file()
    assert (vault / "papers").is_dir()
    assert (vault / "INDEX.json").is_file()
    assert _papers(vault) == []
    assert _health_errors(vault) == []


def test_build_1paper_seed() -> None:
    vault = build_seed("seed-1paper-diffdock")
    papers = _papers(vault)
    assert len(papers) == 1
    assert any("DiffDock" in p for p in papers)
    assert _health_errors(vault) == []


def test_1paper_seed_injects_focal_loss_note() -> None:
    """C3 precondition: the `notes` step writes 'focal loss' into #1's notes.md so
    `lit search "focal loss"` has something to find (else C3 is a guaranteed-0
    false negative). Notes stay health-clean (notes content is not in INDEX)."""
    vault = build_seed("seed-1paper-diffdock")
    (diffdock,) = [p for p in (vault / "papers").iterdir() if "DiffDock" in p.name]
    note = (diffdock / "notes.md").read_text(encoding="utf-8")
    assert "focal loss" in note.lower()
    assert _health_errors(vault) == []


def test_build_2paper_seed_clean() -> None:
    vault = build_seed("seed-2papers-peptide")
    papers = _papers(vault)
    assert len(papers) == 2
    assert any("PeptideBERT" in p for p in papers)
    assert any("Multi-Peptide" in p for p in papers)
    errs = _health_errors(vault)
    assert errs == [], [(i.category, i.message) for i in errs]


def _related_of(paper_dir: Path) -> list[str]:
    from ruamel.yaml import YAML

    meta = YAML(typ="safe").load((paper_dir / "metadata.yaml").read_text(encoding="utf-8"))
    return list(meta.get("related") or [])


def test_2paper_seed_relates_4_and_5() -> None:
    """C4/G2 precondition: the `relate` step asserts a symmetric #4↔#5 `related`
    edge so `lit related <#4>` surfaces #5 — author overlap alone does NOT drive
    `lit related` (it walks explicit edges + shared topics). The CLI double-writes
    the reverse side, so both metadata files carry the edge; health stays clean."""
    vault = build_seed("seed-2papers-peptide")
    papers = {p.name: p for p in (vault / "papers").iterdir() if p.is_dir()}
    (pid4,) = [n for n in papers if "PeptideBERT" in n]
    (pid5,) = [n for n in papers if "Multi-Peptide" in n]
    assert pid5 in _related_of(papers[pid4]), "#4.related must list #5"
    assert pid4 in _related_of(papers[pid5]), "#5.related (reverse double-write) must list #4"
    assert _health_errors(vault) == []


def test_seed_pdf_is_real_fixture() -> None:
    """The added paper.pdf is byte-identical to the committed fixture (#4)."""
    vault = build_seed("seed-2papers-peptide")
    fixture4 = (seeds.PDFS_DIR / "4.pdf").read_bytes()
    pdfs = [
        (p / "paper.pdf").read_bytes()
        for p in (vault / "papers").iterdir()
        if "PeptideBERT" in p.name
    ]
    assert pdfs and pdfs[0] == fixture4


def test_build_is_idempotent_cache_hit() -> None:
    """A second build with the same fingerprint reuses the cached vault."""
    v1 = build_seed("seed-1paper-diffdock")
    key_file = v1.parent / ".seed-key"
    assert key_file.read_text(encoding="utf-8").strip() == litman_fingerprint()
    # Mark the cache so we can detect an unwanted rebuild.
    marker = v1.parent / "_cache_marker"
    marker.write_text("kept", encoding="utf-8")
    v2 = build_seed("seed-1paper-diffdock")
    assert v2 == v1
    assert marker.exists(), "cache hit should not rebuild (marker was wiped)"


def test_force_rebuild_wipes_cache_marker() -> None:
    v1 = build_seed("seed-1paper-diffdock")
    marker = v1.parent / "_force_marker"
    marker.write_text("x", encoding="utf-8")
    build_seed("seed-1paper-diffdock", force=True)
    assert not marker.exists(), "force=True should rebuild from a clean slate"


def test_stale_key_forces_rebuild() -> None:
    """A mismatched .seed-key (simulating litman code change) triggers rebuild."""
    v1 = build_seed("seed-empty")
    key_file = v1.parent / ".seed-key"
    key_file.write_text("deadbeefstale\n", encoding="utf-8")
    marker = v1.parent / "_stale_marker"
    marker.write_text("x", encoding="utf-8")
    v2 = build_seed("seed-empty")
    assert v2 == v1
    assert not marker.exists(), "stale key should have forced a rebuild"
    assert key_file.read_text(encoding="utf-8").strip() == litman_fingerprint()


def test_ensure_seeds_returns_paths() -> None:
    out = ensure_seeds(["seed-empty", "seed-1paper-diffdock"])
    assert set(out) == {"seed-empty", "seed-1paper-diffdock"}
    for name, path in out.items():
        assert path.is_dir()
        assert (path / "lit-config.yaml").is_file()


def test_unknown_seed_raises() -> None:
    with pytest.raises(KeyError, match="unknown seed"):
        build_seed("seed-does-not-exist")


@pytest.mark.slow
def test_build_5paper_tagged_seed() -> None:
    vault = build_seed("seed-5papers-tagged")
    papers = _papers(vault)
    assert len(papers) == 5
    errs = _health_errors(vault)
    assert errs == [], [(i.category, i.message) for i in errs]
    # Tags + project landed.
    from litman.core.taxonomy import parse_taxonomy

    tax = parse_taxonomy((vault / "TAXONOMY.md").read_text(encoding="utf-8"))
    assert "diffusion" in tax["topics"]
    assert "peptide" in tax["topics"]
    assert "PepCodec" in tax["projects"]
