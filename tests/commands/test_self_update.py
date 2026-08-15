"""Tests for ``lit self-update`` (task-self-update D3, AC4).

The three dispatch branches are covered:

* **uv** — ``_is_editable_install`` mocked False, ``uv tool list`` mocked to
  mention litman → ``uv tool upgrade litman`` is the subprocess run.
* **pipx** — same, but only ``pipx list`` mentions litman.
* **reject** — the editable/dev branch (the local conda env is a live editable
  install, so ``_is_editable_install`` is exercised for real elsewhere; here it
  is asserted via a mock for determinism) and the no-tool error branch.

Every subprocess (probe + upgrade) is mocked — no real uv/pipx/pip is invoked
and nothing is ever upgraded.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands import self_update as su
from litman.exceptions import SelfUpdateError


def _no_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(su, "_is_editable_install", lambda: False)
    # Stated explicitly rather than left to the ambient install: CI installs
    # litman editable, where `_install_origin` happens to answer None anyway.
    # A `pip install .` box would otherwise turn every test below red at once.
    monkeypatch.setattr(su, "_install_origin", lambda: None)


def _fake_which(present: set[str]):
    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return _which


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# where this litman came from (task-self-update-source-guard A1-A4)
# ---------------------------------------------------------------------------
#
# PEP 610 writes direct_url.json for anything installed from a direct URL and
# never for anything resolved from an index, so its shape is the whole basis
# for "can this install be upgraded to a release at all".


def _fake_distribution(
    read_text: Callable[[str], str | None],
) -> Callable[[str], object]:
    """Stand in for ``importlib.metadata.distribution`` with one metadata file."""

    class _Dist:
        def read_text(self, name: str) -> str | None:
            return read_text(name)

    return lambda name: _Dist()


def _unreadable(_name: str) -> str:
    raise OSError("distribution metadata is unreadable")


def test_install_origin_reads_git_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git install names the ref it is pinned to — that ref is exactly what
    stops resolving once a release branch is deleted."""
    monkeypatch.setattr(
        su,
        "_direct_url",
        lambda: {
            "url": "https://github.com/wqx1999/litman",
            "vcs_info": {
                "vcs": "git",
                "requested_revision": "dev/1.3.5",
                "commit_id": "b1ed0a4c0ffee1234567890abcdef1234567890a",
            },
        },
    )
    origin = su._install_origin()
    assert origin is not None
    assert "git" in origin
    assert "dev/1.3.5" in origin

    # Pinned at a bare commit: no branch or tag name to quote, so the short hash.
    monkeypatch.setattr(
        su,
        "_direct_url",
        lambda: {
            "url": "https://github.com/wqx1999/litman",
            "vcs_info": {"vcs": "git", "commit_id": "b1ed0a4c0ffee1234"},
        },
    )
    assert su._install_origin() == "git (b1ed0a4)"

    # Neither a revision nor a commit: the vcs name alone, no empty parentheses.
    monkeypatch.setattr(su, "_direct_url", lambda: {"vcs_info": {"vcs": "git"}})
    assert su._install_origin() == "git"


def test_install_origin_none_for_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Editable is not reported as an origin: it has its own branch upstream of
    this one, with its own hint."""
    monkeypatch.setattr(
        su,
        "_direct_url",
        lambda: {"dir_info": {"editable": True}, "url": "file:///src/litman"},
    )
    assert su._install_origin() is None
    assert su._is_editable_install() is True


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "file:///w/litman-1.3.5.whl", "archive_info": {"hash": "sha256=beef"}},
        {"url": "file:///w/litman", "dir_info": {}},
        {"url": "file:///w/litman"},  # unrecognized, but still not from an index
    ],
)
def test_install_origin_reports_local_file(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    monkeypatch.setattr(su, "_direct_url", lambda: payload)
    assert su._install_origin() == "a local file"


@pytest.mark.parametrize(
    "read_text",
    [
        lambda _name: None,  # the distribution carries no such file
        lambda _name: "",  # present but empty
        lambda _name: "{ not json",  # present but unparsable
        lambda _name: "[]",  # parsable, but not an object
        _unreadable,  # the metadata cannot be read at all
    ],
)
def test_missing_direct_url_leaves_editable_probe_unchanged(
    monkeypatch: pytest.MonkeyPatch, read_text: Callable[[str], str | None]
) -> None:
    """No usable ``direct_url.json`` is what an index install looks like: no
    origin to report, and the editable probe answers exactly as it did before
    the two readers were split apart."""
    monkeypatch.setattr("importlib.metadata.distribution", _fake_distribution(read_text))
    assert su._direct_url() is None
    assert su._is_editable_install() is False
    assert su._install_origin() is None


@pytest.mark.parametrize("raw", ['{"dir_info": "x"}', '{"dir_info": null}'])
def test_malformed_dir_info_never_escapes_the_editable_probe(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A ``dir_info`` that is not an object has to stay a quiet False.

    These are the only two payload shapes where the probe could have changed
    meaning: ``.get("editable")`` on a str / None raises ``AttributeError``,
    which the probe's own ``except Exception`` used to swallow. That ``try``
    now sits one level down in ``_direct_url``, so the probe has to be total by
    itself — simplify it back to a bare ``.get("dir_info", {}).get(...)`` and
    ``lit self-update`` dies with a traceback on exactly these payloads, with
    nothing else in the suite noticing.

    Driven through the real ``_direct_url`` seam, since the divergence would be
    in how the two readers split the work between them.
    """
    monkeypatch.setattr(
        "importlib.metadata.distribution", _fake_distribution(lambda _name: raw)
    )
    assert su._direct_url() is not None  # the payload IS readable; dir_info is junk
    assert su._is_editable_install() is False
    # Still installed from a direct URL, so still refused rather than promised.
    assert su._install_origin() == "a local file"


# ---------------------------------------------------------------------------
# reject branch (AC4)
# ---------------------------------------------------------------------------


def test_editable_install_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Editable/dev install → manual hint, NO upgrade attempted."""
    monkeypatch.setattr(su, "_is_editable_install", lambda: True)

    ran: list[list[str]] = []
    monkeypatch.setattr(
        su.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _completed()
    )

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert "editable" in result.output.lower()
    assert ran == []  # never shelled out to any upgrade


def test_no_tool_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not editable, neither uv nor pipx on PATH → error exit with manual cmd."""
    _no_editable(monkeypatch)
    monkeypatch.setattr(su.shutil, "which", _fake_which(set()))

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SelfUpdateError)
    assert "Neither uv nor pipx" in str(result.exception)


def test_tool_present_but_not_managing_litman(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv/pipx exist but neither lists litman → manual hint, no upgrade."""
    _no_editable(monkeypatch)
    monkeypatch.setattr(su.shutil, "which", _fake_which({"uv", "pipx"}))
    monkeypatch.setattr(
        su, "_run_capture", lambda cmd, **kw: _completed(stdout="something-else 1.0\n")
    )

    ran: list[list[str]] = []
    monkeypatch.setattr(
        su.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _completed()
    )

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert "not installed via uv or pipx" in result.output
    assert ran == []


def _wire_owned_by(
    monkeypatch: pytest.MonkeyPatch, *, installer: str, origin: str
) -> dict[str, list[object]]:
    """Wire a litman that ``installer`` owns and ``origin`` describes.

    Captures everything the refusal has to prevent: the in-process upgrade, the
    detached helper, and the PyPI version lookup that makes the promise. No
    platform is faked — the refusal lands ahead of every platform branch, so
    this drives the same code on all three.
    """
    monkeypatch.setattr(su, "_is_editable_install", lambda: False)
    monkeypatch.setattr(su, "_install_origin", lambda: origin)
    monkeypatch.setattr(su.shutil, "which", _fake_which({installer}))
    monkeypatch.setattr(
        su, "_run_capture", lambda cmd, **kw: _completed(stdout="litman 1.1.0\n")
    )
    monkeypatch.setattr(su.launcher_stubs, "repair_default", lambda: [])

    seen: dict[str, list[object]] = {"ran": [], "spawned": [], "fetched": []}
    monkeypatch.setattr(
        su.subprocess, "run", lambda cmd, **kw: seen["ran"].append(cmd) or _completed()
    )
    monkeypatch.setattr(
        su.self_update_helper,
        "write_and_spawn_helper",
        lambda **kw: seen["spawned"].append(kw),
    )
    monkeypatch.setattr(
        su.update_check,
        "_fetch_latest_version",
        lambda **kw: seen["fetched"].append(1) or "9.9.9",
    )
    return seen


def test_git_install_refuses_before_promising(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv re-resolves the git ref it recorded, never the release the version
    check reads off PyPI — so say that up front instead of announcing an
    upgrade and failing where only a log file can see it."""
    seen = _wire_owned_by(monkeypatch, installer="uv", origin="git (dev/1.3.5)")

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    # Rich hard-wraps to the console width, so compare on normalised spacing.
    said = " ".join(result.output.split())
    assert "installed from git (dev/1.3.5), not from a release" in said
    # The way out is its own sentence on its own line, not an aside tacked to
    # the verdict — one judgement, one exit, per the message budget.
    assert "not from a release. Reinstall it:" in said
    assert "uv tool uninstall litman && uv tool install litman" in said

    # The promise is not made, rather than made and broken: no version was
    # fetched, so no `current X -> Y` line could be printed.
    assert seen["fetched"] == []
    assert "current" not in said
    assert "9.9.9" not in said

    # And nothing was started — not here, and not after this process exits.
    assert seen["ran"] == []
    assert seen["spawned"] == []


def test_pipx_install_gets_the_pipx_reinstall_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The way out has to be runnable on the machine reading it: the command
    names the installer that actually owns this litman."""
    seen = _wire_owned_by(monkeypatch, installer="pipx", origin="a local file")

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert "installed from a local file, not from a release" in said
    assert "pipx uninstall litman && pipx install litman" in said
    assert "uv tool" not in said
    assert seen["ran"] == []
    assert seen["spawned"] == []


@pytest.mark.parametrize(
    "origin",
    [
        "hg (feat/[wip])",  # a balanced span: Rich eats it, the name loses a word
        "hg (x[/])",  # an unbalanced close: Rich raises MarkupError
    ],
)
def test_a_bracketed_revision_is_not_read_as_markup(
    monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    """The revision comes out of the distribution's own metadata, and this is
    the first place that data reaches the console.

    Rich would take a bracket in it for a style span — the name silently loses
    a piece, or the command dies on a MarkupError, which would be a traceback
    in place of the very message this guard exists to print. Git refnames
    cannot carry a bracket, but hg / bzr / svn revisions can.
    """
    seen = _wire_owned_by(monkeypatch, installer="uv", origin=origin)

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.split())
    assert f"installed from {origin}, not from a release" in said
    assert seen["ran"] == []


# ---------------------------------------------------------------------------
# uv / pipx dispatch
# ---------------------------------------------------------------------------


def _wire_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: str,
    upgrade_rc: int = 0,
) -> list[list[str]]:
    """Wire probes so ``manager`` (uv|pipx) manages litman; capture upgrade cmds.

    Returns the list the mocked ``subprocess.run`` appends each invoked command
    to (the upgrade + the post-verify ``lit --version``).

    Pins the in-process (non-win32) arm, because that is the arm every caller
    of this helper is about: on win32 the upgrade is handed to a detached
    helper and ``subprocess.run`` is never reached, so on a Windows host these
    tests were asserting against a branch that had not run. The two win32
    tests re-pin the platform *after* calling this, which still wins.
    """
    monkeypatch.setattr(su.sys, "platform", "linux")
    _no_editable(monkeypatch)
    monkeypatch.setattr(su.shutil, "which", _fake_which({"uv", "pipx"}))

    def _run_capture(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["uv", "tool"] and cmd[2] == "list":
            return _completed(stdout="litman v1.1.0\n" if manager == "uv" else "other\n")
        if cmd[:2] == ["pipx", "list"]:
            return _completed(stdout="package litman 1.1.0\n" if manager == "pipx" else "other\n")
        if cmd[:2] == ["lit", "--version"]:
            return _completed(stdout="lit, version 9.9.9\n")
        return _completed(stdout="")

    monkeypatch.setattr(su, "_run_capture", _run_capture)
    monkeypatch.setattr(su.update_check, "_fetch_latest_version", lambda **kw: "9.9.9")

    ran: list[list[str]] = []

    def _run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        return _completed(returncode=upgrade_rc)

    monkeypatch.setattr(su.subprocess, "run", _run)
    return ran


def test_uv_branch_runs_uv_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _wire_dispatch(monkeypatch, manager="uv")
    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert ["uv", "tool", "upgrade", "litman"] in ran
    assert "9.9.9" in result.output


def test_pipx_branch_runs_pipx_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _wire_dispatch(monkeypatch, manager="pipx")
    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert ["pipx", "upgrade", "litman"] in ran


def test_uv_preferred_over_pipx(monkeypatch: pytest.MonkeyPatch) -> None:
    """When BOTH list litman, uv is chosen (probe order)."""
    monkeypatch.setattr(su.sys, "platform", "linux")  # in-process arm; see _wire_dispatch
    _no_editable(monkeypatch)
    monkeypatch.setattr(su.shutil, "which", _fake_which({"uv", "pipx"}))
    monkeypatch.setattr(
        su, "_run_capture", lambda cmd, **kw: _completed(stdout="litman 1.1.0\n")
    )
    monkeypatch.setattr(su.update_check, "_fetch_latest_version", lambda **kw: "9.9.9")

    ran: list[list[str]] = []
    monkeypatch.setattr(
        su.subprocess, "run", lambda cmd, **kw: ran.append(cmd) or _completed()
    )

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert ["uv", "tool", "upgrade", "litman"] in ran
    assert ["pipx", "upgrade", "litman"] not in ran


def test_upgrade_nonzero_exit_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_dispatch(monkeypatch, manager="uv", upgrade_rc=3)
    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SelfUpdateError)
    assert "exited with code 3" in str(result.exception)


def test_confirm_abort_skips_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without -y, a 'no' answer aborts before any upgrade subprocess."""
    ran = _wire_dispatch(monkeypatch, manager="uv")
    result = CliRunner().invoke(cli, ["self-update"], input="n\n")
    assert result.exit_code != 0  # click abort
    assert ran == []


# ---------------------------------------------------------------------------
# Windows: the upgrade is handed to the detached helper
# ---------------------------------------------------------------------------
#
# Windows locks the launcher stub this very command runs from — it can neither
# be overwritten nor renamed aside, so an in-process `uv tool upgrade` always
# dies with os error 32. The whole point of the win32 branch is that the
# upgrade subprocess NEVER runs here; it runs after this process is gone.


def _wire_windows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Pretend win32 and capture what gets handed to the detached helper."""
    monkeypatch.setattr(su.sys, "platform", "win32")
    monkeypatch.setattr(
        su.launcher_stubs, "installed_stubs", lambda: ["lit.exe", "litw.exe"]
    )
    monkeypatch.setattr(su.launcher_stubs, "repair_default", lambda: [])

    spawned: list[dict[str, object]] = []

    def _spawn(**kwargs: object) -> object:
        spawned.append(kwargs)
        return "/tmp/helper.bat"

    monkeypatch.setattr(su.self_update_helper, "write_and_spawn_helper", _spawn)
    return spawned


def test_windows_hands_the_upgrade_to_the_detached_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = _wire_dispatch(monkeypatch, manager="uv")
    spawned = _wire_windows(monkeypatch)

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    # The upgrade did NOT run in this process — that is the entire fix.
    assert ["uv", "tool", "upgrade", "litman"] not in ran
    [call] = spawned
    assert call["upgrade_cmd"] == ["/usr/bin/uv", "tool", "upgrade", "litman"]
    assert call["stub_paths"] == ["lit.exe", "litw.exe"]
    assert call["pid"] == su.os.getpid()
    # No relaunch: a terminal command that respawns a terminal is a surprise.
    assert not call.get("relaunch_cmd")
    assert "lit --version" in result.output


def test_windows_helper_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A helper that cannot be spawned must fail loudly, not pretend success —
    the user would otherwise wait forever for an upgrade nobody started."""
    _wire_dispatch(monkeypatch, manager="uv")
    monkeypatch.setattr(su.sys, "platform", "win32")
    monkeypatch.setattr(su.launcher_stubs, "installed_stubs", lambda: [])
    monkeypatch.setattr(su.launcher_stubs, "repair_default", lambda: [])

    def _boom(**kwargs: object) -> object:
        raise OSError("no temp dir")

    monkeypatch.setattr(su.self_update_helper, "write_and_spawn_helper", _boom)

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 1
    assert isinstance(result.exception, SelfUpdateError)
    assert "upgrade helper" in str(result.exception)


def test_posix_upgrades_in_process_without_the_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detour is Windows-only: POSIX keeps the synchronous upgrade, whose
    live installer output is the better experience."""
    ran = _wire_dispatch(monkeypatch, manager="uv")
    monkeypatch.setattr(su.sys, "platform", "linux")
    spawned: list[object] = []
    monkeypatch.setattr(
        su.self_update_helper,
        "write_and_spawn_helper",
        lambda **kw: spawned.append(kw),
    )

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert ["uv", "tool", "upgrade", "litman"] in ran
    assert spawned == []


def test_missing_launcher_is_healed_before_upgrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A previous half-failed upgrade can leave the venv current but a stub
    gone; heal it on the way in, and say so."""
    _wire_dispatch(monkeypatch, manager="uv")
    monkeypatch.setattr(su.launcher_stubs, "repair_default", lambda: ["litw.exe"])

    result = CliRunner().invoke(cli, ["self-update", "-y"])
    assert result.exit_code == 0, result.output
    assert "restored missing launcher litw.exe" in result.output
