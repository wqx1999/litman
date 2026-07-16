#!/usr/bin/env python
"""Standalone litman-bench runner (M34 §3.5 item 1).

A thin CLI over :mod:`harness.batch`. Runs every non-skipped card N rounds
against a chosen ``(agent, model)`` pair, prints the per-card mean(resolved) +
TRR(mean±std) + an honest coverage report, and optionally dumps the full report to
JSON. This is the "one command to swap the agent/model and run the whole suite"
UX: ``--agent`` picks the scaffold (``claude`` / ``cursor`` / ``agy``), and a
controlled comparison is the same ``--model`` run three times.

Before any card runs, LIVE mode qualifies the instrument
(:func:`harness.qualify.qualify`) against the chosen agent and aborts non-zero if
any check fails — a broken instrument produces numbers that look real, so it must
never get as far as producing them. The qualification sheet is carried into
``report.json``.

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
    python tests/bench/run_bench.py --agent cursor --model claude-sonnet-4-6
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

from harness.agents import AGENT_NAMES, get_adapter  # noqa: E402
from harness.batch import (  # noqa: E402
    BenchReport,
    build_live_routing_run_fn,
    build_live_run_card_fn,
    report_to_dict,
    run_batch,
)
from harness.qualify import format_qualification, qualification_to_dict, qualify  # noqa: E402
from harness.scenarios import load_all_cards  # noqa: E402
from harness.seeds import GOLDEN_DIR  # noqa: E402

DEFAULT_AGENT = "claude"
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
    qualification: dict | None = None
    if dry:
        # Non-live: a fake execution scorer + NO routing_run_fn, so routing cards
        # are tagged/counted but RA stays unscored (the routing seam would spawn a
        # live agent, banned in /dev). The report's routing section is None.
        run_card_fn = _fake_run_card
        routing_run_fn = None
    else:
        # Phase 0 FIRST: the instrument has to be qualified before it is allowed
        # to produce numbers. Any failed check aborts here, before token spend.
        # base_url / auth_token MUST come along: the probes have to run under the
        # same auth the cards will. Qualifying an external-model run against the
        # default Anthropic endpoint asks the user's own OAuth for a model it has
        # never heard of, gets rejected, and gates a run that was perfectly fine.
        qual = qualify(
            args.agent,
            model=args.model,
            work_root=WORK_ROOT,
            base_url=args.base_url,
            auth_token=args.auth_token,
        )
        print(format_qualification(qual))
        print()
        qualification = qualification_to_dict(qual)
        if not qual.ok:
            raise SystemExit(
                f"Phase 0 failed for agent={args.agent}: the instrument is not "
                "qualified, so no cards were run and no tokens were spent. Fix the "
                "FAIL lines above and re-submit."
            )
        # Live (Phase G authorization ONLY): both adapters are the SOLE executor
        # touchpoints (M34 §3.6.A). agent / model / base_url / auth_token pass
        # straight through, not interpreted here. The routing adapter spawns a live
        # agent per utterance, so it is built + wired structurally but NEVER
        # exercised inside /dev — it fires only under explicit live authorization.
        routing_usage = []
        run_card_fn = build_live_run_card_fn(
            fixtures_pdfs_dir=FIXTURES_PDFS_DIR,
            seeds_dir=SEEDS_CACHE_ROOT,
            work_root=WORK_ROOT,
            agent=args.agent,
            base_url=args.base_url,
            auth_token=args.auth_token,
        )
        routing_run_fn = build_live_routing_run_fn(
            seeds_dir=SEEDS_CACHE_ROOT,
            work_root=WORK_ROOT,
            agent=args.agent,
            base_url=args.base_url,
            auth_token=args.auth_token,
            usage_sink=routing_usage,
        )

    transcript_dir = Path(args.keep_transcript) if args.keep_transcript else None

    return run_batch(
        cards,
        agent=args.agent,
        model=args.model,
        rounds=args.rounds,
        run_card_fn=run_card_fn,
        routing_run_fn=routing_run_fn,
        qualification=qualification,
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
    lines.append(
        f"litman-bench report  agent={report.agent}  "
        f"model={report.model_requested}  rounds={report.rounds}"
    )
    # The served string verbatim: same weights through different products is NOT
    # the same conditions (different system prompts, thinking on/off, own proxies),
    # so the reader gets the raw string and judges for themselves. Absent for two
    # different reasons — say which, rather than blame the agent for a dry run.
    if report.model_served:
        served = report.model_served
    elif not get_adapter(report.agent).capabilities.served_model:
        served = "(this agent never reports it — UNVERIFIED)"
    else:
        served = "(none observed — no live run)"
    # `model_family is None` likewise has two causes: nothing to look up, versus
    # looked up and not found. Only the second is a table gap.
    if report.model_family:
        family = report.model_family
    elif report.model_served or not get_adapter(report.agent).capabilities.served_model:
        family = "(unknown — not in the lookup table)"
    else:
        family = "(nothing observed a model)"
    lines.append(f"model served: {served}   family: {family}")
    if report.agent_flags:
        lines.append(f"agent flags: {' '.join(report.agent_flags)}")
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
        # Two different absences; say which one, never print a 0.
        if report.coverage.get("routing_ra") == "not_measurable":
            lines.append(
                f"RA (routing): NOT MEASURABLE for {report.agent} — "
                f"{report.coverage.get('routing_ra_reason', '')}"
            )
        else:
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
    parser.add_argument(
        "--agent",
        default=DEFAULT_AGENT,
        choices=AGENT_NAMES,
        help="which agent CLI to drive (default: claude)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "executor model tier; defaults to the chosen agent's own default "
            "(the three agents do NOT share a model namespace)"
        ),
    )
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

    # The proxy flags are claude-only. The adapters reject them too, but that fires
    # inside the first card — AFTER Phase 0 has already burned two live spawns —
    # and surfaces as a bare traceback. Refuse at the boundary instead, before
    # anything is spawned.
    if (args.base_url is not None or args.auth_token is not None) and args.agent != "claude":
        parser.error(
            f"--base-url / --auth-token are claude-only (the external-model proxy "
            f"mode); --agent {args.agent} has no Anthropic-compatible proxy mode. "
            f"Drop them, or use --agent claude."
        )

    # Resolve the model against the CHOSEN agent, not a shared module constant:
    # `claude-sonnet-4-6` is meaningless to agy, whose models are display names.
    if args.model is None:
        args.model = get_adapter(args.agent).default_model

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
