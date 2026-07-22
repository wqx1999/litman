"""OpenAI Codex (``codex``) adapter — the best-instrumented scaffold after claude.

``codex exec --json`` emits a clean per-line JSON event stream, and three of the
four axes are read straight off it — no PATH shim (agy), no camelCase remap that
hides a silent zero (cursor). The fourth, the served model, is the ONE thing the
stream never carries; it is recovered from the session rollout file codex writes
under its own ``CODEX_HOME`` — the exact analog of opencode's ``export`` second
source. Each place below was found by recording a real ``codex exec`` run in an
isolated ``CODEX_HOME`` + ``HOME`` on 2026-07-18 (codex-cli 0.144.6), not by
reading docs:

* **lit argv** — an ``item.completed`` whose ``item.type == "command_execution"``
  carries ``command`` as ONE shell string, but codex always wraps it as
  ``/usr/bin/bash -lc "<inner>"``. The bare ``<inner>`` is what
  :func:`~harness.agents._shell._lit_calls_from_bash` (claude's compound splitter)
  expects, so :func:`_inner_bash_command` unwraps the ``bash -lc`` shell first —
  otherwise the command word is ``/usr/bin/bash`` and nothing is captured. One
  command can carry several ``lit`` segments (``lit a && lit b``), all sharing the
  one ``item.id`` and its one ``aggregated_output`` — same pairing claude/opencode
  use.
* **skill activation** — codex has no ``Skill`` tool; it activates a skill by
  READING its ``SKILL.md`` via a shell command (like cursor, but the read is a
  ``command_execution`` not a ``readToolCall``). The routing signal is therefore a
  path inside the ``bash -lc "...SKILL.md"`` string. Two things change the regex vs
  cursor's: the path is followed by a closing quote, NOT end-of-string (so cursor's
  ``$`` anchor would never match), and codex's own builtin curated-plugin skills
  also sit under a ``/skills/`` path — so the regex is anchored to the run's own
  install root ``/.agents/skills/`` instead, which excludes the plugins.
* **tokens** — every ``turn.completed`` carries a ``usage`` block
  (``input_tokens`` / ``cached_input_tokens`` / ``output_tokens`` /
  ``reasoning_output_tokens``). :func:`normalize_usage` sums the blocks (a run may
  have >1 turn) and maps them onto the internal snake_case counters. OpenAI's
  ``input_tokens`` is the TOTAL prompt including cache, so the cached part is
  subtracted out to mirror claude's non-cached ``input_tokens`` convention (cache
  reads are not double-counted). ``reasoning_output_tokens`` is kept under its own
  key, neither lost nor summed into output.
* **served model** — NOT in the stream (grepped clean): codex writes a rollout at
  ``CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`` whose records carry
  the model it dispatched. :func:`_served_model` reads the NEWEST rollout under the
  run's isolated ``CODEX_HOME`` and returns the first ``model`` value. This is why
  the adapter is ``served_model=True`` (unlike agy): the model IS knowable, so
  Phase 0's pin-check applies. Unrecoverable (no rollout / no ``model`` field)
  reports ``None`` — NEVER the requested model, which is exactly the mismatch
  Phase 0 exists to catch.

``turns`` is False: codex emits ``turn.started`` / ``turn.completed`` events but
not an agentic-turn count we trust, so ``num_turns`` is an honest ``None`` rather
than an event tally dressed up as one.

Isolation: ``CODEX_HOME`` + ``HOME``. Codex reads auth + config from
``CODEX_HOME`` (falling back to ``~/.codex``), which it resolves BEFORE ``$HOME`` —
so a set one walks past the redirected home to the real login, and it MUST be
dropped in :data:`~harness.agents.HOME_ESCAPING_CONFIG_VARS` and re-set to the
run's own dir, exactly how claude handles ``CLAUDE_CONFIG_DIR``. Seeding just
``auth.json`` into a fresh ``CODEX_HOME`` is enough to run (measured, exit 0);
``HOME`` carries the skills (``$HOME/.agents/skills/``, the open standard, measured
to activate). Codex has no free tier, so :func:`seed_auth` RAISES on a missing
credential (unlike opencode's silent skip) — a legible message beats a mysterious
mid-run auth error. A harmless warning under a ``/tmp`` ``CODEX_HOME`` ("Refusing
to create helper binaries under temporary dir") is emitted and codex PROCEEDS
(exit 0); it is not a failure.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from pathlib import Path

from harness.agents import AgentCapabilities, isolated_env
from harness.agents._shell import _lit_calls_from_bash
from harness.executor import (
    ExecutorResult,
    LitCall,
    ToolResult,
    install_repo_skills,
)

CODEX_BIN = os.environ.get("LITMAN_BENCH_CODEX_BIN", "codex")

# Codex's subscription default (from the `codex exec` header + rollout). ALWAYS
# passed explicitly on a real run; this only keeps a smoke invocation working.
DEFAULT_MODEL = "gpt-5.6-sol"

# Codex's full permission+sandbox bypass. Authorized for the bench harness ONLY
# (disposable /tmp vault, real library shadowed); holds the permission variable
# constant with cursor's --force / agy's --dangerously-skip-permissions. Recorded
# in the report verbatim. The product red line (nothing under src/litman/) is
# untouched. NOTE: Claude Code's Bash classifier blocks a human from typing this
# flag, but the harness spawns codex via run_bounded->Popen, which is not
# classified — so the bench uses it fine.
PERMISSION_FLAGS = ("--dangerously-bypass-approvals-and-sandbox",)

#: Codex's credential, relative to a HOME. The real ``~/.codex`` may be a symlink,
#: but ``~/.codex/auth.json`` is the file that carries the OAuth ``tokens`` block.
#: Resolved against ``Path.home()`` at seed time (see :func:`seed_auth`), NOT bound
#: at import: a module constant would read the maintainer's real home even when a
#: test redirects ``$HOME``, copying a live credential into ``tmp_path`` with the
#: test still green — the "leak that passes". Mirrors opencode/agy/cursor.
AUTH_RELPATH = Path(".codex") / "auth.json"

# Anchored to the run's OWN skill root (.agents/skills/), which does two jobs at
# once: (1) no trailing `$` — the path lives inside a `bash -lc "...SKILL.md"`
# string, so it is followed by a closing quote, not end-of-string, and cursor's
# `$`-anchored regex would never match; (2) scoping to `.agents/skills/` excludes
# codex's own builtin curated-plugin skills (under CODEX_HOME/plugins/.../skills/),
# so reading one of those is not mislabeled as a litman routing hit.
_SKILL_PATH_RE = re.compile(r"/\.agents/skills/([^/]+)/SKILL\.md")

# The codex usage-block keys we recognize. Used as the "is this the shape we know?"
# guard, exactly like cursor's/opencode's: a block carrying NONE of these is a
# renamed schema, and reporting it as {} (not observed) beats defaulting every
# counter to a truthy, fictional 0.
_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def normalize_usage(usage_blocks: list[dict]) -> dict:
    """Sum codex's per-turn ``turn.completed.usage`` blocks onto internal counters.

    ``[]`` in (no ``turn.completed`` carried usage) -> ``{}`` out: nothing to
    account, which downstream reads as "not observed", never "zero tokens spent".

    A list whose blocks carry none of :data:`_USAGE_KEYS` is ALSO ``{}`` out — the
    same reason cursor's/opencode's ``normalize_usage`` exists. If codex renames its
    counters, defaulting each to 0 would produce a TRUTHY all-zero dict that sails
    past every "did we observe usage?" check and is summed into the report as a
    spawn that provably cost nothing. A missing number is recoverable; a wrong one
    is not.

    Mapping (per block, summed):

    * ``output_tokens``          -> ``output_tokens``
    * ``cached_input_tokens``    -> ``cache_read_input_tokens``
    * ``input_tokens`` MINUS ``cached_input_tokens`` -> ``input_tokens``. OpenAI's
      ``input_tokens`` is the TOTAL prompt INCLUDING the cached part, whereas the
      internal ``input_tokens`` convention (claude's) is the NON-cached part, so the
      cache reads are not double-counted in the grand total. Clamped at 0 per block
      in case one ever reports cached > input. **§6 open item: confirm this
      inclusivity at smoke; if it turns out exclusive, drop the subtraction.**
    * ``reasoning_output_tokens`` -> ``reasoning_tokens`` (opencode-only key
      convention; ``harness.batch._sum_usage`` ignores it, the per-spawn record
      keeps it for the reader).

    No ``cache_creation_input_tokens`` key: codex reports no cache-WRITE counter at
    all (its block is input/cached_input/output/reasoning), so emitting a 0 for it
    would be the same fictional-zero this function exists to avoid. ``_sum_usage``
    zero-fills it at the grand total, where 0 is the summer's own default.

    ``num_turns`` is an explicit ``None``: codex emits turn events but not an
    agentic-turn count we trust, and a visible ``null`` says "no turn count" where a
    missing key looks like a parser that forgot.
    """
    blocks = [u for u in usage_blocks if isinstance(u, dict)]
    if not blocks:
        return {}
    if not any(k in u for u in blocks for k in _USAGE_KEYS):
        return {}
    out = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    for u in blocks:
        input_total = int(u.get("input_tokens", 0) or 0)
        cached = int(u.get("cached_input_tokens", 0) or 0)
        out["input_tokens"] += max(0, input_total - cached)
        out["cache_read_input_tokens"] += cached
        out["output_tokens"] += int(u.get("output_tokens", 0) or 0)
        out["reasoning_tokens"] += int(u.get("reasoning_output_tokens", 0) or 0)
    out["num_turns"] = None
    return out


def _inner_bash_command(command: str) -> str:
    """Unwrap codex's ``/usr/bin/bash -lc "<inner>"`` to the bare ``<inner>``.

    ``_lit_calls_from_bash`` expects a bare command string; handed the raw wrapper
    its command word is ``/usr/bin/bash`` and nothing is captured. So split the
    wrapper, find ``-lc`` (or ``-c``), and return the following token. Best-effort:
    on a shlex parse failure, or no ``-lc``/``-c`` (a command codex did not wrap),
    the string is returned unchanged rather than raising.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    for i, tok in enumerate(tokens):
        if tok in ("-lc", "-c"):
            if i + 1 < len(tokens):
                return tokens[i + 1]
            return command
    return command


def parse_stream(lines: list[str]) -> ExecutorResult:
    """Parse a ``codex exec --json`` event list into an :class:`ExecutorResult`.

    The served model is NOT in the stream — the adapter fills it from the rollout
    file (:func:`_served_model`) after this returns.
    """
    result = ExecutorResult()
    usage_blocks: list[dict] = []

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

        if etype == "item.completed":
            # Only item.completed — each command emits an item.started twin with
            # empty output and exit_code null; folding both would double every call.
            item = event.get("item") or {}
            itype = item.get("type")
            if itype == "command_execution":
                cmd = str(item.get("command", ""))
                inner = _inner_bash_command(cmd)
                item_id = item.get("id")
                item_id = str(item_id) if item_id else None
                result.tool_names.append("shell")

                # Routing: search the FULL cmd (the bash -lc "..." wrapper), NOT
                # inner — so detection does not depend on _inner_bash_command having
                # unwrapped cleanly. The regex is scoped to /.agents/skills/, so a
                # read of one of codex's builtin plugin skills (under
                # CODEX_HOME/plugins/.../skills/) is correctly NOT counted.
                m = _SKILL_PATH_RE.search(cmd)
                if m:
                    result.skills.append(m.group(1))

                for argv in _lit_calls_from_bash(inner):
                    # Every lit segment of a compound command shares the ONE item id
                    # (and thus the one aggregated_output below) — same pairing
                    # claude/opencode use for `lit a && lit b`.
                    result.lit_calls.append(
                        LitCall(argv=argv, raw=inner, tool_use_id=item_id)
                    )

                aggregated = item.get("aggregated_output")
                if aggregated is not None:
                    result.tool_results.append(
                        ToolResult(
                            tool="shell",
                            content=str(aggregated),
                            tool_use_id=item_id,
                        )
                    )
            elif itype == "agent_message":
                # Assistant prose; the LAST agent_message is the final answer.
                result.final_text = item.get("text", "")

        elif etype == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                usage_blocks.append(usage)

    result.usage = normalize_usage(usage_blocks)
    return result


def _first_model(obj) -> str | None:
    """First non-empty string value under a ``"model"`` key, at any depth.

    Codex records the served model inside a rollout record's payload (a
    ``turn_context`` record), not always at the top level, so this walks the record
    rather than indexing one fixed key. ``None`` when no ``model`` string is found.
    """
    if isinstance(obj, dict):
        model = obj.get("model")
        if isinstance(model, str) and model:
            return model
        for value in obj.values():
            found = _first_model(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _first_model(item)
            if found:
                return found
    return None


def _served_model(codex_home: Path) -> str | None:
    """The served model from the NEWEST rollout under ``codex_home``, or ``None``.

    Globs ``codex_home/sessions`` recursively for ``rollout-*.jsonl``, picks the
    newest by mtime, and returns the first ``model`` value in it, verbatim. This is
    codex's own record of the model it dispatched (the resolved default was
    ``gpt-5.6-sol`` when ``-m`` was omitted), the exact analog of opencode's
    ``export`` -> ``info.model.id``.

    ``None`` — NEVER the requested model — on: no rollout, an unreadable rollout, or
    no ``model`` field. Trusting the request here is precisely the mismatch Phase 0
    pins.
    """
    sessions = codex_home / "sessions"
    rollouts = list(sessions.rglob("rollout-*.jsonl"))
    if not rollouts:
        return None
    newest = max(rollouts, key=lambda p: p.stat().st_mtime)
    try:
        text = newest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        model = _first_model(record)
        if model:
            return model
    return None


def seed_auth(codex_home: Path) -> None:
    """Copy codex's ``auth.json`` into the fresh ``CODEX_HOME``.

    A DELIBERATE difference from opencode's ``seed_auth``, which skips when the
    credential is absent: codex has NO free tier, so a missing credential is a
    misconfiguration, not a valid loginless run — and a legible message here beats a
    mysterious mid-run auth error. Same shape as agy's raise (name the file, say
    ``codex login`` is the whole setup, note the harness never logs in for you).

    The source is resolved against ``Path.home()`` at call time (NOT a module
    constant): a test redirects ``$HOME`` to a fake dir, and binding the real home
    at import would copy the maintainer's live credential into ``tmp_path`` with the
    test still passing. ``copy2`` preserves the credential's 0600 mode.
    """
    src = Path.home() / AUTH_RELPATH
    if not src.is_file():
        raise RuntimeError(
            f"codex is not logged in on this machine ({src} not found).\n"
            "  Run `codex login` once and complete its ChatGPT OAuth — that is "
            "the whole setup.\n"
            "  The harness never performs a login for you."
        )
    codex_home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, codex_home / "auth.json")


def _codex_home(base: Path) -> Path:
    """The run's isolated ``CODEX_HOME`` (``<base>/codexhome``).

    Used by BOTH :meth:`CodexAdapter.prepare` and :meth:`CodexAdapter.parse`, so
    ``parse`` derives the rollout dir from ``base`` with no stashed state (cleaner
    than opencode's ``_env`` stash, which only exists because its export needs the
    child env; codex's rollout is just a file under this dir).
    """
    return base / "codexhome"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CodexAdapter:
    """Drives ``codex exec --json`` with a redirected ``HOME`` + ``CODEX_HOME``."""

    name = "codex"
    default_model = DEFAULT_MODEL
    permission_flags = PERMISSION_FLAGS
    supports_anthropic_proxy = False  # first-party OpenAI OAuth; ignores ANTHROPIC_*
    evidence_source = (
        "the codex exec --json event stream (+ the session rollout file for the "
        "served model)"
    )
    capabilities = AgentCapabilities(
        tokens=True,         # turn.completed.usage, summed per turn
        turns=False,         # turn events exist, an agentic-turn count does not
        served_model=True,   # not in the stream, but the rollout file has it
        routing=True,        # a SKILL.md read (command_execution) is the activation
    )

    @property
    def bin(self) -> str:
        # Late-bound on purpose: binding the module constant at class-creation time
        # would make any post-import override of it a silent no-op.
        return CODEX_BIN

    def skills_dir(self, base: Path) -> Path:
        """``<home>/.agents/skills/`` — the open standard, measured to activate."""
        return base / "home" / ".agents" / "skills"

    def prepare(
        self,
        base: Path,
        *,
        run_vault: Path,
        base_url: str | None = None,
        auth_token: str | None = None,
    ) -> dict[str, str]:
        if base_url is not None:
            raise ValueError(
                "codex has no Anthropic-compatible proxy mode: --base-url / "
                "--auth-token need an agent whose supports_anthropic_proxy is True. "
                "(run_bench refuses this at the CLI boundary; reaching here means "
                "a caller bypassed it.)"
            )
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        codex_home = _codex_home(base)
        codex_home.mkdir(parents=True, exist_ok=True)
        seed_auth(codex_home)
        install_repo_skills(self.skills_dir(base))

        registry_dir = base / "codex-registry"
        registry_dir.mkdir(parents=True, exist_ok=True)

        env = isolated_env(home=home, run_vault=run_vault, registry_dir=registry_dir)
        # Re-set AFTER isolated_env drops CODEX_HOME from the denylist, exactly how
        # claude re-sets CLAUDE_CONFIG_DIR: the run's own isolated config dir, so
        # codex reads the seeded auth + the run's skills, never the real login.
        env["CODEX_HOME"] = str(codex_home)
        return env

    def build_argv(self, prompt: str, *, model: str, cwd: Path) -> list[str]:
        # `-C` pins the working root to the run's neutral cwd (the same role as
        # opencode's --dir / agy's --add-dir; `lit export` drops refs.bib there and
        # the checker's file_* verbs score it). --skip-git-repo-check: the run root
        # is /tmp, not a git repo, and codex refuses to start otherwise. The prompt
        # is the last positional arg. NOT --ephemeral: it suppresses the rollout
        # file, which is the only source for the served model.
        return [
            self.bin,
            "exec",
            "--json",
            *PERMISSION_FLAGS,
            "--skip-git-repo-check",
            "-C", str(cwd),
            "-m", model,
            prompt,
        ]

    def parse(self, stdout: str, *, base: Path) -> ExecutorResult:
        result = parse_stream(stdout.splitlines())
        result.model_served = _served_model(_codex_home(base))
        return result
