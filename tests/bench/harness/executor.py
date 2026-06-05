"""Phase E — live-agent executor driver.

This is the part that actually *runs the benchmark*: it spawns a separate,
headless ``claude -p`` process (the "executor"), hands it ONLY a card's intent +
fixture paths (scenarios §0 handoff discipline, enforced by
:func:`harness.scenarios.executor_view`), and lets the agent decide which ``lit``
commands to run. Nothing here tells the agent which command to call — that
decision is exactly what litman-bench measures (ADR-007 agent-as-primary-consumer).

Isolation (M34 §4 red line), all welded here, never left to the agent:

* ``LIT_LIBRARY`` is pointed at the disposable **run vault** (not unset, unlike
  the deterministic seed/check path): a naive user has their vault configured
  via env, so the agent's bare ``lit add`` must land somewhere — we make that
  "somewhere" the throwaway /tmp copy. The real vault is thereby shadowed and
  unreachable.
* ``LITMAN_REGISTRY_DIR`` is redirected into the run dir (no real registry).
* ``CLAUDE_CONFIG_DIR`` is redirected into the run dir, and the **repo-source**
  litman skills are installed there via ``lit install-skill --parent-dir`` so
  the skill under test == the repo source (M34 Phase E), never the user's
  already-installed ``~/.claude`` copy.

Routing + execution evidence is parsed from the ``--output-format stream-json
--verbose`` event stream (plain ``json`` carries no per-tool events): every
``tool_use`` block is inspected — ``Skill`` invocations give the routing label,
``Bash`` commands that invoke ``lit`` give the argv log the checker scores
``ran``/``not_ran`` against (the agent runs ``lit`` via its own Bash tool, so
those calls never pass through :class:`harness.runlit.RunVault.run`).

The matching ``tool_result`` blocks (Anthropic stream-json carries these in a
``type=="user"`` event, ``message.content[*]`` with ``type=="tool_result"``)
carry the *stdout* of those Bash calls. We capture them so the checker's
``stdout_contains`` evidence verb can grep a lit command's output (e.g. "the
list output contains #4") even though the agent's Bash calls never touched
:class:`harness.runlit.RunVault`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from harness.scenarios import Card, executor_view
from harness.seeds import LIT_BIN

# The `claude` CLI (headless). Overridable for a different install location.
CLAUDE_BIN = os.environ.get("LITMAN_BENCH_CLAUDE_BIN", "claude")

# v0 default tier (user ruling 2026-06-02): Sonnet — capable enough that a
# working harness resolves cards, so a failure points at litman not the model;
# cheaper than Opus for the repeat-heavy noise pass. Weak-tier sweeps come later.
DEFAULT_MODEL = "claude-sonnet-4-6"

# A generous ceiling: one card may chain several lit calls + PDF reads.
DEFAULT_TIMEOUT_S = 600


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class LitCall:
    """One ``lit`` invocation the agent issued via its Bash tool.

    ``tool_use_id`` (when present) is the stream-json ``tool_use`` block id; it
    pairs the call with the ``tool_result`` event that carries its stdout.
    """

    argv: list[str]
    raw: str
    tool_use_id: str | None = None


@dataclass
class ToolResult:
    """One ``tool_result`` block: the captured output of a prior tool call.

    ``tool`` is the originating tool name when known (else ``""``), ``content``
    is the flattened text of the result (lit stdout, for Bash calls).
    """

    tool: str
    content: str
    tool_use_id: str | None = None


@dataclass
class ExecutorResult:
    """Everything the executor observed about one agent run.

    ``skills`` = routing labels (every ``Skill`` tool_use name, in order).
    ``lit_calls`` = the agent's ``lit`` Bash commands (the checker's argv log).
    ``tool_results`` = captured ``tool_result`` blocks (lit stdout lives here).
    ``tool_names`` = every tool_use name seen (for design-time observation).
    ``final_text`` = the agent's final result message. ``exit_code`` is the
    ``claude`` process exit; ``timed_out`` flags a killed run. ``usage`` =
    the token accounting parsed from the stream-json ``result`` event
    (input / output / cache_creation / cache_read tokens + optional
    num_turns; dollar cost is deliberately not captured, see _parse_usage);
    ``{}`` when the run produced no result event (e.g. a hard API error that
    aborted before any usage was reported).
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

    def as_jsonl_records(self) -> list[dict]:
        """Project the agent's lit calls into the checker's jsonl record shape.

        Each record carries the parsed ``argv`` (the ``ran``/``not_ran`` evidence)
        plus the best-effort ``stdout`` of that call, paired from the matching
        ``tool_result`` block. Pairing is by ``tool_use_id`` when both sides carry
        one; otherwise an empty string (documented best-effort — a lit call whose
        result block we cannot map carries no stdout, never a wrong one).
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
    """Join every captured ``tool_result`` content into one searchable blob.

    Used by the executor-stdout evidence path: ``stdout_contains`` greps this
    when scoring "the agent's lit output mentions X" without needing to map a
    specific call to its result.
    """
    return "\n".join(tr.content for tr in result.tool_results)


# ---------------------------------------------------------------------------
# Isolated config + skill install
# ---------------------------------------------------------------------------


def _real_config_dir() -> Path:
    """The user's real Claude Code config dir (where auth credentials live)."""
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


def install_repo_skills(config_dir: Path, *, only: str | None = None) -> None:
    """Install the repo-source litman skills into ``<config_dir>/skills/``.

    Uses ``lit install-skill --parent-dir`` so the skill text == the repo source
    under test, not whatever is already in the user's ``~/.claude``. ``config_dir``
    is what we hand ``claude`` as ``CLAUDE_CONFIG_DIR``; Claude Code auto-discovers
    skills under ``<CLAUDE_CONFIG_DIR>/skills/<name>/SKILL.md``.
    """
    skills_parent = config_dir / "skills"
    skills_parent.mkdir(parents=True, exist_ok=True)
    args = ["install-skill", "--parent-dir", str(skills_parent), "--force"]
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


def executor_env(
    run_vault: Path,
    registry_dir: Path,
    config_dir: Path,
    *,
    base_url: str | None = None,
    auth_token: str | None = None,
) -> dict[str, str]:
    """Child env for the ``claude`` executor process.

    Starts from ``os.environ`` (PATH / conda / the user's API auth survive — the
    executor must authenticate as a normal claude session), then:

    * ``LIT_LIBRARY=<run_vault>`` — the agent's bare ``lit`` targets the /tmp copy;
    * ``LITMAN_REGISTRY_DIR=<registry_dir>`` — no real registry;
    * ``CLAUDE_CONFIG_DIR=<config_dir>`` — isolated skills + claude config.

    Two auth modes (M34 §3.6.B). When ``base_url`` is ``None`` (default Anthropic
    mode) the env is byte-identical to before — OAuth via the copied
    ``.credentials.json``. When ``base_url`` is set (external mode: ``claude`` CLI
    pointed at an Anthropic-compatible proxy such as LiteLLM / claude-code-router)
    we also export ``ANTHROPIC_BASE_URL`` + the auth token so the proxy
    authenticates; OAuth is skipped in this mode (see :func:`run_card`).
    """
    env = os.environ.copy()
    env["LIT_LIBRARY"] = str(run_vault)
    env["LITMAN_REGISTRY_DIR"] = str(registry_dir)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if base_url is not None:
        # env var names confirmed at external-model integration time per M34 §3.6.B
        env["ANTHROPIC_BASE_URL"] = str(base_url)
        if auth_token is not None:
            env["ANTHROPIC_AUTH_TOKEN"] = str(auth_token)
    return env


# ---------------------------------------------------------------------------
# stream-json parsing
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


# Shell statement separators we split a compound command on before looking for
# ``lit`` segments (so ``lit add ... && rm -f tmp`` does not swallow ``rm`` into
# the lit argv, and ``echo x && lit list`` is found correctly).
_CMD_SEP = re.compile(r"&&|\|\||[;|\n]")
# A leading ``VAR=value`` env assignment to skip before the command word.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")


def _lit_calls_from_bash(command: str) -> list[list[str]]:
    """Extract every ``lit`` invocation's argv from a Bash command string.

    Splits the command on shell separators (``&&`` / ``||`` / ``;`` / ``|`` /
    newline), then for each segment skips any leading ``VAR=val`` assignments and,
    if the command word is ``lit`` (or a path ending ``/lit``), captures the rest
    as argv. A single Bash command may issue several ``lit`` calls, so this
    returns a list. Best-effort — used for substring ``ran:`` evidence, not exact
    replay (``$VAR`` stays literal, redirects survive as tokens).
    """
    calls: list[list[str]] = []
    for segment in _CMD_SEP.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        i = 0
        while i < len(tokens) and _ENV_ASSIGN.match(tokens[i]):
            i += 1
        if i < len(tokens) and (tokens[i] == "lit" or tokens[i].endswith("/lit")):
            calls.append(tokens[i + 1 :])
    return calls


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

        if etype == "assistant":
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
    """
    return Path(run_vault).parent / "cwd"


def run_card(
    card: Card,
    run_vault: Path,
    *,
    fixtures_pdfs_dir: Path,
    model: str = DEFAULT_MODEL,
    base_url: str | None = None,
    auth_token: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> ExecutorResult:
    """Run one card end-to-end against a live ``claude -p`` executor.

    Caller owns ``run_vault`` lifecycle (a cp of the right seed). This function
    sets up the isolated config + skills + a neutral cwd, stages the card's
    fixture PDFs into an agent-readable handoff dir, spawns ``claude``, and parses
    the stream. It does NOT score — that is the checker's job on the returned
    ``run_vault`` + ``result.as_jsonl_records()``.

    ``base_url`` / ``auth_token`` select the auth mode (M34 §3.6.B): ``None``
    (default) is Anthropic OAuth (``seed_auth`` copies the credentials); a set
    ``base_url`` is external mode (proxy), which **skips** ``seed_auth`` and
    instead exports the proxy env via :func:`executor_env`.
    """
    run_vault = Path(run_vault)
    base = run_vault.parent  # the per-run /tmp dir created by RunVault

    registry_dir = base / "claude-registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    config_dir = base / "claude-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    # External mode authenticates via the proxy (ANTHROPIC_BASE_URL + token);
    # only the default Anthropic mode needs the OAuth credentials copied in.
    if base_url is None:
        seed_auth(config_dir)
    install_repo_skills(config_dir)

    # Neutral cwd OUTSIDE litman_dev (naive-user persona; M34 §0). A fresh empty
    # dir under the run root: the agent has no repo context to lean on.
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
    env = executor_env(
        run_vault, registry_dir, config_dir, base_url=base_url, auth_token=auth_token
    )

    argv = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
    ]

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

    result = parse_stream(stdout.splitlines())
    result.exit_code = exit_code
    result.timed_out = timed_out
    return result


def observe_skill_for_utterance(
    utterance: str,
    run_vault: Path,
    *,
    fixtures_pdfs_dir: Path,
    model: str = DEFAULT_MODEL,
    base_url: str | None = None,
    auth_token: str | None = None,
    usage_sink: list[dict] | None = None,
) -> str | None:
    """Route ONE utterance through the executor and return the skill it fired.

    A routing card (scenarios §I) is a bag of utterances, each scored by which
    ``Skill`` the agent invokes — NOT by an execution end-state. This wraps
    :func:`run_card` with a synthetic single-utterance :class:`Card` (no
    fixtures: routing is pure classification) and returns the FIRST observed
    skill label (``ExecutorResult.skills[0]``) or ``None`` when the agent fired
    no skill. The ``None`` case is a routing MISS for a skill-equipped agent (the
    skill exists and should have triggered), scored upstream by
    :func:`harness.routing.score_routing`.

    When ``usage_sink`` is provided, this probe's token ``usage`` is appended to
    it (one dict per spawn) so the routing axis's ~14 classification spawns are
    counted in the run's grand-total cost, not silently dropped. The routing
    label return value is unchanged, so :func:`harness.routing.score_routing`
    is untouched.

    This is the SOLE executor touchpoint for the routing axis (M34 §3.6.A) — like
    :func:`run_card`, it spawns ``claude -p``, so it is exercised ONLY under live
    authorization (Phase G), never inside /dev.
    """
    card = Card(id="routing-probe", intent=str(utterance), fixtures=[])
    result = run_card(
        card,
        run_vault,
        fixtures_pdfs_dir=fixtures_pdfs_dir,
        model=model,
        base_url=base_url,
        auth_token=auth_token,
    )
    if usage_sink is not None and result.usage:
        usage_sink.append(result.usage)
    return result.skills[0] if result.skills else None
