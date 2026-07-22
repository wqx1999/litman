"""Deterministic tests for the provenance module (ruler fingerprint + journal).

Never spawns anything: the fingerprint reads files, the journal is jsonl, and the
resume gate is a pure comparison of two dicts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import provenance
from harness.batch import CardScore
from harness.provenance import (
    JOURNAL_NAME,
    append_record,
    baseline_session,
    card_record,
    check_resumable,
    read_records,
    resumable_scores,
    ruler_fingerprint,
    score_from_record,
    session_record,
)

# ---------------------------------------------------------------------------
# The ruler fingerprint: three parts, and it notices an equal-length edit
# ---------------------------------------------------------------------------


def test_ruler_fingerprint_has_three_named_parts() -> None:
    fp = ruler_fingerprint()
    assert set(fp) == {"litman", "scenarios", "harness"}
    assert all(isinstance(v, str) and v for v in fp.values())


def test_ruler_fingerprint_is_stable_across_calls() -> None:
    assert ruler_fingerprint() == ruler_fingerprint()


def test_scenarios_digest_catches_an_equal_LENGTH_edit(tmp_path: Path) -> None:
    """AC8's teeth. The likeliest way to change a card's meaning — `>=` becoming
    `>` in an assertion — does not change the file's SIZE. A size+mtime scheme
    (what litman_fingerprint uses, correctly, for a different question) would call
    this the same ruler."""
    d = tmp_path / "scenarios"
    d.mkdir()
    card = d / "A1.yaml"
    card.write_text("expected_end_state:\n  - count_ge: 3\n", encoding="utf-8")
    before = provenance._content_digest(list(d.glob("*.yaml")), root=d)

    edited = "expected_end_state:\n  - count_gt: 3\n"
    assert len(edited) == len(card.read_text(encoding="utf-8"))  # same length!
    card.write_text(edited, encoding="utf-8")

    assert provenance._content_digest(list(d.glob("*.yaml")), root=d) != before


def test_content_digest_notices_a_rename(tmp_path: Path) -> None:
    """The path is digested with the bytes: moving an assertion from one card to
    another leaves the byte multiset identical and the meaning different."""
    d = tmp_path / "s"
    d.mkdir()
    (d / "A1.yaml").write_text("x: 1\n", encoding="utf-8")
    before = provenance._content_digest(list(d.glob("*.yaml")), root=d)
    (d / "A1.yaml").rename(d / "A2.yaml")
    assert provenance._content_digest(list(d.glob("*.yaml")), root=d) != before


def test_ruler_fingerprint_sees_an_equal_length_scenario_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8 through the REAL entry point, not the helper: a card edited without
    changing its length must move the `scenarios` part and nothing else.

    Against a COPY of the corpus (the repo's own cards are never touched), which
    is also the only way to make the edit and still be able to run the suite.
    """
    import shutil

    scen = tmp_path / "scenarios"
    shutil.copytree(provenance.SCENARIOS_DIR, scen)
    monkeypatch.setattr(provenance, "SCENARIOS_DIR", scen)
    before = ruler_fingerprint()

    victim = scen / "C2-show.yaml"
    text = victim.read_text(encoding="utf-8")
    # Flip one character, keeping the byte count identical.
    edited = text.replace("expected_end_state", "expected_end_stateX", 1)[: len(text)]
    assert len(edited) == len(text)
    assert edited != text
    victim.write_text(edited, encoding="utf-8")

    after = ruler_fingerprint()
    assert after["scenarios"] != before["scenarios"]
    assert after["harness"] == before["harness"]  # only the part that moved moves
    assert after["litman"] == before["litman"]


def test_ruler_fingerprint_sees_a_harness_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8's other half: editing the scoring chain is editing the ruler."""
    import shutil

    harn = tmp_path / "harness"
    shutil.copytree(provenance.HARNESS_DIR, harn, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(provenance, "HARNESS_DIR", harn)
    before = ruler_fingerprint()

    victim = harn / "checker.py"
    victim.write_text(
        victim.read_text(encoding="utf-8") + "\n# a reviewer's harmless tweak\n",
        encoding="utf-8",
    )

    after = ruler_fingerprint()
    assert after["harness"] != before["harness"]
    assert after["scenarios"] == before["scenarios"]


def test_harness_digest_covers_the_adapters(tmp_path: Path) -> None:
    """The scoring chain runs through harness/agents/*.py — an adapter's parse()
    is what turns stdout into the evidence the checker scores."""
    fp_files = list(provenance.HARNESS_DIR.rglob("*.py"))
    names = {p.name for p in fp_files}
    assert {"checker.py", "batch.py", "provenance.py"} <= names
    assert any(p.parent.name == "agents" for p in fp_files)


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------


def _session(**over) -> dict:
    base = {
        "started_at": "2026-07-17T10:00:00",
        "agent": "cursor",
        "dry": False,
        "model_requested": "composer-2.5",
        "model_served": "Composer 2.5",
        "rounds": 3,
        "cards": ["A1-init", "A2-add-clean"],
        "ruler": {"litman": "aaa", "scenarios": "bbb", "harness": "ccc"},
    }
    base.update(over)
    return session_record(**base)


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    j = tmp_path / JOURNAL_NAME
    append_record(j, _session())
    append_record(j, card_record(CardScore("A1-init", "auto-scored", [1, 1, 1], 1.0)))
    records = read_records(j)
    assert [r["type"] for r in records] == ["session", "card"]
    assert records[0]["model_served"] == "Composer 2.5"
    assert records[1]["card_id"] == "A1-init"


def test_read_records_of_a_missing_journal_is_empty(tmp_path: Path) -> None:
    assert read_records(tmp_path / "nope.jsonl") == []


def test_a_truncated_final_line_does_not_lose_the_rest(tmp_path: Path) -> None:
    """SIGKILL mid-write costs the last card, not the other 29."""
    j = tmp_path / JOURNAL_NAME
    append_record(j, _session())
    append_record(j, card_record(CardScore("A1-init", "auto-scored", [1], 1.0)))
    with j.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "card", "card_id": "A2-add-cl')  # killed here
    records = read_records(j)
    assert [r["type"] for r in records] == ["session", "card"]


def test_score_roundtrips_through_a_record() -> None:
    score = CardScore(
        "A1-init", "auto-scored", [1, 0, 1], 2 / 3,
        usage={"input_tokens": 5, "spawns": 3},
        error=None,
    )
    assert score_from_record(card_record(score)) == score


def test_error_survives_the_record_roundtrip() -> None:
    err = {"reason": "exit", "exit_code": 1, "timed_out": False}
    score = CardScore("A1-init", "auto-scored", [], 0.0, error=err)
    assert score_from_record(card_record(score)).error == err


def test_card_record_is_json_serializable(tmp_path: Path) -> None:
    j = tmp_path / JOURNAL_NAME
    append_record(j, card_record(CardScore("A1", "auto-scored", [1], 1.0)))
    json.loads(j.read_text(encoding="utf-8").strip())


def test_baseline_session_is_the_first_one_not_the_last(tmp_path: Path) -> None:
    """Later sittings are checked against the ORIGINAL, so a run cannot drift one
    tolerable step at a time across five resumes."""
    j = tmp_path / JOURNAL_NAME
    append_record(j, _session(started_at="first", model_served="Composer 2.5"))
    append_record(j, _session(started_at="second", model_served="Composer 2.5"))
    assert baseline_session(read_records(j))["started_at"] == "first"


def test_baseline_session_of_an_empty_journal_is_none() -> None:
    assert baseline_session([]) is None


# ---------------------------------------------------------------------------
# D7: last record wins
# ---------------------------------------------------------------------------


def test_resumable_scores_takes_the_last_record_per_card() -> None:
    records = [
        card_record(CardScore("A1", "auto-scored", [], 0.0, error={"reason": "exit"})),
        card_record(CardScore("A1", "auto-scored", [1, 1], 1.0)),
    ]
    (score,) = resumable_scores(records)
    assert score.mean == 1.0 and score.error is None


def test_an_errored_card_is_retried_on_resume() -> None:
    """D7's payoff: no extra switch is needed to retry failures. The last record
    for a card that died carries an error, so it is simply not resumable."""
    records = [
        card_record(CardScore("A1", "auto-scored", [1, 1], 1.0)),
        card_record(CardScore("A2", "auto-scored", [], 0.0, error={"reason": "exit"})),
    ]
    assert [s.card_id for s in resumable_scores(records)] == ["A1"]


def test_a_card_that_succeeded_after_failing_is_not_retried() -> None:
    records = [
        card_record(CardScore("A1", "auto-scored", [], 0.0, error={"reason": "exit"})),
        card_record(CardScore("A1", "auto-scored", [1, 1, 1], 1.0)),
    ]
    assert [s.card_id for s in resumable_scores(records)] == ["A1"]


def test_routing_cards_are_never_resumed() -> None:
    """A routing card's contribution is its RoutingResult, which the journal does
    not carry. Restoring it from a CardScore (rounds=[], mean=0.0) would drop it
    from the RA denominator without a word — the same silent-shrink this whole
    task exists to prevent. It is re-run instead."""
    records = [
        card_record(CardScore("I-route-batch-1", "routing", [], 0.0)),
        card_record(CardScore("A1", "auto-scored", [1], 1.0)),
    ]
    assert [s.card_id for s in resumable_scores(records)] == ["A1"]


def test_skipped_and_multiturn_cards_are_not_resumed() -> None:
    """Re-derived for free (no spawn), so restoring them would buy nothing."""
    records = [
        card_record(CardScore("E3", "skipped", [], 0.0)),
        card_record(CardScore("G3", "multi-turn", [], 0.0)),
    ]
    assert resumable_scores(records) == []


# ---------------------------------------------------------------------------
# The resume gate
# ---------------------------------------------------------------------------


def test_identical_conditions_resume() -> None:
    assert check_resumable(_session(), _session()) is None


def test_started_at_may_differ() -> None:
    """A resume is by definition a different sitting; the clock is not a condition."""
    assert check_resumable(_session(started_at="a"), _session(started_at="b")) is None


def test_model_served_change_is_refused_and_named() -> None:
    """AC7 at the unit level: D1 — no --force-resume, the two halves are not one
    experiment."""
    why = check_resumable(
        _session(model_served="Composer 2.5"), _session(model_served="Composer 3")
    )
    assert why is not None
    assert "served model" in why
    assert "Composer 2.5" in why and "Composer 3" in why


def test_model_served_going_from_a_name_to_none_is_refused() -> None:
    """Only one side None: the agent's reporting changed, which is more suspicious
    than a model change, not less."""
    why = check_resumable(
        _session(model_served="Composer 2.5"), _session(model_served=None)
    )
    assert why is not None and "served model" in why


def test_agent_change_is_refused_and_named() -> None:
    why = check_resumable(_session(agent="cursor"), _session(agent="claude"))
    assert why is not None and "agent changed" in why


def test_requested_model_change_is_refused_and_named() -> None:
    why = check_resumable(
        _session(model_requested="composer-2.5"), _session(model_requested="composer-3")
    )
    assert why is not None and "requested model" in why


def test_rounds_change_is_refused_and_named() -> None:
    why = check_resumable(_session(rounds=3), _session(rounds=1))
    assert why is not None and "rounds" in why


def test_card_set_change_is_refused_and_named() -> None:
    why = check_resumable(_session(cards=["A1", "A2"]), _session(cards=["A1", "A3"]))
    assert why is not None and "card set" in why
    assert "A2" in why and "A3" in why


def test_card_ORDER_is_not_a_change() -> None:
    """The set is the condition, not the ordering."""
    assert check_resumable(_session(cards=["A1", "A2"]), _session(cards=["A2", "A1"])) is None


@pytest.mark.parametrize("part", ["litman", "scenarios", "harness"])
def test_each_ruler_part_is_refused_BY_NAME(part: str) -> None:
    """AC8/D3: naming the part is the requirement. Two hex strings and "they
    differ" leaves the reader to guess which of three things they changed."""
    ruler = {"litman": "aaa", "scenarios": "bbb", "harness": "ccc"}
    moved = dict(ruler, **{part: "MOVED"})
    why = check_resumable(_session(ruler=ruler), _session(ruler=moved))
    assert why is not None
    assert part in why
    others = {"litman", "scenarios", "harness"} - {part}
    for other in others:
        assert other not in why  # names the one that moved, not all three


# --- D2: agy ---------------------------------------------------------------


def test_agy_both_none_resumes() -> None:
    """AC9. agy reports no model. `None == None` proves nothing, but it also
    contradicts nothing — and refusing an agent for being born blind would be a
    different rule from Phase 0's (which SKIPs it, not FAILs it)."""
    assert (
        check_resumable(
            _session(agent="agy", model_served=None),
            _session(agent="agy", model_served=None),
            served_model_verifiable=False,
        )
        is None
    )


def test_agy_one_side_none_is_still_refused() -> None:
    """An agent that cannot report a model reporting one means the harness changed
    under the run, not that the agent changed its mind."""
    why = check_resumable(
        _session(agent="agy", model_served=None),
        _session(agent="agy", model_served="Something"),
        served_model_verifiable=False,
    )
    assert why is not None
    assert "harness, not the agent" in why


def test_two_dry_runs_both_report_no_model_and_resume() -> None:
    """A verifiable agent with no live Phase 0 (a dry run) also reads None on both
    sides. Equality passes it, which is right: nothing observed a model, and
    nothing contradicts anything."""
    assert (
        check_resumable(
            _session(agent="claude", model_served=None),
            _session(agent="claude", model_served=None),
        )
        is None
    )


# ---------------------------------------------------------------------------
# dry vs live: a hard-coded 0 must never be resumed into a real report
# ---------------------------------------------------------------------------


def test_a_dry_journal_cannot_be_resumed_by_a_live_run() -> None:
    """The hole this closes was REAL, and agy is where it bit.

    A dry run spawns nothing and scores every card a hard-coded 0. Its session
    record and a LIVE agy session record agree on every other field — agent,
    model_requested, rounds, cards, ruler, and model_served (agy reports none, so
    both are None). So the gate passed, the fake zeros were adopted as measured
    cards, and a non-measurement was published as agy's TRR. That is the precise
    inversion of what this module is for.
    """
    why = check_resumable(
        _session(agent="agy", model_served=None, dry=True),
        _session(agent="agy", model_served=None, dry=False),
        served_model_verifiable=False,
    )
    assert why is not None
    assert "dry" in why.lower()


def test_a_live_journal_cannot_be_resumed_by_a_dry_run() -> None:
    """The same hole, mirrored — and it destroys rather than fabricates: the dry
    sitting's fake zeros would be journaled AFTER the real cards, and last-wins
    (D7) would make them the ones that count."""
    why = check_resumable(
        _session(agent="agy", model_served=None, dry=False),
        _session(agent="agy", model_served=None, dry=True),
        served_model_verifiable=False,
    )
    assert why is not None
    assert "dry" in why.lower()


def test_dry_matching_dry_resumes() -> None:
    """The flag is a condition to hold equal, not a ban on dry runs."""
    assert check_resumable(_session(dry=True), _session(dry=True)) is None


@pytest.mark.parametrize("side", ["baseline", "current"])
def test_a_session_record_missing_its_dry_flag_is_refused_not_assumed_live(side) -> None:
    """`session_record` gives `dry` no default so a caller cannot re-open the hole
    by saying nothing; reading a missing key as "live" here would hand that straight
    back. A tampered-with DRY journal would then resume live and publish its
    hard-coded zeros — the whole thing this flag exists to stop."""
    good = _session(dry=True)
    tampered = {k: v for k, v in good.items() if k != "dry"}
    pair = (tampered, good) if side == "baseline" else (good, tampered)

    why = check_resumable(*pair)

    assert why is not None, "a record with no dry flag must never be assumed live"
    assert "'dry' flag" in why
    assert "Refusing rather than assuming" in why


def test_the_dry_refusal_is_checked_before_anything_else() -> None:
    """When a dry journal meets a live sitting that ALSO changed model, the dry
    mismatch is the one to report: it says "this was never data", which is the
    fact that makes the rest not worth discussing."""
    why = check_resumable(
        _session(dry=True, model_served="Composer 2.5"),
        _session(dry=False, model_served="Composer 3"),
    )
    assert why is not None and "dry" in why.lower()


def test_session_record_will_not_let_a_caller_omit_dry() -> None:
    """No default: a dry session that forgets to say so is indistinguishable from
    a live agy one, which is exactly how the zeros got adopted. The caller answers."""
    with pytest.raises(TypeError):
        session_record(
            started_at="t", agent="agy", model_requested="m", model_served=None,
            rounds=1, cards=["A1"], ruler={"litman": "a", "scenarios": "b", "harness": "c"},
        )
