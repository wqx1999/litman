"""HOME-only skill-lifecycle tests (inject-seam / M34 lesson).

Every other skill test isolates through the resolver seams (the conftest
``_isolate_skills_dir`` patch or explicit ``parent_dir=``). If the live
resolver chain broke — ``default_skills_parent_dir`` /
``standard_skills_parent_dir`` themselves, or the catalog wiring on top of
them — those tests would stay green while every real install landed in the
wrong place. These tests therefore patch NOTHING inside litman: only
``$HOME`` is redirected (honored by ``Path.home()`` at call time), and the
full install → status → drift → fix → uninstall cycle runs through the real
resolvers, once per supported skills location (the Claude Code dir and the
shared open-standard dir).

``no_skills_isolation`` opts out of the autouse seam patch; the redirected
``$HOME`` is what keeps the developer's real skills dirs out of reach. The
autouse ``_isolate_registry`` fixture (an env var, not a litman seam) keeps
preferences.yaml isolated the same way it does for every test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core import agent_prefs, agents
from litman.core.checks import apply_autofix, check_skill_drift
from litman.core.library import create_vault
from litman.core.skill import list_bundled_skills


@pytest.mark.no_skills_isolation
@pytest.mark.parametrize(
    ("agent", "rel_dir"),
    [
        ("claude", Path(".claude") / "skills"),
        ("gemini", Path(".agents") / "skills"),
    ],
)
def test_full_skill_lifecycle_through_real_resolvers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    rel_dir: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = home / rel_dir
    runner = CliRunner()

    # Record the default the same way setup/GUI do (real prefs write).
    agent_prefs.save_default_agent(agent)

    # 1. install — the bare command follows the recorded default through
    # the real resolver chain.
    result = runner.invoke(cli, ["install-skill"])
    assert result.exit_code == 0, result.output
    for name in list_bundled_skills():
        assert (target / name / "SKILL.md").is_file()
    # ... and nothing landed anywhere else under home.
    other = {Path(".claude") / "skills", Path(".agents") / "skills"} - {rel_dir}
    for rel in other:
        assert not (home / rel).exists()

    # 2. status — the catalog adapter's live probe agrees, and a re-run is
    # the idempotent no-op success.
    assert agents.get_agent(agent).skill_state() == "current"
    rerun = runner.invoke(cli, ["install-skill"])
    assert rerun.exit_code == 0, rerun.output
    assert "up to date" in rerun.output

    # 3. drift — tamper one installed file; the health probe (which resolves
    # the default agent's dir itself) detects it, --fix's autofix arm
    # refreshes it losslessly, and the post-fix pass is clean.
    vault = create_vault(tmp_path)
    tampered = target / "lit-library" / "SKILL.md"
    tampered.write_text("OUTDATED\n", encoding="utf-8")
    user_file = target / "lit-library" / "my-notes.md"
    user_file.write_text("mine\n", encoding="utf-8")

    issues = check_skill_drift(vault, [])
    assert len(issues) == 1
    assert "lit-library" in issues[0].message
    counts = apply_autofix(vault, issues)
    assert counts["skill_drift"] == 1
    assert "OUTDATED" not in tampered.read_text(encoding="utf-8")
    assert user_file.read_text(encoding="utf-8") == "mine\n"
    assert check_skill_drift(vault, []) == []

    # 4. uninstall — the full sweep clears the bundled files but keeps the
    # user's own (dir survives with the leftover).
    result = runner.invoke(cli, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (target / "lit-reading").exists()
    assert not (target / "lit-library" / "SKILL.md").exists()
    assert user_file.read_text(encoding="utf-8") == "mine\n"
