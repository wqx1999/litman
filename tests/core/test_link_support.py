"""Link-capability probe + the checks that go quiet when it says "none".

The condition under test is a drive that cannot hold folder links at all:
FAT32 / exFAT sticks and most network shares refuse POSIX symlinks and Windows
junctions alike. Before the advisory tier existed, such a host got ~6
permanently-red, unfixable errors per paper out of `lit health-check` — an
environment limitation reported as library damage. These tests pin the
contract: the missing-link arms go silent, ONE info line explains why, the
stale-link arms stay live (removing a link works on any filesystem), and
nothing changes at all on a host where a link mechanism works.

``Path.symlink_to`` is monkeypatched rather than mocking litman's own helpers,
so the fake sits at the OS boundary and every layer above it runs for real.
The Windows arm (junctions) is dispatch-tested through litman's own
``_create_junction`` seam here and in ``test_portable_link.py``; the real
junction syscalls can only run on a Windows host (see the skipif tests there).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from litman.core import checks, portable_link
from litman.core.library import create_vault
from litman.core.portable_link import (
    link_mechanism,
    links_supported,
    links_unsupported_hint,
    reset_link_probe_cache,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> Any:
    """The probe caches per directory for the life of the process.

    Tests flip link availability mid-process, so a stale verdict for the same
    tmp_path would silently invalidate whichever test ran second.
    """
    reset_link_probe_cache()
    yield
    reset_link_probe_cache()


def _no_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process look like a vault on a FAT32 / exFAT drive."""

    def boom(self: Path, target: Any, target_is_directory: bool = False) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "symlink_to", boom)


# --------------------------------------------------------------------------
# The probe itself
# --------------------------------------------------------------------------


def test_mechanism_is_symlink_on_posix(tmp_path: Path) -> None:
    assert link_mechanism(tmp_path) == "symlink"
    assert links_supported(tmp_path) is True


def test_mechanism_none_when_the_os_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_links(monkeypatch)
    assert link_mechanism(tmp_path) == "none"
    assert links_supported(tmp_path) is False


def test_probe_leaves_nothing_behind(tmp_path: Path) -> None:
    """A probe that littered would seed exactly the dangling links we hunt."""
    assert link_mechanism(tmp_path) == "symlink"
    assert list(tmp_path.iterdir()) == []


def test_probe_leaves_nothing_behind_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_links(monkeypatch)
    assert link_mechanism(tmp_path) == "none"
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

    assert link_mechanism(tmp_path) == "symlink"
    assert link_mechanism(tmp_path) == "symlink"
    assert link_mechanism(tmp_path) == "symlink"
    assert len(calls) == 1


def test_probe_is_per_directory_not_per_process(tmp_path: Path) -> None:
    """A vault on an internal drive and a project on an exFAT stick must be
    able to disagree.

    Caching by process (rather than by directory) would let whichever was
    probed first speak for the other.
    """
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"  # never created — an unwritable/absent dir reads as none
    assert link_mechanism(a) == "symlink"
    assert link_mechanism(b) == "none"


def test_mechanism_none_for_missing_directory(tmp_path: Path) -> None:
    assert link_mechanism(tmp_path / "nope") == "none"


def test_win32_mechanism_is_junction_via_the_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the probe asks for a junction, never a symlink.

    ``_create_junction`` is faked at litman's own seam (a junction needs a
    Windows kernel); ``Path.symlink_to`` is poisoned so any symlink attempt
    fails the test loudly. The probe must also clean up its scratch target.
    """

    def poisoned(self: Path, *_a: object, **_k: object) -> None:
        raise AssertionError("win32 must never probe symlinks")

    monkeypatch.setattr(portable_link.sys, "platform", "win32")
    monkeypatch.setattr(portable_link, "_create_junction", lambda link, target: None)
    monkeypatch.setattr(Path, "symlink_to", poisoned)

    assert link_mechanism(tmp_path) == "junction"
    assert links_supported(tmp_path) is True
    assert list(tmp_path.iterdir()) == []


def test_win32_mechanism_on_a_host_without_junctions_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `_create_junction` off-Windows raises OSError (no `_winapi`,
    no `cmd`), so a forced win32 platform on this host degrades to "none" —
    the same deterministic path a FAT32 drive takes on real Windows."""
    monkeypatch.setattr(portable_link.sys, "platform", "win32")

    assert link_mechanism(tmp_path) == "none"
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# The hint — the message that decides what the user does next
# --------------------------------------------------------------------------


def test_hint_names_the_filesystem_causes() -> None:
    hint = links_unsupported_hint()
    assert "exFAT" in hint
    assert "internal drive" in hint


def test_hint_never_sends_users_into_system_settings() -> None:
    """The whole point of the junction tier: by the time this hint fires, the
    cause is the drive itself. Developer Mode, elevation and WSL would change
    nothing — recommending any of them sends a novice into scary system
    dialogs for zero benefit (and elevation is forbidden outright, ADR-020).
    """
    hint = links_unsupported_hint().lower()
    assert "developer mode" not in hint
    assert "administrator" not in hint
    assert "elevat" not in hint
    assert "wsl" not in hint
    assert "ms-settings" not in hint


# --------------------------------------------------------------------------
# check_link_support — the one line that replaces the flood
# --------------------------------------------------------------------------


def test_silent_when_links_work(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    assert checks.check_link_support(vault, []) == []


def test_one_info_line_when_the_drive_cannot_hold_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = create_vault(tmp_path)
    _no_links(monkeypatch)
    reset_link_probe_cache()

    issues = checks.check_link_support(vault, [])

    assert len(issues) == 1
    assert issues[0].category == "links_unsupported"
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

    _no_links(monkeypatch)
    reset_link_probe_cache()

    issues = checks.check_link_support(vault, [])

    assert len(issues) == 1
    msg = issues[0].message
    assert "views/" in msg
    assert "pepforge" in msg
    assert "drugx" in msg


def test_missing_project_dir_is_not_our_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project folder that is gone belongs to project_path_exists, not to us.

    Probing it would report "cannot create links here" — technically true, and
    completely the wrong diagnosis.
    """
    from litman.core.project_link import add_project

    vault = create_vault(tmp_path)
    gone = tmp_path / "gone"
    gone.mkdir()
    add_project(vault, "gone", gone)
    gone.rmdir()

    reset_link_probe_cache()
    issues = checks.check_link_support(vault, [])

    assert issues == []  # vault can link; the dead project is someone else's job
