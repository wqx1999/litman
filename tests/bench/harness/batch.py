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
  needs_pty): the sandbox physically cannot run it; excluded from every metric.
* ``multi-turn``    — the card carries ``single_turn_unfit``: it runs fine in
  the sandbox but encodes an intrinsically multi-turn interaction that cannot be
  FAIRLY scored from one cold-start utterance, so single-turn TRR would measure
  which defensible reading the model picked, not capability. Excluded from TRR
  (distinct bucket, not folded into ``skipped`` — the exclusion reason differs:
  methodology, not sandbox limits — invariant #14 no-silent-skip spirit).
* ``routing``       — a routing card (``layer == "routing"``); scored by RA, not
  TRR.
* ``prose-blocked`` — an execution card with at least one un-mechanizable prose
  line in ``expected_end_state`` (cannot be fully auto-scored; excluded from TRR
  so the number stays honest — invariant #14 no-silent-skip spirit).
* ``auto-scored``   — an execution card whose ``expected_end_state`` is fully
  DSL; it counts toward TRR.

Only ``auto-scored`` EXECUTION cards contribute to TRR. Routing accuracy is
reported on its own axis; prose-blocked, skipped, and multi-turn cards are
surfaced in the coverage dict but never silently fold into a passing number.
"""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable

from harness.checker import _split_assertion, resolve
from harness.routing import RoutingResult, score_routing
from harness.seeds import assert_seed_intact


def _card_field(card: Any, name: str) -> Any:
    if isinstance(card, dict):
        return card.get(name)
    return getattr(card, name, None)


#: Tags whose report contribution is FULLY captured by a :class:`CardScore`, and
#: which may therefore be carried in as ``prior_scores`` instead of re-run.
#:
#: Routing cards are deliberately absent. Their contribution is not their
#: CardScore (``rounds=[]``, ``mean=0.0``) but their RoutingResult — the
#: per-utterance trail the RA section is aggregated from, which no CardScore
#: carries. Restoring one would drop it from the RA denominator without a word,
#: the exact class of silent shrink this module exists to prevent; so they are
#: re-run and RA stays computed over the whole corpus. skipped / multi-turn cards
#: are re-derived for free (no spawn), so restoring them would buy nothing.
#:
#: Lives HERE, next to the loop that enforces it, not beside the journal reader
#: that happens to filter on it today: the filter is one caller, the invariant is
#: the report's. :func:`run_batch` rejects a prior score that violates it.
RESUMABLE_TAGS = ("auto-scored", "prose-blocked")


class BatchAbortedError(RuntimeError):
    """The batch stopped early: the data measured so far is VALID but INCOMPLETE.

    Two events raise this — consecutive card errors (the quota ran out) and a
    mid-run change of served model (the ruler changed) — because they need the
    same handling: stop spending, keep every card already measured, and refuse to
    emit a ``report.json``, whose meaning must stay "one complete measurement".
    The caller persists the journal and re-raises as a distinct exit code.

    NOT the same event as a seed-canary abort (:class:`harness.seeds.SeedLeakError`),
    which says the data is INVALID — nothing measured under a moved seed can be
    kept, so that one deliberately does not come through here.

    ``completed`` / ``detail`` carry the facts a message must state; the journal
    path is deliberately absent — this module never learns where the journal
    lives (that is the CLI's wiring), so the "re-submit with --resume" hint is
    appended by whoever owns the path.
    """

    def __init__(self, message: str, *, completed: int, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.completed = completed
        self.detail = detail


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------


@dataclass
class CardScore:
    """Per-card aggregate over N rounds.

    ``usage`` is the token spend summed over this card's rounds (input / output
    / cache_creation / cache_read + a ``spawns`` count), or ``{}`` for
    non-executed tags (skipped / multi-turn / routing) and for fake (dry-run)
    runs that report no usage.

    ``error`` is non-``None`` when a round of this card did not run to completion
    (see :func:`_error_of`). Such a card was NOT measured: it is kept out of the
    TRR denominator and its ``mean`` (0.0) is a placeholder no reader may score.
    ``rounds`` then holds only the rounds that completed BEFORE the failure, for
    diagnosis — a 1-of-3-round card is not comparable to a 3-of-3 one, so the
    partial rounds are never averaged into anything."""

    card_id: str
    tag: str
    rounds: list[int]
    mean: float
    usage: dict = field(default_factory=dict)
    error: dict | None = None


def _measured_count(card_scores: list[CardScore]) -> int:
    """How many cards an abort message may honestly call "already measured".

    Not ``len(card_scores)``: that list also holds cards nothing was ever spawned
    for (skipped / multi-turn), routing cards (whose contribution is a
    ``RoutingResult`` the journal does not carry, so they re-run), and errored
    cards. Counting those would tell the reader their money bought more than it
    did — the same class of claim this module refuses to make about numbers, one
    layer up in the prose.

    The predicate is deliberately :data:`RESUMABLE_TAGS`: "already measured" and
    "will not be paid for twice" have to name the same set, or the message
    contradicts what the next sitting actually does.
    """
    return sum(1 for c in card_scores if c.tag in RESUMABLE_TAGS and c.error is None)


@dataclass
class BenchReport:
    """The full batch report (M34 §6.3 deterministic subset).

    The reported unit is an ``(agent, model)`` pair, not a model: the same weights
    served through three scaffolds are three different data points.

    ``model_requested`` is what we asked for; ``model_served`` is what the agent
    said it served, VERBATIM (a display name for cursor, ``None`` for agy, which
    reports nothing). Both are kept raw because they carry information the family
    does not — "Sonnet 4.6 200K Medium No Thinking" vs "Claude Sonnet 4.6
    (Thinking)" are the same weights with thinking off and on, and the reader is
    entitled to see that rather than be told they are equivalent.
    ``model_family`` is the explicit-lookup normalization for grouping, or ``None``
    when the string is not in the table (never a guess).

    ``agent_flags`` records the permission flags the adapter actually used. It
    comes from our side of the boundary because the agents' own streams cannot be
    trusted for it (cursor reports ``permissionMode: "default"`` while ``--force``
    is in effect), and because "this run was scored with tool approval globally
    disabled" is a fact the reader must not have to dig for.

    ``routing`` is the aggregated routing-accuracy section (overall RA + summed
    misroute / miss / spurious / na + per-card RA), or ``None`` when RA was not
    scored — either no ``routing_run_fn`` was passed (dry run) or the agent cannot
    expose skill activation at all. ``coverage["routing_ra"]`` distinguishes those
    two: ``"not_scored"`` vs ``"not_measurable"``. Either way the section is an
    honest absence, never a fake 0.

    ``tokens`` is the run's grand-total token accounting (an ``auto_scored``
    bucket summed over executed cards, a ``routing`` bucket summed over the
    routing classification spawns, and a ``total``), or ``None`` when no live
    usage was observed — a dry run, or an agent with no counters at all (agy).

    ``qualification`` is the Phase 0 instrument-qualification record: it gates the
    run, and it is also a deliverable — a reader must be able to see that the
    binary answered, the tools were authorized, the skills came from the repo, the
    evidence chain recorded something and the model was pinned (or, for agy, that
    the model could NOT be verified).

    ``sessions`` lists one record per run that contributed cards to this report —
    a single-element list for an ordinary run, more when a run was resumed. A
    report stitched from two sittings is still a valid measurement, but it is not
    the same artifact as one taken in a single sitting, and the reader is the one
    entitled to decide whether that matters.

    ``model_identity`` is ``"unverified"`` for an agent that reports no served
    model (agy), otherwise ``None``. It is a standing property of such an agent,
    not a finding about a particular run and not an accusation: every other agent
    has its served model re-checked each round, and for this one that check is a
    no-op, so ``model_identity_reason`` states the consequence — if the model HAD
    changed, nobody would know. It does not say the model changed. We have no
    evidence for that, and printing a suspicion as a finding is the same sin as
    printing a guess as a number.
    """

    agent: str
    model_requested: str
    rounds: int
    trr_mean: float
    trr_std: float
    cards: list[CardScore]
    model_served: str | None = None
    model_family: str | None = None
    agent_flags: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] | None = None
    tokens: dict[str, Any] | None = None
    qualification: dict[str, Any] | None = None
    sessions: list[dict[str, Any]] | None = None
    model_identity: str | None = None
    model_identity_reason: str | None = None


# ---------------------------------------------------------------------------
# Token usage aggregation
# ---------------------------------------------------------------------------

# The four Anthropic token counters (one stream-json ``usage`` block per spawn).
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _usage_of(run: Any) -> dict:
    """Best-effort token ``usage`` of one run handle.

    The live adapter returns ``{"run": ExecutorResult, ...}``; the ExecutorResult
    carries ``.usage``. Fakes (ints / tuples / usage-less dicts) carry none, so
    this returns ``{}`` — dry runs simply contribute zero tokens."""
    if isinstance(run, dict):
        result = run.get("run")
        u = getattr(result, "usage", None)
        if isinstance(u, dict):
            return u
    return {}


def _served_model_of(run: Any) -> str | None:
    """The model string one run handle's agent reported serving, if any.

    ``None`` for fakes (dry runs) and for agents that report no model at all —
    both read downstream as "we do not know", which is the truth."""
    if isinstance(run, dict):
        return getattr(run.get("run"), "model_served", None)
    return None


def _error_of(run: Any) -> dict | None:
    """This spawn's failure, or ``None`` if it ran to completion.

    A dead spawn returns empty stdout, which satisfies no assertion and scores 0 —
    indistinguishable from a model that got it wrong. That is the whole reason this
    exists: the quota running out must never be reported as the model failing.
    The executor already records both facts (``timed_out`` on a killed run,
    ``exit_code`` on everything else); until now nothing downstream read them.

    Defensive like :func:`_usage_of`: fakes carry no ``run`` key (or a ``None``
    one), so dry runs and the deterministic tests see ``None`` — no run handle is
    never an error, only a completed run can report one.
    """
    if isinstance(run, dict):
        result = run.get("run")
        exit_code = getattr(result, "exit_code", 0)
        if getattr(result, "timed_out", False):
            return {"reason": "timeout", "exit_code": exit_code, "timed_out": True}
        if exit_code != 0:
            return {"reason": "exit", "exit_code": exit_code, "timed_out": False}
    return None


def _sum_usage(usages: list[dict]) -> dict:
    """Sum a list of per-spawn ``usage`` dicts into one bucket.

    Adds the four token counters and counts the contributing ``spawns``. Empty
    input -> ``{}`` so callers can treat "no usage" as falsy. Cost is NOT summed
    here: dollar figures are derived downstream from these counters x the
    provider's own prices (see :func:`harness.agents.claude._parse_usage`).

    Spawns that reported NO usage (``{}`` / ``None`` — an agent with no counters,
    or a run that died before reporting) are dropped rather than added as zeros,
    and they do not inflate ``spawns`` either: a bucket of "3 spawns, 0 tokens" is
    a claim we cannot make. An all-unreported list yields ``{}``, which surfaces
    as ``None`` in the report.

    Every dict reaching here is already in the internal snake_case key set — each
    adapter normalizes at its own edge. That matters more than it looks: cursor
    counts in camelCase, and summing those keys with ``.get(k, 0)`` raises nothing
    and returns a tidy, wholly fictional zero."""
    contributing = [u for u in usages if u]
    if not contributing:
        return {}
    out: dict[str, Any] = {k: 0 for k in _USAGE_KEYS}
    for u in contributing:
        for k in _USAGE_KEYS:
            out[k] += int(u.get(k, 0) or 0)
    out["spawns"] = len(contributing)
    return out


def _merge_usage_buckets(*buckets: dict) -> dict:
    """Combine already-summed buckets (e.g. auto_scored + routing) into a total."""
    present = [b for b in buckets if b]
    if not present:
        return {}
    out: dict[str, Any] = {k: 0 for k in _USAGE_KEYS}
    spawns = 0
    for b in present:
        for k in _USAGE_KEYS:
            out[k] += int(b.get(k, 0) or 0)
        spawns += int(b.get("spawns", 0) or 0)
    out["spawns"] = spawns
    return out


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
    """Classify a card into one of the five coverage buckets."""
    if _card_field(card, "skip_reason"):
        return "skipped"
    if _card_field(card, "single_turn_unfit"):
        return "multi-turn"
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
    agent: str = "claude",
    base_url: str | None = None,
    auth_token: str | None = None,
    run_card_impl: Callable[..., Any] | None = None,
    ensure_seed_impl: Callable[..., Path] | None = None,
) -> Callable[..., dict]:
    """Build the live ``run_card_fn`` the batch loop calls (one per round).

    Bridges the batch contract ``run_card_fn(card, *, round, model, **_)`` to the
    executor contract ``run_card(card, run_vault, *, fixtures_pdfs_dir, agent,
    model, ...) -> ExecutorResult`` (M34 §3.6.A — this is the ONLY place that
    touches the executor, and it hardwires no ``--model`` / ``stream-json`` /
    ``claude`` literal; ``agent`` and ``model`` are passed straight through).

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
    4. returns a scoreable handle ``{vault, jsonl, cwd, run, _cleanup, _seed_root}``
       — ``cwd`` is :func:`harness.executor.neutral_cwd_for` (where ``lit export``
       drops ``refs.bib``); ``_cleanup`` rm's the whole run dir (called by
       :func:`run_batch` AFTER scoring, so the vault survives the check);
       ``_seed_root`` is the seed this round copied from, which
       :func:`run_batch` re-digests afterwards to prove the round did not write
       through to it (:func:`harness.seeds.assert_seed_intact`).

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
                agent=agent,
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
                # Where run_batch's canary looks. Reserved (_-prefixed) so
                # _score_one strips it before score_fn sees the handle.
                "_seed_root": Path(seed_vault).parent,
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
    agent: str = "claude",
    base_url: str | None = None,
    auth_token: str | None = None,
    observe_impl: Callable[..., Any] | None = None,
    ensure_seed_impl: Callable[..., Path] | None = None,
    usage_sink: list[dict] | None = None,
) -> Callable[..., Any]:
    """Build the live ``routing_run_fn`` :func:`run_batch` calls per routing card.

    Bridges the batch contract ``routing_run_fn(card, *, model) -> list[str|None]``
    (one observed skill per case, in ``card.cases`` order) to the executor's
    per-utterance probe (M34 §3.6.A — like the execution adapter, this is the ONLY
    place that touches the executor for routing, and it hardwires no ``--model`` /
    ``stream-json`` / ``claude`` literal; ``agent`` / ``model`` pass straight
    through).

    For an agent with no skill-activation signal at all it returns the
    :data:`~harness.agents.NOT_MEASURABLE` sentinel INSTEAD of a list, without
    spawning anything: the capability is a known property of the agent, so there is
    nothing to learn from ~14 classification spawns, and a list of ``None`` would
    be scored as a clean sweep of routing misses.

    Each case routes against its own fresh disposable run vault (a ``cp`` of
    ``routing_seed`` — routing is pure classification, so a minimal initialized
    vault suffices) so the cases never interfere; the run dir is removed after the
    probe. ``observe_impl`` (default :func:`harness.executor.observe_skill_for_utterance`)
    is injectable so the tests drive the adapter with canned skills, never spawning
    a live agent (M34 §3.5 hard boundary). It spawns a live agent per utterance in
    production, exercised ONLY under Phase G authorization.

    When ``usage_sink`` is provided AND the default (real) ``observe_impl`` is in
    use, each routing probe's token ``usage`` is appended to it so the routing
    spawns count toward the run's grand-total cost. Injected ``observe_impl``
    doubles (tests) never receive ``usage_sink``, so their signatures are
    untouched and the M34 §3.5 boundary holds.
    """
    from harness.agents import NOT_MEASURABLE, get_adapter
    from harness.runlit import RunVault

    routing_measurable = get_adapter(agent).capabilities.routing

    default_observe = observe_impl is None
    if observe_impl is None:
        from harness.executor import observe_skill_for_utterance as observe_impl  # type: ignore[assignment]
    if ensure_seed_impl is None:
        from harness.seeds import build_seed

        def _default_ensure_seed(name: str) -> Path:
            return build_seed(name, cache_root=Path(seeds_dir))

        ensure_seed_impl = _default_ensure_seed

    def _run(card: Any, *, model: str, **_: Any) -> Any:
        if not routing_measurable:
            return NOT_MEASURABLE
        seed_vault = ensure_seed_impl(str(routing_seed))
        observed: list[str | None] = []
        for case in _card_field(card, "cases") or []:
            utt = _case_field(case, "utt")
            rv = RunVault(Path(seed_vault), run_root=Path(work_root))
            rv.__enter__()  # cp seed -> <work_root>/bench-<uuid>/vault
            kwargs: dict[str, Any] = dict(
                fixtures_pdfs_dir=Path(work_root) / "_routing_no_fixtures",
                agent=agent,
                model=model,
                base_url=base_url,
                auth_token=auth_token,
            )
            # usage_sink is only on the real observe; never forwarded to a double.
            if default_observe and usage_sink is not None:
                kwargs["usage_sink"] = usage_sink
            try:
                skill = observe_impl(str(utt), rv.vault, **kwargs)
                # Same canary as the card loop, for the same reason: this spawns
                # a live agent against a shared, cross-run-cached seed. The known
                # leak (an absolute project path in lit-config.yaml) cannot reach
                # seed-empty, which has no projects at all — but the canary is
                # here for the vectors nobody has enumerated, and reasoning about
                # which of those exist is precisely what failed before.
                assert_seed_intact(
                    Path(seed_vault).resolve().parent,
                    culprit=f"routing probe {str(utt)!r}",
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
    agent: str = "claude",
    run_card_fn: Callable[..., Any] | None = None,
    routing_run_fn: Callable[..., Any] | None = None,
    score_fn: Callable[..., tuple[int, list]] = resolve,
    run_kwargs: dict[str, Any] | None = None,
    score_kwargs: dict[str, Any] | None = None,
    transcript_dir: Path | None = None,
    routing_usage_sink: list[dict] | None = None,
    qualification: dict[str, Any] | None = None,
    max_consecutive_errors: int = 3,
    prior_scores: list[CardScore] | None = None,
    on_card_done: Callable[[CardScore], None] | None = None,
    sessions: list[dict[str, Any]] | None = None,
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
    folded into the report's ``routing`` section.

    RA can be absent for two DIFFERENT reasons, and the report distinguishes them
    in ``coverage["routing_ra"]``:

    * ``"not_scored"``     — no ``routing_run_fn`` (a dry / execution-only run);
    * ``"not_measurable"`` — ``routing_run_fn`` returned
      :data:`~harness.agents.NOT_MEASURABLE`: this agent has no skill-activation
      signal, so its RA is not something we have. The cards are still tagged and
      counted, and they are kept OUT of the RA denominator — scoring them would
      turn "we cannot see this" into "it missed every one".

    In both cases ``report.routing`` is ``None``: an honest absence, never a
    fabricated 0 (invariant #14 no-silent-skip spirit).

    Skipped cards are recorded with their tag and excluded from all metrics.
    Only ``auto-scored`` cards contribute to ``trr_mean`` / ``trr_std``.

    A round that did NOT run to completion (:func:`_error_of`: killed by the
    timeout, or a non-zero exit) voids its whole card: the card carries an
    ``error``, keeps out of ``auto_means``, and is listed in
    ``coverage["errored_cards"]``. It is never a 0 — an instrument that stopped
    answering has not measured anything, and the difference between "the quota ran
    out" and "the model got it wrong" is the difference between a broken run and a
    result. ``max_consecutive_errors`` cards failing in a row raises
    :class:`BatchAbortedError` (``0``/``None`` disables the breaker), as does a
    change in served model mid-run; both keep what was measured and refuse to
    finish the rest.

    ``prior_scores`` are cards measured in an EARLIER session (a resumed run):
    they are not re-run, and fold into the aggregate as if they had just run, so
    the reported TRR is always computed over the whole corpus in one pass. Each
    one MUST be a completed card (``error is None``) whose tag is in
    :data:`RESUMABLE_TAGS`; anything else raises, because a prior score is adopted
    into the numbers unexamined and a bad one produces a self-contradictory report
    rather than a failure.
    ``on_card_done`` is called with each card's :class:`CardScore` the moment it
    is final, so a caller can persist it before the next card is attempted (same
    injection pattern as ``routing_usage_sink``: this module writes no files and
    knows no paths). ``sessions`` is carried onto the report verbatim, so a
    stitched-together report says so.
    """
    from harness.agents import NOT_MEASURABLE, get_adapter, model_family

    adapter = get_adapter(agent)
    if run_card_fn is None:
        raise ValueError(
            "run_batch needs an explicit run_card_fn: build the live adapter via "
            "build_live_run_card_fn(...) or pass a fake (M34 §3.6.A)."
        )
    run_kwargs = run_kwargs or {}
    score_kwargs = score_kwargs or {}

    if transcript_dir is not None:
        # Fail BEFORE any agent spawn (token spend) if the dir is unwritable.
        # A silently-swallowed bad --keep-transcript path wasted a whole live run
        # (no-silent-skip, invariant #14). Probe with a real mkdir + write so an
        # unexpanded "$BENCH/..." (→ /results/...) aborts up front, not after 24
        # rounds of spend with zero artifacts saved.
        try:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            probe = transcript_dir / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            raise ValueError(
                f"--keep-transcript dir is not writable: {transcript_dir} ({e}); "
                "fix the path before re-running (no tokens spent yet)"
            ) from e

    card_scores: list[CardScore] = []
    counts = {"auto-scored": 0, "prose-blocked": 0, "routing": 0, "skipped": 0, "multi-turn": 0}
    auto_means: list[float] = []
    routing_results: list[tuple[str, RoutingResult]] = []
    routing_not_measurable: list[str] = []
    # The model the agent SAID it served, harvested from the runs themselves
    # (first one to report) and then RE-CHECKED every round. Stays None for an
    # agent that reports none.
    served: str | None = None
    # Whether re-checking means anything for this agent at all (agy reports no
    # model, so `None == None` every round proves exactly nothing — see below).
    model_verifiable = adapter.capabilities.served_model
    for prior_score in prior_scores or []:
        # A prior score is adopted verbatim into the aggregate, so a bad one is not
        # a crash — it is a QUIET wrong number, and of the worst kind. An errored
        # prior would land in errored_cards AND in the TRR denominator: one card
        # reported as both "did not run" and "scored 0.0", in the same report, by
        # the code written to make that impossible. A routing prior would silently
        # shrink the RA denominator. The only caller filters correctly today; this
        # is here because "the caller filters correctly" is a property of a module
        # one import away, and that is exactly how the seed leak survived.
        if prior_score.error is not None:
            raise ValueError(
                f"prior_scores carries {prior_score.card_id!r} with an error "
                f"({prior_score.error}): a card that did not run has not been "
                "measured, so it cannot be carried into a report as a result. "
                "Re-run it instead."
            )
        if prior_score.tag not in RESUMABLE_TAGS:
            raise ValueError(
                f"prior_scores carries {prior_score.card_id!r} tagged "
                f"{prior_score.tag!r}, which is not restorable from a CardScore "
                f"(only {', '.join(RESUMABLE_TAGS)} are — see RESUMABLE_TAGS). "
                "Re-run it instead."
            )
    prior_by_id = {c.card_id: c for c in (prior_scores or [])}
    # "Consecutive" (reset by any card that completes), not "cumulative": a flaky
    # card here and there is the noise a bench is built to average over, and
    # aborting on it would make long runs impossible to finish. N in a row is a
    # different claim — that is what a spent quota looks like.
    consecutive_errors = 0
    error_streak: list[str] = []

    for card in cards:
        cid = str(_card_field(card, "id"))
        tag = coverage_tag(card)
        counts[tag] += 1

        prior = prior_by_id.get(cid)
        if prior is not None:
            # Measured in an earlier session and carried in by the caller (D7).
            # It contributes to the report exactly as if it had run just now —
            # the ruler gate upstream has already proven it was the same ruler —
            # so TRR is computed over the WHOLE corpus, never averaged from two
            # half-batches. Not re-journaled: it came from the journal.
            card_scores.append(prior)
            if tag == "auto-scored":
                auto_means.append(prior.mean)
            continue

        if tag in ("skipped", "multi-turn"):
            # Both are excluded from execution + every metric. ``skipped`` =
            # sandbox physically cannot run it (network / pty); ``multi-turn`` =
            # runs fine but is unfair to score single-turn (see ``single_turn_unfit``).
            score = CardScore(card_id=cid, tag=tag, rounds=[], mean=0.0)
            card_scores.append(score)
            if on_card_done is not None:
                on_card_done(score)
            continue

        if tag == "routing":
            score = CardScore(card_id=cid, tag=tag, rounds=[], mean=0.0)
            card_scores.append(score)
            if routing_run_fn is not None:
                observed = routing_run_fn(card, model=model)
                if observed is NOT_MEASURABLE:
                    # This agent exposes no skill-activation signal. Record the
                    # card as unmeasurable and do NOT score it: score_routing
                    # would read the absence as a miss per case.
                    routing_not_measurable.append(cid)
                else:
                    rr = score_routing(
                        card,
                        observed,
                        present_skills=_card_field(card, "in_scope_skills") or [],
                    )
                    routing_results.append((cid, rr))
            if on_card_done is not None:
                on_card_done(score)
            continue

        round_scores: list[int] = []
        round_usages: list[dict] = []
        card_error: dict | None = None
        for i in range(rounds):
            run = run_card_fn(card, round=i, model=model, **run_kwargs)
            round_error: dict | None = None
            try:
                resolved, _trail = _score_one(card, run, score_fn, score_kwargs)
                # Snapshot the spawn's token usage BEFORE _maybe_cleanup; cheap
                # and independent of the opt-in transcript dump below.
                round_usages.append(_usage_of(run))
                round_error = _error_of(run)
                if transcript_dir is not None:
                    # Opt-in only: dump the in-memory transcript (commands +
                    # final answer + per-assertion trail) BEFORE _maybe_cleanup
                    # rm's the disposable vault. Default (None) path is untouched.
                    # A FAILED round is dumped like any other: it is the evidence
                    # of what the failure looked like, which is the whole point.
                    _dump_transcript(
                        transcript_dir, cid, i, model, run, resolved, _trail
                    )
                # Canary LAST inside the try, after the transcript dump: if this
                # round escaped its sandbox, the dump is the evidence of which
                # commands did it, and it must survive the abort. A round that
                # ERRORED still comes through here — a spawn can write through to
                # the seed and then die, and a corpse is not an alibi.
                _assert_seed_intact(run, card_id=cid, round=i)
            finally:
                _maybe_cleanup(run)

            if round_error is not None:
                # D5: the card is void — do not spend the remaining rounds on it.
                # Rounds are the unit of variance; a 1-of-3 card cannot be
                # averaged with a 3-of-3 one, so there is nothing to salvage.
                card_error = round_error
                break

            # Re-check the served model EVERY round, and only for rounds that
            # actually ran. The order is load-bearing: a dead spawn emits no init
            # event, so _served_model_of returns None for it, and checking the
            # model first would report a spent quota as "the model changed from X
            # to None" — the exact confusion this whole module now exists to
            # prevent. Errors are diagnosed above; only survivors are compared.
            if model_verifiable:
                observed_model = _served_model_of(run)
                if served is None:
                    served = observed_model
                elif observed_model != served:
                    # Includes observed_model is None: an agent that reported a
                    # model for 14 cards and then stopped is not a reading we can
                    # score, and silence is not permission.
                    raise BatchAbortedError(
                        f"served model changed mid-run at card {cid!r} round {i}: "
                        f"the run started on {served!r} and this round reports "
                        f"{observed_model!r}. Every card after the change was "
                        f"measured with a different ruler, so the batch stops here; "
                        f"the {_measured_count(card_scores)} card(s) already "
                        f"measured are valid.",
                        completed=_measured_count(card_scores),
                        detail={
                            "reason": "model_changed",
                            "card_id": cid,
                            "round": i,
                            "model_baseline": served,
                            "model_observed": observed_model,
                        },
                    )
            round_scores.append(int(resolved))

        mean = sum(round_scores) / len(round_scores) if round_scores else 0.0
        score = CardScore(
            card_id=cid,
            tag=tag,
            rounds=round_scores,
            mean=mean,
            usage=_sum_usage(round_usages),
            error=card_error,
        )
        card_scores.append(score)
        if on_card_done is not None:
            # BEFORE the breaker below: an aborting run must leave every card it
            # did measure on disk, including the failures that triggered the abort
            # (they are what a resume retries).
            on_card_done(score)
        if card_error is not None:
            # NOT appended to auto_means: this card was not measured, and a 0 here
            # would be the quota's score reported as the model's.
            consecutive_errors += 1
            error_streak.append(cid)
            if max_consecutive_errors and consecutive_errors >= max_consecutive_errors:
                raise BatchAbortedError(
                    f"aborting: {consecutive_errors} cards in a row failed to run "
                    f"({', '.join(error_streak)}), starting at {error_streak[0]!r}. "
                    f"Last failure: reason={card_error['reason']} "
                    f"exit_code={card_error['exit_code']} "
                    f"timed_out={card_error['timed_out']}. "
                    f"That is what an exhausted quota looks like, so the run stops "
                    f"rather than score the rest as zeros; the "
                    f"{_measured_count(card_scores)} card(s) "
                    f"already measured are valid.",
                    completed=_measured_count(card_scores),
                    detail={
                        "reason": "consecutive_errors",
                        "streak": list(error_streak),
                        "last_error": card_error,
                    },
                )
            continue
        consecutive_errors = 0
        error_streak = []
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
        "multi_turn_cards": [c.card_id for c in card_scores if c.tag == "multi-turn"],
        # Cards that did not RUN. They are absent from auto_means (and so from
        # trr_denominator, which is just its length) — the exclusion and the
        # disclosure are the same edit, so a number can never quietly shrink its
        # own denominator.
        "errored_cards": [c.card_id for c in card_scores if c.error],
        "trr_denominator": len(auto_means),
    }
    # WHY the RA axis is (or is not) a number. "not_measurable" is an agent-type
    # property — the RA denominator excludes these cards entirely rather than
    # counting them as misses.
    if routing_not_measurable:
        coverage["routing_ra"] = "not_measurable"
        coverage["routing_ra_reason"] = (
            f"{agent} exposes no skill-activation signal (no Skill tool, no "
            "file-read events); RA cannot be observed for this agent"
        )
        coverage["routing_not_measurable_cards"] = routing_not_measurable
    elif routing_results:
        coverage["routing_ra"] = "scored"
    else:
        coverage["routing_ra"] = "not_scored"

    # Grand-total token accounting: execution cards (per-card usage already summed
    # across rounds) + the routing classification spawns. ``None`` when no live
    # usage was observed (dry run / fakes), so it never reads as a fake zero.
    auto_bucket = _merge_usage_buckets(*(c.usage for c in card_scores))
    routing_bucket = _sum_usage(routing_usage_sink or [])
    total_bucket = _merge_usage_buckets(auto_bucket, routing_bucket)
    tokens: dict[str, Any] | None = None
    if total_bucket:
        tokens = {
            "auto_scored": auto_bucket or None,
            "routing": routing_bucket or None,
            "total": total_bucket,
        }

    return BenchReport(
        agent=agent,
        model_requested=model,
        rounds=rounds,
        trr_mean=trr_mean,
        trr_std=trr_std,
        cards=card_scores,
        model_served=served,
        # The request is a fallback ONLY for an agent that never reports a model
        # (agy). For everyone else `served is None` means nothing observed one — a
        # dry run, a routing-only run (only execution rounds harvest it), or spawns
        # that all died before reporting — and naming a family from what we merely
        # ASKED for would dress that gap up as knowledge.
        model_family=model_family(
            served, model, fallback_to_requested=not adapter.capabilities.served_model
        ),
        agent_flags=list(adapter.permission_flags),
        coverage=coverage,
        routing=_aggregate_routing(routing_results),
        tokens=tokens,
        qualification=qualification,
        sessions=list(sessions) if sessions else None,
        # Not a suspicion about this agent — a property of it. See the field docs.
        model_identity=None if model_verifiable else "unverified",
        model_identity_reason=(
            None
            if model_verifiable
            else (
                f"{agent} reports no served model at all, so the per-round check "
                "that every other agent gets is a no-op here: if this run had been "
                "served by a different model than the one requested, nobody would "
                "know."
            )
        ),
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
        # Per-utterance trail so a routing miss is attributable (which sentence
        # routed where), not just an opaque per-card RA. Without this, a model's
        # routing score in the compatibility matrix is un-diagnosable.
        "per_card_trail": {
            cid: [_routing_case_to_json(c) for c in rr.trail] for cid, rr in results
        },
    }


def _routing_case_to_json(case: Any) -> dict[str, Any]:
    """Serialize one :class:`harness.routing.CaseTrail` for the report."""
    return {
        "utt": _card_field(case, "utt"),
        "golden": _card_field(case, "golden"),
        "observed": _card_field(case, "observed"),
        "outcome": _card_field(case, "outcome"),
        "detail": _card_field(case, "detail"),
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
    bookkeeping keys (``_cleanup`` / ``_seed_root``); a test fake may return an
    ``int`` (pre-scored) or a ``(resolved, trail)`` tuple directly. Reserved keys
    are stripped before spreading into ``score_fn`` so
    :func:`harness.checker.resolve` never sees an unexpected ``_cleanup`` kwarg.
    An int short-circuits to that score; a mapping is folded by ``score_fn``.
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


def _trail_to_json(trail: Any) -> Any:
    """Serialize the per-assertion trail (list of ``AssertResult`` dataclasses).

    Falls back to ``str`` for anything that is neither a dataclass nor a plain
    JSON scalar, so a debug dump never raises on an unexpected element.
    """
    if not isinstance(trail, list):
        return str(trail)
    out = []
    for item in trail:
        if is_dataclass(item) and not isinstance(item, type):
            out.append(asdict(item))
        elif isinstance(item, (str, int, float, bool, type(None))):
            out.append(item)
        else:
            out.append(str(item))
    return out


def _dump_transcript(
    transcript_dir: Path,
    card_id: str,
    round: int,  # noqa: A002 - mirror the loop variable name
    model: str,
    run: Any,
    resolved: int,
    trail: Any,
) -> None:
    """Write one round's transcript artifact to ``<transcript_dir>/<id>-r<n>.json``.

    Opt-in debug aid (only called when ``run_batch(transcript_dir=...)`` is set):
    captures the agent's emitted commands (``jsonl``), final answer
    (``run.final_text``), the resolved score, and the per-assertion ``trail`` —
    everything needed to root-cause a failing card WITHOUT keeping the disposable
    run vault (which ``_maybe_cleanup`` still removes as usual). The default path
    (``transcript_dir=None``) never reaches here, so behavior is byte-identical.

    Never raises into the scoring loop: a dump failure is a debug-convenience
    miss, not a scoring error.
    """
    try:
        transcript_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "card_id": card_id,
            "round": round,
            "model": model,
            "resolved": int(resolved),
            "trail": _trail_to_json(trail),
        }
        if isinstance(run, dict):
            payload["jsonl"] = run.get("jsonl")
            result = run.get("run")
            payload["final_text"] = getattr(result, "final_text", None)
            payload["exit_code"] = getattr(result, "exit_code", None)
            # Per-spawn token accounting (input / output / cache); {} on a hard
            # API error that aborted before any usage was reported, or on an agent
            # with no counters — never a zero.
            payload["usage"] = getattr(result, "usage", None) or None
            # The argv the HARNESS built, verbatim. The only trustworthy record of
            # how this spawn was authorized: cursor's own stream reports
            # permissionMode "default" even while --force is in effect.
            payload["argv"] = getattr(result, "argv", None) or None
            payload["model_served"] = getattr(result, "model_served", None)
        else:
            # A fake handle (int / tuple); record what little we have.
            payload["run_repr"] = str(run)
        path = transcript_dir / f"{card_id}-r{round}.json"
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001 - a dump miss must not kill a long run
        # Non-fatal (one bad round shouldn't abort a 3-hour run), but NEVER
        # silent: the up-front probe in run_batch already caught an unwritable
        # dir, so reaching here means a mid-run anomaly worth surfacing.
        print(
            f"WARNING: failed to write transcript {card_id}-r{round}: {e}",
            file=sys.stderr,
        )


def _assert_seed_intact(run: Any, *, card_id: str, round: int) -> None:
    """Fire the seed canary for one round, if the handle names a seed root.

    ``card_id``/``round`` travel into the error text because the digest cannot
    know them and the reader needs them: ``run_batch`` prints nothing per card,
    so without this the traceback says a seed moved and leaves the operator to
    guess which of N cards moved it.

    Called from the round loop — NOT from ``RunVault.__exit__``, which is where
    it looks like it belongs. Three reasons it cannot live there, each fatal on
    its own:

    * **It would be swallowed.** ``_cleanup`` IS ``rv.__exit__``, and
      :func:`_maybe_cleanup` wraps it in ``except Exception: pass`` by documented
      contract ("Cleanup never raises into the caller"). The alarm would fire,
      get eaten, and the round would score and report as if nothing happened —
      precisely the silent-poisoning failure this exists to end.
    * **It would mask real errors.** ``__exit__`` also runs on the error path
      (``rv.__exit__(*sys.exc_info())``) and in ``finally``; raising from there
      REPLACES the exception that actually broke the run.
    * **It would leak the run dir.** ``__exit__`` owns the rmtree; raising before
      it skips the cleanup it was called to do.

    Fakes (ints / tuples / dicts without ``_seed_root``) carry no seed root, so
    this is a no-op for them.
    """
    if isinstance(run, dict):
        seed_root = run.get("_seed_root")
        if seed_root is not None:
            assert_seed_intact(
                Path(seed_root), culprit=f"card {card_id!r} round {round}"
            )


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

    The ``routing`` key is the aggregated RA section, or ``None`` when RA was not
    scored (dry run) or cannot be measured for this agent — ``coverage["routing_ra"]``
    says which. The ``tokens`` key is the grand-total token accounting (``None``
    for a dry run, or for an agent with no counters); each card carries its own
    per-card ``usage`` so cost can be attributed per task.

    ``model`` is kept as an alias of ``model_requested`` for one version: report
    JSONs already on disk are read by the user's own analysis scripts, and dropping
    the key would have them silently read ``None`` rather than fail.
    """
    return {
        "agent": report.agent,
        "model_requested": report.model_requested,
        "model_served": report.model_served,
        "model_family": report.model_family,
        "agent_flags": report.agent_flags,
        # Back-compat alias (see docstring) — deliberately duplicated, not moved.
        "model": report.model_requested,
        "rounds": report.rounds,
        "trr_mean": report.trr_mean,
        "trr_std": report.trr_std,
        "cards": [
            {
                "card_id": c.card_id,
                "tag": c.tag,
                "rounds": c.rounds,
                "mean": c.mean,
                "usage": c.usage or None,
                # Non-null ⇒ this card did not run; its ``mean`` is a placeholder,
                # not a score. Any consumer aggregating ``mean`` must filter on
                # this first (the report's own TRR already has).
                "error": c.error,
            }
            for c in report.cards
        ],
        "coverage": report.coverage,
        "routing": report.routing,
        "tokens": report.tokens,
        "qualification": report.qualification,
        "sessions": report.sessions,
        "model_identity": report.model_identity,
        "model_identity_reason": report.model_identity_reason,
    }
