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

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))


def pytest_configure(config) -> None:
    """Register the bench-local ``slow`` marker (the 5-paper seed build)."""
    config.addinivalue_line(
        "markers", "slow: a slower deterministic test (multi-paper seed build)"
    )

