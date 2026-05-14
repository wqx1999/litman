"""Tests for `lit install-skill` (M4.3 + M9.2 multi-skill support)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.skill import (
    DEFAULT_PARENT_DIR,
    SKILL_NAME,
    SkillInstallError,
    bundled_skill_root,
    install_all_skills,
    install_skill,
    list_bundled_skills,
)


# ---------------------------------------------------------------------------
# Bundled-resource discovery
# ---------------------------------------------------------------------------


def test_list_bundled_skills_includes_lit_library_and_reading() -> None:
    skills = list_bundled_skills()
    assert "lit-library" in skills
    assert "lit-reading" in skills


def test_list_bundled_skills_is_sorted() -> None:
    skills = list_bundled_skills()
    assert skills == sorted(skills)


def test_bundled_skill_root_default_is_lit_library() -> None:
    """Default name argument matches legacy ``SKILL_NAME``."""
    root = bundled_skill_root()
    assert root.is_dir()
    assert root.name == "lit-library"


def test_bundled_skill_root_lit_reading_exists() -> None:
    root = bundled_skill_root("lit-reading")
    assert root.is_dir()
    names = [c.name for c in root.iterdir() if c.is_file()]
    assert "SKILL.md" in names


def test_bundled_skill_root_unknown_name_raises() -> None:
    with pytest.raises(SkillInstallError, match="No bundled skill"):
        bundled_skill_root("does-not-exist")


def test_bundled_skill_md_has_frontmatter() -> None:
    """Every bundled SKILL.md must start with YAML frontmatter Claude
    Code's router parses (name + description)."""
    for name in list_bundled_skills():
        root = bundled_skill_root(name)
        skill_md = next(c for c in root.iterdir() if c.name == "SKILL.md")
        body = skill_md.read_text(encoding="utf-8")
        assert body.startswith("---\n"), f"{name}: no frontmatter"
        assert f"name: {name}" in body, f"{name}: frontmatter name mismatch"
        assert "description:" in body, f"{name}: no description"


def test_skill_name_constant_for_legacy_callers() -> None:
    """Legacy ``SKILL_NAME`` constant still resolves to lit-library."""
    assert SKILL_NAME == "lit-library"


def test_default_parent_dir_under_home_claude() -> None:
    assert DEFAULT_PARENT_DIR.parts[-2:] == (".claude", "skills")


# ---------------------------------------------------------------------------
# install_skill (single)
# ---------------------------------------------------------------------------


def test_install_skill_creates_target(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    result = install_skill(target=target, name="lit-library")
    assert result["mode"] == "created"
    assert result["name"] == "lit-library"
    assert target.is_dir()
    assert (target / "SKILL.md").is_file()
    assert "SKILL.md" in result["files"]


def test_install_skill_lit_reading(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-reading"
    result = install_skill(target=target, name="lit-reading")
    assert result["mode"] == "created"
    assert result["name"] == "lit-reading"
    assert (target / "SKILL.md").is_file()
    body = (target / "SKILL.md").read_text(encoding="utf-8")
    assert "name: lit-reading" in body


def test_install_skill_creates_parents(tmp_path: Path) -> None:
    """Parent dirs of target are auto-created (mkdir parents=True)."""
    target = (
        tmp_path / "deeply" / "nested" / "claude" / "skills" / "lit-library"
    )
    install_skill(target=target, name="lit-library")
    assert (target / "SKILL.md").is_file()


def test_install_skill_default_target_under_parent_dir() -> None:
    """target=None resolves to DEFAULT_PARENT_DIR / name."""
    # Just exercise the path-construction branch; we don't actually
    # want to write into ``~/.claude`` from a test. Use a name lookup
    # that fails after path construction to confirm the default is
    # consumed but no write happens.
    with pytest.raises(SkillInstallError):
        install_skill(target=None, name="does-not-exist")


def test_install_skill_refuses_existing_without_force(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    target.mkdir(parents=True)
    with pytest.raises(SkillInstallError, match="already exists"):
        install_skill(target=target, name="lit-library")


def test_install_skill_overwrites_with_force(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "lit-library"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("STALE CONTENT", encoding="utf-8")
    result = install_skill(
        target=target, overwrite=True, name="lit-library"
    )
    assert result["mode"] == "overwritten"
    text = (target / "SKILL.md").read_text(encoding="utf-8")
    assert "STALE CONTENT" not in text
    assert "name: lit-library" in text


def test_install_skill_preserves_user_additions(tmp_path: Path) -> None:
    """Files in target that are NOT part of the bundle are left alone."""
    target = tmp_path / "skills" / "lit-library"
    target.mkdir(parents=True)
    user_file = target / "my_local_notes.md"
    user_file.write_text("personal extension\n", encoding="utf-8")
    install_skill(target=target, overwrite=True, name="lit-library")
    assert (target / "SKILL.md").is_file()
    assert user_file.is_file()
    assert "personal extension" in user_file.read_text(encoding="utf-8")


def test_install_skill_copies_bytes_faithfully(tmp_path: Path) -> None:
    """On-disk SKILL.md must be byte-identical to the bundled copy."""
    target = tmp_path / "skills" / "lit-library"
    install_skill(target=target, name="lit-library")
    bundled = bundled_skill_root("lit-library") / "SKILL.md"
    assert (target / "SKILL.md").read_bytes() == bundled.read_bytes()


def test_install_skill_unknown_name_raises(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "ghost"
    with pytest.raises(SkillInstallError, match="No bundled skill"):
        install_skill(target=target, name="ghost-skill")


# ---------------------------------------------------------------------------
# install_all_skills
# ---------------------------------------------------------------------------


def test_install_all_skills_installs_every_bundled(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    results = install_all_skills(parent_dir=parent)
    installed_names = {r["name"] for r in results}
    assert installed_names == set(list_bundled_skills())
    for r in results:
        assert (r["target"] / "SKILL.md").is_file()


def test_install_all_skills_stops_on_collision(tmp_path: Path) -> None:
    """If one of the targets exists, the batch raises before touching
    the rest. Documents the no-partial-installs guarantee."""
    parent = tmp_path / "skills"
    # Pre-create one of the skill dirs so it collides.
    (parent / "lit-library").mkdir(parents=True)
    with pytest.raises(SkillInstallError, match="already exists"):
        install_all_skills(parent_dir=parent)


def test_install_all_skills_overwrite_flag(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    install_all_skills(parent_dir=parent)
    # Mutate one to confirm overwrite=True replaces it.
    (parent / "lit-library" / "SKILL.md").write_text("STALE", encoding="utf-8")
    results = install_all_skills(parent_dir=parent, overwrite=True)
    assert all(r["mode"] == "overwritten" for r in results)
    text = (parent / "lit-library" / "SKILL.md").read_text(encoding="utf-8")
    assert "STALE" not in text


# ---------------------------------------------------------------------------
# CLI: lit install-skill
# ---------------------------------------------------------------------------


def test_cli_install_skill_default_installs_all(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["install-skill", "--parent-dir", str(parent)]
    )
    assert result.exit_code == 0, result.output
    # Both bundled skills land on disk.
    assert (parent / "lit-library" / "SKILL.md").is_file()
    assert (parent / "lit-reading" / "SKILL.md").is_file()
    assert "lit-library" in result.output
    assert "lit-reading" in result.output


def test_cli_install_skill_single(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "install-skill",
            "--skill",
            "lit-reading",
            "--parent-dir",
            str(parent),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (parent / "lit-reading" / "SKILL.md").is_file()
    # Other skill should NOT have been installed in single mode.
    assert not (parent / "lit-library").exists()


def test_cli_install_skill_refuses_existing_target(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    (parent / "lit-library").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["install-skill", "--parent-dir", str(parent)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, SkillInstallError)


def test_cli_install_skill_force_overwrites(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    (parent / "lit-library").mkdir(parents=True)
    (parent / "lit-library" / "SKILL.md").write_text(
        "STALE", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "install-skill",
            "--skill",
            "lit-library",
            "--parent-dir",
            str(parent),
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "overwritten" in result.output
    text = (parent / "lit-library" / "SKILL.md").read_text(encoding="utf-8")
    assert "STALE" not in text


def test_cli_install_skill_unknown_name_exits_nonzero(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "skills"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "install-skill",
            "--skill",
            "ghost",
            "--parent-dir",
            str(parent),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, SkillInstallError)


def test_cli_install_skill_help_mentions_options() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["install-skill", "--help"])
    assert result.exit_code == 0
    assert "--skill" in result.output
    assert "--parent-dir" in result.output
    assert "--force" in result.output
    assert "optional" in result.output.lower()
