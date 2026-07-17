"""Deterministic tests for the opencode adapter — driven by ONE real recorded run.

Both fixtures are the SAME isolated ``opencode run --format json`` session
(``ses_08f4fc888ffeHbmGOCU4yHlEqo``), recorded on this machine against an isolated
empty HOME + a free (loginless) model:

* ``fixtures/agent-streams/opencode-skill-lit.raw.jsonl`` — the event stream. It
  activated the native ``skill`` tool (``lit-library``) and ran a compound
  ``lit hello && lit vault list`` in one bash call.
* ``fixtures/agent-streams/opencode-export.json`` — ``opencode export`` of THAT
  same session (``info.id`` == the stream's ``sessionID``, asserted below). This
  is the true end-to-end pair the spec's "pin from one real run" discipline asks
  for: the served model recovered here is the model that served THIS stream, not a
  mapping demonstrated against some unrelated session.

Both are sanitized (scratchpad paths redacted, the embedded SKILL.md body trimmed);
the fields under test — ``state.input.name``, ``state.input.command``,
``state.metadata.output``, ``part.tokens``, ``sessionID`` and ``info.{id,model}`` —
are intact.

``opencode export`` is GLOBAL BY SESSION ID: it resolves a session by id from any
cwd under the same HOME (the session's ``directory`` field just records where it
was created), so ``_run_export`` correctly passes no ``cwd`` even though the live
agent runs in ``neutral_cwd`` — verified live, do not re-flag.

The served model is the one axis NOT in the stream, so the export subprocess is
stubbed at the ``subprocess.run`` boundary and the REAL ``_run_export`` /
``_served_model`` / ``parse`` path runs against the recorded export — an all-mocked
green suite would hide a broken live path (memory: inject-seam must exercise the
real default).

NEVER spawns anything (M34 §3.5 hard boundary).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.agents import get_adapter
from harness.agents.opencode import (
    OpencodeAdapter,
    normalize_usage,
    parse_stream,
    seed_auth,
)

STREAMS_DIR = Path(__file__).resolve().parent / "fixtures" / "agent-streams"

SKILL_STREAM = (STREAMS_DIR / "opencode-skill-lit.raw.jsonl").read_text(
    encoding="utf-8"
)
EXPORT_JSON = (STREAMS_DIR / "opencode-export.json").read_text(encoding="utf-8")

# The bash callID in the recorded stream (its one compound lit command).
BASH_CALL_ID = "call_00_sb4blQT9SocwWYYpuJSb4857"


class _FakeCompleted:
    """A stand-in for ``subprocess.CompletedProcess`` (returncode + stdout only)."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _stub_export(monkeypatch, returncode: int, stdout: str) -> list[dict]:
    """Replace the export subprocess AND freeze the retry backoff; return a list the
    calls are recorded into.

    Every call gets the SAME ``(returncode, stdout)``, so a non-zero return exercises
    ``_run_export``'s retry (two calls). ``time.sleep`` is stubbed to a no-op so that
    retry never injects a real wall-clock delay into the unit suite (spec D3: stub
    both ``subprocess.run`` and ``time.sleep`` at the ``harness.agents.opencode``
    boundary)."""
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "env": kwargs.get("env")})
        return _FakeCompleted(returncode, stdout)

    monkeypatch.setattr("harness.agents.opencode.subprocess.run", fake_run)
    monkeypatch.setattr("harness.agents.opencode.time.sleep", lambda _s: None)
    return calls


def _stub_export_sequence(monkeypatch, responses: list) -> dict:
    """Stub the export subprocess with a SEQUENCE of per-call responses and freeze
    the retry backoff so the test never actually sleeps.

    Each response is either a ``_FakeCompleted`` (returned) or an exception instance
    (raised — e.g. ``subprocess.TimeoutExpired``, to exercise the timeout path).
    Returns a record with the per-call argv/env list and the number of backoff
    sleeps, so a test can PIN 'it retried exactly once' rather than trust it."""
    seq = list(responses)
    record: dict = {"calls": [], "sleeps": 0}

    def fake_run(argv, **kwargs):
        record["calls"].append({"argv": argv, "env": kwargs.get("env")})
        outcome = seq.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def fake_sleep(_seconds):
        record["sleeps"] += 1

    monkeypatch.setattr("harness.agents.opencode.subprocess.run", fake_run)
    monkeypatch.setattr("harness.agents.opencode.time.sleep", fake_sleep)
    return record


# ---------------------------------------------------------------------------
# AC1 — the stream parse (bash argv + stdout, native skill, tokens, final text)
# ---------------------------------------------------------------------------


def test_compound_bash_yields_one_lit_call_per_segment() -> None:
    """`lit hello && lit vault list` is ONE bash call carrying TWO lit invocations;
    both share the bash callID (opencode reports the raw command, like claude)."""
    result, _ = parse_stream(SKILL_STREAM.splitlines())
    assert [c.argv for c in result.lit_calls] == [["hello"], ["vault", "list"]]
    assert all(c.raw == "lit hello && lit vault list" for c in result.lit_calls)
    assert all(c.tool_use_id == BASH_CALL_ID for c in result.lit_calls)


def test_bash_stdout_is_captured_and_paired_back_to_its_calls() -> None:
    """The one combined stdout pairs to BOTH segments (shared callID) — the same
    way claude folds a compound command's output."""
    result, _ = parse_stream(SKILL_STREAM.splitlines())
    assert len(result.tool_results) == 1
    tr = result.tool_results[0]
    assert tr.tool == "bash"
    assert tr.tool_use_id == BASH_CALL_ID
    assert tr.content.startswith("litman v1.2.0 is installed")
    records = result.as_jsonl_records()
    assert [r["argv"] for r in records] == [["hello"], ["vault", "list"]]
    assert all(r["stdout"].startswith("litman v1.2.0") for r in records)


def test_native_skill_tool_is_the_routing_signal() -> None:
    """opencode has a real ``skill`` tool: the name is at state.input.name — NOT a
    read-the-SKILL.md heuristic (that is cursor's fallback)."""
    result, _ = parse_stream(SKILL_STREAM.splitlines())
    assert result.skills == ["lit-library"]
    assert "skill" in result.tool_names and "bash" in result.tool_names


def test_tokens_are_summed_across_step_finish_blocks() -> None:
    """Three per-step token blocks, summed onto the internal snake_case counters."""
    result, _ = parse_stream(SKILL_STREAM.splitlines())
    u = result.usage
    assert u["input_tokens"] == 8718 + 11823 + 132
    assert u["output_tokens"] == 44 + 48 + 103
    assert u["cache_read_input_tokens"] == 0 + 8960 + 20864
    assert u["cache_creation_input_tokens"] == 0


def test_reasoning_tokens_are_kept_not_dropped_or_folded() -> None:
    """opencode-only ``reasoning`` survives under its own key — not lost, and not
    added into output_tokens (which would inflate it)."""
    result, _ = parse_stream(SKILL_STREAM.splitlines())
    assert result.usage["reasoning_tokens"] == 201 + 119 + 34
    # ... and it did NOT leak into output.
    assert result.usage["output_tokens"] == 44 + 48 + 103


def test_num_turns_is_none_because_steps_are_not_turns() -> None:
    result, _ = parse_stream(SKILL_STREAM.splitlines())
    assert "num_turns" in result.usage
    assert result.usage["num_turns"] is None


def test_final_text_is_the_assistant_answer() -> None:
    result, _ = parse_stream(SKILL_STREAM.splitlines())
    assert result.final_text.startswith("No vaults are registered.")


def test_session_id_is_recovered_for_the_export_step() -> None:
    _, session_id = parse_stream(SKILL_STREAM.splitlines())
    assert session_id == "ses_08f4fc888ffeHbmGOCU4yHlEqo"


# ---------------------------------------------------------------------------
# AC2 — served model via `opencode export` (the real path, subprocess stubbed)
# ---------------------------------------------------------------------------


def test_the_two_fixtures_are_one_session() -> None:
    """The pair is a single real run: the export is of the stream's OWN session, so
    ``info.id == sessionID``. If the fixtures are ever re-recorded from different
    sessions, this fails HERE rather than letting the AC2 mapping test pass against
    an unrelated session's JSON (the whole point of 'pin from one real run')."""
    import json

    _, stream_sid = parse_stream(SKILL_STREAM.splitlines())
    export_id = json.loads(EXPORT_JSON)["info"]["id"]
    assert export_id == stream_sid == "ses_08f4fc888ffeHbmGOCU4yHlEqo"


def test_served_model_comes_from_export_as_provider_slash_id(
    tmp_path: Path, monkeypatch
) -> None:
    """Drives the REAL _run_export -> _served_model -> parse path end-to-end, with
    only the subprocess boundary stubbed to return the recorded export JSON. Because
    the export is of the stream's OWN session (see test above), the model recovered
    here is the model that served THIS stream — the real end-to-end AC2/AC9 claim."""
    calls = _stub_export(monkeypatch, 0, EXPORT_JSON)
    result = OpencodeAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served == "opencode/deepseek-v4-flash-free"
    # It really shelled out to `opencode export <sid>` — not read from the stream.
    assert calls and calls[0]["argv"][1:] == ["export", "ses_08f4fc888ffeHbmGOCU4yHlEqo"]


def test_export_runs_in_the_runs_own_isolated_env(tmp_path: Path, monkeypatch) -> None:
    """D1: parse()'s export must use the SAME env prepare() built — it reads the
    run's session db under that env's redirected HOME. Pins the stash."""
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setattr(
        "harness.agents.opencode.install_repo_skills", lambda *a, **k: None
    )
    adapter = OpencodeAdapter()
    env = adapter.prepare(tmp_path, run_vault=tmp_path / "vault")
    calls = _stub_export(monkeypatch, 0, EXPORT_JSON)

    adapter.parse(SKILL_STREAM, base=tmp_path)

    assert calls[0]["env"] is env  # the exact isolated env, not a fresh os.environ
    assert calls[0]["env"]["HOME"] == str(tmp_path / "home")


# ---------------------------------------------------------------------------
# AC3 — unrecoverable served model reports None, NEVER the requested model
# ---------------------------------------------------------------------------


def test_no_session_id_yields_none_without_even_spawning_export(
    tmp_path: Path, monkeypatch
) -> None:
    """(a) A stream with no sessionID can't be exported — None, and no subprocess."""
    def explode(*a, **k):  # export must not be reached
        raise AssertionError("export should not run without a sessionID")

    monkeypatch.setattr("harness.agents.opencode.subprocess.run", explode)
    stream = '{"type":"text","part":{"type":"text","text":"hi"}}'
    result = OpencodeAdapter().parse(stream, base=tmp_path)
    assert result.model_served is None
    # Reverse-verify: NOT the requested/default model dressed up as served.
    assert result.model_served != OpencodeAdapter.default_model


def test_export_nonzero_exit_yields_none_not_the_request(
    tmp_path: Path, monkeypatch
) -> None:
    """(b) A non-zero ``opencode export`` -> None. Drives _run_export's real exit
    check: with the stub returning code 1 on every call, both the first attempt and
    the one backoff retry fail, so the harvest gives up and reports None (the backoff
    is stubbed to a no-op, so this adds no real delay)."""
    _stub_export(monkeypatch, 1, "irrelevant when the exit is non-zero")
    result = OpencodeAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served is None
    # The exact case Phase 0 exists to catch: never fall back to what we asked for.
    assert result.model_served != OpencodeAdapter.default_model
    assert result.model_served != "opencode/deepseek-v4-flash-free"


def test_export_invalid_json_yields_none(tmp_path: Path, monkeypatch) -> None:
    """(c) Export exits 0 but prints junk -> None, never a guess."""
    _stub_export(monkeypatch, 0, "not json at all {[}")
    result = OpencodeAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served is None


def test_export_json_without_a_model_block_yields_none(
    tmp_path: Path, monkeypatch
) -> None:
    """Valid JSON, but info.model absent -> None (partial data is not a served
    model). Guards the ``provider_id and model_id`` check specifically."""
    _stub_export(monkeypatch, 0, '{"info": {"id": "ses_x"}}')
    result = OpencodeAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served is None


# ---------------------------------------------------------------------------
# Export robustness — timeout + one backoff retry (task-bench-model-gate-none D3)
# The export harvest misses occasionally on a large session; one fixed-backoff
# retry recovers the timing miss, and a hung export (TimeoutExpired) is a failed
# attempt, never a crash. Drives the REAL _run_export -> _served_model -> parse.
# ---------------------------------------------------------------------------


def test_export_first_miss_then_retry_succeeds_recovers_the_model(
    tmp_path: Path, monkeypatch
) -> None:
    """AC7. The realistic timing miss: the first ``opencode export`` exits non-zero
    (session db not flushed yet), the one backoff retry succeeds, and the model is
    recovered end-to-end — subprocess + sleep are the only things stubbed."""
    rec = _stub_export_sequence(
        monkeypatch, [_FakeCompleted(1, ""), _FakeCompleted(0, EXPORT_JSON)]
    )
    result = OpencodeAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served == "opencode/deepseek-v4-flash-free"
    assert len(rec["calls"]) == 2  # it really retried
    assert rec["sleeps"] == 1      # exactly one backoff between the two attempts


def test_export_both_attempts_fail_yields_none(
    tmp_path: Path, monkeypatch
) -> None:
    """AC7. Two non-zero exits in a row -> None (never the requested/default model).
    The retry is best-effort, not a guarantee; D1 downstream degrades this safely."""
    rec = _stub_export_sequence(
        monkeypatch, [_FakeCompleted(1, ""), _FakeCompleted(1, "")]
    )
    result = OpencodeAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served is None
    assert result.model_served != OpencodeAdapter.default_model
    assert len(rec["calls"]) == 2


def test_export_timeout_is_a_failure_not_a_crash(
    tmp_path: Path, monkeypatch
) -> None:
    """AC7. A hung export raises ``subprocess.TimeoutExpired``; that is treated as
    THIS attempt failing (it must not bubble up as a crash), so the retry runs. Here
    the first attempt times out and the second succeeds -> the model is recovered."""
    import subprocess

    rec = _stub_export_sequence(
        monkeypatch,
        [
            subprocess.TimeoutExpired(cmd=["opencode", "export"], timeout=60.0),
            _FakeCompleted(0, EXPORT_JSON),
        ],
    )
    result = OpencodeAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served == "opencode/deepseek-v4-flash-free"
    assert len(rec["calls"]) == 2
    assert rec["sleeps"] == 1


def test_export_timeout_twice_yields_none_without_raising(
    tmp_path: Path, monkeypatch
) -> None:
    """AC7. Two timeouts in a row -> None, and the ``TimeoutExpired`` never escapes
    ``_run_export`` (a bubbled crash would kill the whole card)."""
    import subprocess

    rec = _stub_export_sequence(
        monkeypatch,
        [
            subprocess.TimeoutExpired(cmd=["opencode", "export"], timeout=60.0),
            subprocess.TimeoutExpired(cmd=["opencode", "export"], timeout=60.0),
        ],
    )
    result = OpencodeAdapter().parse(SKILL_STREAM, base=tmp_path)
    assert result.model_served is None
    assert len(rec["calls"]) == 2


# ---------------------------------------------------------------------------
# AC4 — token empty/unknown-shape protection (the silent-zero pitfall)
# ---------------------------------------------------------------------------


def test_no_token_blocks_is_not_observed_rather_than_zero() -> None:
    assert normalize_usage([]) == {}


def test_a_renamed_token_shape_is_not_observed_rather_than_a_fictional_zero() -> None:
    """If opencode renames its counters, defaulting each to 0 would produce a
    TRUTHY all-zero dict that ``_sum_usage`` counts as a free spawn. It must read
    as 'not observed' instead — and land as {} at the counter gate."""
    from harness.batch import _sum_usage

    renamed = [{"promptTokens": 4, "completionTokens": 86}]  # a plausible rename
    assert normalize_usage(renamed) == {}
    assert _sum_usage([normalize_usage(renamed)]) == {}


def test_a_total_only_block_does_not_fabricate_a_zero_breakdown() -> None:
    """``total`` alone is not a breakdown we consume; guarding on it would emit an
    all-zero (truthy) dict. It reads as not-observed instead."""
    assert normalize_usage([{"total": 100}]) == {}


def test_a_partially_recognized_shape_still_sums_what_is_there() -> None:
    """At least one known key = opencode's schema; read what is present."""
    out = normalize_usage([{"input": 4, "output": 86}])
    assert out["input_tokens"] == 4
    assert out["output_tokens"] == 86
    assert out["cache_read_input_tokens"] == 0
    assert out["num_turns"] is None


def test_the_normalized_dict_sums_cleanly_through_the_grand_total() -> None:
    """The internal-key dict is what ``_sum_usage`` is written against — the four
    counters add, the opencode-only reasoning key is simply ignored (harmless)."""
    from harness.batch import _sum_usage

    result, _ = parse_stream(SKILL_STREAM.splitlines())
    bucket = _sum_usage([result.usage])
    assert bucket["input_tokens"] == 8718 + 11823 + 132
    assert bucket["spawns"] == 1
    assert "reasoning_tokens" not in bucket  # unknown to the summer, dropped safely


# ---------------------------------------------------------------------------
# AC5 — isolation (the registry parametrization covers the shared seam; this
#       pins opencode's own prepare)
# ---------------------------------------------------------------------------


def test_opencode_is_a_registered_agent() -> None:
    from harness.agents import AGENT_NAMES

    assert "opencode" in AGENT_NAMES
    assert get_adapter("opencode").name == "opencode"


def test_prepare_isolates_home_registry_and_vault_and_drops_xdg(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    # A set XDG_DATA_HOME points opencode's auth/db at the real ~/.local/share,
    # walking straight past the redirected HOME — prepare() must drop it.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "userhome" / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "userhome" / ".cache"))
    installed: list[Path] = []
    monkeypatch.setattr(
        "harness.agents.opencode.install_repo_skills",
        lambda d, **k: installed.append(Path(d)),
    )
    adapter = OpencodeAdapter()

    env = adapter.prepare(tmp_path, run_vault=tmp_path / "vault")

    assert env["HOME"] == str(tmp_path / "home")
    assert env["LIT_LIBRARY"] == str(tmp_path / "vault")
    assert env["LITMAN_REGISTRY_DIR"] == str(tmp_path / "opencode-registry")
    assert "XDG_DATA_HOME" not in env
    assert "XDG_CACHE_HOME" not in env
    # Skills go to <home>/.agents/skills (measured to activate under a redirect).
    assert installed == [tmp_path / "home" / ".agents" / "skills"]
    # prepare stashed the env for the export subprocess.
    assert adapter._env is env


def test_skills_dir_is_the_agents_skills_dir(tmp_path: Path) -> None:
    assert OpencodeAdapter().skills_dir(tmp_path) == (
        tmp_path / "home" / ".agents" / "skills"
    )


# ---------------------------------------------------------------------------
# AC6 — proxy refusal (backstop; run_bench refuses at the CLI boundary too)
# ---------------------------------------------------------------------------


def test_prepare_rejects_proxy_flags_it_cannot_honor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Anthropic-compatible proxy mode"):
        OpencodeAdapter().prepare(
            tmp_path, run_vault=tmp_path / "vault", base_url="https://proxy.example/v1"
        )


# ---------------------------------------------------------------------------
# AC7 — the compound splitter is the SAME shared helper claude uses (D2)
# ---------------------------------------------------------------------------


def test_opencode_and_claude_share_one_compound_splitter() -> None:
    """D2: the splitter moved to _shell; both adapters import the identical object,
    so there is exactly one implementation of 'how a raw command becomes lit argv'
    across the two agents that see raw commands."""
    import harness.agents.claude as claude_mod
    import harness.agents.opencode as opencode_mod
    from harness.agents._shell import _lit_calls_from_bash

    assert opencode_mod._lit_calls_from_bash is _lit_calls_from_bash
    assert claude_mod._lit_calls_from_bash is _lit_calls_from_bash


def test_shared_splitter_splits_a_two_lit_command_into_two_calls() -> None:
    from harness.agents._shell import _lit_calls_from_bash

    assert _lit_calls_from_bash("lit a && lit b") == [["a"], ["b"]]


# ---------------------------------------------------------------------------
# AC8 — capability honesty (declared, and distinct from agy on served_model)
# ---------------------------------------------------------------------------


def test_capabilities_are_declared_not_inferred() -> None:
    caps = get_adapter("opencode").capabilities
    assert (caps.tokens, caps.turns, caps.served_model, caps.routing) == (
        True, False, True, True,
    )


def test_served_model_capability_distinguishes_opencode_from_agy() -> None:
    """The whole reason opencode is not 'another agy': its model IS recoverable
    (via export), so the Phase 0 pin-check applies to it and not to agy."""
    assert get_adapter("opencode").capabilities.served_model is True
    assert get_adapter("agy").capabilities.served_model is False


# ---------------------------------------------------------------------------
# argv + login seed
# ---------------------------------------------------------------------------


def test_build_argv_pins_the_model_and_carries_the_auto_flag() -> None:
    argv = OpencodeAdapter().build_argv("do a thing", model="opencode/some-model")
    assert argv[1] == "run"
    assert "--format" in argv and "json" in argv
    assert "--model" in argv and "opencode/some-model" in argv
    # opencode's own permission bypass; NOT one of the two forbidden flags.
    assert "--auto" in argv
    assert OpencodeAdapter().permission_flags == ("--auto",)
    # The prompt is the final positional argument.
    assert argv[-1] == "do a thing"


def test_default_model_is_a_free_router_model() -> None:
    """A router's default is only a smoke fallback; a real run always passes
    --model. The default is a loginless free model."""
    assert OpencodeAdapter().default_model == "opencode/deepseek-v4-flash-free"


def test_seed_auth_copies_the_credential_when_present(
    tmp_path: Path, monkeypatch
) -> None:
    user_home = tmp_path / "userhome"
    src = user_home / ".local" / "share" / "opencode" / "auth.json"
    src.parent.mkdir(parents=True)
    # Fabricated, never a real token — this repo is public.
    src.write_text('{"fake": "not-a-real-token"}')
    src.chmod(0o600)
    monkeypatch.setenv("HOME", str(user_home))

    home = tmp_path / "home"
    seed_auth(home)

    seeded = home / ".local" / "share" / "opencode" / "auth.json"
    assert seeded.read_text() == '{"fake": "not-a-real-token"}'
    assert (seeded.stat().st_mode & 0o777) == 0o600  # copy2 keeps the mode


def test_seed_auth_skips_silently_when_absent(tmp_path: Path, monkeypatch) -> None:
    """Unlike agy (which hangs without a token and so must raise), a free opencode
    model runs loginless — a missing auth.json is a valid run, not an error."""
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))

    seed_auth(tmp_path / "home")  # must not raise

    assert not (tmp_path / "home" / ".local" / "share" / "opencode").exists()
