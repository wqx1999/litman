"""Deterministic tests for the executor stream parser (Phase E plumbing).

These NEVER spawn a live ``claude -p`` agent (M34 §3.5 hard boundary). They run
:func:`harness.agents.claude.parse_stream` over a hand-authored recorded
stream-json fixture and exercise ``_lit_calls_from_bash`` / ``as_jsonl_records`` /
``stdout_blob`` on stubbed data only.

The claude-specific pieces moved to :mod:`harness.agents.claude` when the executor
became agent-neutral; the BEHAVIOUR under test here is unchanged, which is the
point — a live TRR/RA baseline exists for this path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import executor as executor_mod
from harness.agents import claude as claude_mod
from harness.agents.claude import _lit_calls_from_bash, executor_env, parse_stream
from harness.executor import (
    ACTIVE_VAULT_NAME,
    ExecutorResult,
    LitCall,
    ToolResult,
    neutral_cwd_for,
    register_active_vault,
    run_card,
    stdout_blob,
)
from harness.scenarios import Card
from harness.seeds import LIT_BIN

STREAMS_DIR = Path(__file__).resolve().parent / "fixtures" / "streams"


def _sample_lines() -> list[str]:
    return (STREAMS_DIR / "sample_run.jsonl").read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# parse_stream on the recorded fixture
# ---------------------------------------------------------------------------


def test_parse_stream_captures_skill_routing() -> None:
    result = parse_stream(_sample_lines())
    assert result.skills == ["lit-library"]


def test_parse_stream_captures_lit_calls() -> None:
    result = parse_stream(_sample_lines())
    argvs = [c.argv for c in result.lit_calls]
    # First Bash: `lit list --year 2023,2024`
    assert ["list", "--year", "2023,2024"] in argvs
    # Second Bash issues `lit show ...` inside a compound command; the `cd` and
    # `rm` segments must NOT be captured as lit calls.
    assert ["show", "2023_Guntuboina_PeptideBERT"] in argvs
    assert all("cd" not in argv and "rm" not in argv for argv in argvs)


def test_parse_stream_captures_tool_results() -> None:
    result = parse_stream(_sample_lines())
    assert len(result.tool_results) == 2
    # The list-output result block (list-of-text-blocks content shape).
    list_tr = next(tr for tr in result.tool_results if "PeptideBERT" in tr.content)
    assert "Multi-Peptide" in list_tr.content
    assert list_tr.tool == "Bash"  # tagged from the originating Bash tool_use id


def test_parse_stream_captures_final_text() -> None:
    result = parse_stream(_sample_lines())
    assert "2023" in result.final_text
    assert "Guntuboina" in result.final_text


def test_parse_stream_tolerates_blank_and_garbage_lines() -> None:
    lines = ["", "   ", "not json at all", *_sample_lines()]
    result = parse_stream(lines)
    # Garbage lines are skipped; the real events still parse.
    assert result.skills == ["lit-library"]
    assert result.final_text


# ---------------------------------------------------------------------------
# _lit_calls_from_bash (compound-command splitter)
# ---------------------------------------------------------------------------


def test_lit_calls_simple() -> None:
    assert _lit_calls_from_bash("lit list") == [["list"]]


def test_lit_calls_compound_separators() -> None:
    cmd = "lit add x.pdf && lit list ; echo done | lit show id"
    calls = _lit_calls_from_bash(cmd)
    assert ["add", "x.pdf"] in calls
    assert ["list"] in calls
    assert ["show", "id"] in calls
    # `echo done` is not a lit call.
    assert ["done"] not in calls


def test_lit_calls_skips_env_assignment() -> None:
    calls = _lit_calls_from_bash("LIT_LIBRARY=/tmp/v lit list --status inbox")
    assert calls == [["list", "--status", "inbox"]]


def test_lit_calls_absolute_path_binary() -> None:
    calls = _lit_calls_from_bash("/work/env/bin/lit export -o refs.bib")
    assert calls == [["export", "-o", "refs.bib"]]


def test_lit_calls_non_lit_command_ignored() -> None:
    assert _lit_calls_from_bash("cat papers/x/metadata.yaml") == []


# ---------------------------------------------------------------------------
# as_jsonl_records: stdout pairing
# ---------------------------------------------------------------------------


def test_as_jsonl_records_pairs_stdout_by_id() -> None:
    """A widened jsonl record carries each lit call's captured stdout."""
    result = parse_stream(_sample_lines())
    records = result.as_jsonl_records()
    list_rec = next(r for r in records if r["argv"] == ["list", "--year", "2023,2024"])
    assert "PeptideBERT" in list_rec["stdout"]
    assert "Multi-Peptide" in list_rec["stdout"]
    show_rec = next(
        r for r in records if r["argv"] == ["show", "2023_Guntuboina_PeptideBERT"]
    )
    assert "Guntuboina" in show_rec["stdout"]


def test_as_jsonl_records_unmappable_stdout_is_empty() -> None:
    """A lit call with no matching tool_result carries '' stdout (best-effort)."""
    result = ExecutorResult(
        lit_calls=[LitCall(argv=["list"], raw="lit list", tool_use_id="missing")],
        tool_results=[ToolResult(tool="Bash", content="x", tool_use_id="other")],
    )
    records = result.as_jsonl_records()
    assert records == [{"argv": ["list"], "raw": "lit list", "stdout": ""}]


def test_stdout_blob_joins_all_tool_results() -> None:
    result = parse_stream(_sample_lines())
    blob = stdout_blob(result)
    assert "PeptideBERT" in blob
    assert "Multi-Peptide" in blob
    assert "Guntuboina" in blob


# ---------------------------------------------------------------------------
# stdout scoring on a STUBBED ExecutorResult (the executor-stdout judging path)
# ---------------------------------------------------------------------------


def test_stdout_contains_verb_on_widened_records(tmp_path: Path) -> None:
    """The checker's stdout_contains greps the widened jsonl records."""
    from harness.checker import check_assertion

    result = ExecutorResult(
        lit_calls=[LitCall(argv=["list"], raw="lit list", tool_use_id="b1")],
        tool_results=[
            ToolResult(tool="Bash", content="#4 PeptideBERT\n#5 Multi-Peptide", tool_use_id="b1")
        ],
    )
    jsonl = result.as_jsonl_records()
    vault = tmp_path / "vault"
    vault.mkdir()
    r = check_assertion(
        "stdout_contains: ~Multi-Peptide", vault=vault, jsonl=jsonl, golden_dir=tmp_path
    )
    assert r.passed, r.detail
    miss = check_assertion(
        "stdout_contains: ~DiffDock", vault=vault, jsonl=jsonl, golden_dir=tmp_path
    )
    assert not miss.passed


def test_answer_contains_verb_on_stubbed_run(tmp_path: Path) -> None:
    from harness.checker import check_assertion

    run = ExecutorResult(final_text="PeptideBERT was published in 2023 by Guntuboina.")
    vault = tmp_path / "vault"
    vault.mkdir()
    r = check_assertion(
        "answer_contains: ~2023", vault=vault, jsonl=[], golden_dir=tmp_path, run=run
    )
    assert r.passed, r.detail
    # No run threaded -> hard fail, never silent pass (invariant #14).
    no_run = check_assertion(
        "answer_contains: ~2023", vault=vault, jsonl=[], golden_dir=tmp_path
    )
    assert not no_run.passed
    assert "no run threaded" in no_run.detail


# ---------------------------------------------------------------------------
# executor_env auth modes (FIX 2 / M34 §3.6.B) — no spawn
# ---------------------------------------------------------------------------


def test_executor_env_default_anthropic_mode(tmp_path: Path) -> None:
    """Default (no base_url) sets the four isolation vars, no proxy vars."""
    env = executor_env(
        tmp_path / "vault", tmp_path / "reg", tmp_path / "cfg", tmp_path / "home"
    )
    assert env["LIT_LIBRARY"] == str(tmp_path / "vault")
    assert env["LITMAN_REGISTRY_DIR"] == str(tmp_path / "reg")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert env["HOME"] == str(tmp_path / "home")
    # External-mode vars are absent in the default mode.
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_executor_env_external_mode_sets_proxy_vars(tmp_path: Path) -> None:
    """External mode (base_url set) also exports the proxy base URL + token."""
    env = executor_env(
        tmp_path / "vault",
        tmp_path / "reg",
        tmp_path / "cfg",
        tmp_path / "home",
        base_url="https://proxy.example/v1",
        auth_token="tok-xyz",
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example/v1"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok-xyz"
    # The default isolation vars are unchanged.
    assert env["LIT_LIBRARY"] == str(tmp_path / "vault")
    assert env["HOME"] == str(tmp_path / "home")


def test_executor_env_external_mode_without_token(tmp_path: Path) -> None:
    """base_url with no token sets the URL but no token var (best-effort slot)."""
    env = executor_env(
        tmp_path / "vault", tmp_path / "reg", tmp_path / "cfg", tmp_path / "home",
        base_url="https://proxy.example/v1",
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example/v1"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


# ---------------------------------------------------------------------------
# run_card auth branching: seed_auth conditional (FIX 2) — NEVER spawns claude
# ---------------------------------------------------------------------------


def _stub_run_card_io(monkeypatch, calls: dict) -> None:
    """Stub everything in run_card that would touch the network / a live agent.

    ``seed_auth`` becomes a spy (records its call), ``install_repo_skills`` a
    no-op, and ``subprocess.run`` returns a canned empty stream so NO ``claude``
    process is ever spawned (M34 §3.5 hard boundary). Both spies are patched on
    :mod:`harness.agents.claude`, which is where the adapter calls them from.
    """
    def spy_seed_auth(config_dir):
        calls["seed_auth"] = calls.get("seed_auth", 0) + 1

    monkeypatch.setattr(claude_mod, "seed_auth", spy_seed_auth)
    monkeypatch.setattr(claude_mod, "install_repo_skills", lambda *a, **k: None)

    class _Proc:
        stdout = ""
        returncode = 0

    def fake_subprocess_run(*a, **k):
        calls["spawned_argv"] = a[0] if a else k.get("args")
        return _Proc()

    monkeypatch.setattr(executor_mod.subprocess, "run", fake_subprocess_run)


def test_run_card_default_mode_calls_seed_auth(tmp_path: Path, monkeypatch) -> None:
    """Default Anthropic mode copies OAuth credentials via seed_auth."""
    calls: dict = {}
    _stub_run_card_io(monkeypatch, calls)

    run_vault = tmp_path / "bench-x" / "vault"
    run_vault.mkdir(parents=True)
    card = Card(id="A1", intent="do a thing", fixtures=[])
    run_card(card, run_vault, fixtures_pdfs_dir=tmp_path / "pdfs")

    assert calls.get("seed_auth") == 1  # OAuth path taken
    # Sanity: a claude argv was assembled but the process was stubbed, not run.
    assert calls["spawned_argv"][0]  # the (fake) claude binary name


def test_run_card_external_mode_skips_seed_auth(tmp_path: Path, monkeypatch) -> None:
    """External mode (base_url set) skips seed_auth — proxy auth, no OAuth."""
    calls: dict = {}
    _stub_run_card_io(monkeypatch, calls)

    run_vault = tmp_path / "bench-y" / "vault"
    run_vault.mkdir(parents=True)
    card = Card(id="A1", intent="do a thing", fixtures=[])
    run_card(
        card,
        run_vault,
        fixtures_pdfs_dir=tmp_path / "pdfs",
        base_url="https://proxy.example/v1",
        auth_token="tok",
    )

    assert "seed_auth" not in calls  # OAuth skipped in external mode


def test_neutral_cwd_for_convention(tmp_path: Path) -> None:
    """neutral_cwd_for is the single source of truth for <run_dir>/cwd."""
    run_vault = tmp_path / "bench-z" / "vault"
    assert neutral_cwd_for(run_vault) == tmp_path / "bench-z" / "cwd"


# ---------------------------------------------------------------------------
# The REAL adapters, driven end-to-end through run_card (AC8 / the M34 lesson)
# ---------------------------------------------------------------------------
#
# The bench is full of injectable seams (run_card_impl / observe_impl / score_fn),
# and the lesson that earned this section is that a suite can be entirely green
# with every seam filled by a fake while the real default underneath is broken.
# So: below, the ONLY thing replaced is the agent spawn itself — and it is replaced
# with a REAL recorded stream. Dispatch, the adapter's prepare/isolation, argv
# assembly and parsing are all production code.

AGENT_STREAMS_DIR = Path(__file__).resolve().parent / "fixtures" / "agent-streams"


def _replay(monkeypatch, calls: dict, stdout: str) -> None:
    """Stub ONLY the spawn; replay a recorded stream as the agent's stdout.

    Rebinds the NAME ``harness.executor.subprocess`` rather than reaching through
    it to patch ``subprocess.run``: the latter is the shared stdlib module object,
    so patching there also silently stubs the test's own subprocess calls — which
    is exactly how the agy shim below first appeared to record nothing.
    """

    class _Proc:
        returncode = 0

        def __init__(self, out: str) -> None:
            self.stdout = out

    def fake_subprocess_run(*a, **k):
        calls["argv"] = a[0] if a else k.get("args")
        calls["env"] = k.get("env")
        calls["cwd"] = k.get("cwd")
        return _Proc(stdout)

    monkeypatch.setattr(
        executor_mod,
        "subprocess",
        SimpleNamespace(
            run=fake_subprocess_run,
            DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )


def test_run_card_drives_the_real_cursor_adapter(tmp_path: Path, monkeypatch) -> None:
    """A real recorded cursor stream, through the real CursorAdapter, via run_card."""
    from harness.agents import cursor as cursor_mod

    # install_repo_skills shells out to `lit`; not what is under test here.
    monkeypatch.setattr(cursor_mod, "install_repo_skills", lambda *a, **k: None)
    # An empty fake user HOME: the auth seed must skip silently, and the test
    # must never read the user's real ~/.config/cursor/auth.json.
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    stream = (AGENT_STREAMS_DIR / "cursor-shell-lit-version.raw.jsonl").read_text(
        encoding="utf-8"
    )
    calls: dict = {}
    _replay(monkeypatch, calls, stream)

    run_vault = tmp_path / "bench-c" / "vault"
    run_vault.mkdir(parents=True)
    result = run_card(
        Card(id="A1", intent="check the version", fixtures=[]),
        run_vault,
        fixtures_pdfs_dir=tmp_path / "pdfs",
        agent="cursor",
    )

    # (a) the real adapter built cursor's argv, with its permission flag.
    assert calls["argv"][0].endswith("cursor-agent")
    assert "--force" in calls["argv"]
    assert "--model" in calls["argv"] and "claude-sonnet-4-6" in calls["argv"]
    # (b) the real adapter isolated the run: HOME redirected, library shadowed.
    assert calls["env"]["HOME"] == str(tmp_path / "bench-c" / "home")
    assert calls["env"]["LIT_LIBRARY"] == str(run_vault)
    # (c) cwd is the neutral dir under the run root (always local /tmp in prod).
    assert calls["cwd"] == str(tmp_path / "bench-c" / "cwd")
    # (d) the real cursor parser recovered the evidence from cursor's own shape.
    assert [c.argv for c in result.lit_calls] == [["--version"]]
    assert result.usage["input_tokens"] == 4          # camelCase normalized
    assert result.model_served == "Sonnet 4.6 200K Medium No Thinking"
    # (e) the harness recorded its own argv: the stream cannot be trusted for it.
    assert result.argv == calls["argv"]


def test_run_card_drives_the_real_agy_adapter(tmp_path: Path, monkeypatch) -> None:
    """agy has no stream at all, so the real evidence path is the PATH shim: this
    drives the real adapter and reads back what the real shim wrote."""
    import json

    from harness.agents import agy as agy_mod

    # A fake logged-in user HOME (fabricated token; Path.home() follows $HOME),
    # so seed_auth never sees the user's real credential file.
    user_home = tmp_path / "userhome"
    token = user_home / agy_mod.TOKEN_RELPATH
    token.parent.mkdir(parents=True)
    token.write_text('{"auth_method": "fake", "token": {"access_token": "fake"}}')
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setattr(agy_mod, "install_repo_skills", lambda *a, **k: None)
    monkeypatch.setattr(agy_mod, "LIT_BIN", Path("/bin/echo"))

    calls: dict = {}
    _replay(monkeypatch, calls, "I listed your papers.\n")

    base = tmp_path / "bench-a"
    run_vault = base / "vault"
    run_vault.mkdir(parents=True)

    result = run_card(
        Card(id="A1", intent="list my papers", fixtures=[]),
        run_vault,
        fixtures_pdfs_dir=tmp_path / "pdfs",
        agent="agy",
    )

    # (a) `-p` last, permission flag present — the real adapter's argv.
    assert calls["argv"][-2] == "-p"
    assert "--dangerously-skip-permissions" in calls["argv"]
    assert "Claude Sonnet 4.6 (Thinking)" in calls["argv"]  # agy's OWN default
    # (b) the shim really landed, first on the child's PATH.
    shim = base / "shim" / "lit"
    assert shim.is_file() and os.access(shim, os.X_OK)
    assert calls["env"]["PATH"].startswith(f"{base / 'shim'}:")
    # (c) drive the shim the way the agent would, then re-parse: the evidence the
    # adapter reports is whatever the shim actually recorded.
    subprocess.run([str(shim), "list"], capture_output=True, text=True, env=calls["env"])
    from harness.agents import get_adapter

    after = get_adapter("agy").parse("I listed your papers.", base=base)
    assert [c.argv for c in after.lit_calls] == [["list"]]
    assert json.loads((base / "lit-calls.jsonl").read_text())["argv"] == ["list"]
    # (d) unmeasurable axes stay absent, never zero.
    assert result.usage == {}
    assert result.model_served is None


def test_run_card_claude_argv_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    """The incumbent path, pinned. A live TRR/RA baseline exists for exactly this
    argv + evidence mechanism; drifting it would silently make the old numbers
    incomparable to the new ones rather than fail anything."""
    from harness.agents import claude as claude_mod

    monkeypatch.setattr(claude_mod, "seed_auth", lambda *a, **k: None)
    monkeypatch.setattr(claude_mod, "install_repo_skills", lambda *a, **k: None)
    calls: dict = {}
    _replay(monkeypatch, calls, "")

    run_vault = tmp_path / "bench-k" / "vault"
    run_vault.mkdir(parents=True)
    run_card(
        Card(id="A1", intent="do a thing", fixtures=[]),
        run_vault,
        fixtures_pdfs_dir=tmp_path / "pdfs",
    )

    assert calls["argv"][1:] == [
        "-p", "do a thing",
        "--model", "claude-sonnet-4-6",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
    ]
    assert calls["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "bench-k" / "claude-config")
    assert calls["env"]["LIT_LIBRARY"] == str(run_vault)


def test_run_card_defaults_the_model_per_agent(tmp_path: Path, monkeypatch) -> None:
    """One shared default across three model namespaces would silently serve a
    different model per agent."""
    from harness.agents import agy as agy_mod

    # Fake logged-in user HOME with a fabricated token (never the real file).
    user_home = tmp_path / "userhome"
    token = user_home / agy_mod.TOKEN_RELPATH
    token.parent.mkdir(parents=True)
    token.write_text('{"auth_method": "fake", "token": {"access_token": "fake"}}')
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setattr(agy_mod, "install_repo_skills", lambda *a, **k: None)
    monkeypatch.setattr(agy_mod, "LIT_BIN", Path("/bin/echo"))
    calls: dict = {}
    _replay(monkeypatch, calls, "ok")

    run_vault = tmp_path / "bench-m" / "vault"
    run_vault.mkdir(parents=True)
    run_card(
        Card(id="A1", intent="x", fixtures=[]),
        run_vault,
        fixtures_pdfs_dir=tmp_path / "pdfs",
        agent="agy",
    )
    assert "Claude Sonnet 4.6 (Thinking)" in calls["argv"]
    assert "claude-sonnet-4-6" not in calls["argv"]


def test_on_prepared_fires_between_isolation_and_spawn(tmp_path: Path, monkeypatch) -> None:
    """Phase 0's seam: the sentinel must land in the isolated skills copy AFTER the
    adapter installed it and BEFORE the agent starts."""
    from harness.agents import claude as claude_mod

    monkeypatch.setattr(claude_mod, "seed_auth", lambda *a, **k: None)
    monkeypatch.setattr(claude_mod, "install_repo_skills", lambda *a, **k: None)
    # Registration is an orthogonal side effect that also shells out; no-op it so
    # its subprocess.run does not read as a spawn in the ordering under test.
    monkeypatch.setattr(executor_mod, "register_active_vault", lambda *a, **k: None)
    order: list[str] = []

    class _Proc:
        stdout = ""
        returncode = 0

    def fake_subprocess_run(*a, **k):
        order.append("spawn")
        return _Proc()

    monkeypatch.setattr(executor_mod.subprocess, "run", fake_subprocess_run)

    run_vault = tmp_path / "bench-h" / "vault"
    run_vault.mkdir(parents=True)
    run_card(
        Card(id="A1", intent="x", fixtures=[]),
        run_vault,
        fixtures_pdfs_dir=tmp_path / "pdfs",
        on_prepared=lambda base, env: order.append("prepared"),
    )
    assert order == ["prepared", "spawn"]


# ---------------------------------------------------------------------------
# register_active_vault: the run's registry stops lying to the agent
# ---------------------------------------------------------------------------
#
# The isolation aims LIT_LIBRARY at the run vault but leaves the registry empty,
# so `lit vault list` reports "No vaults registered" while `lit list`/`export`
# resolve fine — a self-contradicting environment that scores an agent's
# correct-given-the-lie refusal as a failure. register_active_vault registers the
# run vault as active so the two views agree, matching a real machine (exactly one
# active vault). It registers ONLY a real vault (lit-config.yaml present); the
# Phase 0 qualification probe's bare `_probe_base` vault has none and is skipped.
#
# AC1 / AC4-v2 / AC4b-v2 drive REAL lit (the inject-seam lesson: a helper whose
# only job is to shell out must be proven against the real binary, not just a fake
# proc). AC-qual-v2 and AC3 pin the run_card wiring with the spawn faked.


def _real_lit_env(run_vault: Path, registry: Path) -> dict[str, str]:
    """The self-contradicting env the isolation produces: LIT_LIBRARY at the run
    vault, registry redirected at an empty throwaway dir."""
    return {
        **os.environ,
        "LIT_LIBRARY": str(run_vault),
        "LITMAN_REGISTRY_DIR": str(registry),
    }


def test_register_active_vault_makes_the_run_vault_active(tmp_path: Path) -> None:
    """AC1 — real lit: registration flips `lit vault list` from 'No vaults
    registered' to showing the run vault as the single ACTIVE one, and `--use`
    really took effect (lit resolves it with LIT_LIBRARY cleared)."""
    import json

    registry = tmp_path / "reg"
    registry.mkdir()
    parent = tmp_path / "runroot"
    parent.mkdir()

    # A REAL vault, deliberately NOT registered — the empty-registry state the
    # isolation produces (seeds build with `lit init --no-register`).
    init = subprocess.run(
        [str(LIT_BIN), "init", str(parent), "--name", "bench-runvault", "--no-register"],
        env={**os.environ, "LITMAN_REGISTRY_DIR": str(registry)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, init.stderr or init.stdout
    run_vault = parent / "bench-runvault"
    env = _real_lit_env(run_vault, registry)

    # Before: lit authoritatively lies — the vault is reachable via LIT_LIBRARY
    # but the registry is empty.
    before = subprocess.run(
        [str(LIT_BIN), "vault", "list"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert "No vaults registered" in before.stdout

    register_active_vault(run_vault, env)

    # After: the run vault shows up and the lie is gone.
    after = subprocess.run(
        [str(LIT_BIN), "vault", "list"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert "No vaults registered" not in after.stdout
    assert ACTIVE_VAULT_NAME in after.stdout

    # Structured proof: exactly one vault, named `bench`, at the run path, active.
    rows = json.loads(
        subprocess.run(
            [str(LIT_BIN), "vault", "list", "--format", "json"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert len(rows) == 1
    assert rows[0]["name"] == ACTIVE_VAULT_NAME
    assert rows[0]["path"] == str(run_vault)
    assert rows[0]["is_active"] is True

    # `--use` really took effect: with LIT_LIBRARY cleared, lit still resolves the
    # vault through the active-registry entry (active, not merely registered).
    reg_only = {k: v for k, v in env.items() if k != "LIT_LIBRARY"}
    resolved = subprocess.run(
        [str(LIT_BIN), "list"],
        env=reg_only,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert resolved.returncode == 0, resolved.stderr or resolved.stdout


def test_register_active_vault_skips_a_bare_probe_vault(tmp_path: Path) -> None:
    """AC4-v2 — real lit: the Phase 0 qualification probe's vault is a bare `mkdir`
    dir with no lit-config.yaml (`_probe_base`'s shape). It is not a vault to
    register, so register_active_vault returns None without raising and leaves the
    registry empty. Reverse-verify: delete the `if not ...: return` guard in
    executor.register_active_vault and this goes RED — real `lit vault add` rejects
    the bare dir with exit 1, so the helper raises again."""
    from harness.qualify import _probe_base

    _, vault = _probe_base(tmp_path)  # exactly the bare probe shape the gate uses
    assert not (vault / "lit-config.yaml").exists()
    registry = tmp_path / "reg"
    registry.mkdir()
    env = _real_lit_env(vault, registry)

    assert register_active_vault(vault, env) is None  # returns, does not raise

    # The registry was never touched — `lit vault list` still reports it empty.
    listing = subprocess.run(
        [str(LIT_BIN), "vault", "list"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert "No vaults registered" in listing.stdout


def test_register_active_vault_raises_when_a_real_vault_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """AC4b-v2 — the guard skips ONLY a non-vault; a directory that IS a vault
    (real `lit init`, lit-config.yaml present) whose `lit vault add` still exits
    non-zero is a broken seed and must raise, surfacing the exit code. Proves the
    guard did not swallow real registration failures. subprocess is faked to force
    the non-zero exit, so the guard's on-disk lit-config.yaml check passes on the
    real vault while the add 'fails'."""
    registry = tmp_path / "reg"
    registry.mkdir()
    parent = tmp_path / "runroot"
    parent.mkdir()
    init = subprocess.run(
        [str(LIT_BIN), "init", str(parent), "--name", "bench-runvault", "--no-register"],
        env={**os.environ, "LITMAN_REGISTRY_DIR": str(registry)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, init.stderr or init.stdout
    run_vault = parent / "bench-runvault"
    assert (run_vault / "lit-config.yaml").is_file()  # the guard will let this pass

    class _Proc:
        stdout = "boom"
        returncode = 1

    monkeypatch.setattr(executor_mod.subprocess, "run", lambda *a, **k: _Proc())

    with pytest.raises(RuntimeError) as excinfo:
        register_active_vault(run_vault, _real_lit_env(run_vault, registry))
    msg = str(excinfo.value)
    assert "could not register the run vault as active" in msg
    assert "exited 1" in msg  # the non-zero exit is surfaced


def test_run_card_survives_a_bare_probe_vault(tmp_path: Path, monkeypatch) -> None:
    """AC-qual-v2 — anti-regression main anchor: the qualification gate drives the
    REAL run_card against a bare `_probe_base` vault (no lit-config.yaml). run_card
    must NOT raise from registration — the guard skips a non-vault — and must
    return an ExecutorResult normally. The spawn is faked via _replay (so no live
    agent, and the real `lit vault add` never runs here); the real-lit proof that a
    bare vault is skipped is AC4-v2. Together they cover the live path v1 missed."""
    from harness.agents import claude as claude_mod
    from harness.qualify import _probe_base

    monkeypatch.setattr(claude_mod, "seed_auth", lambda *a, **k: None)
    monkeypatch.setattr(claude_mod, "install_repo_skills", lambda *a, **k: None)
    calls: dict = {}
    _replay(monkeypatch, calls, "")

    _, vault = _probe_base(tmp_path)  # the bare shape the gate hands run_card
    assert not (vault / "lit-config.yaml").exists()
    result = run_card(
        Card(id="qual-probe", intent="do a thing", fixtures=[]),
        vault,
        fixtures_pdfs_dir=tmp_path / "pdfs",
    )
    assert isinstance(result, ExecutorResult)


def test_run_card_registers_the_active_vault(tmp_path: Path, monkeypatch) -> None:
    """AC3 — wiring: run_card calls register_active_vault exactly once, with the
    run vault and the isolated env (LIT_LIBRARY aimed at that vault). Spawn is
    stubbed as usual so no live agent starts."""
    calls: dict = {}
    _stub_run_card_io(monkeypatch, calls)

    recorded: list[tuple] = []

    def spy_register(run_vault, env):
        recorded.append((Path(run_vault), dict(env)))

    monkeypatch.setattr(executor_mod, "register_active_vault", spy_register)

    run_vault = tmp_path / "bench-reg" / "vault"
    run_vault.mkdir(parents=True)
    run_card(
        Card(id="A1", intent="do a thing", fixtures=[]),
        run_vault,
        fixtures_pdfs_dir=tmp_path / "pdfs",
    )

    assert len(recorded) == 1
    got_vault, got_env = recorded[0]
    assert got_vault == run_vault
    assert got_env["LIT_LIBRARY"] == str(run_vault)
