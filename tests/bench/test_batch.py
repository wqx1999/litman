"""Deterministic tests for the batch runner + aggregation (Phase G plumbing).

NEVER spawns a live agent (M34 §3.5 hard boundary). ``run_card_fn`` is injected
as a fake that returns a pre-computed score or a scoreable handle; the real
aggregation / coverage / TRR math is what is under test.
"""

from __future__ import annotations

from pathlib import Path

from harness.batch import (
    BenchReport,
    CardScore,
    build_live_routing_run_fn,
    build_live_run_card_fn,
    coverage_tag,
    report_to_dict,
    run_batch,
)
from harness.executor import ExecutorResult, LitCall, ToolResult
from harness.scenarios import load_all_cards


# ---------------------------------------------------------------------------
# coverage_tag classification
# ---------------------------------------------------------------------------


def test_coverage_tag_skipped() -> None:
    assert coverage_tag({"id": "E1", "skip_reason": "needs_network"}) == "skipped"


def test_coverage_tag_multiturn() -> None:
    # Methodology exclusion: runs in the sandbox but unfair to score single-turn.
    assert (
        coverage_tag({"id": "G3", "layer": "maintenance", "single_turn_unfit": "multi-turn — ..."})
        == "multi-turn"
    )


def test_coverage_tag_skipped_precedes_multiturn() -> None:
    # A physical skip wins over a methodology exclusion if both are (wrongly) set.
    assert (
        coverage_tag({"id": "X", "skip_reason": "needs_pty", "single_turn_unfit": "y"})
        == "skipped"
    )


def test_coverage_tag_routing() -> None:
    assert coverage_tag({"id": "I", "layer": "routing", "cases": []}) == "routing"


def test_coverage_tag_auto_scored() -> None:
    card = {
        "id": "A1",
        "layer": "front-door",
        "expected_end_state": ["path_exists: papers", "health: clean"],
    }
    assert coverage_tag(card) == "auto-scored"


def test_coverage_tag_prose_blocked() -> None:
    card = {
        "id": "B2",
        "layer": "curation",
        "expected_end_state": ["ran: revisit", "笔记内容质量足够好"],
    }
    assert coverage_tag(card) == "prose-blocked"


# ---------------------------------------------------------------------------
# run_batch aggregation with injected fakes (no live agent)
# ---------------------------------------------------------------------------


def test_run_batch_mean_and_trr() -> None:
    """Per-card mean(resolved) + TRR over auto-scored cards only."""
    # Card X resolves 2/3, card Y resolves 3/3; both auto-scored.
    scores = {
        ("X", 0): 1, ("X", 1): 0, ("X", 2): 1,
        ("Y", 0): 1, ("Y", 1): 1, ("Y", 2): 1,
    }

    def fake_run(card, *, round, model, **_):
        return scores[(card["id"], round)]

    cards = [
        {"id": "X", "layer": "f", "expected_end_state": ["path_exists: papers"]},
        {"id": "Y", "layer": "f", "expected_end_state": ["path_exists: papers"]},
    ]
    report = run_batch(cards, model="m", rounds=3, run_card_fn=fake_run)
    assert isinstance(report, BenchReport)
    by_id = {c.card_id: c for c in report.cards}
    assert by_id["X"].mean == 2 / 3
    assert by_id["Y"].mean == 1.0
    # TRR = mean of the two card means.
    assert report.trr_mean == (2 / 3 + 1.0) / 2
    assert report.trr_std > 0  # the two card means differ


def test_run_batch_excludes_routing_and_skipped_from_trr() -> None:
    """Routing + skipped + prose-blocked cards never fold into TRR."""

    def fake_run(card, *, round, model, **_):
        return 1  # any auto-scored card resolves perfectly

    cards = [
        {"id": "AUTO", "layer": "f", "expected_end_state": ["path_exists: papers"]},
        {"id": "PROSE", "layer": "f", "expected_end_state": ["自由 prose 行"]},
        {"id": "ROUTE", "layer": "routing", "cases": [{"utt": "x", "golden": "lit-library"}]},
        {"id": "SKIP", "layer": "f", "skip_reason": "needs_network", "expected_end_state": []},
    ]
    report = run_batch(cards, model="m", rounds=2, run_card_fn=fake_run)
    counts = report.coverage["counts"]
    assert counts == {
        "auto-scored": 1,
        "prose-blocked": 1,
        "routing": 1,
        "skipped": 1,
        "multi-turn": 0,
    }
    # TRR denominator is only the single auto-scored card.
    assert report.coverage["trr_denominator"] == 1
    assert report.trr_mean == 1.0
    assert report.trr_std == 0.0  # single sample, no spread
    # Routing / skipped recorded with empty rounds (no agent invoked for them).
    by_id = {c.card_id: c for c in report.cards}
    assert by_id["ROUTE"].rounds == []
    assert by_id["SKIP"].rounds == []
    # The prose-blocked card was still RUN (it could be partially scored) but is
    # excluded from TRR.
    assert by_id["PROSE"].tag == "prose-blocked"
    assert len(by_id["PROSE"].rounds) == 2


def test_run_batch_handle_routes_through_score_fn() -> None:
    """A dict handle from run_card_fn is folded by the injected score_fn."""
    seen: list[str] = []

    def fake_run(card, *, round, model, **_):
        return {"vault": "/tmp/v", "jsonl": [], "tag": card["id"]}

    def fake_score(card, *, vault, jsonl, tag, **_):
        seen.append(tag)
        return (1, [])

    cards = [{"id": "Z", "layer": "f", "expected_end_state": ["path_exists: papers"]}]
    report = run_batch(
        cards, model="m", rounds=1, run_card_fn=fake_run, score_fn=fake_score
    )
    assert seen == ["Z"]
    assert report.cards[0].mean == 1.0


def test_run_batch_pre_scored_tuple_handle() -> None:
    """run_card_fn may return a (resolved, trail) tuple already scored."""

    def fake_run(card, *, round, model, **_):
        return (0, [])

    cards = [{"id": "W", "layer": "f", "expected_end_state": ["path_exists: papers"]}]
    report = run_batch(cards, model="m", rounds=2, run_card_fn=fake_run)
    assert report.cards[0].mean == 0.0


# ---------------------------------------------------------------------------
# report serialization
# ---------------------------------------------------------------------------


def test_report_to_dict_round_trips() -> None:
    report = BenchReport(
        agent="claude",
        model_requested="m",
        rounds=2,
        trr_mean=0.5,
        trr_std=0.1,
        cards=[CardScore("X", "auto-scored", [1, 0], 0.5)],
        coverage={"counts": {"auto-scored": 1}},
    )
    import json

    payload = report_to_dict(report)
    text = json.dumps(payload)  # must be JSON-serializable
    assert json.loads(text)["trr_mean"] == 0.5
    assert payload["cards"][0]["card_id"] == "X"


# ---------------------------------------------------------------------------
# against the real corpus (every card classifies + a full dry batch runs)
# ---------------------------------------------------------------------------


def test_real_corpus_every_card_classifies() -> None:
    """Every loaded card gets a known coverage tag (no card falls through)."""
    cards = load_all_cards()
    valid = {"auto-scored", "prose-blocked", "routing", "skipped", "multi-turn"}
    for c in cards:
        assert coverage_tag(c) in valid, c.id


def test_real_corpus_dry_batch_runs() -> None:
    """A full dry batch over the real corpus runs with a fake (no live agent),
    and the newly-converted cards are tagged auto-scored."""

    def fake_run(card, *, round, model, **_):
        return 1

    cards = load_all_cards()
    report = run_batch(cards, model="claude-sonnet-4-6", rounds=1, run_card_fn=fake_run)
    auto = set(report.coverage["auto_scored_cards"])
    # The conservative-7 conversion should land these as auto-scored.
    for cid in ["A3-add-dup", "C2-show", "C3-search-notes", "C4-related-samegroup", "F1-export-bib"]:
        assert cid in auto, f"{cid} not auto-scored; tags={[(c.card_id, c.tag) for c in report.cards]}"
    # Routing + skipped cards present in coverage.
    assert report.coverage["routing_cards"]
    assert report.coverage["skipped_cards"]


# ---------------------------------------------------------------------------
# build_live_run_card_fn (FIX 1) — injected fakes, NEVER a live agent
# ---------------------------------------------------------------------------


def _fake_seed(tmp_path: Path):
    """A fake ``ensure_seed_impl`` returning a real (copyable) seed dir.

    ``RunVault`` copytree's the seed, so it must exist on disk; it carries a
    ``lit-config.yaml`` marker so the result looks vault-shaped.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "lit-config.yaml").write_text("schema: 1\n", encoding="utf-8")
    (seed / "papers").mkdir()

    def ensure_seed_impl(name: str) -> Path:
        ensure_seed_impl.seen = name  # type: ignore[attr-defined]
        return seed

    return ensure_seed_impl


def test_build_live_run_card_fn_passes_model_and_auth_through(tmp_path: Path) -> None:
    """The adapter calls run_card_impl with run_vault + model/base_url/auth_token,
    and returns a {vault,jsonl,cwd,run,_cleanup} handle (no live agent)."""
    captured: dict = {}

    def fake_run_card(card, run_vault, *, fixtures_pdfs_dir, agent, model, base_url, auth_token):
        captured["card"] = card
        captured["run_vault"] = Path(run_vault)
        captured["fixtures_pdfs_dir"] = Path(fixtures_pdfs_dir)
        captured["model"] = model
        captured["base_url"] = base_url
        captured["auth_token"] = auth_token
        return ExecutorResult(
            lit_calls=[LitCall(argv=["list"], raw="lit list", tool_use_id="b1")],
            tool_results=[ToolResult(tool="Bash", content="#4 PeptideBERT", tool_use_id="b1")],
            final_text="done",
        )

    ensure_seed = _fake_seed(tmp_path)
    work_root = tmp_path / "work"
    work_root.mkdir()

    run_fn = build_live_run_card_fn(
        fixtures_pdfs_dir=tmp_path / "pdfs",
        seeds_dir=tmp_path / "seeds",
        work_root=work_root,
        base_url="https://proxy.example/v1",
        auth_token="tok-123",
        run_card_impl=fake_run_card,
        ensure_seed_impl=ensure_seed,
    )

    card = {"id": "A2", "seed": "seed-1paper-diffdock"}
    handle = run_fn(card, round=0, model="some-model")

    # (a) run_card_impl got the right kwargs, model + auth passed straight through.
    assert captured["model"] == "some-model"
    assert captured["base_url"] == "https://proxy.example/v1"
    assert captured["auth_token"] == "tok-123"
    assert captured["fixtures_pdfs_dir"] == tmp_path / "pdfs"
    assert ensure_seed.seen == "seed-1paper-diffdock"  # type: ignore[attr-defined]

    # (b) the handle shape the scorer consumes.
    assert set(handle) == {"vault", "jsonl", "cwd", "run", "_cleanup"}
    assert handle["vault"] == captured["run_vault"]
    assert handle["vault"].is_dir()  # the cp landed
    assert handle["cwd"] == handle["vault"].parent / "cwd"  # neutral_cwd convention
    assert handle["jsonl"][0]["stdout"] == "#4 PeptideBERT"  # ExecutorResult folded
    assert handle["run"].final_text == "done"


def test_build_live_run_card_fn_cleanup_removes_run_dir(tmp_path: Path) -> None:
    """The handle's _cleanup removes the whole disposable run dir."""

    def fake_run_card(card, run_vault, *, fixtures_pdfs_dir, agent, model, base_url, auth_token):
        return ExecutorResult()

    run_fn = build_live_run_card_fn(
        fixtures_pdfs_dir=tmp_path / "pdfs",
        seeds_dir=tmp_path / "seeds",
        work_root=tmp_path / "work",
        run_card_impl=fake_run_card,
        ensure_seed_impl=_fake_seed(tmp_path),
    )
    (tmp_path / "work").mkdir(exist_ok=True)
    handle = run_fn({"id": "A2", "seed": "s"}, round=0, model="m")
    run_dir = handle["vault"].parent
    assert run_dir.is_dir()
    handle["_cleanup"]()
    assert not run_dir.exists()


def test_build_live_run_card_fn_defaults_anthropic_auth(tmp_path: Path) -> None:
    """Default (no base_url/auth_token) passes None through — Anthropic mode."""
    seen: dict = {}

    def fake_run_card(card, run_vault, *, fixtures_pdfs_dir, agent, model, base_url, auth_token):
        seen["base_url"] = base_url
        seen["auth_token"] = auth_token
        return ExecutorResult()

    run_fn = build_live_run_card_fn(
        fixtures_pdfs_dir=tmp_path / "pdfs",
        seeds_dir=tmp_path / "seeds",
        work_root=tmp_path / "work",
        run_card_impl=fake_run_card,
        ensure_seed_impl=_fake_seed(tmp_path),
    )
    (tmp_path / "work").mkdir(exist_ok=True)
    run_fn({"id": "A2", "seed": "s"}, round=0, model="m")
    assert seen == {"base_url": None, "auth_token": None}


def test_build_live_run_card_fn_missing_seed_errors(tmp_path: Path) -> None:
    """A card with no seed is a real error (routing/skipped never reach here)."""

    def fake_run_card(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("run_card_impl should not run when seed is missing")

    run_fn = build_live_run_card_fn(
        fixtures_pdfs_dir=tmp_path / "pdfs",
        seeds_dir=tmp_path / "seeds",
        work_root=tmp_path / "work",
        run_card_impl=fake_run_card,
        ensure_seed_impl=_fake_seed(tmp_path),
    )
    import pytest

    with pytest.raises(ValueError, match="no seed"):
        run_fn({"id": "X"}, round=0, model="m")


# ---------------------------------------------------------------------------
# run_batch lifecycle: _cleanup after scoring + _score_one strips reserved keys
# ---------------------------------------------------------------------------


def test_run_batch_calls_cleanup_after_scoring() -> None:
    """run_batch invokes the handle's _cleanup after _score_one (try/finally)."""
    order: list[str] = []

    def fake_run(card, *, round, model, **_):
        def cleanup() -> None:
            order.append("cleanup")

        return {"vault": "/tmp/v", "jsonl": [], "_cleanup": cleanup}

    def fake_score(card, *, vault, jsonl, **_):
        order.append("score")
        return (1, [])

    cards = [{"id": "Z", "layer": "f", "expected_end_state": ["path_exists: papers"]}]
    run_batch(cards, model="m", rounds=1, run_card_fn=fake_run, score_fn=fake_score)
    # Scoring happens first, cleanup strictly after.
    assert order == ["score", "cleanup"]


def test_run_batch_transcript_dir_none_writes_nothing(tmp_path: Path) -> None:
    """DEFAULT path: transcript_dir unset → no artifact dir, scoring unchanged.

    Guards the red line that the opt-in debug dump is byte-identical to the
    pre-existing behavior when not requested.
    """
    out = tmp_path / "transcripts"

    def fake_run(card, *, round, model, **_):
        return {"vault": "/tmp/v", "jsonl": [{"argv": ["list"]}], "_cleanup": lambda: None}

    def fake_score(card, *, vault, jsonl, **_):
        return (1, [])

    cards = [{"id": "Z", "layer": "f", "expected_end_state": ["path_exists: papers"]}]
    run_batch(cards, model="m", rounds=2, run_card_fn=fake_run, score_fn=fake_score)
    assert not out.exists()  # nothing written when transcript_dir is None


def test_run_batch_keep_transcript_dumps_per_round(tmp_path: Path) -> None:
    """transcript_dir set → one <id>-r<n>.json per round with commands + final
    answer + resolved + the per-assertion trail; cleanup still fires afterwards."""
    import json

    from harness.checker import AssertResult

    out = tmp_path / "transcripts"
    cleaned: list[bool] = []

    def fake_run(card, *, round, model, **_):
        return {
            "vault": "/tmp/v",
            "jsonl": [{"argv": ["show", "x"], "stdout": "2023 Guntuboina"}],
            "run": ExecutorResult(final_text=f"answer r{round}"),
            "_cleanup": lambda: cleaned.append(True),
        }

    def fake_score(card, *, vault, jsonl, **_):
        # A failing assertion, exactly the root-cause signal we want captured.
        return (0, [AssertResult("answer_contains", "~Guntuboina", False, "not in final_text")])

    cards = [{"id": "C2-show", "layer": "f", "expected_end_state": ["answer_contains: ~Guntuboina"]}]
    run_batch(
        cards,
        model="m",
        rounds=2,
        run_card_fn=fake_run,
        score_fn=fake_score,
        transcript_dir=out,
    )

    f0, f1 = out / "C2-show-r0.json", out / "C2-show-r1.json"
    assert f0.is_file() and f1.is_file()
    p0 = json.loads(f0.read_text(encoding="utf-8"))
    assert p0["card_id"] == "C2-show"
    assert p0["round"] == 0
    assert p0["resolved"] == 0
    assert p0["final_text"] == "answer r0"
    assert p0["jsonl"][0]["stdout"] == "2023 Guntuboina"
    # the trail (AssertResult dataclass) is serialized field-by-field.
    assert p0["trail"][0]["verb"] == "answer_contains"
    assert p0["trail"][0]["passed"] is False
    assert "not in final_text" in p0["trail"][0]["detail"]
    # the vault was still cleaned up (transcript keeps only the small artifact).
    assert cleaned == [True, True]


def test_run_batch_keep_transcript_unwritable_dir_raises_before_spend(tmp_path: Path) -> None:
    """An unwritable --keep-transcript dir aborts UP FRONT (before any run_card_fn
    call), so a bad path never wastes a live run with silently-dropped artifacts.

    REGRESSION: an unexpanded ``$BENCH/...`` (→ ``/results/...``) used to be
    swallowed by _dump_transcript's bare except, so 24 rounds of live spend
    produced zero transcripts. The up-front probe must turn that into a loud,
    token-free failure (no-silent-skip, invariant #14).
    """
    import pytest

    spawned: list[str] = []

    def fake_run(card, *, round, model, **_):  # pragma: no cover - must not run
        spawned.append(card["id"])
        return 1

    # A path UNDER a regular file can never be mkdir'd → OSError on the probe.
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    bad_dir = blocker / "transcripts"

    cards = [{"id": "Z", "layer": "f", "expected_end_state": ["path_exists: papers"]}]
    with pytest.raises(ValueError, match="not writable"):
        run_batch(cards, model="m", rounds=3, run_card_fn=fake_run, transcript_dir=bad_dir)
    assert spawned == []  # aborted before spending a single round


def test_run_batch_strips_reserved_keys_before_score_fn() -> None:
    """_score_one must not spread _-prefixed keys (e.g. _cleanup) into score_fn."""
    seen_kwargs: dict = {}

    def fake_run(card, *, round, model, **_):
        return {
            "vault": "/tmp/v",
            "jsonl": [],
            "cwd": "/tmp/v/cwd",
            "_cleanup": lambda: None,
            "_internal": 7,
        }

    def fake_score(card, *, vault, jsonl, cwd, **extra):
        seen_kwargs.update(extra)  # any leftover _-keys would land here
        return (1, [])

    cards = [{"id": "Z", "layer": "f", "expected_end_state": ["path_exists: papers"]}]
    run_batch(cards, model="m", rounds=1, run_card_fn=fake_run, score_fn=fake_score)
    assert seen_kwargs == {}  # no _cleanup / _internal leaked through


def test_run_batch_requires_explicit_run_card_fn() -> None:
    """run_card_fn=None is a misuse now (live adapter built explicitly by caller)."""
    import pytest

    cards = [{"id": "Z", "layer": "f", "expected_end_state": ["path_exists: papers"]}]
    with pytest.raises(ValueError, match="run_card_fn"):
        run_batch(cards, model="m", rounds=1, run_card_fn=None)


# ---------------------------------------------------------------------------
# FIX A — run_batch drives the REAL resolve (golden_dir threaded), no live agent
# ---------------------------------------------------------------------------
#
# The coverage blind spot: EVERY test above returns an int / tuple / fake
# score_fn, so resolve() is never actually called by run_batch. The live path
# defaults score_fn=resolve, whose ``golden_dir`` is a REQUIRED kwarg the live
# handle does not carry — so a real run raises TypeError on the first scored
# card. These two tests close that gap by driving the DEFAULT score_fn (resolve)
# over a real (clean) seed vault, scoring a load-bearing ``pdf_eq`` line that
# dereferences ``golden_dir.parent/"pdfs"``. NEVER spawns: the run handle is a
# canned ExecutorResult pointing at the prebuilt seed.


def _peptidebert_auto_card() -> dict:
    """An auto-scored card whose end-state needs the REAL resolve + golden_dir.

    ``pdf_eq`` is load-bearing: its impl reads ``golden_dir.parent/"pdfs"/4.pdf``
    and byte-compares it to the seed's ``paper.pdf`` (which ``lit add`` moved from
    fixture 4), so a missing ``golden_dir`` raises before any check runs.
    """
    return {
        "id": "A2-resolve",
        "layer": "front-door",
        "expected_end_state": [
            "index_has: title~PeptideBERT year==2023",
            "pdf_eq: papers/<peptidebert>/paper.pdf == fixture:4",
            "health: clean",
        ],
    }


def test_run_batch_real_resolve_missing_golden_dir_raises() -> None:
    """REGRESSION (FIX A): without score_kwargs={'golden_dir': ...} the default
    score_fn=resolve raises TypeError on the first scored card.

    This is the exact crash the live path hit (golden_dir never threaded). It
    must FAIL on the pre-fix code and is the guard that keeps run_bench.py honest.
    """
    import pytest
    from harness.seeds import build_seed

    seed_vault = build_seed("seed-2papers-peptide")

    def fake_run(card, *, round, model, **_):
        # The live-shaped handle: NO golden_dir, NO _cleanup (the real seed must
        # survive). resolve() needs golden_dir as a required kwarg.
        return {"vault": seed_vault, "jsonl": [], "cwd": None, "run": None}

    cards = [_peptidebert_auto_card()]
    with pytest.raises(TypeError, match="golden_dir"):
        run_batch(cards, model="m", rounds=1, run_card_fn=fake_run)  # no score_kwargs


def test_run_batch_real_resolve_with_golden_dir_scores() -> None:
    """FIX A: threading score_kwargs={'golden_dir': GOLDEN_DIR} lets the REAL
    resolve score the card with no TypeError; resolved is computed (here 1)."""
    from harness.seeds import GOLDEN_DIR, build_seed

    seed_vault = build_seed("seed-2papers-peptide")

    def fake_run(card, *, round, model, **_):
        return {"vault": seed_vault, "jsonl": [], "cwd": None, "run": None}

    cards = [_peptidebert_auto_card()]
    report = run_batch(
        cards,
        model="m",
        rounds=1,
        run_card_fn=fake_run,
        score_kwargs={"golden_dir": GOLDEN_DIR},
    )
    # The card is auto-scored and resolves (real resolve ran end-to-end): the
    # PeptideBERT seed has the #4 pdf, a matching INDEX entry, and a clean health.
    assert report.cards[0].tag == "auto-scored"
    assert report.cards[0].mean == 1.0
    assert report.coverage["trr_denominator"] == 1
    assert report.trr_mean == 1.0


# ---------------------------------------------------------------------------
# FIX B — the live run-vault adapter tears down on run_card_impl failure
# ---------------------------------------------------------------------------


def test_build_live_run_card_fn_cleans_up_when_run_card_impl_raises(tmp_path: Path) -> None:
    """FIX B: if run_card_impl raises AFTER __enter__'s copytree but BEFORE the
    handle returns, the adapter must rm the run dir (no /tmp leak) and re-raise.

    The pre-fix code returned no handle, so run_batch never got a _cleanup and the
    copied vault leaked once per round. NEVER spawns — the fake just raises.
    """
    import pytest

    seen: dict = {}

    def boom_run_card(card, run_vault, *, fixtures_pdfs_dir, agent, model, base_url, auth_token):
        # __enter__ already copied the seed; capture the run dir, then fail like a
        # real install_repo_skills RuntimeError / claude-bin FileNotFoundError.
        seen["run_dir"] = Path(run_vault).parent
        raise RuntimeError("install-skill failed")

    run_fn = build_live_run_card_fn(
        fixtures_pdfs_dir=tmp_path / "pdfs",
        seeds_dir=tmp_path / "seeds",
        work_root=tmp_path / "work",
        run_card_impl=boom_run_card,
        ensure_seed_impl=_fake_seed(tmp_path),
    )
    (tmp_path / "work").mkdir(exist_ok=True)

    with pytest.raises(RuntimeError, match="install-skill failed"):
        run_fn({"id": "A2", "seed": "s"}, round=0, model="m")

    # The copytree landed (run dir existed) but was torn down on the exception.
    assert "run_dir" in seen and not seen["run_dir"].exists()


# ---------------------------------------------------------------------------
# FIX C — routing accuracy wired into the report (fake routing_run_fn, no spawn)
# ---------------------------------------------------------------------------


def _routing_card() -> dict:
    """A routing card with single + acceptable-set + absent-skill cases."""
    return {
        "id": "I-route",
        "layer": "routing",
        "in_scope_skills": ["lit-library", "lit-reading"],
        # observed below: hit, miss (wrong skill), acceptable-set hit, na (absent).
        "cases": [
            {"utt": "把这篇加到库里", "golden": "lit-library"},
            {"utt": "继续读", "golden": "lit-reading"},
            {"utt": "browse or roundup", "golden": ["lit-library", "lit-reading"]},
            {"utt": "引用这篇", "golden": "cite-retrieval"},
        ],
    }


def test_run_batch_routing_run_fn_none_leaves_ra_unscored() -> None:
    """Without routing_run_fn the routing card is tagged/counted but RA is None
    (honest 'not scored', never a fake 0)."""

    def fake_run(card, *, round, model, **_):
        return 1

    cards = [_routing_card()]
    report = run_batch(cards, model="m", rounds=1, run_card_fn=fake_run)
    assert report.coverage["counts"]["routing"] == 1
    assert report.routing is None
    assert report_to_dict(report)["routing"] is None


def test_run_batch_scores_routing_with_fake_routing_run_fn() -> None:
    """FIX C: a fake routing_run_fn returning canned observed skills makes
    run_batch compute the RA section (hit/miss/acceptable-set/na) correctly."""
    seen: dict = {}

    def fake_run(card, *, round, model, **_):
        return 1  # execution side not under test here

    def fake_routing_run(card, *, model):
        seen["model"] = model
        # Observed per case, in card.cases order: hit, wrong route (miss+spurious),
        # acceptable-set hit, and an observation for the absent-skill (na) case.
        return ["lit-library", "lit-library", "lit-reading", "lit-library"]

    cards = [_routing_card()]
    report = run_batch(
        cards, model="some-model", rounds=1, run_card_fn=fake_run, routing_run_fn=fake_routing_run
    )

    assert seen["model"] == "some-model"  # model passed straight through
    routing = report.routing
    assert routing is not None
    # 2 hits (case 1 + case 3), 1 miss (case 2), 1 na (case 4, absent skill).
    assert routing["hit"] == 2
    assert routing["miss"] == 1
    assert routing["na"] == 1
    assert routing["spurious"] == 1  # case 2 fired a present-but-wrong skill
    assert routing["scored"] == 3    # hits + misses (na excluded)
    assert routing["ra"] == 2 / 3
    assert routing["per_card"]["I-route"] == 2 / 3
    # Per-utterance trail is persisted so a miss is attributable (which sentence
    # routed where), not just an opaque per-card RA.
    trail = routing["per_card_trail"]["I-route"]
    assert [t["outcome"] for t in trail] == ["hit", "miss", "hit", "na"]
    assert [t["utt"] for t in trail] == [
        "把这篇加到库里", "继续读", "browse or roundup", "引用这篇",
    ]
    assert trail[1]["observed"] == "lit-library"  # the wrong-route miss
    assert trail[1]["detail"]  # carries why it missed
    assert trail[2]["golden"] == ["lit-library", "lit-reading"]  # acceptable set
    # report_to_dict carries the routing section (JSON-serializable, incl. trail).
    import json

    payload = report_to_dict(report)
    rt = json.loads(json.dumps(payload))["routing"]
    assert rt["ra"] == 2 / 3
    assert rt["per_card_trail"]["I-route"][1]["outcome"] == "miss"


def test_run_batch_aggregates_ra_across_two_routing_cards() -> None:
    """Overall RA = total hits / (hits + misses) across all scored routing cards."""

    def fake_run(card, *, round, model, **_):
        return 1

    def fake_routing_run(card, *, model):
        # card A: 2/2 hits; card B: 0/2 (both miss).
        return ["lit-library"] * 2 if card["id"] == "RA" else [None, None]

    card_a = {
        "id": "RA",
        "layer": "routing",
        "in_scope_skills": ["lit-library"],
        "cases": [
            {"utt": "u1", "golden": "lit-library"},
            {"utt": "u2", "golden": "lit-library"},
        ],
    }
    card_b = {
        "id": "RB",
        "layer": "routing",
        "in_scope_skills": ["lit-library", "lit-reading"],
        "cases": [
            {"utt": "u3", "golden": "lit-reading"},
            {"utt": "u4", "golden": "lit-library"},
        ],
    }
    report = run_batch(
        [card_a, card_b], model="m", rounds=1, run_card_fn=fake_run, routing_run_fn=fake_routing_run
    )
    routing = report.routing
    assert routing["hit"] == 2
    assert routing["miss"] == 2
    assert routing["scored"] == 4
    assert routing["ra"] == 0.5  # 2 hits / 4 scored
    assert routing["per_card"] == {"RA": 1.0, "RB": 0.0}


# ---------------------------------------------------------------------------
# FIX C — the live routing adapter delegates to the executor, never spawns here
# ---------------------------------------------------------------------------


def test_build_live_routing_run_fn_delegates_per_case(tmp_path: Path) -> None:
    """The routing adapter calls observe_impl once per case (in cases order) with
    model/auth passed through, and returns the observed skills. NEVER spawns —
    observe_impl is a fake."""
    calls: list[str] = []

    def fake_observe(utt, run_vault, *, fixtures_pdfs_dir, agent, model, base_url, auth_token):
        calls.append(str(utt))
        # the run vault is a real cp of the seed (so the adapter exercised RunVault)
        assert Path(run_vault).is_dir()
        assert model == "some-model"
        assert base_url == "https://proxy.example/v1"
        assert auth_token == "tok-9"
        return "lit-library" if "加到库" in str(utt) else None

    run_fn = build_live_routing_run_fn(
        seeds_dir=tmp_path / "seeds",
        work_root=tmp_path / "work",
        base_url="https://proxy.example/v1",
        auth_token="tok-9",
        observe_impl=fake_observe,
        ensure_seed_impl=_fake_seed(tmp_path),
    )
    (tmp_path / "work").mkdir(exist_ok=True)

    card = {
        "id": "I",
        "layer": "routing",
        "in_scope_skills": ["lit-library"],
        "cases": [{"utt": "把这篇加到库里"}, {"utt": "继续读"}],
    }
    observed = run_fn(card, model="some-model")
    assert calls == ["把这篇加到库里", "继续读"]
    assert observed == ["lit-library", None]


# ---------------------------------------------------------------------------
# Multi-agent reporting: (agent, model) as the unit, and honest absences
# ---------------------------------------------------------------------------


def _exec_card() -> dict:
    return {"id": "X", "layer": "f", "expected_end_state": ["path_exists: papers"]}


def test_report_unit_is_agent_plus_model() -> None:
    """The same weights served through three scaffolds are three data points."""

    def fake_run(card, *, round, model, **_):
        return 1

    report = run_batch(
        [_exec_card()], agent="cursor", model="claude-sonnet-4-6", rounds=1,
        run_card_fn=fake_run,
    )
    assert report.agent == "cursor"
    assert report.model_requested == "claude-sonnet-4-6"


def test_agent_flags_record_how_the_run_was_authorized() -> None:
    """Read from OUR adapter, never from the agent's stream: cursor reports
    permissionMode "default" while --force is in effect, so a transcript cannot be
    audited for this. A reader must see "this round ran with approval disabled"."""

    def fake_run(card, *, round, model, **_):
        return 1

    for agent, expected in [
        ("claude", ["--permission-mode", "bypassPermissions"]),
        ("cursor", ["--force"]),
        ("agy", ["--dangerously-skip-permissions"]),
    ]:
        report = run_batch(
            [_exec_card()], agent=agent, model="m", rounds=1, run_card_fn=fake_run
        )
        assert report.agent_flags == expected
        assert report_to_dict(report)["agent_flags"] == expected


def test_model_served_is_harvested_from_the_runs() -> None:
    def fake_run(card, *, round, model, **_):
        return {
            "vault": Path("/tmp/nope"),
            "jsonl": [],
            "run": ExecutorResult(model_served="Sonnet 4.6 200K Medium No Thinking"),
        }

    report = run_batch(
        [_exec_card()], agent="cursor", model="claude-sonnet-4-6", rounds=1,
        run_card_fn=fake_run, score_fn=lambda card, **kw: (1, []),
    )
    assert report.model_served == "Sonnet 4.6 200K Medium No Thinking"
    assert report.model_family == "claude-sonnet-4.6"  # explicit table, not a guess


def test_agent_that_reports_no_model_says_so() -> None:
    """agy: model_served is None. The family still resolves from what we REQUESTED
    (so a controlled comparison can group it), and Phase 0 records that agy's model
    went unverified — nothing here pretends the served model is known."""

    def fake_run(card, *, round, model, **_):
        return 1

    report = run_batch(
        [_exec_card()], agent="agy", model="Claude Sonnet 4.6 (Thinking)", rounds=1,
        run_card_fn=fake_run,
    )
    assert report.model_served is None
    assert report.model_family == "claude-sonnet-4.6"


def test_old_model_key_survives_as_an_alias() -> None:
    """report.json parsers already on the user's disk must not start reading None."""

    def fake_run(card, *, round, model, **_):
        return 1

    report = run_batch([_exec_card()], agent="cursor", model="claude-sonnet-4-6",
                       rounds=1, run_card_fn=fake_run)
    payload = report_to_dict(report)
    assert payload["model"] == "claude-sonnet-4-6" == payload["model_requested"]


# ---------------------------------------------------------------------------
# The routing axis: "cannot measure" is not "missed every one"
# ---------------------------------------------------------------------------


def test_not_measurable_routing_is_excluded_from_ra_not_scored_as_zero() -> None:
    """The most insidious pitfall in the design, at the aggregation layer.

    An agent with no skill-activation signal must NOT get RA 0.0. The cards stay
    tagged and counted; the RA section is an honest absence; and coverage says WHY
    it is absent so a reader can tell this apart from a dry run."""
    from harness.agents import NOT_MEASURABLE

    def fake_run(card, *, round, model, **_):
        return 1

    def unmeasurable_routing(card, *, model):
        return NOT_MEASURABLE

    report = run_batch(
        [_routing_card()], agent="agy", model="m", rounds=1,
        run_card_fn=fake_run, routing_run_fn=unmeasurable_routing,
    )

    assert report.routing is None                       # NOT {"ra": 0.0, ...}
    assert report.coverage["routing_ra"] == "not_measurable"
    assert report.coverage["routing_not_measurable_cards"] == ["I-route"]
    assert "agy" in report.coverage["routing_ra_reason"]
    assert report.coverage["counts"]["routing"] == 1    # still counted
    payload = report_to_dict(report)
    assert payload["routing"] is None
    assert payload["coverage"]["routing_ra"] == "not_measurable"


def test_coverage_distinguishes_not_scored_from_not_measurable() -> None:
    """Both leave report.routing None. They are completely different facts."""

    def fake_run(card, *, round, model, **_):
        return 1

    dry = run_batch([_routing_card()], model="m", rounds=1, run_card_fn=fake_run)
    assert dry.routing is None
    assert dry.coverage["routing_ra"] == "not_scored"

    def fake_routing_run(card, *, model):
        return ["lit-library", "lit-library", "lit-reading", "lit-library"]

    scored = run_batch(
        [_routing_card()], model="m", rounds=1,
        run_card_fn=fake_run, routing_run_fn=fake_routing_run,
    )
    assert scored.coverage["routing_ra"] == "scored"


def test_score_routing_rejects_the_sentinel_loudly() -> None:
    """Defence in depth. If the sentinel ever reaches the scorer it is NOT None, so
    it would fall through to the miss branch and produce a confident RA of 0.0."""
    import pytest

    from harness.agents import NOT_MEASURABLE
    from harness.routing import score_routing

    with pytest.raises(ValueError, match="NOT_MEASURABLE"):
        score_routing(
            _routing_card(),
            [NOT_MEASURABLE, NOT_MEASURABLE, NOT_MEASURABLE, NOT_MEASURABLE],
            present_skills=["lit-library", "lit-reading"],
        )


def test_routing_adapter_short_circuits_without_spawning(tmp_path: Path) -> None:
    """For an agent whose RA is unmeasurable the adapter returns the sentinel up
    front: ~14 classification spawns per card would buy exactly nothing."""
    from harness.agents import NOT_MEASURABLE

    def explode(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("must not probe an agent whose RA is unmeasurable")

    run_fn = build_live_routing_run_fn(
        seeds_dir=tmp_path / "seeds",
        work_root=tmp_path / "work",
        agent="agy",
        observe_impl=explode,
        ensure_seed_impl=explode,
    )
    assert run_fn(_routing_card(), model="m") is NOT_MEASURABLE


# ---------------------------------------------------------------------------
# Token accounting across agents with different counter support
# ---------------------------------------------------------------------------


def test_sum_usage_never_treats_a_missing_report_as_zero() -> None:
    """A spawn that reported no usage (agy has no counters; a crashed run reported
    none) must be DROPPED, not added as zeros — and it must not inflate `spawns`
    either. "3 spawns, 0 tokens" is a claim we cannot make."""
    from harness.batch import _sum_usage

    real = {
        "input_tokens": 10, "output_tokens": 20,
        "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40,
    }
    mixed = _sum_usage([real, {}, None, real])  # type: ignore[list-item]
    assert mixed["input_tokens"] == 20
    assert mixed["output_tokens"] == 40
    assert mixed["spawns"] == 2  # the two unreported spawns are not counted


def test_sum_usage_of_an_agent_with_no_counters_is_absent_not_zero() -> None:
    from harness.batch import _sum_usage

    assert _sum_usage([{}, None, {}]) == {}  # type: ignore[list-item]


def test_tokens_section_is_none_for_an_agent_with_no_counters() -> None:
    """agy's tokens must read None all the way to the report — never a 0 bucket."""

    def fake_run(card, *, round, model, **_):
        return {"vault": Path("/tmp/nope"), "jsonl": [], "run": ExecutorResult(usage={})}

    report = run_batch(
        [_exec_card()], agent="agy", model="Claude Sonnet 4.6 (Thinking)", rounds=2,
        run_card_fn=fake_run, score_fn=lambda card, **kw: (1, []),
    )
    assert report.tokens is None
    assert report_to_dict(report)["tokens"] is None
    assert report.trr_mean == 1.0  # TRR is still scored: only tokens are unmeasurable


def test_cursor_usage_reaches_the_report_normalized() -> None:
    """End-to-end for the camelCase pitfall: the adapter normalizes at its edge, so
    the batch's snake_case summing sees real numbers rather than a silent zero."""
    from harness.agents.cursor import normalize_usage

    raw = {"inputTokens": 4, "outputTokens": 86,
           "cacheReadTokens": 19868, "cacheWriteTokens": 20024}

    def fake_run(card, *, round, model, **_):
        return {
            "vault": Path("/tmp/nope"), "jsonl": [],
            "run": ExecutorResult(usage=normalize_usage(raw)),
        }

    report = run_batch(
        [_exec_card()], agent="cursor", model="claude-sonnet-4-6", rounds=1,
        run_card_fn=fake_run, score_fn=lambda card, **kw: (1, []),
    )
    assert report.tokens["total"]["input_tokens"] == 4
    assert report.tokens["total"]["cache_read_input_tokens"] == 19868
    assert report.tokens["total"]["spawns"] == 1


def test_qualification_rides_into_the_report() -> None:
    """Phase 0 is a deliverable, not just a gate: a reader must see what was
    verified — and, for agy, what could not be."""

    def fake_run(card, *, round, model, **_):
        return 1

    sheet = {"agent": "agy", "ok": True, "checks": [
        {"name": "model_pinned", "status": "skip", "detail": "UNVERIFIED"},
    ]}
    report = run_batch(
        [_exec_card()], agent="agy", model="m", rounds=1,
        run_card_fn=fake_run, qualification=sheet,
    )
    assert report_to_dict(report)["qualification"] == sheet


def test_family_is_not_invented_when_no_run_observed_a_model() -> None:
    """Minor 8: only execution rounds harvest `model_served`, so a routing-only
    run (or one whose spawns all died before reporting) leaves it None for an
    agent that DOES report models. The family must stay None there — deriving it
    from the request would dress the gap up as knowledge."""

    def fake_run(card, *, round, model, **_):
        return 1

    routing_only = run_batch(
        [_routing_card()], agent="cursor", model="claude-sonnet-4-6", rounds=1,
        run_card_fn=fake_run,
    )
    assert routing_only.model_served is None
    assert routing_only.model_family is None  # NOT "claude-sonnet-4.6"

    # agy is the one agent for which the request IS the only source there is.
    agy = run_batch(
        [_routing_card()], agent="agy", model="Claude Sonnet 4.6 (Thinking)", rounds=1,
        run_card_fn=fake_run,
    )
    assert agy.model_served is None
    assert agy.model_family == "claude-sonnet-4.6"
