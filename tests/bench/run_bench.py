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
from datetime import datetime
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from harness.agents import AGENT_NAMES, get_adapter  # noqa: E402
from harness.batch import (  # noqa: E402
    BatchAbortedError,
    BenchReport,
    CardScore,
    build_live_routing_run_fn,
    build_live_run_card_fn,
    report_to_dict,
    run_batch,
)
from harness.provenance import (  # noqa: E402
    JOURNAL_NAME,
    append_record,
    baseline_session,
    card_record,
    check_resumable,
    read_records,
    resumable_scores,
    ruler_fingerprint,
    session_record,
)
from harness.qualify import format_qualification, qualification_to_dict, qualify  # noqa: E402
from harness.scenarios import load_all_cards  # noqa: E402
from harness.seeds import GOLDEN_DIR  # noqa: E402

DEFAULT_AGENT = "claude"
DEFAULT_ROUNDS = 3

#: Exit code for a batch that stopped early with valid-but-incomplete data (a
#: spent quota, or the served model changing mid-run). Distinct from Phase 0's
#: failure (1) ON PURPOSE: Phase 0 means nothing ran and nothing was spent, this
#: means cards were measured and are waiting in the journal for a --resume. A
#: launcher that cannot tell those apart cannot decide whether to re-submit.
EXIT_BATCH_ABORTED = 3

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
    served_now: str | None = None
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
        # Phase 0 has already asked the agent which model it serves, before any
        # card. That answer is what a resume must be judged against, and having it
        # HERE is what makes a refusal free: a mismatch caught now costs nothing,
        # while one caught from the first card's run handle has already paid for a
        # card to learn what Phase 0 knew.
        served_now = qual.model_served
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

    # --- provenance: what ruler is this sitting, and may it continue another? ---
    # Built AFTER Phase 0 (it needs the served model) and BEFORE the first card
    # (a refusal must cost nothing). Recorded even for a run that is not resumable
    # and never resumed: report.json has never carried what measured it — the
    # SLURM wrapper's meta.json did, and that file is overwritten by the next
    # submission — so every run from here on states its own instrument.
    session = session_record(
        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        agent=args.agent,
        # Whether anything was spawned at all. Journaled because it is NOT implied
        # by the other fields: a dry run and a live agy run agree on every one of
        # them (agy reports no served model, so both record None), and without this
        # a live sitting could resume a dry journal and adopt its hard-coded zeros
        # as measured cards.
        dry=dry,
        model_requested=args.model,
        model_served=served_now,
        rounds=args.rounds,
        cards=[c.id for c in cards],
        ruler=ruler_fingerprint(),
    )
    sessions = [session]
    prior_scores: list[CardScore] = []
    on_card_done = None
    journal = Path(args.run_dir) / JOURNAL_NAME if args.run_dir else None

    if journal is not None:
        records = read_records(journal)
        baseline = baseline_session(records)
        if records and baseline is None:
            raise SystemExit(
                f"{journal} has card records but no session record naming the "
                "conditions they were measured under, so nothing can be proven "
                "about them. Move it aside and start a fresh run dir."
            )
        if baseline is not None:
            if not args.resume:
                # D6. Both silent readings of this are traps: continuing would
                # hand someone who meant to re-measure a free report identical to
                # last time, and overwriting would throw away cards that were paid
                # for. Neither is worth the convenience of not typing a flag.
                raise SystemExit(
                    f"{journal} already holds a run of {len(resumable_scores(records))} "
                    f"completed card(s) started at {baseline.get('started_at')}. "
                    "Pass --resume to continue it (finished cards are skipped), or "
                    "use a different --run-dir to measure again from scratch. "
                    "Refusing to guess which you meant: one answer silently "
                    "re-reports old numbers, the other silently destroys them."
                )
            why = check_resumable(
                baseline,
                session,
                served_model_verifiable=get_adapter(args.agent).capabilities.served_model,
            )
            if why is not None:
                raise SystemExit(
                    f"--resume refused: {why}\n"
                    f"No cards were run and nothing was spent. The {len(records)} "
                    f"record(s) in {journal} are untouched."
                )
            prior_scores = resumable_scores(records)
            sessions = [r for r in records if r.get("type") == "session"] + [session]
            print(
                f"resuming {journal}: {len(prior_scores)} card(s) already measured "
                f"will be skipped, {len(cards) - len(prior_scores)} to go.\n"
            )
        elif args.resume:
            # Not fatal: unlike D6's two readings, starting fresh here destroys
            # nothing and invents nothing — it only costs a full run. But it is the
            # shape of a mistyped --run-dir, so it must not pass in silence.
            print(
                f"WARNING: --resume was passed but {journal} does not exist; there "
                "is nothing to resume, so this run starts from scratch and pays "
                "for every card. If you meant to continue an earlier run, check "
                "the --run-dir path.\n",
                file=sys.stderr,
            )
        append_record(journal, session)

        def on_card_done(score: CardScore) -> None:
            # Flushed per card: the point is to survive a SIGKILL between two
            # cards, which is exactly how a wall-clock limit ends a run.
            append_record(journal, card_record(score))

    return run_batch(
        cards,
        agent=args.agent,
        model=args.model,
        rounds=args.rounds,
        run_card_fn=run_card_fn,
        routing_run_fn=routing_run_fn,
        qualification=qualification,
        prior_scores=prior_scores,
        on_card_done=on_card_done,
        sessions=sessions,
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
    if report.model_identity == "unverified":
        # Standing property of this agent, printed for every run of it — not a
        # finding about this one. The sentence says what is UNKNOWABLE, never that
        # something happened: we have no evidence the model ever changed, and
        # printing a suspicion where a measurement belongs is the same error as
        # printing a guess as a number.
        lines.append(f"model identity: UNVERIFIED — {report.model_identity_reason}")
    if report.agent_flags:
        lines.append(f"agent flags: {' '.join(report.agent_flags)}")
    if report.sessions and len(report.sessions) > 1:
        # A report stitched from several sittings is still valid, but it is not the
        # same artifact as one taken in a single sitting, and that is the reader's
        # call to make, not ours to hide.
        stamps = ", ".join(str(s.get("started_at")) for s in report.sessions)
        lines.append(
            f"NOTE: stitched from {len(report.sessions)} sessions (resumed): {stamps}"
        )
    lines.append("=" * 60)
    counts = report.coverage.get("counts", {})
    lines.append(
        "coverage: "
        + ", ".join(f"{k}={v}" for k, v in counts.items())
    )
    errored = report.coverage.get("errored_cards") or []
    trr_line = (
        f"TRR (auto-scored, n={report.coverage.get('trr_denominator', 0)}): "
        f"{report.trr_mean:.3f} +/- {report.trr_std:.3f}"
    )
    if errored:
        # The denominator moved, so say so ON the line that reports it. A reader
        # comparing this TRR to last week's must not have to notice a count in a
        # different paragraph to learn that n cards never ran.
        trr_line += f"  [denominator excludes {len(errored)} errored card(s)]"
    lines.append(trr_line)
    # Always printed, including the 0 case: "errored: 0" is the sentence that
    # makes "errored: 7" legible to someone who has never seen it non-zero.
    lines.append(
        f"errored (did NOT run — excluded from TRR, never scored 0): {len(errored)}"
        + (f" — {', '.join(errored)}" if errored else "")
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
        elif c.error:
            # NEVER "mean=0.000" here. Its mean is a placeholder for a card that
            # did not run, and printing it next to the cards that did is the whole
            # bug in one line of output.
            lines.append(
                f"  [      ERRORED] {c.card_id}: DID NOT RUN "
                f"(reason={c.error.get('reason')} "
                f"exit_code={c.error.get('exit_code')} "
                f"timed_out={c.error.get('timed_out')}) "
                f"— not scored, excluded from TRR"
            )
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
        "--resume",
        action="store_true",
        help=(
            "continue the run already journaled in --run-dir: execution cards that "
            "finished are skipped, cards that errored are retried, and the report "
            "covers the whole corpus. Routing (I-*) cards are re-run every time — "
            "their per-utterance trail is not journaled, so skipping them would "
            "quietly shrink the RA denominator; budget for those spawns. Refuses "
            "if anything about the instrument changed (agent / model / rounds / "
            "cards / dry-vs-live / litman / scenarios / harness)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise the pipeline with a non-live fake executor (no claude -p)",
    )
    args = parser.parse_args(argv)

    # The journal lives in the run dir; without one there is nothing to resume
    # from and nowhere to resume to.
    if args.resume and not args.run_dir:
        parser.error("--resume needs --run-dir: the journal it continues lives there.")

    # The proxy flags need an agent that honors ANTHROPIC_BASE_URL. The adapters
    # reject them too, but that fires inside the first card — AFTER Phase 0 has
    # already burned two live spawns — and surfaces as a bare traceback. Refuse at
    # the boundary instead, before anything is spawned.
    #
    # Asks the adapter rather than testing the name: a hard-coded `!= "claude"`
    # silently auto-refuses every agent added after it was written — including one
    # that does support a proxy — and the refusal reads like someone's decision.
    if (args.base_url is not None or args.auth_token is not None) and not get_adapter(
        args.agent
    ).supports_anthropic_proxy:
        supported = [n for n in AGENT_NAMES if get_adapter(n).supports_anthropic_proxy]
        # Guarded: with no proxy-capable agent registered, the join is empty and
        # the sentence trails off into "or use ." — a way out that names nothing.
        way_out = (
            "Drop them, or use " + " / ".join("--agent " + n for n in supported) + "."
            if supported
            else "Drop them — no registered agent has one."
        )
        parser.error(
            f"--base-url / --auth-token need an agent with an Anthropic-compatible "
            f"proxy mode; --agent {args.agent} has none. {way_out}"
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

    try:
        report = build_report(args)
    except BatchAbortedError as e:
        # D4: NO report.json. The batch measured real cards and they are safe in
        # the journal, but "report.json" has exactly one meaning — one complete
        # measurement — and a file that sometimes means half a run is a file whose
        # TRR will eventually be quoted by someone who did not read the fine print.
        print(f"\nBATCH ABORTED: {e}", file=sys.stderr)
        if not args.run_dir:
            print(
                "Nothing was journaled, because this run had no --run-dir: the "
                "cards it measured are gone. Use --run-dir next time so an abort "
                "is resumable.",
                file=sys.stderr,
            )
        elif e.detail.get("reason") == "model_changed":
            # NOT "--resume". The gate re-checks the served model at Phase 0 and
            # refuses a mismatch by design (D1), so telling this user to resume is
            # telling them to spend a live Phase 0 to be told no. Say what will
            # actually happen instead of the generic advice.
            print(
                f"The {e.completed} card(s) in {Path(args.run_dir) / JOURNAL_NAME} "
                f"are still valid — every one of them was served by "
                f"{e.detail.get('model_baseline')!r}. But this run dir can only be "
                f"resumed while that model is being served again: --resume re-checks "
                f"the served model at Phase 0 and refuses (correctly) while it "
                f"reports {e.detail.get('model_observed')!r}. If the change is "
                f"permanent — which is what a downgraded quota looks like — start a "
                f"NEW run dir and measure again. Do not try to make one report out "
                f"of two rulers. No report.json was written.",
                file=sys.stderr,
            )
        else:
            print(
                f"The journal at {Path(args.run_dir) / JOURNAL_NAME} holds every "
                f"card measured so far. Re-submit the same command with --resume "
                f"in this run dir to continue; no finished card is paid for twice, "
                f"and the cards that errored are retried. No report.json was "
                f"written: it means a complete run, and this was not one.",
                file=sys.stderr,
            )
        raise SystemExit(EXIT_BATCH_ABORTED) from e

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
