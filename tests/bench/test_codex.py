"""Deterministic tests for the codex adapter — built from the spec's live samples.

``fixtures/agent-streams/codex-skill-lit.raw.jsonl`` is assembled from the
empirically-captured ``codex exec --json`` event shapes in the codex-adapter spec
(§1c/§1e, codex-cli 0.144.6, 2026-07-18), sanitized to ``/tmp`` paths with no
credentials. It carries, in one run: an early + a final ``agent_message`` (last
wins), a SKILL.md-read ``command_execution`` (routing), a compound
``lit hello && lit vault list`` in one shell call (two lit calls sharing the item
id), a read of one of codex's OWN builtin plugin skills (must NOT be a routing
hit — scoping), each command's ``item.started`` twin (must NOT double-count), and a
``turn.completed`` usage block.

The served model is the ONE axis NOT in the stream: codex writes it into a session
rollout under its isolated ``CODEX_HOME``, so ``_served_model`` reads a rollout file
on disk (no subprocess). ``CodexAdapter.parse`` drives that REAL path against a
rollout planted under ``base/codexhome`` — an all-mocked green suite would hide a
broken live path (memory: inject-seam must exercise the real default).

NEVER spawns anything (M34 §3.5 hard boundary).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness.agents import family_of, get_adapter
from harness.agents.codex import (
    CodexAdapter,
    _codex_home,
    _inner_bash_command,
    _served_model,
    normalize_usage,
    parse_stream,
    seed_auth,
)

STREAMS_DIR = Path(__file__).resolve().parent / "fixtures" / "agent-streams"
SKILL_STREAM = (STREAMS_DIR / "codex-skill-lit.raw.jsonl").read_text(encoding="utf-8")

# The lit command's item id in the fixture (its one compound lit call).
LIT_ITEM_ID = "item_2"


def _run():
    return parse_stream(SKILL_STREAM.splitlines())


def _rollout_records(model: str) -> list[dict]:
    """A realistic two-record codex rollout: a model-LESS session_meta first, then a
    turn_context whose PAYLOAD carries the model (codex nests it, not top-level)."""
    return [
        {
            "timestamp": "2026-07-18T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": "sess-1", "cwd": "/tmp/run/cwd", "cli_version": "0.144.6"},
        },
        {
            "timestamp": "2026-07-18T10:00:01Z",
            "type": "turn_context",
            "payload": {
                "cwd": "/tmp/run/cwd",
                "approval_policy": "never",
                "sandbox_policy": {"mode": "danger-full-access"},
                "model": model,
                "effort": None,
                "summary": "auto",
            },
        },
    ]


def _write_rollout(codex_home: Path, relpath: str, records: list[dict]) -> Path:
    path = codex_home / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# AC3 — parse_stream: lit argv + stdout, compound, scoped routing, final, twins
# ---------------------------------------------------------------------------


def test_compound_command_yields_one_lit_call_per_segment() -> None:
    """`lit hello && lit vault list` is ONE command_execution carrying TWO lit
    invocations; both share the item id and the unwrapped inner as their raw."""
    result = _run()
    assert [c.argv for c in result.lit_calls] == [["hello"], ["vault", "list"]]
    assert all(c.raw == "lit hello && lit vault list" for c in result.lit_calls)
    assert all(c.tool_use_id == LIT_ITEM_ID for c in result.lit_calls)


def test_lit_stdout_is_captured_and_paired_back_to_its_calls() -> None:
    """The one aggregated_output pairs to BOTH segments (shared item id)."""
    result = _run()
    records = result.as_jsonl_records()
    assert [r["argv"] for r in records] == [["hello"], ["vault", "list"]]
    assert all(r["stdout"].startswith("litman v1.2.0") for r in records)


def test_skill_md_read_is_the_routing_signal_scoped_to_agents_skills() -> None:
    """codex activates by READING a skill's SKILL.md via a shell command. Only the
    read under /.agents/skills/ is the routing hit — the builtin-plugin read (also
    under a /skills/ path) is correctly NOT counted."""
    result = _run()
    assert result.skills == ["lit-library"]


def test_plugin_skill_read_is_not_a_routing_hit() -> None:
    """A read of one of codex's OWN plugin skills (CODEX_HOME/plugins/.../skills/)
    has no /.agents/skills/ in its path, so scoping keeps it out of the labels."""
    events = [
        json.dumps({"type": "item.completed", "item": {
            "id": "i1", "type": "command_execution",
            "command": "/usr/bin/bash -lc \"sed -n '1,80p' "
                       "/h/codexhome/plugins/openai-curated-remote/skills/git/SKILL.md\"",
            "aggregated_output": "..."}}),
        json.dumps({"type": "item.completed", "item": {
            "id": "i2", "type": "command_execution",
            "command": "/usr/bin/bash -lc \"sed -n '1,240p' "
                       "/h/home/.agents/skills/lit-reading/SKILL.md\"",
            "aggregated_output": "..."}}),
    ]
    assert parse_stream(events).skills == ["lit-reading"]


def test_last_agent_message_is_the_final_answer() -> None:
    result = _run()
    assert result.final_text == (
        "No vaults are registered. Register one with `lit vault add <name> <path>`."
    )
    assert "Let me check" not in result.final_text  # the earlier one was overwritten


def test_only_item_completed_is_counted_not_the_started_twin() -> None:
    """Each command emits an item.started twin (empty output, exit_code null);
    processing only item.completed keeps one real call as one evidence record."""
    result = _run()
    assert len(result.lit_calls) == 2  # not doubled by item_2's started twin
    # Three item.completed command_executions -> three "shell"; the two started
    # twins (item_1, item_2) added nothing.
    assert result.tool_names == ["shell", "shell", "shell"]


# ---------------------------------------------------------------------------
# AC4 — normalize_usage: the four codex keys, the guard, num_turns
# ---------------------------------------------------------------------------


def test_usage_maps_the_four_codex_keys_from_the_stream() -> None:
    u = _run().usage
    assert u["input_tokens"] == 12558 - 9984  # total prompt MINUS cached
    assert u["cache_read_input_tokens"] == 9984
    assert u["output_tokens"] == 11
    assert u["reasoning_tokens"] == 0
    assert u["num_turns"] is None


def test_normalize_usage_sums_multiple_turn_blocks() -> None:
    """A run may have >1 turn.completed; the blocks are summed onto the counters."""
    blocks = [
        {"input_tokens": 100, "cached_input_tokens": 40,
         "output_tokens": 5, "reasoning_output_tokens": 2},
        {"input_tokens": 200, "cached_input_tokens": 50,
         "output_tokens": 7, "reasoning_output_tokens": 3},
    ]
    out = normalize_usage(blocks)
    assert out["input_tokens"] == (100 - 40) + (200 - 50)
    assert out["cache_read_input_tokens"] == 40 + 50
    assert out["output_tokens"] == 12
    assert out["reasoning_tokens"] == 5
    assert out["num_turns"] is None


def test_empty_block_list_is_not_observed_rather_than_zero() -> None:
    assert normalize_usage([]) == {}


def test_a_renamed_usage_shape_is_not_observed_rather_than_a_fictional_zero() -> None:
    """If codex renames its counters, defaulting each to 0 would produce a TRUTHY
    all-zero dict that _sum_usage counts as a free spawn. It must read as 'not
    observed' instead — and land as {} at the counter gate."""
    from harness.batch import _sum_usage

    renamed = [{"promptTokens": 4, "completionTokens": 86}]  # a plausible rename
    assert normalize_usage(renamed) == {}
    assert _sum_usage([normalize_usage(renamed)]) == {}


def test_input_tokens_is_the_non_cached_part_and_clamps_at_zero() -> None:
    """OpenAI's input_tokens is the TOTAL prompt incl. cache, so the cached part is
    subtracted. A (pathological) block reporting cached > input clamps at 0."""
    out = normalize_usage(
        [{"input_tokens": 30, "cached_input_tokens": 50, "output_tokens": 1}]
    )
    assert out["input_tokens"] == 0
    assert out["cache_read_input_tokens"] == 50


def test_partial_codex_shape_still_sums_what_is_there() -> None:
    """At least one known key = codex's schema; read what is present."""
    out = normalize_usage([{"input_tokens": 10}])
    assert out["input_tokens"] == 10
    assert out["cache_read_input_tokens"] == 0
    assert out["output_tokens"] == 0
    assert out["num_turns"] is None


def test_no_fabricated_cache_creation_counter() -> None:
    """codex reports NO cache-write counter, so normalize_usage does not invent a 0
    for it — the same fictional-zero the unknown-shape guard exists to avoid. The
    grand total's summer supplies its own 0 default for the key."""
    out = normalize_usage(
        [{"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}]
    )
    assert "cache_creation_input_tokens" not in out


def test_normalized_usage_sums_cleanly_through_the_grand_total() -> None:
    from harness.batch import _sum_usage

    bucket = _sum_usage([_run().usage])
    assert bucket["input_tokens"] == 12558 - 9984
    assert bucket["cache_read_input_tokens"] == 9984
    assert bucket["cache_creation_input_tokens"] == 0  # zero-filled by the summer
    assert bucket["spawns"] == 1
    assert "reasoning_tokens" not in bucket  # unknown to the summer, dropped safely


# ---------------------------------------------------------------------------
# _inner_bash_command — unwrap codex's /usr/bin/bash -lc "<inner>"
# ---------------------------------------------------------------------------


def test_inner_bash_command_unwraps_a_single_quoted_command() -> None:
    assert _inner_bash_command("/usr/bin/bash -lc 'lit list'") == "lit list"


def test_inner_bash_command_unwraps_a_double_quoted_sed() -> None:
    cmd = "/usr/bin/bash -lc \"sed -n '1,240p' /x/.agents/skills/y/SKILL.md\""
    assert _inner_bash_command(cmd) == "sed -n '1,240p' /x/.agents/skills/y/SKILL.md"


def test_inner_bash_command_handles_the_plain_c_flag() -> None:
    assert _inner_bash_command("/bin/bash -c 'lit hello'") == "lit hello"


def test_inner_bash_command_returns_a_non_bash_string_unchanged() -> None:
    assert _inner_bash_command("lit vault list") == "lit vault list"


def test_inner_bash_command_returns_unchanged_on_a_parse_failure() -> None:
    """An unbalanced quote makes shlex.split raise; best-effort returns the input
    rather than crashing the parse."""
    broken = "/usr/bin/bash -lc 'lit list"
    assert _inner_bash_command(broken) == broken


def test_routing_survives_even_if_the_wrapper_is_unparseable() -> None:
    """Routing searches the FULL cmd (the wrapper), not the unwrapped inner, so a
    SKILL.md read is still labeled even when _inner_bash_command can't split it."""
    events = [
        json.dumps({"type": "item.completed", "item": {
            "id": "i1", "type": "command_execution",
            # a stray unbalanced quote after the path defeats shlex, but the regex
            # still finds the .agents/skills/<name>/SKILL.md path.
            "command": "/usr/bin/bash -lc \"cat /h/.agents/skills/lit-library/SKILL.md' ",
            "aggregated_output": ""}}),
    ]
    assert parse_stream(events).skills == ["lit-library"]


# ---------------------------------------------------------------------------
# AC5 — _served_model: newest rollout's model; missing -> None
# ---------------------------------------------------------------------------


def test_served_model_reads_the_nested_model_from_the_rollout(tmp_path: Path) -> None:
    codex_home = tmp_path / "codexhome"
    _write_rollout(
        codex_home, "sessions/2026/07/18/rollout-a.jsonl",
        _rollout_records("gpt-5.6-sol"),
    )
    assert _served_model(codex_home) == "gpt-5.6-sol"


def test_served_model_also_reads_a_top_level_model_field(tmp_path: Path) -> None:
    """Robust to a flat record too — the first `model` string at any depth."""
    codex_home = tmp_path / "codexhome"
    _write_rollout(
        codex_home, "sessions/2026/07/18/rollout-flat.jsonl",
        [{"type": "response_item", "model": "gpt-5.6-luna"}],
    )
    assert _served_model(codex_home) == "gpt-5.6-luna"


def test_served_model_picks_the_newest_of_two_rollouts(tmp_path: Path) -> None:
    codex_home = tmp_path / "codexhome"
    old = _write_rollout(
        codex_home, "sessions/2026/07/17/rollout-old.jsonl",
        _rollout_records("gpt-5.6-terra"),
    )
    new = _write_rollout(
        codex_home, "sessions/2026/07/18/rollout-new.jsonl",
        _rollout_records("gpt-5.6-sol"),
    )
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert _served_model(codex_home) == "gpt-5.6-sol"


def test_served_model_is_none_when_there_is_no_rollout(tmp_path: Path) -> None:
    (tmp_path / "codexhome").mkdir()
    assert _served_model(tmp_path / "codexhome") is None


def test_served_model_is_none_when_the_rollout_has_no_model(tmp_path: Path) -> None:
    """A model-less rollout -> None, never a guess (Phase 0 pins served==requested,
    so inventing the request here would mask exactly the mismatch it catches)."""
    codex_home = tmp_path / "codexhome"
    _write_rollout(
        codex_home, "sessions/2026/07/18/rollout-x.jsonl",
        [{"type": "session_meta", "payload": {"id": "s", "cwd": "/x"}}],
    )
    assert _served_model(codex_home) is None


# ---------------------------------------------------------------------------
# Inject-seam — parse() drives the REAL _served_model against a planted rollout
# ---------------------------------------------------------------------------


def test_parse_recovers_served_model_from_the_rollout_end_to_end(tmp_path: Path) -> None:
    """The real parse -> _served_model path: a rollout planted under base/codexhome
    is read with NO subprocess and NO mock, and the stream evidence is intact
    through the same call (memory: inject-seam must exercise the real default)."""
    _write_rollout(
        _codex_home(tmp_path), "sessions/2026/07/18/rollout-x.jsonl",
        _rollout_records("gpt-5.6-sol"),
    )
    result = CodexAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served == "gpt-5.6-sol"
    assert [c.argv for c in result.lit_calls] == [["hello"], ["vault", "list"]]
    assert result.skills == ["lit-library"]


def test_parse_reports_none_served_model_when_no_rollout(tmp_path: Path) -> None:
    """Unrecoverable served model -> None, NEVER the requested/default model."""
    _codex_home(tmp_path).mkdir()
    result = CodexAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served is None
    assert result.model_served != CodexAdapter.default_model


# ---------------------------------------------------------------------------
# AC6 — seed_auth raises on a missing credential (codex has no free tier)
# ---------------------------------------------------------------------------


def test_seed_auth_raises_on_a_missing_credential(tmp_path: Path, monkeypatch) -> None:
    """Unlike opencode (loginless free model -> silent skip), codex has no free
    tier, so a missing credential is a misconfiguration and must fail legibly."""
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))  # empty: no ~/.codex
    with pytest.raises(RuntimeError, match="codex login"):
        seed_auth(tmp_path / "codexhome")


def test_seed_auth_copies_the_credential_preserving_mode(
    tmp_path: Path, monkeypatch
) -> None:
    """The credential is COPIED into CODEX_HOME/auth.json (never moved/edited), with
    its 0600 mode preserved. Fabricated token — this repo is public."""
    user_home = tmp_path / "userhome"
    src = user_home / ".codex" / "auth.json"
    src.parent.mkdir(parents=True)
    src.write_text('{"fake": "not-a-real-token"}')
    src.chmod(0o600)
    monkeypatch.setenv("HOME", str(user_home))

    codex_home = tmp_path / "codexhome"
    seed_auth(codex_home)

    seeded = codex_home / "auth.json"
    assert seeded.read_text() == '{"fake": "not-a-real-token"}'
    assert (seeded.stat().st_mode & 0o777) == 0o600  # copy2 keeps it private
    assert src.is_file()  # source untouched (copied, not moved)


# ---------------------------------------------------------------------------
# AC1/AC2 — registration, isolation (codexhome + CODEX_HOME drop/re-set)
# ---------------------------------------------------------------------------


def test_codex_is_a_registered_agent() -> None:
    from harness.agents import AGENT_NAMES

    assert "codex" in AGENT_NAMES
    assert get_adapter("codex").name == "codex"


def test_codex_home_is_derived_from_base() -> None:
    assert _codex_home(Path("/tmp/bench-x")) == Path("/tmp/bench-x") / "codexhome"


def test_skills_dir_is_the_agents_skills_dir(tmp_path: Path) -> None:
    assert CodexAdapter().skills_dir(tmp_path) == (
        tmp_path / "home" / ".agents" / "skills"
    )


def test_prepare_isolates_home_codexhome_registry_vault_and_reseats_codex_home(
    tmp_path: Path, monkeypatch
) -> None:
    user_home = tmp_path / "userhome"
    # Fabricate the credential at the real location (relative to the faked HOME) so
    # the REAL seed_auth runs without ever reading the maintainer's ~/.codex.
    auth = user_home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"fake": "not-a-real-token"}')
    auth.chmod(0o600)
    monkeypatch.setenv("HOME", str(user_home))
    # A set CODEX_HOME points codex at the real login store, walking past the
    # redirected HOME — prepare() must DROP it and re-set its own isolated dir.
    monkeypatch.setenv("CODEX_HOME", str(user_home / ".codex"))
    installed: list[Path] = []
    monkeypatch.setattr(
        "harness.agents.codex.install_repo_skills",
        lambda d, **k: installed.append(Path(d)),
    )
    adapter = CodexAdapter()

    env = adapter.prepare(tmp_path, run_vault=tmp_path / "vault")

    assert env["HOME"] == str(tmp_path / "home")
    assert env["LIT_LIBRARY"] == str(tmp_path / "vault")
    assert env["LITMAN_REGISTRY_DIR"] == str(tmp_path / "codex-registry")
    # CODEX_HOME re-set to the run's OWN dir (the claude/CLAUDE_CONFIG_DIR pattern).
    assert env["CODEX_HOME"] == str(tmp_path / "codexhome")
    assert env["CODEX_HOME"] != str(user_home / ".codex")
    # Skills go to <home>/.agents/skills (the open standard).
    assert installed == [tmp_path / "home" / ".agents" / "skills"]
    # The credential was seeded into CODEX_HOME/auth.json; the source is untouched.
    seeded = tmp_path / "codexhome" / "auth.json"
    assert seeded.read_text() == '{"fake": "not-a-real-token"}'
    assert auth.is_file()


# ---------------------------------------------------------------------------
# AC7 — capabilities + model family; argv; proxy refusal; default
# ---------------------------------------------------------------------------


def test_capabilities_are_declared_not_inferred() -> None:
    caps = get_adapter("codex").capabilities
    assert (caps.tokens, caps.turns, caps.served_model, caps.routing) == (
        True, False, True, True,
    )


def test_model_family_has_the_three_gpt_ids() -> None:
    assert family_of("gpt-5.6-sol") == "gpt-5.6-sol"
    assert family_of("gpt-5.6-terra") == "gpt-5.6-terra"
    assert family_of("gpt-5.6-luna") == "gpt-5.6-luna"


def test_build_argv_pins_model_permission_cwd_and_git_check() -> None:
    argv = CodexAdapter().build_argv(
        "do a thing", model="gpt-5.6-sol", cwd=Path("/x/cwd")
    )
    assert argv[1] == "exec"
    assert "--json" in argv
    # The full bench bypass, held constant with cursor's/agy's flags.
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert CodexAdapter().permission_flags == (
        "--dangerously-bypass-approvals-and-sandbox",
    )
    # The run root is /tmp, not a git repo, so this is required or codex refuses.
    assert "--skip-git-repo-check" in argv
    i = argv.index("-C")
    assert argv[i : i + 2] == ["-C", "/x/cwd"]
    j = argv.index("-m")
    assert argv[j : j + 2] == ["-m", "gpt-5.6-sol"]
    # The prompt is the final positional argument.
    assert argv[-1] == "do a thing"
    # NOT --ephemeral: it suppresses the rollout, the served-model source.
    assert "--ephemeral" not in argv


def test_default_model_is_the_subscription_default() -> None:
    assert CodexAdapter().default_model == "gpt-5.6-sol"


def test_prepare_rejects_proxy_flags_it_cannot_honor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Anthropic-compatible proxy mode"):
        CodexAdapter().prepare(
            tmp_path, run_vault=tmp_path / "vault", base_url="https://proxy.example/v1"
        )
