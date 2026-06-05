#!/usr/bin/env python
"""Standalone litman-bench runner (M34 §3.5 item 1).

A thin CLI over :mod:`harness.batch`. Runs every non-skipped card N rounds
against a chosen model, prints the per-card mean(resolved) + TRR(mean±std) + an
honest coverage report, and optionally dumps the full report to JSON. This is
the "one command to swap the model and run the whole suite" UX.

Routing accuracy (RA): in LIVE mode the CLI also builds a ``routing_run_fn``
(:func:`harness.batch.build_live_routing_run_fn`) that runs each I-* routing
utterance through the executor and reports RA per the routing section of the
report. That adapter spawns ``claude -p`` per utterance, so it is wired only on
the live path and is NEVER exercised in ``--dry-run`` / tests. In ``--dry-run``
``routing_run_fn`` is ``None``: routing cards are still tagged + counted, but RA
is left unscored and the report's routing section is ``None`` (an honest "not
scored", never a fabricated 0).

It is a plain script under ``tests/bench/`` (NOT a ``lit`` subcommand — decided):
the bench harness is dev tooling, not a user-facing litman feature.

Live runs build the executor adapter via
:func:`harness.batch.build_live_run_card_fn` (the SOLE executor touchpoint — M34
§3.6.A: this CLI never hardwires ``stream-json`` / ``claude``; ``--model`` is a
pass-through value). For each round the adapter builds/locates the card's seed,
``cp``s it into a fresh disposable run vault under a ``/tmp`` work root (NOT
``/work`` — §4.6 EDQUOT), spawns ``claude -p``, and the run vault is removed after
scoring. ``--base-url`` / ``--auth-token`` select the external-model auth mode
(M34 §3.6.B); the default (no ``--base-url``) is Anthropic OAuth and is
byte-identical to before.

To keep the CLI testable without a live agent (M34 §3.5 hard boundary),
``--dry-run`` (or ``LITMAN_BENCH_FAKE=1``) routes every round through a fake
scorer that returns 0 without spawning anything. Use the live path ONLY under
explicit user authorization (Phase G), never inside /dev.

Usage::

    python tests/bench/run_bench.py --model claude-sonnet-4-6 --rounds 3
    python tests/bench/run_bench.py --cards F1-export-bib --rounds 1 --dry-run
    python tests/bench/run_bench.py --out report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from harness.batch import (  # noqa: E402
    BenchReport,
    build_live_routing_run_fn,
    build_live_run_card_fn,
    report_to_dict,
    run_batch,
)
from harness.scenarios import load_all_cards  # noqa: E402
from harness.seeds import GOLDEN_DIR  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_ROUNDS = 3

# Bench dir layout (M34 §0): fixtures committed under the bench tree; the
# disposable run vaults live under /tmp (NOT /work — §4.6 EDQUOT).
FIXTURES_PDFS_DIR = BENCH_DIR / "fixtures" / "pdfs"
SEEDS_CACHE_ROOT = Path("/tmp/litman-bench-seeds")
WORK_ROOT = Path("/tmp")


def _fake_run_card(card, *, round, model, **_kw):  # noqa: A002 - mirror run_card kw
    """A non-live stand-in for ``run_card`` (``--dry-run`` / LITMAN_BENCH_FAKE).

    Returns ``0`` (unresolved) without spawning any process, so the CLI exercises
    the full batch/aggregation/coverage path with zero agent calls. The int is
    short-circuited by :func:`harness.batch._score_one`.
    """
    return 0


def build_report(args: argparse.Namespace) -> BenchReport:
    cards = load_all_cards()
    if args.cards:
        wanted = {c.strip() for c in args.cards.split(",")}
        cards = [c for c in cards if c.id in wanted]
        if not cards:
            raise SystemExit(f"no cards matched --cards {args.cards!r}")

    dry = args.dry_run or os.environ.get("LITMAN_BENCH_FAKE") == "1"
    # Routing token sink: filled by the live routing adapter (one usage dict per
    # classification spawn), read back by run_batch into the report's grand
    # total. None in dry mode (no routing spawns).
    routing_usage: list[dict] | None = None
    if dry:
        # Non-live: a fake execution scorer + NO routing_run_fn, so routing cards
        # are tagged/counted but RA stays unscored (the routing seam would spawn
        # claude -p, banned in /dev). The report's routing section is None.
        run_card_fn = _fake_run_card
        routing_run_fn = None
    else:
        # Live (Phase G authorization ONLY): both adapters are the SOLE executor
        # touchpoints (M34 §3.6.A). model / base_url / auth_token pass straight
        # through, not interpreted here. The routing adapter spawns claude -p per
        # utterance, so it is built + wired structurally but NEVER exercised inside
        # /dev — it fires only under explicit live authorization.
        routing_usage = []
        run_card_fn = build_live_run_card_fn(
            fixtures_pdfs_dir=FIXTURES_PDFS_DIR,
            seeds_dir=SEEDS_CACHE_ROOT,
            work_root=WORK_ROOT,
            base_url=args.base_url,
            auth_token=args.auth_token,
        )
        routing_run_fn = build_live_routing_run_fn(
            seeds_dir=SEEDS_CACHE_ROOT,
            work_root=WORK_ROOT,
            base_url=args.base_url,
            auth_token=args.auth_token,
            usage_sink=routing_usage,
        )

    transcript_dir = Path(args.keep_transcript) if args.keep_transcript else None

    return run_batch(
        cards,
        model=args.model,
        rounds=args.rounds,
        run_card_fn=run_card_fn,
        routing_run_fn=routing_run_fn,
        # FIX A: the auto-scorer (harness.checker.resolve) needs golden_dir as a
        # required kwarg (pdf_eq dereferences golden_dir.parent/"pdfs"). The live
        # handle carries no golden_dir, so thread the committed fixtures here.
        score_kwargs={"golden_dir": GOLDEN_DIR},
        # Opt-in debug aid: when set, each round's transcript (commands + final
        # answer + per-assertion trail) is dumped here before the run vault is
        # removed. Unset (default) leaves the scoring path byte-identical.
        transcript_dir=transcript_dir,
        # Routing spawns' tokens (filled above during the run) fold into the
        # report's grand total alongside the execution cards.
        routing_usage_sink=routing_usage,
    )


def format_report(report: BenchReport) -> str:
    lines: list[str] = []
    lines.append(f"litman-bench report  model={report.model}  rounds={report.rounds}")
    lines.append("=" * 60)
    counts = report.coverage.get("counts", {})
    lines.append(
        "coverage: "
        + ", ".join(f"{k}={v}" for k, v in counts.items())
    )
    lines.append(
        f"TRR (auto-scored, n={report.coverage.get('trr_denominator', 0)}): "
        f"{report.trr_mean:.3f} +/- {report.trr_std:.3f}"
    )
    routing = report.routing
    if routing is None:
        lines.append("RA (routing): not scored (no routing_run_fn — dry/non-live run)")
    else:
        lines.append(
            f"RA (routing, n={routing['scored']}): {routing['ra']:.3f}  "
            f"(miss={routing['miss']} spurious={routing['spurious']} na={routing['na']})"
        )
    if report.tokens:
        lines.append(_format_tokens(report.tokens))
    lines.append("-" * 60)
    for c in report.cards:
        if c.tag in ("skipped", "routing", "multi-turn"):
            lines.append(f"  [{c.tag:>13}] {c.card_id}")
        else:
            line = f"  [{c.tag:>13}] {c.card_id}: mean={c.mean:.3f} rounds={c.rounds}"
            if c.usage:
                line += f"  [tok in={c.usage.get('input_tokens', 0):,}" \
                        f" out={c.usage.get('output_tokens', 0):,}" \
                        f" cache_r={c.usage.get('cache_read_input_tokens', 0):,}]"
            lines.append(line)
    return "\n".join(lines)


def _format_tokens(tokens: dict) -> str:
    """One-block token summary: grand total + the auto/routing split.

    Breaks input into fresh (``in``), cache-read (``cache_r``), and cache-write
    (``cache_w``) so cost can be computed at each tier's own price; output (``out``)
    is separate. No dollar figure is shown: against an external proxy the CLI's
    cost is mispriced, so cost is left to be derived from these counters x the
    provider's real per-token prices.
    """
    def fmt(b: dict | None, label: str) -> str:
        if not b:
            return f"  {label:>11}: (none)"
        return (
            f"  {label:>11}: in={b.get('input_tokens', 0):,}"
            f"  cache_r={b.get('cache_read_input_tokens', 0):,}"
            f"  cache_w={b.get('cache_creation_input_tokens', 0):,}"
            f"  out={b.get('output_tokens', 0):,}"
            f"  spawns={b.get('spawns', 0)}"
        )

    out = ["tokens (input / output / cache — apply provider prices for $):"]
    out.append(fmt(tokens.get("total"), "TOTAL"))
    out.append(fmt(tokens.get("auto_scored"), "auto-scored"))
    out.append(fmt(tokens.get("routing"), "routing"))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the litman-bench suite.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="executor model tier")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="repeats per card")
    parser.add_argument("--cards", default=None, help="comma-separated card ids to run")
    parser.add_argument("--out", default=None, help="write the full report JSON here")
    parser.add_argument(
        "--run-dir",
        default=None,
        metavar="DIR",
        help=(
            "group this run's artifacts under DIR: writes DIR/report.json + "
            "DIR/transcripts/ (sugar for --out DIR/report.json --keep-transcript "
            "DIR/transcripts). Explicit --out / --keep-transcript override. The "
            "launcher (submit.sh) builds DIR; this flag just files everything inside it."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="external-model proxy base URL (M34 §3.6.B); unset -> Anthropic OAuth",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help="auth token for the external-model proxy (used only with --base-url)",
    )
    parser.add_argument(
        "--keep-transcript",
        default=None,
        metavar="DIR",
        help=(
            "opt-in debug: dump each round's transcript (commands + final answer "
            "+ per-assertion trail) to DIR before the run vault is removed; unset "
            "leaves the scoring path unchanged"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise the pipeline with a non-live fake executor (no claude -p)",
    )
    args = parser.parse_args(argv)

    # --run-dir is sugar: file report.json + transcripts/ under one dir. Explicit
    # --out / --keep-transcript still win (lower-level one-off override).
    if args.run_dir:
        run_dir = Path(args.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        if args.out is None:
            args.out = str(run_dir / "report.json")
        if args.keep_transcript is None:
            args.keep_transcript = str(run_dir / "transcripts")

    report = build_report(args)
    print(format_report(report))

    if args.out:
        Path(args.out).write_text(
            json.dumps(report_to_dict(report), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
