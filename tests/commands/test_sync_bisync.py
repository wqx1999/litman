"""Two-way ``lit sync`` (rclone bisync, ADR-030).

Two vaults stand for machine A (home) and machine B (school), and a ``type =
local`` rclone remote stands for the cloud, so every run is a real ``rclone
bisync`` with no cloud account. "Switching machines" is just syncing the other
vault: each vault keeps its own bisync state, keyed by its path, under the data
dir that ``tests/conftest.py`` pins inside the test's home.

Tests that drive rclone are skipped when it is missing or older than
:data:`BISYNC_MIN_VERSION`; CI installs a pinned rclone on all three platforms
so they run there. The version-check and conflict-sweep tests need no rclone.

Every edit sets its file's mtime explicitly: bisync compares size and modtime,
and some filesystems give files written in the same instant the same mtime.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from rich.console import Console

from litman.cli import cli
from litman.commands import sync as sync_cmd
from litman.core import locking
from litman.core import sync as core_sync
from litman.core.library import create_vault
from litman.core.sync import (
    BISYNC_MIN_VERSION,
    SYNC_STATE_FILENAME,
    SetupPayload,
    SyncState,
    bisync_paths,
    local_snapshot,
    read_sync_state,
    require_bisync_rclone,
    stamp_push,
    stamp_two_way,
    sweep_conflicts,
    two_way_filter_rules,
    two_way_sync,
    write_sync_state,
    write_sync_to_config,
)
from litman.exceptions import SyncError


def _rclone_usable() -> bool:
    exe = shutil.which("rclone")
    if exe is None:
        return False
    try:
        out = subprocess.run(
            [exe, "version"], capture_output=True, text=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.search(r"rclone v(\d+)\.(\d+)\.(\d+)", out)
    if match is None:
        return False
    return tuple(int(part) for part in match.groups()) >= BISYNC_MIN_VERSION


needs_rclone = pytest.mark.skipif(
    not _rclone_usable(),
    reason="rclone missing or too old for two-way sync; CI installs a pinned one.",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _wide_consoles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let panels and rclone lines print unwrapped, so phrases stay whole."""
    monkeypatch.setattr(sync_cmd, "console", Console(width=400))
    monkeypatch.setattr(sync_cmd, "_rclone_console", Console(stderr=True, width=400))


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """A short temporary folder, in place of pytest's own.

    rclone bisync names its record files after both synced paths, and one
    file name may not pass 255 characters: pytest's folder on the macOS and
    Windows runners is too deep for that.
    """
    path = Path(tempfile.mkdtemp(prefix="lt-")).resolve()
    yield path
    locking.rmtree(path, ignore_errors=True)


@pytest.fixture
def cloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The cloud folder; ``fake-cloud:<it>/lib`` is the library on the remote."""
    conf = tmp_path / "rclone.conf"
    conf.write_text("[fake-cloud]\ntype = local\nnounc = false\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    storage = tmp_path / "cloud"
    storage.mkdir()
    return storage


def _target(cloud: Path) -> str:
    return f"fake-cloud:{cloud / 'lib'}"


def _machine(tmp_path: Path, cloud: Path, name: str) -> Path:
    """A fresh vault for machine ``name``, configured for the cloud."""
    parent = tmp_path / name
    parent.mkdir()
    vault = create_vault(parent)
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(remote="fake-cloud", path=str(cloud / "lib")),
    )
    return vault


_T0 = time.time() - 24 * 3600


def _touch(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _write_paper(
    vault: Path, pid: str, *, status: str = "inbox", mtime: float = _T0
) -> None:
    """Write (or rewrite) a health-check-clean paper, locked like a real one."""
    paper = vault / "papers" / pid
    paper.mkdir(parents=True, exist_ok=True)
    for name in ("metadata.yaml", "paper.pdf"):
        locking.unlock_truth_file(paper / name)
    (paper / "metadata.yaml").write_text(
        f"id: {pid}\ntitle: Paper {pid}\nyear: 2024\nstatus: {status}\n"
        "created-at: '2024-01-01T00:00:00+00:00'\n"
        "updated-at: '2024-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    (paper / "notes.md").write_text(f"# Paper {pid}\n", encoding="utf-8")
    (paper / "paper.pdf").write_bytes(b"%PDF-1.4\n% " + pid.encode() + b"\n")
    for name in ("metadata.yaml", "notes.md", "paper.pdf"):
        _touch(paper / name, mtime)
    for name in ("metadata.yaml", "paper.pdf"):
        locking.lock_truth_file(paper / name)


def _edit_metadata(vault: Path, pid: str, status: str, mtime: float) -> None:
    path = vault / "papers" / pid / "metadata.yaml"
    locking.unlock_truth_file(path)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(r"status: .*", f"status: {status}", text), encoding="utf-8"
    )
    _touch(path, mtime)
    locking.lock_truth_file(path)


def _edit_notes(vault: Path, pid: str, text: str, mtime: float) -> None:
    path = vault / "papers" / pid / "notes.md"
    path.write_text(text, encoding="utf-8")
    _touch(path, mtime)


def _sync(vault: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["sync", *args, "--library", str(vault)])


# Machine-local or derived: never synced, so left out of tree comparisons.
_LOCAL_ONLY = ("INDEX.json", "lit-config.yaml", SYNC_STATE_FILENAME)


def _tree(root: Path) -> dict[str, bytes]:
    """``{relative path: bytes}`` of the files two-way sync carries."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if parts[0] in ("views", ".litman-staging") or rel in _LOCAL_ONLY:
            continue
        if len(parts) > 3 and parts[0] == "codes" and parts[2] == "repo":
            continue
        if path.is_file() and not path.is_symlink():
            out[rel] = path.read_bytes()
    return out


def _conflict_leftovers(*roots: Path) -> list[str]:
    return [str(p) for root in roots for p in root.rglob("*.sync-conflict*")]


def _truth_files(vault: Path) -> list[Path]:
    files = [vault / "TAXONOMY.md"]
    for paper in sorted((vault / "papers").iterdir()):
        files += [paper / "metadata.yaml", paper / "paper.pdf"]
    return [f for f in files if f.exists()]


def _local_backup_files(vault: Path, cloud: Path) -> dict[str, bytes]:
    """Every file under this machine's backup root, by path relative to its run folder."""
    root = bisync_paths(vault.resolve(), _target(cloud)).local_backup_root
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            parts = path.relative_to(root).parts
            # <run>/<rel...> for bisync's backups, conflicts/<run>/<rel...> for conflicts
            rel_parts = parts[2:] if parts[0] == "conflicts" else parts[1:]
            out["/".join(rel_parts)] = path.read_bytes()
    return out


def _two_synced_machines(tmp_path: Path, cloud: Path, n_papers: int = 4) -> tuple[Path, Path]:
    """A with ``n_papers`` papers synced up, B synced down from the cloud."""
    a = _machine(tmp_path, cloud, "a")
    b = _machine(tmp_path, cloud, "b")
    for i in range(1, n_papers + 1):
        _write_paper(a, f"p{i}")
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0
    return a, b


# ---------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------


@needs_rclone
def test_edits_on_both_machines_reach_every_side(tmp_path: Path, cloud: Path) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    now = time.time()
    _edit_metadata(a, "p1", "reading", now - 300)
    _edit_notes(b, "p2", "# Paper p2\nread at school\n", now - 300)
    _write_paper(b, "p5", mtime=now - 300)

    for vault in (a, b, a):
        result = _sync(vault)
        assert result.exit_code == 0, result.output

    tree_a, tree_b, tree_cloud = _tree(a), _tree(b), _tree(cloud / "lib")
    assert tree_a == tree_b == tree_cloud
    assert b"status: reading" in tree_b["papers/p1/metadata.yaml"]
    assert b"read at school" in tree_a["papers/p2/notes.md"]
    assert "papers/p5/metadata.yaml" in tree_a


@needs_rclone
def test_excluded_files_never_travel(tmp_path: Path, cloud: Path) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    (a / "codes" / "tool" / "repo").mkdir(parents=True)
    (a / "codes" / "tool" / "repo" / "main.py").write_text("print()\n", encoding="utf-8")
    (a / "codes" / "tool" / "repo-meta.yaml").write_text("name: tool\n", encoding="utf-8")
    (a / ".DS_Store").write_bytes(b"x")
    si = a / "papers" / "p1" / "supplementary"
    si.mkdir()
    (si / "INDEX.json").write_text("an SI file named like the index\n", encoding="utf-8")

    assert _sync(a).exit_code == 0
    on_cloud = set(_tree(cloud / "lib"))
    for never in ("INDEX.json", "lit-config.yaml", SYNC_STATE_FILENAME, ".DS_Store"):
        assert never not in on_cloud
    assert not any(rel.startswith(("views/", "codes/tool/repo/")) for rel in on_cloud)
    assert "codes/tool/repo-meta.yaml" in on_cloud
    assert "papers/p1/supplementary/INDEX.json" in on_cloud


# ---------------------------------------------------------------------------
# First sync on a machine
# ---------------------------------------------------------------------------


@needs_rclone
def test_first_sync_with_empty_remote_copies_this_library_up(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    _write_paper(a, "p2")

    result = _sync(a)

    assert result.exit_code == 0, result.output
    assert "the cloud folder was empty" in result.output
    assert _tree(cloud / "lib") == _tree(a)
    assert read_sync_state(a).last_sync_result == "ok"


@needs_rclone
def test_first_sync_on_fresh_machine_takes_the_cloud_and_backs_up_what_it_replaced(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    taxonomy_a = a / "TAXONOMY.md"
    locking.unlock_truth_file(taxonomy_a)
    taxonomy_a.write_text(
        taxonomy_a.read_text(encoding="utf-8") + "\n<!-- edited on A -->\n",
        encoding="utf-8",
    )
    _touch(taxonomy_a, _T0 + 60)
    assert _sync(a).exit_code == 0

    b = _machine(tmp_path, cloud, "b")
    old_taxonomy_b = (b / "TAXONOMY.md").read_bytes()
    result = _sync(b)

    assert result.exit_code == 0, result.output
    assert "this library was new" in result.output
    assert _tree(b) == _tree(a)
    assert _local_backup_files(b, cloud)["TAXONOMY.md"] == old_taxonomy_b


@needs_rclone
def test_first_sync_with_a_library_on_both_sides_is_refused_and_changes_nothing(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    b = _machine(tmp_path, cloud, "b")
    _write_paper(b, "b1")
    before_b = {**_tree(b), **{n: (b / n).read_bytes() for n in _LOCAL_ONLY if (b / n).exists()}}
    before_cloud = _tree(cloud / "lib")

    result = _sync(b)

    assert result.exit_code == sync_cmd.EXIT_SYNC_NEEDS_CHOICE == 20
    assert "both hold a library" in result.output
    assert "--first-from cloud" in result.output
    after_b = {**_tree(b), **{n: (b / n).read_bytes() for n in _LOCAL_ONLY if (b / n).exists()}}
    assert after_b == before_b
    assert _tree(cloud / "lib") == before_cloud


@needs_rclone
@pytest.mark.parametrize("winner", ["local", "cloud"])
def test_first_from_picks_the_winner_and_keeps_the_loser_in_the_backup(
    tmp_path: Path, cloud: Path, winner: str
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1", status="from-a")
    assert _sync(a).exit_code == 0
    b = _machine(tmp_path, cloud, "b")
    _write_paper(b, "p1", status="from-b-longer", mtime=_T0 + 60)
    _write_paper(b, "b1")
    a_copy = (a / "papers" / "p1" / "metadata.yaml").read_bytes()
    b_copy = (b / "papers" / "p1" / "metadata.yaml").read_bytes()

    result = _sync(b, "--first-from", winner)

    assert result.exit_code == 0, result.output
    expected = b_copy if winner == "local" else a_copy
    assert (b / "papers" / "p1" / "metadata.yaml").read_bytes() == expected
    assert _tree(cloud / "lib")["papers/p1/metadata.yaml"] == expected
    # A paper on one side only is kept and copied across, whoever wins.
    assert "papers/b1/metadata.yaml" in _tree(cloud / "lib")
    if winner == "cloud":
        assert _local_backup_files(b, cloud)["papers/p1/metadata.yaml"] == b_copy
    else:
        remote_backups = list((cloud / "lib-litman-backup").rglob("metadata.yaml"))
        assert [p.read_bytes() for p in remote_backups] == [a_copy]
    assert _sync(b).exit_code == 0


# ---------------------------------------------------------------------------
# rclone's record names, checked before a first sync
# ---------------------------------------------------------------------------

#: The platform the tests run on, read once at import (dev_docs/ci.md, trap 1).
_WINDOWS = sys.platform == "win32"


@pytest.mark.parametrize(
    ("path1", "path2", "windows", "expected"),
    [
        pytest.param(
            ("local", "/home/me/literature_vault"),
            ("gdrive", "litman/vault"),
            False,
            "home_me_literature_vault..gdrive_litman_vault",
            id="posix",
        ),
        pytest.param(
            ("local", "//?/C:/Users/me/literature_vault"),
            ("fake-cloud", "//?/C:/Users/me/cloud/lib"),
            True,
            "C__Users_me_literature_vault..fake-cloud_____C__Users_me_cloud_lib",
            id="windows-a-local-type-remote-keeps-its-prefix",
        ),
        pytest.param(
            ("local", "//?/UNC/server/share/vault"),
            ("gdrive", "lib"),
            True,
            "UNC_server_share_vault..gdrive_lib",
            id="windows-network-share",
        ),
        pytest.param(
            ("local", "/home/a b/v/"),
            ("gdrive{b6816}", "/My Library/lit/"),
            False,
            "home_a_b_v..gdrive__My_Library_lit",
            id="spaces-slashes-and-an-options-hash",
        ),
        pytest.param(
            ("local", "/home/a\u3000b/v*?:"),
            ("gdrive", "x"),
            False,
            "home_a\u3000b_v___..gdrive_x",
            id="only-ascii-whitespace",
        ),
    ],
)
def test_the_session_name_follows_rclones_rules(
    path1: tuple[str, str], path2: tuple[str, str], windows: bool, expected: str
) -> None:
    """rclone's cmd/bisync/bilib/canonical.go; the Windows rows are names seen on CI."""
    assert core_sync.bisync_session_name(path1, path2, windows=windows) == expected


@pytest.mark.parametrize(
    ("name", "posix", "windows"),
    [("abc", 3, 3), ("\u00e9", 2, 1), ("\u6587\u732e", 6, 2), ("\U0001f600", 4, 2)],
)
def test_a_name_is_measured_in_bytes_off_windows_and_utf16_units_on_it(
    name: str, posix: int, windows: int
) -> None:
    assert core_sync.name_length(name, windows=False) == posix
    assert core_sync.name_length(name, windows=True) == windows


_TOO_LONG = "This library path plus the cloud path is {} too long for rclone."
_SHORTER_CLOUD_PATH = " Pick a shorter cloud path: `lit sync setup`."
_MOVE_THE_LIBRARY = " Move the library, then run `lit vault set-path`."


@pytest.mark.parametrize(
    ("library", "cloud_folder", "shortest_known", "message"),
    [
        # The session name, "v…v..gdrive_c…c", is library + 9 + cloud_folder long,
        # 241 at most; with the folder "x" it is library + 10.
        pytest.param(100, 132, True, None, id="fits"),
        pytest.param(
            100, 133, True, _TOO_LONG.format("1 character") + _SHORTER_CLOUD_PATH,
            id="a-shorter-cloud-path-is-enough",
        ),
        pytest.param(
            231, 10, True, _TOO_LONG.format("9 characters") + _SHORTER_CLOUD_PATH,
            id="a-one-letter-cloud-folder-is-just-enough",
        ),
        pytest.param(
            232, 10, True, _TOO_LONG.format("10 characters") + _MOVE_THE_LIBRARY,
            id="the-library-must-move",
        ),
        pytest.param(
            100, 133, False, _TOO_LONG.format("1 character") + _MOVE_THE_LIBRARY,
            id="the-shortest-cloud-path-is-unknown",
        ),
    ],
)
def test_record_names_too_long_are_refused_with_a_way_out_that_is_enough(
    monkeypatch: pytest.MonkeyPatch,
    library: int,
    cloud_folder: int,
    shortest_known: bool,
    message: str | None,
) -> None:
    infos = {
        "vault": ("local", "/" + "v" * library),
        "gdrive:lib": ("gdrive", "c" * cloud_folder),
        "gdrive:x": ("gdrive", "x") if shortest_known else None,
    }
    monkeypatch.setattr(core_sync, "_rclone_fs_info", infos.get)

    if message is None:
        core_sync.require_bisync_names_fit(Path("vault"), "gdrive:lib")
    else:
        with pytest.raises(SyncError) as refused:
            core_sync.require_bisync_names_fit(Path("vault"), "gdrive:lib")
        assert str(refused.value) == message


@needs_rclone
def test_the_record_name_check_steps_aside_when_rclone_cannot_reach_the_remote(
    tmp_path: Path, cloud: Path
) -> None:
    """The real ``rclone backend features`` behind the ``_rclone_fs_info`` seam fails here."""
    assert core_sync._rclone_fs_info("no-such-remote:lib") is None
    core_sync.require_bisync_names_fit(tmp_path, "no-such-remote:" + "x" * 300)


@needs_rclone
def test_the_record_name_check_asks_rclone_only_before_the_first_sync(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _machine(tmp_path, cloud, "a")
    asked: list[str] = []
    real = core_sync._rclone_fs_info

    def asking(remote: str) -> tuple[str, str] | None:
        asked.append(remote)
        return real(remote)

    monkeypatch.setattr(core_sync, "_rclone_fs_info", asking)

    assert _sync(a).exit_code == 0
    assert asked == [str(a.resolve()), _target(cloud)]
    asked.clear()
    assert _sync(a).exit_code == 0
    assert asked == []


def _machine_with_record_name(tmp_path: Path, cloud: Path, length: int) -> tuple[Path, Path]:
    """Machine A with a paper, set to sync with a cloud folder that makes rclone's
    session name for the pair ``length`` long, as litman measures it.

    Returns the vault and that cloud folder, which does not exist yet.
    """
    vault = _machine(tmp_path, cloud, "a").resolve()
    _write_paper(vault, "p1")
    local = core_sync._rclone_fs_info(str(vault))
    assert local is not None

    def session_length(folder: Path) -> int:
        remote = core_sync._rclone_fs_info(f"fake-cloud:{folder}")
        assert remote is not None
        name = core_sync.bisync_session_name(local, remote, windows=_WINDOWS)
        return core_sync.name_length(name, windows=_WINDOWS)

    folder = cloud / ("x" * (length - session_length(cloud / "x") + 1))
    assert session_length(folder) == length
    write_sync_to_config(
        vault / "lit-config.yaml", SetupPayload(remote="fake-cloud", path=str(folder))
    )
    return vault, folder


@needs_rclone
def test_a_library_whose_record_names_just_fit_syncs(tmp_path: Path, cloud: Path) -> None:
    vault, folder = _machine_with_record_name(tmp_path, cloud, 241)

    first = _sync(vault)
    again = _sync(vault)

    assert first.exit_code == 0, first.output
    assert again.exit_code == 0, again.output
    assert _tree(folder) == _tree(vault)
    # The longest record, <session name>.path1.lst-old, is as long as a name can be.
    workdir = bisync_paths(vault, f"fake-cloud:{folder}").workdir
    lengths = [core_sync.name_length(name, windows=_WINDOWS) for name in os.listdir(workdir)]
    assert max(lengths) == 255


@needs_rclone
def test_a_library_one_character_over_is_refused_before_anything_is_touched(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, folder = _machine_with_record_name(tmp_path, cloud, 242)
    workdir = bisync_paths(vault, f"fake-cloud:{folder}").workdir
    tree, state = _tree(vault), read_sync_state(vault)

    refused = _sync(vault)

    assert refused.exit_code == 1
    assert isinstance(refused.exception, SyncError)
    assert str(refused.exception) == _TOO_LONG.format("1 character") + _SHORTER_CLOUD_PATH
    # Nothing went up and nothing was written, on either side.
    assert not folder.exists()
    assert not workdir.exists()
    assert _tree(vault) == tree
    assert read_sync_state(vault) == state

    # The limit is rclone's: without the check, rclone itself cannot save its
    # records for this pair.
    monkeypatch.setattr(core_sync, "require_bisync_names_fit", lambda *args: None)
    unchecked = _sync(vault)
    assert unchecked.exit_code != 0, unchecked.output
    assert not core_sync.bisync_initialised(workdir)


# ---------------------------------------------------------------------------
# Conflicts, deletions, safety stops
# ---------------------------------------------------------------------------


@needs_rclone
@pytest.mark.parametrize("b_is_newer", [True, False])
def test_conflict_keeps_the_newer_copy_and_moves_the_older_out(
    tmp_path: Path, cloud: Path, b_is_newer: bool
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    now = time.time()
    a_time, b_time = (now - 200, now - 100) if b_is_newer else (now - 100, now - 200)
    _edit_metadata(a, "p1", "edited-on-a", a_time)
    _edit_metadata(b, "p1", "edited-on-b-longer", b_time)
    a_copy = (a / "papers" / "p1" / "metadata.yaml").read_bytes()
    b_copy = (b / "papers" / "p1" / "metadata.yaml").read_bytes()
    assert _sync(a).exit_code == 0

    result = _sync(b)

    assert result.exit_code == sync_cmd.EXIT_SYNC_CONFLICTS == 21, result.output
    assert "1 conflict" in result.output
    assert "p1 · metadata.yaml" in result.output
    newer, older = (b_copy, a_copy) if b_is_newer else (a_copy, b_copy)
    kept_word = "this machine's" if b_is_newer else "the cloud's"
    assert f"kept {kept_word} copy" in result.output
    assert (b / "papers" / "p1" / "metadata.yaml").read_bytes() == newer
    backed_up = {
        rel: data
        for rel, data in _local_backup_files(b, cloud).items()
        if ".sync-conflict" in rel
    }
    assert list(backed_up.values()) == [older]
    assert read_sync_state(b).last_sync_result == "conflicts"

    assert _sync(a).exit_code == 0
    assert (a / "papers" / "p1" / "metadata.yaml").read_bytes() == newer
    assert _conflict_leftovers(a, b, cloud / "lib") == []
    assert _tree(a) == _tree(b) == _tree(cloud / "lib")


@needs_rclone
def test_a_paper_deleted_on_one_machine_goes_on_the_other_into_its_backup(
    tmp_path: Path, cloud: Path
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    deleted = (b / "papers" / "p2" / "metadata.yaml").read_bytes()
    locking.rmtree(a / "papers" / "p2")

    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0

    assert not (b / "papers" / "p2").exists()
    assert _local_backup_files(b, cloud)["papers/p2/metadata.yaml"] == deleted
    assert any((cloud / "lib-litman-backup").rglob("metadata.yaml"))


@needs_rclone
def test_deleting_most_of_the_library_is_stopped_and_nothing_goes_remotely(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    for i in range(1, 11):
        _write_paper(a, f"p{i}")
    assert _sync(a).exit_code == 0
    for i in range(1, 8):
        locking.rmtree(a / "papers" / f"p{i}")

    result = _sync(a)

    assert result.exit_code == 1
    assert isinstance(result.exception, SyncError)
    assert "more than half of the files" in str(result.exception)
    assert "lit sync --force" in str(result.exception)
    assert len(list((cloud / "lib" / "papers").iterdir())) == 10
    assert read_sync_state(a).last_sync_result == "failed"


@needs_rclone
def test_force_lets_a_mass_deletion_through_with_backups(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    for i in range(1, 5):
        _write_paper(a, f"p{i}")
    assert _sync(a).exit_code == 0
    for i in range(1, 4):
        locking.rmtree(a / "papers" / f"p{i}")
    assert _sync(a).exit_code == 1

    result = _sync(a, "--force")

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (cloud / "lib" / "papers").iterdir()) == ["p4"]
    assert len(list((cloud / "lib-litman-backup").rglob("metadata.yaml"))) == 3


# ---------------------------------------------------------------------------
# After the sync: locks, derived files, per-machine config
# ---------------------------------------------------------------------------


def _index_without_stamp(vault: Path) -> dict[str, object]:
    payload = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    payload.pop("generated_at")
    return payload


def _views_listing(vault: Path) -> list[str]:
    return sorted(p.relative_to(vault / "views").as_posix() for p in (vault / "views").rglob("*"))


@needs_rclone
def test_truth_is_locked_and_derived_files_rebuilt_after_a_sync(
    tmp_path: Path, cloud: Path
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud, n_papers=3)
    _write_paper(a, "p9", status="reading", mtime=time.time() - 100)
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0

    truth = _truth_files(b)
    assert len(truth) == 1 + 2 * 4
    assert not [str(f) for f in truth if os.access(f, os.W_OK)]
    index_after_sync = _index_without_stamp(b)
    views_after_sync = _views_listing(b)
    assert index_after_sync["n_papers"] == 4

    refreshed = CliRunner().invoke(cli, ["refresh-views", "--library", str(b)])
    assert refreshed.exit_code == 0, refreshed.output
    assert _index_without_stamp(b) == index_after_sync
    assert _views_listing(b) == views_after_sync


@needs_rclone
def test_lit_config_stays_per_machine_and_a_new_project_can_get_a_folder_here(
    tmp_path: Path, cloud: Path
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud, n_papers=2)
    folder_a = tmp_path / "project-on-a"
    folder_a.mkdir()
    added = CliRunner().invoke(
        cli, ["project", "add", "peptides", "--path", str(folder_a), "--library", str(a)]
    )
    assert added.exit_code == 0, added.output
    config_b = (b / "lit-config.yaml").read_bytes()
    assert _sync(a).exit_code == 0

    result = _sync(b)

    assert result.exit_code == 0, result.output
    assert (b / "lit-config.yaml").read_bytes() == config_b
    assert "peptides" in (b / "TAXONOMY.md").read_text(encoding="utf-8")
    assert "No folder on this machine for project:" in result.output
    assert 'lit project set-path peptides "<folder>"' in result.output

    folder_b = tmp_path / "project-on-b"
    folder_b.mkdir()
    set_path = CliRunner().invoke(
        cli, ["project", "set-path", "peptides", str(folder_b), "--library", str(b)],
        input="n\n",
    )
    assert set_path.exit_code == 0, set_path.output
    assert "peptides" in (b / "lit-config.yaml").read_text(encoding="utf-8")
    assert "No folder on this machine" not in _sync(b).output


# ---------------------------------------------------------------------------
# Repeat runs, filters, the hook for auto sync
# ---------------------------------------------------------------------------


@needs_rclone
def test_a_sync_with_nothing_new_changes_nothing_and_reports_no_remote_change(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    first = two_way_sync(a, _target(cloud))
    assert first.mode == "first-local" and first.remote_changed

    again = two_way_sync(a, _target(cloud))

    assert again.mode == "sync"
    assert again.came_down.total == 0
    assert again.went_up is not None and again.went_up.total == 0
    assert again.remote_changed is False
    assert again.deletions_unchecked is False

    _edit_notes(a, "p1", "# Paper p1\nmore\n", time.time() - 50)
    edited = two_way_sync(a, _target(cloud))
    assert edited.went_up is not None
    assert edited.went_up.changed == ("papers/p1/notes.md",)
    assert edited.remote_changed is True
    assert edited.deletions_unchecked is False


@needs_rclone
def test_a_changed_filter_set_asks_for_a_choice_instead_of_crashing(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    filters = bisync_paths(a.resolve(), _target(cloud)).filters_file
    filters.write_text(filters.read_text(encoding="utf-8") + "- /extra/**\n", encoding="utf-8")

    refused = _sync(a)

    assert refused.exit_code == 20
    assert "has changed since the last sync" in refused.output
    started_over = _sync(a, "--first-from", "local")
    assert started_over.exit_code == 0, started_over.output
    assert filters.read_text(encoding="utf-8") == two_way_filter_rules()
    assert _sync(a).exit_code == 0


@needs_rclone
def test_an_emptied_cloud_folder_asks_before_filling_it_again(
    tmp_path: Path, cloud: Path
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    shutil.rmtree(cloud / "lib")

    refused = _sync(a)

    assert refused.exit_code == 20
    assert "The cloud folder is empty" in refused.output
    assert "lit sync --first-from local" in refused.output
    assert (a / "papers" / "p1" / "metadata.yaml").exists()
    assert _sync(a, "--first-from", "local").exit_code == 0
    assert _tree(cloud / "lib") == _tree(a)


@needs_rclone
def test_the_installed_rclone_passes_the_version_check(cloud: Path) -> None:
    """The real default behind the ``run_rclone`` seam."""
    assert require_bisync_rclone() >= BISYNC_MIN_VERSION


def test_a_too_old_rclone_is_named_with_the_way_to_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_rclone(
        args: list[str], *, capture: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        stdout = {"version": "rclone v1.66.0\n- os/version: test\n", "listremotes": "fake-cloud:\n"}
        return subprocess.CompletedProcess(args, 0, stdout=stdout[args[0]], stderr="")

    monkeypatch.setattr(core_sync, "run_rclone", fake_run_rclone)
    monkeypatch.setattr(sync_cmd, "remote_exists", lambda name: True)
    vault = create_vault(tmp_path)
    write_sync_to_config(
        vault / "lit-config.yaml", SetupPayload(remote="fake-cloud", path="lib/")
    )

    result = _sync(vault)

    assert result.exit_code == 1
    message = str(result.exception)
    assert "rclone 1.66.0 is too old for two-way sync" in message
    assert "1.74.2 or newer" in message
    assert "rclone selfupdate" in message


def test_a_missing_rclone_says_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = create_vault(tmp_path)
    write_sync_to_config(
        vault / "lit-config.yaml", SetupPayload(remote="fake-cloud", path="lib/")
    )
    monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin"))

    result = _sync(vault)

    assert result.exit_code == 1
    assert "rclone" in str(result.exception)
    assert "https://rclone.org/install/" in str(result.exception)


def test_the_remote_top_level_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="a folder inside the remote"):
        core_sync.remote_backup_root("gdrive:")
    with pytest.raises(SyncError, match="a folder inside the remote"):
        core_sync.remote_backup_root("gdrive:/")
    assert core_sync.remote_backup_root("gdrive:litman-vault/") == (
        "gdrive:litman-vault-litman-backup"
    )


def test_the_filter_rules_are_pinned() -> None:
    """Any change here makes every machine start over with --first-from."""
    assert two_way_filter_rules() == (
        "# Written by litman for rclone bisync. Do not edit.\n"
        "- /.litman-staging/**\n"
        "- /.litman-upload/**\n"
        "- /views/**\n"
        "- /.litman-sync-state.yaml\n"
        "- /INDEX.json\n"
        "- /lit-config.yaml\n"
        "- /codes/*/repo/**\n"
        "- /.litman-sync-stamp.json\n"
        "- /INDEX.json.tmp\n"
        "- /lit-config.yaml.tmp\n"
        "- /.litman-sync-state.yaml.tmp\n"
        "- .DS_Store\n"
        "- ._*\n"
        "- Thumbs.db\n"
        "- desktop.ini\n"
        "- *.partial\n"
        "- .litman-link-probe-*\n"
        "- .litman-link-probe-*/**\n"
        "- .litman-write-probe-*\n"
    )


def test_the_local_snapshot_follows_the_filter_rules(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    for rel in (
        "TAXONOMY.md",
        "INDEX.json",
        "lit-config.yaml",
        "views/by-topic/x.md",
        "codes/tool/repo/main.py",
        "codes/tool/repo-meta.yaml",
        "papers/p1/metadata.yaml",
        "papers/p1/supplementary/INDEX.json",
        "papers/p1/.DS_Store",
        "papers/p1/metadata.yaml.sync-conflict2",
        "INDEX.json.tmp",
        "papers/p1/paper.pdf.4b221ac0.partial",
        ".litman-write-probe-12-ab34",
        ".litman-link-probe-target-12/inside",
    ):
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel, encoding="utf-8")

    assert sorted(local_snapshot(vault)) == [
        "TAXONOMY.md",
        "codes/tool/repo-meta.yaml",
        "papers/p1/metadata.yaml",
        "papers/p1/supplementary/INDEX.json",
    ]


# ---------------------------------------------------------------------------
# The conflict sweep's safety net (no rclone needed)
# ---------------------------------------------------------------------------


def test_sweep_moves_the_losing_copy_out_and_reports_whose_copy_was_kept(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "v"
    paper = vault / "papers" / "p1"
    paper.mkdir(parents=True)
    (paper / "metadata.yaml").write_text("kept\n", encoding="utf-8")
    (paper / "metadata.yaml.sync-conflict1").write_text("this machine's\n", encoding="utf-8")
    dest = tmp_path / "backup"

    rows = sweep_conflicts(vault, dest)

    assert [(r.path, r.paper_id, r.kept, r.restored) for r in rows] == [
        ("papers/p1/metadata.yaml", "p1", "cloud", False)
    ]
    assert rows[0].backup == dest / "papers" / "p1" / "metadata.yaml.sync-conflict1"
    assert rows[0].backup.read_text(encoding="utf-8") == "this machine's\n"
    assert sorted(p.name for p in paper.iterdir()) == ["metadata.yaml"]


def test_sweep_puts_the_newer_copy_back_when_rclone_kept_neither(tmp_path: Path) -> None:
    """A paper never ends up without metadata.yaml (red line 2)."""
    vault = tmp_path / "v"
    paper = vault / "papers" / "p1"
    paper.mkdir(parents=True)
    older, newer = paper / "metadata.yaml.sync-conflict1", paper / "metadata.yaml.sync-conflict2"
    older.write_text("older, this machine's\n", encoding="utf-8")
    newer.write_text("newer, the cloud's\n", encoding="utf-8")
    _touch(older, _T0)
    _touch(newer, _T0 + 60)

    rows = sweep_conflicts(vault, tmp_path / "backup")

    assert (paper / "metadata.yaml").read_text(encoding="utf-8") == "newer, the cloud's\n"
    assert sorted(p.name for p in paper.iterdir()) == ["metadata.yaml"]
    assert [(r.kept, r.restored) for r in rows] == [("cloud", True)]
    assert rows[0].backup.read_text(encoding="utf-8") == "older, this machine's\n"


def test_unlock_sweep_is_the_inverse_of_the_lock_sweep(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    _write_paper(vault, "p1")
    si = vault / "papers" / "p1" / "supplementary"
    si.mkdir()
    (si / "table-s1.csv").write_text("a,b\n", encoding="utf-8")
    locking.ensure_truth_locked(vault)
    locked = [f for f in [*_truth_files(vault), si / "table-s1.csv"] if not os.access(f, os.W_OK)]
    assert len(locked) == 4

    assert locking.ensure_truth_unlocked(vault) == 4
    assert all(os.access(f, os.W_OK) for f in locked)
    assert locking.ensure_truth_unlocked(vault) == 0
    assert locking.ensure_truth_locked(vault) == 4


# ---------------------------------------------------------------------------
# State file, CLI surface, docs
# ---------------------------------------------------------------------------


def test_state_file_keeps_the_two_way_fields_next_to_push_and_pull(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    write_sync_state(vault, SyncState(last_push="2026-11-01T10:00:00+01:00"))

    stamp_two_way(vault, "ok")
    stamp_push(vault)
    stamp_two_way(vault, "failed")

    state = read_sync_state(vault)
    assert state.last_push is not None and state.last_push != "2026-11-01T10:00:00+01:00"
    assert state.last_sync is not None
    assert state.last_sync_result == "failed"


def test_options_before_a_subcommand_are_refused_not_dropped(tmp_path: Path) -> None:
    runner = CliRunner()

    misplaced = runner.invoke(cli, ["sync", "--library", str(tmp_path), "status"])
    assert misplaced.exit_code == 2
    assert "lit sync status --library" in misplaced.output

    two_way_only = runner.invoke(cli, ["sync", "--force", "push"])
    assert two_way_only.exit_code == 2
    assert "--force belongs to two-way `lit sync`" in two_way_only.output


def test_lit_library_from_the_environment_still_reaches_the_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIT_LIBRARY", str(tmp_path))
    result = CliRunner().invoke(cli, ["sync", "setup", "--help"])
    assert result.exit_code == 0, result.output


def test_sync_help_documents_two_way_mode_and_its_exit_codes() -> None:
    result = CliRunner().invoke(cli, ["sync", "--help"])
    assert result.exit_code == 0
    assert "--first-from" in result.output
    assert "20 waiting for your --first-from" in " ".join(result.output.split())


def test_exit_codes_are_documented_in_the_command_reference() -> None:
    doc = (Path(__file__).parents[2] / "docs" / "4-commands.md").read_text(encoding="utf-8")
    section = doc.split("### `lit sync`", 1)[1].split("\n### ", 1)[0]
    for code in (sync_cmd.EXIT_SYNC_NEEDS_CHOICE, sync_cmd.EXIT_SYNC_CONFLICTS):
        assert f"| `{code}` |" in section


# ---------------------------------------------------------------------------
# A folder deleted (or renamed) on one machine and changed on the other
# ---------------------------------------------------------------------------


_REAL_WIN32 = sys.platform == "win32"


def _lit(*args: str, vault: Path) -> Result:
    return CliRunner().invoke(cli, [*args, "--library", str(vault)])


def _incomplete_papers(root: Path) -> list[str]:
    """Paper folders under ``root`` without both metadata.yaml and paper.pdf."""
    papers = root / "papers"
    if not papers.is_dir():
        return []
    return [
        d.name
        for d in sorted(papers.iterdir())
        if not ((d / "metadata.yaml").is_file() and (d / "paper.pdf").is_file())
    ]


def _sync_in_order(a: Path, b: Path, deleter_first: bool) -> tuple[Path, Result]:
    """Sync A, B, A (deleter first) or B, A, B; return the machine that settled it.

    A is the machine that deletes. The machine syncing second meets the
    collision and must exit 21; the other two runs are ordinary.
    """
    first, second = (a, b) if deleter_first else (b, a)
    assert _sync(first).exit_code == 0
    settled = _sync(second)
    assert settled.exit_code == sync_cmd.EXIT_SYNC_CONFLICTS, settled.output
    last = _sync(first)
    assert last.exit_code == 0, last.output
    return second, settled


def _assert_deletion_won(a: Path, b: Path, cloud: Path, gone: str) -> None:
    for root in (a, b, cloud / "lib"):
        assert _incomplete_papers(root) == []
        assert not (root / "papers" / gone).exists()
    assert _conflict_leftovers(a, b, cloud / "lib") == []
    assert _tree(a) == _tree(b) == _tree(cloud / "lib")


_ORDERS = pytest.mark.parametrize(
    "deleter_first", [True, False], ids=["deleter-syncs-first", "changer-syncs-first"]
)


@needs_rclone
@_ORDERS
def test_a_paper_deleted_on_one_machine_wins_over_a_notes_edit_on_the_other(
    tmp_path: Path, cloud: Path, deleter_first: bool
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    assert _lit("rm", "p2", "-y", vault=a).exit_code == 0
    _edit_notes(b, "p2", "# Paper p2\nwritten at school\n", time.time() - 50)
    changed = (b / "papers" / "p2" / "notes.md").read_bytes()

    settler, result = _sync_in_order(a, b, deleter_first)

    where = "here" if settler == a else "on another machine"
    assert f"p2: deleted or renamed {where}" in result.output
    assert "(notes.md)" in result.output
    assert "lit trash restore p2" in result.output
    assert _local_backup_files(settler, cloud)["papers/p2/notes.md"] == changed
    _assert_deletion_won(a, b, cloud, "p2")
    assert read_sync_state(settler).last_sync_result == "conflicts"


@needs_rclone
@_ORDERS
def test_a_rename_on_one_machine_wins_over_a_notes_edit_on_the_other(
    tmp_path: Path, cloud: Path, deleter_first: bool
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    renamed = _lit("rename", "p2", "p9", vault=a)
    assert renamed.exit_code == 0, renamed.output
    _edit_notes(b, "p2", "# Paper p2\nwritten at school\n", time.time() - 50)
    changed = (b / "papers" / "p2" / "notes.md").read_bytes()

    settler, result = _sync_in_order(a, b, deleter_first)

    assert "p2: deleted or renamed" in result.output
    assert "lit trash restore" not in result.output
    assert _local_backup_files(settler, cloud)["papers/p2/notes.md"] == changed
    _assert_deletion_won(a, b, cloud, "p2")
    for root in (a, b, cloud / "lib"):
        assert (root / "papers" / "p9" / "metadata.yaml").is_file()


@needs_rclone
@_ORDERS
def test_a_paper_deleted_on_one_machine_wins_over_marking_it_read_on_the_other(
    tmp_path: Path, cloud: Path, deleter_first: bool
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    assert _lit("rm", "p2", "-y", vault=a).exit_code == 0
    assert _lit("read", "p2", vault=b).exit_code == 0
    changed = (b / "papers" / "p2" / "metadata.yaml").read_bytes()

    settler, result = _sync_in_order(a, b, deleter_first)

    # `lit read` may also backfill a reminder into notes.md, so both can be listed.
    assert re.search(r"p2: deleted or renamed [^;]+; [^(]+\([^)]*metadata\.yaml", result.output)
    assert "lit trash restore p2" in result.output
    assert _local_backup_files(settler, cloud)["papers/p2/metadata.yaml"] == changed
    _assert_deletion_won(a, b, cloud, "p2")


@needs_rclone
@_ORDERS
def test_a_code_folder_deleted_on_one_machine_wins_and_its_checkout_is_left_alone(
    tmp_path: Path, cloud: Path, deleter_first: bool
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud, n_papers=2)
    tool_a = a / "codes" / "tool"
    tool_a.mkdir(parents=True)
    (tool_a / "repo-meta.yaml").write_text("name: tool\n", encoding="utf-8")
    (tool_a / "notes.md").write_text("# tool\n", encoding="utf-8")
    for name in ("repo-meta.yaml", "notes.md"):
        _touch(tool_a / name, _T0)
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0
    checkout = b / "codes" / "tool" / "repo"
    checkout.mkdir()
    (checkout / "README").write_text("work in progress\n", encoding="utf-8")
    locking.rmtree(tool_a)
    # Bytes, compared as bytes below: write_text would end lines with \r\n on Windows.
    (b / "codes" / "tool" / "notes.md").write_bytes(b"# tool\nedited\n")
    _touch(b / "codes" / "tool" / "notes.md", time.time() - 50)

    settler, result = _sync_in_order(a, b, deleter_first)

    assert "codes/tool: deleted or renamed" in result.output
    if settler == b:
        assert "codes/tool/repo/ stays on this machine" in result.output
    assert _local_backup_files(settler, cloud)["codes/tool/notes.md"] == b"# tool\nedited\n"
    for root in (a, b, cloud / "lib"):
        assert not any(rel.startswith("codes/tool/") for rel in _tree(root))
    assert (checkout / "README").read_text(encoding="utf-8") == "work in progress\n"


@needs_rclone
def test_a_new_paper_only_partly_uploaded_is_not_taken_for_a_deletion(
    tmp_path: Path, cloud: Path
) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    half = cloud / "lib" / "papers" / "new1"
    half.mkdir()
    (half / "metadata.yaml").write_text("id: new1\n", encoding="utf-8")
    _touch(half / "metadata.yaml", time.time() - 50)

    result = _sync(a)

    assert result.exit_code == 0, result.output
    assert (a / "papers" / "new1" / "metadata.yaml").is_file()
    assert "deleted on one machine" not in result.output


@needs_rclone
def test_a_single_file_deleted_on_purpose_is_just_synced(
    tmp_path: Path, cloud: Path
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    (a / "papers" / "p1" / "notes.md").unlink()

    assert _sync(a).exit_code == 0
    result = _sync(b)

    assert result.exit_code == 0, result.output
    assert not (b / "papers" / "p1" / "notes.md").exists()
    assert (b / "papers" / "p1" / "metadata.yaml").is_file()
    assert (b / "papers" / "p1" / "paper.pdf").is_file()


def _append_truth(path: Path, data: bytes, mtime: float) -> None:
    locking.unlock_truth_file(path)
    with path.open("ab") as fh:
        fh.write(data)
    _touch(path, mtime)
    locking.lock_truth_file(path)


@needs_rclone
@_ORDERS
def test_a_deletion_wins_even_when_the_other_machine_changed_every_file(
    tmp_path: Path, cloud: Path, deleter_first: bool
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    assert _lit("rm", "p2", "-y", vault=a).exit_code == 0
    later = time.time() - 50
    _edit_metadata(b, "p2", "read", later)
    _edit_notes(b, "p2", "# Paper p2\nwritten at school\n", later)
    _append_truth(b / "papers" / "p2" / "paper.pdf", b"% highlight\n", later)
    changed = {
        f"papers/p2/{name}": (b / "papers" / "p2" / name).read_bytes()
        for name in ("metadata.yaml", "notes.md", "paper.pdf")
    }

    settler, result = _sync_in_order(a, b, deleter_first)

    where = "here" if settler == a else "on another machine"
    assert f"p2: deleted or renamed {where}" in result.output
    assert "(metadata.yaml, notes.md, paper.pdf)" in result.output
    backups = _local_backup_files(settler, cloud)
    for rel, data in changed.items():
        assert backups[rel] == data
    _assert_deletion_won(a, b, cloud, "p2")


@needs_rclone
@_ORDERS
def test_an_si_file_deleted_on_one_machine_leaves_the_paper_whole_everywhere(
    tmp_path: Path, cloud: Path, deleter_first: bool
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    source = tmp_path / "table.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    added = _lit("add", "--si", "p2", str(source), "--force", vault=a)
    assert added.exit_code == 0, added.output
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0
    si = Path("papers", "p2", "supplementary", "si-01.csv")
    assert (b / si).is_file()

    removed = _lit("rm", "p2", "--si", "si-01", "-y", vault=a)
    assert removed.exit_code == 0, removed.output
    # The other machine reads the paper: every file but the SI file changes there.
    now = time.time()
    _touch_truth(a / "papers" / "p2" / "metadata.yaml", now - 50)
    _edit_metadata(b, "p2", "read", now - 100)
    _edit_notes(b, "p2", "# Paper p2\nread at school\n", now - 100)
    _append_truth(b / "papers" / "p2" / "paper.pdf", b"% highlight\n", now - 100)
    read_there = {
        name: (b / "papers" / "p2" / name).read_bytes() for name in ("notes.md", "paper.pdf")
    }

    first, second = (a, b) if deleter_first else (b, a)
    assert _sync(first).exit_code == 0
    settled = _sync(second)
    last = _sync(first)

    # Both machines changed metadata.yaml: an ordinary conflict, the newer (A's) kept.
    assert settled.exit_code == sync_cmd.EXIT_SYNC_CONFLICTS, settled.output
    assert "p2 · metadata.yaml" in settled.output
    assert "deleted or renamed" not in settled.output
    assert last.exit_code == 0, last.output
    for root in (a, b, cloud / "lib"):
        assert _incomplete_papers(root) == []
        for name, data in read_there.items():
            assert (root / "papers" / "p2" / name).read_bytes() == data
        assert not (root / si).exists()
        assert b"si-01" not in (root / "papers" / "p2" / "metadata.yaml").read_bytes()
    assert _conflict_leftovers(a, b, cloud / "lib") == []
    assert _tree(a) == _tree(b) == _tree(cloud / "lib")


def _recording_runs(monkeypatch: pytest.MonkeyPatch) -> list[core_sync.BisyncRun]:
    """Every bisync run from here on, in order."""
    runs: list[core_sync.BisyncRun] = []
    real = core_sync.run_bisync

    def recording(
        args: list[str], on_log: Callable[[str], None] | None = None
    ) -> core_sync.BisyncRun:
        run = real(args, on_log)
        runs.append(run)
        return run

    monkeypatch.setattr(core_sync, "run_bisync", recording)
    return runs


@needs_rclone
def test_bisync_reports_every_changed_file_and_litman_reads_each_one_back(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _b = _two_synced_machines(tmp_path, cloud)
    later = time.time() - 50
    # This machine: a file changed, one added, one deleted.
    _edit_notes(a, "p1", "# Paper p1\nedited here\n", later)
    (a / "papers" / "p1" / "extra.md").write_text("extra\n", encoding="utf-8")
    (a / "papers" / "p2" / "notes.md").unlink()
    # The cloud, as other machines left it: a paper deleted whole, a file
    # changed, files added; a name with a double quote is quoted in the report
    # (Windows allows none).
    lib = cloud / "lib"
    locking.rmtree(lib / "papers" / "p3")
    (lib / "papers" / "p4" / "notes.md").write_text("# Paper p4\nedited there\n", encoding="utf-8")
    _touch(lib / "papers" / "p4" / "notes.md", later)
    added = ["new.md", "a - b c.md", *([] if _REAL_WIN32 else ['it\'s "odd" - x.md'])]
    for name in added:
        (lib / "papers" / "p4" / name).write_text("new\n", encoding="utf-8")
    runs = _recording_runs(monkeypatch)

    result = _sync(a)

    assert result.exit_code == 0, result.output
    assert core_sync.parse_bisync_deltas(runs[0].report) == core_sync.BisyncDeltas(
        local=_side(
            new=("papers/p1/extra.md",),
            changed=("papers/p1/notes.md",),
            deleted=("papers/p2/notes.md",),
        ),
        cloud=_side(
            new=tuple(f"papers/p4/{name}" for name in added),
            changed=("papers/p4/notes.md",),
            deleted=(
                "papers/p3",
                "papers/p3/metadata.yaml",
                "papers/p3/notes.md",
                "papers/p3/paper.pdf",
            ),
        ),
    )
    # The report stays out of sight; rclone's other lines still show.
    assert "File was deleted" not in result.output
    assert "INFO" not in result.output
    assert "NOTICE" in result.output


@needs_rclone
def test_rclone_log_settings_in_the_environment_change_nothing_litman_reads(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    assert _lit("rm", "p2", "-y", vault=a).exit_code == 0
    _edit_notes(b, "p2", "# Paper p2\nwritten at school\n", time.time() - 50)
    assert _sync(a).exit_code == 0
    # RCLONE_VERBOSE next to --log-level stops rclone before it starts; the
    # others reword, reformat or redirect the log.
    monkeypatch.setenv("RCLONE_VERBOSE", "2")
    monkeypatch.setenv("RCLONE_USE_JSON_LOG", "true")
    monkeypatch.setenv("RCLONE_LOG_FORMAT", "date,time,microseconds,shortfile")
    monkeypatch.setenv("RCLONE_LOG_FILE", str(tmp_path / "rclone.log"))

    result = _sync(b)

    assert result.exit_code == sync_cmd.EXIT_SYNC_CONFLICTS, result.output
    assert "p2: deleted or renamed on another machine" in result.output
    assert _sync(a).exit_code == 0
    _assert_deletion_won(a, b, cloud, "p2")


@needs_rclone
def test_an_unreadable_report_moves_no_folder_out_and_says_so(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    assert _lit("rm", "p2", "-y", vault=a).exit_code == 0
    _edit_notes(b, "p2", "# Paper p2\nwritten at school\n", time.time() - 50)
    assert _sync(a).exit_code == 0
    real = core_sync.run_bisync

    def reworded(
        args: list[str], on_log: Callable[[str], None] | None = None
    ) -> core_sync.BisyncRun:
        # As if a later rclone worded its per-file report differently.
        run = real(args, on_log)
        report = tuple(line.replace("File was deleted", "File gone") for line in run.report)
        return replace(run, report=report)

    monkeypatch.setattr(core_sync, "run_bisync", reworded)

    result = _sync(b)

    assert result.exit_code == 0, result.output
    assert "Could not read rclone's per-file report" in result.output
    assert "deleted or renamed" not in result.output
    # A half folder, not a paper gone: nothing was moved out.
    assert sorted(p.name for p in (b / "papers" / "p2").iterdir()) == ["notes.md"]
    conflicts = bisync_paths(b.resolve(), _target(cloud)).conflicts_root
    assert not any(path.is_file() for path in conflicts.rglob("*"))


@needs_rclone
def test_backup_paths_print_whole_under_the_summary_to_be_copied(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    now = time.time()
    _edit_notes(a, "p1", "# Paper p1\nhome\n", now - 100)
    _edit_notes(b, "p1", "# Paper p1\nschool\n", now - 50)
    assert _sync(a).exit_code == 0
    monkeypatch.setattr(sync_cmd, "console", Console(width=60))

    result = _sync(b)

    assert result.exit_code == sync_cmd.EXIT_SYNC_CONFLICTS, result.output
    conflicts = bisync_paths(b.resolve(), _target(cloud)).conflicts_root
    (backup,) = conflicts.rglob("notes.md.sync-conflict*")
    assert len(str(backup)) > 60
    assert f"  {backup}" in result.output.splitlines()


def _side(
    new: tuple[str, ...] = (), changed: tuple[str, ...] = (), deleted: tuple[str, ...] = ()
) -> core_sync.SideDeltas:
    return core_sync.SideDeltas(frozenset(new), frozenset(changed), frozenset(deleted))


def _files(*rels: str) -> dict[str, tuple[int, int]]:
    return dict.fromkeys(rels, (1, 1))


_PAPER = ("papers/p/metadata.yaml", "papers/p/paper.pdf", "papers/p/notes.md")
_WHOLE_PAPER = ("papers/p", *_PAPER)  # bisync reports the folder too
_NOTES = ("papers/p/notes.md",)
_SI = ("papers/p/supplementary", "papers/p/supplementary/si-01.csv")


@pytest.mark.parametrize(
    ("local", "cloud_side", "after", "expected"),
    [
        pytest.param(
            _side(changed=_NOTES),
            _side(deleted=_WHOLE_PAPER),
            _files(*_NOTES),
            [("papers/p", False)],
            id="deleted-elsewhere-notes-changed-here",
        ),
        pytest.param(
            _side(deleted=_WHOLE_PAPER),
            _side(changed=_NOTES),
            _files(*_NOTES),
            [("papers/p", True)],
            id="deleted-here-notes-changed-elsewhere",
        ),
        pytest.param(
            _side(changed=_PAPER),
            _side(deleted=_WHOLE_PAPER),
            _files(*_PAPER),
            [("papers/p", False)],
            id="deleted-elsewhere-every-file-changed-here",
        ),
        pytest.param(
            _side(deleted=_WHOLE_PAPER),
            _side(changed=_PAPER),
            _files(*_PAPER),
            [("papers/p", True)],
            id="deleted-here-every-file-changed-elsewhere",
        ),
        pytest.param(
            _side(new=("papers/p/extra.md",)),
            _side(deleted=_WHOLE_PAPER),
            _files("papers/p/extra.md"),
            [("papers/p", False)],
            id="deleted-elsewhere-a-file-added-here",
        ),
        pytest.param(
            _side(changed=_PAPER),
            _side(changed=("papers/p/metadata.yaml",), deleted=_SI),
            _files(*_PAPER),
            [],
            id="si-removed-elsewhere-every-other-file-changed-here",
        ),
        pytest.param(
            _side(changed=_PAPER),
            _side(deleted=_SI),
            _files(*_PAPER),
            [],
            id="si-file-deleted-elsewhere-every-other-file-changed-here",
        ),
        pytest.param(
            _side(),
            _side(new=("papers/p", "papers/p/metadata.yaml")),
            _files("papers/p/metadata.yaml"),
            [],
            id="new-paper-half-uploaded",
        ),
        pytest.param(
            _side(),
            _side(deleted=("papers/p/paper.pdf",)),
            _files("papers/p/metadata.yaml", "papers/p/notes.md"),
            [],
            id="only-the-pdf-deleted-elsewhere",
        ),
        pytest.param(
            _side(deleted=_NOTES),
            _side(),
            _files("papers/p/metadata.yaml", "papers/p/paper.pdf"),
            [],
            id="a-file-deleted-here-and-nothing-came",
        ),
        pytest.param(
            _side(changed=_NOTES),
            _side(deleted=_PAPER, new=("papers/p/other.md",)),
            _files(*_NOTES, "papers/p/other.md"),
            [],
            id="emptied-and-filled-again-elsewhere",
        ),
        pytest.param(
            _side(deleted=_WHOLE_PAPER, new=("papers/p/extra.md",)),
            _side(new=("papers/p/extra.md",)),
            _files("papers/p/extra.md"),
            [],
            id="emptied-here-but-a-file-added-on-both-sides",
        ),
        pytest.param(
            _side(changed=("codes/t/notes.md",)),
            _side(deleted=("codes/t", "codes/t/repo-meta.yaml", "codes/t/notes.md")),
            _files("codes/t/notes.md"),
            [("codes/t", False)],
            id="code-folder-deleted-elsewhere",
        ),
    ],
)
def test_which_folders_count_as_deleted_on_one_side_and_changed_on_the_other(
    local: core_sync.SideDeltas,
    cloud_side: core_sync.SideDeltas,
    after: dict[str, tuple[int, int]],
    expected: list[tuple[str, bool]],
) -> None:
    deltas = core_sync.BisyncDeltas(local=local, cloud=cloud_side)
    assert core_sync.find_deleted_folder_conflicts(deltas, after) == expected


#: Lines as rclone 1.75.2 prints them at --log-level INFO, date and time taken off.
_REPORT = (
    "INFO  : Path1 checking for diffs",
    "INFO  : - Path1             File changed: size (larger), time (newer)   - papers/x/notes.md",
    "INFO  : - Path1             File is new                                 - papers/y/new.md",
    "INFO  : Path1:    2 changes:    1 new,    1 modified,    0 deleted",
    "INFO  : (Modified:    1 newer,    0 older,    1 larger,    0 smaller)",
    "INFO  : Path2 checking for diffs",
    "INFO  : - Path2             File was deleted                            - papers/x",
    "INFO  : - Path2             File was deleted                            "
    "- papers/x/metadata.yaml",
    "INFO  : - Path2             File was deleted                            "
    '- "papers/z/it\'s \\"q\\" - a.md"',
    "INFO  : Path2:    3 changes:    0 new,    0 modified,    3 deleted",
    "INFO  : Applying changes",
    "INFO  : - Path1             Queue copy to Path2                         "
    "- fake-cloud:/c/papers/x/notes.md",
)


def test_the_per_file_report_is_read_back_into_paths_per_side() -> None:
    assert core_sync.parse_bisync_deltas(_REPORT) == core_sync.BisyncDeltas(
        local=_side(new=("papers/y/new.md",), changed=("papers/x/notes.md",)),
        cloud=_side(deleted=("papers/x", "papers/x/metadata.yaml", 'papers/z/it\'s "q" - a.md')),
    )


def test_a_run_that_found_no_changes_is_an_empty_report_not_an_unreadable_one() -> None:
    report = (
        "INFO  : Path1 checking for diffs",
        "INFO  : Path2 checking for diffs",
        "INFO  : No changes found",
    )
    assert core_sync.parse_bisync_deltas(report) == core_sync.BisyncDeltas()


def _report_without(*dropped: int) -> tuple[str, ...]:
    return tuple(line for i, line in enumerate(_REPORT) if i not in dropped)


@pytest.mark.parametrize(
    "report",
    [
        pytest.param(_report_without(1), id="a-line-lost"),
        pytest.param(_report_without(9), id="a-count-lost"),
        pytest.param(
            tuple(line.replace("File was deleted", "File gone") for line in _REPORT),
            id="reworded",
        ),
        pytest.param(
            tuple(line.replace('\\"q\\"', "\\q") for line in _REPORT),
            id="unreadable-path",
        ),
        pytest.param(
            ("INFO  : Path1 checking for diffs", "INFO  : Path2 checking for diffs"),
            id="neither-counts-nor-no-changes",
        ),
        pytest.param((), id="nothing"),
    ],
)
def test_a_report_that_does_not_add_up_is_not_trusted(report: tuple[str, ...]) -> None:
    assert core_sync.parse_bisync_deltas(report) is None


def _fake_rclone(monkeypatch: pytest.MonkeyPatch, output: str, code: int = 0) -> list[str]:
    """Make "rclone" a Python that prints ``output`` and exits ``code``; return its args."""
    monkeypatch.setattr(core_sync, "RCLONE_BIN", sys.executable)
    return ["-c", f"import sys; sys.stdout.write({output!r}); sys.exit({code})"]


def test_records_below_notice_go_to_the_report_with_their_continuation_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _fake_rclone(
        monkeypatch,
        "2026/11/05 17:00:00 NOTICE: a notice\n"
        "2026/11/05 17:00:00 INFO  : Bisyncing with Comparison Settings: \n"
        "{\n"
        '\t"Modtime": true\n'
        "}\n"
        "2026/11/05 17:00:00 INFO  : - Path1             File is new"
        "                                 - a/b.md\n"
        "2026/11/05 17:00:00 ERROR : an error\n"
        "the rest of the error\n",
    )
    shown: list[str] = []

    run = core_sync.run_bisync(args, shown.append)

    assert run.returncode == 0
    assert list(run.lines) == shown == [
        "NOTICE: a notice",
        "ERROR : an error",
        "the rest of the error",
    ]
    assert run.report == (
        "INFO  : Bisyncing with Comparison Settings: ",
        "{",
        '\t"Modtime": true',
        "}",
        "INFO  : - Path1             File is new                                 - a/b.md",
    )


def test_a_crash_right_after_a_hidden_record_is_shown_after_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _fake_rclone(
        monkeypatch,
        "2026/11/05 17:00:00 INFO  : Lockfile info: \n"
        "{\n"
        "}\n"
        "2026/11/05 17:00:00 NOTICE: a notice\n"
        "2026/11/05 17:00:00 INFO  : Path1 checking for diffs\n"
        "panic: runtime error: index out of range\n"
        "goroutine 1 [running]:\n",
        code=2,
    )
    shown: list[str] = []

    run = core_sync.run_bisync(args, shown.append)
    error = core_sync._bisync_failure(run.returncode, list(run.lines), Path("."))

    # What follows the last record without a date is no part of it.
    assert run.returncode == 2
    assert list(run.lines) == shown == [
        "NOTICE: a notice",
        "panic: runtime error: index out of range",
        "goroutine 1 [running]:",
    ]
    assert "panic: runtime error: index out of range" in str(error)


# ---------------------------------------------------------------------------
# TAXONOMY.md conflicts keep the values both machines added
# ---------------------------------------------------------------------------


def _touch_truth(path: Path, mtime: float) -> None:
    locking.unlock_truth_file(path)
    _touch(path, mtime)
    locking.lock_truth_file(path)


@needs_rclone
@pytest.mark.parametrize("a_is_newer", [False, True], ids=["b-newer", "a-newer"])
def test_a_taxonomy_conflict_adds_back_what_only_the_older_copy_had(
    tmp_path: Path, cloud: Path, a_is_newer: bool
) -> None:
    from litman.core.checks.taxonomy import check_taxonomy_drift
    from litman.core.document import list_papers
    from litman.core.taxonomy import parse_taxonomy, read_taxonomy_text

    a, b = _two_synced_machines(tmp_path, cloud, n_papers=2)
    assert _lit("taxonomy", "add", "topics", "zebrafish", vault=a).exit_code == 0
    tagged = _lit("modify", "p1", "--add-tag", "topics=zebrafish", vault=a)
    assert tagged.exit_code == 0, tagged.output
    folder = tmp_path / "home-project-folder"
    folder.mkdir()
    assert _lit("project", "add", "homework", "--path", str(folder), vault=a).exit_code == 0
    assert _lit("taxonomy", "add", "topics", "quasar", vault=b).exit_code == 0
    now = time.time()
    _touch_truth(a / "TAXONOMY.md", now - (100 if a_is_newer else 200))
    _touch_truth(b / "TAXONOMY.md", now - (200 if a_is_newer else 100))

    assert _sync(a).exit_code == 0
    result = _sync(b)
    assert _sync(a).exit_code == 0

    assert result.exit_code == 21, result.output
    # One conflict, reported once: writing the values back must not make the
    # next pass see the file changed on both sides again (after a conflict
    # where the cloud's copy won, rclone leaves the file out of its listings).
    assert "1 conflict:" in result.output, result.output
    rows = [line for line in result.output.splitlines() if "• TAXONOMY.md:" in line]
    kept = "the cloud's" if a_is_newer else "this machine's"
    assert len(rows) == 1 and f"kept {kept} copy" in rows[0], result.output
    copies = [
        rel for rel in _local_backup_files(b, cloud)
        if rel.startswith("TAXONOMY.md.sync-conflict")
    ]
    assert copies == [f"TAXONOMY.md.sync-conflict{1 if a_is_newer else 2}"]
    assert "TAXONOMY.md: added back what only the older copy had" in result.output
    restored = "quasar" if a_is_newer else "zebrafish"
    assert restored in result.output
    for vault in (a, b):
        taxonomy = parse_taxonomy(read_taxonomy_text(vault))
        assert {"zebrafish", "quasar"} <= set(taxonomy["topics"])
        assert "homework" in taxonomy["projects"]
        drift = check_taxonomy_drift(vault, list_papers(vault))
        assert [i.message for i in drift if "unregistered" in i.message] == []
        assert not os.access(vault / "TAXONOMY.md", os.W_OK)
    assert "No folder on this machine for project: homework" in result.output
    assert _conflict_leftovers(a, b, cloud / "lib") == []
    assert _tree(a) == _tree(b) == _tree(cloud / "lib")


@needs_rclone
@pytest.mark.parametrize(
    ("edited_before", "local_newer"),
    [({1, 2, 3, 4}, False), ({2}, True)],
    ids=["before-every-pass", "after-the-first-add-back"],
)
def test_taxonomy_values_come_back_when_taxonomy_changes_again_during_the_sync(
    tmp_path: Path,
    cloud: Path,
    monkeypatch: pytest.MonkeyPatch,
    edited_before: set[int],
    local_newer: bool,
) -> None:
    from litman.core.taxonomy import parse_taxonomy, read_taxonomy_text

    a, b = _two_synced_machines(tmp_path, cloud, n_papers=2)
    assert _lit("taxonomy", "add", "topics", "zebrafish", vault=a).exit_code == 0
    assert _lit("taxonomy", "add", "topics", "quasar", vault=b).exit_code == 0
    now = time.time()
    _touch_truth(a / "TAXONOMY.md", now - 100)
    _touch_truth(b / "TAXONOMY.md", now - 200)
    assert _sync(a).exit_code == 0

    # Someone changes TAXONOMY.md on both sides again while B syncs, so the
    # next pass meets a new conflict on it. Changed before every pass, the
    # passes run out with values still to add back; changed before the pass
    # that takes the first add-back up, values are added back a second time.
    real = core_sync.run_bisync
    passes: list[int] = []

    def edited(
        args: list[str], on_log: Callable[[str], None] | None = None
    ) -> core_sync.BisyncRun:
        n = len(passes)
        passes.append(n)
        if n in edited_before:
            here, there = (30, 60) if local_newer else (60, 30)
            for path, value, age in (
                (b / "TAXONOMY.md", f"here-{n}", here),
                (cloud / "lib" / "TAXONOMY.md", f"there-{n}", there),
            ):
                locking.unlock_truth_file(path)
                text = path.read_text(encoding="utf-8")
                path.write_text(
                    text.replace("## topics\n", f"## topics\n- {value}\n", 1),
                    encoding="utf-8",
                )
                _touch(path, now - age + n)
        return real(args, on_log)

    monkeypatch.setattr(core_sync, "run_bisync", edited)
    result = _sync(b)

    assert result.exit_code == 21, result.output
    assert len(passes) == 5  # the first pass and the four that may follow
    lost = {"quasar", *(f"{'there' if local_newer else 'here'}-{n}" for n in edited_before)}
    assert lost <= set(parse_taxonomy(read_taxonomy_text(b))["topics"])
    [line] = [x for x in result.output.splitlines() if "added back what only" in x]
    assert all(value in line for value in lost), line
    assert _conflict_leftovers(b) == []


# ---------------------------------------------------------------------------
# rclone's half-written files and its lock
# ---------------------------------------------------------------------------


def test_leftover_partial_files_are_moved_out_of_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    for rel in (
        "papers/p1/paper.pdf.4b221ac0.partial",
        "papers/p1/notes.partial",
        "views/by-topic/x.0123abcd.partial",
    ):
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel, encoding="utf-8")
    dest = tmp_path / "backup"

    moved = core_sync.sweep_partials(vault, dest)

    assert moved == ["papers/p1/paper.pdf.4b221ac0.partial"]
    assert (dest / "papers" / "p1" / "paper.pdf.4b221ac0.partial").is_file()
    assert (vault / "papers" / "p1" / "notes.partial").is_file()
    assert (vault / "views" / "by-topic" / "x.0123abcd.partial").is_file()


@needs_rclone
def test_partial_files_never_travel_and_leave_this_vault_after_a_sync(
    tmp_path: Path, cloud: Path
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud)
    (a / "papers" / "p1" / "paper.pdf.4b221ac0.partial").write_bytes(b"half")
    (cloud / "lib" / "papers" / "p2" / "big.bin.0a1b2c3d.partial").write_bytes(b"half")

    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0

    assert not (a / "papers" / "p1" / "paper.pdf.4b221ac0.partial").exists()
    assert not (cloud / "lib" / "papers" / "p1" / "paper.pdf.4b221ac0.partial").exists()
    assert not (a / "papers" / "p2" / "big.bin.0a1b2c3d.partial").exists()
    assert not (b / "papers" / "p2" / "big.bin.0a1b2c3d.partial").exists()


def _write_lock(workdir: Path, name: str, pid: int, expires: datetime) -> None:
    (workdir / name).write_text(
        json.dumps(
            {
                "Session": name,
                "PID": str(pid),
                "TimeRenewed": expires.isoformat(),
                "TimeExpires": expires.isoformat(),
            }
        ),
        encoding="utf-8",
    )


def test_the_lock_message_tells_a_cut_off_sync_from_a_running_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expires = datetime.now().astimezone() + timedelta(minutes=4)
    _write_lock(tmp_path, "a..b.lck", 4242, expires)
    at = expires.strftime("%H:%M")

    monkeypatch.setattr(core_sync, "_pid_running", lambda pid: False)
    cut_off = core_sync._lock_message(tmp_path)
    monkeypatch.setattr(core_sync, "_pid_running", lambda pid: True)
    running = core_sync._lock_message(tmp_path)
    monkeypatch.setattr(core_sync, "_pid_running", lambda pid: None)
    unknown = core_sync._lock_message(tmp_path)

    assert "was cut off and left its lock behind" in cut_off and at in cut_off
    assert "is running on this machine" in running
    assert "one is running, or the last one was cut off" in unknown and at in unknown


def test_pid_check_through_the_real_default() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    if _REAL_WIN32:
        assert core_sync._pid_running(os.getpid()) is None
    else:
        assert core_sync._pid_running(os.getpid()) is True
        assert core_sync._pid_running(child.pid) is False


@needs_rclone
def test_a_lock_left_by_a_killed_sync_names_its_expiry(
    tmp_path: Path, cloud: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _machine(tmp_path, cloud, "a")
    _write_paper(a, "p1")
    assert _sync(a).exit_code == 0
    workdir = bisync_paths(a.resolve(), _target(cloud)).workdir
    session = next(n for n in os.listdir(workdir) if n.endswith(".path1.lst"))
    expires = datetime.now().astimezone() + timedelta(minutes=4)
    _write_lock(workdir, session[: -len(".path1.lst")] + ".lck", 4242, expires)
    monkeypatch.setattr(core_sync, "_pid_running", lambda pid: False)

    result = _sync(a)

    assert result.exit_code == 1
    assert "was cut off and left its lock behind" in str(result.exception)
    assert expires.strftime("%H:%M") in str(result.exception)
    # rclone's own way out (delete the lock by hand) is not offered as a second one.
    assert "deletefile" not in str(result.exception)
