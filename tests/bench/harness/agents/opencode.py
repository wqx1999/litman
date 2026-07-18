"""opencode adapter — a model router with the fullest instrumentation but one.

``opencode run --format json`` emits a clean per-event JSON stream, and three of
the four axes are read straight off it — no PATH shim (agy), no camelCase remap
that hides a silent zero (cursor's is close, opencode's is nested instead). Each
place below was found by recording a real free-model run in an isolated HOME, not
by reading docs:

* **lit argv** — ``tool_use`` where ``part.tool == "bash"``,
  ``part.state.input.command`` is the RAW shell string (``lit hello && lit vault
  list``), same as claude and unlike cursor (which pre-tokenizes). So it reuses
  claude's compound splitter (:func:`~harness.agents._shell._lit_calls_from_bash`)
  and one bash call can yield several :class:`LitCall`, all sharing the bash
  ``callID`` and the one combined stdout (``part.state.metadata.output``).
* **skill activation** — opencode has a NATIVE ``skill`` tool (no read-the-file
  heuristic like cursor): ``tool_use`` where ``part.tool == "skill"`` carries the
  skill name at ``part.state.input.name``. That is the routing signal.
* **tokens** — every ``step_finish`` carries ``part.tokens`` (``input`` /
  ``output`` / ``reasoning`` / ``cache.{write,read}``), PER STEP not cumulative,
  so :func:`normalize_usage` sums the blocks and maps them onto the internal
  snake_case counters. ``reasoning`` is opencode-only; it is kept under its own
  key so it is neither lost nor summed into another counter — ``_sum_usage`` reads
  only the four it knows, so the extra key is harmless.
* **served model** — the ONE axis not in the stream (grepped clean): recovered by
  a second subprocess, ``opencode export <sessionID>`` -> ``info.model.id`` +
  ``info.model.providerID``, reported as ``"<providerID>/<id>"``. The sessionID is
  on every stream event. This is why the adapter is ``served_model=True`` (unlike
  agy): the model IS knowable, so Phase 0's pin-check applies. Unrecoverable
  (no sessionID / export non-zero / bad JSON) reports ``None`` — NEVER the
  requested model, which is exactly the mismatch Phase 0 exists to catch.

``turns`` is False: opencode has a "step" concept but a step is not an agentic
turn, so ``num_turns`` is an honest ``None`` rather than a step count dressed up
as one.

Isolation: ``HOME`` is redirected and the XDG data/cache/state dirs are DROPPED
(see :data:`~harness.agents.HOME_ESCAPING_CONFIG_VARS`) — opencode keeps its auth
and its session db under ``$XDG_DATA_HOME`` (default ``~/.local/share/opencode``),
so a set ``XDG_DATA_HOME`` would walk past the redirected HOME to the real
credential and the real db. Dropped, opencode falls back to ``$HOME/.local/share``
inside the run's home — which is also where ``opencode export`` then finds the run's
own session (measured: an isolated empty HOME both serves a free model and exports
it). Skills install to ``<home>/.agents/skills/`` (measured to activate). Login is
optional: a free ``*-free`` model runs with zero credentials, so :func:`seed_auth`
copies ``auth.json`` when present and skips silently when not (unlike agy, which
must raise).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from harness.agents import AgentCapabilities, isolated_env
from harness.agents._shell import _lit_calls_from_bash
from harness.executor import (
    ExecutorResult,
    LitCall,
    ToolResult,
    install_repo_skills,
)

OPENCODE_BIN = os.environ.get("LITMAN_BENCH_OPENCODE_BIN", "opencode")

# opencode is a ROUTER: `--model` is ALWAYS passed explicitly on a real run, so a
# run served by any default is not a data point. This default is a currently-live
# FREE model (loginless), used only to keep a smoke invocation working; which
# CONTROLLED model opencode routes for the real comparison is wangq's call (spec
# "待 wangq 决定"). Run `opencode models` for the live `*-free` list.
DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"

# opencode's own permission bypass: "auto-approve permissions that are not
# explicitly denied". It is NOT one of the two flags the product red line forbids
# (`--dangerously-skip-permissions` / `--yolo`), so it needs no bench carve-out —
# it is just this adapter's authorization flag, published verbatim in the report
# like every other agent's.
PERMISSION_FLAGS = ("--auto",)

#: opencode's credential, relative to a HOME. ``$XDG_DATA_HOME`` defaults to
#: ``~/.local/share`` and is dropped from the child env, so the run's own auth
#: lives here under the redirected HOME.
AUTH_RELPATH = Path(".local") / "share" / "opencode" / "auth.json"

# The top-level opencode token keys we recognize. Used as the "is this the shape
# we know?" guard, exactly like cursor's: a block carrying NONE of these is a
# renamed schema, and reporting it as {} (not observed) beats defaulting every
# counter to a truthy, fictional 0. ``total`` is deliberately NOT here — it is a
# sum we do not consume, and guarding on it would let a total-only block through
# as all-zeros.
_USAGE_TOP_KEYS = ("input", "output", "reasoning", "cache")


def normalize_usage(token_blocks: list[dict]) -> dict:
    """Sum opencode's per-step ``part.tokens`` blocks onto the internal counters.

    ``[]`` in (no ``step_finish`` carried tokens) -> ``{}`` out: nothing to
    account, which downstream reads as "not observed", never "zero tokens spent".

    A list whose blocks carry none of :data:`_USAGE_TOP_KEYS` is ALSO ``{}`` out —
    the same reason cursor's ``normalize_usage`` exists. If opencode renames its
    keys, defaulting every counter to 0 would produce a TRUTHY
    ``{"input_tokens": 0, ...}`` that sails past every "did we observe usage?"
    check and is summed into the report as a spawn that provably cost nothing. A
    missing number is recoverable; a wrong one is not.

    ``reasoning`` is opencode-only and kept under ``reasoning_tokens`` — not folded
    into ``output_tokens`` (that would inflate output) and not dropped. It is not
    in ``harness.batch._USAGE_KEYS``, so the grand-total summer ignores it; the
    per-spawn record keeps it for the reader.

    ``num_turns`` is an explicit ``None``: opencode counts steps, not turns, and a
    visible ``null`` says "no turn count" where a missing key looks like a parser
    that forgot.
    """
    blocks = [t for t in token_blocks if isinstance(t, dict)]
    if not blocks:
        return {}
    if not any(k in t for t in blocks for k in _USAGE_TOP_KEYS):
        return {}
    out = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    for t in blocks:
        out["input_tokens"] += int(t.get("input", 0) or 0)
        out["output_tokens"] += int(t.get("output", 0) or 0)
        out["reasoning_tokens"] += int(t.get("reasoning", 0) or 0)
        cache = t.get("cache")
        if isinstance(cache, dict):
            out["cache_creation_input_tokens"] += int(cache.get("write", 0) or 0)
            out["cache_read_input_tokens"] += int(cache.get("read", 0) or 0)
    out["num_turns"] = None
    return out


def parse_stream(lines: list[str]) -> tuple[ExecutorResult, str | None]:
    """Parse an opencode ``--format json`` event list.

    Returns the :class:`ExecutorResult` with everything the STREAM carries, plus
    the ``sessionID`` (or ``None``) — the served model is not in the stream, so the
    adapter recovers it separately via ``opencode export`` using this id.
    """
    result = ExecutorResult()
    session_id: str | None = None
    token_blocks: list[dict] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        result.raw_events.append(event)

        sid = event.get("sessionID")
        if sid:
            session_id = str(sid)

        etype = event.get("type")
        part = event.get("part") or {}

        if etype == "tool_use":
            tool = str(part.get("tool", ""))
            result.tool_names.append(tool)
            state = part.get("state") or {}
            inp = state.get("input") or {}
            call_id = part.get("callID")
            call_id = str(call_id) if call_id else None
            if tool == "bash":
                cmd = str(inp.get("command", ""))
                for argv in _lit_calls_from_bash(cmd):
                    # Every lit segment of a compound command shares the ONE bash
                    # callID (and thus the one combined stdout below) — same
                    # pairing claude uses for `lit a && lit b`.
                    result.lit_calls.append(
                        LitCall(argv=argv, raw=cmd, tool_use_id=call_id)
                    )
                meta = state.get("metadata") or {}
                stdout = meta.get("output")
                if stdout is None:
                    stdout = state.get("output")
                if stdout is not None:
                    result.tool_results.append(
                        ToolResult(
                            tool="bash", content=str(stdout), tool_use_id=call_id
                        )
                    )
            elif tool == "skill":
                # Native skill tool: the name is the routing label (NOT a
                # read-the-SKILL.md heuristic — that is cursor's fallback).
                name = inp.get("name")
                if name:
                    result.skills.append(str(name))

        elif etype == "step_finish":
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                token_blocks.append(tokens)

        elif etype == "text":
            # Assistant prose; the last one is the final answer.
            result.final_text = str(part.get("text", ""))

    result.usage = normalize_usage(token_blocks)
    return result, session_id


#: Cap for the export subprocess: it only reads a local session db, so a slow one
#: is hung, not working, and the executor's per-card timeout does NOT cover this
#: after-the-fact child — an uncapped hang would stall the whole round.
_EXPORT_TIMEOUT_S = 60.0
#: Fixed (not randomized — tests must stay deterministic) backoff before the one
#: retry: the session db may not be flushed the instant the run ends.
_EXPORT_RETRY_BACKOFF_S = 1.5


def _run_export(bin_: str, session_id: str, env: dict[str, str] | None) -> str | None:
    """Run ``opencode export <sid>`` in the RUN's isolated env; stdout or ``None``.

    ``env`` MUST be the same env the run itself used — export reads the session db
    under that env's redirected ``$HOME``. No ``cwd`` is passed on purpose: export
    is GLOBAL BY SESSION ID (verified live — it resolves a session from any cwd
    under the same HOME; the session's ``directory`` field only records where it was
    created), so it finds the run's session even though the live agent ran in
    ``neutral_cwd``.

    Runs at most twice. The harvest misses occasionally (a large session's db may
    not be flushed the instant the run ends), so one fixed-backoff retry recovers a
    good share of those timing misses at low risk. ANY attempt that does not return
    clean stdout is this-attempt-failed and triggers the retry — a non-zero exit OR
    a :class:`subprocess.TimeoutExpired` (a hung export must not stall the round);
    the retry is deliberately not keyed to one exit code, since the exact failure
    mode was never pinned. Two failures return ``None`` — the served model is then
    unrecoverable, and the caller reports ``None`` rather than inventing the
    requested model.

    Stdout is captured as BYTES and decoded HERE with an explicit ``utf-8`` +
    ``errors="replace"`` (not ``text=True``'s strict, locale-dependent decode):
    ``opencode export`` can truncate at a 64KB boundary mid-multibyte-character, and
    a strict decode raises ``UnicodeDecodeError`` (a ``ValueError``, NOT a
    ``json.JSONDecodeError``) that would sail past ``_served_model``'s guard and abort
    the whole round. Lenient decode never raises; the truncated tail degrades to a
    U+FFFD and the now-unparseable JSON becomes an honest ``None`` downstream. Parsing
    the model string out of stdout stays in :meth:`OpencodeAdapter._served_model`;
    this function only hands back the decoded stdout.
    """
    for attempt in range(2):
        if attempt:
            time.sleep(_EXPORT_RETRY_BACKOFF_S)
        try:
            proc = subprocess.run(
                [bin_, "export", session_id],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_EXPORT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode == 0:
            # Explicit utf-8 + errors="replace": export can truncate its stdout at a
            # 64KB boundary mid-multibyte-character (a lone lead byte with its
            # continuation cut off), which strict decoding raises on. Lenient decode
            # never raises; the mangled tail becomes U+FFFD and the now-broken JSON
            # degrades to None via _served_model's json.loads. Decode only here, on
            # the returned attempt — error/timeout paths never touch stdout.
            return proc.stdout.decode("utf-8", errors="replace")
    return None


# ---------------------------------------------------------------------------
# Login seed
# ---------------------------------------------------------------------------


def seed_auth(home: Path) -> None:
    """Copy opencode's ``auth.json`` into the fresh HOME IF it exists; else skip.

    A DELIBERATE difference from agy's ``seed_auth``, which raises when the
    credential is absent: agy hangs on a browser OAuth fallback with no token,
    whereas a free opencode ``*-free`` model runs with ZERO credentials (measured:
    an isolated empty HOME serves one). So a missing ``auth.json`` is a valid
    loginless run, not a hang — only paid/controlled models need the file. Same
    minimal shape as claude/cursor (the credential and only the credential);
    ``copy2`` preserves the file's mode.
    """
    src = Path.home() / AUTH_RELPATH
    if not src.is_file():
        return
    dst = home / AUTH_RELPATH
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpencodeAdapter:
    """Drives ``opencode run --format json`` with a redirected HOME."""

    name = "opencode"
    default_model = DEFAULT_MODEL
    permission_flags = PERMISSION_FLAGS
    supports_anthropic_proxy = False  # its own provider/auth, ignores ANTHROPIC_*
    evidence_source = "the event stream (+ `opencode export` for the served model)"
    capabilities = AgentCapabilities(
        tokens=True,         # step_finish.part.tokens, summed per step
        turns=False,         # steps exist, turns do not — report None
        served_model=True,   # not in the stream, but `opencode export` has it
        routing=True,        # native skill tool: part.state.input.name
    )

    #: The run's isolated env, stashed by :meth:`prepare` so :meth:`parse` can run
    #: ``opencode export`` under the SAME redirected HOME (``parse``'s signature
    #: takes no env — executor.py:318). ``run_card`` builds ONE adapter per card
    #: and calls ``prepare`` before ``parse`` (executor.py:268/274/318), so this
    #: per-run state is valid across that pair; every ``prepare`` reassigns it, so
    #: nothing leaks across cards even though the instance is not reused.
    _env: dict[str, str] | None = None

    @property
    def bin(self) -> str:
        # Late-bound on purpose: binding the module constant at class-creation
        # time would make any post-import override of it a silent no-op.
        return OPENCODE_BIN

    def skills_dir(self, base: Path) -> Path:
        """``<home>/.agents/skills/`` — measured to activate under a redirected HOME."""
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
                "opencode has no Anthropic-compatible proxy mode: --base-url / "
                "--auth-token need an agent whose supports_anthropic_proxy is True. "
                "(run_bench refuses this at the CLI boundary; reaching here means "
                "a caller bypassed it.)"
            )
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        seed_auth(home)
        install_repo_skills(self.skills_dir(base))

        registry_dir = base / "opencode-registry"
        registry_dir.mkdir(parents=True, exist_ok=True)

        env = isolated_env(home=home, run_vault=run_vault, registry_dir=registry_dir)
        # Stash for the export subprocess in parse(): it must read the run's own
        # session db, which lives under this env's redirected HOME.
        self._env = env
        return env

    def build_argv(self, prompt: str, *, model: str, cwd: Path) -> list[str]:
        # opencode's bash tool does NOT honor the process cwd (it defaults to the
        # repo work-tree root); `--dir` pins it to the run's neutral cwd. The
        # positional message MUST stay last, so --dir goes before it.
        return [
            self.bin,
            "run",
            "--format", "json",
            *PERMISSION_FLAGS,
            "--dir", str(cwd),
            "--model", model,
            prompt,
        ]

    def parse(self, stdout: str, *, base: Path) -> ExecutorResult:
        result, session_id = parse_stream(stdout.splitlines())
        result.model_served = self._served_model(session_id)
        return result

    def _served_model(self, session_id: str | None) -> str | None:
        """``"<providerID>/<id>"`` from ``opencode export``, or ``None``.

        ``None`` — never the requested model — whenever the served model cannot be
        recovered: no sessionID in the stream (short-circuits without a spawn), a
        non-zero export exit, or JSON we cannot parse. Trusting the request here is
        precisely the mismatch Phase 0 pins.
        """
        if not session_id:
            return None
        raw = _run_export(self.bin, session_id, self._env)
        if raw is None:
            return None
        try:
            info = (json.loads(raw) or {}).get("info") or {}
        except json.JSONDecodeError:
            return None
        model = info.get("model") or {}
        provider_id = model.get("providerID")
        model_id = model.get("id")
        if provider_id and model_id:
            return f"{provider_id}/{model_id}"
        return None
