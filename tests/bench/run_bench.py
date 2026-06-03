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
        )

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
    lines.append("-" * 60)
    for c in report.cards:
        if c.tag in ("skipped", "routing"):
            lines.append(f"  [{c.tag:>13}] {c.card_id}")
        else:
            lines.append(
                f"  [{c.tag:>13}] {c.card_id}: mean={c.mean:.3f} rounds={c.rounds}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the litman-bench suite.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="executor model tier")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="repeats per card")
    parser.add_argument("--cards", default=None, help="comma-separated card ids to run")
    parser.add_argument("--out", default=None, help="write the full report JSON here")
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
        "--dry-run",
        action="store_true",
        help="exercise the pipeline with a non-live fake executor (no claude -p)",
    )
    args = parser.parse_args(argv)

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
