"""litman's one way to run git, so that no git command can wait forever.

Every git command litman runs goes through :func:`run_git`, which:

* sets ``GIT_TERMINAL_PROMPT=0``: git fails instead of asking for a user name
  or password on the terminal, where no one may be there to answer (a sync
  run by an agent, or by the GUI);
* passes ``-c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60``: an HTTP
  transfer that stays under 1000 bytes a second for a minute is given up;
* drops the variables that point git at another repository (``GIT_DIR`` and
  the like): litman always names the repository it means;
* opens no console window from a process that has none (the GUI's auto sync
  under ``litw``, :func:`litman.core.terminal.hidden_console_flags`).

With ``progress``, git's output goes to the terminal as git writes it, so a
clone or a fetch shows its progress, and its last lines are still kept for
the error message.
"""

from __future__ import annotations

import codecs
import io
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO, cast

from litman.core.terminal import hidden_console_flags
from litman.exceptions import CodeError

#: Given to every git command: an HTTP transfer slower than 1000 bytes a
#: second for 60 seconds is given up.
LOW_SPEED_FLAGS: tuple[str, ...] = (
    "-c", "http.lowSpeedLimit=1000",
    "-c", "http.lowSpeedTime=60",
)

#: Variables that would point ``git -C <dir>`` at another repository.
GIT_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
    }
)

#: How much of git's output a streamed run keeps for the error message.
_KEPT_OUTPUT = 8192

_LINE_BREAKS_RE = re.compile(r"[\r\n]+")


class GitNotFoundError(CodeError):
    """git is not installed, or not on PATH."""


def git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment every git command runs in: this one, made safe."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in GIT_LOCATION_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def git_command(args: Sequence[str], *, repo: Path | None = None) -> list[str]:
    """The full command line for ``git <args>``, in ``repo`` when given."""
    command = ["git", *LOW_SPEED_FLAGS]
    if repo is not None:
        command += ["-C", str(repo)]
    return [*command, *args]


def run_git(
    args: Sequence[str],
    *,
    repo: Path | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    progress: bool = False,
    progress_to: TextIO | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` (in ``repo`` when given) and return what it did.

    The output is captured, ``stdout`` and ``stderr`` apart. With
    ``progress``, git's output is written to ``progress_to`` (the terminal's
    stderr by default) as it comes, and both fields of the result hold its
    last part; pass ``--progress`` in ``args`` to have git report progress
    while its output is not a terminal. ``timeout`` (seconds) applies to a
    captured run only.

    Raises:
        GitNotFoundError: git is not installed.
        subprocess.TimeoutExpired: the run took longer than ``timeout``.
    """
    command = git_command(args, repo=repo)
    run_env = git_env(env)
    try:
        if progress:
            return _run_streamed(command, run_env, progress_to or sys.stderr)
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            env=run_env,
            timeout=timeout,
            creationflags=hidden_console_flags(),
        )
    except FileNotFoundError as err:
        raise GitNotFoundError(
            "`git` executable not found on PATH. Install git first."
        ) from err


def _run_streamed(
    command: list[str], env: dict[str, str], out: TextIO
) -> subprocess.CompletedProcess[str]:
    """Run ``command``, writing its output to ``out`` as it comes; keep its end."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=hidden_console_flags(),
    )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    kept = ""
    pipe = cast(io.BufferedReader, process.stdout)
    with pipe:
        while chunk := pipe.read1(4096):
            text = decoder.decode(chunk)
            out.write(text)
            out.flush()
            kept = (kept + text)[-_KEPT_OUTPUT:]
    text = decoder.decode(b"", final=True)
    out.write(text)
    kept = (kept + text)[-_KEPT_OUTPUT:]
    returncode = process.wait()
    return subprocess.CompletedProcess(command, returncode, stdout=kept, stderr=kept)


def output_tail(text: str | None, lines: int = 5) -> str:
    """The last ``lines`` lines of git's output, progress updates included."""
    kept = [line for line in _LINE_BREAKS_RE.split(text or "") if line.strip()]
    return "\n".join(kept[-lines:])
