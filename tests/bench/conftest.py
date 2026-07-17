"""pytest config for the litman-bench deterministic-core tests.

Adds the bench dir to ``sys.path`` so ``import harness`` resolves the harness
package regardless of pytest's rootdir / cwd. Also exposes session-scoped
markers/skips for the cards that cannot run in the sandbox (needs_network /
needs_pty) — those carry an explicit ``skip_reason`` in their YAML and the
loader yields them as part of the full corpus.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))


def pytest_configure(config) -> None:
    """Register the bench-local ``slow`` marker (the 5-paper seed build)."""
    config.addinivalue_line(
        "markers", "slow: a slower deterministic test (multi-paper seed build)"
    )


@pytest.fixture(autouse=True)
def _no_real_credential_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop any test resolving the developer's REAL credential dirs.

    A test that fakes a home does it by pointing ``$HOME`` at a ``tmp_path``. That
    is not sufficient on its own, and the gap is not theoretical — it was found
    live: ``claude._real_config_dir()`` reads ``$CLAUDE_CONFIG_DIR`` **first** and
    only falls back to ``~/.claude``, so with that documented, supported var
    exported the fake home is ignored, ``seed_auth`` copies the developer's real
    OAuth credential into ``tmp_path``, and the test still passes. A leak that
    passes is the kind nobody finds. ``XDG_CONFIG_HOME`` is the same shape one
    level over (cursor's auth path), so both are cleared for every test.

    ``$HOME`` itself is deliberately NOT cleared here: ``Path.home()`` falls back
    to the ``pwd`` database when it is unset, which resolves to the real home
    anyway — an unset HOME would look isolated and not be. Faking a home stays the
    individual test's job; this fixture only guarantees that when a test does fake
    one, nothing silently overrides it.

    The list is :data:`harness.agents.HOME_ESCAPING_CONFIG_VARS`, shared with
    :func:`harness.agents.isolated_env` rather than repeated here: this fixture
    protects the TEST process and ``isolated_env`` protects the AGENT's process,
    but they are the same question — "which var names a real config dir absolutely"
    — and two copies of one answer drift. Adding an agent means adding its var
    THERE, and both sides move together.
    """
    from harness.agents import HOME_ESCAPING_CONFIG_VARS

    for var in HOME_ESCAPING_CONFIG_VARS:
        monkeypatch.delenv(var, raising=False)

