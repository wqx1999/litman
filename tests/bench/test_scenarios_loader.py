"""Deterministic tests for the scenario loader + handoff discipline (Phase D)."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.scenarios import (
    SCENARIOS_DIR,
    WITHHELD_FIELDS,
    Card,
    Handoff,
    executor_view,
    load_all_cards,
    load_card,
)

# The full corpus is 33 cards; 8 carry an explicit skip_reason (7 needs_network
# + 1 needs_pty) — the sandbox physically cannot run those. E2/H1 join the
# needs_network set: both preconditions require a bound code repo (a `code add`
# = git clone), which the sandbox seed builder cannot stage — leaving them
# auto-scored produced guaranteed-0 false negatives for every model.
# A SEPARATE class is `single_turn_unfit` (the ``multi-turn`` coverage bucket):
# the card runs fine but cannot be FAIRLY scored single-turn (an intrinsically
# multi-turn interaction). It is NOT a skip_reason — different exclusion reason
# (methodology, not sandbox limits) — so it lives in its own expected set below.
EXPECTED_CARD_COUNT = 33
EXPECTED_SKIP_IDS = {
    "E1-code-add",
    "E2-code-rm",
    "H1-inject-dangling-clone",
    "J1-read-compare-link-clone",
    "J1-corrupt",
    "J2-amp-survey",
    "D2-pty-taxonomy-rm",
    "G5-sync-push",  # needs harness-configured fake rclone remote + out-of-vault content verb (infra unbuilt)
}
EXPECTED_MULTITURN_IDS = {
    "G3-trash-restore",  # 'delete it… wait no, restore it' — one-breath retraction, unfair single-turn
}


def _all_cards() -> list[Card]:
    return load_all_cards()


def test_loads_full_corpus() -> None:
    cards = _all_cards()
    assert len(cards) == EXPECTED_CARD_COUNT
    # ids are unique
    ids = [c.id for c in cards]
    assert len(set(ids)) == len(ids)


def test_skip_cards_marked() -> None:
    cards = _all_cards()
    skipped = {c.id for c in cards if c.skip_reason}
    assert skipped == EXPECTED_SKIP_IDS
    for c in cards:
        if c.skip_reason:
            assert c.skip_reason.strip(), f"{c.id} has empty skip_reason"
            assert c.needs_network or c.needs_pty, (
                f"{c.id} carries skip_reason but is neither network nor pty"
            )


def test_non_skip_cards_are_sandbox_runnable() -> None:
    """A card without a skip_reason must not need network or a pty."""
    for c in _all_cards():
        if c.skip_reason is None:
            assert not c.needs_network, f"{c.id} needs_network but no skip_reason"
            assert not c.needs_pty, f"{c.id} needs_pty but no skip_reason"


def test_multiturn_cards_marked() -> None:
    """``single_turn_unfit`` cards form their own set, disjoint from skip_reason.

    A multi-turn-excluded card runs fine in the sandbox (no network / pty), so it
    must carry NO skip_reason — the two exclusion classes never overlap.
    """
    cards = _all_cards()
    multiturn = {c.id for c in cards if c.single_turn_unfit}
    assert multiturn == EXPECTED_MULTITURN_IDS
    by_id = {c.id: c for c in cards}
    for cid in EXPECTED_MULTITURN_IDS:
        c = by_id[cid]
        assert c.single_turn_unfit.strip(), f"{cid} has empty single_turn_unfit"
        assert c.skip_reason is None, f"{cid} is both skip_reason and single_turn_unfit"
        assert not c.needs_network and not c.needs_pty, f"{cid} multi-turn but needs sandbox cap"
    assert not (EXPECTED_MULTITURN_IDS & EXPECTED_SKIP_IDS), "exclusion sets must be disjoint"


def test_every_non_routing_card_has_intent() -> None:
    for c in _all_cards():
        if not c.is_routing:
            assert c.intent.strip(), f"{c.id} has no intent"


def test_routing_cards_have_cases() -> None:
    routing = [c for c in _all_cards() if c.is_routing]
    assert {c.id for c in routing} == {"I-route-batch-1", "I-route-batch-2"}
    for c in routing:
        assert c.cases, f"{c.id} has no cases"
        for case in c.cases:
            assert "utt" in case and "golden" in case


def test_executor_view_only_exposes_intent_and_fixtures() -> None:
    """Handoff discipline: the executor view withholds every answer field."""
    handoff = executor_view(load_card(SCENARIOS_DIR / "A2-add-clean.yaml"))
    assert isinstance(handoff, Handoff)
    # Handoff is a frozen dataclass with exactly two fields.
    field_names = set(handoff.__dataclass_fields__)
    assert field_names == {"intent", "fixtures"}
    # None of the withheld fields leaked in as attributes.
    for withheld in WITHHELD_FIELDS:
        assert not hasattr(handoff, withheld)


def test_executor_view_intent_is_verbatim() -> None:
    card = load_card(SCENARIOS_DIR / "A2-add-clean.yaml")
    handoff = executor_view(card)
    assert handoff.intent == card.intent
    assert "PeptideBERT" in handoff.intent


def test_executor_view_resolves_fixture_paths() -> None:
    card = load_card(SCENARIOS_DIR / "A2-add-clean.yaml")
    handoff = executor_view(card)
    assert card.fixtures == [4]
    assert len(handoff.fixtures) == 1
    assert handoff.fixtures[0].name == "4.pdf"


def test_no_card_stages_an_already_seeded_paper() -> None:
    """Handoff discipline (Q2): a card may only stage fixtures the agent does NOT
    already have in its seed vault.

    Staging an already-seeded paper is the 'staged-PDF trap' — the agent is handed
    a PDF that is already in the vault, so it may re-``lit add`` it (creating a
    duplicate) instead of operating on the in-vault copy, polluting the end state.
    Vault-operation cards must therefore carry ``fixtures: []``. A3-add-dup is the
    sole sanctioned exception (it deliberately re-hands a seeded PDF to test that
    ``lit add`` rejects the duplicate)."""
    from harness.seeds import SEED_SPECS

    DUP_EXEMPT = {"A3-add-dup"}
    for c in _all_cards():
        if c.is_routing or c.skip_reason or not c.fixtures or not c.seed:
            continue
        if c.id in DUP_EXEMPT:
            continue
        spec = SEED_SPECS.get(c.seed)
        seeded: set[int] = set()
        if spec:
            for s in spec.steps:
                if s.fixture is not None:
                    seeded.add(s.fixture)
                if s.fixture_b is not None:
                    seeded.add(s.fixture_b)
        trap = set(c.fixtures) & seeded
        assert not trap, (
            f"{c.id} stages already-seeded fixture(s) {sorted(trap)} from seed "
            f"{c.seed!r} (staged-PDF trap — set fixtures: [] for a vault-op card, "
            f"or add to DUP_EXEMPT if it deliberately tests dup rejection)"
        )


def test_every_card_names_a_seed_that_exists() -> None:
    """A card's ``seed`` is resolved at RUN time, so a typo here is a live-run
    crash (or worse, a silently wrong precondition) rather than a red test. The
    corpus and the seed set are edited in different files by different changes —
    this is the only thing tying them together."""
    from harness.seeds import SEED_SPECS

    for c in _all_cards():
        if c.seed is None:
            continue
        assert c.seed in SEED_SPECS, (
            f"{c.id} names seed {c.seed!r}, which is not in SEED_SPECS "
            f"(known: {sorted(SEED_SPECS)})"
        )


def test_c2_asks_a_question_only_show_can_answer() -> None:
    """C2's whole point, pinned as a card-level property.

    This card scores `ran: show`, which is honest ONLY while the question cannot
    be answered any other way. Its original question (author + year) stopped
    meeting that bar when ADR-022 put `authors` into the INDEX projection that
    `lit list --format json` re-uses, and nobody noticed until three agents
    answered correctly and all scored 0. The exit surface is enforced against the
    live CLI in test_seeds.py; this test enforces that the CARD still rests on it.
    """
    from litman.core.views import INDEX_PAPER_FIELDS

    card = {c.id: c for c in _all_cards()}["C2-show"]
    assert card.seed == "seed-2papers-peptide-revisited"
    end = " ".join(str(a) for a in card.expected_end_state)
    assert "ran: show" in end
    # The value the seed pins, not "today" — a re-run tomorrow scores the same.
    assert "2026-06-15" in end
    # The regression guard with teeth, read off the product's own projection
    # constant: nothing this card asks for may be a field `lit list` hands over
    # for free. `lit list --format json` re-uses INDEX_PAPER_FIELDS verbatim
    # (views.project_paper), so this IS the list schema, not a copy of it.
    assert "last-revisited" not in INDEX_PAPER_FIELDS


def test_c2_proves_retrieval_through_stdout_not_through_the_prose() -> None:
    """The assertion shape is load-bearing, so it is pinned rather than left to
    whoever edits the card next.

    `stdout_contains` greps what the agent's TOOLS returned and never reads
    `final_text`, so no rendering choice by the model can intervene. That is the
    whole point, and it is the property `answer_contains` lacks: the latter greps
    the prose through `_norm` (whitespace + casefold, NO date normalization), which
    measured a 33% false-negative rate on exactly this value — n=3 -> [1,0,1], and
    the 0 had PERFECT tool choice and answered "2026 年 6 月 15 日".

    Not claimed here: that `stdout_contains` proves the value came from `lit`. Its
    fallback joins every tool_result, so a `cat` of metadata.yaml satisfies it
    (measured, r2) — which is why the card pairs it with `ran: show` and why that
    pairing is pinned by its own test above. See the verb's docstring.

    So the card must NOT drift back to answer_contains, and must not grow a weak
    `answer_contains: ~2026` either — `read-date` (2026-05-01) sits in the same
    file, so that would pass an agent reporting the wrong field.
    """
    card = {c.id: c for c in _all_cards()}["C2-show"]
    end = [str(a) for a in card.expected_end_state]
    assert any(a.startswith("stdout_contains:") for a in end)
    assert not any(a.startswith("answer_contains:") for a in end), (
        "C2 asserts retrieval through tool output, never the prose (see its notes); "
        "answer_contains on a date measured [1,0,1] with the 0 being a correct "
        "answer rendered as 2026 年 6 月 15 日"
    )


def test_c2_guards_the_value_it_reads_against_a_stray_revisit_stamp() -> None:
    """An agent that stamps a revisit turns last-revisited into today, and the card then
    fails with no signal about why — this guard makes the failure name its cause. The
    precedent is B2-revisit's own read-date-unchanged assertion. Cheap paranoia on a
    pure-read card needs no stronger justification than that.

    ⚠️ 2026-07-17: this docstring used to claim 回看 "is `lit revisit`'s own trigger
    vocabulary in lit-reading". That is FALSE — `grep -r 回看 src/litman/` returns zero.
    revisit's documented triggers (lit-reading/SKILL.md:3, :146, :306) are 又把X翻出来想了想
    / 重新看了下X / re-opened X / looking at X again. 回看 is semantically adjacent, nothing
    more, and the card's own ⑤ measurement shows haiku read it as 浏览历史 instead. The false
    claim shipped in 6682bf1 and I restored it once during rework before re-grepping — the
    very failure this spec exists to stop: an assertion about the product, never checked
    against the product.
    """
    card = {c.id: c for c in _all_cards()}["C2-show"]
    end = " ".join(str(a) for a in card.expected_end_state)
    assert "yaml_eq" in end and "last-revisited == 2026-06-15" in end


def test_c2_intent_still_asks_for_the_field_only_show_can_answer() -> None:
    """wangq's signed decision (2026-07-16): C2's intent asks an OUT-OF-PROJECTION
    field. The test above pins the assertion side; this pins the intent side, because
    ① can be reopened from either.

    Why the intent side is load-bearing, measured 2026-07-17. An intent that asks
    generically ("详细信息给我看看") scores well — haiku [1, 1, 1] — and is broken:
    `lit list --title X --format json` hands over the 14-field projection, which IS a
    reasonable "详细信息". An agent taking that path answers ADEQUATELY and fails both
    assertions → false negative. That is ADR-022's disease (three agents right, all 0),
    reintroduced from the intent instead of the projection.

    Keeping 回看 in the intent is what makes the `list` path *wrong* rather than merely
    incomplete. Live proof, from the "上次回看的日期记录是什么？" probe, round 1:

        lit calls = [list]  ->  answered "上次回看日期是 2026-05-01"

    which is `read-date`, not `last-revisited` (2026-06-15). Same tool path; the intent
    alone decides whether scoring it 0 is a true negative or a lie. (That agent is the
    exact one the card's notes predicted: "把 read-date 当成回看日期报出来".)

    So: 回看 must stay in the intent. If a future edit needs to drop it, the card needs a
    different out-of-projection field — not a generic question — and that is a change to a
    signed decision, i.e. wangq's call, not an editor's.
    """
    card = {c.id: c for c in _all_cards()}["C2-show"]
    assert "回看" in card.intent, (
        "C2's intent must ask for last-revisited (wangq's signed decision: 问投影外字段). "
        "A generic 'show me the details' lets `lit list` answer adequately while both "
        "assertions fail — the ADR-022 false negative, reopened from the intent side"
    )


def test_c2_intent_names_the_library() -> None:
    """Weaker than the test above, and honest about it: naming the domain is neither
    necessary (C3-search-notes says 笔记, never 库, and passes) nor sufficient (G1 names
    库 and still drew "你说的库是指什么？" 4/4). It is pinned for C2 only, and only
    because C2 was measured breaking without it.

    Until 2026-07-17 this card was the only retrieval card naming no domain (C1 "把我库里…",
    C4 "我库里还有没有…", G1 "检查一下我的库…"). It scored haiku [0, 0] by REFUSING to try —
    "我没有直接的权限访问你的文献库来查询 PeptideBERT 论文的浏览历史" — in a round that named
    the lit-library skill in its own prose. The skill was found; the question just did not
    read as answerable.

    Do NOT read the fix as "add 库". Measured, adding it alone: [0, 0] -> [0, 1, 0]. The
    word 回看 standing alone reads as access history, and that had to go too — the shipping
    intent frames it as a stored record ("完整记录调出来看看——我想知道上次回看是哪天"),
    which is why both halves are pinned separately.

    The failure this prevents is the nasty kind: NOT a false negative — the agent really did
    skip `show`, so 0 was "correct". The card was simply measuring a different capability
    (does the agent guess a library exists) from the one it claims. Invisible in the score.
    """
    card = {c.id: c for c in _all_cards()}["C2-show"]
    assert "库" in card.intent, (
        "C2's intent must name the library; measured without it: haiku [0, 0], the agent "
        "refusing to try. Necessary but NOT sufficient — see this test's docstring"
    )


def test_load_card_rejects_missing_id(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("title: no id here\nintent: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing a non-empty 'id'"):
        load_card(bad)


def test_load_card_rejects_non_routing_without_intent(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: X1\nlayer: front-door\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'intent'"):
        load_card(bad)


def test_every_card_yaml_parses() -> None:
    """All shipped scenario files load without error (no malformed YAML)."""
    files = sorted(SCENARIOS_DIR.glob("*.yaml"))
    assert len(files) == EXPECTED_CARD_COUNT
    for f in files:
        card = load_card(f)
        assert card.id


@pytest.mark.parametrize(
    "card_id",
    sorted(EXPECTED_SKIP_IDS),
)
def test_skip_cards_carry_reason(card_id: str) -> None:
    """Each non-sandbox card is yielded (not dropped) but flagged for CI skip."""
    cards = {c.id: c for c in _all_cards()}
    assert card_id in cards
    assert cards[card_id].skip_reason
