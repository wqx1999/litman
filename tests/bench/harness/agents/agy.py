"""Antigravity (``agy``) adapter — an agent that reports nothing about itself.

agy emits plain prose. No event stream, no tool events, no token counters, no
served-model line. Three of the bench's axes therefore have no source at all, and
the honest thing — the whole point of :class:`~harness.agents.AgentCapabilities` —
is to report them as not-measurable rather than as zeros:

* ``tokens``       -> ``None`` (no counters exist to read)
* ``model_served`` -> ``None`` (agy never says which model served the run)
* ``routing``      -> ``NOT_MEASURABLE`` (no ``Skill`` tool, no file-read event;
  the ONLY remaining source would be reading the agent's prose and deciding it
  "sounds like" it used the skill, which is inventing a number)

The one axis that CAN be recovered is execution, via a ``lit`` **PATH shim**: a
generated script placed first on the child's ``PATH`` that logs every invocation's
argv / stdout / exit code to ``lit-calls.jsonl`` and then runs the real ``lit``,
passing its streams and exit code through unchanged. Two rules make it safe:

1. the real ``lit`` path is frozen into the shim at write time. A shim that looked
   ``lit`` up on ``PATH`` at run time would find *itself* first and recurse;
2. the shim is agy's evidence source and nobody else's. It is strictly better than
   splitting command strings (it sees ``$(lit show 3)``, which no event stream
   reports), but putting it under claude would change how claude's evidence is
   gathered and invalidate the existing live baseline — so claude and cursor keep
   their event streams. One agent, one evidence source, never two that can disagree.

Known blind spot: an agent that invokes ``lit`` by absolute path bypasses the
shim. The skills say bare ``lit``, so this is accepted — and Phase 0's evidence
gate fails the run outright if the log comes back empty, rather than reporting a
confident 0% execution rate.

Isolation: ``HOME`` (agy's own logs confirm ``appDataDir=<home>/.gemini/...``;
``ANTIGRAVITY_EXECUTABLE_DATA_DIR`` is not a seam — tested, no effect). The login
is a single file — ``~/.gemini/antigravity-cli/antigravity-oauth-token`` (JSON:
``auth_method`` + a ``token`` block with access/refresh tokens) — and copying just
that file into a brand-new empty HOME is enough: measured, ``agy -p`` answers
normally, skills dir absent, isolation holds. An expired ``expiry`` does not
matter either — the ``refresh_token`` mints a new access token *inside the bench
HOME's copy*, and the user's real token file is never modified (mtime unchanged,
login still valid afterwards). So each run builds a fresh HOME and seeds only the
token, mirroring claude's ``seed_auth``. One deliberate difference: a missing
token RAISES (see :func:`seed_auth`) instead of being skipped, because agy with
no credential falls back to a browser OAuth flow and hangs — a silent skip would
surface as Phase 0's probe timing out, not as an actionable message.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from harness.agents import AgentCapabilities
from harness.executor import (
    ExecutorResult,
    LitCall,
    ToolResult,
    install_repo_skills,
)
from harness.seeds import LIT_BIN

AGY_BIN = os.environ.get("LITMAN_BENCH_AGY_BIN", "agy")

# agy validates the model name before spending anything, and these are display
# names with spaces and parens — always passed as a list element, never through a
# shell.
DEFAULT_MODEL = "Claude Sonnet 4.6 (Thinking)"

# agy's permission bypass. Its settings.json allow-rules were measured to be
# decorative (emptying `allow: []` changed nothing; the binary itself says
# "Settings allow-rules do not apply; re-run with --dangerously-skip-permissions
# to auto-approve all tools"), so this flag is the only way to drive it headless.
# Recorded in the report verbatim: "running litman on Antigravity requires the
# user to globally disable tool approval" is a FINDING about agy, and it is a mark
# against it, not a footnote.
PERMISSION_FLAGS = ("--dangerously-skip-permissions",)

#: agy's OAuth credential, relative to a HOME. The whole login is this one file.
TOKEN_RELPATH = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"

LIT_CALLS_FILENAME = "lit-calls.jsonl"

_SHIM_BODY = '''
import json
import shlex
import subprocess
import sys

proc = subprocess.run([LIT_BIN, *sys.argv[1:]], capture_output=True, text=True)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
with open(LOG_PATH, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "argv": sys.argv[1:],
        "raw": shlex.join(["lit", *sys.argv[1:]]),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
    }, ensure_ascii=False) + "\\n")
sys.exit(proc.returncode)
'''


def seed_auth(home: Path) -> None:
    """Copy the OAuth token — one file — into the fresh bench HOME.

    Same shape as claude's ``seed_auth`` (the credential and ONLY the credential;
    settings stay clean and reproducible), with one deliberate difference: a
    missing source RAISES instead of being skipped. claude without credentials
    fails fast and legibly; agy falls back to a browser OAuth flow and *hangs*,
    so a silent skip here would surface as Phase 0's probe timing out rather than
    as this message. ``copy2`` preserves the file's 0600 mode. An expired token
    is fine — agy refreshes it inside the bench copy, never in the user's file.
    """
    src = Path.home() / TOKEN_RELPATH
    if not src.is_file():
        raise RuntimeError(
            f"agy is not logged in on this machine ({src} not found).\n"
            "  Run `agy` once, interactively, and complete its login — that is "
            "the whole setup.\n"
            "  The harness never performs a login for you."
        )
    dst = home / TOKEN_RELPATH
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def resolve_lit_bin() -> Path:
    """The real ``lit``, as an absolute path, frozen into the shim at write time."""
    lit = Path(LIT_BIN)
    if not lit.is_absolute():
        found = shutil.which(str(lit))
        if not found:
            raise RuntimeError(
                f"cannot resolve the real lit binary from {LIT_BIN!r}; set "
                "LITMAN_BENCH_LIT_BIN to an absolute path so the shim cannot "
                "resolve itself recursively."
            )
        lit = Path(found)
    return lit


def write_lit_shim(base: Path) -> Path:
    """Generate ``<base>/shim/lit`` and return the dir to prepend to ``PATH``.

    The real ``lit`` and the log path are baked in as literals: the shim must not
    consult ``PATH`` (it is itself first on it) and must not depend on any env var
    the agent could change.
    """
    lit = resolve_lit_bin()
    shim_dir = base / "shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "lit"
    shim.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                '"""Bench shim: log every lit call, then run the real lit."""',
                f"LIT_BIN = {json.dumps(str(lit))}",
                f"LOG_PATH = {json.dumps(str(base / LIT_CALLS_FILENAME))}",
                _SHIM_BODY,
            ]
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim_dir


def read_lit_calls(base: Path) -> list[dict]:
    """Read ``<base>/lit-calls.jsonl``; ``[]`` when the agent ran no ``lit``."""
    path = base / LIT_CALLS_FILENAME
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AgyAdapter:
    """Drives ``agy -p`` with a fresh token-seeded HOME and a ``lit`` PATH shim."""

    name = "agy"
    default_model = DEFAULT_MODEL
    permission_flags = PERMISSION_FLAGS
    capabilities = AgentCapabilities(
        tokens=False,        # no counters emitted at all
        turns=False,
        served_model=False,  # never reports which model served the run
        routing=False,       # no Skill tool, no file-read events -> NOT_MEASURABLE
    )

    @property
    def bin(self) -> str:
        # Late-bound on purpose: binding the module constant at class-creation
        # time would make any post-import override of it a silent no-op.
        return AGY_BIN

    def skills_dir(self, base: Path) -> Path:
        """``{appDataDir}/skills/``. agy does NOT read ``~/.agents/skills/``."""
        return base / "home" / ".gemini" / "antigravity-cli" / "skills"

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
                "agy has no Anthropic-compatible proxy mode: --base-url / "
                "--auth-token are claude-only. Drop them, or run --agent claude."
            )
        # A brand-new HOME per run (measured: costs ~1s over a warm one), holding
        # exactly two things — the seeded token and the repo-source skills. There
        # is nothing to wipe: nothing else ever existed in it.
        home = base / "home"
        home.mkdir(parents=True, exist_ok=True)
        seed_auth(home)
        install_repo_skills(self.skills_dir(base))

        shim_dir = write_lit_shim(base)

        registry_dir = base / "agy-registry"
        registry_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["LIT_LIBRARY"] = str(run_vault)
        env["LITMAN_REGISTRY_DIR"] = str(registry_dir)
        env["HOME"] = str(home)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
        return env

    def build_argv(self, prompt: str, *, model: str) -> list[str]:
        # `-p` MUST come last: `agy -p --model X "prompt"` swallows `--model` as
        # part of the prompt. The order is not cosmetic — do not "tidy" it.
        return [
            self.bin,
            *PERMISSION_FLAGS,
            "--model", model,
            "-p", prompt,
        ]

    def parse(self, stdout: str, *, base: Path) -> ExecutorResult:
        """Prose in, shim log out.

        ``usage`` stays ``{}`` (no counters exist) and ``model_served`` stays
        ``None`` — both flow downstream as "not observed", never as 0.
        """
        result = ExecutorResult(final_text=stdout.strip())
        for i, rec in enumerate(read_lit_calls(base)):
            # Synthetic id: pairs each logged call with its own stdout the same
            # way an event stream's tool_use_id would.
            tuid = f"shim-{i}"
            result.lit_calls.append(
                LitCall(
                    argv=[str(a) for a in rec.get("argv") or []],
                    raw=str(rec.get("raw") or ""),
                    tool_use_id=tuid,
                )
            )
            result.tool_results.append(
                ToolResult(
                    tool="shim",
                    content=str(rec.get("stdout") or ""),
                    tool_use_id=tuid,
                )
            )
            result.raw_events.append(rec)
        return result
