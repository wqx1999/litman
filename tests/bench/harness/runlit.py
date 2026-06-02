"""Phase B — isolation wrapper + run-vault lifecycle + jsonl logging.

This is the §4 safety red line welded into code (M34). A scoring unit is one
disposable run vault: ``cp <seed>`` into ``/tmp/bench-<uuid>/`` → run ``lit``
commands inside it → ``rm -rf`` the whole run dir on exit (M34 §3.0 layer 2).

Two isolations, both enforced here, never left to a scenario's good behaviour:

* **From the real vault / registry (safety).** :func:`isolated_env` redirects
  ``LITMAN_REGISTRY_DIR`` into the run dir AND deletes ``LIT_LIBRARY`` from the
  child env. The real vault on this machine is discoverable *only* via
  ``$LIT_LIBRARY`` (there is no registry file), so unsetting it is necessary,
  not merely redirecting the registry (M34 §4.1, implementation-verified
  2026-06-02).
* **Between scoring units (comparability).** Each :class:`RunVault` is a fresh
  ``cp`` of the seed, so N repeats start identical and a failure never cascades.

Every ``lit`` invocation is a real **subprocess** (so the jsonl captures true
argv / exit / stdout / stderr and env injection works), is flag-driven (the
caller passes ``--yes`` / ``--auto-suffix`` as needed — OQ1), and never opens a
stdin pipe (``stdin=DEVNULL``). For determinism each command targets the run
vault with an explicit ``--library`` rather than relying on the registry active
pointer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from harness.seeds import LIT_BIN

DEFAULT_RUN_ROOT = Path("/tmp")
RUN_LOG_FILENAME = "littest-run.jsonl"


@dataclass
class RunResult:
    """The captured outcome of one ``lit`` subprocess invocation."""

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str


def isolated_env(registry_dir: Path) -> dict[str, str]:
    """Build the child env that isolates a run from the real vault + registry.

    Starts from ``os.environ`` (so PATH / conda survive), then:

    * sets ``LITMAN_REGISTRY_DIR=<registry_dir>`` — redirects the vault registry
      out of the user's real ``~/.config/litman/``;
    * **deletes ``LIT_LIBRARY``** — the user's real vault is discoverable only
      through this var, so it must not leak into the child.

    Returned dict is a fresh copy; the caller's environment is untouched.
    """
    env = os.environ.copy()
    env["LITMAN_REGISTRY_DIR"] = str(registry_dir)
    env.pop("LIT_LIBRARY", None)
    return env


class RunVault:
    """Context manager: a disposable copy of a seed vault for one scoring unit.

    Usage::

        with RunVault(seed_path) as rv:
            rv.run("list", log=True)
        # run dir is gone here

    On ``__enter__`` it ``cp``-copies the seed into ``<run_root>/bench-<uuid>/vault``
    and prepares an isolated registry dir alongside. On ``__exit__`` it removes
    the entire run dir (best-effort; a leftover under /tmp is a quota nuisance,
    not a correctness bug, so cleanup never masks a test failure by raising).
    """

    def __init__(self, seed_path: Path, run_root: Path = DEFAULT_RUN_ROOT) -> None:
        self.seed_path = Path(seed_path)
        self.run_root = Path(run_root)
        self.run_dir: Path | None = None
        self.vault: Path | None = None
        self.registry_dir: Path | None = None
        self.log_path: Path | None = None
        self.env: dict[str, str] | None = None

    def __enter__(self) -> "RunVault":
        self.run_dir = self.run_root / f"bench-{uuid.uuid4().hex}"
        self.run_dir.mkdir(parents=True)
        self.vault = self.run_dir / "vault"
        # cp the seed snapshot (symlinks preserved — views/by-* are symlinks).
        shutil.copytree(self.seed_path, self.vault, symlinks=True)
        self.registry_dir = self.run_dir / "registry"
        self.registry_dir.mkdir()
        self.log_path = self.run_dir / RUN_LOG_FILENAME
        self.env = isolated_env(self.registry_dir)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.run_dir is not None and self.run_dir.exists():
            shutil.rmtree(self.run_dir, ignore_errors=True)

    def run(self, *args: str, log: bool = True, cwd: Path | None = None) -> RunResult:
        """Run ``lit <args>`` as an isolated subprocess; optionally log to jsonl.

        ``--library <run-vault>`` is appended automatically unless the caller
        already passed ``--library`` (so a deliberate decoy-vault test can
        override it). stdin is ``/dev/null``; the child env is the isolated env
        built in ``__enter__``. ``cwd`` defaults to the run vault so a child's
        ``find_vault`` cwd-walk (no flag, no active registry) resolves here.
        """
        assert self.vault is not None and self.env is not None

        argv = list(args)
        if "--library" not in argv:
            argv += ["--library", str(self.vault)]

        proc = subprocess.run(
            [str(LIT_BIN), *argv],
            env=self.env,
            cwd=str(cwd) if cwd is not None else str(self.vault),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        result = RunResult(
            argv=argv,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        if log:
            self._append_log(result)
        return result

    def _append_log(self, result: RunResult) -> None:
        """Append one JSON object per line to ``littest-run.jsonl``.

        Records argv / exit_code / stdout / stderr — the agent can neither
        forget nor edit this, so it is the primary scoring evidence (proves the
        CLI was actually invoked). No wall-clock field: the deterministic tests
        must not depend on timestamps.
        """
        assert self.log_path is not None
        line = json.dumps(
            {
                "argv": result.argv,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            ensure_ascii=False,
        )
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_run_log(log_path: Path) -> list[dict]:
    """Parse a ``littest-run.jsonl`` file into a list of record dicts.

    Returns ``[]`` when the file is absent (a unit that ran no commands). A
    malformed line raises — a corrupt log is a real fault, not something to
    silently skip (invariant #14 no-silent-skip spirit).
    """
    if not Path(log_path).is_file():
        return []
    out: list[dict] = []
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out
