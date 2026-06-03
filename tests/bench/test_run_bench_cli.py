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
