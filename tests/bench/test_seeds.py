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
from harness.seeds import (
    SEED_DIGEST_FILE,
    SEED_SPECS,
    assert_seed_intact,
    build_seed,
    ensure_seeds,
    litman_fingerprint,
    seed_digest,
)


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
        "seed-2papers-peptide-revisited",
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


# ---------------------------------------------------------------------------
# seed-2papers-peptide-revisited + the CLI property C2 rests on: a fact that
# ONLY `lit show` hands over, across every exit the CLI has.
# ---------------------------------------------------------------------------


def _lit_json(rv, *args):
    """Run a `lit` read through an isolated RunVault and parse its stdout."""
    import json

    res = rv.run(*args)
    assert res.exit_code == 0, f"lit {' '.join(args)} exited {res.exit_code}: {res.stderr}"
    return json.loads(res.stdout)


def _peptidebert_id(vault: Path) -> str:
    import json

    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    (pid,) = [p["id"] for p in index["papers"] if "PeptideBERT" in p["title"]]
    return pid


def test_revisited_seed_builds_clean_and_pins_the_read_state() -> None:
    """The seed's own contract: #4 finished on a FIXED past date and revisited on
    another. Fixed, never "today", so a re-run tomorrow scores identically."""
    from ruamel.yaml import YAML

    vault = build_seed("seed-2papers-peptide-revisited")
    assert len(_papers(vault)) == 2
    pid = _peptidebert_id(vault)
    meta = YAML(typ="safe").load(
        (vault / "papers" / pid / "metadata.yaml").read_text(encoding="utf-8")
    )
    assert str(meta["last-revisited"]) == "2026-06-15"
    # read-date is not decoration: last-revisited without it is a state the
    # product itself could never produce (`lit revisit` requires a read paper).
    assert str(meta["read-date"]) == "2026-05-01"
    assert meta["status"] == "deep-read"
    errs = _health_errors(vault)
    assert errs == [], [(i.category, i.message) for i in errs]


def test_the_revisited_seed_is_a_superset_of_the_plain_one() -> None:
    """It must inherit the #4<->#5 related edge, and the plain seed must NOT
    inherit the read state: A3/D1/F1/G1/C4/G2 all start from the plain one and a
    silently-read #4 would change what they measure."""
    revisited = SEED_SPECS["seed-2papers-peptide-revisited"].steps
    plain = SEED_SPECS["seed-2papers-peptide"].steps
    assert revisited[: len(plain)] == plain
    assert [s.op for s in revisited[len(plain):]] == ["modify"]
    assert not any(s.op == "modify" for s in plain)


def test_last_revisited_is_reachable_by_show_and_not_by_list() -> None:
    """The load-bearing fact under C2's `ran: show`, asserted against the real CLI.

    C2 asks when #4 was last revisited and scores `ran: show`. That is honest only
    while `lit list` cannot answer it — exactly what stopped being true for the
    card's original question (author/year) when ADR-022 put `authors` into the
    INDEX projection, after which three agents answered correctly via `lit list`
    and all scored 0. Pinned here against the same CLI the agent drives: a future
    projection change that adds `last-revisited` fails in /dev instead of silently
    scoring correct agents 0.
    """
    from harness.runlit import RunVault

    seed = build_seed("seed-2papers-peptide-revisited")
    with RunVault(seed) as rv:
        pid = _peptidebert_id(rv.vault)

        show = _lit_json(rv, "show", pid, "--format", "json")
        assert show["last-revisited"] == "2026-06-15"

        listed = _lit_json(rv, "list", "--format", "json")
        papers = listed["papers"] if isinstance(listed, dict) else listed
        (one,) = [p for p in papers if p["id"] == pid]
        assert "last-revisited" not in one, (
            "`lit list` now carries last-revisited: C2's `ran: show` has become a "
            "false negative exactly as the author/year question did — re-anchor "
            "the card, do not loosen the assertion"
        )
        # show is a strict superset here: the FULL metadata dict, not the 14-field
        # projection. Pinning the relationship, not just the one key.
        assert set(one) < set(show)


def test_no_other_command_hands_over_the_last_revisited_date() -> None:
    """`lit show` must be the ONLY exit — the guard the card was bitten by twice.

    Checking `lit list` alone is not enough, and that gap is not hypothetical: it
    is how the author/year question died (nobody re-checked the lookup surface
    after ADR-022), and it is why the interim `arxiv-id` anchor was unsound —
    `lit cite` and `lit export` both emit an arXiv id, so an agent answering
    correctly through either would have failed `ran: show`. `last-revisited` is
    the one candidate with no such second exit (measured); this pins that, so a
    future change that starts emitting it anywhere trips a test instead of quietly
    rotting the card.
    """
    from harness.runlit import RunVault

    seed = build_seed("seed-2papers-peptide-revisited")
    with RunVault(seed) as rv:
        pid = _peptidebert_id(rv.vault)
        for argv in (
            ["list"],
            ["list", "--title", "PeptideBERT", "--format", "json"],
            ["search", "PeptideBERT"],
            ["search", "2026-06-15"],
            ["related", pid],
            ["cite", pid],
        ):
            res = rv.run(*argv)
            assert "2026-06-15" not in (res.stdout + res.stderr), (
                f"`lit {' '.join(argv)}` now hands the agent the revisit date — "
                f"C2's `ran: show` would fail agents that answer correctly "
                f"through it, exactly as ADR-022 did to the author/year question"
            )
        # `lit export` writes a file rather than printing; check its output too.
        res = rv.run("export", "--all", cwd=rv.run_dir)
        assert res.exit_code == 0, res.stderr
        bib = (rv.run_dir / "refs.bib").read_text(encoding="utf-8")
        assert "2026-06-15" not in bib, (
            "`lit export` now carries last-revisited into the .bib — same false "
            "negative as above, via a file instead of stdout"
        )


def test_the_arxiv_id_by_contrast_leaks_through_cite_and_export() -> None:
    """The measurement that disqualified the `arxiv-id` anchor, kept as a test.

    Not a wart being enshrined: it is the evidence for why C2 asks what it asks.
    `arxiv-id` looks like an equally good show-only field — it is genuinely absent
    from the INDEX projection — but `lit cite` and `lit export` both emit it, so
    `ran: show` would punish an agent that answered correctly through either. If a
    future litman stops emitting it there, this test fails and whoever sees it can
    reconsider the anchor with the notes in hand, rather than rediscovering the
    exit surface from scratch a third time.
    """
    from harness.runlit import RunVault

    seed = build_seed("seed-2papers-peptide-revisited")
    with RunVault(seed) as rv:
        pid = _peptidebert_id(rv.vault)
        # Absent from the projection — the property that made it tempting.
        listed = _lit_json(rv, "list", "--format", "json")
        papers = listed["papers"] if isinstance(listed, dict) else listed
        (one,) = [p for p in papers if p["id"] == pid]
        assert "arxiv-id" not in one

        # ...but reachable without `show`, which is what rules it out.
        cite = rv.run("cite", pid)
        assert "2309.03099" in (cite.stdout + cite.stderr)
        res = rv.run("export", "--all", cwd=rv.run_dir)
        assert res.exit_code == 0, res.stderr
        assert "2309.03099" in (rv.run_dir / "refs.bib").read_text(encoding="utf-8")


def test_the_revisit_date_survives_a_plain_show_too() -> None:
    """`lit show <id>` with no --format must also carry the date: an agent reading
    the table (the documented normal path) has to be able to retrieve it, or the
    card would be scoring flag-guessing rather than tool choice."""
    from harness.runlit import RunVault

    seed = build_seed("seed-2papers-peptide-revisited")
    with RunVault(seed) as rv:
        pid = _peptidebert_id(rv.vault)
        res = rv.run("show", pid)
        assert res.exit_code == 0
        assert "2026-06-15" in res.stdout


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
    # Detect an unwanted rebuild by watching `.seed-key`'s mtime (a rebuild
    # rewrites it) rather than by dropping a marker file in the seed root: the
    # cache hit now verifies a CONTENT digest of that root, so a marker would
    # itself read as a mutated seed and force the very rebuild it exists to
    # detect. Nanosecond resolution — a rebuild takes seconds, so no collision.
    stamped_at = key_file.stat().st_mtime_ns
    v2 = build_seed("seed-1paper-diffdock")
    assert v2 == v1
    assert key_file.stat().st_mtime_ns == stamped_at, "cache hit should not rebuild"


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


# ---------------------------------------------------------------------------
# Seed integrity: the .seed-digest stamp, the canary, the self-healing cache
# ---------------------------------------------------------------------------


def test_build_stamps_a_digest_that_matches_the_seed_it_describes() -> None:
    """The stamp build_seed writes reproduces on a re-read of the same seed.

    Pins the ordering trap: `.seed-key` is INSIDE the digest scope, so stamping
    before writing the key would bake in a digest nothing can reproduce; and
    `.seed-digest` must be OUT of scope, since no file can contain the hash of a
    tree containing itself.
    """
    vault = build_seed("seed-empty")
    seed_root = vault.parent
    stamp = seed_root / SEED_DIGEST_FILE
    assert stamp.is_file(), "build_seed must stamp the seed it just built"
    assert stamp.read_text(encoding="utf-8").strip() == seed_digest(seed_root)
    # A stamped seed is by definition intact.
    assert_seed_intact(seed_root)


def test_digest_excludes_only_its_own_stamp() -> None:
    """.seed-digest is out of scope; .seed-key is IN it (it must not move either)."""
    vault = build_seed("seed-empty")
    seed_root = vault.parent
    before = seed_digest(seed_root)

    # Rewriting the stamp itself cannot change the digest...
    (seed_root / SEED_DIGEST_FILE).write_text("0" * 64 + "\n", encoding="utf-8")
    assert seed_digest(seed_root) == before
    # ...but the key is in scope.
    key = seed_root / ".seed-key"
    key.write_text(key.read_text(encoding="utf-8") + "x", encoding="utf-8")
    assert seed_digest(seed_root) != before
    build_seed("seed-empty", force=True)  # restore for later tests


def test_digest_reads_symlinks_instead_of_following_them(tmp_path: Path) -> None:
    """Retargeting a symlink must move the digest.

    The whole poisoning mode is deleting/retargeting bridge symlinks. A digest
    that FOLLOWED links would hash the target's bytes and read a retargeted link
    whose new target happens to have identical content as "nothing changed".
    """
    root = tmp_path / "seed"
    (root / "papers").mkdir(parents=True)
    (root / "papers" / "a").write_text("same", encoding="utf-8")
    (root / "papers" / "b").write_text("same", encoding="utf-8")
    link = root / "bridge"
    link.symlink_to("papers/a")
    before = seed_digest(root)

    link.unlink()
    link.symlink_to("papers/b")  # same target CONTENT, different target PATH
    assert seed_digest(root) != before, "digest must track the link, not its target"


def test_digest_notices_an_equal_length_edit(tmp_path: Path) -> None:
    """Content hashing, not size: an equal-length edit is a real poisoning."""
    root = tmp_path / "seed"
    root.mkdir()
    f = root / "metadata.yaml"
    f.write_text("read-date: 2026-05-01\n", encoding="utf-8")
    before = seed_digest(root)
    f.write_text("read-date: 2026-06-15\n", encoding="utf-8")  # same byte count
    assert seed_digest(root) != before


def test_digest_separates_fields_so_paths_cannot_collide(tmp_path: Path) -> None:
    """("a","12") vs ("a1","2") — no separator means identical hashes."""
    one = tmp_path / "one"
    (one / "a").mkdir(parents=True)
    (one / "a" / "12").write_text("", encoding="utf-8")
    two = tmp_path / "two"
    (two / "a1").mkdir(parents=True)
    (two / "a1" / "2").write_text("", encoding="utf-8")
    assert seed_digest(one) != seed_digest(two)


def test_assert_seed_intact_is_a_noop_without_a_stamp(tmp_path: Path) -> None:
    """An unstamped root is a hand-made test seed — never alarm on it.

    Load-bearing: test_batch.py's `_fake_seed` returns `tmp_path/"seed"` while its
    callers pass `work_root=tmp_path/"work"`, so the run dirs live INSIDE the root
    a digest would cover and every round would look like a leak.
    """
    root = tmp_path / "fake-seed"
    root.mkdir()
    (root / "lit-config.yaml").write_text("schema: 1\n", encoding="utf-8")
    assert_seed_intact(root)  # no stamp -> silent
    (root / "whatever").write_text("mutated freely", encoding="utf-8")
    assert_seed_intact(root)  # still silent


def test_assert_seed_intact_raises_on_a_mutated_stamped_seed(tmp_path: Path) -> None:
    root = tmp_path / "seed"
    root.mkdir()
    (root / "paper.txt").write_text("original", encoding="utf-8")
    seeds._stamp_seed(root)
    assert_seed_intact(root)

    (root / "paper.txt").write_text("poisoned", encoding="utf-8")
    with pytest.raises(seeds.SeedLeakError, match="MUTATED"):
        assert_seed_intact(root)


@pytest.mark.slow
def test_a_poisoned_cache_heals_itself_on_the_next_build() -> None:
    """AC5: the exact damage `lit unlink` did to the shared seed, undone.

    Before the stamp, a poisoned seed kept its matching `.seed-key` (litman had
    not changed), so the cache-hit test waved it through on every future run on
    the node — the poison outlived the run that caused it, and nothing short of a
    human remembering to `rm -rf /tmp/litman-bench-seeds` recovered it.
    """
    vault = build_seed("seed-5papers-tagged")
    seed_root = vault.parent
    bridge = seed_root / "projects" / "PepCodec" / "litman_reflib"
    links = [p for p in bridge.iterdir() if p.is_symlink()]
    assert links, "seed precondition: PepCodec has a bridge symlink to unlink"

    victim = links[0]
    victim.unlink()  # exactly what an escaped `lit unlink` did
    assert not victim.exists()
    with pytest.raises(seeds.SeedLeakError):
        assert_seed_intact(seed_root)

    rebuilt = build_seed("seed-5papers-tagged")
    assert rebuilt == vault
    assert victim.is_symlink(), "poisoned cache must be rebuilt, not reused"
    assert_seed_intact(seed_root)  # and re-stamped


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
