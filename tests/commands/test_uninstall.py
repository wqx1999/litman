"""Tests for ``lit uninstall`` and its teardown helpers.

Isolation: ``HOME`` is redirected to ``tmp_path`` (so ``Path.home()`` and the
skill/completion paths land in a scratch dir) and ``LITMAN_REGISTRY_DIR``
points the vaults.yaml registry into the same scratch dir. Nothing touches
the developer's real ~/.claude, ~/.agents, ~/.bashrc, or config dir. The
CLI-command tests carry ``no_skills_isolation``: the uninstall sweep resolves
its directories through the real catalog resolvers, which the redirected
``$HOME`` isolates — the conftest seam patch would point them somewhere the
tests do not seed.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands import _drift
from litman.commands.gui import browser_profile_dir, remove_shortcut, shortcut_path
from litman.commands.install_completion import (
    _build_plan,
    _install_block,
    completion_installed,
    uninstall_completion,
)
from litman.core import gui_registry, update_check
from litman.core.agent_prefs import prefs_path, remove_prefs, save_default_agent
from litman.core.gui_registry import (
    gui_dir,
    gui_record_files,
    register_gui,
    remove_gui_records,
    unregister_gui,
)
from litman.core.library import create_vault
from litman.core.self_update_helper import fail_flag_path, log_path
from litman.core.skill import (
    install_all_skills,
    install_skill,
    list_bundled_skills,
    uninstall_skill,
)
from litman.core.ui_state import save_pins, ui_state_path
from litman.core.vault_registry import (
    add_vault,
    load_registry,
    registry_path,
    remove_registry,
    save_registry,
)
from litman.core.portable_link import is_portable_link

# --------------------------------------------------------------------------
# uninstall_skill
# --------------------------------------------------------------------------


def test_uninstall_skill_removes_dir(tmp_path: Path) -> None:
    name = list_bundled_skills()[0]
    install_skill(target=tmp_path / name, name=name)
    result = uninstall_skill(name, tmp_path)
    assert result["mode"] == "removed"
    assert result["removed"]  # at least SKILL.md deleted
    assert not (tmp_path / name).exists()


def test_uninstall_skill_keeps_user_files(tmp_path: Path) -> None:
    name = list_bundled_skills()[0]
    install_skill(target=tmp_path / name, name=name)
    extra = tmp_path / name / "my-notes.md"
    extra.write_text("mine", encoding="utf-8")

    result = uninstall_skill(name, tmp_path)

    assert result["mode"] == "kept"
    assert "my-notes.md" in result["leftover"]
    assert extra.exists()
    # the bundled files are gone even though the dir survives
    assert not (tmp_path / name / "SKILL.md").exists()


def test_uninstall_skill_absent(tmp_path: Path) -> None:
    result = uninstall_skill("lit-library", tmp_path)
    assert result["mode"] == "absent"
    assert result["removed"] == []


# --------------------------------------------------------------------------
# uninstall_completion
# --------------------------------------------------------------------------


def test_uninstall_completion_bash_strips_block(tmp_path: Path) -> None:
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("export FOO=1\n", encoding="utf-8")
    _install_block(_build_plan("bash", tmp_path))
    assert completion_installed("bash", tmp_path)

    result = uninstall_completion("bash", tmp_path)

    assert result["removed"] is True
    body = bashrc.read_text(encoding="utf-8")
    assert "export FOO=1" in body  # user content preserved
    assert "lit-completion" not in body
    assert "_LIT_COMPLETE" not in body
    assert bashrc.exists()  # rc file is never deleted
    assert not completion_installed("bash", tmp_path)
    # no dangling blank-line block left behind
    assert body == "export FOO=1\n"


def test_uninstall_completion_fish_deletes_file(tmp_path: Path) -> None:
    _install_block(_build_plan("fish", tmp_path))
    fish = tmp_path / ".config" / "fish" / "completions" / "lit.fish"
    assert fish.is_file()

    result = uninstall_completion("fish", tmp_path)

    assert result["removed"] is True
    assert not fish.exists()  # dedicated file deleted when it held only our block


def test_uninstall_completion_absent(tmp_path: Path) -> None:
    result = uninstall_completion("zsh", tmp_path)
    assert result["removed"] is False


# --------------------------------------------------------------------------
# remove_registry
# --------------------------------------------------------------------------


def test_remove_registry_deletes_file_and_empty_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    reg = registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text("vaults: {}\n", encoding="utf-8")

    result = remove_registry()

    assert result["removed"] is True
    assert result["dir_removed"] is True
    assert not reg.exists()
    assert not reg.parent.exists()


def test_remove_registry_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    result = remove_registry()
    assert result["removed"] is False


# --------------------------------------------------------------------------
# lit uninstall (command)
# --------------------------------------------------------------------------


def _seed_artifacts(home: Path) -> Path:
    """Install skills + bash completion + a registry under ``home``.

    Returns the registry path so callers can assert on it.
    """
    install_all_skills(parent_dir=home / ".claude" / "skills")
    _install_block(_build_plan("bash", home))
    reg = registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text("vaults: {}\n", encoding="utf-8")
    return reg


@pytest.mark.no_skills_isolation
def test_uninstall_dry_run_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    reg = _seed_artifacts(tmp_path)

    result = CliRunner().invoke(cli, ["uninstall", "--dry-run"])

    assert result.exit_code == 0
    assert "dry run" in result.output
    # everything still present
    assert (tmp_path / ".claude" / "skills" / "lit-library").exists()
    assert completion_installed("bash", tmp_path)
    assert reg.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_yes_removes_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    reg = _seed_artifacts(tmp_path)

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Removed" in result.output
    assert "pipx uninstall litman" in result.output
    assert not (tmp_path / ".claude" / "skills" / "lit-library").exists()
    assert not completion_installed("bash", tmp_path)
    assert not reg.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_removes_the_app_window_browser_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `lit gui --window` leaves tens of MB of Chromium state behind; an
    # uninstall that keeps it is not an uninstall.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    _seed_artifacts(tmp_path)
    profile = browser_profile_dir()
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert "browser profile" in result.output
    assert not profile.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_dry_run_lists_but_keeps_the_browser_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    _seed_artifacts(tmp_path)
    profile = browser_profile_dir()
    profile.mkdir(parents=True)

    result = CliRunner().invoke(cli, ["uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "browser profile" in result.output
    assert profile.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_decline_aborts_without_removing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    _seed_artifacts(tmp_path)
    monkeypatch.setattr("litman.core.confirm._stdin_is_tty", lambda: True)

    result = CliRunner().invoke(cli, ["uninstall"], input="n\n")

    assert result.exit_code != 0  # click.Abort on a declined confirm
    assert (tmp_path / ".claude" / "skills" / "lit-library").exists()
    assert completion_installed("bash", tmp_path)


@pytest.mark.no_skills_isolation
def test_uninstall_without_a_terminal_asks_for_yes_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list of what would go still prints; a stray "y" on stdin is not an
    answer."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    _seed_artifacts(tmp_path)

    result = CliRunner().invoke(cli, ["uninstall"], input="y\n")

    assert result.exit_code == 1
    assert "pass --yes to confirm" in result.output
    assert "Remove the items listed above?" not in result.output
    assert (tmp_path / ".claude" / "skills" / "lit-library").exists()
    assert completion_installed("bash", tmp_path)


@pytest.mark.no_skills_isolation
def test_uninstall_nothing_to_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))

    result = CliRunner().invoke(cli, ["uninstall"])

    assert result.exit_code == 0
    assert "Nothing to remove" in result.output
    assert "pipx uninstall litman" in result.output


@pytest.mark.no_skills_isolation
def test_uninstall_sweeps_all_skills_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skills installed for claude AND cursor AND agy: the plan groups by
    all three directories, the sweep clears all three, user files survive —
    uninstall is the full-sweep exception to the health-check's
    default-dir-only probe."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    claude_dir = tmp_path / ".claude" / "skills"
    standard_dir = tmp_path / ".agents" / "skills"
    antigravity_dir = tmp_path / ".gemini" / "antigravity-cli" / "skills"
    install_all_skills(parent_dir=claude_dir)
    install_all_skills(parent_dir=standard_dir)
    install_all_skills(parent_dir=antigravity_dir)
    user_file = standard_dir / "lit-library" / "my-notes.md"
    user_file.write_text("mine", encoding="utf-8")

    plan = CliRunner().invoke(cli, ["uninstall", "--dry-run"])
    assert plan.exit_code == 0, plan.output
    # One "Agent skills (<dir>)" group per known directory; the literal
    # paths may be line-wrapped by the panel, so count groups, not paths.
    assert plan.output.count("Agent skills") == 3
    assert plan.output.count("lit-library") == 3  # one per directory group

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (claude_dir / "lit-library").exists()
    assert not (claude_dir / "lit-reading").exists()
    assert not (standard_dir / "lit-reading").exists()
    assert not (antigravity_dir / "lit-library").exists()
    assert not (antigravity_dir / "lit-reading").exists()
    # bundled files gone from the standard dir too; the user file survives
    assert not (standard_dir / "lit-library" / "SKILL.md").exists()
    assert user_file.read_text(encoding="utf-8") == "mine"


def test_uninstall_registered_in_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(cli, ["uninstall", "--help"])
    assert result.exit_code == 0
    # help names the installer-neutral CLI-removal step; collapse whitespace so
    # click's line-wrapping can't split the phrases.
    normalized = " ".join(result.output.split())
    assert "uv tool uninstall litman" in normalized
    assert "pipx uninstall litman" in normalized


# --------------------------------------------------------------------------
# safety / robustness regressions
# --------------------------------------------------------------------------


@pytest.mark.no_skills_isolation
def test_uninstall_preserves_registered_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline guarantee: a real registered vault (and its papers) must
    survive `lit uninstall` — only the registry *pointer* is dropped."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    parent = tmp_path / "libs"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(add_vault(load_registry(), "main", vault))
    paper_marker = vault / "papers" / "keepme.txt"
    paper_marker.parent.mkdir(parents=True, exist_ok=True)
    paper_marker.write_text("my data", encoding="utf-8")
    install_all_skills(parent_dir=tmp_path / ".claude" / "skills")

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    # registry pointer gone, but the vault dir + its contents untouched
    assert not registry_path().is_file()
    assert vault.is_dir()
    assert paper_marker.read_text(encoding="utf-8") == "my data"


def test_uninstall_skill_skips_symlinked_dir(tmp_path: Path) -> None:
    """A symlinked skill dir is left untouched — no deletion through it, no
    rmdir crash on the symlink path."""
    name = list_bundled_skills()[0]
    real = tmp_path / "real_skill"
    real.mkdir()
    (real / "SKILL.md").write_text("real", encoding="utf-8")
    parent = tmp_path / ".claude" / "skills"
    parent.mkdir(parents=True)
    link = parent / name
    os.symlink(real, link)

    result = uninstall_skill(name, parent)

    assert result["mode"] == "skipped"
    assert result["removed"] == []
    assert is_portable_link(link)  # link left in place
    assert (real / "SKILL.md").exists()  # nothing deleted through the link


def test_uninstall_completion_survives_non_utf8_rc(tmp_path: Path) -> None:
    """A latin-1 byte in .bashrc must not crash detection or stripping, and
    the user's non-UTF-8 content must survive byte-for-byte."""
    bashrc = tmp_path / ".bashrc"
    bashrc.write_bytes(b"export CAFE=caf\xe9\n")  # 0xE9 = latin-1 'é'
    _install_block(_build_plan("bash", tmp_path))

    assert completion_installed("bash", tmp_path)  # no UnicodeDecodeError

    result = uninstall_completion("bash", tmp_path)

    assert result["removed"] is True
    raw = bashrc.read_bytes()
    assert b"caf\xe9" in raw  # original bytes preserved
    assert b"lit-completion" not in raw
    assert not completion_installed("bash", tmp_path)


@pytest.mark.no_skills_isolation
def test_uninstall_cmd_dry_run_survives_non_utf8_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lit uninstall --dry-run` must not traceback on a non-UTF-8 rc file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    (tmp_path / ".bashrc").write_bytes(b"caf\xe9\n")
    _install_block(_build_plan("bash", tmp_path))

    result = CliRunner().invoke(cli, ["uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output


def test_uninstall_completion_sentinel_substring_only(tmp_path: Path) -> None:
    """When the sentinel appears only inside another line (not as its own
    block), nothing is stripped and `removed` is False."""
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(
        'echo "# lit-completion (do not edit) is just a note"\n',
        encoding="utf-8",
    )
    before = bashrc.read_text(encoding="utf-8")

    result = uninstall_completion("bash", tmp_path)

    assert result["removed"] is False
    assert bashrc.read_text(encoding="utf-8") == before  # untouched


def test_uninstall_completion_keeps_user_line_when_eval_manually_removed(
    tmp_path: Path,
) -> None:
    """Never over-remove: if the user deleted the eval line but left the
    sentinel, the line now following the sentinel is THEIR content and must
    survive. Only the sentinel (and any blank separator) is stripped.
    """
    bashrc = tmp_path / ".bashrc"
    # Sentinel present, but the eval line is gone — the user's own line sits
    # directly beneath the sentinel comment.
    bashrc.write_text(
        "export BEFORE=1\n"
        "\n"
        "# lit-completion (do not edit)\n"
        "export MY_API_KEY=secret123\n"
        "export AFTER=2\n",
        encoding="utf-8",
    )

    result = uninstall_completion("bash", tmp_path)

    assert result["removed"] is True
    body = bashrc.read_text(encoding="utf-8")
    # The sentinel (and its preceding blank) are gone...
    assert "lit-completion" not in body
    # ...but the user's line that merely happened to follow it is untouched.
    assert "export MY_API_KEY=secret123" in body
    assert "export BEFORE=1" in body
    assert "export AFTER=2" in body
    assert body == "export BEFORE=1\nexport MY_API_KEY=secret123\nexport AFTER=2\n"


def test_uninstall_completion_zsh_strips_block(tmp_path: Path) -> None:
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("alias ll='ls -l'\n", encoding="utf-8")
    _install_block(_build_plan("zsh", tmp_path))
    assert completion_installed("zsh", tmp_path)

    result = uninstall_completion("zsh", tmp_path)

    assert result["removed"] is True
    body = zshrc.read_text(encoding="utf-8")
    assert body == "alias ll='ls -l'\n"
    assert not completion_installed("zsh", tmp_path)


def test_remove_registry_keeps_nonempty_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    reg = registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text("vaults: {}\n", encoding="utf-8")
    sibling = reg.parent / "something-else.txt"
    sibling.write_text("keep", encoding="utf-8")

    result = remove_registry()

    assert result["removed"] is True
    assert result["dir_removed"] is False
    assert not reg.exists()
    assert reg.parent.is_dir()  # dir kept because a sibling remains
    assert sibling.exists()


# --------------------------------------------------------------------------
# remove_shortcut
# --------------------------------------------------------------------------


def _seed_shortcut(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a dummy shortcut file at the platform ``shortcut_path()``.

    HOME must already be redirected by the caller; XDG_DATA_HOME is cleared so
    the Linux path resolves under the redirected HOME (not the dev's real
    ~/.local/share). The platform is pinned to Linux: the darwin branch scans
    real bundle homes (its flavour has bundle tests of its own below) and the
    win32 branch writes to the real Desktop.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    target = shortcut_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[Desktop Entry]\n", encoding="utf-8")
    return target


def test_remove_shortcut_deletes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _seed_shortcut(monkeypatch)
    assert target.exists()

    removed = remove_shortcut()

    assert removed == target
    assert not target.exists()


def test_remove_shortcut_absent_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pinned off darwin: that branch sweeps the real bundle homes, and on a
    # Mac host with litman installed this test would delete the real bundle.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert remove_shortcut() is None


def _seed_bundle(home: Path, bundle_id: str = "io.github.litman") -> Path:
    """Build a minimal ``litman.app`` under ``home`` and return it."""
    contents = home / "litman.app" / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_text(
        f"<string>{bundle_id}</string>", encoding="utf-8"
    )
    return home / "litman.app"


def test_remove_shortcut_removes_app_bundle_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The macOS artifact is a ``.app`` directory bundle — removed whole,
    once its Info.plist vouches it is ours."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "litman.commands.gui._DARWIN_SYSTEM_APPS", tmp_path / "system-apps"
    )
    bundle = _seed_bundle(tmp_path / "Applications")

    removed = remove_shortcut()

    assert removed == bundle
    assert not bundle.exists()


def test_remove_shortcut_darwin_sweeps_both_bundle_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uninstall is a one-shot exit — a bundle in each home (a hand-moved
    install, a pre-migration leftover) must not survive it."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr("litman.commands.gui._DARWIN_SYSTEM_APPS", system_apps)
    system_bundle = _seed_bundle(system_apps)
    user_bundle = _seed_bundle(tmp_path / "Applications")

    removed = remove_shortcut()

    assert removed == system_bundle
    assert not system_bundle.exists()
    assert not user_bundle.exists()


def test_remove_shortcut_darwin_reports_only_what_actually_went(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ignore_errors=True`` swallows a failed delete (a running bundle on a
    locked volume, a permission flip) — the return value must come from a
    re-check of the filesystem, not from having made the call."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr("litman.commands.gui._DARWIN_SYSTEM_APPS", system_apps)
    system_bundle = _seed_bundle(system_apps)
    user_bundle = _seed_bundle(tmp_path / "Applications")

    real_rmtree = shutil.rmtree

    def sparing_the_system_bundle(path, ignore_errors=False, **kwargs):
        if Path(path) == system_bundle:
            return  # the silent failure ignore_errors=True turns into
        real_rmtree(path, ignore_errors=ignore_errors, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", sparing_the_system_bundle)

    removed = remove_shortcut()

    assert removed == user_bundle
    assert system_bundle.exists()
    assert not user_bundle.exists()


def test_remove_shortcut_darwin_spares_a_foreign_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A litman.app whose Info.plist is not ours is not ours to delete."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    system_apps = tmp_path / "system-apps"
    monkeypatch.setattr("litman.commands.gui._DARWIN_SYSTEM_APPS", system_apps)
    foreign_bundle = _seed_bundle(system_apps, bundle_id="com.example.imposter")
    user_bundle = _seed_bundle(tmp_path / "Applications")

    removed = remove_shortcut()

    assert removed == user_bundle
    assert not user_bundle.exists()
    assert foreign_bundle.exists()


# --------------------------------------------------------------------------
# remove_prefs
# --------------------------------------------------------------------------


def test_remove_prefs_deletes_file_and_empty_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    save_default_agent("claude")
    prefs = prefs_path()
    assert prefs.is_file()

    result = remove_prefs()

    assert result["removed"] is True
    assert result["dir_removed"] is True
    assert not prefs.exists()
    assert not prefs.parent.exists()


def test_remove_prefs_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    assert remove_prefs()["removed"] is False


def test_remove_prefs_keeps_nonempty_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    save_default_agent("claude")
    prefs = prefs_path()
    sibling = prefs.parent / "vaults.yaml"
    sibling.write_text("vaults: {}\n", encoding="utf-8")

    result = remove_prefs()

    assert result["removed"] is True
    assert result["dir_removed"] is False
    assert prefs.parent.is_dir()  # dir kept because vaults.yaml remains
    assert sibling.exists()


# --------------------------------------------------------------------------
# lit uninstall — shortcut + preferences coverage
# --------------------------------------------------------------------------


@pytest.mark.no_skills_isolation
def test_uninstall_yes_removes_shortcut_and_prefs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lit uninstall -y` must also carry off the desktop shortcut and the
    machine-level preferences.yaml — and, with both config files gone, the
    shared config dir itself."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    reg = _seed_artifacts(tmp_path)
    save_default_agent("claude")
    prefs = prefs_path()
    shortcut = _seed_shortcut(monkeypatch)

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert not shortcut.exists()
    assert not prefs.exists()
    # registry + preferences both gone → the shared config dir is removed too
    assert not reg.parent.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_yes_removes_ui_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6 (task-gui-pin): `lit uninstall -y` carries off ui-state.json too,
    and — it being the last sibling — rmdirs the shared config dir. Real
    delete sequence, no mocks."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    reg = _seed_artifacts(tmp_path)
    save_default_agent("claude")
    vault = tmp_path / "vault"
    vault.mkdir()
    save_pins(vault, ["some_paper"])
    state = ui_state_path()
    assert state.is_file()

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert not state.exists()
    # registry + prefs + ui-state all gone → the shared config dir too
    assert not reg.parent.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_dry_run_lists_and_keeps_ui_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    vault = tmp_path / "vault"
    vault.mkdir()
    save_pins(vault, ["some_paper"])

    result = CliRunner().invoke(cli, ["uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "GUI state" in normalized
    assert ui_state_path().is_file()


@pytest.mark.no_skills_isolation
def test_uninstall_dry_run_lists_shortcut_and_prefs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    save_default_agent("claude")
    shortcut = _seed_shortcut(monkeypatch)

    result = CliRunner().invoke(cli, ["uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "Desktop shortcut" in normalized
    assert "Agent preferences" in normalized
    # dry run changes nothing
    assert shortcut.exists()
    assert prefs_path().is_file()


# --------------------------------------------------------------------------
# GUI server records (core/gui_registry.py) — a killed `lit gui` leaves one
# --------------------------------------------------------------------------


def _redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))


def _seed_other_host_record() -> Path:
    """A record another machine sharing this config dir wrote."""
    record = gui_dir() / "other-node-4242.json"
    record.write_text('{"app": "litman", "host": "other-node"}\n', encoding="utf-8")
    return record


@pytest.mark.no_skills_isolation
def test_uninstall_removes_leftover_gui_records_and_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record a killed GUI left (this process stands in for it) and one
    another host left both go, and with them `gui/` and the config dir."""
    _redirect(tmp_path, monkeypatch)
    reg = _seed_artifacts(tmp_path)
    save_default_agent("claude")
    own = register_gui(8765, None)
    other = _seed_other_host_record()

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "GUI server records (2)" in normalized
    assert not own.exists()
    assert not other.exists()
    assert not gui_dir().exists()
    assert not reg.parent.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_with_only_a_gui_record_still_removes_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No registry/prefs/ui-state remover runs, so the records' own remover
    # has to take the config dir.
    _redirect(tmp_path, monkeypatch)
    record = register_gui(8765, None)

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Nothing to remove" not in result.output
    assert not record.exists()
    assert not registry_path().parent.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_without_gui_dir_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(tmp_path, monkeypatch)
    reg = _seed_artifacts(tmp_path)

    plan = CliRunner().invoke(cli, ["uninstall", "--dry-run"])
    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert plan.exit_code == 0, plan.output
    assert result.exit_code == 0, result.output
    assert "GUI server records" not in " ".join(plan.output.split())
    assert "GUI server records" not in " ".join(result.output.split())
    assert not gui_dir().exists()
    assert not reg.parent.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_dry_run_lists_and_keeps_gui_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(tmp_path, monkeypatch)
    record = register_gui(8765, None)

    result = CliRunner().invoke(cli, ["uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "GUI server records" in " ".join(result.output.split())
    assert record.is_file()


@pytest.mark.no_skills_isolation
def test_uninstall_keeps_a_foreign_file_in_the_gui_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only names litman writes are deleted; anything else keeps `gui/` and
    therefore the config dir, and the Removed list says so."""
    _redirect(tmp_path, monkeypatch)
    reg = _seed_artifacts(tmp_path)
    record = register_gui(8765, None)
    notes = gui_dir() / "notes.txt"
    notes.write_text("mine", encoding="utf-8")
    lookalike = gui_dir() / "export.json"
    lookalike.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "GUI server records (1) (kept 2 user file(s) + gui/ dir)" in normalized
    assert not record.exists()
    assert notes.read_text(encoding="utf-8") == "mine"
    assert lookalike.is_file()
    assert not reg.exists()
    assert reg.parent.is_dir()


def test_remove_gui_records_takes_a_stranded_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What a write killed between create and rename leaves beside the record.
    _redirect(tmp_path, monkeypatch)
    record = register_gui(8765, None)
    temp = record.with_name(f".{record.name}.{'0' * 32}.tmp")
    temp.write_text("{", encoding="utf-8")
    assert gui_record_files() == sorted([record, temp])

    result = remove_gui_records()

    assert result["removed"] == sorted([record, temp])
    assert result["kept"] == []
    assert result["dir_removed"] is True
    assert not registry_path().parent.exists()


def test_running_gui_exits_cleanly_after_uninstall_took_its_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Uninstall does not ask whether a GUI is still up; that GUI's own
    # shutdown then finds nothing to remove and must neither raise nor
    # recreate anything.
    _redirect(tmp_path, monkeypatch)
    record = register_gui(8765, None)
    remove_gui_records()

    unregister_gui(record)

    assert not gui_dir().exists()
    assert not registry_path().parent.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_keeps_a_record_named_file_litman_did_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record is ours by its name AND its `app` field: a lookalike name
    holding something else stays, while a record of another litman version
    (same `app`, different shape) goes."""
    _redirect(tmp_path, monkeypatch)
    record = register_gui(8765, None)
    other_app = gui_dir() / "node-12.json"
    other_app.write_text('{"app": "something-else"}', encoding="utf-8")
    not_json = gui_dir() / "node-13.json"
    not_json.write_text("not json", encoding="utf-8")
    older = gui_dir() / "node-14.json"
    older.write_text('{"app": "litman"}', encoding="utf-8")

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "GUI server records (2) (kept 2 user file(s) + gui/ dir)" in normalized
    assert not record.exists()
    assert not older.exists()
    assert other_app.is_file()
    assert not_json.read_text(encoding="utf-8") == "not json"


@pytest.mark.no_skills_isolation
def test_uninstall_removes_an_empty_gui_dir_and_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No record to delete, but the folder alone would keep the config dir.
    _redirect(tmp_path, monkeypatch)
    reg = _seed_artifacts(tmp_path)
    gui_dir().mkdir()

    plan = CliRunner().invoke(cli, ["uninstall", "--dry-run"])
    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert plan.exit_code == 0, plan.output
    assert "GUI server records" in " ".join(plan.output.split())
    assert result.exit_code == 0, result.output
    assert "GUI server records (empty folder)" in " ".join(result.output.split())
    assert not gui_dir().exists()
    assert not reg.parent.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_with_only_an_empty_gui_dir_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(tmp_path, monkeypatch)
    gui_dir().mkdir(parents=True)

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Nothing to remove" not in result.output
    assert not registry_path().parent.exists()


def test_remove_gui_records_counts_only_what_it_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A record listed a moment ago may be gone by the time it is unlinked
    # (that GUI exited and took it): it must not be reported as removed.
    _redirect(tmp_path, monkeypatch)
    record = register_gui(8765, None)
    vanished = gui_dir() / "gone-99.json"
    monkeypatch.setattr(gui_registry, "gui_record_files", lambda: [vanished, record])

    result = remove_gui_records()

    assert result["removed"] == [record]
    assert not record.exists()


# --------------------------------------------------------------------------
# update-check cache + self-update log (beside the registry)
# --------------------------------------------------------------------------


def _seed_update_leftovers() -> list[Path]:
    """The cache (written for real), a temp file a killed cache write left,
    and what a self-update helper writes: its log and its failure flag."""
    update_check._write_cache({"latest": "1.0.0"})
    stale_tmp = update_check.cache_path().with_name("update-check.json.tmp")
    stale_tmp.write_text("{", encoding="utf-8")
    log_path().write_text("[helper] upgrading\n", encoding="utf-8")
    fail_flag_path().write_text("", encoding="utf-8")
    return [update_check.cache_path(), stale_tmp, log_path(), fail_flag_path()]


@pytest.mark.no_skills_isolation
def test_uninstall_removes_update_check_and_self_update_files_and_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(tmp_path, monkeypatch)
    reg = _seed_artifacts(tmp_path)
    leftovers = _seed_update_leftovers()

    plan = CliRunner().invoke(cli, ["uninstall", "--dry-run"])
    assert plan.exit_code == 0, plan.output
    normalized = " ".join(plan.output.split())
    assert "Update-check cache" in normalized
    assert "Self-update log" in normalized
    assert all(p.is_file() for p in leftovers)

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    normalized = " ".join(result.output.split())
    assert "update-check cache" in normalized
    assert "self-update log (self-update.log, self-update-failed)" in normalized
    assert not any(p.exists() for p in leftovers)
    assert not reg.parent.exists()


@pytest.mark.no_skills_isolation
def test_uninstall_keeps_the_config_dir_for_a_file_litman_did_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(tmp_path, monkeypatch)
    reg = _seed_artifacts(tmp_path)
    leftovers = _seed_update_leftovers()
    mine = reg.parent / "notes.txt"
    mine.write_text("mine", encoding="utf-8")

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert not any(p.exists() for p in leftovers)
    assert mine.read_text(encoding="utf-8") == "mine"
    assert reg.parent.is_dir()


@pytest.mark.no_skills_isolation
def test_uninstall_on_a_terminal_does_not_write_the_update_cache_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After any other command on a terminal the update tip refreshes a
    missing cache — which after uninstall would recreate the config dir."""
    _redirect(tmp_path, monkeypatch)
    monkeypatch.delenv(update_check.OPT_OUT_ENV, raising=False)
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    fetched: list[int] = []

    def _fetch(**kw: object) -> str | None:
        fetched.append(1)
        return "9.9.9"

    monkeypatch.setattr(update_check, "_fetch_latest_version", _fetch)
    reg = _seed_artifacts(tmp_path)
    _seed_update_leftovers()

    result = CliRunner().invoke(cli, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert fetched == []
    assert "is available" not in result.output
    assert not update_check.cache_path().exists()
    assert not reg.parent.exists()
