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
  Copying ``vault/`` is not by itself enough to mean that: a seed's
  ``lit-config.yaml`` lives INSIDE the vault and names its projects by ABSOLUTE
  path, so the copy inherits pointers straight back to the shared seed and any
  project write follows them home. :meth:`RunVault._localize_projects` closes
  that hole. It matters more than one run: seeds are cached ACROSS runs, so a
  card that got through poisoned every later card on the node, not just its own
  run. :func:`harness.seeds.assert_seed_intact` is the standing proof it stays
  closed.

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

    On ``__enter__`` it ``cp``-copies the seed into ``<run_root>/bench-<uuid>/vault``,
    prepares an isolated registry dir alongside, and localizes the seed's
    ``projects/`` into the run dir (:meth:`_localize_projects` — without it the
    copy still points at the shared seed). On ``__exit__`` it removes the entire
    run dir (best-effort; a leftover under /tmp is a quota nuisance, not a
    correctness bug, so cleanup never masks a test failure by raising).
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
        try:
            self._localize_projects()
        except BaseException:
            # The copytree above is already on disk; __enter__ raising means no
            # caller ever gets the handle whose _cleanup would remove it (one
            # full vault leaked per round). __exit__ is idempotent + rmtree'd
            # with ignore_errors, so calling it eagerly here is safe.
            self.__exit__()
            raise
        return self

    def _localize_projects(self) -> None:
        """Copy the seed's ``projects/`` sibling in and repoint the config at it.

        **Copying the vault is not isolation.** A seed built with ``lit project
        add`` has the project's ABSOLUTE path baked into ``vault/lit-config.yaml``
        — which lives inside the vault, so the copy inherits it verbatim and
        still aims every project write back at the SHARED seed: ``lit unlink``,
        ``lit refresh``'s REFERENCES rebuild, ``lit link --rebuild-all``, a drift
        auto-repair. Copying the directory without repointing the config changes
        nothing (lit follows the absolute path home); repointing without copying
        leaves it dangling. It takes both.

        ``projects/`` lands as the vault's SIBLING (``<run_dir>/projects``,
        mirroring the seed root's own layout) because the bridge symlinks are
        relative (``../../../vault/papers/<id>``): that shape makes them resolve
        to THIS run's papers with no link rebuild.

        Paths are moved through ``lit project set-path``, never by editing the
        YAML: ``lit project --help`` is explicit that both truth sources are kept
        consistent by its subcommands and that neither side may be hand-edited.
        """
        assert self.run_dir is not None and self.vault is not None

        seed_root = self.seed_path.resolve().parent
        src_projects = seed_root / "projects"
        if src_projects.is_dir():
            shutil.copytree(src_projects, self.run_dir / "projects", symlinks=True)

        config = self.vault / "lit-config.yaml"
        if not config.is_file():
            return
        # ruamel, not PyYAML, to match harness.checker and litman itself: ruamel
        # is the only YAML litman declares, so PyYAML is here transitively at
        # best. This module is imported by harness.batch, which makes an import
        # error the whole bench's problem.
        from ruamel.yaml import YAML

        payload = YAML(typ="safe").load(config.read_text(encoding="utf-8")) or {}
        for name, configured in (payload.get("projects") or {}).items():
            try:
                # Remap by RELPATH, never <run_dir>/projects/<name>: a project's
                # NAME is a label papers tag with and may differ from its folder
                # name (`lit project add --help`), so the two only coincide by
                # convention.
                rel = Path(str(configured)).resolve().relative_to(seed_root)
            except ValueError:
                raise RuntimeError(
                    f"seed {seed_root} has project {name!r} configured at "
                    f"{configured!r}, which is OUTSIDE the seed root. The run copy "
                    "cannot contain it, so the card would write through to that "
                    "path for real. No seed builds this today; if one now does, "
                    "that is a new case to design for, not to silently rewrite."
                ) from None
            result = self.run(
                "project", "set-path", str(name), str(self.run_dir / rel),
                # log=False: littest-run.jsonl is the scoring evidence that "the
                # CLI was actually invoked" BY THE AGENT. Harness plumbing in that
                # file would make the card's own `ran:` assertions read commands
                # nobody asked the agent for.
                log=False,
            )
            if result.exit_code != 0:
                # Loud, because the silent version is this whole bug: a set-path
                # that fails leaves the config aimed at the shared seed, and the
                # card then poisons it exactly as before — while everything looks
                # normal.
                raise RuntimeError(
                    f"could not localize project {name!r} into the run copy: "
                    f"`lit project set-path` exited {result.exit_code}\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )

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
