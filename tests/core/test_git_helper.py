"""litman's one way to run git (:mod:`litman.core.git`): no prompt, no stall.

Every git command gets ``GIT_TERMINAL_PROMPT=0`` and the low-speed limits,
and none sees a variable that points git at another repository. The seam
tests read the command line and environment handed to ``subprocess``; the
real-default tests have git itself report them.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from litman.core import git as git_mod
from litman.core.git import GitNotFoundError, output_tail, run_git
from tests._git_clone import make_upstream

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed; CI has it."
)


class _Recorder:
    """Stands in for ``subprocess.run`` and keeps what it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    fake = types.SimpleNamespace(
        run=recorder.run,
        CompletedProcess=subprocess.CompletedProcess,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(git_mod, "subprocess", fake)
    return recorder


def test_every_git_command_gets_the_low_speed_limits_and_no_prompt(
    recorded: _Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    run_git(["fetch", "origin"], repo=tmp_path / "repo", env={"GIT_OPTIONAL_LOCKS": "0"})

    [(command, kwargs)] = recorded.calls
    assert command == [
        "git",
        "-c", "http.lowSpeedLimit=1000",
        "-c", "http.lowSpeedTime=60",
        "-C", str(tmp_path / "repo"),
        "fetch", "origin",
    ]
    env = kwargs["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert kwargs["capture_output"] is True


def test_a_command_without_a_repository_runs_where_it_is_told(recorded: _Recorder) -> None:
    run_git(["clone", "--", "https://example.invalid/x.git", "dest"])

    [(command, _kwargs)] = recorded.calls
    assert command[:5] == ["git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60"]
    assert "-C" not in command


@needs_git
def test_git_itself_sees_the_low_speed_limits_and_no_prompt() -> None:
    """The real default: git reports the settings it runs with."""
    limit = run_git(["config", "--get", "http.lowSpeedLimit"])
    time_ = run_git(["config", "--get", "http.lowSpeedTime"])
    prompt = run_git(["-c", 'alias.prompt=!echo "[$GIT_TERMINAL_PROMPT]"', "prompt"])

    assert (limit.stdout.strip(), time_.stdout.strip()) == ("1000", "60")
    assert prompt.returncode == 0, prompt.stderr
    assert prompt.stdout.strip() == "[0]"


@needs_git
def test_git_answers_for_the_repository_it_is_given_whatever_git_dir_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_upstream(tmp_path / "repo")
    other = make_upstream(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    result = run_git(["rev-parse", "--absolute-git-dir"], repo=repo)

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == (repo / ".git").resolve()


def test_no_git_on_path_is_said_plainly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "no-programs"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(GitNotFoundError, match="`git` executable not found on PATH"):
        run_git(["--version"])


@needs_git
def test_a_streamed_clone_shows_git_progress_and_keeps_its_last_lines(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path / "upstream")
    shown = io.StringIO()

    done = run_git(
        ["clone", "--progress", "--", upstream.as_uri(), str(tmp_path / "copy")],
        progress=True,
        progress_to=shown,
    )
    failed = run_git(
        ["clone", "--progress", "--", (tmp_path / "missing").as_uri(), str(tmp_path / "x")],
        progress=True,
        progress_to=shown,
    )

    assert done.returncode == 0
    assert (tmp_path / "copy" / "README.md").is_file()
    assert "Cloning into" in shown.getvalue()
    assert failed.returncode != 0
    kept = output_tail(failed.stderr).splitlines()
    assert any(line.startswith("fatal:") for line in kept)
    assert all(line in shown.getvalue() for line in kept)


def test_the_tail_counts_progress_updates_as_lines() -> None:
    text = "Receiving objects:  50%\rReceiving objects: 100%, done.\n\nfatal: one\nfatal: two\n"

    assert output_tail(text, lines=2) == "fatal: one\nfatal: two"
    assert output_tail(text, lines=9).splitlines()[0] == "Receiving objects:  50%"
    assert output_tail(None) == ""
