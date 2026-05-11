"""Tests for `lit install-skill` (M4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.skill import (
    DEFAULT_TARGET,
    SKILL_NAME,
    SkillInstallError,
    bundled_skill_root,
    install_skill,
)


# ---------------------------------------------------------------------------
# Bundled-resource discovery
# ---------------------------------------------------------------------------


def test_bundled_skill_root_exists() -> None:
    root = bundled_skill_root()
    assert root.is_dir()


def test_bundled_skill_contains_skill_md() -> None:
    root = bundled_skill_root()
    names = [c.name for c in root.iterdir() if c.is_file()]
    assert "SKILL.md" in names


def test_bundled_skill_md_has_frontmatter() -> None:
    """SKILL.md must start with the YAML frontmatter Claude Code parses."""
    root = bundled_skill_root()
    skill_md = next(c for c in root.iterdir() if c.name == "SKILL.md")
    body = skill_md.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "name: lit-library" in body
    assert "description:" in body


def test_skill_name_constant_matches_dir() -> None:
    assert SKILL_NAME == "lit-library"


def test_default_target_under_home_claude() -> None:
    assert DEFAULT_TARGET.parts[-3:] == (".claude", "skills", "lit-library")


# ---------------------------------------------------------------------------
# install_skill (pure function, no CLI)
# ---------------------------------------------------------------------------


def test_install_skill_creates_target(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    result = install_skill(target=target)
    assert result["mode"] == "created"
    assert target.is_dir()
    assert (target / "SKILL.md").is_file()
    assert "SKILL.md" in result["files"]


def test_install_skill_creates_parents(tmp_path: Path) -> None:
    """Parent dirs of target are auto-created (mkdir parents=True)."""
    target = tmp_path / "deeply" / "nested" / "claude" / "skills" / "lit-library"
    install_skill(target=target)
    assert (target / "SKILL.md").is_file()


def test_install_skill_refuses_existing_without_force(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    target.mkdir(parents=True)
    with pytest.raises(SkillInstallError, match="already exists"):
        install_skill(target=target)


def test_install_skill_overwrites_with_force(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("STALE CONTENT", encoding="utf-8")
    result = install_skill(target=target, overwrite=True)
    assert result["mode"] == "overwritten"
    assert "STALE CONTENT" not in (target / "SKILL.md").read_text(encoding="utf-8")
    assert "name: lit-library" in (target / "SKILL.md").read_text(encoding="utf-8")


def test_install_skill_preserves_user_additions(tmp_path: Path) -> None:
    """Files in target that are NOT part of the bundle are left alone."""
    target = tmp_path / "skills" / "lit-library"
    target.mkdir(parents=True)
    user_file = target / "my_local_notes.md"
    user_file.write_text("personal extension\n", encoding="utf-8")
    install_skill(target=target, overwrite=True)
    # SKILL.md is overwritten with the bundled version...
    assert (target / "SKILL.md").is_file()
    # ...but the user's own file survives.
    assert user_file.is_file()
    assert "personal extension" in user_file.read_text(encoding="utf-8")


def test_install_skill_copies_bytes_faithfully(tmp_path: Path) -> None:
    """The on-disk SKILL.md must be byte-identical to the bundled copy."""
    target = tmp_path / "skills" / "lit-library"
    install_skill(target=target)
    bundled = bundled_skill_root() / "SKILL.md"
    assert (target / "SKILL.md").read_bytes() == bundled.read_bytes()


# ---------------------------------------------------------------------------
# CLI: lit install-skill
# ---------------------------------------------------------------------------


def test_cli_install_skill_happy_path(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["install-skill", "--target", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert (target / "SKILL.md").is_file()
    assert "Skill created" in result.output
    assert "lit-library" in result.output


def test_cli_install_skill_refuses_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    target.mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["install-skill", "--target", str(target)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, SkillInstallError)


def test_cli_install_skill_force_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("STALE", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["install-skill", "--target", str(target), "--force"]
    )
    assert result.exit_code == 0, result.output
    assert "Skill overwritten" in result.output
    assert "STALE" not in (target / "SKILL.md").read_text(encoding="utf-8")


def test_cli_install_skill_help_mentions_optional() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["install-skill", "--help"])
    assert result.exit_code == 0
    assert "--target" in result.output
    assert "--force" in result.output
    assert "optional" in result.output.lower()
