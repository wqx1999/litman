"""Machine-level agent-preference tests (task-agent-onboarding, AC4).

Covers the ``preferences.yaml`` round-trip, ``$LITMAN_REGISTRY_DIR``
redirection (the same env var the autouse ``_isolate_registry`` fixture sets,
so these run against a tmp dir automatically), the supported-only validation
in ``save_default_agent``, and the None-on-missing/garbage contract of
``load_default_agent``.

The D0 config-retirement half of AC4 (LitConfig no longer has agents /
default_agent, and a config carrying legacy keys still loads) lives in
``tests/commands/test_config.py`` next to the other loader tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from litman.core import agent_prefs
from litman.core.vault_registry import REGISTRY_ENV_VAR


def test_prefs_path_honors_registry_env(tmp_path: Path) -> None:
    """The autouse fixture points $LITMAN_REGISTRY_DIR at a tmp dir; prefs
    must resolve under it (so isolation and registry symmetry hold)."""
    override = os.environ[REGISTRY_ENV_VAR]
    assert agent_prefs.prefs_path() == Path(override) / "preferences.yaml"


def test_round_trip_save_then_load() -> None:
    assert agent_prefs.load_default_agent() is None  # fresh tmp dir, no file
    agent_prefs.save_default_agent("claude")
    assert agent_prefs.prefs_path().is_file()
    assert agent_prefs.load_default_agent() == "claude"


def test_load_missing_file_returns_none() -> None:
    assert not agent_prefs.prefs_path().exists()
    assert agent_prefs.load_default_agent() is None


def test_load_non_mapping_returns_none() -> None:
    path = agent_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert agent_prefs.load_default_agent() is None


def test_load_missing_key_returns_none() -> None:
    path = agent_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("something_else: 1\n", encoding="utf-8")
    assert agent_prefs.load_default_agent() is None


def test_load_garbage_yaml_returns_none() -> None:
    path = agent_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not: : valid: yaml: [", encoding="utf-8")
    assert agent_prefs.load_default_agent() is None


def test_save_rejects_unknown_agent() -> None:
    with pytest.raises(ValueError, match="not a supported agent"):
        agent_prefs.save_default_agent("nope")


def test_save_is_atomic_no_tmp_left_behind() -> None:
    agent_prefs.save_default_agent("claude")
    path = agent_prefs.prefs_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# The saved way of starting an agent (task-agent-launch-modes)
# ---------------------------------------------------------------------------


def test_launch_mode_round_trip_per_agent() -> None:
    assert agent_prefs.load_launch_modes() == {}
    agent_prefs.save_launch_mode("claude", "app")
    assert agent_prefs.load_launch_modes() == {"claude": "app"}
    agent_prefs.save_launch_mode("codex", "terminal")
    agent_prefs.save_launch_mode("claude", "terminal")
    assert agent_prefs.load_launch_modes() == {
        "claude": "terminal",
        "codex": "terminal",
    }


def test_saving_the_default_keeps_the_launch_modes() -> None:
    agent_prefs.save_launch_mode("claude", "app")
    agent_prefs.save_default_agent("codex")
    assert agent_prefs.load_launch_modes() == {"claude": "app"}
    assert agent_prefs.load_default_agent() == "codex"


def test_saving_a_launch_mode_keeps_the_default() -> None:
    agent_prefs.save_default_agent("codex")
    agent_prefs.save_launch_mode("claude", "app")
    assert agent_prefs.load_default_agent() == "codex"
    assert agent_prefs.load_launch_modes() == {"claude": "app"}


def test_saving_keeps_a_key_it_does_not_know() -> None:
    path = agent_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("from_a_newer_litman: 1\n", encoding="utf-8")
    agent_prefs.save_launch_mode("claude", "app")
    agent_prefs.save_default_agent("claude")
    assert "from_a_newer_litman: 1" in path.read_text(encoding="utf-8")
    assert agent_prefs.load_launch_modes() == {"claude": "app"}


def test_resaving_keeps_one_header_comment() -> None:
    agent_prefs.save_default_agent("claude")
    agent_prefs.save_launch_mode("claude", "app")
    agent_prefs.save_default_agent("codex")
    text = agent_prefs.prefs_path().read_text(encoding="utf-8")
    assert text.count("# litman machine-level preferences") == 1


@pytest.mark.parametrize(
    "text",
    [
        "not: : valid: yaml: [",
        "- just\n- a\n- list\n",
        "launch_modes: app\n",
        "launch_modes:\n  - claude\n",
    ],
    ids=["garbage", "top-level-list", "scalar", "list"],
)
def test_a_broken_file_reads_as_not_chosen_and_saving_recovers(text: str) -> None:
    path = agent_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert agent_prefs.load_launch_modes() == {}
    agent_prefs.save_launch_mode("claude", "app")
    assert agent_prefs.load_launch_modes() == {"claude": "app"}


def test_an_entry_that_is_not_a_mode_reads_as_not_chosen() -> None:
    path = agent_prefs.prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "launch_modes:\n"
        "  claude: rm -rf\n"
        "  codex: 1\n"
        "  cursor: [app]\n"
        "  agy: terminal\n",
        encoding="utf-8",
    )
    assert agent_prefs.load_launch_modes() == {"agy": "terminal"}


@pytest.mark.parametrize(
    ("name", "mode", "message"),
    [
        ("nope", "app", "not a supported agent"),
        ("claude", "fullscreen", "no launch mode"),
        ("codex", "app", "no launch mode"),
        ("zcode", "terminal", "no launch mode"),
    ],
)
def test_save_launch_mode_rejects_what_the_catalog_does_not_have(
    name: str, mode: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        agent_prefs.save_launch_mode(name, mode)
    assert not agent_prefs.prefs_path().exists()
