"""Integration tests for the PyPI update nudge on the root CLI group (D2).

The nudge fires post-dispatch in ``LitGroup.invoke`` (sibling of the staleness
nudge) but with a STRICTER gate than staleness: interactive TTY only AND not
opted out. On a non-TTY (agent pipe) or opt-out there is zero network, zero
cache read, zero output. These drive ``lit list`` through ``CliRunner`` so the
TTY gate, the frequency cap, and the skip set are exercised end-to-end.

The network seam (``update_check._fetch_latest_version``) is always mocked — no
test hits pypi.org. The cache is isolated by the conftest ``_isolate_registry``
fixture (``$LITMAN_REGISTRY_DIR`` → a temp dir).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.commands import _drift
from litman.core import update_check
from litman.core.library import create_vault
from litman.core.vault_registry import add_vault, load_registry, save_registry

_TIP = "is available"


@pytest.fixture(autouse=True)
def _pin_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the current version so the nudge message assertion (``you have
    1.1.0``) stays hermetic across release bumps. The nudge reads
    ``litman.__version__`` live via ``update_check._current_version``."""
    monkeypatch.setattr("litman.__version__", "1.1.0")


@pytest.fixture(autouse=True)
def _clear_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(update_check.OPT_OUT_ENV, raising=False)


def _seed_active_vault(tmp_path: Path) -> Path:
    parent = tmp_path / "real_parent"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(add_vault(load_registry(), "main", vault))
    return vault


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)


def _mock_fetch(monkeypatch: pytest.MonkeyPatch, version: str | None) -> list[int]:
    """Mock the network seam; return a call-counter list."""
    calls: list[int] = []

    def _fetch(**kw: object) -> str | None:
        calls.append(1)
        return version

    monkeypatch.setattr(update_check, "_fetch_latest_version", _fetch)
    return calls


def test_update_nudge_fires_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTY + a newer PyPI version → tip on stderr naming the version."""
    _seed_active_vault(tmp_path)
    _force_tty(monkeypatch)
    _mock_fetch(monkeypatch, "9.9.9")

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert "litman 9.9.9 is available (you have 1.1.0)" in result.stderr
    assert "lit self-update" in result.stderr


def test_update_nudge_silent_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: non-TTY (default CliRunner) → no tip AND no network / cache touch."""
    _seed_active_vault(tmp_path)
    calls = _mock_fetch(monkeypatch, "9.9.9")

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _TIP not in result.stdout
    assert _TIP not in result.stderr
    assert calls == []  # zero network
    assert not update_check.cache_path().exists()  # zero cache read/write


def test_update_nudge_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: opt-out → zero network, zero nudge even on a TTY."""
    _seed_active_vault(tmp_path)
    _force_tty(monkeypatch)
    monkeypatch.setenv(update_check.OPT_OUT_ENV, "1")
    calls = _mock_fetch(monkeypatch, "9.9.9")

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _TIP not in result.stdout
    assert _TIP not in result.stderr
    assert calls == []
    assert not update_check.cache_path().exists()


def test_update_nudge_one_request_one_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: two consecutive commands → one network request + one nudge."""
    _seed_active_vault(tmp_path)
    _force_tty(monkeypatch)
    calls = _mock_fetch(monkeypatch, "9.9.9")

    first = CliRunner().invoke(cli, ["list"])
    assert first.exit_code == 0, first.output
    second = CliRunner().invoke(cli, ["list"])
    assert second.exit_code == 0, second.output

    assert len(calls) == 1  # second run hit the fresh cache
    assert _TIP in first.stderr
    assert _TIP not in second.stderr  # capped once per 24h


def test_update_nudge_netdown_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: net down (fetch → None) → no tip, no error, command succeeds."""
    _seed_active_vault(tmp_path)
    _force_tty(monkeypatch)
    _mock_fetch(monkeypatch, None)

    result = CliRunner().invoke(cli, ["list"])
    assert result.exit_code == 0, result.output
    assert _TIP not in result.stdout
    assert _TIP not in result.stderr


def test_update_nudge_skipped_for_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lit help` is in the skip set → no nudge even when newer + TTY."""
    _seed_active_vault(tmp_path)
    _force_tty(monkeypatch)
    _mock_fetch(monkeypatch, "9.9.9")

    result = CliRunner().invoke(cli, ["help"])
    assert result.exit_code == 0, result.output
    assert _TIP not in result.output
