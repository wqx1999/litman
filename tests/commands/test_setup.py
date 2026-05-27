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
    # completion no / skill skip / vault no. (sync auto-skips: rclone absent.)
    result = runner.invoke(cli, ["setup"], input="n\n2\nn\n")
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
    # completion no / skill skip / vault yes / parent dir.
    result = runner.invoke(
        cli, ["setup"], input=f"n\n2\ny\n{parent}\n"
    )
    assert result.exit_code == 0, result.output

    reg = load_registry()
    assert len(reg.vaults) == 1
    assert find_active(reg) is not None
    assert (parent / "literature_vault" / "lit-config.yaml").is_file()

    # Summary's "Done" block must name the performed step. Rich may wrap the
    # panel, so assert on robust substrings rather than exact layout: a
    # regression that drops "vault" from the `did` list would fail here.
    assert "Done" in result.output
    assert "vault" in result.output


def test_setup_second_vault_requires_register_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    # completion no / skill skip / vault yes -> "create another" -> register
    # name 'fork1' -> parent dir.
    result = runner.invoke(
        cli, ["setup"], input=f"n\n2\ny\nfork1\n{parent2}\n"
    )
    assert result.exit_code == 0, result.output

    reg = load_registry()
    by_name = {v.name: v for v in reg.vaults}
    assert "fork1" in by_name
    assert by_name["fork1"].is_active is False
    assert by_name["literature_vault"].is_active is True
    assert (parent2 / "literature_vault" / "lit-config.yaml").is_file()


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
    calls: list[bool] = []
    monkeypatch.setattr(
        "litman.commands.setup.sync_setup_cmd",
        _make_recording_stub(calls),
    )

    runner = CliRunner()
    # completion no / skill skip / vault: have one -> "create another" no /
    # sync yes.
    result = runner.invoke(cli, ["setup"], input="n\n2\nn\ny\n")
    assert result.exit_code == 0, result.output
    assert calls == [True]


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
    calls: list[bool] = []
    monkeypatch.setattr(
        "litman.commands.setup.sync_setup_cmd",
        _make_recording_stub(calls),
    )

    runner = CliRunner()
    # completion no / skill skip / vault "create another" no / reconfigure:
    # press Enter (empty line) to accept the N default.
    result = runner.invoke(cli, ["setup"], input="n\n2\nn\n\n")
    assert result.exit_code == 0, result.output

    # Pressing Enter accepted default=False: no reconfiguration happened.
    assert calls == []
    # Summary lists sync as skipped (already configured).
    assert "Skipped" in result.output
    assert "sync" in result.output


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
    # No line reserved for completion (it must not prompt): skill skip /
    # vault no.
    result = runner.invoke(cli, ["setup"], input="2\nn\n")
    assert result.exit_code == 0, result.output
    assert "already installed" in result.output


# A click.Command-shaped stub: ctx.invoke calls the underlying callback, so a
# plain Click command whose callback records the call is enough to assert the
# wizard reached step 4 without launching rclone.
def _make_recording_stub(calls: list[bool]):
    import click

    @click.command("sync-setup-stub")
    def _stub() -> None:
        calls.append(True)

    return _stub
