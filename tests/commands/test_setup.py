"""Tests for ``lit setup`` — the interactive onboarding wizard (M27).

The wizard is a pure orchestrator: every step delegates to a standalone
command via ``ctx.invoke``. These tests force the interactive branch
(``_stdin_is_tty`` patched to True) and feed scripted ``input`` to drive the
prompts. ``shutil.which`` is patched per-test so a real rclone on the host
can never launch its TUI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.config import load_config
from litman.core.library import create_vault
from litman.core.sync import SetupPayload, write_sync_to_config
from litman.core.vault_registry import (
    add_vault,
    find_active,
    load_registry,
    save_registry,
)


# ---------------------------------------------------------------------------
# Registry isolation (copied verbatim from test_init.py — no shared conftest
# fake_home; lit setup writes ~/.bashrc, ~/.claude/skills/, and the registry).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME + clear cross-cutting env vars so every test in this
    module reads/writes a tmp-path-rooted registry instead of the real
    ``~/.config/litman/vaults.yaml``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _fake_skill_status(**states: str):
    """Build a ``skill_status()``-shaped dict: every bundled skill defaults
    to the given ``default`` state (kwarg), individual skills overridable by
    name with ``-`` spelled ``_`` (``lit_library="stale"``)."""
    from litman.core.skill import list_bundled_skills

    default = states.pop("default", "absent")
    by_name = {k.replace("_", "-"): v for k, v in states.items()}
    return {
        name: {"state": by_name.get(name, default), "stale_files": []}
        for name in list_bundled_skills()
    }


@pytest.fixture(autouse=True)
def pretend_no_skills_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``skill_status`` to report every bundled skill absent by default.

    Why: left un-stubbed, the probe scans the developer's real
    skills dirs (the shared conftest ``_isolate_skills_dir``
    fixture already neutralizes that, but this module's branch selection
    must not silently depend on it) — on machines that have already run
    ``lit install-skill`` that would flip setup tests from the "no skills
    installed" branch to the "already installed" branch and break their
    scripted input. Tests that need the installed / stale branches override
    with their own ``monkeypatch.setattr`` call. The stub takes ``**kw``
    because the wizard passes the chosen agent's ``parent_dir=``.
    """
    monkeypatch.setattr(
        "litman.commands.setup.skill_status",
        lambda **kw: _fake_skill_status(default="absent"),
    )


@pytest.fixture(autouse=True)
def pin_step5_to_auto_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin step 5 (desktop shortcut) to its no-prompt auto-skip branch.

    The step probes machine-global state — an existing shortcut under
    XDG_DATA_HOME / APPDATA and the DISPLAY env — so on a workstation with a
    display (or a shortcut already created) it would consume an extra scripted
    ``input`` answer and silently break every pre-existing test here. Step-5
    tests override these two attributes explicitly.
    """
    monkeypatch.setattr(
        "litman.commands.setup.shortcut_path",
        lambda: tmp_path / "pin-step5" / "litman.desktop",
    )
    monkeypatch.setattr(
        "litman.commands.setup.display_available", lambda: False
    )


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "litman.commands.setup._stdin_is_tty", lambda: True
    )


def _no_rclone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the 'rclone not installed' branch so step 4 never launches a
    real rclone TUI even if the host has rclone on PATH."""
    monkeypatch.setattr(
        "litman.commands.setup.shutil.which", lambda _x: None
    )


# ---------------------------------------------------------------------------
# Non-TTY gate (OQ1)
# ---------------------------------------------------------------------------


def test_setup_non_tty_errors() -> None:
    # No _stdin_is_tty patch: CliRunner's stdin is not a tty by default.
    runner = CliRunner()
    result = runner.invoke(cli, ["setup"])
    assert result.exit_code != 0
    out = (result.output or "") + (
        str(result.exception) if result.exception else ""
    )
    for cmd in (
        "install-completion",
        "install-skill",
        "init",
        "sync setup",
        "gui --make-shortcut",
    ):
        assert cmd in out


# ---------------------------------------------------------------------------
# Interactive flows
# ---------------------------------------------------------------------------


def test_setup_decline_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    runner = CliRunner()
    # completion no / agent choice: Enter accepts claude / skill skip /
    # vault no. (sync auto-skips: rclone absent.)
    result = runner.invoke(cli, ["setup"], input="n\n\nn\nn\n")
    assert result.exit_code == 0, result.output

    # Nothing was registered.
    assert load_registry().vaults == []
    # No completion block written.
    bashrc = Path.home() / ".bashrc"
    assert not bashrc.exists() or "lit-completion" not in bashrc.read_text(
        encoding="utf-8"
    )
    assert "Skipped" in result.output


def test_setup_creates_first_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")
    parent = tmp_path / "vault_parent"
    parent.mkdir()

    runner = CliRunner()
    # completion no / agent choice: Enter / skill skip / vault yes /
    # accept default name / parent.
    result = runner.invoke(
        cli, ["setup"], input=f"n\n\nn\ny\n\n{parent}\n"
    )
    assert result.exit_code == 0, result.output

    reg = load_registry()
    assert len(reg.vaults) == 1
    assert find_active(reg) is not None
    assert reg.vaults[0].name == "literature_vault"
    assert (parent / "literature_vault" / "lit-config.yaml").is_file()

    # Summary's "Done" block must name the performed step. Rich may wrap the
    # panel, so assert on robust substrings rather than exact layout: a
    # regression that drops "vault" from the `did` list would fail here.
    assert "Done" in result.output
    assert "vault" in result.output


def test_setup_second_vault_uses_chosen_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a vault is already registered, step 3 suggests a collision-free
    default (e.g. ``literature_vault_2``) but the user may override. The
    typed name flows to BOTH the on-disk subdir AND the registry entry."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    # Seed one already-registered, active vault.
    existing_parent = tmp_path / "p0"
    existing_parent.mkdir()
    existing = create_vault(existing_parent, name="literature_vault")
    save_registry(
        add_vault(load_registry(), "literature_vault", existing)
    )

    parent2 = tmp_path / "p2"
    parent2.mkdir()

    runner = CliRunner()
    # completion no / agent choice: Enter / skill skip / vault yes ->
    # "create another" -> override the suggested default with 'fork1' ->
    # parent dir.
    result = runner.invoke(
        cli, ["setup"], input=f"n\n\nn\ny\nfork1\n{parent2}\n"
    )
    assert result.exit_code == 0, result.output

    reg = load_registry()
    by_name = {v.name: v for v in reg.vaults}
    assert "fork1" in by_name
    assert by_name["fork1"].is_active is False
    assert by_name["literature_vault"].is_active is True
    # The chosen name drives the on-disk subdir too — not the hardcoded
    # 'literature_vault/' from before the wizard exposed --name.
    assert (parent2 / "fork1" / "lit-config.yaml").is_file()


def test_setup_first_vault_custom_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-vault branch: user can rename the vault from the default
    ``literature_vault`` to something domain-specific (e.g. ``pepforge_lib``)
    by typing at the name prompt instead of pressing Enter."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")
    parent = tmp_path / "vault_parent"
    parent.mkdir()

    runner = CliRunner()
    # completion no / agent choice: Enter / skill skip / vault yes /
    # typed name / parent.
    result = runner.invoke(
        cli, ["setup"], input=f"n\n\nn\ny\npepforge_lib\n{parent}\n"
    )
    assert result.exit_code == 0, result.output

    reg = load_registry()
    assert len(reg.vaults) == 1
    assert reg.vaults[0].name == "pepforge_lib"
    assert (parent / "pepforge_lib" / "lit-config.yaml").is_file()
    assert not (parent / "literature_vault").exists()


def test_setup_sync_when_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_tty(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    # An active vault to attach sync to.
    parent = tmp_path / "p0"
    parent.mkdir()
    vault = create_vault(parent, name="literature_vault")
    save_registry(add_vault(load_registry(), "literature_vault", vault))

    # rclone present so step 4 reaches the sync prompt.
    monkeypatch.setattr(
        "litman.commands.setup.shutil.which", lambda _x: "/usr/bin/rclone"
    )
    # Stub the invoked sync command so no real rclone TUI launches.
    calls: list[dict] = []
    monkeypatch.setattr(
        "litman.commands.setup.sync_setup_cmd",
        _make_recording_stub(calls),
    )

    runner = CliRunner()
    # completion no / agent choice: Enter / skill skip / vault: have one ->
    # "create another" no / sync yes.
    result = runner.invoke(cli, ["setup"], input="n\n\nn\nn\ny\n")
    assert result.exit_code == 0, result.output
    assert calls == [{}]


def test_setup_sync_already_configured_reconfigure_default_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 4 with sync already configured: the reconfigure prompt defaults to
    N, so pressing Enter SKIPS reconfiguration (stub never invoked) and the
    summary lists sync under 'Skipped'. Proves default=False on that confirm
    (setup.py:222-228)."""
    _force_tty(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    # An active vault whose load_config(vault).sync is already non-None. Reuse
    # the same write_sync_to_config helper test_sync.py uses to materialize a
    # configured `sync:` block — do not invent a new way to write config.
    parent = tmp_path / "p0"
    parent.mkdir()
    vault = create_vault(parent, name="literature_vault")
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(remote="some-remote", path="litman-vault/"),
    )
    save_registry(add_vault(load_registry(), "literature_vault", vault))
    assert load_config(vault).sync is not None  # precondition for the branch

    # rclone present so step 4 reaches the reconfigure prompt.
    monkeypatch.setattr(
        "litman.commands.setup.shutil.which", lambda _x: "/usr/bin/rclone"
    )
    # Stub the invoked sync command; reconfigure==No must leave it uncalled.
    calls: list[dict] = []
    monkeypatch.setattr(
        "litman.commands.setup.sync_setup_cmd",
        _make_recording_stub(calls),
    )

    runner = CliRunner()
    # completion no / agent choice: Enter / skill skip / vault "create
    # another" no / reconfigure: press Enter (empty line) to accept the N
    # default.
    result = runner.invoke(cli, ["setup"], input="n\n\nn\nn\n\n")
    assert result.exit_code == 0, result.output

    # Pressing Enter accepted default=False: no reconfiguration happened.
    assert calls == []
    # Summary lists sync as skipped (already configured).
    assert "Skipped" in result.output
    assert "sync" in result.output


def test_setup_skill_step_auto_skips_when_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-run idempotency for step 2: every bundled skill installed AND
    byte-identical to the bundle → nothing to refresh, so the step auto-skips
    (no prompt, no scripted-input slot) while still recording the machine
    default and pointing at `install-skill --force` for a manual re-copy."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    monkeypatch.setattr(
        "litman.commands.setup.skill_status",
        lambda **kw: _fake_skill_status(default="current"),
    )
    # If the wizard slipped through to install, fail loudly.
    calls: list[dict] = []
    monkeypatch.setattr(
        "litman.commands.setup.install_skill_cmd",
        _make_recording_stub(calls),
    )
    recorded: list[str] = []
    monkeypatch.setattr(
        "litman.commands.setup.agent_prefs.save_default_agent",
        lambda name: recorded.append(name),
    )

    runner = CliRunner()
    # completion no / agent choice: Enter (the skill step's ONLY input) /
    # vault no.
    result = runner.invoke(cli, ["setup"], input="n\n\nn\n")
    assert result.exit_code == 0, result.output
    assert calls == []  # install_skill_cmd was NOT called
    assert "up to date" in result.output
    assert recorded == ["claude"]
    # Neither the fresh-install confirm nor a refresh prompt may appear.
    assert "Install the Claude Code agent skill now?" not in result.output
    assert "Refresh" not in result.output


def test_setup_skill_step_stale_prompts_refresh_default_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skills installed but out of date → the wizard surfaces the drift with
    a [Y/n] refresh prompt defaulting to Y; plain Enter must call
    install_skill_cmd with force=True (the underlying flag the wizard
    exposes — feedback_wizard_mirrors_command_flags)."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    monkeypatch.setattr(
        "litman.commands.setup.skill_status",
        lambda **kw: _fake_skill_status(default="current", lit_library="stale"),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "litman.commands.setup.install_skill_cmd",
        _make_recording_stub(calls),
    )

    runner = CliRunner()
    # completion no / agent choice: Enter / skill refresh: Enter accepts the
    # Y default / vault no.
    result = runner.invoke(cli, ["setup"], input="n\n\n\nn\n")
    assert result.exit_code == 0, result.output
    assert "out of date" in result.output
    assert "lit-library" in result.output
    assert len(calls) == 1
    assert calls[0].get("force") is True
    assert calls[0].get("agent_name") == "claude"  # follows the choice


def test_setup_skill_step_stale_refresh_declined_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declining the stale-refresh prompt must NOT install and must continue
    the wizard (the drift stays flagged by health-check for later)."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    monkeypatch.setattr(
        "litman.commands.setup.skill_status",
        lambda **kw: _fake_skill_status(default="stale"),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "litman.commands.setup.install_skill_cmd",
        _make_recording_stub(calls),
    )

    runner = CliRunner()
    # completion no / agent choice: Enter / skill refresh no / vault no.
    result = runner.invoke(cli, ["setup"], input="n\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "out of date" in result.output


def test_setup_skill_step_fresh_install_accepts_via_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh-install branch (no skills present): the step is now a plain
    [Y/n] confirm, not a numbered picker. Pressing Enter accepts the Yes
    default -> installs the Claude Code skill WITHOUT --force and records the
    machine-level default agent (so `lit setup` clears the GUI red dot too).
    Also proves the agent-management hint note is shown."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    # pretend_no_skills_installed autouse fixture -> fresh-install branch.
    calls: list[dict] = []
    monkeypatch.setattr(
        "litman.commands.setup.install_skill_cmd",
        _make_recording_stub(calls),
    )
    recorded: list[str] = []
    monkeypatch.setattr(
        "litman.commands.setup.agent_prefs.save_default_agent",
        lambda name: recorded.append(name),
    )

    runner = CliRunner()
    # completion no / agent choice: Enter / skill: press Enter to accept the
    # Y default / vault no.
    result = runner.invoke(cli, ["setup"], input="n\n\n\nn\n")
    assert result.exit_code == 0, result.output
    # install_skill_cmd invoked WITHOUT force, targeting the chosen agent.
    assert calls == [{"agent_name": "claude"}]
    assert recorded == ["claude"]  # machine-level default recorded
    assert "More agents can be added" in result.output  # agent-management hint


def test_setup_agent_choice_agy_records_default_before_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing agy end-to-end, strict three-beat order: the choice is
    recorded as the machine-level default BEFORE the install runs, and the
    install targets the chosen agent (--agent agy underneath)."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    events: list[object] = []
    monkeypatch.setattr(
        "litman.commands.setup.agent_prefs.save_default_agent",
        lambda name: events.append(("default", name)),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "litman.commands.setup.install_skill_cmd",
        _make_recording_stub(calls, events),
    )

    runner = CliRunner()
    # completion no / agent choice: type agy / skill install: Enter /
    # vault no.
    result = runner.invoke(cli, ["setup"], input="n\nagy\n\nn\n")
    assert result.exit_code == 0, result.output
    assert calls == [{"agent_name": "agy"}]
    # The default was recorded strictly before the install ran.
    assert events[0] == ("default", "agy")
    assert events[1] == ("install", {"agent_name": "agy"})
    assert "Antigravity CLI" in result.output  # step speaks the display name


def test_setup_agent_choice_recorded_even_when_skill_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining the skill install does NOT roll back the agent choice — the
    selection is machine state the moment it is made (decision: choose
    first, install second)."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    recorded: list[str] = []
    monkeypatch.setattr(
        "litman.commands.setup.agent_prefs.save_default_agent",
        lambda name: recorded.append(name),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "litman.commands.setup.install_skill_cmd",
        _make_recording_stub(calls),
    )

    runner = CliRunner()
    # completion no / agent choice: agy / skill install: no / vault no.
    result = runner.invoke(cli, ["setup"], input="n\nagy\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert calls == []  # nothing installed
    assert recorded == ["agy"]  # ... but the choice stuck
    assert "skill (declined)" in result.output


def test_setup_agent_prompt_defaults_to_recorded_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running setup after choosing agy: plain Enter keeps agy (the
    prompt default follows the recorded machine-level default), and the
    probe runs against agy's directory (parent_dir forwarded)."""
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    monkeypatch.setattr(
        "litman.commands.setup.agent_prefs.load_default_agent",
        lambda: "agy",
    )
    recorded: list[str] = []
    monkeypatch.setattr(
        "litman.commands.setup.agent_prefs.save_default_agent",
        lambda name: recorded.append(name),
    )
    probed: list[dict] = []

    def fake_status(**kw):
        probed.append(dict(kw))
        return _fake_skill_status(default="current")

    monkeypatch.setattr("litman.commands.setup.skill_status", fake_status)

    from litman.core.agents import agent_skills_parent_dir

    runner = CliRunner()
    # completion no / agent choice: Enter keeps agy / (up to date: no
    # skill input) / vault no.
    result = runner.invoke(cli, ["setup"], input="n\n\nn\n")
    assert result.exit_code == 0, result.output
    assert recorded == ["agy"]
    assert "up to date" in result.output  # idempotent re-run, zero prompts
    # The probe hit agy's skills dir (the conftest-isolated antigravity dir).
    assert probed == [{"parent_dir": agent_skills_parent_dir("agy")}]


def test_setup_sweeps_other_agent_dirs_after_skill_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard's skill step ends with the cross-directory sweep: chosen
    agent up to date, but a stale copy installed for ANOTHER agent gets its
    own [Y/n] (default yes) and comes back current on plain Enter."""
    from litman.core import skill
    from litman.core.skill import install_all_skills

    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    # Chosen agent (claude, the prompt default) reads as fully up to date.
    monkeypatch.setattr(
        "litman.commands.setup.skill_status",
        lambda **kw: _fake_skill_status(default="current"),
    )
    # ... while the open-standard dir holds a stale real copy.
    standard = skill.standard_skills_parent_dir()
    install_all_skills(parent_dir=standard)
    stale_md = standard / "lit-library" / "SKILL.md"
    stale_md.write_text("OLD OTHER-AGENT COPY\n", encoding="utf-8")

    runner = CliRunner()
    # completion no / agent choice: Enter keeps claude / sweep refresh:
    # Enter accepts the Y default / vault no.
    result = runner.invoke(cli, ["setup"], input="n\n\n\nn\n")
    assert result.exit_code == 0, result.output
    assert "another agent" in result.output
    text = stale_md.read_text(encoding="utf-8")
    assert "OLD OTHER-AGENT COPY" not in text
    assert "name: lit-library" in text


def test_setup_sweep_decline_leaves_other_agent_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litman.core import skill
    from litman.core.skill import install_all_skills

    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    monkeypatch.setattr(
        "litman.commands.setup.skill_status",
        lambda **kw: _fake_skill_status(default="current"),
    )
    standard = skill.standard_skills_parent_dir()
    install_all_skills(parent_dir=standard)
    stale_md = standard / "lit-library" / "SKILL.md"
    stale_md.write_text("OLD OTHER-AGENT COPY\n", encoding="utf-8")

    runner = CliRunner()
    # completion no / agent Enter / sweep refresh: no / vault no.
    result = runner.invoke(cli, ["setup"], input="n\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert stale_md.read_text(encoding="utf-8") == "OLD OTHER-AGENT COPY\n"


def test_setup_completion_step_skips_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")

    # Pre-write the completion sentinel so step 1 detects it as installed.
    from litman.commands.install_completion import SENTINEL

    bashrc = Path.home() / ".bashrc"
    bashrc.write_text(f"{SENTINEL}\n", encoding="utf-8")

    runner = CliRunner()
    # No line reserved for completion (it must not prompt): agent choice
    # Enter / skill skip / vault no.
    result = runner.invoke(cli, ["setup"], input="\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert "already installed" in result.output


# A click.Command-shaped stub: ctx.invoke calls the underlying callback, so a
# plain Click command whose callback records the call is enough to assert the
# wizard reached the invocation site. The callback accepts **kwargs so the
# stub works whether the wizard invokes it with no args (sync_setup_cmd) or
# with flags (install_skill_cmd, force=True). An optional shared `events`
# list additionally records ("install", kwargs) so a test can assert
# ordering against other recorded events (e.g. the default-agent write).
def _make_recording_stub(calls: list[dict], events: list | None = None):
    import click

    @click.command("recording-stub")
    def _stub(**kwargs) -> None:
        calls.append(dict(kwargs))
        if events is not None:
            events.append(("install", dict(kwargs)))

    return _stub


# ---------------------------------------------------------------------------
# Step 5 — desktop shortcut (task-gui-desktop-entry D4)
# ---------------------------------------------------------------------------


def test_setup_step5_skips_when_shortcut_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")
    existing = tmp_path / "apps" / "litman.desktop"
    existing.parent.mkdir(parents=True)
    existing.write_text("[Desktop Entry]\n", encoding="utf-8")
    monkeypatch.setattr(
        "litman.commands.setup.shortcut_path", lambda: existing
    )
    # Display present: proves the exists-probe short-circuits BEFORE any
    # prompt (input script carries no answer for step 5).
    monkeypatch.setattr(
        "litman.commands.setup.display_available", lambda: True
    )

    result = CliRunner().invoke(cli, ["setup"], input="n\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert "already exists" in result.output
    assert "shortcut (already exists)" in result.output


def test_setup_step5_headless_skips_without_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")
    # autouse pin: no existing shortcut + display_available -> False.
    # Input carries no answer for step 5 — a prompt would exhaust it and
    # abort.
    result = CliRunner().invoke(cli, ["setup"], input="n\n\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert "shortcut (headless session)" in result.output


def test_setup_step5_prompt_names_underlying_command_and_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_tty(monkeypatch)
    _no_rclone(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        "litman.commands.setup.display_available", lambda: True
    )

    result = CliRunner().invoke(cli, ["setup"], input="n\n\nn\nn\nn\n")
    assert result.exit_code == 0, result.output
    # Wizard prompts must surface the underlying command.
    assert "lit gui --make-shortcut" in result.output
    assert "shortcut (declined)" in result.output


def test_setup_step5_accept_creates_shortcut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_tty(monkeypatch)
    monkeypatch.setenv("SHELL", "/bin/bash")
    # One which-stub serves both steps: no rclone (step 4 auto-skips) and a
    # deterministic `lit` path for the shortcut's Exec line.
    monkeypatch.setattr(
        "litman.commands.setup.shutil.which",
        lambda name: "/opt/lit/bin/lit" if name == "lit" else None,
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    dest = tmp_path / "xdg" / "applications" / "litman.desktop"
    monkeypatch.setattr(
        "litman.commands.setup.shortcut_path", lambda: dest
    )
    monkeypatch.setattr(
        "litman.commands.setup.display_available", lambda: True
    )

    result = CliRunner().invoke(cli, ["setup"], input="n\n\nn\nn\ny\n")
    assert result.exit_code == 0, result.output
    assert dest.is_file()
    assert '"/opt/lit/bin/lit" gui --window' in dest.read_text(
        encoding="utf-8"
    )
    assert "desktop shortcut" in result.output
