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
    """One ``lit`` invocation the agent issued via its Bash tool."""

    argv: list[str]
    raw: str


@dataclass
class ExecutorResult:
    """Everything the executor observed about one agent run.

    ``skills`` = routing labels (every ``Skill`` tool_use name, in order).
    ``lit_calls`` = the agent's ``lit`` Bash commands (the checker's argv log).
    ``tool_names`` = every tool_use name seen (for design-time observation).
    ``final_text`` = the agent's final result message. ``exit_code`` is the
    ``claude`` process exit; ``timed_out`` flags a killed run.
    """

    skills: list[str] = field(default_factory=list)
    lit_calls: list[LitCall] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    final_text: str = ""
    exit_code: int = 0
    timed_out: bool = False
    raw_events: list[dict] = field(default_factory=list)

    def as_jsonl_records(self) -> list[dict]:
        """Project the agent's lit calls into the checker's jsonl record shape.

        The checker (``ran``/``not_ran``) only reads ``record["argv"]`` and
        substring-matches the joined argv, so a record per lit call with its
        parsed argv is sufficient; we have no per-call exit from tool_use alone.
        """
        return [{"argv": c.argv, "raw": c.raw} for c in self.lit_calls]


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


def executor_env(run_vault: Path, registry_dir: Path, config_dir: Path) -> dict[str, str]:
    """Child env for the ``claude`` executor process.

    Starts from ``os.environ`` (PATH / conda / the user's API auth survive — the
    executor must authenticate as a normal claude session), then:

    * ``LIT_LIBRARY=<run_vault>`` — the agent's bare ``lit`` targets the /tmp copy;
    * ``LITMAN_REGISTRY_DIR=<registry_dir>`` — no real registry;
    * ``CLAUDE_CONFIG_DIR=<config_dir>`` — isolated skills + claude config.
    """
    env = os.environ.copy()
    env["LIT_LIBRARY"] = str(run_vault)
    env["LITMAN_REGISTRY_DIR"] = str(registry_dir)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
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


def parse_stream(lines: list[str]) -> ExecutorResult:
    """Parse a ``stream-json`` event list into an :class:`ExecutorResult`."""
    result = ExecutorResult()
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
                    cmd = str(inp.get("command", ""))
                    for argv in _lit_calls_from_bash(cmd):
                        result.lit_calls.append(LitCall(argv=argv, raw=cmd))

        elif etype == "result":
            result.final_text = str(event.get("result", ""))

    return result


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


def run_card(
    card: Card,
    run_vault: Path,
    *,
    fixtures_pdfs_dir: Path,
    model: str = DEFAULT_MODEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    work_root: Path | None = None,
) -> ExecutorResult:
    """Run one card end-to-end against a live ``claude -p`` executor.

    Caller owns ``run_vault`` lifecycle (a cp of the right seed). This function
    sets up the isolated config + skills + a neutral cwd, stages the card's
    fixture PDFs into an agent-readable handoff dir, spawns ``claude``, and parses
    the stream. It does NOT score — that is the checker's job on the returned
    ``run_vault`` + ``result.as_jsonl_records()``.
    """
    run_vault = Path(run_vault)
    base = run_vault.parent  # the per-run /tmp dir created by RunVault
    work_root = Path(work_root) if work_root else base

    registry_dir = base / "claude-registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    config_dir = base / "claude-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    seed_auth(config_dir)
    install_repo_skills(config_dir)

    # Neutral cwd OUTSIDE litman_dev (naive-user persona; M34 §0). A fresh empty
    # dir under the run root: the agent has no repo context to lean on.
    neutral_cwd = base / "cwd"
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
    env = executor_env(run_vault, registry_dir, config_dir)

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
