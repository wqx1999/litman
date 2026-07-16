"""Phase 0 — instrument qualification. Run it before spending a single token.

A benchmark that reports numbers from a broken instrument is worse than one that
reports nothing: the numbers look exactly like real ones. Every check here failed
for real at least once while the three agents were being brought up, and every one
of those failures would have produced a plausible-looking report — a 0% execution
rate that was really an unauthorized tool, an RA that was really the user's own
installed skill copy answering, a "sonnet" run that was really whatever ``auto``
picked.

So this gate runs first, against the CURRENT ``--agent``, and any failure aborts
the whole round non-zero before a card is touched. Its results are ALSO an
output: they land in ``report.json`` under ``qualification`` so a reader can see
what was verified — and, for agy, what could not be.

The seven checks (§6):

1. ``binary``            — ``<bin> --version`` exits 0.
2. ``headless``          — a trivial prompt comes back with non-empty text.
3. ``tool_authorization``— the agent actually RAN ``lit --version`` and got output.
4. ``skill_source``      — the skill it loaded is the repo source: a sentinel
   planted in the isolated copy comes back in the agent's answer. If it comes back
   empty, the agent read someone else's skill (or none).
5. ``evidence_chain``    — the evidence source recorded the call (a shim log entry
   for agy, a parsed lit argv for claude/cursor). This is the one that catches "the
   agent worked fine, the parser did not".
6. ``model_pinned``      — served and requested normalize to the same family.
   SKIPPED for agy, which reports no model — and the skip is written into the
   report as "agy's model was NOT verified", because a silent skip here is how a
   whole run gets served by the wrong model without anyone noticing.
7. ``tokens``            — the counters are actually there. SKIPPED where the
   capability sheet already says they do not exist.

Checks 2/3/5/6/7 share ONE probe spawn; check 4 needs its own (it plants a
sentinel). Both probes are injectable via ``run_card_impl`` so the tests drive the
full gate without spawning anything.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from harness.agents import family_of, get_adapter, known_model_strings
from harness.scenarios import Card

# Prompts kept mechanical: the gate tests the instrument, not the model's flair.
PROBE_PROMPT = "Run the shell command: lit --version — then reply with ONLY its exact output."
SENTINEL_PROMPT = (
    "Your lit-library skill's SKILL.md contains a line starting with "
    "`bench-sentinel:`. Reply with ONLY the value that follows that prefix."
)

# The stdout every `lit --version` starts with; the tool-authorization proof.
LIT_VERSION_PREFIX = "lit, version"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_WARN = "warn"


@dataclass
class QualCheck:
    """One qualification check's outcome. ``detail`` is written for a human."""

    name: str
    status: str  # pass | fail | skip | warn
    detail: str = ""


@dataclass
class Qualification:
    """The Phase 0 record for one ``(agent, model)`` — a gate AND a deliverable."""

    agent: str
    model_requested: str
    model_served: str | None = None
    checks: list[QualCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """False iff any check FAILED. A ``skip`` / ``warn`` does not gate."""
        return not any(c.status == STATUS_FAIL for c in self.checks)


def qualification_to_dict(qual: Qualification) -> dict[str, Any]:
    """JSON projection for the report's ``qualification`` section."""
    return {
        "agent": qual.agent,
        "model_requested": qual.model_requested,
        "model_served": qual.model_served,
        "ok": qual.ok,
        "checks": [
            {"name": c.name, "status": c.status, "detail": c.detail}
            for c in qual.checks
        ],
    }


def format_qualification(qual: Qualification) -> str:
    """One human-readable block, printed before any card runs."""
    mark = {STATUS_PASS: "OK  ", STATUS_FAIL: "FAIL", STATUS_SKIP: "skip", STATUS_WARN: "WARN"}
    lines = [
        f"Phase 0 — instrument qualification: agent={qual.agent} "
        f"model={qual.model_requested}",
        "-" * 60,
    ]
    for c in qual.checks:
        line = f"  [{mark.get(c.status, c.status)}] {c.name}"
        if c.detail:
            line += f": {c.detail}"
        lines.append(line)
    lines.append("-" * 60)
    lines.append("qualified" if qual.ok else "NOT QUALIFIED — no cards will run")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _lit_version_stdout(result: Any) -> str:
    """The captured stdout of the probe's ``lit --version``, across evidence sources."""
    return "\n".join(tr.content for tr in getattr(result, "tool_results", []))


def _ran_lit(result: Any) -> bool:
    """True iff the evidence source recorded a ``lit`` invocation."""
    return bool(getattr(result, "lit_calls", []))


def _probe_base(work_root: Path) -> tuple[Path, Path]:
    """A throwaway run dir + vault for one probe (local /tmp, like every run dir).

    An empty vault is enough: nothing the gate asks for reads the library.
    """
    base = Path(work_root) / f"bench-qual-{uuid.uuid4().hex}"
    vault = base / "vault"
    vault.mkdir(parents=True)
    return base, vault


def _plant_sentinel(adapter: Any, sentinel: str) -> Callable[[Path, dict], None]:
    """Build the ``on_prepared`` hook that marks the isolated lit-library skill.

    Fires between "the adapter installed the repo-source skills" and "the agent is
    spawned", so the sentinel lands in the copy under test and nowhere else. The
    repo source is never touched.
    """

    def _hook(base: Path, env: dict) -> None:
        skill = adapter.skills_dir(base) / "lit-library" / "SKILL.md"
        if not skill.is_file():
            raise RuntimeError(
                f"no lit-library SKILL.md at {skill} after {adapter.name}'s "
                "prepare(): the repo skills did not install where this agent "
                "looks for them."
            )
        with skill.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\nbench-sentinel: {sentinel}\n")

    return _hook


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def qualify(
    agent: str,
    *,
    model: str | None = None,
    work_root: Path = Path("/tmp"),
    base_url: str | None = None,
    auth_token: str | None = None,
    run_card_impl: Callable[..., Any] | None = None,
    version_impl: Callable[[str], int] | None = None,
    skill_source_is_fatal: bool = True,
) -> Qualification:
    """Qualify one ``(agent, model)`` pair. Never raises for a failed check.

    A check that cannot even be attempted (the binary is missing, agy's login token
    is not there) is recorded as a FAIL with the human-readable reason rather than
    exploding: the caller prints the whole sheet and exits non-zero, so the user
    sees every problem at once instead of one per re-run.

    ``base_url`` / ``auth_token`` MUST be threaded through from the caller: the
    probes have to run under exactly the auth the cards will run under. Qualifying
    an external-model run against the default Anthropic endpoint asks the user's
    own OAuth for a model it has never heard of, gets rejected, and gates a run
    that would have been perfectly fine.

    ``skill_source_is_fatal=False`` downgrades check 4 to a warning — the escape
    hatch for an agent whose skills provably cannot be isolated. Such a run is
    still measuring SOMETHING, but not the repo source, and the warning is carried
    into the report so no reader can miss it.

    ``run_card_impl`` / ``version_impl`` are injectable so the tests drive the gate
    with canned results and never spawn (M34 §3.5 hard boundary).
    """
    adapter = get_adapter(agent)
    model = model or adapter.default_model
    if run_card_impl is None:
        from harness.executor import run_card as run_card_impl  # type: ignore[assignment]

    qual = Qualification(agent=agent, model_requested=model)
    caps = adapter.capabilities

    # --- 1. binary present ---------------------------------------------------
    rc = _check_binary(adapter, version_impl)
    qual.checks.append(rc)
    if rc.status == STATUS_FAIL:
        # Nothing downstream can be attempted without the binary; the rest would
        # be a wall of identical FileNotFoundErrors.
        return qual

    # --- 2/3/5/6/7 — one probe spawn -----------------------------------------
    base, vault = _probe_base(work_root)
    try:
        try:
            result = run_card_impl(
                Card(id="qual-probe", intent=PROBE_PROMPT, fixtures=[]),
                vault,
                fixtures_pdfs_dir=base / "_no_fixtures",
                agent=agent,
                model=model,
                base_url=base_url,
                auth_token=auth_token,
            )
        # A probe that raises IS a gate failure: report it, never propagate.
        except Exception as e:
            # Not "headless": the probe never got as far as being driven (agy's
            # login token is missing, the skills would not install, ...). Checks
            # 2/3/5/6/7 all read off this one run, so none of them can be
            # evaluated — say so rather than leave a reader hunting for them.
            qual.checks.append(
                QualCheck(
                    "probe",
                    STATUS_FAIL,
                    f"the qualification probe could not run, so headless / "
                    f"tool_authorization / evidence_chain / model_pinned / tokens "
                    f"are all unevaluated: {e}",
                )
            )
            return qual

        qual.model_served = getattr(result, "model_served", None)
        qual.checks.extend(_checks_from_probe(agent, model, caps, result))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    # --- 4. the skill under test is the repo source --------------------------
    qual.checks.append(
        _check_skill_source(
            agent,
            model,
            adapter,
            work_root,
            run_card_impl,
            base_url=base_url,
            auth_token=auth_token,
            fatal=skill_source_is_fatal,
        )
    )
    return qual


def _check_binary(adapter: Any, version_impl: Callable[[str], int] | None) -> QualCheck:
    if version_impl is None:

        def version_impl(binary: str) -> int:  # type: ignore[misc]
            return subprocess.run(
                [binary, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            ).returncode

    try:
        code = version_impl(adapter.bin)
    except OSError as e:
        return QualCheck("binary", STATUS_FAIL, f"{adapter.bin!r} not runnable: {e}")
    if code != 0:
        return QualCheck(
            "binary", STATUS_FAIL, f"{adapter.bin} --version exited {code}"
        )
    return QualCheck("binary", STATUS_PASS, adapter.bin)


def _checks_from_probe(agent: str, model: str, caps: Any, result: Any) -> list[QualCheck]:
    """Checks 2/3/5/6/7, all read off the single ``lit --version`` probe run."""
    checks: list[QualCheck] = []

    # 2. headless drive
    final = str(getattr(result, "final_text", "") or "").strip()
    if getattr(result, "timed_out", False):
        checks.append(QualCheck("headless", STATUS_FAIL, "probe timed out"))
    elif not final:
        checks.append(
            QualCheck(
                "headless",
                STATUS_FAIL,
                f"empty final text (exit={getattr(result, 'exit_code', '?')})",
            )
        )
    else:
        checks.append(QualCheck("headless", STATUS_PASS, f"answered {len(final)} chars"))

    # 3. tool authorization: it really ran lit and really got output back
    out = _lit_version_stdout(result)
    if LIT_VERSION_PREFIX in out:
        checks.append(QualCheck("tool_authorization", STATUS_PASS, out.strip()[:60]))
    else:
        checks.append(
            QualCheck(
                "tool_authorization",
                STATUS_FAIL,
                "the agent never got 'lit, version ...' back — its tool approval "
                "most likely blocked the shell call (this looks exactly like a "
                "broken CLI in the scores, so it gates)",
            )
        )

    # 5. evidence chain recorded the call
    if _ran_lit(result):
        checks.append(
            QualCheck(
                "evidence_chain",
                STATUS_PASS,
                f"{len(result.lit_calls)} lit call(s) recorded",
            )
        )
    else:
        source = "lit-calls.jsonl (PATH shim)" if agent == "agy" else "the event stream"
        checks.append(
            QualCheck(
                "evidence_chain",
                STATUS_FAIL,
                f"no lit argv recovered from {source}; every card would score 0 "
                "for reasons that have nothing to do with litman",
            )
        )

    # 6. model pinned
    checks.append(_check_model_pinned(agent, model, caps, result))

    # 7. token counters
    #
    # Asserts the counters are NON-ZERO, not merely present. Presence proves
    # nothing: the adapters normalize onto a fixed internal key set, so the keys
    # are there whatever the agent sent, and a key rename upstream shows up as a
    # tidy row of zeros rather than a missing key. Any real generation burns both
    # input and output tokens, so a zero in either means we are not reading the
    # counters we think we are — and the cost of not noticing is a published
    # token total of 0.
    usage = getattr(result, "usage", None) or {}
    if not caps.tokens:
        checks.append(
            QualCheck(
                "tokens",
                STATUS_SKIP,
                f"{agent} emits no token counters; the report's tokens section "
                "is None for this agent, not 0",
            )
        )
    elif usage.get("input_tokens") and usage.get("output_tokens"):
        checks.append(
            QualCheck(
                "tokens",
                STATUS_PASS,
                f"in={usage.get('input_tokens')} out={usage.get('output_tokens')}",
            )
        )
    else:
        checks.append(
            QualCheck(
                "tokens",
                STATUS_FAIL,
                f"{agent} should report counters but the probe carried {usage!r}. "
                "A live generation cannot cost 0 input + 0 output tokens, so the "
                "counter keys have most likely been renamed upstream — which would "
                "publish this run's token totals as 0",
            )
        )

    return checks


def _check_model_pinned(agent: str, model: str, caps: Any, result: Any) -> QualCheck:
    """Check 6. Proves the model is PINNED — which is not the same as NAMEABLE.

    The family table only exists to group runs across scaffolds; it will never know
    every model, and an external model behind a proxy is guaranteed not to be in
    it. So the family is consulted only when it is actually needed — to decide
    whether two DIFFERENT strings mean the same model. When the agent echoes back
    exactly what we asked for, the model is pinned by definition and the table has
    nothing to add.

    An unrecognized string is still never guessed into a family (the report's
    ``model_family`` stays ``None``); it just no longer gates a run whose model is
    demonstrably pinned.
    """
    served = getattr(result, "model_served", None)
    if not caps.served_model:
        return QualCheck(
            "model_pinned",
            STATUS_SKIP,
            f"{agent} does not report the model it served: this run's model is "
            f"UNVERIFIED — we asked for {model!r} and have only its word for it",
        )
    if served is None:
        return QualCheck(
            "model_pinned",
            STATUS_FAIL,
            f"{agent} should report a served model but the probe carried none",
        )
    if served == model:
        # Echoed verbatim: pinned, whether or not we can name its family.
        return QualCheck("model_pinned", STATUS_PASS, f"served exactly {served!r}")

    # The strings differ (cursor reports a display name for the id we sent), so
    # the only way to tell "same model, different spelling" from "wrong model" is
    # the table.
    want = family_of(model)
    got = family_of(served)
    if want is not None and got is not None:
        if want == got:
            return QualCheck("model_pinned", STATUS_PASS, f"{served!r} -> {want}")
        return QualCheck(
            "model_pinned",
            STATUS_FAIL,
            f"requested {model!r} ({want}) but {agent} served {served!r} ({got})",
        )
    unknown = model if want is None else served
    return QualCheck(
        "model_pinned",
        STATUS_FAIL,
        f"{agent} served {served!r} but we requested {model!r}, and {unknown!r} is "
        f"not in the model-family table, so the two cannot be shown to be the same "
        f"model. Add it to harness.agents._MODEL_FAMILY (known: "
        f"{', '.join(known_model_strings())}) — deliberately NOT guessed, since a "
        "regex match here would hide a thinking/no-thinking swap",
    )


def _check_skill_source(
    agent: str,
    model: str,
    adapter: Any,
    work_root: Path,
    run_card_impl: Callable[..., Any],
    *,
    base_url: str | None = None,
    auth_token: str | None = None,
    fatal: bool,
) -> QualCheck:
    """Check 4. Proves the agent read OUR skill copy, not the user's installed one."""
    sentinel = f"LITMAN-BENCH-{uuid.uuid4().hex[:12].upper()}"
    base, vault = _probe_base(work_root)
    try:
        result = run_card_impl(
            Card(id="qual-sentinel", intent=SENTINEL_PROMPT, fixtures=[]),
            vault,
            fixtures_pdfs_dir=base / "_no_fixtures",
            agent=agent,
            model=model,
            base_url=base_url,
            auth_token=auth_token,
            on_prepared=_plant_sentinel(adapter, sentinel),
        )
    # A probe that raises IS a gate failure: report it, never propagate.
    except Exception as e:
        return QualCheck("skill_source", STATUS_FAIL, f"sentinel probe raised: {e}")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    answered = str(getattr(result, "final_text", "") or "")
    if sentinel in answered:
        return QualCheck("skill_source", STATUS_PASS, "repo-source skill confirmed")
    return QualCheck(
        "skill_source",
        STATUS_FAIL if fatal else STATUS_WARN,
        f"the sentinel planted in {agent}'s isolated lit-library SKILL.md did not "
        "come back: the agent is reading some OTHER copy of the skill, so this run "
        "would not be measuring the repo source"
        + ("" if fatal else " (accepted as a warning for this agent — see report)"),
    )
