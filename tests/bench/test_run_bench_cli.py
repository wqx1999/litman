"""Deterministic tests for run_bench.py's CLI glue (the --run-dir sugar).

Drives ``run_bench.main`` in ``--dry-run`` mode (the fake executor returns 0 with
NO claude -p spawn, M34 §3.5 hard boundary), so the run-dir filing is exercised
end-to-end with zero agent calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import run_bench
from harness.agents import AGENT_NAMES, get_adapter
from harness.executor import ExecutorResult


def _proxy_agents() -> list[str]:
    """Agents declaring an Anthropic-compatible proxy mode. Called, never cached.

    Resolved inside a test body, NOT at import time. A module-level comprehension
    over ``get_adapter(n).supports_anthropic_proxy`` reads the attribute during
    COLLECTION, so an adapter that simply forgot to declare it aborts the whole
    session with a bare AttributeError pointing at this file — and
    ``test_every_agent_declares_the_whole_adapter_surface``, the test written to
    explain exactly that mistake, never gets to run. Deferring it keeps the
    purpose-built test the one that reports.
    """
    return [n for n in AGENT_NAMES if get_adapter(n).supports_anthropic_proxy]


def test_run_dir_files_report_and_transcripts(tmp_path: Path) -> None:
    """--run-dir DIR writes DIR/report.json AND DIR/transcripts/ (the sugar)."""
    rd = tmp_path / "debug" / "cc_haiku_260603_0"
    rc = run_bench.main(
        [
            "--model", "claude-haiku-4-5-20251001",
            "--rounds", "1",
            "--cards", "C2-show",
            "--run-dir", str(rd),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert (rd / "report.json").is_file()
    report = json.loads((rd / "report.json").read_text(encoding="utf-8"))
    assert report["model"] == "claude-haiku-4-5-20251001"

    # The transcript dump path is wired through --run-dir (dry-run round → file).
    tdir = rd / "transcripts"
    assert tdir.is_dir()
    assert any("C2-show" in f.name for f in tdir.glob("*.json"))


def test_run_dir_explicit_out_overrides_but_transcripts_still_default(tmp_path: Path) -> None:
    """Explicit --out wins over the run-dir default; --keep-transcript still
    defaults into the run dir (each override is independent)."""
    rd = tmp_path / "debug" / "cc_haiku_260603_1"
    custom = tmp_path / "elsewhere" / "custom.json"
    custom.parent.mkdir(parents=True)
    rc = run_bench.main(
        [
            "--model", "claude-haiku-4-5-20251001",
            "--rounds", "1",
            "--cards", "C2-show",
            "--run-dir", str(rd),
            "--out", str(custom),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert custom.is_file()                  # explicit --out honored
    assert not (rd / "report.json").exists()  # run-dir default not used for report
    assert (rd / "transcripts").is_dir()      # transcript default still applied


# ---------------------------------------------------------------------------
# --base-url needs a proxy-capable agent, and must be refused BEFORE anything spawns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_proxy_flags_are_refused_for_agents_without_a_proxy_mode(agent, capsys) -> None:
    """Their `prepare()` also raises — but that fires inside the first card, after
    Phase 0 has already burned two live spawns, and surfaces as a bare traceback.
    argparse must refuse it at the boundary instead.

    Parametrized over every agent and branched INSIDE the body, so the roster is
    never hand-listed and the next agent is covered whichever way it declares.
    """
    supported = _proxy_agents()
    if agent in supported:
        pytest.skip(f"{agent} has a proxy mode; its accept path is tested separately")

    with pytest.raises(SystemExit) as e:
        run_bench.main(
            ["--agent", agent, "--base-url", "http://localhost:4000", "--dry-run"]
        )
    assert e.value.code == 2  # argparse usage error, not a traceback
    err = capsys.readouterr().err
    assert "proxy mode" in err
    # The way out is named from the registry, so the day a second proxy-capable
    # agent lands the message stops advertising only claude. Asserting the list is
    # non-empty first: an empty loop below would assert nothing while looking like
    # it did — the same shape as the bug this whole change repairs.
    assert supported, "no agent declares a proxy mode; the error text has no way out"
    for name in supported:
        assert f"--agent {name}" in err


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_auth_token_alone_is_also_refused(agent, capsys) -> None:
    if agent in _proxy_agents():
        pytest.skip(f"{agent} has a proxy mode")
    with pytest.raises(SystemExit):
        run_bench.main(["--agent", agent, "--auth-token", "tok", "--dry-run"])
    assert "proxy mode" in capsys.readouterr().err


def test_proxy_flags_are_still_accepted_for_claude(tmp_path: Path) -> None:
    """The external-model path (6 of the 9 completed production runs) must keep
    working exactly as before."""
    rc = run_bench.main(
        [
            "--agent", "claude",
            "--base-url", "http://localhost:4000",
            "--auth-token", "tok",
            "--model", "deepseek-v4-pro",
            "--rounds", "1",
            "--cards", "C2-show",
            "--out", str(tmp_path / "r.json"),
            "--dry-run",
        ]
    )
    assert rc == 0


def test_agent_appears_in_the_report_json(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    run_bench.main(
        ["--agent", "cursor", "--rounds", "1", "--cards", "C2-show",
         "--out", str(out), "--dry-run"]
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["agent"] == "cursor"
    assert report["model_requested"] == "claude-sonnet-4-6"  # the agent's own default
    assert report["agent_flags"] == ["--force"]


# ---------------------------------------------------------------------------
# Phase 0 gate wiring: a failed qualification aborts the WHOLE run (AC4)
# ---------------------------------------------------------------------------


def test_a_failed_qualification_exits_nonzero_before_any_live_wiring(
    monkeypatch, capsys
) -> None:
    """Live-mode ``main()`` with a failing Phase 0 must exit non-zero and never
    reach the live adapter builders. Every other test here drives --dry-run
    (which skips the gate), so if the SystemExit at the gate were ever softened
    to a print-and-continue, this is the only test that goes red."""
    from harness.qualify import QualCheck, Qualification

    monkeypatch.delenv("LITMAN_BENCH_FAKE", raising=False)
    qual = Qualification(agent="claude", model_requested="claude-haiku-4-5-20251001")
    qual.checks.append(
        QualCheck(name="binary present", status="fail", detail="no such binary")
    )
    monkeypatch.setattr(run_bench, "qualify", lambda *a, **k: qual)

    def _boom(*a, **k):
        raise AssertionError("a live adapter was built despite a failed Phase 0")

    monkeypatch.setattr(run_bench, "build_live_run_card_fn", _boom)
    monkeypatch.setattr(run_bench, "build_live_routing_run_fn", _boom)

    with pytest.raises(SystemExit) as e:
        run_bench.main(["--rounds", "1", "--cards", "C2-show"])  # NO --dry-run
    # SystemExit with a message exits 1; the message itself says why.
    assert e.value.code not in (0, None)
    assert "Phase 0 failed" in str(e.value.code)
    assert "NOT QUALIFIED" in capsys.readouterr().out  # the sheet printed first


# ---------------------------------------------------------------------------
# The journal: a run that stops half way keeps what it paid for
# ---------------------------------------------------------------------------

_TWO_CARDS = "C1-list-filter,C2-show"


def _run(rd: Path, *extra: str, cards: str = _TWO_CARDS) -> int:
    return run_bench.main(
        ["--model", "claude-haiku-4-5-20251001", "--rounds", "1",
         "--cards", cards, "--run-dir", str(rd), "--dry-run", *extra]
    )


def _journal(rd: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (rd / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_resume_requires_a_run_dir() -> None:
    """The journal lives in the run dir; --resume without one names nothing."""
    with pytest.raises(SystemExit) as ei:
        run_bench.main(["--resume", "--dry-run"])
    assert ei.value.code == 2  # argparse usage error


def test_a_run_journals_its_session_and_every_card(tmp_path: Path) -> None:
    rd = tmp_path / "run"
    assert _run(rd) == 0
    records = _journal(rd)
    assert records[0]["type"] == "session"
    assert set(records[0]["ruler"]) == {"litman", "scenarios", "harness"}
    assert [r["card_id"] for r in records[1:]] == ["C1-list-filter", "C2-show"]


def test_journal_without_resume_refuses_and_preserves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC10. Silently continuing hands someone who meant to re-measure a free
    report identical to last time; silently overwriting throws away cards that
    were paid for. Refuse, spend nothing, and touch nothing."""
    rd = tmp_path / "run"
    assert _run(rd) == 0
    before = (rd / "journal.jsonl").read_text(encoding="utf-8")

    ran: list[str] = []
    monkeypatch.setattr(
        run_bench, "_fake_run_card", lambda card, **kw: ran.append(card.id) or 0
    )
    with pytest.raises(SystemExit) as ei:
        _run(rd)
    assert ei.value.code != 0
    assert "--resume" in str(ei.value)  # says how to continue it
    assert ran == []  # and spends nothing while saying so
    assert (rd / "journal.jsonl").read_text(encoding="utf-8") == before  # untouched


def test_resume_refuses_a_changed_card_set(tmp_path: Path) -> None:
    """Resume means "the same run", not "the same run plus two extra cards": the
    corpus is part of what the run's TRR means."""
    rd = tmp_path / "run"
    assert _run(rd, cards="C1-list-filter") == 0

    with pytest.raises(SystemExit) as ei:
        _run(rd, "--resume", cards=_TWO_CARDS)
    assert "card set" in str(ei.value)
    assert "C2-show" in str(ei.value)  # names what it is refusing over


def test_resume_runs_only_the_unfinished_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6. The second sitting runs only what is left, and still produces a FULL
    report — TRR recomputed over the whole corpus in one pass, never two TRRs
    averaged together."""
    rd = tmp_path / "run"
    ran: list[str] = []
    real_fake = run_bench._fake_run_card

    def counting(card, *, round, model, **kw):
        ran.append(card.id)
        return real_fake(card, round=round, model=model, **kw)

    monkeypatch.setattr(run_bench, "_fake_run_card", counting)
    assert _run(rd) == 0
    assert ran == ["C1-list-filter", "C2-show"]

    # Amputate the journal to the first card, as an abort mid-run would have left it.
    records = _journal(rd)
    (rd / "journal.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records[:2]), encoding="utf-8"
    )

    ran.clear()
    assert _run(rd, "--resume") == 0
    assert ran == ["C2-show"]  # C1 was NOT re-run

    report = json.loads((rd / "report.json").read_text(encoding="utf-8"))
    assert [c["card_id"] for c in report["cards"]] == ["C1-list-filter", "C2-show"]
    assert report["coverage"]["trr_denominator"] == 2  # the WHOLE corpus, recomputed
    assert len(report["sessions"]) == 2


def test_resume_refuses_a_changed_served_model_before_spending_a_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7. D1: no --force-resume — the two halves would not be one experiment.

    Driven through the LIVE branch (Phase 0 + the adapter builders, all faked) so
    the refusal is proven where it has to happen: after Phase 0 has named the
    model, before the first card is spawned. That ordering is the whole value —
    a mismatch found from the first card's run handle has already paid a card to
    learn what Phase 0 knew for free.
    """
    from harness.qualify import QualCheck, Qualification

    monkeypatch.delenv("LITMAN_BENCH_FAKE", raising=False)
    rd = tmp_path / "run"
    ran: list[str] = []

    def _serving(model: str):
        qual = Qualification(
            agent="cursor", model_requested="composer-2.5", model_served=model
        )
        qual.checks.append(QualCheck(name="model pinned", status="pass", detail=model))
        return lambda *a, **k: qual

    # Never spawns: the adapter builder is replaced by a counting fake, so "zero
    # cards spent" is asserted on the call list rather than inferred.
    monkeypatch.setattr(
        run_bench,
        "build_live_run_card_fn",
        lambda **kw: (lambda card, **k: ran.append(card.id) or 0),
    )
    monkeypatch.setattr(run_bench, "build_live_routing_run_fn", lambda **kw: None)

    argv = ["--agent", "cursor", "--model", "composer-2.5", "--rounds", "1",
            "--cards", _TWO_CARDS, "--run-dir", str(rd)]

    monkeypatch.setattr(run_bench, "qualify", _serving("Composer 2.5"))
    assert run_bench.main(argv) == 0
    assert ran == ["C1-list-filter", "C2-show"]
    assert _journal(rd)[0]["model_served"] == "Composer 2.5"
    was = (rd / "report.json").read_text(encoding="utf-8")

    # Sitting 2: the same command, a different model on the other end of it.
    monkeypatch.setattr(run_bench, "qualify", _serving("Composer 3"))
    ran.clear()
    with pytest.raises(SystemExit) as ei:
        run_bench.main([*argv, "--resume"])
    msg = str(ei.value)
    assert "served model" in msg          # names the field, not two hashes
    assert "Composer 2.5" in msg and "Composer 3" in msg
    assert ran == []                      # zero cards spent to learn this
    # A refusal changes nothing: sitting 1's report is still sitting 1's, measured
    # by Composer 2.5 and still saying so.
    assert (rd / "report.json").read_text(encoding="utf-8") == was


def test_resume_refuses_a_moved_ruler_and_names_the_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8 at the CLI seam. D3: naming the part is the requirement — "something
    changed" plus two hex strings is a refusal the reader cannot act on."""
    rd = tmp_path / "run"
    assert _run(rd) == 0

    records = _journal(rd)
    records[0]["ruler"]["scenarios"] = "0000stale0000"
    (rd / "journal.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )

    ran: list[str] = []
    monkeypatch.setattr(
        run_bench, "_fake_run_card", lambda card, **kw: ran.append(card.id) or 0
    )
    with pytest.raises(SystemExit) as ei:
        _run(rd, "--resume")
    assert "scenarios" in str(ei.value)
    assert ran == []


def test_resume_with_no_journal_warns_and_starts_fresh(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A mistyped --run-dir under --resume silently pays for a whole run. Nothing
    is lost or invented by starting fresh, so this is a warning rather than D6's
    refusal — but it must not pass in silence."""
    rd = tmp_path / "empty"
    assert _run(rd, "--resume") == 0
    assert "nothing to resume" in capsys.readouterr().err


def test_abort_writes_no_report_but_keeps_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5. D4: report.json means ONE COMPLETE MEASUREMENT. A half-batch file
    would eventually have its TRR quoted by someone who did not read the caveat.
    The journal is the partial artifact — that is what it is for."""
    rd = tmp_path / "run"
    cards = "C1-list-filter,C2-show,C3-search-notes,C4-related-samegroup"

    def dying(card, *, round, model, **kw):
        # Every spawn is dead on arrival: out of quota.
        return {
            "vault": Path("/tmp/nope"),
            "jsonl": [],
            "run": ExecutorResult(exit_code=1),
        }

    monkeypatch.setattr(run_bench, "_fake_run_card", dying)
    with pytest.raises(SystemExit) as ei:
        _run(rd, cards=cards)
    assert ei.value.code == run_bench.EXIT_BATCH_ABORTED
    assert ei.value.code != 1  # distinguishable from a Phase 0 failure
    assert not (rd / "report.json").exists()

    records = _journal(rd)
    errored = [r for r in records if r["type"] == "card"]
    assert len(errored) == 3  # the breaker's three, all on disk
    assert all(r["error"]["reason"] == "exit" for r in errored)


def test_journal_survives_a_crash_mid_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC11. The failure this guards against is a SIGKILL between two cards (a
    wall-clock limit ends a run exactly this way). Cards already measured must be
    on disk, not in a buffer."""
    rd = tmp_path / "run"
    cards = "C1-list-filter,C2-show,C3-search-notes"
    seen: list[str] = []

    def boom(card, *, round, model, **kw):
        seen.append(card.id)
        if len(seen) == 3:
            raise KeyboardInterrupt("the node went away")
        return 0

    monkeypatch.setattr(run_bench, "_fake_run_card", boom)
    with pytest.raises(KeyboardInterrupt):
        _run(rd, cards=cards)

    done = [r["card_id"] for r in _journal(rd) if r["type"] == "card"]
    assert done == ["C1-list-filter", "C2-show"]  # the two that finished, on disk
    assert not (rd / "report.json").exists()


# ---------------------------------------------------------------------------
# agy: the model nobody can verify (D2)
# ---------------------------------------------------------------------------


def test_agy_report_is_marked_unverified(tmp_path: Path) -> None:
    """AC9. agy reports no served model, so the per-round check every other agent
    gets is a no-op for it. That is a standing property of the agent, marked on
    EVERY report of it — not something a resume introduces."""
    rd = tmp_path / "run"
    rc = run_bench.main(
        ["--agent", "agy", "--rounds", "1", "--cards", "C2-show",
         "--run-dir", str(rd), "--dry-run"]
    )
    assert rc == 0
    report = json.loads((rd / "report.json").read_text(encoding="utf-8"))
    assert report["model_identity"] == "unverified"
    # The reason states what is UNKNOWABLE. It must not assert that agy changed
    # model — we have no evidence of that, and a suspicion printed as a finding is
    # the same sin as a guess printed as a number.
    reason = report["model_identity_reason"]
    assert "nobody would know" in reason
    assert "might" not in reason and "may have" not in reason


def test_verifiable_agents_carry_no_unverified_mark(tmp_path: Path) -> None:
    """AC9's control group: the mark means something only if it is not on
    everything."""
    for agent in ("claude", "cursor"):
        rd = tmp_path / agent
        assert (
            run_bench.main(
                ["--agent", agent, "--rounds", "1", "--cards", "C2-show",
                 "--run-dir", str(rd), "--dry-run"]
            )
            == 0
        )
        report = json.loads((rd / "report.json").read_text(encoding="utf-8"))
        assert report["model_identity"] is None
        assert report["model_identity_reason"] is None


def test_agy_resumes_with_both_sides_reporting_no_model(tmp_path: Path) -> None:
    """AC9: `None == None` proves nothing, but contradicts nothing either, and
    Phase 0 SKIPs agy's model check rather than FAILing it. Refusing to resume it
    would be a different rule for the same fact."""
    rd = tmp_path / "run"
    args = ["--agent", "agy", "--rounds", "1", "--cards", _TWO_CARDS,
            "--run-dir", str(rd), "--dry-run"]
    assert run_bench.main(args) == 0
    assert run_bench.main([*args, "--resume"]) == 0
    report = json.loads((rd / "report.json").read_text(encoding="utf-8"))
    assert report["model_identity"] == "unverified"  # still marked, 2 sessions in
    assert len(report["sessions"]) == 2


def test_the_seed_alarm_is_not_swallowed_by_the_abort_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() grew an `except BatchAbortedError` around build_report. It must stay
    exactly that narrow.

    A seed leak and an aborted batch are opposite verdicts: an abort says the data
    is valid but incomplete (keep the journal, resume later), a leak says the data
    is INVALID (nothing measured against a moved seed can be kept). Widening that
    clause to `except RuntimeError` — which SeedLeakError also is — would convert
    every leak into a tidy "resume me" message and an exit code that means the
    opposite of what happened. This test is the tripwire on that edit.
    """
    from harness.seeds import SeedLeakError

    def leaking_run_batch(*a, **kw):
        raise SeedLeakError("seed /tmp/seed was MUTATED by card 'D4-unlink' round 0")

    monkeypatch.setattr(run_bench, "run_batch", leaking_run_batch)
    with pytest.raises(SeedLeakError):  # NOT SystemExit(EXIT_BATCH_ABORTED)
        _run(tmp_path / "run")


def test_a_dry_run_dir_cannot_be_resumed_live_and_publish_its_zeros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CRITICAL hole, end to end, on the agent it actually bit.

    agy reports no served model, so a DRY agy session and a LIVE agy session
    journal identical fields — same agent, model, rounds, cards, ruler, and
    model_served=None on both sides. The gate therefore passed, and `_fake_run_card`'s
    hard-coded `mean=0.0` was adopted into the live report's auto_means and
    published under agy's name: a non-measurement silently becoming a number.

    claude/cursor only escaped by accident (Phase 0's _check_model_pinned FAILs a
    served_model=True agent that reports None, so a live claude either records a
    real model or never gets here). agy has no such backstop and is a first-class
    agent on this branch, so the fix cannot rest on that accident.
    """
    from harness.qualify import QualCheck, Qualification

    rd = tmp_path / "run"
    # Sitting 1: DRY. Nothing spawns; every card is a hard-coded 0.
    assert run_bench.main(
        ["--agent", "agy", "--rounds", "1", "--cards", _TWO_CARDS,
         "--run-dir", str(rd), "--dry-run"]
    ) == 0
    assert all(r["mean"] == 0.0 for r in _journal(rd) if r["type"] == "card")

    # Sitting 2: LIVE agy, same command minus --dry-run. Phase 0 reports no model,
    # exactly as the dry sitting recorded — every journaled field matches.
    monkeypatch.delenv("LITMAN_BENCH_FAKE", raising=False)
    qual = Qualification(
        agent="agy", model_requested=run_bench.get_adapter("agy").default_model,
        model_served=None,
    )
    qual.checks.append(QualCheck(name="model pinned", status="skip", detail="agy"))
    monkeypatch.setattr(run_bench, "qualify", lambda *a, **k: qual)
    ran: list[str] = []
    monkeypatch.setattr(
        run_bench,
        "build_live_run_card_fn",
        lambda **kw: (lambda card, **k: ran.append(card.id) or 1),
    )
    monkeypatch.setattr(run_bench, "build_live_routing_run_fn", lambda **kw: None)

    with pytest.raises(SystemExit) as ei:
        run_bench.main(
            ["--agent", "agy", "--rounds", "1", "--cards", _TWO_CARDS,
             "--run-dir", str(rd), "--resume"]
        )
    assert "dry" in str(ei.value).lower()
    assert ran == []  # refused before a single live spawn
    # The dry sitting's report is still the dry sitting's: the refusal published
    # nothing and overwrote nothing.
    assert json.loads((rd / "report.json").read_text(encoding="utf-8"))["cards"] == [
        {"card_id": "C1-list-filter", "tag": "auto-scored", "rounds": [0],
         "mean": 0.0, "usage": None, "error": None},
        {"card_id": "C2-show", "tag": "auto-scored", "rounds": [0],
         "mean": 0.0, "usage": None, "error": None},
    ]


def test_a_live_run_dir_cannot_be_resumed_dry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror: a dry sitting on a live journal would journal fake zeros AFTER
    the real cards, and last-wins (D7) would promote them over the measurements."""
    from harness.qualify import QualCheck, Qualification

    rd = tmp_path / "run"
    monkeypatch.delenv("LITMAN_BENCH_FAKE", raising=False)
    qual = Qualification(
        agent="agy", model_requested=run_bench.get_adapter("agy").default_model,
        model_served=None,
    )
    qual.checks.append(QualCheck(name="model pinned", status="skip", detail="agy"))
    monkeypatch.setattr(run_bench, "qualify", lambda *a, **k: qual)
    monkeypatch.setattr(
        run_bench, "build_live_run_card_fn", lambda **kw: (lambda card, **k: 1)
    )
    monkeypatch.setattr(run_bench, "build_live_routing_run_fn", lambda **kw: None)
    argv = ["--agent", "agy", "--rounds", "1", "--cards", _TWO_CARDS,
            "--run-dir", str(rd)]
    assert run_bench.main(argv) == 0
    before = (rd / "journal.jsonl").read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as ei:
        run_bench.main([*argv, "--dry-run", "--resume"])
    assert "dry" in str(ei.value).lower()
    assert (rd / "journal.jsonl").read_text(encoding="utf-8") == before


def test_a_model_change_abort_points_at_a_new_run_dir_not_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Two aborts, two different ways out — the advice must know which it is.

    After a model change, --resume is refused by the D1 gate for as long as Phase 0
    keeps reading the new model, so the generic "re-submit with --resume" would
    cost the reader a live Phase 0 to be told no. NOT "guaranteed to be refused":
    a transient flip that reverts would legitimately pass the gate, because the
    flipping card never reached the journal (the abort fires before on_card_done),
    so every card in it really was served by the baseline model. The message names
    that condition instead of claiming resume is impossible. The quota case is the
    opposite: --resume is exactly right there.
    """
    rd = tmp_path / "run"
    cards = "C1-list-filter,C2-show,C3-search-notes"
    seen: list[str] = []

    def swapping(card, *, round, model, **kw):
        seen.append(card.id)
        served = "claude-haiku-4-5-20251001" if len(seen) < 2 else "claude-haiku-3"
        return {
            "vault": Path("/tmp/nope"), "jsonl": [],
            "run": ExecutorResult(exit_code=0, model_served=served),
        }

    monkeypatch.setattr(run_bench, "_fake_run_card", swapping)
    with pytest.raises(SystemExit) as ei:
        _run(rd, cards=cards)
    assert ei.value.code == run_bench.EXIT_BATCH_ABORTED

    err = capsys.readouterr().err
    assert "NEW run dir" in err
    assert "claude-haiku-3" in err  # names what it is now serving
    # The one thing it must NOT do is send them down a path the gate will refuse.
    assert "--resume in this run dir" not in err
    assert not (rd / "report.json").exists()


def test_a_quota_abort_still_points_at_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The control for the test above: the breaker's advice is unchanged."""
    rd = tmp_path / "run"
    cards = "C1-list-filter,C2-show,C3-search-notes,C4-related-samegroup"
    monkeypatch.setattr(
        run_bench,
        "_fake_run_card",
        lambda card, **kw: {"vault": Path("/tmp/nope"), "jsonl": [],
                            "run": ExecutorResult(exit_code=1)},
    )
    with pytest.raises(SystemExit):
        _run(rd, cards=cards)
    err = capsys.readouterr().err
    assert "--resume" in err
    assert "NEW run dir" not in err


def test_a_full_dry_corpus_reports_zero_errored_cards(tmp_path: Path) -> None:
    """AC3, literally: the whole corpus, dry, and not one card judged errored.

    _fake_run_card returns a bare int, so _error_of must read "no run handle" as
    "no error". Get that backwards and every dry run — and every test in this
    file — reports the entire corpus as failed."""
    rd = tmp_path / "run"
    assert run_bench.main(["--rounds", "1", "--run-dir", str(rd), "--dry-run"]) == 0
    report = json.loads((rd / "report.json").read_text(encoding="utf-8"))
    assert report["coverage"]["errored_cards"] == []
    assert all(c["error"] is None for c in report["cards"])
    assert len(report["cards"]) > 20  # the real corpus, not a filtered handful


def test_abort_then_resume_retries_exactly_the_errored_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole loop, end to end: quota dies mid-batch → abort with no report →
    quota returns → --resume skips what was measured, retries what died (D7, no
    extra switch), and emits ONE full report."""
    rd = tmp_path / "run"
    cards = "C1-list-filter,C2-show,C3-search-notes,C4-related-samegroup,F1-export-bib"
    sitting1: list[str] = []

    def quota_dies(card, *, round, model, **kw):
        sitting1.append(card.id)
        ok = len(sitting1) <= 2
        return {
            "vault": Path("/tmp/nope"), "jsonl": [],
            "run": ExecutorResult(exit_code=0 if ok else 1),
        }

    monkeypatch.setattr(run_bench, "_fake_run_card", quota_dies)
    with pytest.raises(SystemExit) as ei:
        _run(rd, cards=cards)
    assert ei.value.code == run_bench.EXIT_BATCH_ABORTED
    assert not (rd / "report.json").exists()
    assert sitting1 == ["C1-list-filter", "C2-show", "C3-search-notes",
                        "C4-related-samegroup", "F1-export-bib"]

    # The quota comes back.
    sitting2: list[str] = []

    def healthy(card, *, round, model, **kw):
        sitting2.append(card.id)
        return {"vault": Path("/tmp/nope"), "jsonl": [],
                "run": ExecutorResult(exit_code=0)}

    monkeypatch.setattr(run_bench, "_fake_run_card", healthy)
    assert _run(rd, "--resume", cards=cards) == 0
    # Exactly the three that died — the two that finished are not paid for twice.
    assert sitting2 == ["C3-search-notes", "C4-related-samegroup", "F1-export-bib"]

    report = json.loads((rd / "report.json").read_text(encoding="utf-8"))
    assert len(report["cards"]) == 5              # one full report, not an increment
    assert report["coverage"]["errored_cards"] == []  # the retries cleared them
    assert report["coverage"]["trr_denominator"] == 5
    assert len(report["sessions"]) == 2
