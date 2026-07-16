"""Deterministic tests for run_bench.py's CLI glue (the --run-dir sugar).

Drives ``run_bench.main`` in ``--dry-run`` mode (the fake executor returns 0 with
NO claude -p spawn, M34 §3.5 hard boundary), so the run-dir filing is exercised
end-to-end with zero agent calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import run_bench


def test_run_dir_files_report_and_transcripts(tmp_path: Path) -> None:
    """--run-dir DIR writes DIR/report.json AND DIR/transcripts/ (the sugar)."""
    rd = tmp_path / "debug" / "cc_haiku_260603_0"
    rc = run_bench.main(
        [
            "--model", "claude-haiku-4-5-20251001",
            "--rounds", "1",
            "--cards", "C2-show",
            "--run-dir", str(rd),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert (rd / "report.json").is_file()
    report = json.loads((rd / "report.json").read_text(encoding="utf-8"))
    assert report["model"] == "claude-haiku-4-5-20251001"

    # The transcript dump path is wired through --run-dir (dry-run round → file).
    tdir = rd / "transcripts"
    assert tdir.is_dir()
    assert any("C2-show" in f.name for f in tdir.glob("*.json"))


def test_run_dir_explicit_out_overrides_but_transcripts_still_default(tmp_path: Path) -> None:
    """Explicit --out wins over the run-dir default; --keep-transcript still
    defaults into the run dir (each override is independent)."""
    rd = tmp_path / "debug" / "cc_haiku_260603_1"
    custom = tmp_path / "elsewhere" / "custom.json"
    custom.parent.mkdir(parents=True)
    rc = run_bench.main(
        [
            "--model", "claude-haiku-4-5-20251001",
            "--rounds", "1",
            "--cards", "C2-show",
            "--run-dir", str(rd),
            "--out", str(custom),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert custom.is_file()                  # explicit --out honored
    assert not (rd / "report.json").exists()  # run-dir default not used for report
    assert (rd / "transcripts").is_dir()      # transcript default still applied


# ---------------------------------------------------------------------------
# --base-url is claude-only, and must be refused BEFORE anything spawns
# ---------------------------------------------------------------------------


def test_proxy_flags_are_refused_for_non_claude_agents(capsys) -> None:
    """cursor/agy `prepare()` also raise — but that fires inside the first card,
    after Phase 0 has already burned two live spawns, and surfaces as a bare
    traceback. argparse must refuse it at the boundary instead."""
    import pytest

    for agent in ("cursor", "agy"):
        with pytest.raises(SystemExit) as e:
            run_bench.main(
                ["--agent", agent, "--base-url", "http://localhost:4000", "--dry-run"]
            )
        assert e.value.code == 2  # argparse usage error, not a traceback
        err = capsys.readouterr().err
        assert "claude-only" in err
        assert "--agent claude" in err  # tells the user the way out


def test_auth_token_alone_is_also_refused_for_non_claude(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit):
        run_bench.main(["--agent", "agy", "--auth-token", "tok", "--dry-run"])
    assert "claude-only" in capsys.readouterr().err


def test_proxy_flags_are_still_accepted_for_claude(tmp_path: Path) -> None:
    """The external-model path (6 of the 9 completed production runs) must keep
    working exactly as before."""
    rc = run_bench.main(
        [
            "--agent", "claude",
            "--base-url", "http://localhost:4000",
            "--auth-token", "tok",
            "--model", "deepseek-v4-pro",
            "--rounds", "1",
            "--cards", "C2-show",
            "--out", str(tmp_path / "r.json"),
            "--dry-run",
        ]
    )
    assert rc == 0


def test_agent_appears_in_the_report_json(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    run_bench.main(
        ["--agent", "cursor", "--rounds", "1", "--cards", "C2-show",
         "--out", str(out), "--dry-run"]
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["agent"] == "cursor"
    assert report["model_requested"] == "claude-sonnet-4-6"  # the agent's own default
    assert report["agent_flags"] == ["--force"]


# ---------------------------------------------------------------------------
# Phase 0 gate wiring: a failed qualification aborts the WHOLE run (AC4)
# ---------------------------------------------------------------------------


def test_a_failed_qualification_exits_nonzero_before_any_live_wiring(
    monkeypatch, capsys
) -> None:
    """Live-mode ``main()`` with a failing Phase 0 must exit non-zero and never
    reach the live adapter builders. Every other test here drives --dry-run
    (which skips the gate), so if the SystemExit at the gate were ever softened
    to a print-and-continue, this is the only test that goes red."""
    import pytest

    from harness.qualify import QualCheck, Qualification

    monkeypatch.delenv("LITMAN_BENCH_FAKE", raising=False)
    qual = Qualification(agent="claude", model_requested="claude-haiku-4-5-20251001")
    qual.checks.append(
        QualCheck(name="binary present", status="fail", detail="no such binary")
    )
    monkeypatch.setattr(run_bench, "qualify", lambda *a, **k: qual)

    def _boom(*a, **k):
        raise AssertionError("a live adapter was built despite a failed Phase 0")

    monkeypatch.setattr(run_bench, "build_live_run_card_fn", _boom)
    monkeypatch.setattr(run_bench, "build_live_routing_run_fn", _boom)

    with pytest.raises(SystemExit) as e:
        run_bench.main(["--rounds", "1", "--cards", "C2-show"])  # NO --dry-run
    # SystemExit with a message exits 1; the message itself says why.
    assert e.value.code not in (0, None)
    assert "Phase 0 failed" in str(e.value.code)
    assert "NOT QUALIFIED" in capsys.readouterr().out  # the sheet printed first
