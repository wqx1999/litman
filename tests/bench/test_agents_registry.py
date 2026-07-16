"""Tests for the adapter registry, the capability sheet and model normalization.

NEVER spawns anything (M34 §3.5 hard boundary).
"""

from __future__ import annotations

import pytest

from harness.agents import (
    AGENT_NAMES,
    NOT_MEASURABLE,
    family_of,
    get_adapter,
    known_model_strings,
    model_family,
)


def test_every_named_agent_resolves() -> None:
    for name in AGENT_NAMES:
        assert get_adapter(name).name == name


def test_unknown_agent_names_the_known_ones() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        get_adapter("copilot")


def test_each_agent_carries_its_own_default_model() -> None:
    """A shared default would silently serve a different model per agent: the
    three CLIs do not share a model namespace at all."""
    defaults = {name: get_adapter(name).default_model for name in AGENT_NAMES}
    assert defaults["claude"] == "claude-sonnet-4-6"
    assert defaults["cursor"] == "claude-sonnet-4-6"
    assert defaults["agy"] == "Claude Sonnet 4.6 (Thinking)"


def test_every_agent_publishes_its_permission_flags() -> None:
    """The report records how a run was authorized from OUR side of the boundary:
    cursor's own stream reports permissionMode "default" while --force is live."""
    assert get_adapter("claude").permission_flags == ("--permission-mode", "bypassPermissions")
    assert get_adapter("cursor").permission_flags == ("--force",)
    assert get_adapter("agy").permission_flags == ("--dangerously-skip-permissions",)


def test_each_agent_installs_skills_where_that_agent_looks() -> None:
    from pathlib import Path

    base = Path("/tmp/bench-x")
    assert get_adapter("claude").skills_dir(base) == base / "claude-config" / "skills"
    # cursor: the process CWD — with HOME redirected it reads no HOME-level
    # skills dir at all (measured), so delivery goes through neutral_cwd_for.
    assert get_adapter("cursor").skills_dir(base) == base / "cwd" / ".claude" / "skills"
    # agy reads {appDataDir}/skills only; it does NOT read ~/.agents/skills.
    assert get_adapter("agy").skills_dir(base) == (
        base / "home" / ".gemini" / "antigravity-cli" / "skills"
    )


# ---------------------------------------------------------------------------
# model_family: an explicit table, never a regex guess
# ---------------------------------------------------------------------------


def test_served_display_name_maps_to_the_same_family_as_the_requested_id() -> None:
    assert model_family(
        "Sonnet 4.6 200K Medium No Thinking", "claude-sonnet-4-6",
        fallback_to_requested=False,
    ) == "claude-sonnet-4.6"


def test_falls_back_to_requested_only_when_nothing_was_served() -> None:
    """agy reports no model, so the family can only come from what we asked for —
    and the report says model_served is None so nobody mistakes it for verified."""
    assert model_family(
        None, "Claude Sonnet 4.6 (Thinking)", fallback_to_requested=True
    ) == "claude-sonnet-4.6"


def test_an_unrecognized_served_model_never_falls_back_to_the_request() -> None:
    """The exact case where trusting the request would MASK a mismatch: the agent
    told us something we do not recognize, so the honest answer is 'unknown'."""
    assert model_family(
        "Gemini 3 Pro", "claude-sonnet-4-6", fallback_to_requested=True
    ) is None


def test_unknown_strings_are_none_not_guessed() -> None:
    assert model_family("sonnet-4-6-turbo-max", None, fallback_to_requested=True) is None
    assert model_family(None, None, fallback_to_requested=True) is None


def test_thinking_and_no_thinking_are_separate_table_entries() -> None:
    """They DO share a family (same weights) — but only because a human decided so,
    entry by entry. A regex over "sonnet 4.6" would fold them, and every future
    string too, hiding exactly the differences the reader needs (thinking on vs
    off, context window, tier). Both raw strings are reported verbatim."""
    known = known_model_strings()
    assert "Sonnet 4.6 200K Medium No Thinking" in known
    assert "Claude Sonnet 4.6 (Thinking)" in known


# ---------------------------------------------------------------------------
# The NOT_MEASURABLE sentinel
# ---------------------------------------------------------------------------


def test_sentinel_is_distinct_from_none_and_from_falsiness() -> None:
    """A falsy sentinel would slip straight through the `if not observed:` /
    `is None` branches it exists to be distinguished from."""
    assert NOT_MEASURABLE is not None
    assert bool(NOT_MEASURABLE) is True
    assert NOT_MEASURABLE != None  # noqa: E711 - the point is the comparison


# ---------------------------------------------------------------------------
# The request-fallback must not fire for an agent that DOES report its model
# ---------------------------------------------------------------------------


def test_fallback_is_gated_on_the_agent_actually_reporting_nothing() -> None:
    """`served is None` means two different things.

    For agy it means "this agent never reports a model" -> the request is all we
    have, and the report says model_served is None so nobody mistakes it for
    verified. For claude/cursor it means "nothing observed one" — a dry run, a
    routing-only run (only execution rounds harvest it), or spawns that all died
    before reporting. Naming a family from the request there dresses a gap up as
    knowledge.
    """
    # agy: reports nothing, so the request is the only source there is.
    assert model_family(None, "Claude Sonnet 4.6 (Thinking)", fallback_to_requested=True) == (
        "claude-sonnet-4.6"
    )
    # claude/cursor: nothing was observed -> we do not know. Say so.
    assert model_family(None, "claude-sonnet-4-6", fallback_to_requested=False) is None


def test_family_of_is_a_pure_single_string_lookup() -> None:
    assert family_of("claude-sonnet-4-6") == "claude-sonnet-4.6"
    assert family_of("deepseek-v4-pro") is None  # an external model: simply unknown
    assert family_of(None) is None


def test_an_unknown_family_is_a_reporting_gap_not_a_verdict() -> None:
    """The table only exists to group runs across scaffolds. An external model
    routed through a proxy will never be in it, and that must cost it nothing."""
    assert family_of("deepseek-v4-pro") is None
    assert model_family("deepseek-v4-pro", "deepseek-v4-pro", fallback_to_requested=False) is None
