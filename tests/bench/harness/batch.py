"""Batch runner + aggregation (M34 §3.5 item 1).

Drives every non-skipped card N rounds, scores each round, and folds the results
into a :class:`BenchReport`: per-card mean(resolved), the overall TRR(mean±std)
over auto-scored execution cards, routing accuracy (RA) aggregated on its own
axis, and an HONEST coverage report tagging each card.

This module never spawns a live agent itself. The per-round run is delegated to
``run_card_fn`` and the score to ``score_fn`` (default
:func:`harness.checker.resolve`); both are injected so the tests pass
deterministic fakes (M34 §3.5 hard boundary — no live ``claude -p``).

Routing accuracy (RA) is scored ONLY when an explicit ``routing_run_fn`` is
passed (the live executor adapter in Phase G, a fake in tests):
:func:`run_batch` calls it per routing card to obtain the observed skill per
case, scores each card via :func:`harness.routing.score_routing`, and aggregates
into the report's ``routing`` section. With ``routing_run_fn=None`` (a dry /
execution-only run) routing cards are still tagged + counted but RA is left
unscored and the ``routing`` section is ``None`` — never a fabricated 0
(invariant #14 no-silent-skip spirit).

The live ``run_card_fn`` is built by :func:`build_live_run_card_fn`, the adapter
that bridges the batch loop to the executor: for each round it builds/locates the
card's seed, ``cp``s it into a fresh disposable run vault, calls
:func:`harness.executor.run_card` (the SOLE executor touchpoint — M34 §3.6.A:
``batch`` / ``run_bench`` never hardwire ``--model`` / ``stream-json`` /
``claude`` literals; the model name is a pass-through parameter), and returns a
scoreable handle. The run vault survives until scoring finishes, then
:func:`run_batch` calls the handle's ``_cleanup`` (run-vault lifecycle, M34
§3.0 layer 2). The live path is exercised ONLY under Phase G authorization, never
inside /dev (tests inject fakes for ``run_card_impl`` / ``ensure_seed_impl``).

Coverage tags (``coverage_tag``):

* ``skipped``       — the card carries a ``skip_reason`` (needs_network /
  needs_pty) and is excluded from every metric.
* ``routing``       — a routing card (``layer == "routing"``); scored by RA, not
  TRR.
* ``prose-blocked`` — an execution card with at least one un-mechanizable prose
  line in ``expected_end_state`` (cannot be fully auto-scored; excluded from TRR
  so the number stays honest — invariant #14 no-silent-skip spirit).
* ``auto-scored``   — an execution card whose ``expected_end_state`` is fully
  DSL; it counts toward TRR.

Only ``auto-scored`` EXECUTION cards contribute to TRR. Routing accuracy is
reported on its own axis; prose-blocked and skipped cards are surfaced in the
coverage dict but never silently fold into a passing number.
"""

from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.checker import _split_assertion, resolve
from harness.routing import RoutingResult, score_routing


def _card_field(card: Any, name: str) -> Any:
    if isinstance(card, dict):
        return card.get(name)
    return getattr(card, name, None)


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------


@dataclass
class CardScore:
    """Per-card aggregate over N rounds."""

    card_id: str
    tag: str
    rounds: list[int]
    mean: float


@dataclass
class BenchReport:
    """The full batch report (M34 §6.3 deterministic subset).

    ``routing`` is the aggregated routing-accuracy section (overall RA + summed
    misroute / miss / spurious / na + per-card RA), or ``None`` when no routing
    card was scored (no ``routing_run_fn`` was passed to :func:`run_batch`). A
    ``None`` section reads as an honest "RA not scored", never a fake 0.
    """

    model: str
    rounds: int
    trr_mean: float
    trr_std: float
    cards: list[CardScore]
    coverage: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Coverage classification
# ---------------------------------------------------------------------------


def _has_prose_line(card: Any) -> bool:
    """True if any ``expected_end_state`` line is un-mechanizable prose.

    A line is prose when :func:`harness.checker._split_assertion` cannot map it
    to a known verb (returns verb ``None``). Such a card can never be fully
    auto-scored, so it is excluded from TRR.
    """
    for line in _card_field(card, "expected_end_state") or []:
        verb, _ = _split_assertion(line)
        if verb is None:
            return True
    return False


def coverage_tag(card: Any) -> str:
    """Classify a card into one of the four coverage buckets."""
    if _card_field(card, "skip_reason"):
        return "skipped"
    if _card_field(card, "layer") == "routing":
        return "routing"
    if _has_prose_line(card):
        return "prose-blocked"
    return "auto-scored"


# ---------------------------------------------------------------------------
# Live executor adapter (M34 §3.6.A — the SOLE bridge to the executor)
# ---------------------------------------------------------------------------


def build_live_run_card_fn(
    *,
    fixtures_pdfs_dir: Path,
    seeds_dir: Path,
    work_root: Path,
    base_url: str | None = None,
    auth_token: str | None = None,
    run_card_impl: Callable[..., Any] | None = None,
    ensure_seed_impl: Callable[..., Path] | None = None,
) -> Callable[..., dict]:
    """Build the live ``run_card_fn`` the batch loop calls (one per round).

    Bridges the batch contract ``run_card_fn(card, *, round, model, **_)`` to the
    executor contract ``run_card(card, run_vault, *, fixtures_pdfs_dir, model,
    ...) -> ExecutorResult`` (M34 §3.6.A — this is the ONLY place that touches the
    executor, and it hardwires no ``--model`` / ``stream-json`` / ``claude``
    literal; ``model`` is passed straight through).

    Each returned ``_run`` call:

    1. resolves the card's ``seed`` name (a card field; routing/skipped cards do
       not reach here so a missing seed is a real error),
    2. builds/locates that seed deterministically via ``ensure_seed_impl``
       (default :func:`harness.seeds.build_seed`, cached under ``seeds_dir``) and
       ``cp``s it into a fresh disposable run vault via
       :class:`harness.runlit.RunVault` (its copytree + rmtree are reused — no
       hand-rolled cp/rm),
    3. calls ``run_card_impl`` (default :func:`harness.executor.run_card`) with
       the run vault + ``model`` / ``base_url`` / ``auth_token`` passed through,
    4. returns a scoreable handle ``{vault, jsonl, cwd, run, _cleanup}`` —
       ``cwd`` is :func:`harness.executor.neutral_cwd_for` (where ``lit export``
       drops ``refs.bib``); ``_cleanup`` rm's the whole run dir (called by
       :func:`run_batch` AFTER scoring, so the vault survives the check).

    ``run_card_impl`` / ``ensure_seed_impl`` are injectable so the tests drive the
    full adapter with a canned :class:`ExecutorResult` + a tmp seed dir, never
    spawning a live agent (M34 §3.5 hard boundary).
    """
    from harness.executor import neutral_cwd_for
    from harness.runlit import RunVault

    if run_card_impl is None:
        from harness.executor import run_card as run_card_impl  # type: ignore[assignment]
    if ensure_seed_impl is None:
        from harness.seeds import build_seed

        def _default_ensure_seed(name: str) -> Path:
            return build_seed(name, cache_root=Path(seeds_dir))

        ensure_seed_impl = _default_ensure_seed

    def _run(card: Any, *, round: int, model: str, **_: Any) -> dict:  # noqa: A002
        seed_name = _card_field(card, "seed")
        if not seed_name:
            raise ValueError(
                f"card {_card_field(card, 'id')!r} has no seed; cannot build a run vault"
            )
        seed_vault = ensure_seed_impl(str(seed_name))

        rv = RunVault(Path(seed_vault), run_root=Path(work_root))
        # FIX B: once __enter__ has done the copytree, the run dir exists on disk.
        # If run_card_impl raises BEFORE we return the handle (install_repo_skills
        # RuntimeError, claude-bin FileNotFoundError, ...), run_batch never gets
        # the handle, so its _cleanup never fires and the copied vault leaks under
        # /tmp (one full vault per round). Tear down here and re-raise; __exit__ is
        # idempotent + ignore_errors=True, so it is safe to call eagerly.
        rv.__enter__()  # cp seed -> <work_root>/bench-<uuid>/vault
        try:
            run_vault = rv.vault
            result = run_card_impl(
                card,
                run_vault,
                fixtures_pdfs_dir=Path(fixtures_pdfs_dir),
                model=model,
                base_url=base_url,
                auth_token=auth_token,
            )
            return {
                "vault": run_vault,
                "jsonl": result.as_jsonl_records(),
                "cwd": neutral_cwd_for(run_vault),
                "run": result,
                "_cleanup": rv.__exit__,  # rm -rf the whole run dir
            }
        except BaseException:
            rv.__exit__(*sys.exc_info())
            raise

    return _run


def build_live_routing_run_fn(
    *,
    seeds_dir: Path,
    work_root: Path,
    routing_seed: str = "seed-empty",
    base_url: str | None = None,
    auth_token: str | None = None,
    observe_impl: Callable[..., str | None] | None = None,
    ensure_seed_impl: Callable[..., Path] | None = None,
) -> Callable[..., list[str | None]]:
    """Build the live ``routing_run_fn`` :func:`run_batch` calls per routing card.

    Bridges the batch contract ``routing_run_fn(card, *, model) -> list[str|None]``
    (one observed skill per case, in ``card.cases`` order) to the executor's
    per-utterance probe (M34 §3.6.A — like the execution adapter, this is the ONLY
    place that touches the executor for routing, and it hardwires no ``--model`` /
    ``stream-json`` / ``claude`` literal; ``model`` is passed straight through).

    Each case routes against its own fresh disposable run vault (a ``cp`` of
    ``routing_seed`` — routing is pure classification, so a minimal initialized
    vault suffices) so the cases never interfere; the run dir is removed after the
    probe. ``observe_impl`` (default :func:`harness.executor.observe_skill_for_utterance`)
    is injectable so the tests drive the adapter with canned skills, never spawning
    a live agent (M34 §3.5 hard boundary). It spawns ``claude -p`` per utterance in
    production, exercised ONLY under Phase G authorization.
    """
    from harness.runlit import RunVault

    if observe_impl is None:
        from harness.executor import observe_skill_for_utterance as observe_impl  # type: ignore[assignment]
    if ensure_seed_impl is None:
        from harness.seeds import build_seed

        def _default_ensure_seed(name: str) -> Path:
            return build_seed(name, cache_root=Path(seeds_dir))

        ensure_seed_impl = _default_ensure_seed

    def _run(card: Any, *, model: str, **_: Any) -> list[str | None]:
        seed_vault = ensure_seed_impl(str(routing_seed))
        observed: list[str | None] = []
        for case in _card_field(card, "cases") or []:
            utt = _case_field(case, "utt")
            rv = RunVault(Path(seed_vault), run_root=Path(work_root))
            rv.__enter__()  # cp seed -> <work_root>/bench-<uuid>/vault
            try:
                skill = observe_impl(
                    str(utt),
                    rv.vault,
                    fixtures_pdfs_dir=Path(work_root) / "_routing_no_fixtures",
                    model=model,
                    base_url=base_url,
                    auth_token=auth_token,
                )
            finally:
                rv.__exit__()  # routing probe leaves nothing to score; rm now
            observed.append(skill)
        return observed

    return _run


def _case_field(case: Any, name: str) -> Any:
    if isinstance(case, dict):
        return case.get(name)
    return getattr(case, name, None)


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------


def run_batch(
    cards: list[Any],
    *,
    model: str,
    rounds: int,
    run_card_fn: Callable[..., Any] | None = None,
    routing_run_fn: Callable[..., list[str | None]] | None = None,
    score_fn: Callable[..., tuple[int, list]] = resolve,
    run_kwargs: dict[str, Any] | None = None,
    score_kwargs: dict[str, Any] | None = None,
) -> BenchReport:
    """Run every non-skipped EXECUTION card ``rounds`` times and aggregate.

    For each auto-scored / prose-blocked execution card and each round, calls
    ``run_card_fn(card, round=i, model=model, **run_kwargs)`` (the live adapter
    from :func:`build_live_run_card_fn` in production, a fake in tests) to obtain
    a run handle, then ``score_fn(card, **score_kwargs, ...)`` to fold it to
    ``resolved ∈ {0,1}``. The live adapter returns a ``{vault, jsonl, cwd, run,
    _cleanup}`` handle consumed by :func:`harness.checker.resolve`; a fake may
    return an ``int`` / ``(resolved, trail)`` / a smaller dict.

    ``score_kwargs`` is spread into every ``score_fn`` call. The live path MUST
    pass ``score_kwargs={"golden_dir": ...}`` because the default
    :func:`harness.checker.resolve` takes ``golden_dir`` as a required kwarg (the
    live handle does not carry it); a fake ``score_fn`` that short-circuits before
    ``resolve`` does not need it.

    Scoring is wrapped in try/finally: when the handle carries a ``_cleanup``
    callable (the live adapter's run-vault remover) it is invoked AFTER
    ``_score_one``, so the disposable run vault survives the check and is then
    removed (run-vault lifecycle, M34 §3.0 layer 2).

    Routing cards are tagged ``routing`` and recorded with an empty rounds list
    (they never contribute to TRR). Routing ACCURACY is scored on its own axis
    ONLY when ``routing_run_fn`` is provided: for each routing card it is called as
    ``routing_run_fn(card, model=model)`` to obtain the observed skill per case (in
    ``card.cases`` order), then :func:`harness.routing.score_routing` scores the
    card against its in-scope skills and the per-card :class:`RoutingResult` is
    folded into the report's ``routing`` section. With ``routing_run_fn=None`` (a
    dry / execution-only run) RA is left unscored and ``report.routing`` is
    ``None`` — never a fabricated 0 (invariant #14 no-silent-skip spirit).

    Skipped cards are recorded with their tag and excluded from all metrics.
    Only ``auto-scored`` cards contribute to ``trr_mean`` / ``trr_std``.
    """
    if run_card_fn is None:
        raise ValueError(
            "run_batch needs an explicit run_card_fn: build the live adapter via "
            "build_live_run_card_fn(...) or pass a fake (M34 §3.6.A)."
        )
    run_kwargs = run_kwargs or {}
    score_kwargs = score_kwargs or {}

    card_scores: list[CardScore] = []
    counts = {"auto-scored": 0, "prose-blocked": 0, "routing": 0, "skipped": 0}
    auto_means: list[float] = []
    routing_results: list[tuple[str, RoutingResult]] = []

    for card in cards:
        cid = str(_card_field(card, "id"))
        tag = coverage_tag(card)
        counts[tag] += 1

        if tag == "skipped":
            card_scores.append(CardScore(card_id=cid, tag=tag, rounds=[], mean=0.0))
            continue

        if tag == "routing":
            card_scores.append(CardScore(card_id=cid, tag=tag, rounds=[], mean=0.0))
            if routing_run_fn is not None:
                observed = routing_run_fn(card, model=model)
                rr = score_routing(
                    card,
                    observed,
                    present_skills=_card_field(card, "in_scope_skills") or [],
                )
                routing_results.append((cid, rr))
            continue

        round_scores: list[int] = []
        for i in range(rounds):
            run = run_card_fn(card, round=i, model=model, **run_kwargs)
            try:
                resolved, _trail = _score_one(card, run, score_fn, score_kwargs)
            finally:
                _maybe_cleanup(run)
            round_scores.append(int(resolved))

        mean = sum(round_scores) / len(round_scores) if round_scores else 0.0
        card_scores.append(
            CardScore(card_id=cid, tag=tag, rounds=round_scores, mean=mean)
        )
        if tag == "auto-scored":
            auto_means.append(mean)

    trr_mean = statistics.fmean(auto_means) if auto_means else 0.0
    # Population-style spread; with <2 cards std is 0 (single sample has no spread).
    trr_std = statistics.pstdev(auto_means) if len(auto_means) >= 2 else 0.0

    coverage = {
        "counts": counts,
        "auto_scored_cards": [c.card_id for c in card_scores if c.tag == "auto-scored"],
        "prose_blocked_cards": [c.card_id for c in card_scores if c.tag == "prose-blocked"],
        "routing_cards": [c.card_id for c in card_scores if c.tag == "routing"],
        "skipped_cards": [c.card_id for c in card_scores if c.tag == "skipped"],
        "trr_denominator": len(auto_means),
    }

    return BenchReport(
        model=model,
        rounds=rounds,
        trr_mean=trr_mean,
        trr_std=trr_std,
        cards=card_scores,
        coverage=coverage,
        routing=_aggregate_routing(routing_results),
    )


def _aggregate_routing(
    results: list[tuple[str, RoutingResult]],
) -> dict[str, Any] | None:
    """Fold per-card :class:`RoutingResult`s into the report's ``routing`` section.

    Returns ``None`` when no routing card was scored (``routing_run_fn`` absent),
    so the report reads "RA not scored" rather than a fake 0. Otherwise: overall
    RA = total hits / (hits + misses) across all scored cases (``na`` excluded),
    summed miss / spurious / na, the scored-case count, and per-card RA.
    """
    if not results:
        return None
    hit = sum(rr.hit for _, rr in results)
    miss = sum(rr.miss for _, rr in results)
    spurious = sum(rr.spurious for _, rr in results)
    na = sum(rr.na for _, rr in results)
    scored = hit + miss
    return {
        "ra": hit / scored if scored else 0.0,
        "hit": hit,
        "miss": miss,
        "spurious": spurious,
        "na": na,
        "scored": scored,
        "per_card": {cid: rr.ra for cid, rr in results},
    }


def _score_one(
    card: Any,
    run: Any,
    score_fn: Callable[..., tuple[int, list]],
    score_kwargs: dict[str, Any],
) -> tuple[int, list]:
    """Adapt a run handle to ``score_fn``.

    The live adapter returns a mapping carrying the artifacts ``score_fn`` needs
    (``vault`` / ``jsonl`` / ``cwd`` / ``run``) plus reserved ``_``-prefixed
    bookkeeping keys (``_cleanup``); a test fake may return an ``int`` (pre-scored)
    or a ``(resolved, trail)`` tuple directly. Reserved keys are stripped before
    spreading into ``score_fn`` so :func:`harness.checker.resolve` never sees an
    unexpected ``_cleanup`` kwarg. An int short-circuits to that score; a mapping
    is folded by ``score_fn``.
    """
    if isinstance(run, int):
        return run, []
    if isinstance(run, tuple) and len(run) == 2 and isinstance(run[0], int):
        return run  # already (resolved, trail)
    if isinstance(run, dict):
        scoreable = {k: v for k, v in run.items() if not k.startswith("_")}
        kwargs = {**score_kwargs, **scoreable}
        return score_fn(card, **kwargs)
    raise TypeError(f"run_card_fn returned unscoreable handle: {type(run).__name__}")


def _maybe_cleanup(run: Any) -> None:
    """Invoke a handle's ``_cleanup`` callable, if it carries one.

    The live adapter (:func:`build_live_run_card_fn`) puts the run-vault remover
    here; fakes return ints / tuples / cleanup-less dicts, for which this is a
    no-op. Cleanup never raises into the caller — a leftover /tmp run dir is a
    quota nuisance, not a scoring error (mirrors :class:`harness.runlit.RunVault`).
    """
    if isinstance(run, dict):
        cleanup = run.get("_cleanup")
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# JSON serialization (for --out)
# ---------------------------------------------------------------------------


def report_to_dict(report: BenchReport) -> dict[str, Any]:
    """Project a :class:`BenchReport` to a JSON-serializable dict.

    The ``routing`` key is the aggregated RA section, or ``None`` when no routing
    card was scored (no ``routing_run_fn`` — e.g. a dry run).
    """
    return {
        "model": report.model,
        "rounds": report.rounds,
        "trr_mean": report.trr_mean,
        "trr_std": report.trr_std,
        "cards": [
            {"card_id": c.card_id, "tag": c.tag, "rounds": c.rounds, "mean": c.mean}
            for c in report.cards
        ],
        "coverage": report.coverage,
        "routing": report.routing,
    }
