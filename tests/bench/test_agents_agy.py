"""Deterministic tests for the agy adapter — the shim is RUN, not mocked.

agy emits no event stream at all, so there is no recording to parse and no fixture
to hand it: its evidence comes from a ``lit`` PATH shim the harness generates. The
shim is therefore the thing under test, and a mocked shim would prove nothing —
these tests write the real script and execute it against a stand-in ``lit``, then
read the log back through the real adapter.

NEVER spawns an agent (M34 §3.5 hard boundary): the only subprocess here is the
generated shim itself, wrapping a fake `lit`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.agents import NOT_MEASURABLE, get_adapter
from harness.agents.agy import (
    LIT_CALLS_FILENAME,
    TOKEN_RELPATH,
    AgyAdapter,
    read_lit_calls,
    resolve_lit_bin,
    seed_auth,
    write_lit_shim,
)

# A FABRICATED token (this repo is public; no real credential ever enters a
# fixture). Shape mirrors the real file: auth_method + a token block.
_FAKE_TOKEN = json.dumps({
    "auth_method": "oauth",
    "token": {
        "access_token": "fake-access-token-for-tests",
        "refresh_token": "fake-refresh-token-for-tests",
        "expiry": "2000-01-01T00:00:00Z",
        "token_type": "Bearer",
    },
})


def _fake_user_home(tmp_path: Path, monkeypatch, *, logged_in: bool = True) -> Path:
    """A stand-in user HOME. `Path.home()` follows $HOME on POSIX, so the adapter
    reads the fake token — never the user's real credential file."""
    user_home = tmp_path / "userhome"
    user_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(user_home))
    if logged_in:
        token = user_home / TOKEN_RELPATH
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(_FAKE_TOKEN, encoding="utf-8")
        token.chmod(0o600)
    return user_home


def _fake_lit(tmp_path: Path, *, stdout: str = "lit, version 1.2.0\n", code: int = 0) -> Path:
    """A stand-in `lit` the shim will wrap."""
    lit = tmp_path / "realbin" / "lit"
    lit.parent.mkdir(parents=True, exist_ok=True)
    lit.write_text(
        "#!/bin/sh\n"
        # %b so the JSON-escaped \n in the literal becomes a real newline.
        f"printf '%b' {json.dumps(stdout)}\n"
        'printf "%s" "err:$*" >&2\n'
        f"exit {code}\n",
        encoding="utf-8",
    )
    lit.chmod(0o755)
    return lit


# ---------------------------------------------------------------------------
# The shim: run it for real
# ---------------------------------------------------------------------------


def test_shim_logs_the_call_and_passes_everything_through(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("harness.agents.agy.LIT_BIN", _fake_lit(tmp_path))
    base = tmp_path / "bench-x"
    base.mkdir()
    shim_dir = write_lit_shim(base)

    proc = subprocess.run(
        [str(shim_dir / "lit"), "list", "--format", "json"],
        capture_output=True,
        text=True,
    )

    # (a) the real lit's stdout / stderr / exit code reach the caller untouched:
    # the agent must not be able to tell the shim is there.
    assert proc.stdout == "lit, version 1.2.0\n"
    assert proc.stderr == "err:list --format json"
    assert proc.returncode == 0

    # (b) and the call was recorded.
    records = read_lit_calls(base)
    assert len(records) == 1
    assert records[0] == {
        "argv": ["list", "--format", "json"],
        "raw": "lit list --format json",
        "stdout": "lit, version 1.2.0\n",
        "stderr": "err:list --format json",
        "exit_code": 0,
    }


def test_shim_passes_through_a_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("harness.agents.agy.LIT_BIN", _fake_lit(tmp_path, code=3))
    base = tmp_path / "bench-y"
    base.mkdir()
    shim_dir = write_lit_shim(base)

    proc = subprocess.run([str(shim_dir / "lit"), "show", "nope"], capture_output=True, text=True)
    assert proc.returncode == 3
    assert read_lit_calls(base)[0]["exit_code"] == 3


def test_shim_appends_one_record_per_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("harness.agents.agy.LIT_BIN", _fake_lit(tmp_path))
    base = tmp_path / "bench-z"
    base.mkdir()
    shim_dir = write_lit_shim(base)

    subprocess.run([str(shim_dir / "lit"), "list"], capture_output=True, text=True)
    subprocess.run([str(shim_dir / "lit"), "show", "x"], capture_output=True, text=True)

    assert [r["argv"] for r in read_lit_calls(base)] == [["list"], ["show", "x"]]


def test_shim_freezes_the_real_lit_path_instead_of_searching_path(
    tmp_path: Path, monkeypatch
) -> None:
    """The shim is FIRST on PATH under its own name. If it resolved `lit` at run
    time it would find itself and recurse until the stack blew."""
    lit = _fake_lit(tmp_path)
    monkeypatch.setattr("harness.agents.agy.LIT_BIN", lit)
    base = tmp_path / "bench-w"
    base.mkdir()
    shim_dir = write_lit_shim(base)

    body = (shim_dir / "lit").read_text(encoding="utf-8")
    assert json.dumps(str(lit)) in body

    # Prove it: run the shim with its own dir first on PATH.
    env = {"PATH": f"{shim_dir}:/usr/bin:/bin"}
    proc = subprocess.run(
        [str(shim_dir / "lit"), "list"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0
    assert proc.stdout == "lit, version 1.2.0\n"


def test_resolve_lit_bin_refuses_an_unresolvable_relative_binary(monkeypatch) -> None:
    monkeypatch.setattr("harness.agents.agy.LIT_BIN", Path("definitely-not-on-path-xyz"))
    with pytest.raises(RuntimeError, match="absolute path"):
        resolve_lit_bin()


# ---------------------------------------------------------------------------
# parse: prose in, shim log out
# ---------------------------------------------------------------------------


def test_parse_folds_the_shim_log_into_the_neutral_result(tmp_path: Path) -> None:
    base = tmp_path / "bench-p"
    base.mkdir()
    (base / LIT_CALLS_FILENAME).write_text(
        json.dumps({
            "argv": ["list", "--status", "inbox"],
            "raw": "lit list --status inbox",
            "stdout": "#4 PeptideBERT\n",
            "stderr": "",
            "exit_code": 0,
        }) + "\n",
        encoding="utf-8",
    )

    result = AgyAdapter().parse("The library has one paper.\n", base=base)

    assert result.final_text == "The library has one paper."
    assert [c.argv for c in result.lit_calls] == [["list", "--status", "inbox"]]
    # Paired the same way an event stream's tool_use_id would pair them.
    assert result.as_jsonl_records() == [
        {"argv": ["list", "--status", "inbox"], "raw": "lit list --status inbox",
         "stdout": "#4 PeptideBERT\n"}
    ]


def test_parse_reports_absent_axes_as_absent_never_as_zero(tmp_path: Path) -> None:
    base = tmp_path / "bench-q"
    base.mkdir()
    result = AgyAdapter().parse("done", base=base)

    # No counters exist -> {} ("not observed"), which surfaces as None in the
    # report. A dict of zeros would be a measurement we never made.
    assert result.usage == {}
    assert result.model_served is None
    assert result.skills == []
    assert result.lit_calls == []  # no shim log = the agent ran no lit


def test_read_lit_calls_is_empty_when_the_agent_ran_no_lit(tmp_path: Path) -> None:
    assert read_lit_calls(tmp_path) == []


# ---------------------------------------------------------------------------
# argv order + capabilities + the token seed
# ---------------------------------------------------------------------------


def test_build_argv_keeps_p_last() -> None:
    """`agy -p --model X "prompt"` swallows --model into the prompt. Order is
    load-bearing, not cosmetic."""
    argv = AgyAdapter().build_argv(
        "do a thing", model="Claude Sonnet 4.6 (Thinking)", cwd=Path("/x/cwd")
    )
    assert argv[-2:] == ["-p", "do a thing"]
    assert argv[1] == "--dangerously-skip-permissions"


def test_build_argv_relocates_the_bash_tool_before_p() -> None:
    """agy's bash tool runs in a HOME scratch, not the process cwd; `--add-dir`
    pins it to the neutral cwd. It MUST land before `-p` (which must stay last,
    or --model gets swallowed into the prompt)."""
    argv = AgyAdapter().build_argv(
        "do a thing", model="Claude Sonnet 4.6 (Thinking)", cwd=Path("/x/cwd")
    )
    i = argv.index("--add-dir")
    assert argv[i : i + 2] == ["--add-dir", "/x/cwd"]
    assert argv[-2] == "-p"
    assert argv[-1] == "do a thing"


def test_model_with_spaces_and_parens_stays_one_argv_element() -> None:
    """The model name is a display name; it never goes through a shell."""
    argv = AgyAdapter().build_argv(
        "x", model="Claude Sonnet 4.6 (Thinking)", cwd=Path("/x/cwd")
    )
    assert "Claude Sonnet 4.6 (Thinking)" in argv


def test_capabilities_declare_three_axes_unmeasurable() -> None:
    caps = get_adapter("agy").capabilities
    assert (caps.tokens, caps.turns, caps.served_model, caps.routing) == (
        False, False, False, False,
    )


def test_permission_flag_is_recorded_verbatim() -> None:
    assert AgyAdapter().permission_flags == ("--dangerously-skip-permissions",)


def test_seed_auth_raises_with_login_instructions_when_not_logged_in(
    tmp_path: Path, monkeypatch
) -> None:
    """Unlike claude's silent skip, this MUST raise: agy with no credential falls
    back to a browser OAuth flow and hangs, so a skip here would surface as Phase
    0's probe timing out (240s of nothing) instead of a one-line fix."""
    _fake_user_home(tmp_path, monkeypatch, logged_in=False)
    with pytest.raises(RuntimeError) as e:
        seed_auth(tmp_path / "bench-home")
    msg = str(e.value)
    assert "not logged in" in msg
    assert "agy" in msg and "once" in msg  # tells the user how to fix it
    assert "never performs a login" in msg


def test_prepare_rejects_proxy_flags_it_cannot_honor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Anthropic-compatible proxy mode"):
        AgyAdapter().prepare(
            tmp_path, run_vault=tmp_path / "vault", base_url="https://proxy.example/v1"
        )


def test_prepare_builds_a_fresh_home_seeds_the_token_and_shims_path(
    tmp_path: Path, monkeypatch
) -> None:
    user_home = _fake_user_home(tmp_path, monkeypatch)
    monkeypatch.setattr("harness.agents.agy.LIT_BIN", _fake_lit(tmp_path))
    installed: list[Path] = []
    monkeypatch.setattr(
        "harness.agents.agy.install_repo_skills", lambda d, **k: installed.append(Path(d))
    )

    base = tmp_path / "bench-r"
    base.mkdir()
    env = AgyAdapter().prepare(base, run_vault=base / "vault")

    home = base / "home"
    assert env["HOME"] == str(home)
    # The token — and ONLY the token — was seeded, 0600 preserved (copy2).
    seeded = home / TOKEN_RELPATH
    assert seeded.read_text(encoding="utf-8") == _FAKE_TOKEN
    assert (seeded.stat().st_mode & 0o777) == 0o600
    assert [p for p in home.rglob("*") if p.is_file()] == [seeded]
    # The user's real token file is never touched (agy refreshes the bench copy).
    assert (user_home / TOKEN_RELPATH).read_text(encoding="utf-8") == _FAKE_TOKEN
    # The shim dir is FIRST on PATH, or the agent's bare `lit` misses it entirely.
    assert env["PATH"].startswith(f"{base / 'shim'}:")
    # Skills go where agy actually looks — NOT ~/.agents/skills, which it ignores.
    assert installed == [home / ".gemini" / "antigravity-cli" / "skills"]


def test_prepare_raises_before_spawning_anything_when_not_logged_in(
    tmp_path: Path, monkeypatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch, logged_in=False)
    base = tmp_path / "bench-s"
    base.mkdir()
    with pytest.raises(RuntimeError, match="not logged in"):
        AgyAdapter().prepare(base, run_vault=base / "vault")


# ---------------------------------------------------------------------------
# The routing axis: not measurable is NOT a miss
# ---------------------------------------------------------------------------


def test_observe_skill_returns_not_measurable_without_spawning(tmp_path: Path) -> None:
    """The most insidious pitfall in the whole design.

    `observe_skill_for_utterance` already spends `None` on "the agent fired no
    skill" — a routing MISS that lands in the RA denominator. agy's "there is no
    skill-activation signal to read" is a different thing entirely, and returning
    `None` for it would report agy's RA as a confident 0.0 rather than as unknown.

    It also must not spawn: the capability is known up front, so ~14 classification
    spawns per routing card would buy nothing.
    """
    from harness import executor as executor_mod

    def explode(*a, **k):  # pragma: no cover - asserts it is never reached
        raise AssertionError("must not spawn for an agent whose RA is unmeasurable")

    original = executor_mod.run_card
    executor_mod.run_card = explode  # type: ignore[assignment]
    try:
        observed = executor_mod.observe_skill_for_utterance(
            "把这篇加到库里", tmp_path, fixtures_pdfs_dir=tmp_path, agent="agy"
        )
    finally:
        executor_mod.run_card = original  # type: ignore[assignment]

    assert observed is NOT_MEASURABLE
    assert observed is not None  # the whole point
