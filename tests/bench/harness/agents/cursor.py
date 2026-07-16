"""Cursor Agent adapter — same job as claude, an entirely different event shape.

``cursor-agent`` speaks its own ``stream-json``: not the Anthropic block shape at
all. Recovering the same evidence therefore means reading different places, and
each of the four below was found by recording a real run, not by reading docs.

* **lit argv** — ``tool_call.shellToolCall.args.parsingResult.executableCommands``.
  Cursor parses the shell command for us: ``lit list --format json | python3 -c ...``
  arrives already split into a ``lit`` entry and a ``python3`` entry, with
  redirects lifted out. So this adapter does NOT re-split on ``&&`` / ``|`` the way
  the claude one has to — it just keeps the entries whose ``name`` is ``lit``.
* **lit stdout** — ``tool_call.shellToolCall.result.success.stdout``, one level
  deeper than the obvious ``tool_call.result``. Reading the obvious place yields
  ``{}`` — an empty-but-plausible result that looks like a broken run rather than
  a wrong parser.
* **skill activation** — cursor has no ``Skill`` tool. It activates a skill by
  *reading the file*, so the routing signal is a ``readToolCall`` whose path ends
  ``skills/<name>/SKILL.md``. ``<name>`` is the label.
* **tokens** — ``result.usage`` counts in **camelCase** (``inputTokens``) where
  claude uses snake_case (``input_tokens``), and carries **no turn count**. Handing
  cursor's dict to the shared snake_case summing would not raise — it would sum to
  a clean, plausible, entirely fictional 0. :func:`normalize_usage` therefore maps
  the keys here, at the edge, and reports the absent turn count as ``None``.

One more thing the stream will lie about: ``system/init.permissionMode`` still
reads ``"default"`` while ``--force`` is in effect, so how a run was authorized
cannot be audited from the transcript. The harness records its own argv and the
adapter's ``permission_flags`` instead.

Isolation: ``HOME`` is redirected, and the redirect does exactly one job —
hiding the user's installed skills and real registry. It does NOT deliver skills:
with ``HOME`` redirected, cursor reads *neither* ``~/.claude/skills/`` nor
``~/.agents/skills/`` (measured — real skills copied into both dirs of the
isolated HOME went undiscovered; why is unknown and does not need to be). Skills
are delivered through the **process CWD** instead: put them in
``<cwd>/.claude/skills/`` and cursor issues a ``readToolCall`` on the SKILL.md
right after init and acts on it — same behavior as a control run in a real HOME.
The login is ``~/.config/cursor/auth.json`` (``accessToken``/``refreshToken``),
NOT ``~/.cursor/cli-config.json`` — that file holds authInfo (email/userId) and
preferences but no token, so seeding it leaves the run logged out. ``auth.json``
is copied in (mirroring claude's ``seed_auth``); if ``CURSOR_API_KEY`` is
exported instead it survives via the inherited env. Phase 0's sentinel gate is
what actually proves the isolation held.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from harness.agents import AgentCapabilities
from harness.executor import (
    ExecutorResult,
    LitCall,
    ToolResult,
    install_repo_skills,
)

CURSOR_BIN = os.environ.get("LITMAN_BENCH_CURSOR_BIN", "cursor-agent")

# `auto` is cursor's default across ~189 models, so the model is ALWAYS passed
# explicitly — a run served by whatever `auto` picked is not a data point.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Cursor's permission bypass ("Force allow commands unless explicitly denied";
# `--yolo` is an alias). Recorded in the report verbatim — see the package
# docstring for why the bench holds this variable constant across agents.
PERMISSION_FLAGS = ("--force",)

# A read of `<anywhere>/skills/<name>/SKILL.md` is cursor's skill activation.
# Anchored on the filename so a read of a skill's reference/ material is not
# mistaken for an activation.
_SKILL_PATH_RE = re.compile(r"/skills/([^/]+)/SKILL\.md$")

# cursor's camelCase token counters -> the internal (claude-shaped) keys the
# report and `harness.batch._sum_usage` are written against.
_USAGE_MAP = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheWriteTokens": "cache_creation_input_tokens",
    "cacheReadTokens": "cache_read_input_tokens",
}


def normalize_usage(usage: dict) -> dict:
    """Map cursor's camelCase ``result.usage`` onto the internal counter keys.

    ``{}`` in -> ``{}`` out (no result event / no usage block: nothing to account,
    which downstream reads as "no tokens observed", not "zero tokens spent").

    A NON-empty dict carrying none of the keys we know is ALSO ``{}`` out, and
    that branch is half the reason this function exists. Defaulting every counter
    to 0 unconditionally would turn a renamed upstream key into
    ``{"input_tokens": 0, ...}`` — which is *truthy*, so it sails past every "did
    we observe usage?" test and gets summed into the report as a spawn that
    provably cost nothing. Reporting "not observed" makes the number go missing
    (honest) instead of wrong, and Phase 0's counter check fails the run up front.

    ``num_turns`` is set to an explicit ``None`` rather than omitted: cursor has
    no turn counter at all, and a visible ``null`` in a transcript says so, where
    a missing key just looks like a parser that forgot.
    """
    if not usage:
        return {}
    if not any(camel in usage for camel in _USAGE_MAP):
        return {}
    out: dict = {
        internal: int(usage.get(camel, 0) or 0)
        for camel, internal in _USAGE_MAP.items()
    }
    out["num_turns"] = None
    return out


def _tool_call_id(event: dict) -> str:
    """The id pairing a ``started`` tool_call event with its ``completed`` twin.

    Three independent sources, because losing this id is not a crash — it silently
    collapses every shell call onto one key and keeps only the last, i.e. it
    under-reports the agent's lit calls. ``shellToolCall`` in particular carries
    its own copy nested in ``args``. ``""`` when the event names no id at all;
    :func:`parse_stream` handles that rather than letting calls share a key.
    """
    tc = event.get("tool_call") or {}
    inner = tc.get("shellToolCall") or tc.get("readToolCall") or {}
    args = inner.get("args") or {}
    return str(
        event.get("call_id")
        or tc.get("toolCallId")
        or args.get("toolCallId")
        or ""
    )


def _fold_shell_call(result: ExecutorResult, cid: str, shell: dict) -> None:
    """Fold one ``shellToolCall`` into the result: lit argv + that call's stdout."""
    result.tool_names.append("shell")
    args = shell.get("args") or {}
    raw = str(args.get("command", ""))
    parsing = args.get("parsingResult") or {}
    for cmd in parsing.get("executableCommands") or []:
        if not isinstance(cmd, dict):
            continue
        name = str(cmd.get("name", ""))
        if name != "lit" and not name.endswith("/lit"):
            continue
        # Cursor already tokenized the argv for us; `type` is word/string, and
        # `value` is the token as written.
        argv = [
            str(a.get("value", ""))
            for a in (cmd.get("args") or [])
            if isinstance(a, dict)
        ]
        result.lit_calls.append(LitCall(argv=argv, raw=raw, tool_use_id=cid))

    # Nested one deeper than tool_call.result — see the module docstring.
    success = (shell.get("result") or {}).get("success") or {}
    stdout = success.get("stdout")
    if stdout is not None:
        result.tool_results.append(
            ToolResult(tool="shell", content=str(stdout), tool_use_id=cid)
        )


def _fold_read_call(result: ExecutorResult, read: dict) -> None:
    """Fold one ``readToolCall``: a SKILL.md read is the routing signal."""
    result.tool_names.append("read")
    path = str((read.get("args") or {}).get("path") or "")
    m = _SKILL_PATH_RE.search(path)
    if m:
        result.skills.append(m.group(1))


def parse_stream(lines: list[str]) -> ExecutorResult:
    """Parse a cursor ``stream-json`` event list into an :class:`ExecutorResult`."""
    result = ExecutorResult()
    # Cursor emits every tool call TWICE — `started` then `completed` — and both
    # carry the same fully-parsed args. Folding both would double every lit argv
    # in the evidence log, so calls are keyed by id and the later `completed`
    # event (the one carrying the result/stdout) overwrites its `started` twin.
    # Insertion order is preserved, so lit_calls stay chronological.
    calls: dict[str, dict] = {}

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
            # A DISPLAY NAME ("Sonnet 4.6 200K Medium No Thinking"), not the id we
            # requested. Kept verbatim; normalization is an explicit lookup.
            served = event.get("model")
            if served:
                result.model_served = str(served)

        elif etype == "tool_call":
            cid = _tool_call_id(event)
            if cid:
                calls[cid] = event
            elif event.get("subtype") == "completed":
                # No id anywhere (all three sources absent). Such an event cannot
                # be paired with its twin, so give it a unique key and keep only
                # the `completed` one — it is the half carrying the result. A
                # shared "" key would silently drop every call but the last; a
                # unique key on BOTH halves would double-count every argv.
                calls[f"_noid-{len(calls)}"] = event

        elif etype == "result":
            result.final_text = str(event.get("result", ""))
            result.usage = normalize_usage(event.get("usage") or {})

    for cid, event in calls.items():
        tc = event.get("tool_call") or {}
        shell = tc.get("shellToolCall")
        read = tc.get("readToolCall")
        if isinstance(shell, dict):
            _fold_shell_call(result, cid, shell)
        elif isinstance(read, dict):
            _fold_read_call(result, read)

    return result


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def _real_cursor_auth() -> Path:
    """The user's real ``auth.json`` — the file that actually carries the tokens.

    NOT ``~/.cursor/cli-config.json``: that one has authInfo (email/userId) and
    preferences but no token, and seeding it produces a logged-out run.
    """
    return Path.home() / ".config" / "cursor" / "auth.json"


class CursorAdapter:
    """Drives ``cursor-agent -p`` with a redirected ``HOME``."""

    name = "cursor"
    default_model = DEFAULT_MODEL
    permission_flags = PERMISSION_FLAGS
    capabilities = AgentCapabilities(
        tokens=True,         # four counters, camelCase (normalize_usage)
        turns=False,         # no turn count anywhere in the stream
        served_model=True,   # system/init, as a display name
        routing=True,        # readToolCall on SKILL.md is the activation
    )

    @property
    def bin(self) -> str:
        # Late-bound on purpose: binding the module constant at class-creation
        # time would make any post-import override of it a silent no-op.
        return CURSOR_BIN

    def skills_dir(self, base: Path) -> Path:
        """``<cwd>/.claude/skills/`` — the process CWD, the only place a
        HOME-redirected cursor was measured to discover skills (HOME-level dirs
        are ignored; see the module docstring). ``base == run_vault.parent`` is
        the executor's convention (grep ``base = run_vault.parent``), so
        ``base / "cwd"`` IS ``neutral_cwd_for(run_vault)``: installing here
        creates the CWD early and the executor's later ``mkdir(exist_ok=True)``
        is a no-op on it. CWD-level ``.agents/skills`` appears in cursor's glob
        list but was never verified live — this is the path that was."""
        return base / "cwd" / ".claude" / "skills"

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
                "cursor has no Anthropic-compatible proxy mode: --base-url / "
                "--auth-token are claude-only. Drop them, or run --agent claude."
            )
        home = base / "home"
        auth_dir = home / ".config" / "cursor"
        auth_dir.mkdir(parents=True, exist_ok=True)
        src = _real_cursor_auth()
        if src.is_file():
            shutil.copy2(src, auth_dir / "auth.json")

        install_repo_skills(self.skills_dir(base))

        registry_dir = base / "cursor-registry"
        registry_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["LIT_LIBRARY"] = str(run_vault)
        env["LITMAN_REGISTRY_DIR"] = str(registry_dir)
        env["HOME"] = str(home)
        # HOME is the isolation seam, but a set XDG_CONFIG_HOME would still point
        # cursor at the user's real ~/.config — drop it so the fake HOME wins.
        env.pop("XDG_CONFIG_HOME", None)
        return env

    def build_argv(self, prompt: str, *, model: str) -> list[str]:
        return [
            self.bin,
            "-p",
            prompt,
            "--model", model,
            "--output-format", "stream-json",
            *PERMISSION_FLAGS,
        ]

    def parse(self, stdout: str, *, base: Path) -> ExecutorResult:
        return parse_stream(stdout.splitlines())
