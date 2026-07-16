"""Tests for the claude adapter: its served-model read + its isolation seam.

Why this file exists: ``_MODEL_FAMILY`` maps ``"claude-sonnet-4-6"`` on the
assumption that claude's ``system/init`` echoes the model id we asked for rather
than resolving it to a dated one (``claude-sonnet-4-6-20260514``-style). That
assumption was carried by nobody but a comment. If it had been wrong, check 6
would have compared a requested id against a dated served id, found no family for
the latter, and failed every claude run at the gate — the exact "looks like a
result, is actually an instrument fault" class this bench is built to refuse.

Provenance of ``claude-init-model.raw.jsonl``: the maintainer recorded a real
``claude -p --model claude-sonnet-4-6 --output-format stream-json --verbose`` init
line on this machine (2026-07-16) and reported its contents; the assumption holds
— the id comes back VERBATIM. The fixture pins that string. Everything else in the
line is redacted, because a real claude init event carries ``cwd``,
``memory_paths``, ``skills``, ``slash_commands``, ``plugins``, ``agents``,
``session_id`` and ``uuid`` — i.e. the recording user's home paths and private
skill/plugin names — and this repo is public. Same rule as the three cursor
recordings alongside it: keep the field under test, redact the rest.

NEVER spawns (M34 §3.5 hard boundary).
"""

from __future__ import annotations

from pathlib import Path

from harness.agents import family_of, get_adapter
from harness.agents.claude import ClaudeAdapter, parse_stream

STREAMS_DIR = Path(__file__).resolve().parent / "fixtures" / "agent-streams"


def _init_line() -> list[str]:
    return (STREAMS_DIR / "claude-init-model.raw.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()


def test_claude_reports_the_requested_id_verbatim() -> None:
    """The load-bearing fact: NOT resolved to a dated id."""
    result = parse_stream(_init_line())
    assert result.model_served == "claude-sonnet-4-6"


def test_the_served_string_is_the_one_the_family_table_maps() -> None:
    """Pins the table against the real string. If claude ever starts resolving ids,
    this fails here — in a unit test — instead of at the gate of a live run."""
    served = parse_stream(_init_line()).model_served
    assert family_of(served) == "claude-sonnet-4.6"
    # And it is exactly what we would have requested, so check 6's exact-match
    # branch carries claude without the table being involved at all.
    assert served == ClaudeAdapter().default_model


def test_an_init_without_a_model_leaves_served_none() -> None:
    """Absent -> None ("not observed"), never a guess from what we requested."""
    result = parse_stream(['{"type":"system","subtype":"init","session_id":"x"}'])
    assert result.model_served is None


def test_reading_the_served_model_disturbs_nothing_else() -> None:
    """model_served was added to a path with a live TRR/RA baseline. It must be
    purely additive: an init-only stream yields no skills, no calls, no usage."""
    result = parse_stream(_init_line())
    assert result.skills == []
    assert result.lit_calls == []
    assert result.tool_results == []
    assert result.usage == {}
    assert result.final_text == ""


def test_claude_capabilities_declare_the_served_model() -> None:
    assert get_adapter("claude").capabilities.served_model is True


def test_the_fixture_carries_no_home_paths_or_private_names() -> None:
    """This repo is public and the recording was made against a real home.

    Guards the sanitization itself: every field a claude init line carries besides
    the model is either absent or redacted. A future re-record that pastes the raw
    line in fails here rather than in a git history nobody can rewrite.
    """
    import json

    event = json.loads((STREAMS_DIR / "claude-init-model.raw.jsonl").read_text(encoding="utf-8"))
    leaky = ("cwd", "memory_paths", "skills", "slash_commands", "plugins", "agents",
             "session_id", "uuid")
    for field in leaky:
        if field in event:
            assert str(event[field]).startswith("<redacted:"), (
                f"{field!r} is not redacted: a real claude init line leaks the "
                f"user's home paths and private skill names into a PUBLIC repo"
            )
    # The one field that must survive verbatim.
    assert event["model"] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# The isolation seam: HOME redirect (still never spawns)
# ---------------------------------------------------------------------------


def _fake_user_home(tmp_path: Path, monkeypatch) -> Path:
    """A stand-in for the user's real HOME.

    ``Path.home()`` follows ``$HOME`` on POSIX, so this redirects what the ADAPTER
    reads as "the real home" — the credential source — away from the machine's
    actual one. Nothing in this file may touch the maintainer's own ``~/.claude``.

    Sufficient ONLY in combination with conftest's autouse ``_no_real_credential_dirs``,
    which clears ``$CLAUDE_CONFIG_DIR``: ``_real_config_dir()`` reads that var
    BEFORE falling back to ``$HOME``, so with it exported this redirect is bypassed
    and ``seed_auth`` reads the developer's real config dir. ``$HOME`` alone does
    not isolate this adapter.
    """
    user_home = tmp_path / "userhome"
    user_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(user_home))
    return user_home


def _stub_skill_install(monkeypatch) -> list[Path]:
    installed: list[Path] = []
    monkeypatch.setattr(
        "harness.agents.claude.install_repo_skills",
        lambda d, **k: installed.append(Path(d)),
    )
    return installed


def test_prepare_redirects_home_and_drops_xdg_config_home(
    tmp_path: Path, monkeypatch
) -> None:
    """The seam this adapter used to lack. CLAUDE_CONFIG_DIR re-homes the config
    dir only; everything claude reads straight from $HOME (the user's installed
    skills, and whatever else lives beside the config dir) stayed visible without
    this. XDG_CONFIG_HOME is dropped for the reason cursor documents: set, it
    names the real ~/.config by absolute path and survives a HOME change."""
    _fake_user_home(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "userhome" / ".config"))
    _stub_skill_install(monkeypatch)

    env = ClaudeAdapter().prepare(tmp_path, run_vault=tmp_path / "vault")

    assert env["HOME"] == str(tmp_path / "home")
    assert "XDG_CONFIG_HOME" not in env
    # The config dir keeps doing its own job — the HOME redirect is additive.
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-config")
    assert env["LIT_LIBRARY"] == str(tmp_path / "vault")
    assert env["LITMAN_REGISTRY_DIR"] == str(tmp_path / "claude-registry")


def test_the_isolated_home_is_created_and_holds_no_skills(
    tmp_path: Path, monkeypatch
) -> None:
    """The run's HOME exists (claude must be able to write into it) and is empty
    of skills: the repo-source copies go to CLAUDE_CONFIG_DIR/skills, so a skills
    dir appearing under HOME would mean a second, unmanaged candidate set."""
    _fake_user_home(tmp_path, monkeypatch)
    installed = _stub_skill_install(monkeypatch)

    ClaudeAdapter().prepare(tmp_path, run_vault=tmp_path / "vault")

    assert (tmp_path / "home").is_dir()
    assert installed == [tmp_path / "claude-config" / "skills"]
    assert not (tmp_path / "home" / ".claude").exists()
    assert not (tmp_path / "home" / ".agents").exists()


def test_the_credential_still_seeds_from_the_configured_source(
    tmp_path: Path, monkeypatch
) -> None:
    """The pit the HOME redirect could have fallen into, pinned.

    ``seed_auth`` runs in the HARNESS process and resolves its source there
    (``_real_config_dir()``: ``$CLAUDE_CONFIG_DIR``, else ``Path.home()/.claude``),
    while the redirect only ever lands in the CHILD's env dict — so the credential
    is still found and copied. Had it read the child's HOME instead, every claude
    run would have started logged out, which looks like a broken agent rather than
    a broken seam.

    Compared by HASH, never by content: this test's whole subject is a credential
    file, and an assertion on the text would print whatever it actually found into
    a failure diff — in a public repo, on the exact code path whose failure mode is
    "read the developer's real token instead". The hash also makes the leak LOUD:
    if a real credential is ever seeded here, the digests differ and the test fails
    rather than passing on a file that merely exists.
    """
    import hashlib

    user_home = _fake_user_home(tmp_path, monkeypatch)
    _stub_skill_install(monkeypatch)
    cred = user_home / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True)
    # Fabricated, never a real token — this repo is public.
    cred.write_bytes(b'{"fake": "not-a-real-token"}')

    env = ClaudeAdapter().prepare(tmp_path, run_vault=tmp_path / "vault")

    seeded = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    assert seeded.is_file(), "the isolated config dir lost the login"
    assert (
        hashlib.sha256(seeded.read_bytes()).hexdigest()
        == hashlib.sha256(cred.read_bytes()).hexdigest()
    ), "the seeded credential is not the fabricated one this test planted"
    # And it went to the config dir, NOT into the fake HOME.
    assert not (Path(env["HOME"]) / ".claude" / ".credentials.json").exists()


def test_a_set_claude_config_dir_would_bypass_a_faked_home(
    tmp_path: Path, monkeypatch
) -> None:
    """Pins the seam conftest's autouse fixture exists to close.

    ``_real_config_dir()`` reads ``$CLAUDE_CONFIG_DIR`` before ``$HOME``, so a test
    that fakes only ``$HOME`` reads the developer's real config dir whenever that
    (documented, supported) var is exported — and ``seed_auth`` then copies a real
    credential into ``tmp_path`` while the test still passes. Asserting the
    precedence here means the fixture cannot be deleted without a red test.
    """
    from harness.agents.claude import _real_config_dir

    _fake_user_home(tmp_path, monkeypatch)
    assert _real_config_dir() == tmp_path / "userhome" / ".claude"

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert _real_config_dir() == tmp_path / "elsewhere", (
        "CLAUDE_CONFIG_DIR no longer wins over HOME — if the precedence flipped, "
        "conftest's _no_real_credential_dirs may be guarding the wrong var"
    )


def test_external_mode_skips_the_credential_but_still_redirects_home(
    tmp_path: Path, monkeypatch
) -> None:
    """Proxy mode authenticates via ANTHROPIC_BASE_URL + token, so no OAuth
    credential is copied — the isolation seam is orthogonal to that and holds."""
    user_home = _fake_user_home(tmp_path, monkeypatch)
    _stub_skill_install(monkeypatch)
    cred = user_home / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True)
    cred.write_text('{"fake": "not-a-real-token"}', encoding="utf-8")

    env = ClaudeAdapter().prepare(
        tmp_path,
        run_vault=tmp_path / "vault",
        base_url="https://proxy.example/v1",
        auth_token="tok",
    )

    assert env["HOME"] == str(tmp_path / "home")
    assert not (tmp_path / "claude-config" / ".credentials.json").exists()


def test_the_redirect_leaves_path_alone(tmp_path: Path, monkeypatch) -> None:
    """The skills tell the agent to run a bare ``lit``, which PATH resolves. PATH
    is inherited from os.environ and a HOME change does not touch it — but the
    whole run would score 0 for "litman reasons" if that were ever wrong."""
    _fake_user_home(tmp_path, monkeypatch)
    _stub_skill_install(monkeypatch)
    monkeypatch.setenv("PATH", "/sentinel/bin:/usr/bin")

    env = ClaudeAdapter().prepare(tmp_path, run_vault=tmp_path / "vault")

    assert env["PATH"] == "/sentinel/bin:/usr/bin"
