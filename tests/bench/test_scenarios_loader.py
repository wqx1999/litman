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

# The full corpus is 33 cards; 7 carry an explicit skip_reason (6 needs_network
# + 1 needs_pty), the rest are sandbox-runnable definitions. E2/H1 join the
# needs_network set: both preconditions require a bound code repo (a `code add`
# = git clone), which the sandbox seed builder cannot stage — leaving them
# auto-scored produced guaranteed-0 false negatives for every model.
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
