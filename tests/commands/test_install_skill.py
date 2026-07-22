"""Tests for `lit install-skill` (M4.3 + M9.2 multi-skill support)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.skill import (
    DEFAULT_PARENT_DIR,
    SKILL_NAME,
    SkillInstallError,
    aggregate_skill_state,
    bundled_skill_root,
    install_all_skills,
    install_skill,
    list_bundled_skills,
    skill_status,
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


def test_install_skill_linked_target_untouched_even_with_force(
    tmp_path: Path,
) -> None:
    """A symlinked skill dir points at a copy managed elsewhere (a dev
    checkout); install must never copy *through* the link — not even with
    overwrite=True — or it would clobber the source of truth in place."""
    real = tmp_path / "checkout" / "lit-library"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("DEV COPY\n", encoding="utf-8")
    parent = tmp_path / "skills"
    parent.mkdir()
    link = parent / "lit-library"
    link.symlink_to(real)

    result = install_skill(target=link, overwrite=True, name="lit-library")
    assert result["mode"] == "linked"
    assert result["files"] == []
    assert (real / "SKILL.md").read_text(encoding="utf-8") == "DEV COPY\n"


# ---------------------------------------------------------------------------
# skill_status / aggregate_skill_state — content-level freshness probes
# ---------------------------------------------------------------------------


def test_skill_status_absent_for_empty_parent(tmp_path: Path) -> None:
    statuses = skill_status(parent_dir=tmp_path / "skills")
    assert set(statuses) == set(list_bundled_skills())
    assert all(s["state"] == "absent" for s in statuses.values())


def test_skill_status_current_after_install(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    install_all_skills(parent_dir=parent)
    statuses = skill_status(parent_dir=parent)
    assert all(s["state"] == "current" for s in statuses.values())
    assert all(s["stale_files"] == [] for s in statuses.values())


def test_skill_status_tampered_file_is_stale_and_named(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "skills"
    install_all_skills(parent_dir=parent)
    (parent / "lit-library" / "SKILL.md").write_text(
        "OUTDATED\n", encoding="utf-8"
    )
    statuses = skill_status(parent_dir=parent)
    assert statuses["lit-library"]["state"] == "stale"
    assert "SKILL.md" in statuses["lit-library"]["stale_files"]
    assert statuses["lit-reading"]["state"] == "current"


def test_skill_status_missing_bundled_file_is_stale(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    install_all_skills(parent_dir=parent)
    (parent / "lit-library" / "SKILL.md").unlink()
    assert skill_status(parent_dir=parent)["lit-library"]["state"] == "stale"


def test_skill_status_user_additions_do_not_affect_state(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "skills"
    install_all_skills(parent_dir=parent)
    (parent / "lit-library" / "my_local_notes.md").write_text(
        "mine\n", encoding="utf-8"
    )
    assert skill_status(parent_dir=parent)["lit-library"]["state"] == "current"


def test_skill_status_linked_dir_never_stale(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    parent.mkdir()
    real = tmp_path / "dev-checkout" / "lit-library"
    real.mkdir(parents=True)  # deliberately diverges from the bundle
    (parent / "lit-library").symlink_to(real)
    statuses = skill_status(parent_dir=parent)
    assert statuses["lit-library"]["state"] == "linked"
    assert statuses["lit-library"]["stale_files"] == []


def test_aggregate_skill_state_absent(tmp_path: Path) -> None:
    assert aggregate_skill_state(parent_dir=tmp_path / "skills") == "absent"


def test_aggregate_skill_state_current(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    install_all_skills(parent_dir=parent)
    assert aggregate_skill_state(parent_dir=parent) == "current"


def test_aggregate_skill_state_any_stale_wins(tmp_path: Path) -> None:
    parent = tmp_path / "skills"
    install_all_skills(parent_dir=parent)
    (parent / "lit-reading" / "SKILL.md").write_text("OLD\n", encoding="utf-8")
    assert aggregate_skill_state(parent_dir=parent) == "stale"


def test_aggregate_skill_state_partial_install_counts_current(
    tmp_path: Path,
) -> None:
    """Installing only one bundled skill is a deliberate choice
    (``--skill``), not drift — the GUI must not nag about the other."""
    parent = tmp_path / "skills"
    install_skill(target=parent / "lit-library", name="lit-library")
    assert aggregate_skill_state(parent_dir=parent) == "current"


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


def test_cli_install_skill_rerun_reports_up_to_date(tmp_path: Path) -> None:
    """Re-running after a clean install is a no-op success, not an error —
    the upgrade path (`lit install-skill` after `pipx upgrade litman`) must
    not require --force when there is nothing to refresh."""
    parent = tmp_path / "skills"
    runner = CliRunner()
    assert (
        runner.invoke(cli, ["install-skill", "--parent-dir", str(parent)])
        .exit_code
        == 0
    )
    result = runner.invoke(
        cli, ["install-skill", "--parent-dir", str(parent)]
    )
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
    assert "lit-library" in result.output
    assert "lit-reading" in result.output


def test_cli_install_skill_stale_non_tty_requires_force(
    tmp_path: Path,
) -> None:
    """Out-of-date content + nobody at the keyboard → refuse loudly with a
    --force pointer, and touch nothing (an agent or script must never
    overwrite skill files silently)."""
    parent = tmp_path / "skills"
    runner = CliRunner()
    runner.invoke(cli, ["install-skill", "--parent-dir", str(parent)])
    stale_md = parent / "lit-library" / "SKILL.md"
    stale_md.write_text("OLD LOCAL COPY\n", encoding="utf-8")

    result = runner.invoke(
        cli, ["install-skill", "--parent-dir", str(parent)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, SkillInstallError)
    assert "--force" in str(result.exception)
    assert "lit-library" in str(result.exception)
    assert stale_md.read_text(encoding="utf-8") == "OLD LOCAL COPY\n"


def test_cli_install_skill_stale_tty_enter_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive run + stale skill → [Y/n] prompt whose default (plain
    Enter) refreshes. The drift surfaces at the next relevant operation and
    one keypress fixes it."""
    parent = tmp_path / "skills"
    runner = CliRunner()
    runner.invoke(cli, ["install-skill", "--parent-dir", str(parent)])
    stale_md = parent / "lit-library" / "SKILL.md"
    stale_md.write_text("OLD LOCAL COPY\n", encoding="utf-8")
    monkeypatch.setattr(
        "litman.commands.install_skill._stdin_is_tty", lambda: True
    )

    result = runner.invoke(
        cli, ["install-skill", "--parent-dir", str(parent)], input="\n"
    )
    assert result.exit_code == 0, result.output
    assert "overwritten" in result.output
    text = stale_md.read_text(encoding="utf-8")
    assert "OLD LOCAL COPY" not in text
    assert "name: lit-library" in text


def test_cli_install_skill_stale_tty_decline_leaves_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "skills"
    runner = CliRunner()
    runner.invoke(cli, ["install-skill", "--parent-dir", str(parent)])
    stale_md = parent / "lit-library" / "SKILL.md"
    stale_md.write_text("OLD LOCAL COPY\n", encoding="utf-8")
    monkeypatch.setattr(
        "litman.commands.install_skill._stdin_is_tty", lambda: True
    )

    result = runner.invoke(
        cli, ["install-skill", "--parent-dir", str(parent)], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output
    assert stale_md.read_text(encoding="utf-8") == "OLD LOCAL COPY\n"


def test_cli_install_skill_linked_left_untouched_even_with_force(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "skills"
    parent.mkdir(parents=True)
    real = tmp_path / "checkout" / "lit-library"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("DEV COPY\n", encoding="utf-8")
    (parent / "lit-library").symlink_to(real)

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
    assert "linked" in result.output
    assert (real / "SKILL.md").read_text(encoding="utf-8") == "DEV COPY\n"


def test_cli_install_skill_help_mentions_options() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["install-skill", "--help"])
    assert result.exit_code == 0
    assert "--skill" in result.output
    assert "--agent" in result.output
    assert "--parent-dir" in result.output
    assert "--force" in result.output
    assert "optional" in result.output.lower()
    # No hardcoded skills path in the help — the default follows the
    # default agent, resolved at run time.
    assert ".claude/skills" not in result.output


# ---------------------------------------------------------------------------
# CLI: --agent + default-agent resolution (task-multi-agent-skills)
# ---------------------------------------------------------------------------


def test_cli_install_skill_agent_cursor_writes_standard_dir() -> None:
    """--agent cursor resolves the open-standard dir through the catalog
    (all three resolvers isolated at tmp by the conftest fixture)."""
    from litman.core import skill

    result = CliRunner().invoke(cli, ["install-skill", "--agent", "cursor"])
    assert result.exit_code == 0, result.output
    standard = skill.standard_skills_parent_dir()
    for name in list_bundled_skills():
        assert (standard / name / "SKILL.md").is_file()
    assert not skill.default_skills_parent_dir().exists()
    assert not skill.antigravity_skills_parent_dir().exists()


@pytest.mark.no_skills_isolation
def test_cli_agent_install_also_adds_only_that_agents_lit_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = CliRunner().invoke(cli, ["install-skill", "--agent", "cursor"])

    assert result.exit_code == 0, result.output
    assert "lit command approval created" in result.output
    config = json.loads(
        (home / ".cursor" / "cli-config.json").read_text(encoding="utf-8")
    )
    assert config == {"permissions": {"allow": ["Shell(lit)"]}}
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".codex" / "rules" / "litman.rules").exists()


def test_cli_install_skill_agent_agy_writes_antigravity_dir() -> None:
    """--agent agy resolves the Antigravity CLI app-data dir through the
    catalog — NOT the open-standard dir, which agy does not read."""
    from litman.core import skill

    result = CliRunner().invoke(cli, ["install-skill", "--agent", "agy"])
    assert result.exit_code == 0, result.output
    antigravity = skill.antigravity_skills_parent_dir()
    for name in list_bundled_skills():
        assert (antigravity / name / "SKILL.md").is_file()
    assert not skill.default_skills_parent_dir().exists()
    assert not skill.standard_skills_parent_dir().exists()


def test_cli_install_skill_agent_and_parent_dir_conflict(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "install-skill",
            "--agent",
            "cursor",
            "--parent-dir",
            str(tmp_path / "skills"),
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    assert not (tmp_path / "skills").exists()


def test_cli_install_skill_agent_unknown_name_lists_supported() -> None:
    result = CliRunner().invoke(cli, ["install-skill", "--agent", "nope"])
    assert result.exit_code != 0
    # click.Choice error names the valid agents (the full supported set).
    assert "claude" in result.output
    assert "agy" in result.output
    assert "codex" in result.output
    assert "cursor" in result.output
    assert "opencode" in result.output


def test_cli_install_skill_bare_defaults_to_claude_dir() -> None:
    """No flags, no recorded default → the resolved default is claude and
    the skills land in the Claude Code dir (byte-for-byte the pre-1.3
    behaviour)."""
    from litman.core import skill

    result = CliRunner().invoke(cli, ["install-skill"])
    assert result.exit_code == 0, result.output
    claude_dir = skill.default_skills_parent_dir()
    for name in list_bundled_skills():
        assert (claude_dir / name / "SKILL.md").is_file()
    assert not skill.standard_skills_parent_dir().exists()
    assert not skill.antigravity_skills_parent_dir().exists()


def test_cli_install_skill_bare_follows_recorded_default() -> None:
    """With the machine default set to agy, the bare command installs into
    the Antigravity CLI dir — otherwise it would silently install to the
    wrong place for that user."""
    from litman.core import agent_prefs, skill

    agent_prefs.save_default_agent("agy")  # registry dir is isolated
    result = CliRunner().invoke(cli, ["install-skill"])
    assert result.exit_code == 0, result.output
    antigravity = skill.antigravity_skills_parent_dir()
    for name in list_bundled_skills():
        assert (antigravity / name / "SKILL.md").is_file()
    assert not skill.default_skills_parent_dir().exists()
    assert not skill.standard_skills_parent_dir().exists()


def test_cli_install_skill_explicit_parent_dir_still_wins(
    tmp_path: Path,
) -> None:
    """--parent-dir stays the manual escape hatch: an explicit path is used
    verbatim, whatever the recorded default agent."""
    from litman.core import agent_prefs

    agent_prefs.save_default_agent("agy")
    parent = tmp_path / "elsewhere"
    result = CliRunner().invoke(
        cli, ["install-skill", "--parent-dir", str(parent)]
    )
    assert result.exit_code == 0, result.output
    assert (parent / "lit-library" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# CLI: cross-directory refresh sweep (bare runs only)
# ---------------------------------------------------------------------------


def _tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "litman.commands.install_skill._stdin_is_tty", lambda: True
    )


def _plant_stale_standard_copy() -> Path:
    """Install the bundle into the (conftest-isolated) open-standard dir and
    tamper one file — the classic 'stale copy for another agent'. Returns
    the tampered SKILL.md path."""
    from litman.core import skill

    standard = skill.standard_skills_parent_dir()
    install_all_skills(parent_dir=standard)
    stale_md = standard / "lit-library" / "SKILL.md"
    stale_md.write_text("OLD OTHER-AGENT COPY\n", encoding="utf-8")
    return stale_md


def test_sweep_tty_enter_refreshes_other_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare interactive run: a stale copy in ANOTHER agent's dir gets its
    own [Y/n] prompt whose default (plain Enter) refreshes it — a stale
    shadow copy is one keypress from fresh."""
    stale_md = _plant_stale_standard_copy()
    _tty(monkeypatch)

    result = CliRunner().invoke(cli, ["install-skill"], input="\n")
    assert result.exit_code == 0, result.output
    assert "Also refreshed" in result.output
    text = stale_md.read_text(encoding="utf-8")
    assert "OLD OTHER-AGENT COPY" not in text
    assert "name: lit-library" in text


def test_sweep_tty_decline_leaves_other_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_md = _plant_stale_standard_copy()
    _tty(monkeypatch)

    result = CliRunner().invoke(cli, ["install-skill"], input="n\n")
    assert result.exit_code == 0, result.output
    assert stale_md.read_text(encoding="utf-8") == "OLD OTHER-AGENT COPY\n"
    assert "Skipped" in result.output


def test_sweep_non_tty_reports_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-interactive without --force: the main install proceeds (its own
    target is not stale) but the other dir's stale copy is only reported,
    never overwritten silently — same contract as the main target's
    non-TTY stale gate."""
    stale_md = _plant_stale_standard_copy()

    result = CliRunner().invoke(cli, ["install-skill"])
    assert result.exit_code == 0, result.output
    assert stale_md.read_text(encoding="utf-8") == "OLD OTHER-AGENT COPY\n"
    assert "left untouched" in result.output
    assert "--force" in result.output


def test_sweep_force_refreshes_without_prompt() -> None:
    stale_md = _plant_stale_standard_copy()

    result = CliRunner().invoke(cli, ["install-skill", "--force"])
    assert result.exit_code == 0, result.output
    assert "Also refreshed" in result.output
    assert "OLD OTHER-AGENT COPY" not in stale_md.read_text(encoding="utf-8")


def test_sweep_skipped_with_explicit_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--agent is a precise instruction: no sweep, no prompt, the other
    dir's stale copy stays (an unconsumed confirm would abort — input is
    deliberately empty)."""
    stale_md = _plant_stale_standard_copy()
    _tty(monkeypatch)

    result = CliRunner().invoke(cli, ["install-skill", "--agent", "claude"])
    assert result.exit_code == 0, result.output
    assert stale_md.read_text(encoding="utf-8") == "OLD OTHER-AGENT COPY\n"
    assert "Also refreshed" not in result.output
    assert "left untouched" not in result.output


def test_sweep_skipped_with_explicit_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale_md = _plant_stale_standard_copy()
    _tty(monkeypatch)

    result = CliRunner().invoke(
        cli, ["install-skill", "--parent-dir", str(tmp_path / "elsewhere")]
    )
    assert result.exit_code == 0, result.output
    assert stale_md.read_text(encoding="utf-8") == "OLD OTHER-AGENT COPY\n"
    assert "Also refreshed" not in result.output


def test_sweep_never_first_installs_absent_dirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing installed elsewhere → the sweep neither prompts (empty input
    would abort a confirm) nor creates the other directories: absent is a
    respected opt-out."""
    from litman.core import skill

    _tty(monkeypatch)
    result = CliRunner().invoke(cli, ["install-skill"], input="")
    assert result.exit_code == 0, result.output
    assert not skill.standard_skills_parent_dir().exists()
    assert not skill.antigravity_skills_parent_dir().exists()


def test_sweep_leaves_linked_copies_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linked skill dir in another agent's location is dev-managed:
    diverging is the point, the sweep neither prompts nor copies through
    the link."""
    from litman.core import skill

    real = tmp_path / "checkout" / "lit-library"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("DEV COPY\n", encoding="utf-8")
    standard = skill.standard_skills_parent_dir()
    standard.mkdir(parents=True)
    (standard / "lit-library").symlink_to(real)
    _tty(monkeypatch)

    result = CliRunner().invoke(cli, ["install-skill"], input="")
    assert result.exit_code == 0, result.output
    assert (real / "SKILL.md").read_text(encoding="utf-8") == "DEV COPY\n"


@pytest.mark.no_skills_isolation
def test_sweep_closes_cursor_claude_shadowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """default=cursor + a dirty ~/.claude copy — the copy Cursor actually
    prefers over the open-standard dir — must be one Enter from fresh on a
    bare install-skill run, or Cursor keeps executing the stale skill while
    every status view shows green. Drives the REAL resolvers ($HOME
    redirect only)."""
    from litman.core import agent_prefs

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    install_all_skills(parent_dir=home / ".claude" / "skills")
    claude_md = home / ".claude" / "skills" / "lit-library" / "SKILL.md"
    claude_md.write_text("STALE SHADOWING COPY\n", encoding="utf-8")
    install_all_skills(parent_dir=home / ".agents" / "skills")
    agent_prefs.save_default_agent("cursor")  # registry dir is isolated
    _tty(monkeypatch)

    result = CliRunner().invoke(cli, ["install-skill"], input="\n")
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output  # the cursor dir itself was fine
    assert "Also refreshed" in result.output
    text = claude_md.read_text(encoding="utf-8")
    assert "STALE SHADOWING COPY" not in text
    assert "name: lit-library" in text
