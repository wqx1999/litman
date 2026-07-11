"""Symlink-capability probe + the checks that go quiet when it says no.

The condition under test is a Windows box without Developer Mode: every
``symlink()`` raises WinError 1314 and litman's three symlink kinds (``views/``,
``litman_reflib/``, ``litman_code/``) simply cannot exist. Before this, that host
got ~6 permanently-red, unfixable errors per paper out of `lit health-check` —
an environment limitation reported as library damage. These tests pin the
contract: the missing-link arms go silent, ONE info line explains why, the
stale-link arms stay live (deleting a symlink needs no privilege), and nothing
changes at all on a host that CAN make symlinks.

``Path.symlink_to`` is monkeypatched rather than mocking litman's own helpers, so
the fake is at the OS boundary and every layer above it runs for real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from litman.core import checks
from litman.core.library import create_vault
from litman.core.portable_link import (
    reset_symlink_support_cache,
    symlink_hint,
    symlink_supported,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> Any:
    """The probe caches per directory for the life of the process.

    Tests flip symlink availability mid-process, so a stale verdict for the same
    tmp_path would silently invalidate whichever test ran second.
    """
    reset_symlink_support_cache()
    yield
    reset_symlink_support_cache()


def _no_symlinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process look like Windows without Developer Mode."""

    def boom(self: Path, target: Any, target_is_directory: bool = False) -> None:
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", boom)


# --------------------------------------------------------------------------
# The probe itself
# --------------------------------------------------------------------------


def test_probe_true_on_a_filesystem_that_allows_symlinks(tmp_path: Path) -> None:
    assert symlink_supported(tmp_path) is True


def test_probe_false_when_the_os_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_symlinks(monkeypatch)
    assert symlink_supported(tmp_path) is False


def test_probe_leaves_nothing_behind(tmp_path: Path) -> None:
    """A probe that littered would seed exactly the dangling links we hunt."""
    assert symlink_supported(tmp_path) is True
    assert list(tmp_path.iterdir()) == []


def test_probe_leaves_nothing_behind_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_symlinks(monkeypatch)
    assert symlink_supported(tmp_path) is False
    assert list(tmp_path.iterdir()) == []


def test_probe_is_cached_per_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One probe per directory per process — the server must not pay it per request."""
    calls: list[Path] = []
    real = Path.symlink_to

    def counting(self: Path, target: Any, target_is_directory: bool = False) -> None:
        calls.append(self)
        return real(self, target, target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", counting)

    assert symlink_supported(tmp_path) is True
    assert symlink_supported(tmp_path) is True
    assert symlink_supported(tmp_path) is True
    assert len(calls) == 1


def test_probe_is_per_directory_not_per_process(tmp_path: Path) -> None:
    """A vault on NTFS and a project on exFAT must be able to disagree.

    Caching by process (rather than by directory) would let whichever was probed
    first speak for the other.
    """
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"  # never created — an unwritable/absent dir reads as False
    assert symlink_supported(a) is True
    assert symlink_supported(b) is False


def test_probe_false_for_missing_directory(tmp_path: Path) -> None:
    assert symlink_supported(tmp_path / "nope") is False


# --------------------------------------------------------------------------
# The hint — the message that decides whether the user finds the free fix
# --------------------------------------------------------------------------


def test_windows_hint_leads_with_developer_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("litman.core.portable_link.sys.platform", "win32")
    hint = symlink_hint()
    assert "Developer Mode" in hint
    assert "ms-settings:developers" in hint


def test_windows_hint_never_recommends_running_elevated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-020: the server spawns agent processes. Elevating litman elevates that.

    The pre-fix message recommended exactly this ("Run as administrator"), and an
    elevated run also leaves library files owned by Administrators, after which
    the user's ordinary `lit add` cannot write its own vault.
    """
    monkeypatch.setattr("litman.core.portable_link.sys.platform", "win32")
    hint = symlink_hint().lower()
    assert "administrator" in hint  # it is named...
    assert "do not run litman as administrator" in hint  # ...only to warn against it


def test_posix_hint_names_the_filesystem_causes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("litman.core.portable_link.sys.platform", "linux")
    hint = symlink_hint()
    assert "exFAT" in hint
    assert "Developer Mode" not in hint


# --------------------------------------------------------------------------
# check_symlink_support — the one line that replaces the flood
# --------------------------------------------------------------------------


def test_silent_when_symlinks_work(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    assert checks.check_symlink_support(vault, []) == []


def test_one_info_line_when_the_host_cannot_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = create_vault(tmp_path)
    _no_symlinks(monkeypatch)
    reset_symlink_support_cache()

    issues = checks.check_symlink_support(vault, [])

    assert len(issues) == 1
    assert issues[0].category == "symlink_unsupported"
    assert issues[0].severity == "info"
    assert "views/" in issues[0].message
    # The reassurance is the point: a user who reads this must not conclude
    # their library is damaged.
    assert "your library itself is fine" in issues[0].message


def test_names_every_affected_scope_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vault + each project, collapsed into a single finding — not one per scope."""
    from litman.core.project_link import add_project

    vault = create_vault(tmp_path)
    for name in ("pepforge", "drugx"):
        d = tmp_path / name
        d.mkdir()
        add_project(vault, name, d)

    _no_symlinks(monkeypatch)
    reset_symlink_support_cache()

    issues = checks.check_symlink_support(vault, [])

    assert len(issues) == 1
    msg = issues[0].message
    assert "views/" in msg
    assert "pepforge" in msg
    assert "drugx" in msg


def test_missing_project_dir_is_not_our_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project folder that is gone belongs to project_path_exists, not to us.

    Probing it would report "cannot create symlinks here" — technically true, and
    completely the wrong diagnosis.
    """
    from litman.core.project_link import add_project

    vault = create_vault(tmp_path)
    gone = tmp_path / "gone"
    gone.mkdir()
    add_project(vault, "gone", gone)
    gone.rmdir()

    reset_symlink_support_cache()
    issues = checks.check_symlink_support(vault, [])

    assert issues == []  # vault can symlink; the dead project is someone else's job
