"""Deterministic tests for the executor stream parser (Phase E plumbing).

These NEVER spawn a live ``claude -p`` agent (M34 §3.5 hard boundary). They run
:func:`harness.executor.parse_stream` over a hand-authored recorded stream-json
fixture and exercise ``_lit_calls_from_bash`` / ``as_jsonl_records`` /
``stdout_blob`` on stubbed data only.
"""

from __future__ import annotations

from pathlib import Path

from harness import executor as executor_mod
from harness.executor import (
    ExecutorResult,
    LitCall,
    ToolResult,
    _lit_calls_from_bash,
    executor_env,
    neutral_cwd_for,
    parse_stream,
    run_card,
    stdout_blob,
)
from harness.scenarios import Card

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
    """Default (no base_url) sets the three isolation vars, no proxy vars."""
    env = executor_env(tmp_path / "vault", tmp_path / "reg", tmp_path / "cfg")
    assert env["LIT_LIBRARY"] == str(tmp_path / "vault")
    assert env["LITMAN_REGISTRY_DIR"] == str(tmp_path / "reg")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    # External-mode vars are absent in the default mode.
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_executor_env_external_mode_sets_proxy_vars(tmp_path: Path) -> None:
    """External mode (base_url set) also exports the proxy base URL + token."""
    env = executor_env(
        tmp_path / "vault",
        tmp_path / "reg",
        tmp_path / "cfg",
        base_url="https://proxy.example/v1",
        auth_token="tok-xyz",
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example/v1"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok-xyz"
    # The default isolation vars are unchanged.
    assert env["LIT_LIBRARY"] == str(tmp_path / "vault")


def test_executor_env_external_mode_without_token(tmp_path: Path) -> None:
    """base_url with no token sets the URL but no token var (best-effort slot)."""
    env = executor_env(
        tmp_path / "vault", tmp_path / "reg", tmp_path / "cfg",
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
    process is ever spawned (M34 §3.5 hard boundary).
    """
    def spy_seed_auth(config_dir):
        calls["seed_auth"] = calls.get("seed_auth", 0) + 1

    monkeypatch.setattr(executor_mod, "seed_auth", spy_seed_auth)
    monkeypatch.setattr(executor_mod, "install_repo_skills", lambda *a, **k: None)

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
