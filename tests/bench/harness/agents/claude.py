"""Claude Code adapter — the incumbent path, held byte-identical on purpose.

Everything in this module is the pre-multi-agent executor's claude code, moved
verbatim from ``harness.executor``. The argv, the child env, the isolation seam
(``CLAUDE_CONFIG_DIR`` + a copied ``.credentials.json``) and the stream-json
parsing are unchanged, because a live TRR/RA baseline already exists for this
path: change how claude's evidence is gathered and every historical number
silently stops being comparable to the new ones. New capability goes to the other
adapters; this one only got a ``model_served`` read (purely additive — it touches
no field the baseline was computed from).

Isolation: ``CLAUDE_CONFIG_DIR`` re-homes both the skills and the config, so
:func:`seed_auth` copies the OAuth credential back in — and ONLY the credential,
never ``settings.json``, so the executor's settings stay clean and reproducible.
The permission mode is set on the CLI, not in a settings file.

Evidence: the ``--output-format stream-json --verbose`` event stream. ``Skill``
tool_use blocks give the routing label; ``Bash`` commands are split on shell
separators to recover ``lit`` argv; the paired ``tool_result`` blocks carry that
argv's stdout.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from pathlib import Path

from harness.agents import AgentCapabilities
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

    Two auth modes. When ``base_url`` is ``None`` (default Anthropic mode) the env
    is byte-identical to before — OAuth via the copied ``.credentials.json``. When
    ``base_url`` is set (external mode: ``claude`` CLI pointed at an
    Anthropic-compatible proxy such as LiteLLM / claude-code-router) we also export
    ``ANTHROPIC_BASE_URL`` + the auth token so the proxy authenticates; OAuth is
    skipped in this mode (see :meth:`ClaudeAdapter.prepare`).
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
        # External mode authenticates via the proxy (ANTHROPIC_BASE_URL + token);
        # only the default Anthropic mode needs the OAuth credentials copied in.
        if base_url is None:
            seed_auth(config_dir)
        install_repo_skills(self.skills_dir(base))
        return executor_env(
            run_vault,
            registry_dir,
            config_dir,
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
