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
