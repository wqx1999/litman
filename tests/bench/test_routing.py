"""Deterministic tests for the routing scorer (Phase F).

No agent is spawned (M34 §3.5 hard boundary). The observed skill labels are
stubbed — exactly what :func:`harness.executor.parse_stream` would have captured
into ``ExecutorResult.skills`` from a live ``Skill`` tool_use.
"""

from __future__ import annotations

from harness.routing import RoutingResult, score_routing
from harness.scenarios import load_card, SCENARIOS_DIR


def _card(cases, in_scope=None):
    return {
        "id": "I-test",
        "layer": "routing",
        "cases": cases,
        "in_scope_skills": in_scope or ["lit-library", "lit-reading"],
    }


# ---------------------------------------------------------------------------
# Single-golden cases
# ---------------------------------------------------------------------------


def test_single_golden_hit_and_miss() -> None:
    card = _card(
        [
            {"utt": "add this", "golden": "lit-library"},
            {"utt": "search my notes", "golden": "lit-reading"},
        ]
    )
    r = score_routing(
        card, ["lit-library", "lit-library"], present_skills=["lit-library", "lit-reading"]
    )
    assert isinstance(r, RoutingResult)
    assert r.hit == 1
    assert r.miss == 1
    assert r.spurious == 1  # the wrong route picked a present-but-wrong skill
    assert r.na == 0
    assert r.ra == 0.5


def test_no_skill_invoked_is_miss_not_spurious() -> None:
    card = _card([{"utt": "add this", "golden": "lit-library"}])
    r = score_routing(card, [None], present_skills=["lit-library", "lit-reading"])
    assert r.miss == 1
    assert r.spurious == 0  # nothing was routed, so not a spurious route
    assert r.ra == 0.0


# ---------------------------------------------------------------------------
# Acceptable-set (collision) cases
# ---------------------------------------------------------------------------


def test_acceptable_set_hit_on_any_member() -> None:
    card = _card([{"utt": "restore that paper", "golden": ["lit-library", "lit-reading"]}])
    assert score_routing(card, ["lit-library"], present_skills=["lit-library", "lit-reading"]).hit == 1
    assert score_routing(card, ["lit-reading"], present_skills=["lit-library", "lit-reading"]).hit == 1


def test_acceptable_set_miss_outside_members() -> None:
    card = _card(
        [{"utt": "restore", "golden": ["lit-library", "lit-reading"]}],
        in_scope=["lit-library", "lit-reading", "ref-manager"],
    )
    r = score_routing(card, ["ref-manager"], present_skills=["lit-library", "lit-reading", "ref-manager"])
    assert r.hit == 0
    assert r.miss == 1


# ---------------------------------------------------------------------------
# N/A: golden references an absent (external) skill
# ---------------------------------------------------------------------------


def test_na_excluded_from_denominator() -> None:
    card = _card(
        [
            {"utt": "cite this", "golden": ["lit-library", "cite-retrieval?"]},
            {"utt": "add this", "golden": "lit-library"},
        ]
    )
    # cite-retrieval not present -> first case N/A, only the second is scored.
    r = score_routing(
        card, ["lit-library", "lit-library"], present_skills=["lit-library", "lit-reading"]
    )
    assert r.na == 1
    assert r.hit == 1
    assert r.miss == 0
    assert r.ra == 1.0  # denominator excludes the N/A case


def test_optional_marker_in_scope_when_present() -> None:
    card = _card(
        [{"utt": "cite this", "golden": ["lit-library", "cite-retrieval?"]}],
        in_scope=["lit-library", "lit-reading", "cite-retrieval"],
    )
    # cite-retrieval present -> case is in scope; routing to it is a hit.
    r = score_routing(
        card, ["cite-retrieval"], present_skills=["lit-library", "lit-reading", "cite-retrieval"]
    )
    assert r.na == 0
    assert r.hit == 1


# ---------------------------------------------------------------------------
# Length mismatch is a hard error (no silent skip)
# ---------------------------------------------------------------------------


def test_length_mismatch_raises() -> None:
    card = _card([{"utt": "a", "golden": "lit-library"}])
    try:
        score_routing(card, [], present_skills=["lit-library"])
    except ValueError as e:
        assert "length" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on length mismatch")


# ---------------------------------------------------------------------------
# Against the real corpus cards
# ---------------------------------------------------------------------------


def test_real_routing_card_all_correct() -> None:
    """Score I-route-batch-1 with a perfect oracle (every case routed to a
    golden / acceptable member); N/A cases excluded, RA == 1.0."""
    card = load_card(SCENARIOS_DIR / "I-route-batch-1.yaml")
    present = set(card.in_scope_skills)
    observed: list[str | None] = []
    for case in card.cases:
        golden = case["golden"]
        observed.append(golden[0] if isinstance(golden, list) else golden)
    r = score_routing(card, observed, present_skills=present)
    assert r.miss == 0
    assert r.ra == 1.0
