"""Tests for the Phase 0 instrument-qualification gate.

The gate exists because every one of its checks has a failure mode that looks like
a RESULT rather than a fault: an unauthorized tool reads as a 0% execution rate, a
leaked skill copy reads as good routing, an unpinned model reads as whatever
`auto` picked. So these tests mostly assert that a broken instrument FAILS —
loudly, before any card runs.

``run_card_impl`` / ``version_impl`` are injected, so nothing here spawns an agent
(M34 §3.5 hard boundary).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.executor import ExecutorResult, LitCall, ToolResult
from harness.qualify import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    format_qualification,
    qualification_to_dict,
    qualify,
)


def _ok_version(_binary: str) -> int:
    return 0


def _good_probe(*, model_served: str | None, usage: dict | None = None) -> ExecutorResult:
    """What a healthy `lit --version` probe looks like."""
    return ExecutorResult(
        lit_calls=[LitCall(argv=["--version"], raw="lit --version", tool_use_id="t1")],
        tool_results=[ToolResult(tool="shell", content="lit, version 1.2.0\n", tool_use_id="t1")],
        final_text="lit, version 1.2.0",
        usage=usage if usage is not None else {"input_tokens": 4, "output_tokens": 86},
        model_served=model_served,
    )


def _impl(probe: ExecutorResult, *, sentinel_echo: bool = True, seen: list | None = None):
    """A run_card_impl double: probe run first, sentinel run second.

    The sentinel probe genuinely exercises the ``on_prepared`` hook, so the hook's
    contract (plant into the adapter's own skills dir) is covered too. ``seen``
    collects each call's pass-through kwargs so the auth threading can be pinned.
    """
    seen = seen if seen is not None else []

    def run_card_impl(card, run_vault, *, agent, model, on_prepared=None, **kw: Any):
        seen.append(kw)
        if on_prepared is None:
            return probe
        # Sentinel probe: run the real hook against a faked-out skills tree.
        base = Path(run_vault).parent
        from harness.agents import get_adapter

        skill = get_adapter(agent).skills_dir(base) / "lit-library" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("---\nname: lit-library\n---\nbody\n", encoding="utf-8")
        on_prepared(base, {})
        planted = skill.read_text(encoding="utf-8").rsplit("bench-sentinel: ", 1)[-1].strip()
        return ExecutorResult(final_text=planted if sentinel_echo else "I could not find it.")

    return run_card_impl


def _by_name(qual, name: str):
    return next(c for c in qual.checks if c.name == name)


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


def test_claude_fully_qualifies(tmp_path: Path) -> None:
    qual = qualify(
        "claude",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served="claude-sonnet-4-6")),
        version_impl=_ok_version,
    )
    assert qual.ok
    assert {c.name for c in qual.checks} == {
        "binary", "headless", "tool_authorization", "evidence_chain",
        "model_pinned", "tokens", "skill_source",
    }
    assert all(c.status == STATUS_PASS for c in qual.checks)


def test_cursor_display_name_pins_against_the_requested_id(tmp_path: Path) -> None:
    """served != requested as STRINGS; the family table is what makes them match."""
    qual = qualify(
        "cursor",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served="Sonnet 4.6 200K Medium No Thinking")),
        version_impl=_ok_version,
    )
    assert qual.ok
    assert _by_name(qual, "model_pinned").status == STATUS_PASS
    assert qual.model_served == "Sonnet 4.6 200K Medium No Thinking"


def test_agy_qualifies_with_its_unmeasurable_axes_skipped_and_said_out_loud(
    tmp_path: Path,
) -> None:
    """agy reports no model and no counters. Those checks skip — but the skip is
    written down: a silent skip is how a run gets served by the wrong model and
    nobody ever finds out."""
    qual = qualify(
        "agy",
        model="Claude Sonnet 4.6 (Thinking)",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served=None, usage={})),
        version_impl=_ok_version,
    )
    assert qual.ok  # skips do not gate
    model_check = _by_name(qual, "model_pinned")
    assert model_check.status == STATUS_SKIP
    assert "UNVERIFIED" in model_check.detail
    tokens_check = _by_name(qual, "tokens")
    assert tokens_check.status == STATUS_SKIP
    assert "not 0" in tokens_check.detail


# ---------------------------------------------------------------------------
# Every check gates
# ---------------------------------------------------------------------------


def test_missing_binary_fails_and_stops_early(tmp_path: Path) -> None:
    def missing(_binary: str) -> int:
        raise OSError("No such file or directory")

    qual = qualify("cursor", work_root=tmp_path, version_impl=missing)
    assert not qual.ok
    assert _by_name(qual, "binary").status == STATUS_FAIL
    # No point attempting the rest; the user would get a wall of the same error.
    assert len(qual.checks) == 1


def test_binary_nonzero_exit_fails(tmp_path: Path) -> None:
    qual = qualify("cursor", work_root=tmp_path, version_impl=lambda _b: 127)
    assert not qual.ok
    assert "exited 127" in _by_name(qual, "binary").detail


def test_blocked_tool_approval_fails_instead_of_scoring_zero(tmp_path: Path) -> None:
    """The agent answered, but never got `lit, version` back. Without this gate the
    whole suite would score 0 and the report would blame litman."""
    denied = ExecutorResult(final_text="I was not allowed to run that command.")
    qual = qualify(
        "cursor",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(denied),
        version_impl=_ok_version,
    )
    assert not qual.ok
    assert _by_name(qual, "tool_authorization").status == STATUS_FAIL
    assert _by_name(qual, "evidence_chain").status == STATUS_FAIL


def test_empty_answer_fails_headless(tmp_path: Path) -> None:
    qual = qualify(
        "cursor",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(ExecutorResult(final_text="   ", exit_code=1)),
        version_impl=_ok_version,
    )
    assert _by_name(qual, "headless").status == STATUS_FAIL
    assert not qual.ok


def test_timeout_fails_headless(tmp_path: Path) -> None:
    qual = qualify(
        "claude",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(ExecutorResult(final_text="", timed_out=True)),
        version_impl=_ok_version,
    )
    assert "timed out" in _by_name(qual, "headless").detail


def test_evidence_chain_failure_names_the_agents_own_source(tmp_path: Path) -> None:
    """agy's evidence is the PATH shim; claude/cursor's is the event stream. A
    failure has to say which one came back empty."""
    blind = ExecutorResult(final_text="I ran lit --version: lit, version 1.2.0")
    qual = qualify(
        "agy",
        work_root=tmp_path,
        run_card_impl=_impl(blind),
        version_impl=_ok_version,
    )
    assert "lit-calls.jsonl" in _by_name(qual, "evidence_chain").detail


def test_served_model_mismatch_fails(tmp_path: Path) -> None:
    qual = qualify(
        "claude",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served="claude-haiku-4-5-20251001")),
        version_impl=_ok_version,
    )
    assert not qual.ok
    detail = _by_name(qual, "model_pinned").detail
    assert "claude-sonnet-4.6" in detail and "claude-haiku-4.5" in detail


def test_unknown_served_model_fails_rather_than_guessing(tmp_path: Path) -> None:
    """A new cursor display name must NOT be regex-matched into a family: the run
    stops and asks a human to add the entry."""
    qual = qualify(
        "cursor",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served="Sonnet 4.7 1M High Thinking")),
        version_impl=_ok_version,
    )
    assert not qual.ok
    detail = _by_name(qual, "model_pinned").detail
    assert "not in the model-family table" in detail
    assert "_MODEL_FAMILY" in detail  # tells the user exactly where to fix it


def test_missing_counters_fail_for_an_agent_that_should_have_them(tmp_path: Path) -> None:
    """A key rename upstream would silently sum to 0; this catches it up front."""
    qual = qualify(
        "cursor",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(
            _good_probe(model_served="Sonnet 4.6 200K Medium No Thinking", usage={})
        ),
        version_impl=_ok_version,
    )
    assert not qual.ok
    assert _by_name(qual, "tokens").status == STATUS_FAIL


# ---------------------------------------------------------------------------
# Check 4: the skill under test is the repo source
# ---------------------------------------------------------------------------


def test_sentinel_planted_in_the_isolated_skill_comes_back(tmp_path: Path) -> None:
    qual = qualify(
        "claude",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served="claude-sonnet-4-6")),
        version_impl=_ok_version,
    )
    assert _by_name(qual, "skill_source").status == STATUS_PASS


def test_a_lost_sentinel_means_the_agent_read_someone_elses_skill(tmp_path: Path) -> None:
    qual = qualify(
        "claude",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served="claude-sonnet-4-6"), sentinel_echo=False),
        version_impl=_ok_version,
    )
    assert not qual.ok
    assert "reading some OTHER copy" in _by_name(qual, "skill_source").detail


def test_skill_source_can_be_downgraded_to_a_warning(tmp_path: Path) -> None:
    """The documented escape hatch for an agent whose skills cannot be isolated:
    the run proceeds, but the report carries the warning rather than a silent pass."""
    qual = qualify(
        "cursor",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(
            _good_probe(model_served="Sonnet 4.6 200K Medium No Thinking"),
            sentinel_echo=False,
        ),
        version_impl=_ok_version,
        skill_source_is_fatal=False,
    )
    assert qual.ok  # a warning does not gate ...
    assert _by_name(qual, "skill_source").status == STATUS_WARN  # ... but it is recorded


def test_probe_dirs_are_cleaned_up(tmp_path: Path) -> None:
    qualify(
        "claude",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served="claude-sonnet-4-6")),
        version_impl=_ok_version,
    )
    assert list(tmp_path.glob("bench-qual-*")) == []


# ---------------------------------------------------------------------------
# The record is a deliverable, not just a gate
# ---------------------------------------------------------------------------


def test_qualification_serializes_for_the_report(tmp_path: Path) -> None:
    import json

    qual = qualify(
        "agy",
        model="Claude Sonnet 4.6 (Thinking)",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served=None, usage={})),
        version_impl=_ok_version,
    )
    payload = qualification_to_dict(qual)
    json.dumps(payload)  # must be JSON-serializable
    assert payload["agent"] == "agy"
    assert payload["model_served"] is None
    assert payload["ok"] is True
    assert {c["name"] for c in payload["checks"]} >= {"model_pinned", "tokens"}


def test_format_qualification_says_not_qualified_out_loud(tmp_path: Path) -> None:
    qual = qualify("cursor", work_root=tmp_path, version_impl=lambda _b: 1)
    text = format_qualification(qual)
    assert "NOT QUALIFIED" in text
    assert "no cards will run" in text


def test_a_probe_that_cannot_start_is_not_reported_as_a_headless_failure(
    tmp_path: Path,
) -> None:
    """agy's login token missing raises inside prepare() (seed_auth), before the
    agent is ever driven. Calling that "headless" would send the reader looking in
    the wrong place; and checks 2/3/5/6/7 all hang off this one probe, so the
    failure has to say they went unevaluated."""

    def boom(card, run_vault, **_: Any):
        raise RuntimeError(
            "agy is not logged in on this machine "
            "(/h/.gemini/antigravity-cli/antigravity-oauth-token not found).\n"
            "  Run `agy` once, interactively, and complete its login — that is "
            "the whole setup.\n"
            "  The harness never performs a login for you."
        )

    qual = qualify("agy", work_root=tmp_path, run_card_impl=boom, version_impl=_ok_version)
    assert not qual.ok
    assert {c.name for c in qual.checks} == {"binary", "probe"}
    detail = _by_name(qual, "probe").detail
    assert "Run `agy` once" in detail  # the actionable part survives
    assert "unevaluated" in detail


# ---------------------------------------------------------------------------
# External-model auth must reach the probes (or every external run dies here)
# ---------------------------------------------------------------------------


def test_proxy_auth_is_threaded_into_both_probes(tmp_path: Path) -> None:
    """The gate has to run under the SAME auth the cards will.

    Without this, an external-model run qualifies against the default Anthropic
    endpoint: it asks the user's own OAuth for a model that endpoint has never
    heard of, gets rejected, fails `headless`, and exits — killing every
    external-model run at the gate rather than running it.
    """
    seen: list[dict] = []
    qualify(
        "claude",
        model="deepseek-v4-pro",
        work_root=tmp_path,
        base_url="http://localhost:4000",
        auth_token="tok-9",
        run_card_impl=_impl(_good_probe(model_served="deepseek-v4-pro"), seen=seen),
        version_impl=_ok_version,
    )
    assert len(seen) == 2  # the lit --version probe AND the sentinel probe
    for kw in seen:
        assert kw["base_url"] == "http://localhost:4000"
        assert kw["auth_token"] == "tok-9"


def test_an_external_model_qualifies_even_though_its_family_is_unknown(
    tmp_path: Path,
) -> None:
    """A healthy external-model run: the agent echoed back exactly what we asked
    for, so the model is pinned by definition. The family table has never heard of
    it — and that must cost the run nothing. It is a REPORTING table, not a gate."""
    qual = qualify(
        "claude",
        model="deepseek-v4-pro",
        work_root=tmp_path,
        base_url="http://localhost:4000",
        auth_token="tok-9",
        run_card_impl=_impl(_good_probe(model_served="deepseek-v4-pro")),
        version_impl=_ok_version,
    )
    assert qual.ok, [c for c in qual.checks if c.status == STATUS_FAIL]
    assert _by_name(qual, "model_pinned").status == STATUS_PASS
    # ... and the family stays honestly unnameable, never guessed.
    from harness.agents import family_of

    assert family_of("deepseek-v4-pro") is None


def test_model_pinned_has_exactly_three_outcomes(tmp_path: Path) -> None:
    """(1) served == requested -> pinned, table irrelevant.
    (2) served != requested but same known family -> pinned (cursor's display name).
    (3) served != requested and unnameable -> cannot prove pinning -> FAIL."""

    def outcome(model: str, served: str):
        q = qualify(
            "cursor", model=model, work_root=tmp_path,
            run_card_impl=_impl(_good_probe(model_served=served)),
            version_impl=_ok_version,
        )
        return _by_name(q, "model_pinned")

    # (1) exact echo
    assert outcome("claude-sonnet-4-6", "claude-sonnet-4-6").status == STATUS_PASS
    # (2) different spelling, same family
    assert outcome(
        "claude-sonnet-4-6", "Sonnet 4.6 200K Medium No Thinking"
    ).status == STATUS_PASS
    # (3) different string we cannot place: NOT provably the same model
    c3 = outcome("claude-sonnet-4-6", "Sonnet 4.7 1M High Thinking")
    assert c3.status == STATUS_FAIL
    assert "_MODEL_FAMILY" in c3.detail  # says exactly where to fix it


def test_a_genuinely_wrong_model_still_fails(tmp_path: Path) -> None:
    """The check got looser; it must not have gotten blind."""
    qual = qualify(
        "claude",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(_good_probe(model_served="claude-haiku-4-5-20251001")),
        version_impl=_ok_version,
    )
    assert not qual.ok
    detail = _by_name(qual, "model_pinned").detail
    assert "claude-sonnet-4.6" in detail and "claude-haiku-4.5" in detail


# ---------------------------------------------------------------------------
# Check 7 must detect the zeros, not just the missing keys
# ---------------------------------------------------------------------------


def test_all_zero_counters_fail_even_though_the_keys_are_present(tmp_path: Path) -> None:
    """The adapters normalize onto a fixed key set, so a renamed upstream counter
    yields present-but-zero keys. A live generation cannot cost 0 in + 0 out, so
    zeros mean we are reading the wrong keys — and the cost of not noticing is a
    published token total of 0."""
    zeros = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "num_turns": None,
    }
    qual = qualify(
        "cursor",
        model="claude-sonnet-4-6",
        work_root=tmp_path,
        run_card_impl=_impl(
            _good_probe(model_served="Sonnet 4.6 200K Medium No Thinking", usage=zeros)
        ),
        version_impl=_ok_version,
    )
    assert not qual.ok
    assert _by_name(qual, "tokens").status == STATUS_FAIL
    assert "cannot cost 0" in _by_name(qual, "tokens").detail
