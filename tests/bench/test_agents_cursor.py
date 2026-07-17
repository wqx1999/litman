"""Deterministic tests for the cursor adapter — driven by REAL recorded streams.

Every fixture under ``fixtures/agent-streams/`` is an actual ``cursor-agent
--output-format stream-json`` recording made on this machine, not a hand-authored
approximation. That distinction is the entire value of these tests: each of the
shapes below (the double-emitted tool_call, the stdout nested a level deeper than
it looks, the camelCase counters, the read-a-file-to-activate-a-skill signal) was
discovered by recording a run and being surprised. A parser tested against events
we invented would agree with our misunderstanding.

The recordings are sanitized: they were made against a copy of a real library, so
home paths, library contents and the agent's prose are redacted. The three signals
under test — usage / executableCommands / readToolCall.path — are intact.

NEVER spawns anything (M34 §3.5 hard boundary).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.agents import get_adapter
from harness.agents.cursor import (
    CursorAdapter,
    normalize_usage,
    parse_stream,
)

STREAMS_DIR = Path(__file__).resolve().parent / "fixtures" / "agent-streams"


def _stream(name: str) -> str:
    return (STREAMS_DIR / name).read_text(encoding="utf-8")


def _lit_version_run():
    return parse_stream(_stream("cursor-shell-lit-version.raw.jsonl").splitlines())


def _skill_run():
    return parse_stream(_stream("cursor-skill-activation.raw.jsonl").splitlines())


# ---------------------------------------------------------------------------
# executableCommands -> lit argv (cursor pre-splits the shell for us)
# ---------------------------------------------------------------------------


def test_parses_lit_argv_from_executable_commands() -> None:
    result = _lit_version_run()
    assert [c.argv for c in result.lit_calls] == [["--version"]]
    assert result.lit_calls[0].raw == "lit --version"


def test_lit_call_is_not_double_counted_across_started_and_completed() -> None:
    """cursor emits every tool_call TWICE (started + completed), both carrying the
    same parsed args. One real invocation must stay ONE evidence record."""
    result = _lit_version_run()
    # The recording has both events for a single `lit --version`.
    subtypes = [
        e.get("subtype") for e in result.raw_events if e.get("type") == "tool_call"
    ]
    assert subtypes == ["started", "completed"]
    assert len(result.lit_calls) == 1


def test_ignores_non_lit_binaries_in_the_same_command() -> None:
    """`lit list --format json | python3 -c ...` yields TWO executableCommands;
    only the lit one is evidence, and the pipe/redirect never leak into its argv."""
    result = _skill_run()
    argvs = [c.argv for c in result.lit_calls]
    assert ["list", "--format", "json"] in argvs
    assert all("python3" not in a and "2>&1" not in a for a in argvs)


def test_redirects_are_stripped_by_cursors_own_parser() -> None:
    """`lit list 2>&1` -> argv ["list"] (the redirect is lifted out upstream)."""
    result = _skill_run()
    assert ["list"] in [c.argv for c in result.lit_calls]


def test_non_allowlisted_binary_under_force_yields_no_lit_evidence() -> None:
    """The --force recording runs `uname -sr`: a shell call, but not a lit call."""
    result = parse_stream(_stream("cursor-force-uname.raw.jsonl").splitlines())
    assert result.lit_calls == []
    assert result.tool_names == ["shell"]
    # The run itself was fine — it is only lit evidence that is (correctly) absent.
    assert result.final_text == "Linux 6.12.92-1.el8.x86_64"


# ---------------------------------------------------------------------------
# result.success.stdout (nested one deeper than tool_call.result)
# ---------------------------------------------------------------------------


def test_captures_stdout_from_shell_tool_call_result_success() -> None:
    result = _lit_version_run()
    assert [tr.content for tr in result.tool_results] == ["lit, version 1.2.0\n"]


def test_stdout_is_paired_back_to_its_lit_call() -> None:
    result = _lit_version_run()
    records = result.as_jsonl_records()
    assert records == [
        {"argv": ["--version"], "raw": "lit --version", "stdout": "lit, version 1.2.0\n"}
    ]


# ---------------------------------------------------------------------------
# readToolCall on SKILL.md == cursor's skill activation (the RA signal)
# ---------------------------------------------------------------------------


def test_reading_skill_md_is_the_routing_signal() -> None:
    """cursor has no Skill tool: it activates by READING skills/<name>/SKILL.md."""
    result = _skill_run()
    assert result.skills == ["lit-library"]


def test_only_skill_md_itself_counts_as_activation() -> None:
    """A read of a skill's reference material is not an activation."""
    events = [
        '{"type":"tool_call","subtype":"completed","call_id":"a","tool_call":'
        '{"readToolCall":{"args":{"path":"/h/.agents/skills/lit-library/references/x.md"}}}}',
        '{"type":"tool_call","subtype":"completed","call_id":"b","tool_call":'
        '{"readToolCall":{"args":{"path":"/h/.agents/skills/lit-reading/SKILL.md"}}}}',
    ]
    assert parse_stream(events).skills == ["lit-reading"]


# ---------------------------------------------------------------------------
# usage: camelCase -> internal keys (the silent-zero pitfall)
# ---------------------------------------------------------------------------


def test_camelcase_usage_is_normalized_to_the_internal_keys() -> None:
    result = _lit_version_run()
    assert result.usage["input_tokens"] == 4
    assert result.usage["output_tokens"] == 86
    assert result.usage["cache_read_input_tokens"] == 19868
    assert result.usage["cache_creation_input_tokens"] == 20024


def test_raw_cursor_usage_would_have_summed_to_a_silent_zero() -> None:
    """Guards the reason normalize_usage exists.

    Feeding cursor's dict to the shared summing raises NOTHING — it returns a
    clean, plausible, entirely fictional zero. It is the normalized dict that must
    reach `_sum_usage`, and this pins that the two shapes really are different."""
    from harness.batch import _sum_usage

    raw = {"inputTokens": 4, "outputTokens": 86, "cacheReadTokens": 19868}
    silently_wrong = _sum_usage([raw])
    assert silently_wrong["input_tokens"] == 0  # not an error — just fiction
    assert silently_wrong["output_tokens"] == 0

    honest = _sum_usage([normalize_usage(raw)])
    assert honest["input_tokens"] == 4
    assert honest["output_tokens"] == 86


def test_absent_turn_count_is_none_not_zero() -> None:
    """cursor reports no turns anywhere. `None` says so; `0` would be a claim."""
    result = _lit_version_run()
    assert "num_turns" in result.usage
    assert result.usage["num_turns"] is None
    assert CursorAdapter().capabilities.turns is False


def test_no_usage_block_yields_no_usage_at_all() -> None:
    assert normalize_usage({}) == {}


# ---------------------------------------------------------------------------
# system/init: the served model, verbatim
# ---------------------------------------------------------------------------


def test_served_model_is_kept_as_the_raw_display_name() -> None:
    """cursor reports a display name, not the id we requested. Kept verbatim so
    "200K Medium No Thinking" stays visible to the reader."""
    result = _lit_version_run()
    assert result.model_served == "Sonnet 4.6 200K Medium No Thinking"


# ---------------------------------------------------------------------------
# argv + capabilities
# ---------------------------------------------------------------------------


def test_build_argv_pins_the_model_and_carries_the_force_flag() -> None:
    argv = CursorAdapter().build_argv("do a thing", model="claude-sonnet-4-6")
    assert argv[1:3] == ["-p", "do a thing"]
    assert "--model" in argv and "claude-sonnet-4-6" in argv
    assert "--output-format" in argv and "stream-json" in argv
    # Hard-coded on purpose: the bench holds the permission variable constant
    # across agents, and records the flag it used in the report.
    assert "--force" in argv
    assert CursorAdapter().permission_flags == ("--force",)


def test_capabilities_are_declared_not_inferred() -> None:
    caps = get_adapter("cursor").capabilities
    assert (caps.tokens, caps.turns, caps.served_model, caps.routing) == (
        True, False, True, True,
    )


def test_prepare_rejects_proxy_flags_it_cannot_honor(tmp_path: Path) -> None:
    """--base-url would be silently ignored otherwise: an un-proxied run reported
    as a proxied one is a wrong data point, not a warning.

    Its registry-level twin asserts this for EVERY agent, derived from
    ``supports_anthropic_proxy``; this one stays as cursor's own backstop and pins the text.
    """
    with pytest.raises(ValueError, match="no Anthropic-compatible proxy mode"):
        CursorAdapter().prepare(
            tmp_path, run_vault=tmp_path / "vault", base_url="https://proxy.example/v1"
        )


def test_prepare_isolates_home_and_installs_skills_where_cursor_looks(
    tmp_path: Path, monkeypatch
) -> None:
    # Empty fake user HOME: never read the user's real ~/.config/cursor/auth.json.
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    # A set XDG_CONFIG_HOME would let cursor bypass the redirected HOME and read
    # the user's real ~/.config — prepare() must drop it from the child env.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "userhome" / ".config"))
    installed: list[Path] = []
    monkeypatch.setattr(
        "harness.agents.cursor.install_repo_skills",
        lambda d, **k: installed.append(Path(d)),
    )
    adapter = CursorAdapter()
    env = adapter.prepare(tmp_path, run_vault=tmp_path / "vault")

    assert env["HOME"] == str(tmp_path / "home")
    assert env["LIT_LIBRARY"] == str(tmp_path / "vault")
    assert env["LITMAN_REGISTRY_DIR"] == str(tmp_path / "cursor-registry")
    # The process CWD, NOT a HOME-level dir: with HOME redirected, cursor reads
    # neither ~/.claude/skills nor ~/.agents/skills (measured) — CWD delivery is
    # the only path that works. base == run_vault.parent, so base/"cwd" is the
    # executor's neutral_cwd_for(run_vault).
    assert installed == [tmp_path / "cwd" / ".claude" / "skills"]
    assert not (tmp_path / "home" / ".claude" / "skills").exists()
    assert not (tmp_path / "home" / ".agents" / "skills").exists()
    assert "XDG_CONFIG_HOME" not in env


def test_prepare_seeds_auth_json_not_cli_config(tmp_path: Path, monkeypatch) -> None:
    """The token lives in ~/.config/cursor/auth.json; cli-config.json carries
    authInfo + preferences but NO token — seeding it produces a logged-out run."""
    user_home = tmp_path / "userhome"
    # Both files present (fabricated — this repo is public): only auth.json moves.
    auth = user_home / ".config" / "cursor" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"accessToken": "fake-access", "refreshToken": "fake-refresh"}')
    auth.chmod(0o600)
    cli_cfg = user_home / ".cursor" / "cli-config.json"
    cli_cfg.parent.mkdir(parents=True)
    cli_cfg.write_text('{"authInfo": {"email": "fake@example.org"}}')
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setattr(
        "harness.agents.cursor.install_repo_skills", lambda *a, **k: None
    )

    CursorAdapter().prepare(tmp_path, run_vault=tmp_path / "vault")

    seeded = tmp_path / "home" / ".config" / "cursor" / "auth.json"
    assert seeded.read_text() == '{"accessToken": "fake-access", "refreshToken": "fake-refresh"}'
    assert (seeded.stat().st_mode & 0o777) == 0o600  # copy2 keeps it private
    assert not (tmp_path / "home" / ".cursor" / "cli-config.json").exists()


def test_prepare_skips_the_auth_seed_silently_when_absent(
    tmp_path: Path, monkeypatch
) -> None:
    """Unlike agy (which hangs without a credential and so must raise), cursor has
    a working fallback: an exported CURSOR_API_KEY survives via the inherited env."""
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setattr(
        "harness.agents.cursor.install_repo_skills", lambda *a, **k: None
    )

    env = CursorAdapter().prepare(tmp_path, run_vault=tmp_path / "vault")

    assert env["HOME"] == str(tmp_path / "home")
    assert not (tmp_path / "home" / ".config" / "cursor" / "auth.json").exists()


def test_an_unrecognized_usage_shape_is_not_observed_rather_than_zero() -> None:
    """坑 1, one level down.

    If cursor renames its counters, defaulting each to 0 would produce
    `{"input_tokens": 0, ...}` — a dict that is TRUTHY, so `_sum_usage` counts it
    as a contributing spawn and the report publishes "1 spawn, 0 tokens". A number
    that is missing is recoverable; a number that is wrong is not.
    """
    from harness.batch import _sum_usage

    renamed = {"promptTokens": 4, "completionTokens": 86}  # a plausible rename
    assert normalize_usage(renamed) == {}
    # ... so it lands in the report as "not observed", not as a free spawn.
    assert _sum_usage([normalize_usage(renamed)]) == {}


def test_a_partially_recognized_shape_still_normalizes() -> None:
    """At least one known key = cursor's schema, so read what is there. The
    zeros-for-absent-keys risk is caught at the Phase 0 counter gate."""
    out = normalize_usage({"inputTokens": 4, "outputTokens": 86})
    assert out["input_tokens"] == 4
    assert out["cache_read_input_tokens"] == 0


def test_shell_call_id_falls_back_to_the_nested_copy() -> None:
    """`shellToolCall` carries its own id at args.toolCallId. Losing the id does
    not crash — it collapses every shell call onto one key and keeps only the
    last, i.e. it silently under-reports the agent's lit calls."""
    events = [
        '{"type":"tool_call","subtype":"completed","tool_call":{"shellToolCall":'
        '{"args":{"command":"lit list","toolCallId":"t1","parsingResult":'
        '{"executableCommands":[{"name":"lit","args":[{"type":"word","value":"list"}]}]}},'
        '"result":{"success":{"stdout":"one\\n"}}}}}',
        '{"type":"tool_call","subtype":"completed","tool_call":{"shellToolCall":'
        '{"args":{"command":"lit show x","toolCallId":"t2","parsingResult":'
        '{"executableCommands":[{"name":"lit","args":[{"type":"word","value":"show"},'
        '{"type":"word","value":"x"}]}]}},"result":{"success":{"stdout":"two\\n"}}}}}',
    ]
    result = parse_stream(events)
    # Both survive, each paired with its OWN stdout.
    assert [c.argv for c in result.lit_calls] == [["list"], ["show", "x"]]
    assert [r["stdout"] for r in result.as_jsonl_records()] == ["one\n", "two\n"]


def test_calls_with_no_id_anywhere_do_not_collapse_onto_one_key() -> None:
    """All three id sources absent: keep the `completed` halves, each under its own
    key. A shared "" would drop all but the last."""
    events = [
        '{"type":"tool_call","subtype":"completed","tool_call":{"shellToolCall":'
        '{"args":{"command":"lit list","parsingResult":{"executableCommands":'
        '[{"name":"lit","args":[{"type":"word","value":"list"}]}]}}}}}',
        '{"type":"tool_call","subtype":"completed","tool_call":{"shellToolCall":'
        '{"args":{"command":"lit show x","parsingResult":{"executableCommands":'
        '[{"name":"lit","args":[{"type":"word","value":"show"},{"type":"word","value":"x"}]}]}}}}}',
    ]
    assert [c.argv for c in parse_stream(events).lit_calls] == [["list"], ["show", "x"]]
