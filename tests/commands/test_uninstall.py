"""Tests for ``lit uninstall`` and its teardown helpers.

Isolation: ``HOME`` is redirected to ``tmp_path`` (so ``Path.home()`` and the
skill/completion paths land in a scratch dir) and ``LITMAN_REGISTRY_DIR``
points the vaults.yaml registry into the same scratch dir. Nothing touches
the developer's real ~/.claude, ~/.bashrc, or config dir.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands.install_completion import (
    _build_plan,
    _install_block,
    completion_installed,
    uninstall_completion,
)
from litman.core.library import create_vault
from litman.core.skill import (
    install_all_skills,
    install_skill,
    list_bundled_skills,
    uninstall_skill,
)
from litman.core.vault_registry import (
    add_vault,
    load_registry,
    registry_path,
    remove_registry,
    save_registry,
)

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


def test_uninstall_decline_aborts_without_removing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))
    _seed_artifacts(tmp_path)

    result = CliRunner().invoke(cli, ["uninstall"], input="n\n")

    assert result.exit_code != 0  # click.Abort on a declined confirm
    assert (tmp_path / ".claude" / "skills" / "lit-library").exists()
    assert completion_installed("bash", tmp_path)


def test_uninstall_nothing_to_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(tmp_path / "cfg"))

    result = CliRunner().invoke(cli, ["uninstall"])

    assert result.exit_code == 0
    assert "Nothing to remove" in result.output
    assert "pipx uninstall litman" in result.output


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
    assert link.is_symlink()  # link left in place
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
