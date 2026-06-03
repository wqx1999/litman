"""Phase F — routing scorer (pure classification, no execution).

The I-* routing cards carry ``cases`` — an utterance plus its golden skill
label — instead of an execution end-state. Each case is scored independently:

* golden is a ``str``        → hit iff the observed skill == golden.
* golden is a ``list``       → an *acceptable set* (two bundled skills both
  plausibly claim the utterance, scenarios §I) → hit iff observed ∈ set.

A case whose golden / acceptable-set references a skill NOT in ``present_skills``
is **N/A** — excluded from the RA denominator (OQ2: external skills like
``cite-retrieval`` are not installed in every bench run, so a collision utterance
that needs one cannot be fairly scored). The N/A count is surfaced separately so
the report stays honest about coverage.

``score_routing`` does NOT spawn an agent: it consumes the *observed* skill (the
``Skill`` tool_use label the executor already captured into
``ExecutorResult.skills``), so the live capture happens upstream — the scorer is
pure and unit-testable on stubbed observations.

RA (routing accuracy) = hits / (hits + misses), N/A cases excluded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CaseTrail:
    """One scored routing case (for the per-card trail in the report)."""

    utt: str
    golden: Any
    observed: str | None
    outcome: str  # "hit" | "miss" | "na"
    detail: str = ""


@dataclass
class RoutingResult:
    """Aggregate routing outcome for one routing card.

    ``ra`` = hits / (hits + misses) over scored cases; ``na`` cases are excluded
    from that denominator. ``spurious`` counts observed labels that were neither
    the golden nor (for a list golden) in the acceptable set — i.e. a wrong
    route, tallied alongside ``miss`` for misroute visibility.
    """

    ra: float
    hit: int
    miss: int
    spurious: int
    na: int
    trail: list[CaseTrail] = field(default_factory=list)


def _golden_skills(golden: Any) -> list[str]:
    """Flatten a case golden (str or list) into the referenced skill names."""
    if isinstance(golden, str):
        return [golden]
    if isinstance(golden, (list, tuple)):
        return [str(g) for g in golden]
    return []


def _is_in_scope(golden: Any, present_skills: set[str]) -> bool:
    """A case is in scope iff EVERY referenced skill is present in this run.

    The acceptable-set collision cards (e.g. ``[lit-library, cite-retrieval?]``)
    reference an external skill not installed here; such a case is N/A so it does
    not unfairly count as a miss when the external skill simply cannot fire.
    Trailing ``?`` markers (optional-presence hints in the corpus) are stripped
    before the membership test.
    """
    for raw in _golden_skills(golden):
        if raw.rstrip("?") not in present_skills:
            return False
    return True


def score_routing(
    card: Any,
    observed_skills: list[str | None],
    *,
    present_skills: list[str] | set[str],
) -> RoutingResult:
    """Score one routing card against the per-case observed skill labels.

    ``observed_skills`` is the routed skill for each case in ``card.cases``
    order (``None`` = the agent invoked no skill). ``present_skills`` is the set
    of skills installed in this run (OQ2: a case referencing an absent skill is
    N/A). Returns a :class:`RoutingResult` with RA + counts + per-case trail.
    """
    cases = _card_field(card, "cases") or []
    if len(observed_skills) != len(cases):
        raise ValueError(
            f"observed_skills length {len(observed_skills)} != "
            f"{len(cases)} cases for card {_card_field(card, 'id')!r}"
        )
    present = {str(s).rstrip("?") for s in present_skills}

    hit = miss = spurious = na = 0
    trail: list[CaseTrail] = []
    for case, observed in zip(cases, observed_skills):
        utt = str(_case_field(case, "utt"))
        golden = _case_field(case, "golden")
        acceptable = [g.rstrip("?") for g in _golden_skills(golden)]

        if not _is_in_scope(golden, present):
            na += 1
            trail.append(
                CaseTrail(utt, golden, observed, "na", "golden references absent skill")
            )
            continue

        if observed is not None and observed in acceptable:
            hit += 1
            trail.append(CaseTrail(utt, golden, observed, "hit"))
        else:
            # For Claude, ``observed is None`` (no skill fired) is a routing MISS,
            # not N/A: the skill exists and should have triggered. The skill-less
            # -> N/A rule (M34 §3.6.A) is an *agent-type* property of a future
            # skill-less executor, NOT derivable here from an empty observation;
            # adding it later means an agent-type flag, not a change to this branch.
            miss += 1
            # A wrong-but-present route (the agent picked a skill, just not the
            # right one) is also "spurious" for misroute reporting.
            if observed is not None:
                spurious += 1
            trail.append(
                CaseTrail(
                    utt, golden, observed, "miss",
                    f"observed {observed!r} not in acceptable {acceptable!r}",
                )
            )

    scored = hit + miss
    ra = hit / scored if scored else 0.0
    return RoutingResult(ra=ra, hit=hit, miss=miss, spurious=spurious, na=na, trail=trail)


def _card_field(card: Any, name: str) -> Any:
    if isinstance(card, dict):
        return card.get(name)
    return getattr(card, name, None)


def _case_field(case: Any, name: str) -> Any:
    if isinstance(case, dict):
        return case.get(name)
    return getattr(case, name, None)
