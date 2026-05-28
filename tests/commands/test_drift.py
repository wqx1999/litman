"""Tests for the M28 vault registry drift surfacing hook.

Two layers exercised:

1. ``find_dangling`` — pure function over ``VaultRegistry`` (no I/O).
2. ``check_and_prompt_registry_drift`` — TTY vs non-TTY branches, clean
   state silence, corrupt-registry silence. The function reads/writes the
   user-level registry via ``load_registry`` / ``save_registry``, so every
   test runs under a ``fake_home`` autouse fixture that redirects HOME +
   clears ``LITMAN_REGISTRY_DIR`` / ``XDG_CONFIG_HOME`` to a tmp dir.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from litman.commands import _drift
from litman.core.vault_registry import (
    VaultEntry,
    VaultRegistry,
    VaultRegistryError,
    find_dangling,
    load_registry,
    registry_path,
    save_registry,
)


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME + clear cross-cutting env vars so every test in this
    module reads/writes a tmp-path-rooted registry instead of the real
    ``~/.config/litman/vaults.yaml``.

    Mirrors the ``fake_home`` fixture in test_init.py / test_vault.py.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


# ---------------------------------------------------------------------------
# find_dangling
# ---------------------------------------------------------------------------


def test_find_dangling_empty_registry() -> None:
    assert find_dangling(VaultRegistry()) == []


def test_find_dangling_all_exist(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    reg = VaultRegistry(
        vaults=[
            VaultEntry(name="a", path=str(a), is_active=True),
            VaultEntry(name="b", path=str(b), is_active=False),
        ]
    )
    assert find_dangling(reg) == []


def test_find_dangling_mixed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    ghost = tmp_path / "ghost"  # not created
    reg = VaultRegistry(
        vaults=[
            VaultEntry(name="real", path=str(real), is_active=True),
            VaultEntry(name="ghost", path=str(ghost), is_active=False),
        ]
    )
    dangling = find_dangling(reg)
    assert len(dangling) == 1
    assert dangling[0].name == "ghost"


# ---------------------------------------------------------------------------
# check_and_prompt_registry_drift — helpers
# ---------------------------------------------------------------------------


def _seed_two_entries_one_dangling(tmp_path: Path) -> tuple[Path, Path]:
    """Persist a registry with one real entry + one dangling entry.

    Returns (real_path, ghost_path).
    """
    real = tmp_path / "real"
    real.mkdir()
    ghost = tmp_path / "ghost"  # intentionally not created
    reg = VaultRegistry(
        vaults=[
            VaultEntry(name="real", path=str(real), is_active=True),
            VaultEntry(name="ghost", path=str(ghost), is_active=False),
        ]
    )
    save_registry(reg)
    return real, ghost


# ---------------------------------------------------------------------------
# TTY branch
# ---------------------------------------------------------------------------


def test_drift_prompt_tty_yes_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_entries_one_dangling(tmp_path)
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: True)

    _drift.check_and_prompt_registry_drift()

    remaining = load_registry().vaults
    assert [v.name for v in remaining] == ["real"]


def test_drift_prompt_tty_no_keeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_two_entries_one_dangling(tmp_path)
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: False)

    _drift.check_and_prompt_registry_drift()

    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]


# ---------------------------------------------------------------------------
# Non-TTY branch
# ---------------------------------------------------------------------------


def test_drift_prompt_non_tty_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_two_entries_one_dangling(tmp_path)
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: False)

    _drift.check_and_prompt_registry_drift()

    captured = capsys.readouterr()
    # Registry unchanged: non-TTY must never mutate without consent.
    remaining = load_registry().vaults
    assert sorted(v.name for v in remaining) == ["ghost", "real"]
    # One stderr warning carrying the dangling name and the remediation hint.
    # Normalize whitespace because Rich wraps long lines at the auto-detected
    # console width; the literal "lit vault remove" can straddle a wrap point.
    err_flat = " ".join(captured.err.split())
    assert "ghost" in err_flat
    assert "lit vault remove" in err_flat


# ---------------------------------------------------------------------------
# Clean / corrupt states
# ---------------------------------------------------------------------------


def test_drift_prompt_clean_registry_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="real", path=str(real), is_active=True)]
        )
    )
    # Force TTY so we exercise the path that WOULD print; the early return
    # on "no dangling" must still produce zero output.
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    _drift.check_and_prompt_registry_drift()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    remaining = load_registry().vaults
    assert [v.name for v in remaining] == ["real"]


def test_drift_prompt_corrupt_registry_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Write a malformed registry: top-level YAML is a string, not a mapping —
    # ``load_registry`` raises ``VaultRegistryError`` which the drift function
    # must swallow silently (the real diagnostic surfaces from `lit vault *`).
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a mapping\n", encoding="utf-8")
    original_bytes = path.read_bytes()

    # Sanity check: confirm the corrupt content actually triggers a
    # VaultRegistryError on direct load. Without this, the test silently
    # passes if load_registry() ever stops raising on bad input — the
    # drift function would be exercising the "clean registry" path
    # instead of the swallow-the-exception path we mean to assert.
    with pytest.raises(VaultRegistryError):
        load_registry()

    # TTY is irrelevant — the function should bail before even probing.
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)

    # Must not raise.
    _drift.check_and_prompt_registry_drift()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    # File unchanged.
    assert path.read_bytes() == original_bytes
