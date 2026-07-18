"""Phase E — live-agent executor driver (agent-neutral).

This is the part that actually *runs the benchmark*: it spawns a separate,
headless agent process (the "executor"), hands it ONLY a card's intent + fixture
paths (scenarios §0 handoff discipline, enforced by
:func:`harness.scenarios.executor_view`), and lets the agent decide which ``lit``
commands to run. Nothing here tells the agent which command to call — that
decision is exactly what litman-bench measures (ADR-007 agent-as-primary-consumer).

Which agent is a parameter (``claude`` / ``cursor`` / ``agy``). Everything that
differs between them — isolation seam, argv, evidence recovery — belongs to an
:class:`~harness.agents.AgentAdapter`; this module owns only what is common:

* fixture staging (``lit add`` MOVES the pdf, so the agent only ever sees a copy),
* the prompt,
* the neutral cwd,
* the spawn + timeout,
* the :class:`ExecutorResult` contract every consumer downstream reads.

Isolation (M34 §4 red line) is welded into the adapters, never left to the agent.
Every adapter's ``prepare`` must point ``LIT_LIBRARY`` at the disposable **run
vault** (not unset, unlike the deterministic seed/check path: a naive user has
their vault configured via env, so the agent's bare ``lit add`` must land
somewhere — we make that "somewhere" the throwaway /tmp copy, and the real vault
is thereby shadowed and unreachable), redirect ``LITMAN_REGISTRY_DIR`` into the
run dir, and install the **repo-source** skills into the agent's own isolated
skills dir via :func:`install_repo_skills`, so the skill under test == the repo
source, never the user's already-installed copy.

:class:`ExecutorResult` was agent-neutral from the start — that is why adding two
more agents needed no change to ``checker`` / ``seeds`` / the scenario corpus.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from harness.scenarios import Card, executor_view
from harness.seeds import LIT_BIN

if TYPE_CHECKING:
    from harness.agents import NotMeasurable

# A generous ceiling: one card may chain several lit calls + PDF reads.
DEFAULT_TIMEOUT_S = 600


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class LitCall:
    """One ``lit`` invocation the agent issued.

    ``tool_use_id`` (when present) pairs the call with the record carrying its
    stdout: a stream-json ``tool_use`` id for claude/cursor, a synthetic
    ``shim-<n>`` for agy's PATH-shim log.
    """

    argv: list[str]
    raw: str
    tool_use_id: str | None = None


@dataclass
class ToolResult:
    """One captured tool output: the stdout of a prior ``lit`` call.

    ``tool`` is the originating tool name when known (else ``""``), ``content``
    is the flattened text of the result.
    """

    tool: str
    content: str
    tool_use_id: str | None = None


@dataclass
class ExecutorResult:
    """Everything the executor observed about one agent run.

    Agent-neutral by construction: each adapter fills these fields from its own
    evidence source, and every consumer downstream reads only this.

    ``skills`` = routing labels, in order (claude: ``Skill`` tool_use names;
    cursor: the ``<name>`` of each ``skills/<name>/SKILL.md`` it read; agy: always
    empty — and empty here means "no signal exists", which is why the routing axis
    is short-circuited to ``NOT_MEASURABLE`` for agy upstream rather than read off
    this list).
    ``lit_calls`` = the agent's ``lit`` invocations (the checker's argv log).
    ``tool_results`` = captured outputs (lit stdout lives here).
    ``tool_names`` = every tool name seen (for design-time observation).
    ``final_text`` = the agent's final answer. ``exit_code`` is the agent process
    exit; ``timed_out`` flags a killed run.

    ``usage`` = per-spawn token accounting, normalized by the adapter to one
    internal key set (input / output / cache_creation / cache_read + optional
    num_turns; dollar cost deliberately not captured — see
    :func:`harness.agents.claude._parse_usage`). ``{}`` when the agent reports no
    usage at all — either because it emits no counters (agy) or because the run
    aborted before any were reported. ``{}`` means "not observed" everywhere
    downstream; it never becomes a zero.

    ``argv`` = the command line the HARNESS built. Recorded because an agent's own
    stream cannot be trusted to describe how it was authorized (cursor reports
    ``permissionMode: "default"`` while ``--force`` is in effect), so how a run was
    actually invoked must come from our side of the boundary.
    ``model_served`` = the model string the agent reported serving, verbatim and
    un-normalized; ``None`` when the agent does not report one (agy).
    """

    skills: list[str] = field(default_factory=list)
    lit_calls: list[LitCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    final_text: str = ""
    exit_code: int = 0
    timed_out: bool = False
    raw_events: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    argv: list[str] = field(default_factory=list)
    model_served: str | None = None

    def as_jsonl_records(self) -> list[dict]:
        """Project the agent's lit calls into the checker's jsonl record shape.

        Each record carries the parsed ``argv`` (the ``ran``/``not_ran`` evidence)
        plus the best-effort ``stdout`` of that call, paired from the matching
        result. Pairing is by ``tool_use_id`` when both sides carry one; otherwise
        an empty string (documented best-effort — a lit call whose result we cannot
        map carries no stdout, never a wrong one).
        """
        by_id: dict[str, str] = {
            tr.tool_use_id: tr.content
            for tr in self.tool_results
            if tr.tool_use_id
        }
        return [
            {
                "argv": c.argv,
                "raw": c.raw,
                "stdout": by_id.get(c.tool_use_id or "", ""),
            }
            for c in self.lit_calls
        ]


def stdout_blob(result: ExecutorResult) -> str:
    """Join every captured tool output into one searchable blob.

    Used by the executor-stdout evidence path: ``stdout_contains`` greps this
    when scoring "the agent's lit output mentions X" without needing to map a
    specific call to its result.
    """
    return "\n".join(tr.content for tr in result.tool_results)


# ---------------------------------------------------------------------------
# Repo-source skill install (target dir supplied by the adapter)
# ---------------------------------------------------------------------------


def install_repo_skills(skills_dir: Path, *, only: str | None = None) -> None:
    """Install the repo-source litman skills into ``skills_dir``.

    Uses ``lit install-skill --parent-dir`` so the skill text == the repo source
    under test, not whatever is already in the user's home. ``skills_dir`` is the
    parent that will hold ``<name>/SKILL.md``, and each agent discovers skills
    somewhere different, so the caller is the adapter
    (:meth:`~harness.agents.AgentAdapter.skills_dir`) — this function has no
    opinion about which agent it is serving.
    """
    skills_dir = Path(skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)
    args = ["install-skill", "--parent-dir", str(skills_dir), "--force"]
    if only:
        args += ["--skill", only]
    proc = subprocess.run(
        [str(LIT_BIN), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "install-skill failed:\n" + (proc.stderr or proc.stdout)
        )


# ---------------------------------------------------------------------------
# Run one card through a live executor
# ---------------------------------------------------------------------------


def build_prompt(card: Card, staged_fixtures: list[Path]) -> str:
    """Compose the user-turn prompt: verbatim intent + staged fixture paths.

    Only the intent + fixture paths are included (handoff discipline). The
    staged paths are real, agent-readable copies (``lit add`` MOVES the file, so
    we never hand it the canonical fixture).
    """
    parts = [card.intent.strip()]
    if staged_fixtures:
        parts.append("")
        parts.append("可用文件:")
        for p in staged_fixtures:
            parts.append(f"  - {p}")
    return "\n".join(parts)


def neutral_cwd_for(run_vault: Path) -> Path:
    """The neutral cwd dir for a run vault (``<run_dir>/cwd``).

    A single source of truth for the convention :func:`run_card` follows, so the
    batch adapter can locate the same dir (where ``lit export`` drops ``refs.bib``,
    scored by the checker's ``file_*`` verbs) without re-deriving it or widening
    the agent-neutral :class:`ExecutorResult` contract (M34 §3.6.A).

    Always under the run root, which is always local ``/tmp``: an agent's cwd on
    NFS costs minutes per spawn (measured: 14s local vs >2m20s on /net for the
    same empty dir), which across a full suite is hours.
    """
    return Path(run_vault).parent / "cwd"


ACTIVE_VAULT_NAME = "bench"


def register_active_vault(run_vault: Path, env: dict[str, str]) -> None:
    """Register the just-built run vault as the active vault in the run's
    disposable registry, so `lit vault list`/`use` agree with `lit list`/`export`.

    The isolation sets LIT_LIBRARY at the run vault (so list/export resolve it)
    but leaves LITMAN_REGISTRY_DIR pointing at an empty registry, so `lit vault
    list` reports "No vaults registered". An agent that checks vault registration
    before acting believes that and gives up — its correct-given-the-lie refusal
    then scores as a failure. Registering the run vault (which LIT_LIBRARY already
    names) as active makes the environment self-consistent and matches how a real
    machine always looks: exactly one active vault.

    Safe: the registry is a throwaway dir under the run root; `--use` cannot reach
    the real library. Uses the CLI, never hand-edits vaults.yaml (lit forbids it).
    Loud on failure: a non-zero exit means the run vault is not a vault (no
    lit-config.yaml) — a broken seed, not something to score around.
    """
    proc = subprocess.run(
        [str(LIT_BIN), "vault", "add", ACTIVE_VAULT_NAME, str(run_vault), "--use"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not register the run vault as active: "
            f"`lit vault add {ACTIVE_VAULT_NAME} <run-vault> --use` exited "
            f"{proc.returncode}\nstdout:\n{proc.stdout}\n"
            f"stderr:\n{getattr(proc, 'stderr', '')}"
        )


def run_card(
    card: Card,
    run_vault: Path,
    *,
    fixtures_pdfs_dir: Path,
    agent: str = "claude",
    model: str | None = None,
    base_url: str | None = None,
    auth_token: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    on_prepared: Callable[[Path, dict[str, str]], None] | None = None,
) -> ExecutorResult:
    """Run one card end-to-end against a live agent.

    Caller owns ``run_vault`` lifecycle (a cp of the right seed). This function
    resolves the adapter, lets it isolate + install skills + build the child env,
    stages the card's fixture PDFs into an agent-readable handoff dir, spawns the
    agent, and hands the raw stdout back to the adapter to parse. It does NOT
    score — that is the checker's job on the returned ``run_vault`` +
    ``result.as_jsonl_records()``.

    ``model`` defaults to the ADAPTER's own default: the three agents do not share
    a model namespace, so one shared default would silently serve a different
    model per agent.

    ``base_url`` / ``auth_token`` select claude's auth mode (M34 §3.6.B): ``None``
    (default) is Anthropic OAuth; a set ``base_url`` is external mode (proxy).
    The other adapters reject them rather than ignore them — a silently
    un-proxied run is a wrong data point, not a warning.

    ``on_prepared(base, env)`` is a Phase 0 seam: it fires after the adapter has
    isolated the run but before the agent is spawned, so the qualification gate can
    plant its sentinel in the isolated skills dir. Unset on every scoring path.
    """
    from harness.agents import get_adapter  # lazy: the adapters import this module

    adapter = get_adapter(agent)
    run_vault = Path(run_vault)
    base = run_vault.parent  # the per-run /tmp dir created by RunVault
    if model is None:
        model = adapter.default_model

    env = adapter.prepare(
        base, run_vault=run_vault, base_url=base_url, auth_token=auth_token
    )
    register_active_vault(run_vault, env)
    if on_prepared is not None:
        on_prepared(base, env)

    # Neutral cwd OUTSIDE litman_dev (naive-user persona; M34 §0). A fresh dir
    # under the run root — no repo context to lean on. Not always empty: cursor's
    # repo-source skills are delivered through it (CursorAdapter.skills_dir puts
    # them in <cwd>/.claude/skills — the only place a HOME-redirected cursor
    # discovers skills), so prepare() may have created it already.
    neutral_cwd = neutral_cwd_for(run_vault)
    neutral_cwd.mkdir(parents=True, exist_ok=True)

    # Stage fixtures into an agent-readable handoff dir (lit add MOVES the pdf).
    handoff = base / "handoff"
    handoff.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    view = executor_view(card, fixtures_dir=fixtures_pdfs_dir)
    for src in view.fixtures:
        dst = handoff / src.name
        if src.is_file():
            shutil.copy2(src, dst)
        staged.append(dst)

    prompt = build_prompt(card, staged)
    argv = adapter.build_argv(prompt, model=model)

    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            env=env,
            cwd=str(neutral_cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        stdout, exit_code = proc.stdout, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        exit_code, timed_out = -1, True

    result = adapter.parse(stdout, base=base)
    result.exit_code = exit_code
    result.timed_out = timed_out
    result.argv = argv
    return result


def observe_skill_for_utterance(
    utterance: str,
    run_vault: Path,
    *,
    fixtures_pdfs_dir: Path,
    agent: str = "claude",
    model: str | None = None,
    base_url: str | None = None,
    auth_token: str | None = None,
    usage_sink: list[dict] | None = None,
) -> str | None | NotMeasurable:
    """Route ONE utterance through the executor and return the skill it fired.

    A routing card (scenarios §I) is a bag of utterances, each scored by which
    skill the agent activates — NOT by an execution end-state. This wraps
    :func:`run_card` with a synthetic single-utterance :class:`Card` (no fixtures:
    routing is pure classification).

    THREE return states, and the difference between the last two is the whole
    honesty of the RA axis:

    * ``"<skill>"``       — the agent activated that skill.
    * ``None``            — the agent activated NO skill. For a skill-equipped
      agent this is a routing MISS (the skill was there and should have fired), and
      it counts in the RA denominator via :func:`harness.routing.score_routing`.
    * :data:`~harness.agents.NOT_MEASURABLE` — this agent exposes no
      skill-activation signal at all, so its RA is not a number we have. Returning
      ``None`` here instead would make an unmeasurable agent look like one that
      missed every single utterance: RA 0.0, reported with a straight face.

    The not-measurable case returns WITHOUT spawning: the capability is a known
    property of the agent, so there is nothing to learn from ~14 classification
    spawns per routing card.

    When ``usage_sink`` is provided, this probe's token ``usage`` is appended to it
    (one dict per spawn) so the routing axis's spawns are counted in the run's
    grand-total cost, not silently dropped.

    This is the SOLE executor touchpoint for the routing axis (M34 §3.6.A) — like
    :func:`run_card`, it spawns a live agent, so it is exercised ONLY under live
    authorization (Phase G), never inside /dev.
    """
    from harness.agents import NOT_MEASURABLE, get_adapter

    if not get_adapter(agent).capabilities.routing:
        return NOT_MEASURABLE

    card = Card(id="routing-probe", intent=str(utterance), fixtures=[])
    result = run_card(
        card,
        run_vault,
        fixtures_pdfs_dir=fixtures_pdfs_dir,
        agent=agent,
        model=model,
        base_url=base_url,
        auth_token=auth_token,
    )
    if usage_sink is not None and result.usage:
        usage_sink.append(result.usage)
    return result.skills[0] if result.skills else None
