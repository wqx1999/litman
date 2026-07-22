"""Run provenance: what ruler measured this, and may a second sitting continue it.

A bench run can stop half way — the quota runs out, the node's wall clock ends,
someone hits Ctrl-C. Resuming is only meaningful if the second sitting measures
with the SAME instrument as the first, so this module owns two things and nothing
else:

* :func:`ruler_fingerprint` — what the instrument was, in three separately named
  parts, so a refusal can say WHICH part moved rather than show two hex strings
  and let the reader work it out;
* the **journal** (``DIR/journal.jsonl``) — an append-only record of the session
  and of every card as it finishes, so a run killed between two cards keeps
  everything it already paid for.

Why a separate module rather than a few functions in ``batch`` / ``seeds``: the
resume decision is a safety gate, and a reviewer should be able to read the whole
chain — fingerprint, journal format, comparison — in one file, rather than
assemble it from three.

The journal is deliberately dumb. It is jsonl, one object per line, append-only,
and the LAST record for a card wins (:func:`resumable_scores`). No rewriting, no
compaction: a process killed mid-write loses at most its final line, and every
line before it is still exactly what it was when it was written.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.batch import RESUMABLE_TAGS, CardScore

BENCH_DIR = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = BENCH_DIR / "scenarios"
HARNESS_DIR = Path(__file__).resolve().parent

#: The journal's file name inside a ``--run-dir``. Sits beside ``report.json`` and
#: ``transcripts/``, and the SLURM template's ``RUN_DIR="$SLURM_SUBMIT_DIR"``
#: already lands a re-submission in the same dir, so continuing a run is the same
#: command in the same place plus ``--resume``.
#:
#: That flag is NOT optional and the template does not carry it: re-submitting
#: without it is a hard refusal (D6 — continuing and re-measuring are both
#: plausible readings of the same command line, and guessing wrong either
#: re-reports old numbers or destroys them). Adding it to the template is
#: deliberately out of scope here, so for now it is typed by hand.
JOURNAL_NAME = "journal.jsonl"


# ---------------------------------------------------------------------------
# The ruler fingerprint
# ---------------------------------------------------------------------------


def _content_digest(paths: list[Path], *, root: Path) -> str:
    """sha256 over each file's path AND its bytes, in sorted order.

    CONTENT, not ``size:mtime`` — the difference matters here in a way it does not
    for :func:`harness.seeds.litman_fingerprint`, which only has to notice that
    the installed package changed. What must be caught here is a change in
    MEANING, and the cheapest such edit is exactly the one a size check cannot
    see: turning a ``>=`` in an assertion into a ``>`` keeps every byte count
    identical. The path goes in with the bytes so that renaming a file, or moving
    an assertion between two cards, is also a change.
    """
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(str(p.relative_to(root)).encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def ruler_fingerprint() -> dict[str, str]:
    """The measuring instrument, in three separately comparable parts.

    Three parts rather than one hash because a refusal has to be actionable: "the
    scenarios changed" sends the reader to their own edit, while a single combined
    digest sends them nowhere.

    * ``litman``    — the code under test, via :func:`harness.seeds.litman_fingerprint`
      (reused verbatim, never reimplemented and never edited: it is also the seed
      cache key, so changing what it folds in would invalidate every cached seed).
    * ``scenarios`` — the cards themselves. ``litman_fingerprint`` cannot see
      these at all, which is precisely why this exists: an edited assertion is a
      different ruler, and nothing else would notice.
    * ``harness``   — the scoring chain: checker, batch, the adapters, and this
      file. Every link in the chain from stdout to a number is part of the ruler.

    This digests THIS file too. That is correct and not a curiosity: an edit to the
    resume gate is an edit to the instrument, and a gate that exempted itself would
    be the one component able to change unnoticed between two sittings.
    """
    from harness.seeds import litman_fingerprint

    return {
        "litman": litman_fingerprint(),
        "scenarios": _content_digest(
            list(SCENARIOS_DIR.glob("*.yaml")), root=SCENARIOS_DIR
        ),
        # rglob: harness/agents/*.py is as much the ruler as harness/checker.py —
        # an adapter's parse() is what turns stdout into the evidence the checker
        # scores, so an edit there moves the number just as surely.
        "harness": _content_digest(list(HARNESS_DIR.rglob("*.py")), root=HARNESS_DIR),
    }


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------


def session_record(
    *,
    started_at: str,
    agent: str,
    dry: bool,
    model_requested: str,
    model_served: str | None,
    rounds: int,
    cards: list[str],
    ruler: dict[str, str],
) -> dict[str, Any]:
    """One ``type: "session"`` record — the conditions this sitting ran under.

    ``started_at`` is passed IN rather than read from the clock here: a function
    that stamps itself cannot be tested for what it stamps, and the caller has a
    clock anyway.

    ``dry`` says whether any agent was spawned at all, and it has no default ON
    PURPOSE. It is the one condition that cannot be recovered from the others: a
    dry run and a live run of agy journal an IDENTICAL set of fields (agy reports
    no served model, so both record ``None``), which made a dry journal
    indistinguishable from a live one and let hard-coded zeros be resumed into a
    live report. A default here would let the next caller re-open that by saying
    nothing — so the caller is made to answer.
    """
    return {
        "type": "session",
        "started_at": started_at,
        "agent": agent,
        "dry": dry,
        "model_requested": model_requested,
        "model_served": model_served,
        "rounds": rounds,
        "cards": list(cards),
        "ruler": dict(ruler),
    }


def card_record(score: CardScore) -> dict[str, Any]:
    """One ``type: "card"`` record. ``error`` non-null ⇒ the card did not run."""
    return {
        "type": "card",
        "card_id": score.card_id,
        "tag": score.tag,
        "rounds": list(score.rounds),
        "mean": score.mean,
        "usage": dict(score.usage),
        "error": score.error,
    }


def score_from_record(record: dict[str, Any]) -> CardScore:
    """Rebuild a :class:`CardScore` from its journal record."""
    return CardScore(
        card_id=record["card_id"],
        tag=record["tag"],
        rounds=list(record.get("rounds") or []),
        mean=record.get("mean", 0.0),
        usage=dict(record.get("usage") or {}),
        error=record.get("error"),
    )


def append_record(journal: Path, record: dict[str, Any]) -> None:
    """Append one record and flush it to the OS before returning.

    Flushed per record on purpose: the failure this guards against is the process
    dying (SIGKILL on a wall-clock limit, a node going away) between two cards,
    and a buffered journal would lose exactly the cards the resume was for.
    """
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        fh.flush()


def read_records(journal: Path) -> list[dict[str, Any]]:
    """Every record in the journal, in order. Missing file ⇒ ``[]``.

    An unparseable line is skipped rather than raised on, so a process killed
    mid-write costs at most the card it was writing and not the other 29 — which
    is the entire point of journaling per card.

    Note what that tolerance actually is: ANY bad line is skipped, not just a
    truncated last one. The failure it is scoped for can only produce a trailing
    partial line (appends are the only writer, and each is flushed whole), but the
    code does not verify that. So a corrupted FIRST line would be dropped silently
    and :func:`baseline_session` would return the SECOND session as the baseline —
    quietly defeating its own "the first, not the last" anti-drift rule. Nothing
    observed has ever produced that; it is written down because the tolerance is
    wider than its reason, and the next person to widen it should know.
    """
    if not journal.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def baseline_session(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The FIRST session record — the conditions the run's data was defined by.

    The first, not the last: every later sitting is checked against the original,
    so a run cannot drift one tolerable step at a time across five resumes.
    """
    for r in records:
        if r.get("type") == "session":
            return r
    return None


def resumable_scores(records: list[dict[str, Any]]) -> list[CardScore]:
    """Cards that are done and need not be re-run: last record wins (D7).

    "Last wins" is what makes an errored card retry itself on resume with no extra
    switch — its last record carries an ``error``, so it is simply not in here, so
    it runs again. A card that failed and later succeeded is correctly kept.

    Only :data:`RESUMABLE_TAGS` (read its note on routing).
    """
    last: dict[str, dict[str, Any]] = {}
    for r in records:
        if r.get("type") == "card":
            last[r["card_id"]] = r
    return [
        score_from_record(r)
        for r in last.values()
        if r.get("error") is None and r.get("tag") in RESUMABLE_TAGS
    ]


# ---------------------------------------------------------------------------
# The resume gate
# ---------------------------------------------------------------------------


def check_resumable(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    served_model_verifiable: bool = True,
) -> str | None:
    """``None`` if this sitting may continue the baseline's run, else why not.

    Refuses on ANY difference in the conditions, and names the one that differs.
    There is no ``--force``: the two halves of a run that changed instrument mid
    way are not one experiment, and a report that says so in red is a report whose
    red line nobody remembers three months later. The way to resume a run whose
    ruler moved is to not resume it — start a new run dir and measure again.

    ``model_served`` compares by plain equality, which already does everything D1
    and D2 ask for: two different names differ; a name against ``None`` differs
    (an agent that reported its model and then stopped has changed in a way worth
    stopping for); and ``None`` against ``None`` — agy, or two dry runs — is equal,
    so it passes. ``served_model_verifiable`` therefore does not gate the
    comparison; it only sharpens the sentence, because "both sittings reported no
    model" means something categorically different for agy (it never reports one)
    than for claude (something went wrong), and telling agy's operator that their
    models "match" would be reporting ``None == None`` as verification.
    """
    # FIRST, before anything else: was the other half a measurement at all? A dry
    # run spawns nothing and scores every card a hard-coded 0. Those zeros are not
    # data, and the ONLY thing separating them from a real agy run is this flag —
    # agy reports no served model, so a dry session and a live agy session agree on
    # every other field in this record. Without this check, resuming a live agy run
    # on top of a dry journal adopts the fakes as measured cards and publishes them
    # under the model's name: a non-measurement silently becoming a number, which
    # is the one thing this whole file exists to prevent.
    #
    # A MISSING key refuses rather than defaulting: `session_record` gives `dry` no
    # default precisely so a caller cannot re-open this hole by saying nothing, and
    # reading silence here as "live" would hand that back at the comparison layer —
    # a hand-edited dry journal with the key dropped would resume live. Only
    # `session_record` writes these, so a missing key means the record was tampered
    # with or truncated, and neither is a thing to guess about.
    for side, rec in (("this run dir's", baseline), ("this sitting's", current)):
        if not isinstance(rec.get("dry"), bool):
            return (
                f"{side} session record carries no usable 'dry' flag "
                f"({rec.get('dry')!r}), so whether it was a real run or a dry one "
                f"cannot be established. Refusing rather than assuming: a dry run's "
                f"cards are hard-coded zeros. Use a fresh run dir."
            )
    if baseline["dry"] != current["dry"]:
        was, now = (
            ("a DRY run (nothing was spawned)", "live")
            if baseline["dry"]
            else ("a LIVE run", "a dry run")
        )
        return (
            f"dry-vs-live changed: this run dir holds {was} and this sitting is "
            f"{now}. A dry run's cards are hard-coded zeros, not measurements, so "
            f"the two cannot be halves of one report — in either direction. Use a "
            f"separate run dir for dry runs."
        )
    if baseline.get("agent") != current.get("agent"):
        return (
            f"agent changed: this run was measured with "
            f"{baseline.get('agent')!r}, you are now running {current.get('agent')!r}."
        )
    for key, label in (
        ("model_requested", "requested model"),
        ("rounds", "rounds per card"),
    ):
        if baseline.get(key) != current.get(key):
            return (
                f"{label} changed: this run used {baseline.get(key)!r}, "
                f"you are now asking for {current.get(key)!r}."
            )
    if baseline.get("model_served") != current.get("model_served"):
        if not served_model_verifiable:
            # agy reports no model, so any difference here is not a model change
            # (it cannot report one) — it is the harness disagreeing with itself.
            return (
                f"served model record changed: {baseline.get('model_served')!r} -> "
                f"{current.get('model_served')!r}. This agent does not report a "
                f"served model, so both sittings should read None; that they do not "
                f"means the harness, not the agent, changed under this run."
            )
        return (
            f"served model changed: this run's cards were measured by "
            f"{baseline.get('model_served')!r}, this sitting is served by "
            f"{current.get('model_served')!r}. The two halves would not be one "
            f"experiment. Start a new run dir and measure again."
        )
    if set(baseline.get("cards") or []) != set(current.get("cards") or []):
        base_cards = set(baseline.get("cards") or [])
        now_cards = set(current.get("cards") or [])
        return (
            f"card set changed: {len(base_cards - now_cards)} card(s) dropped "
            f"({', '.join(sorted(base_cards - now_cards)) or 'none'}), "
            f"{len(now_cards - base_cards)} added "
            f"({', '.join(sorted(now_cards - base_cards)) or 'none'})."
        )
    base_ruler = baseline.get("ruler") or {}
    now_ruler = current.get("ruler") or {}
    for part, what in (
        ("litman", "the litman package under test"),
        ("scenarios", "the scenario cards (an assertion or a prompt was edited)"),
        ("harness", "the bench harness itself (scoring / adapters / batch)"),
    ):
        if base_ruler.get(part) != now_ruler.get(part):
            return (
                f"ruler changed: {part} ({what}) was {base_ruler.get(part)!r} when "
                f"this run started and is {now_ruler.get(part)!r} now. The cards "
                f"already measured were scored by a different instrument. Do not "
                f"change the ruler during a run; start a new run dir instead."
            )
    return None
