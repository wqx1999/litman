"""Tests for the adapter registry, the capability sheet and model normalization.

NEVER spawns anything (M34 §3.5 hard boundary).
"""

from __future__ import annotations

from pathlib import Path

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
# The isolation seam, as a property of the SET of agents (never spawns)
# ---------------------------------------------------------------------------


def _isolate_the_world(tmp_path, monkeypatch) -> Path:
    """Point every adapter's "real home" at a fake one and stub the side effects.

    Covers every REGISTERED agent (``AGENT_NAMES``, not a hand-listed three): skill
    installs become no-ops, agy's login token is fabricated (its seed_auth RAISES
    when absent, by design) and its ``lit`` is a stub the shim writer can freeze.
    Nothing here may read or write the maintainer's actual home.

    Faking ``$HOME`` is only half of that; the other half is conftest's autouse
    ``_no_real_credential_dirs``. claude resolves its credential source from
    ``$CLAUDE_CONFIG_DIR`` FIRST, so with that var exported this helper would hand
    ``prepare()`` the developer's real config dir and quietly copy a live
    credential into ``tmp_path`` — with every assertion below still green.
    """
    user_home = tmp_path / "userhome"
    (user_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user_home))

    # Assumes each registered name is also its module name (true today; a
    # divergence raises here rather than silently skipping a stub).
    for mod in AGENT_NAMES:
        monkeypatch.setattr(
            f"harness.agents.{mod}.install_repo_skills", lambda d, **k: None
        )
    # agy: fabricate the login token (public repo — never a real one).
    from harness.agents.agy import TOKEN_RELPATH

    token = user_home / TOKEN_RELPATH
    token.parent.mkdir(parents=True)
    token.write_text('{"fake": "not-a-real-token"}', encoding="utf-8")
    lit = tmp_path / "fake-lit"
    lit.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    lit.chmod(0o755)
    monkeypatch.setattr("harness.agents.agy.LIT_BIN", lit)
    return user_home


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_no_agent_can_see_the_users_real_home(agent, tmp_path, monkeypatch) -> None:
    """The one isolation property every agent must share, asserted as a set.

    claude was the outlier until the ruler audit: it isolated CLAUDE_CONFIG_DIR and
    inherited HOME, so a `lit health-check` that the AGENT ran resolved installed
    skills through the MAINTAINER's real `~/.claude/skills` (the check reads
    `Path.home()`, and the subprocess inherited that home) and told the agent its
    library was not clean — a finding about the laptop, landing in the agent's
    answer. Parametrized over AGENT_NAMES rather than a hand-listed three, so a
    fourth adapter cannot join the registry without answering this.

    Two things this does NOT claim, both measured:

    * that the redirect changes how many skills claude LOADS — it does not (18
      either way; 16 are built into the claude CLI and CLAUDE_CONFIG_DIR was
      already hiding the user's own);
    * that it changes any CARD's verdict — it does not. The `health: clean` oracle
      runs in the HARNESS process, whose home this never touches.

    The property under test is that the child cannot reach the real home. Nothing
    more.
    """
    user_home = _isolate_the_world(tmp_path, monkeypatch)
    base = tmp_path / "base"
    base.mkdir()

    env = get_adapter(agent).prepare(base, run_vault=base / "vault")

    assert env["HOME"] == str(base / "home"), f"{agent} does not redirect HOME"
    assert env["HOME"] != str(user_home)
    # A set XDG_CONFIG_HOME names the real ~/.config absolutely: it survives the
    # HOME change and re-opens the seam. Nobody may leave it in the child env.
    assert "XDG_CONFIG_HOME" not in env
    # And the redirect must not have been achieved by pointing at something that
    # does not exist — the agent has to be able to write into its own home.
    assert Path(env["HOME"]).is_dir()


@pytest.mark.parametrize("agent", AGENT_NAMES)
def test_every_agent_redirects_the_registry_and_the_vault(
    agent, tmp_path, monkeypatch
) -> None:
    """The safety half of the seam (M34 §4): the real registry is unreachable and
    a bare ``lit`` lands in the disposable run vault, never the user's library."""
    _isolate_the_world(tmp_path, monkeypatch)
    base = tmp_path / "base"
    base.mkdir()

    env = get_adapter(agent).prepare(base, run_vault=base / "vault")

    assert env["LIT_LIBRARY"] == str(base / "vault")
    assert Path(env["LITMAN_REGISTRY_DIR"]).parent == base


# ---------------------------------------------------------------------------
# model_family: an explicit table, never a regex guess
# ---------------------------------------------------------------------------


def test_served_display_name_maps_to_the_same_family_as_the_requested_id() -> None:
    assert model_family(
        "Sonnet 4.6 200K Medium No Thinking", "claude-sonnet-4-6",
        fallback_to_requested=False,
    ) == "claude-sonnet-4.6"


def test_cursors_relabeled_suffixless_display_name_maps_to_the_same_family() -> None:
    """Cursor dropped the "No Thinking" suffix from non-thinking display names
    (its model list now marks thinking explicitly and leaves non-thinking
    unmarked). Both spellings are table entries; neither is a regex guess."""
    assert model_family(
        "Sonnet 4.6 200K Medium", "claude-sonnet-4-6",
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
