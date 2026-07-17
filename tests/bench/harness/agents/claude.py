"""Claude Code adapter — driving ``claude -p`` in an isolated HOME + config dir.

Everything here except the isolation seam is the pre-multi-agent executor's
claude code, moved verbatim from ``harness.executor``: the argv, the stream-json
parsing and the evidence recovery are unchanged on purpose, because how a run's
evidence is GATHERED must not drift silently under a comparison.

Isolation, and why it changed. ``CLAUDE_CONFIG_DIR`` re-homes the skills and the
config, so :func:`seed_auth` copies the OAuth credential back in — and ONLY the
credential, never ``settings.json``, so the executor's settings stay clean and
reproducible. The permission mode is set on the CLI, not in a settings file.
``HOME`` is redirected TOO, which cursor and agy always did and claude did not.
What that buys, measured rather than assumed (2026-07-16):

* ``lit health-check`` run BY THE AGENT no longer walks the user's real
  ``~/.claude/skills``. It used to: the check resolves installed skills through
  ``Path.home()`` (``litman.core.skill``), the ``lit`` subprocess inherits this
  env, and a real home with a stale installed copy yields two ``skill_drift``
  warnings + exit 1. So an agent asked "is my library clean?" was told "no" — by
  findings describing the MAINTAINER's laptop, about a vault that was in fact
  clean. Redirected, those skills read as absent (ADR-015 opt-out) and the agent
  sees exit 0. What this fixes is the agent's ANSWER, and only that.
* The user's ``$HOME``-relative state is out of reach generally — the same
  property cursor and agy already had. (``$XDG_CONFIG_HOME`` is popped too, and
  it is the only override of this kind we set; the other ``XDG_*`` dirs are not
  set in this environment and are not handled — same footing as cursor.)

What it does NOT fix, measured after the change, because the obvious reading is
wrong: **any card's verdict**. The ``health: clean`` oracle
(``harness.checker._health_issues``) calls ``run_all_checks`` in the HARNESS
process against the run-vault path. It never reads the agent's ``lit
health-check``, and no child env var reaches it — so it still returns those same
two ``skill_drift`` warnings today. Every ``health: clean`` card is green only
because the verb counts ``severity == "error"``. Nothing here cleaned a card up.

What it also does NOT buy: the ``skills`` list on
claude's ``system/init`` is **unchanged by the redirect** — 18 before, the same 18
after, name for name. Those 18 are 16 skills BUILT INTO the claude CLI plus the 2
repo-source litman ones; the user's own ``~/.claude/skills`` never appeared in it,
because ``CLAUDE_CONFIG_DIR`` was already re-homing the skills dir correctly. So
"claude sees 18 candidates where cursor and agy see 2" is a product difference
between the CLIs (claude ships built-ins; the others do not), NOT an isolation
leak, and no env var here will close it. Do not read the count as a seam.

This does move claude off the exact env the 2026-06-04 haiku baseline ran under —
accepted deliberately (see the ruler-audit spec): that comparability was already
gone, since both the product and the scenario cards changed underneath the
baseline, and a half-closed seam only hides which of the two moved.
``XDG_CONFIG_HOME`` is dropped alongside ``HOME`` for the reason cursor
documents: a set ``XDG_CONFIG_HOME`` points back at the real ``~/.config`` and
quietly re-opens the seam that ``HOME`` just closed.

Evidence: the ``--output-format stream-json --verbose`` event stream. ``Skill``
tool_use blocks give the routing label; ``Bash`` commands are split on shell
separators to recover ``lit`` argv; the paired ``tool_result`` blocks carry that
argv's stdout.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from harness.agents import AgentCapabilities, isolated_env

# Moved to a shared module now that opencode needs the same splitter (D2). Kept
# importable from THIS namespace on purpose: `test_executor` and the claude tests
# do `from harness.agents.claude import _lit_calls_from_bash`, and this re-export
# means the move is behavior-preserving down to the import path.
from harness.agents._shell import _lit_calls_from_bash
from harness.executor import (
    ExecutorResult,
    LitCall,
    ToolResult,
    install_repo_skills,
)

# The `claude` CLI (headless). Overridable for a different install location.
CLAUDE_BIN = os.environ.get("LITMAN_BENCH_CLAUDE_BIN", "claude")

# v0 default tier (user ruling 2026-06-02): Sonnet — capable enough that a
# working harness resolves cards, so a failure points at litman not the model;
# cheaper than Opus for the repeat-heavy noise pass. Weak-tier sweeps come later.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Claude Code's permission bypass, as passed. Recorded in the report verbatim.
PERMISSION_FLAGS = ("--permission-mode", "bypassPermissions")


# ---------------------------------------------------------------------------
# Isolated config + auth
# ---------------------------------------------------------------------------


def _oauth_token_from_env() -> str | None:
    """The operator's static ``claude setup-token`` credential, or ``None``.

    Read from the HARNESS process env, the same source as ``_real_config_dir()``:
    it is an operator credential, not a per-card input, so it is never plumbed
    through ``run_card``. Present ⇒ the token path (no rotating credential copied,
    no refresh, no rotation race); absent ⇒ today's ``seed_auth`` snapshot path.
    An empty string counts as absent.
    """
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    return tok or None


def _real_config_dir() -> Path:
    """The user's real Claude Code config dir (where auth credentials live).

    Reads the HARNESS process's env and ``Path.home()``, both of which are the
    real ones: the redirected ``HOME`` this adapter builds lives only in the
    CHILD's env dict, so it cannot reach back and hide the credential we are
    about to copy out of here.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def seed_auth(config_dir: Path) -> None:
    """Copy the OAuth credentials into the isolated config dir so the executor
    can authenticate.

    We isolate ``CLAUDE_CONFIG_DIR`` to control *which* skills load (repo source
    only, never the user's installed copy), but isolation also hides the user's
    login. The minimal fix is to copy ``.credentials.json`` only — NOT
    ``settings.json`` (keeping the executor's settings clean + reproducible;
    permission mode is set on the CLI). Mode 0600 is preserved by ``copy2``.
    """
    cred = _real_config_dir() / ".credentials.json"
    if cred.is_file():
        shutil.copy2(cred, config_dir / ".credentials.json")


def executor_env(
    run_vault: Path,
    registry_dir: Path,
    config_dir: Path,
    home: Path,
    *,
    base_url: str | None = None,
    auth_token: str | None = None,
) -> dict[str, str]:
    """Child env for the ``claude`` executor process.

    The shared isolation seam (:func:`harness.agents.isolated_env`) does the part
    every agent needs — ``LIT_LIBRARY`` / ``LITMAN_REGISTRY_DIR`` / ``HOME``
    redirected, ``XDG_CONFIG_HOME`` dropped. Layered on top, claude-only:

    * ``CLAUDE_CONFIG_DIR=<config_dir>`` — isolated skills + claude config. It does
      NOT subsume the ``HOME`` redirect: it re-homes the config dir only, and
      everything claude reads from ``$HOME`` directly stayed reachable until that
      redirect existed. See the module docstring.

    ``home`` is required rather than defaulted precisely because it is a seam: an
    optional one would let a new call site inherit the real ``HOME`` by saying
    nothing, which is the bug this parameter exists to make impossible.

    Auth modes. When ``base_url`` is ``None`` (default Anthropic mode) auth is
    OAuth: either the copied ``.credentials.json`` or, when the operator exports
    ``CLAUDE_CODE_OAUTH_TOKEN``, that static non-rotating token injected here
    instead (the two are mutually exclusive — see :meth:`ClaudeAdapter.prepare`).
    When ``base_url`` is set (external mode: ``claude`` CLI pointed at an
    Anthropic-compatible proxy such as LiteLLM / claude-code-router) we export
    ``ANTHROPIC_BASE_URL`` + the auth token so the proxy authenticates; neither
    OAuth path applies in this mode.
    """
    env = isolated_env(home=home, run_vault=run_vault, registry_dir=registry_dir)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if base_url is not None:
        # env var names confirmed at external-model integration time per M34 §3.6.B
        env["ANTHROPIC_BASE_URL"] = str(base_url)
        if auth_token is not None:
            env["ANTHROPIC_AUTH_TOKEN"] = str(auth_token)
    else:
        # Anthropic mode. isolated_env already inherits this var from os.environ,
        # so the explicit set is belt-and-suspenders: it makes the token path
        # assertable in a test and survives isolated_env ever moving to an
        # allowlist. Not touched in proxy mode — that path authenticates via the
        # ANTHROPIC_* pair above.
        token = _oauth_token_from_env()
        if token is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


# ---------------------------------------------------------------------------
# stream-json parsing (Anthropic shape)
# ---------------------------------------------------------------------------


def _iter_content_blocks(event: dict):
    """Yield content blocks from an ``assistant`` stream event, shape-tolerant."""
    msg = event.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _tool_result_content(block: dict) -> str:
    """Flatten a ``tool_result`` block's ``content`` into plain text.

    The content is either a string or a list of ``{type: text, text: ...}``
    blocks (Anthropic shape); both are normalized to a joined string.
    """
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for sub in content:
            if isinstance(sub, dict):
                parts.append(str(sub.get("text", "")))
            else:
                parts.append(str(sub))
        return "\n".join(parts)
    return "" if content is None else str(content)


def parse_stream(lines: list[str]) -> ExecutorResult:
    """Parse a ``stream-json`` event list into an :class:`ExecutorResult`."""
    result = ExecutorResult()
    # Bash tool_use ids seen so far, so a later tool_result can be tagged with
    # the originating tool name even though the result block names only the id.
    bash_ids: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        result.raw_events.append(event)
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            # Additive: the served model. Nothing the historical baseline is
            # computed from is touched by reading it.
            served = event.get("model")
            if served:
                result.model_served = str(served)

        elif etype == "assistant":
            for block in _iter_content_blocks(event):
                if block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", ""))
                result.tool_names.append(name)
                block_id = block.get("id")
                inp = block.get("input") or {}
                if name == "Skill":
                    # Skill tool_use input names the skill (key varies by version).
                    label = (
                        inp.get("skill")
                        or inp.get("command")
                        or inp.get("name")
                        or ""
                    )
                    if label:
                        result.skills.append(str(label))
                elif name == "Bash":
                    if block_id:
                        bash_ids.add(str(block_id))
                    cmd = str(inp.get("command", ""))
                    for argv in _lit_calls_from_bash(cmd):
                        result.lit_calls.append(
                            LitCall(argv=argv, raw=cmd, tool_use_id=block_id)
                        )

        elif etype == "user":
            # tool_result blocks live on the user turn that follows a tool_use.
            for block in _iter_content_blocks(event):
                if block.get("type") != "tool_result":
                    continue
                tuid = block.get("tool_use_id")
                tool = "Bash" if tuid and str(tuid) in bash_ids else ""
                result.tool_results.append(
                    ToolResult(
                        tool=tool,
                        content=_tool_result_content(block),
                        tool_use_id=str(tuid) if tuid else None,
                    )
                )

        elif etype == "result":
            result.final_text = str(event.get("result", ""))
            result.usage = _parse_usage(event)

    return result


def _parse_usage(event: dict) -> dict:
    """Extract the token accounting from a stream-json ``result`` event.

    Claude Code's final ``result`` event carries a ``usage`` block (the four
    Anthropic token counters) plus a top-level ``num_turns``. We keep the four
    counters always (defaulting absent ones to 0 so downstream math never trips
    on a missing key) and fold in turns when present. We deliberately do NOT
    capture ``total_cost_usd``: against an external proxy (a non-Anthropic model
    routed through a compat endpoint) that figure is computed from the wrong
    price table and does not reflect the provider's real charge — recording it
    would mislead. Cost is derived downstream from the raw counters x the
    provider's own per-token prices. Returns ``{}`` when the event has no usage
    block at all (nothing to account)."""
    u = event.get("usage") or {}
    if not u:
        return {}
    out = {
        "input_tokens": int(u.get("input_tokens", 0) or 0),
        "output_tokens": int(u.get("output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
        "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
    }
    turns = event.get("num_turns")
    if turns is not None:
        out["num_turns"] = int(turns)
    return out


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ClaudeAdapter:
    """Drives ``claude -p`` (see the module docstring: behavior is frozen)."""

    name = "claude"
    default_model = DEFAULT_MODEL
    permission_flags = PERMISSION_FLAGS
    # The only agent with an Anthropic-compatible proxy mode: the CLI honors
    # ANTHROPIC_BASE_URL, which is what --base-url / --auth-token export.
    supports_anthropic_proxy = True
    evidence_source = "the event stream"
    capabilities = AgentCapabilities(
        tokens=True,        # four counters on the result event
        turns=True,         # num_turns on the result event
        served_model=True,  # system/init reports the model id
        routing=True,       # Skill tool_use is the activation signal
    )

    @property
    def bin(self) -> str:
        # Late-bound on purpose: binding the module constant at class-creation
        # time would make any post-import override of it a silent no-op.
        return CLAUDE_BIN

    def skills_dir(self, base: Path) -> Path:
        """Claude Code auto-discovers ``<CLAUDE_CONFIG_DIR>/skills/<name>/SKILL.md``."""
        return base / "claude-config" / "skills"

    def prepare(
        self,
        base: Path,
        *,
        run_vault: Path,
        base_url: str | None = None,
        auth_token: str | None = None,
    ) -> dict[str, str]:
        registry_dir = base / "claude-registry"
        registry_dir.mkdir(parents=True, exist_ok=True)
        config_dir = base / "claude-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        # A brand-new empty HOME per run, same shape as cursor's and agy's. It is
        # seeded with NOTHING: claude's credential goes to CLAUDE_CONFIG_DIR (via
        # seed_auth just below), not here, so an empty dir is the whole story.
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        # External mode authenticates via the proxy (ANTHROPIC_BASE_URL + token);
        # only the default Anthropic mode needs the OAuth credentials copied in.
        # seed_auth reads the REAL home (harness process) and writes into the
        # isolated config dir — the redirect above lives in the child's env only.
        #
        # And only when no static token is set: a CLAUDE_CODE_OAUTH_TOKEN in the
        # harness env means the operator opted into the non-rotating credential,
        # which executor_env injects — copying a rotating snapshot too would just
        # re-introduce the refresh-token rotation race the token exists to avoid.
        # The two paths are mutually exclusive. Proxy mode (base_url set) copies
        # neither and is unchanged.
        if base_url is None and _oauth_token_from_env() is None:
            seed_auth(config_dir)
        install_repo_skills(self.skills_dir(base))
        return executor_env(
            run_vault,
            registry_dir,
            config_dir,
            home,
            base_url=base_url,
            auth_token=auth_token,
        )

    def build_argv(self, prompt: str, *, model: str) -> list[str]:
        return [
            self.bin,
            "-p",
            prompt,
            "--model", model,
            "--output-format", "stream-json",
            "--verbose",
            *PERMISSION_FLAGS,
        ]

    def parse(self, stdout: str, *, base: Path) -> ExecutorResult:
        return parse_stream(stdout.splitlines())
