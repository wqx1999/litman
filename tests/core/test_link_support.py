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

import errno
import io
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from litman.core import checks, portable_link
from litman.core.library import create_vault
from litman.core.portable_link import (
    LINKS_DENIED_HINT,
    link_mechanism,
    links_denied,
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


# What a working drive answers on this host. The probe asks for a junction on
# Windows and a symlink everywhere else, so a bare "symlink" expectation
# describes the developer's box rather than the contract.
_NATIVE = "junction" if sys.platform == "win32" else "symlink"


def _no_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process look like a vault on a FAT32 / exFAT drive.

    BOTH mechanisms have to go. Poisoning only ``Path.symlink_to`` left the
    Windows probe free to make a junction, so every "this drive cannot hold
    links" test quietly asserted against "links work fine" there.
    """

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "symlink_to", boom)
    monkeypatch.setattr(portable_link, "_create_junction", boom)


# --------------------------------------------------------------------------
# The probe itself
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the POSIX arm; Windows is covered by the junction tests below",
)
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
    assert link_mechanism(tmp_path) == _NATIVE
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

    # Count whichever mechanism this host actually probes with — on Windows
    # Path.symlink_to is never reached, so counting it would assert 0 == 1.
    if sys.platform == "win32":
        real_junction = portable_link._create_junction

        def counting_junction(link: Path, target: Path) -> None:
            calls.append(link)
            return real_junction(link, target)

        monkeypatch.setattr(portable_link, "_create_junction", counting_junction)
    else:
        real = Path.symlink_to

        def counting(
            self: Path, target: Any, target_is_directory: bool = False
        ) -> None:
            calls.append(self)
            return real(self, target, target_is_directory)

        monkeypatch.setattr(Path, "symlink_to", counting)

    assert link_mechanism(tmp_path) == _NATIVE
    assert link_mechanism(tmp_path) == _NATIVE
    assert link_mechanism(tmp_path) == _NATIVE
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
    assert link_mechanism(a) == _NATIVE
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="its premise is that _create_junction cannot work here; on a real "
    "Windows kernel it can, and the drive-refuses case is "
    "test_mechanism_none_when_the_os_refuses",
)
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
    for name in ("seahorse", "drugx"):
        d = tmp_path / name
        d.mkdir()
        add_project(vault, name, d)

    _no_links(monkeypatch)
    reset_link_probe_cache()

    issues = checks.check_link_support(vault, [])

    assert len(issues) == 1
    msg = issues[0].message
    assert "views/" in msg
    assert "seahorse" in msg
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


# --------------------------------------------------------------------------
# Access denied — a fact about this run, not about the drive
# --------------------------------------------------------------------------


def _refuse_links_with(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """Both mechanisms refused with ``code``, as the OS would raise it: on
    Windows ``WinError 5`` arrives as a ``PermissionError`` with ``EACCES``."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError(code, "Refused (mocked)")

    monkeypatch.setattr(Path, "symlink_to", boom)
    monkeypatch.setattr(portable_link, "_create_junction", boom)


def _capture_warning(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    from rich.console import Console

    out = io.StringIO()
    monkeypatch.setattr(portable_link, "_console", Console(file=out, width=400))
    portable_link.reset_warning_state()
    return out


def test_access_denied_does_not_read_as_a_drive_without_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_links_with(monkeypatch, errno.EACCES)

    assert links_denied(tmp_path) is True
    assert links_supported(tmp_path) is True
    assert link_mechanism(tmp_path) == _NATIVE
    assert list(tmp_path.iterdir()) == []


def test_access_denied_is_not_remembered_in_this_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The long-lived server or a health check asking again must see it clear."""
    with monkeypatch.context() as patch:
        _refuse_links_with(patch, errno.EACCES)
        assert links_denied(tmp_path) is True

    assert links_denied(tmp_path) is False
    assert link_mechanism(tmp_path) == _NATIVE


@pytest.mark.parametrize("code", [errno.EPERM, errno.EINVAL, errno.ENOTSUP])
def test_the_filesystems_own_refusals_still_read_as_no_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """``EPERM`` is how Linux says a FAT drive takes no symlinks; ``WinError 1``
    is how Windows says one takes no junctions, and arrives as ``EINVAL``."""
    _refuse_links_with(monkeypatch, code)

    assert links_denied(tmp_path) is False
    assert link_mechanism(tmp_path) == "none"


def test_a_folder_that_takes_no_file_is_not_access_denied_on_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """That one is reported by the folder it could not update, as before."""
    _refuse_links_with(monkeypatch, errno.EACCES)
    monkeypatch.setattr(
        portable_link, "entry_refusal", lambda _d: OSError(errno.EACCES, "no")
    )

    assert links_denied(tmp_path) is False
    assert link_mechanism(tmp_path) == _NATIVE


def test_the_warning_for_access_denied_says_so_and_how_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _capture_warning(monkeypatch)
    _refuse_links_with(monkeypatch, errno.EACCES)
    target = tmp_path / "target"
    target.mkdir()

    assert portable_link.make_portable_link(tmp_path / "a", target) is False
    assert portable_link.make_portable_link(tmp_path / "b", target) is False

    said = out.getvalue().strip()
    assert said.startswith("warning: could not create folder links (access denied)")
    assert "lit refresh-views" in said
    # One verdict plus one way out, with no drive advice to act on.
    assert len(said.splitlines()) == 1
    assert len(said) <= 140
    for wrong in ("cannot hold", "FAT32", "internal drive", "first failure"):
        assert wrong not in said


def test_the_warning_for_a_drive_without_links_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _capture_warning(monkeypatch)
    _refuse_links_with(monkeypatch, errno.EPERM)
    target = tmp_path / "target"
    target.mkdir()

    assert portable_link.make_portable_link(tmp_path / "a", target) is False

    said = out.getvalue()
    assert "this filesystem cannot hold folder links" in said
    assert "internal drive" in said
    assert "access denied" not in said


def test_one_line_for_access_denied_naming_every_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.core.project_link import add_project

    vault = create_vault(tmp_path)
    project = tmp_path / "seahorse"
    project.mkdir()
    add_project(vault, "seahorse", project)
    _refuse_links_with(monkeypatch, errno.EACCES)

    issues = checks.check_link_access(vault, [])

    assert [(i.category, i.severity) for i in issues] == [("links_denied", "info")]
    assert "access denied" in issues[0].message
    assert "views/" in issues[0].message
    assert "seahorse" in issues[0].message
    assert issues[0].hint == LINKS_DENIED_HINT
    assert checks.check_link_support(vault, []) == []


def test_a_drive_without_links_is_not_reported_as_access_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = create_vault(tmp_path)
    _no_links(monkeypatch)

    assert checks.check_link_access(vault, []) == []
    assert len(checks.check_link_support(vault, [])) == 1


def test_silent_on_access_when_links_work(tmp_path: Path) -> None:
    assert checks.check_link_access(create_vault(tmp_path), []) == []


# --------------------------------------------------------------------------
# What a refused junction leaves behind: _winapi.CreateJunction makes a real
# directory first and turns it into the junction second.
# --------------------------------------------------------------------------


def _junction_api(monkeypatch: pytest.MonkeyPatch, create: Any) -> None:
    """Stand in for ``_winapi``, on any platform."""
    monkeypatch.setitem(
        sys.modules, "_winapi", types.SimpleNamespace(CreateJunction=create)
    )


def _makes_the_folder_then_refuses(_target: str, link: str) -> None:
    Path(link).mkdir()
    raise PermissionError(errno.EACCES, "Access is denied")


def test_a_refused_junction_leaves_no_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _junction_api(monkeypatch, _makes_the_folder_then_refuses)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(PermissionError):
        portable_link._create_junction(tmp_path / "link", target)

    assert not (tmp_path / "link").exists()


def test_the_junction_probe_leaves_nothing_when_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _junction_api(monkeypatch, _makes_the_folder_then_refuses)

    failure = portable_link._probe_junction(tmp_path)

    assert failure is not None
    assert failure.errno == errno.EACCES
    assert list(tmp_path.iterdir()) == []


def test_a_refused_junction_never_removes_a_folder_with_anything_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fills_the_folder_then_refuses(_target: str, link: str) -> None:
        Path(link).mkdir()
        (Path(link) / "note.txt").write_text("mine", encoding="utf-8")
        raise PermissionError(errno.EACCES, "Access is denied")

    _junction_api(monkeypatch, fills_the_folder_then_refuses)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(PermissionError):
        portable_link._create_junction(tmp_path / "link", target)

    assert (tmp_path / "link" / "note.txt").read_text(encoding="utf-8") == "mine"


def test_a_refused_junction_leaves_a_folder_it_did_not_make(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def already_there(_target: str, _link: str) -> None:
        raise FileExistsError(errno.EEXIST, "Cannot create a file")

    _junction_api(monkeypatch, already_there)
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "link").mkdir()

    with pytest.raises(FileExistsError):
        portable_link._create_junction(tmp_path / "link", target)

    assert (tmp_path / "link").is_dir()
