"""Agent adapter layer — one bench, three scaffolds (claude / cursor / agy).

The bench measures *litman*, not a model: every axis it reports must therefore be
attributable to a named ``(agent, model)`` pair, and an axis a given agent
physically cannot expose must read as **not measurable**, never as a zero.

An :class:`AgentAdapter` owns everything that differs between the three CLIs:

* **isolation** (:meth:`prepare`) — which env var re-homes the agent's config, how
  its login is seeded, and where the repo-source skills get installed;
* **invocation** (:meth:`build_argv`) — the flag spelling, including each CLI's
  own permission-bypass flag and its argument-order quirks;
* **evidence** (:meth:`parse`) — how a finished run's ``lit`` argv, ``lit`` stdout,
  skill activation, served model and token counters are recovered.

Everything downstream (``harness.executor.run_card``, ``harness.batch``,
``harness.checker``) stays agent-neutral and consumes the single
:class:`~harness.executor.ExecutorResult` contract.

Capability honesty
------------------

:class:`AgentCapabilities` is a *declared* per-agent fact sheet, not an inference.
It exists so a missing axis is reported from a known property of the agent rather
than guessed from an empty observation. The subtle one is routing:
:data:`NOT_MEASURABLE` is a distinct third state from ``None``, because
``harness.executor.observe_skill_for_utterance`` already spends ``None`` on "the
agent fired no skill" — a routing MISS that belongs in the RA denominator.
Collapsing "this agent has no skill-activation signal at all" into that same
``None`` would silently score such an agent's RA as 0.0 instead of excluding it.

Permission flags
----------------

Two adapters hard-code a flag that disables their agent's tool approval:
``cursor`` uses ``--force`` and ``agy`` uses ``--dangerously-skip-permissions``.
This is authorized for the **bench harness only** (it runs against a disposable
/tmp vault with the real library shadowed) and is the only way to hold the
permission variable constant across the three scaffolds — mixing full-bypass
agents with a narrow-allowlist agent would push the allowlisted one's TRR down on
permissions rather than capability, invalidating the comparison. The product red
line is untouched: nothing under ``src/litman/`` may use or suggest these flags.
Each adapter therefore publishes its ``permission_flags`` verbatim so the report
records how the run was actually authorized (the agents' own event streams cannot
be trusted for this: cursor still reports ``permissionMode: "default"`` while
``--force`` is in effect).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from harness.executor import ExecutorResult

AGENT_NAMES = ("claude", "cursor", "agy")


# ---------------------------------------------------------------------------
# The isolation seam, shared
# ---------------------------------------------------------------------------

#: Env vars that name a config/credential dir by ABSOLUTE path, so they outlive a
#: ``$HOME`` redirect and re-open the isolation seam. :func:`isolated_env` drops
#: every one from the child env; an adapter that legitimately uses one re-sets it
#: to its OWN isolated path afterwards (claude does exactly that with
#: ``CLAUDE_CONFIG_DIR``). ``tests/bench/conftest.py`` clears the same tuple from
#: the TEST process, so the child-side and test-side lists cannot drift.
#:
#: NOT exhaustive, and it cannot be: this is a denylist applied to an inherited
#: ``os.environ``, so it only knows the vars of the agents written so far. Adding
#: an agent means asking which var ITS CLI resolves BEFORE ``$HOME`` and putting
#: it here — that question has a different answer for every vendor, and getting it
#: wrong leaks a real credential rather than failing. The structural alternative
#: (build the child env from an ALLOWLIST instead of copying ``os.environ``) is not
#: taken, because PATH/conda/locale/proxy vars all have to survive and enumerating
#: them is its own denylist in disguise; this is a deliberate trade, not an
#: oversight.
HOME_ESCAPING_CONFIG_VARS: tuple[str, ...] = (
    # cursor's login lives in ~/.config/cursor; every XDG-respecting CLI reads it.
    "XDG_CONFIG_HOME",
    # claude._real_config_dir() reads this FIRST, before ~/.claude.
    "CLAUDE_CONFIG_DIR",
)


def isolated_env(*, home: Path, run_vault: Path, registry_dir: Path) -> dict[str, str]:
    """The child env EVERY adapter starts from: real home sealed, ``lit`` re-aimed.

    Starts from ``os.environ`` so PATH and conda survive — PATH is what resolves
    the agent's bare ``lit``, which is what the skills teach it to type, and it is
    deliberately inherited — then applies the three redirects and drops the vars
    that would otherwise outlive them:

    * ``LIT_LIBRARY`` / ``LITMAN_REGISTRY_DIR`` — the agent's ``lit`` targets the
      run's disposable vault and registry, never the maintainer's real library;
    * ``HOME`` — a per-run home, so nothing installed in the real one (skills above
      all) is visible to the run;
    * every var in :data:`HOME_ESCAPING_CONFIG_VARS` — **dropped, never
      redirected**. Read that tuple's comment before adding an agent: the list is
      a denylist and is only complete for the agents written so far.

    Shared rather than copied per adapter because it *is* the isolation contract
    that ``test_agents_registry`` asserts for every name in :data:`AGENT_NAMES`.
    Three hand-rolled copies is exactly how ``agy`` shipped with no
    ``XDG_CONFIG_HOME`` drop at all while the parametrized test stayed green (the
    test only ever saw an env where the var was already unset, so it asserted
    nothing). An adapter layers its OWN vars on top of the returned dict.
    """
    env = os.environ.copy()
    env["LIT_LIBRARY"] = str(run_vault)
    env["LITMAN_REGISTRY_DIR"] = str(registry_dir)
    env["HOME"] = str(home)
    for var in HOME_ESCAPING_CONFIG_VARS:
        env.pop(var, None)
    return env


# ---------------------------------------------------------------------------
# The "not measurable" sentinel
# ---------------------------------------------------------------------------


class NotMeasurable:
    """Type of :data:`NOT_MEASURABLE`. Compared by identity, never by truthiness.

    Deliberately NOT falsy: a falsy sentinel would slip through the very
    ``if observed:`` / ``if observed is None:`` branches it exists to be
    distinguished from.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "NOT_MEASURABLE"


#: This agent cannot observe the axis AT ALL (an agent-type property).
#:
#: Distinct from ``None``, which on the routing axis already means "the agent
#: fired no skill" — a MISS that counts in the RA denominator. An axis that
#: reports this sentinel is excluded from its metric and tagged in the report's
#: coverage section; it never contributes a 0.
NOT_MEASURABLE = NotMeasurable()


# ---------------------------------------------------------------------------
# Capability declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCapabilities:
    """What a given agent CLI can physically report about its own run.

    Every ``False`` here is an empirically verified absence, and it is what makes
    the corresponding report field an honest ``None`` instead of a fabricated 0.

    * ``tokens``       — emits per-run token counters.
    * ``turns``        — emits a turn count (cursor reports tokens but no turns).
    * ``served_model`` — reports which model actually served the run.
    * ``routing``      — skill activation is observable, so RA can be scored.
    """

    tokens: bool
    turns: bool
    served_model: bool
    routing: bool


# ---------------------------------------------------------------------------
# Model family normalization (explicit table — never a regex guess)
# ---------------------------------------------------------------------------

#: Maps every model string the bench can see — the ids we *request* and the
#: display names agents *report* — onto a normalized family, so a controlled
#: comparison can group "the same weights across three scaffolds".
#:
#: This is a hand-curated lookup ON PURPOSE. A regex over "sonnet 4.6" would
#: happily fold ``"Sonnet 4.6 200K Medium No Thinking"`` and
#: ``"Claude Sonnet 4.6 (Thinking)"`` together AND would keep folding future
#: strings whose differences nobody reviewed. An unknown string maps to ``None``
#: (see :func:`family_of`) — the reader is told we do not know, and the raw
#: ``model_served`` string is always reported verbatim alongside so the
#: thinking/no-thinking and context-window differences stay visible.
#:
#: The table is a REPORTING convenience, not a gate. It only has to know the
#: models someone wants grouped across scaffolds; an external model routed through
#: a proxy will not be in here, and that must cost it nothing (Phase 0 proves a
#: model is PINNED by comparing served against requested, not by naming it).
_MODEL_FAMILY: dict[str, str] = {
    # --- claude: CLI model ids. Verified 2026-07-16 against a real stream-json
    #     `system/init`: claude reports the id VERBATIM, not resolved to a dated
    #     one (fixtures/agent-streams/claude-init-model.raw.jsonl pins this).
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-haiku-4-5-20251001": "claude-haiku-4.5",
    "claude-opus-4-8": "claude-opus-4.8",
    # --- cursor: requests an id, reports a display name ----------------------
    "Sonnet 4.6 200K Medium No Thinking": "claude-sonnet-4.6",
    # Cursor relabeled the same tier later in 2026-07: its display grammar now
    # marks thinking explicitly ("... Thinking") and leaves non-thinking
    # unmarked (verified against the full `cursor-agent models` listing), so
    # the suffixless string below is the SAME non-thinking Sonnet 4.6 the entry
    # above named. Both spellings stay: old transcripts report the old string.
    "Sonnet 4.6 200K Medium": "claude-sonnet-4.6",
    # --- agy: requests a display name, reports nothing -----------------------
    "Claude Sonnet 4.6 (Thinking)": "claude-sonnet-4.6",
}


def family_of(name: str | None) -> str | None:
    """One model string -> its family, or ``None`` when it is not in the table.

    Never guesses. ``None`` means "we cannot name this model's family", which is a
    reporting gap, not a fault: a model can be perfectly well pinned and still be
    absent from a table that only exists to group runs across scaffolds.
    """
    if name is None:
        return None
    return _MODEL_FAMILY.get(name)


def model_family(
    served: str | None, requested: str | None, *, fallback_to_requested: bool
) -> str | None:
    """The family to report for a run, or ``None`` when it cannot be named.

    Prefers the string the agent actually *served*. An unrecognized served string
    stays ``None`` rather than falling back to the request: that is precisely the
    case where trusting the request would mask a mismatch.

    ``fallback_to_requested`` must be set ONLY for an agent that reports no model
    at all (``capabilities.served_model`` is False, i.e. agy) — otherwise the
    family would be derived from the request whenever nothing was harvested, which
    happens in a dry run, a routing-only run, or a run whose spawns all died before
    reporting. In those cases the honest answer is ``None``: nothing observed a
    model, so nothing can name one.
    """
    if served is not None:
        return family_of(served)
    if fallback_to_requested:
        return family_of(requested)
    return None


def known_model_strings() -> list[str]:
    """Every model string the family table recognizes (for error messages)."""
    return sorted(_MODEL_FAMILY)


# ---------------------------------------------------------------------------
# The adapter protocol
# ---------------------------------------------------------------------------


class AgentAdapter(Protocol):
    """What ``harness.executor.run_card`` needs from one agent CLI."""

    #: This adapter's registry key — one of :data:`AGENT_NAMES`.
    name: str
    #: The CLI binary (env-overridable per adapter module).
    bin: str
    #: This agent's own default model — never shared across agents, or a run
    #: would silently be served by whoever's default won the import race.
    default_model: str
    #: Declared, empirically verified capability sheet.
    capabilities: AgentCapabilities
    #: The permission flags this adapter hard-codes, verbatim, for the report.
    permission_flags: tuple[str, ...]
    #: True only for an agent whose CLI honors ``ANTHROPIC_BASE_URL`` /
    #: ``ANTHROPIC_AUTH_TOKEN`` — what ``--base-url`` / ``--auth-token`` export.
    #: Declared here so ``run_bench`` can refuse at the CLI boundary before Phase 0
    #: burns a live spawn; each adapter's ``prepare`` still raises as the backstop.
    #:
    #: Named for the Anthropic shape ON PURPOSE. An agent can have a perfectly good
    #: proxy mode with different env vars (an OpenAI-shaped one, say) and must still
    #: declare False here — ``--base-url`` exports the Anthropic pair and nothing
    #: else. A generic ``supports_anthropic_proxy`` would read as an invitation to declare
    #: True and then be refused for a mode it really has.
    supports_anthropic_proxy: bool
    #: Where THIS agent's ``lit`` argv is recovered from, in prose, for the
    #: qualification sheet's failure text: agy's PATH-shim log is a FILE on disk,
    #: claude's and cursor's is the CLI's own event stream. A reader told the wrong
    #: one debugs the wrong thing.
    #:
    #: Convention, not a contract: the tests check it is a non-empty string and
    #: nothing more. An adapter that says "the event stream" while using a shim
    #: passes. It is a label for a human, and it is only as true as its author.
    evidence_source: str

    def skills_dir(self, base: Path) -> Path:
        """Where THIS agent discovers skills, inside the run's isolated home."""

    def prepare(
        self,
        base: Path,
        *,
        run_vault: Path,
        base_url: str | None = None,
        auth_token: str | None = None,
    ) -> dict[str, str]:
        """Isolate the agent, install the repo-source skills, return a child env."""

    def build_argv(self, prompt: str, *, model: str) -> list[str]:
        """The exact argv to spawn (permission flags included)."""

    def parse(self, stdout: str, *, base: Path) -> ExecutorResult:
        """Recover the run's evidence. ``base`` is the run dir (agy's shim log)."""


def get_adapter(name: str) -> AgentAdapter:
    """Look up an agent adapter by name.

    Imports lazily so ``harness.agents`` stays importable from
    ``harness.executor`` (which the adapter modules import for the
    :class:`~harness.executor.ExecutorResult` contract).
    """
    if name == "claude":
        from harness.agents.claude import ClaudeAdapter

        return ClaudeAdapter()
    if name == "cursor":
        from harness.agents.cursor import CursorAdapter

        return CursorAdapter()
    if name == "agy":
        from harness.agents.agy import AgyAdapter

        return AgyAdapter()
    raise ValueError(
        f"unknown agent {name!r}; known agents: {', '.join(AGENT_NAMES)}"
    )


__all__ = [
    "AGENT_NAMES",
    "HOME_ESCAPING_CONFIG_VARS",
    "NOT_MEASURABLE",
    "AgentAdapter",
    "AgentCapabilities",
    "NotMeasurable",
    "family_of",
    "get_adapter",
    "isolated_env",
    "known_model_strings",
    "model_family",
]
