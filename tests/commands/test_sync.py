"""Tests for ``lit sync`` group — M6.1 (rclone integration basics).

Strategy
--------

We do not mock rclone. The tests run against the real ``rclone`` binary on
PATH (verified by ``test_rclone_available`` first) and use a temporary,
test-scoped rclone config file holding a single ``[fake-cloud]`` remote of
type ``local``. That gives us a fully real ``rclone listremotes`` / ``rclone
sync`` / ``rclone size --json`` exercise without touching any cloud
account.

The ``fake_rclone_env`` fixture writes the temp config, points
``RCLONE_CONFIG`` at it, and returns the storage directory the remote
ultimately reads/writes. Test bodies pass ``fake-cloud:<storage>`` as the
sync target — rclone's ``local`` backend interprets the path after ``:``
as a regular filesystem path.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.config import SyncConfig, load_config
from litman.core.library import create_vault
from litman.core.sync import (
    DEFAULT_EXCLUDES,
    SYNC_STATE_FILENAME,
    SetupPayload,
    Size,
    SyncState,
    build_exclude_args,
    codes_ignore_patterns_to_rclone,
    compute_status,
    format_iso,
    humanize_bytes,
    largest_files,
    list_remotes,
    local_vault_size,
    pull,
    push,
    rclone_available,
    read_sync_state,
    remote_exists,
    remote_size,
    stamp_pull,
    stamp_push,
    write_sync_state,
    write_sync_to_config,
)
from litman.exceptions import SyncError

_yaml_safe = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Skip the whole module if rclone isn't installed on the test machine.
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    shutil.which("rclone") is None,
    reason="rclone not installed; M6.1 integration tests require it on PATH.",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A fresh vault under tmp_path."""
    return create_vault(tmp_path)


@pytest.fixture
def fake_rclone_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point RCLONE_CONFIG at a temp config defining a [fake-cloud] remote.

    Returns the storage directory the test should use as the rclone *path*
    (the bit after ``fake-cloud:``). The remote itself is type ``local``,
    so files end up as plain files under that directory.
    """
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[fake-cloud]\n"
        "type = local\n"
        "nounc = false\n"
    )
    storage = tmp_path / "cloud-storage"
    storage.mkdir()
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    return storage


@pytest.fixture
def configured_vault(
    vault: Path, fake_rclone_env: Path
) -> tuple[Path, str]:
    """A vault wired up to ``fake-cloud:<storage>`` via lit-config.yaml."""
    target_path = str(fake_rclone_env)
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(remote="fake-cloud", path=target_path),
    )
    return vault, f"fake-cloud:{target_path}"


def _seed_paper(vault: Path, paper_id: str, body: str = "content\n") -> None:
    """Drop a health-check-clean paper folder so push has something to transfer.

    Carries the schema-required fields (created-at / updated-at / status) and a
    paper.pdf so the pre-push integrity gate (C-ops1) sees a clean vault. Tests
    that want the gate to fire seed a *broken* paper explicitly.
    """
    paper = vault / "papers" / paper_id
    paper.mkdir(parents=True, exist_ok=True)
    (paper / "metadata.yaml").write_text(
        f"id: {paper_id}\n"
        "title: Test\n"
        "year: 2024\n"
        "status: inbox\n"
        "created-at: '2024-01-01T00:00:00+00:00'\n"
        "updated-at: '2024-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    (paper / "summary.md").write_text(body, encoding="utf-8")
    (paper / "paper.pdf").write_bytes(b"%PDF-1.4\n%minimal\n")


def _seed_broken_paper(vault: Path, paper_id: str) -> None:
    """A paper missing schema-required fields + paper.pdf.

    Trips the pre-push integrity gate (schema + paper_dir_validity errors),
    both klass=validity, so the C-ops1 gate must abort the push.
    """
    paper = vault / "papers" / paper_id
    paper.mkdir(parents=True, exist_ok=True)
    (paper / "metadata.yaml").write_text(
        f"id: {paper_id}\ntitle: Broken\nyear: 2024\n", encoding="utf-8"
    )
    (paper / "summary.md").write_text("broken\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# rclone availability
# ---------------------------------------------------------------------------


def test_rclone_available_true() -> None:
    assert rclone_available() is True


def test_rclone_unavailable_when_path_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/this/path/does/not/exist")
    assert rclone_available() is False


# ---------------------------------------------------------------------------
# list_remotes / remote_exists
# ---------------------------------------------------------------------------


def test_list_remotes_returns_configured(fake_rclone_env: Path) -> None:
    remotes = list_remotes()
    assert "fake-cloud" in remotes


def test_remote_exists_true(fake_rclone_env: Path) -> None:
    assert remote_exists("fake-cloud") is True


def test_remote_exists_false_for_unknown(fake_rclone_env: Path) -> None:
    assert remote_exists("not-a-real-remote") is False


# ---------------------------------------------------------------------------
# humanize_bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (1024 * 1024 * 1024 * 3, "3.0 GiB"),
    ],
)
def test_humanize_bytes(n: int, expected: str) -> None:
    assert humanize_bytes(n) == expected


# ---------------------------------------------------------------------------
# local_vault_size
# ---------------------------------------------------------------------------


def test_local_vault_size_includes_seeded_files(vault: Path) -> None:
    _seed_paper(vault, "2024_Test_X", body="x" * 100)
    size = local_vault_size(vault)
    # At minimum: lit-config.yaml, TAXONOMY.md, INDEX.json, metadata.yaml,
    # summary.md = 5 files. Some seed sub-dirs are empty so they don't add
    # files. Be conservative and just check the lower bound.
    assert size.count >= 5
    assert size.bytes >= 100  # at least the summary body


def test_local_vault_size_excludes_state_file(vault: Path) -> None:
    """The default-exclude .litman-sync-state.yaml must not inflate size."""
    write_sync_state(vault, SyncState(last_push="2026-05-12T10:00:00+02:00"))
    assert (vault / SYNC_STATE_FILENAME).exists()
    size_with_state = local_vault_size(vault)

    # Compare against a re-count after deleting the state file.
    (vault / SYNC_STATE_FILENAME).unlink()
    size_without = local_vault_size(vault)
    assert size_with_state.count == size_without.count
    assert size_with_state.bytes == size_without.bytes


def test_local_vault_size_excludes_staging_dir(vault: Path) -> None:
    """``.litman-staging/**`` is excluded recursively — size must not change
    when a sizeable file lands inside the staging tree."""
    baseline = local_vault_size(vault)
    staging = vault / ".litman-staging" / "op-abc"
    staging.mkdir(parents=True)
    (staging / "garbage").write_text("x" * 999, encoding="utf-8")
    after = local_vault_size(vault)
    assert after.count == baseline.count
    assert after.bytes == baseline.bytes


def test_default_excludes_contains_views() -> None:
    # Review F33: ADR-003 mandates views/** in the hard exclude set (it is a
    # derived projection, rebuilt by `lit refresh-views`).
    assert "views/**" in DEFAULT_EXCLUDES


def test_local_vault_size_excludes_views(vault: Path) -> None:
    """Review F33/F35: the derived views/ tree (the vault's only symlinks) is
    excluded from the size walk, so the count matches what rclone transfers and
    there is no permanent false "not in sync" delta."""
    baseline = local_vault_size(vault)
    bucket = vault / "views" / "by-topic" / "peptide"
    bucket.mkdir(parents=True)
    (bucket / "garbage").write_text("y" * 999, encoding="utf-8")
    after = local_vault_size(vault)
    assert after.count == baseline.count
    assert after.bytes == baseline.bytes


# ---------------------------------------------------------------------------
# Bug 5 — exclude-matching honours multi-segment globs (codes/*/repo/**)
# ---------------------------------------------------------------------------


def _seed_codes_repo_file(
    vault: Path, repo: str = "foo", body: str = "B" * 999
) -> Path:
    """Drop a file inside ``codes/<repo>/repo/`` — the checkout directory that
    ``sync.exclude_repos`` filters out. Returns the file path."""
    repo_dir = vault / "codes" / repo / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    f = repo_dir / "big.bin"
    f.write_text(body, encoding="utf-8")
    return f


def test_compile_exclude_matcher_codes_repo_glob() -> None:
    """``codes/*/repo/**`` excludes everything under any checkout dir but not
    the repo-meta.yaml / notes.md siblings — the precise match the old
    ``rel.parts[0]`` over-approximation failed (top is always ``codes``)."""
    from litman.core.sync import _compile_exclude_matcher

    excluded = _compile_exclude_matcher(("codes/*/repo/**",))
    assert excluded(Path("codes/foo/repo/main.py"))
    assert excluded(Path("codes/foo/repo/src/deep/x.c"))
    # siblings of the checkout must survive (they sync to the cloud)
    assert not excluded(Path("codes/foo/repo-meta.yaml"))
    assert not excluded(Path("codes/foo/notes.md"))
    # the <name> segment is mandatory — codes/repo/x has none
    assert not excluded(Path("codes/repo/x"))


def test_compile_exclude_matcher_single_segment_wildcard() -> None:
    """``*`` (no trailing ``/**``) stays within one path segment; only ``**``
    crosses separators."""
    from litman.core.sync import _compile_exclude_matcher

    excluded = _compile_exclude_matcher(("codes/*/foo.cache",))
    assert excluded(Path("codes/bar/foo.cache"))
    assert not excluded(Path("codes/bar/baz/foo.cache"))


def test_compile_exclude_matcher_bare_basename() -> None:
    """A pattern without ``/`` matches a file of that name at any depth."""
    from litman.core.sync import _compile_exclude_matcher

    excluded = _compile_exclude_matcher((".litman-sync-state.yaml",))
    assert excluded(Path(".litman-sync-state.yaml"))
    assert excluded(Path("papers/p1/.litman-sync-state.yaml"))
    assert not excluded(Path("papers/p1/metadata.yaml"))


def test_local_vault_size_counts_codes_repo_without_glob(vault: Path) -> None:
    """Guard: with only DEFAULT_EXCLUDES the checkout file IS counted. This is
    exactly why a `lit sync status` that drops the codes glob reports a false
    out-of-sync delta against an exclude_repos push."""
    baseline = local_vault_size(vault)
    _seed_codes_repo_file(vault)
    after = local_vault_size(vault)
    assert after.count == baseline.count + 1


def test_local_vault_size_excludes_codes_repo_with_glob(vault: Path) -> None:
    """Bug 5 (layer 2): ``codes/*/repo/**`` must actually drop the checkout
    file from the walk so the count matches what rclone transfers."""
    baseline = local_vault_size(vault)
    _seed_codes_repo_file(vault, body="B" * 4096)
    glob = codes_ignore_patterns_to_rclone(("repo/",))
    after = local_vault_size(vault, excludes=(*DEFAULT_EXCLUDES, *glob))
    assert after.count == baseline.count
    assert after.bytes == baseline.bytes


def test_largest_files_excludes_codes_repo_with_glob(vault: Path) -> None:
    """The size preview must neither list nor total the excluded checkout
    file when ``exclude_repos`` is in effect."""
    _seed_paper(vault, "p1", body="small\n")
    big = _seed_codes_repo_file(vault, body="B" * 5000)
    rel_big = big.relative_to(vault)
    glob = codes_ignore_patterns_to_rclone(("repo/",))
    with_glob = largest_files(vault, n=5, excludes=(*DEFAULT_EXCLUDES, *glob))
    assert all(rel != rel_big for rel, _ in with_glob)
    # without the glob the same big file dominates the top-5
    without = largest_files(vault, n=5)
    assert any(rel == rel_big for rel, _ in without)


# ---------------------------------------------------------------------------
# SyncState round-trip
# ---------------------------------------------------------------------------


def test_sync_state_round_trip(vault: Path) -> None:
    state = SyncState(
        last_push="2026-05-12T10:00:00+02:00",
        last_pull="2026-05-11T09:00:00+02:00",
    )
    write_sync_state(vault, state)
    loaded = read_sync_state(vault)
    assert loaded == state


def test_sync_state_missing_returns_empty(vault: Path) -> None:
    state = read_sync_state(vault)
    assert state == SyncState(last_push=None, last_pull=None)


def test_stamp_push_updates_only_push(vault: Path) -> None:
    write_sync_state(vault, SyncState(last_pull="2026-05-01T00:00:00+02:00"))
    stamp_push(vault)
    after = read_sync_state(vault)
    assert after.last_push is not None
    assert after.last_pull == "2026-05-01T00:00:00+02:00"


def test_stamp_pull_updates_only_pull(vault: Path) -> None:
    write_sync_state(vault, SyncState(last_push="2026-05-01T00:00:00+02:00"))
    stamp_pull(vault)
    after = read_sync_state(vault)
    assert after.last_pull is not None
    assert after.last_push == "2026-05-01T00:00:00+02:00"


def test_format_iso() -> None:
    assert format_iso(None) == "(never)"
    assert format_iso("2026-05-12T10:00:00+02:00") == "2026-05-12T10:00:00+02:00"


# ---------------------------------------------------------------------------
# build_exclude_args
# ---------------------------------------------------------------------------


def test_build_exclude_args_default_only() -> None:
    args = build_exclude_args()
    # One --exclude PATTERN pair per default.
    assert len(args) == 2 * len(DEFAULT_EXCLUDES)
    assert "--exclude" in args
    for pat in DEFAULT_EXCLUDES:
        assert pat in args


def test_build_exclude_args_extra_appends() -> None:
    args = build_exclude_args(("codes/*/repo/**",))
    assert "codes/*/repo/**" in args
    for pat in DEFAULT_EXCLUDES:
        assert pat in args


# ---------------------------------------------------------------------------
# SyncConfig schema + target_url
# ---------------------------------------------------------------------------


def test_sync_config_target_url_with_path() -> None:
    cfg = SyncConfig(remote="my-gdrive", path="litman-vault/")
    assert cfg.target_url() == "my-gdrive:litman-vault/"


def test_sync_config_target_url_empty_path() -> None:
    cfg = SyncConfig(remote="my-gdrive", path="")
    assert cfg.target_url() == "my-gdrive:"


def test_sync_config_remote_required() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SyncConfig(remote="", path="x")


def test_lit_config_sync_defaults_to_none(vault: Path) -> None:
    cfg = load_config(vault)
    assert cfg.sync is None


def test_lit_config_loads_sync_block(vault: Path) -> None:
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(remote="some-remote", path="litman-vault/"),
    )
    cfg = load_config(vault)
    assert cfg.sync is not None
    assert cfg.sync.remote == "some-remote"
    assert cfg.sync.path == "litman-vault/"
    assert cfg.sync.exclude_repos is False


# ---------------------------------------------------------------------------
# write_sync_to_config — yaml round-trip preserving other fields
# ---------------------------------------------------------------------------


def test_write_sync_to_config_preserves_other_fields(vault: Path) -> None:
    config_path = vault / "lit-config.yaml"
    original = config_path.read_text(encoding="utf-8")
    assert "default_pdf_viewer" in original  # baseline

    write_sync_to_config(
        config_path,
        SetupPayload(remote="my-gdrive", path="litman-vault/"),
    )

    # Other fields still load + still resolve to their original values.
    cfg = load_config(vault)
    assert cfg.library_name == "literature_vault"
    assert cfg.default_pdf_viewer is None
    assert cfg.sync is not None
    assert cfg.sync.remote == "my-gdrive"


def test_write_sync_to_config_overwrites_existing_sync(vault: Path) -> None:
    config_path = vault / "lit-config.yaml"
    write_sync_to_config(
        config_path,
        SetupPayload(remote="first", path="p1/"),
    )
    write_sync_to_config(
        config_path,
        SetupPayload(remote="second", path="p2/", exclude_repos=True),
    )
    cfg = load_config(vault)
    assert cfg.sync is not None
    assert cfg.sync.remote == "second"
    assert cfg.sync.path == "p2/"
    assert cfg.sync.exclude_repos is True


def test_write_sync_to_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="No lit-config.yaml"):
        write_sync_to_config(
            tmp_path / "nope.yaml",
            SetupPayload(remote="x"),
        )


# ---------------------------------------------------------------------------
# push / pull — end-to-end against the fake-cloud local backend
# ---------------------------------------------------------------------------


def test_push_uploads_seeded_paper(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "2024_Test_X")

    push(vault, target)
    # The remote ("fake-cloud" = local backend) is just the storage dir.
    expected = fake_rclone_env / "papers" / "2024_Test_X" / "summary.md"
    assert expected.is_file()
    assert expected.read_text(encoding="utf-8") == "content\n"


def test_push_excludes_staging_dir(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    vault, target = configured_vault
    staging = vault / ".litman-staging" / "op-x"
    staging.mkdir(parents=True)
    (staging / "scratch").write_text("private\n", encoding="utf-8")

    push(vault, target)
    assert not (fake_rclone_env / ".litman-staging").exists()


def test_push_excludes_sync_state_file(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    vault, target = configured_vault
    write_sync_state(vault, SyncState(last_push="prior"))
    push(vault, target)
    assert not (fake_rclone_env / SYNC_STATE_FILENAME).exists()


def test_push_stamps_last_push(
    configured_vault: tuple[Path, str],
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    assert read_sync_state(vault).last_push is None
    push(vault, target)
    assert read_sync_state(vault).last_push is not None


def test_push_dry_run_does_not_transfer(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    push(vault, target, dry_run=True)
    # No files should have been transferred.
    assert not (fake_rclone_env / "papers" / "p1" / "summary.md").exists()
    # State should NOT have been stamped (we previewed only).
    assert read_sync_state(vault).last_push is None


def test_push_deletes_remote_file_removed_locally(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """`rclone sync` is one-way mirror: remote files absent locally vanish."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    push(vault, target)
    assert (fake_rclone_env / "papers" / "p1" / "summary.md").is_file()

    # Remove the paper locally and push again.
    shutil.rmtree(vault / "papers" / "p1")
    push(vault, target)
    assert not (fake_rclone_env / "papers" / "p1").exists()


def test_pull_restores_vault_from_remote(
    configured_vault: tuple[Path, str], fake_rclone_env: Path, tmp_path: Path
) -> None:
    """Push then wipe local + recreate empty vault + pull → state restored."""
    vault, target = configured_vault
    _seed_paper(vault, "p1", body="restored\n")
    push(vault, target)

    # Simulate cross-machine: drop the paper locally and pull it back.
    shutil.rmtree(vault / "papers" / "p1")
    assert not (vault / "papers" / "p1").exists()

    pull(vault, target)
    assert (vault / "papers" / "p1" / "summary.md").is_file()
    assert (
        vault / "papers" / "p1" / "summary.md"
    ).read_text(encoding="utf-8") == "restored\n"


def test_pull_stamps_last_pull(
    configured_vault: tuple[Path, str],
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    push(vault, target)
    assert read_sync_state(vault).last_pull is None
    pull(vault, target)
    assert read_sync_state(vault).last_pull is not None


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_pull_relocks_truth_files(
    configured_vault: tuple[Path, str],
) -> None:
    """rclone drops Unix perms, so pull re-locks TRUTH files locally (M32)."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    push(vault, target)

    # Simulate the post-pull state: a TRUTH file arrives writable.
    meta = vault / "papers" / "p1" / "metadata.yaml"
    pdf = vault / "papers" / "p1" / "paper.pdf"
    tax = vault / "TAXONOMY.md"
    os.chmod(meta, 0o644)
    os.chmod(pdf, 0o644)
    os.chmod(tax, 0o644)
    assert os.access(meta, os.W_OK)

    pull(vault, target)

    assert not os.access(meta, os.W_OK)
    assert not os.access(pdf, os.W_OK)
    assert not os.access(tax, os.W_OK)


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_pull_dry_run_does_not_relock(
    configured_vault: tuple[Path, str],
) -> None:
    """A dry-run pull must not touch local file modes."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    push(vault, target)
    meta = vault / "papers" / "p1" / "metadata.yaml"
    os.chmod(meta, 0o644)

    pull(vault, target, dry_run=True)

    assert os.access(meta, os.W_OK)


# ---------------------------------------------------------------------------
# remote_size / compute_status
# ---------------------------------------------------------------------------


def test_remote_size_empty_target_returns_zero(
    fake_rclone_env: Path,
) -> None:
    # Empty storage dir — count and bytes both 0.
    sz = remote_size(f"fake-cloud:{fake_rclone_env}")
    assert sz == Size(count=0, bytes=0)


def test_remote_size_after_push(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1", body="hello\n")
    push(vault, target)
    sz = remote_size(target)
    assert sz.count >= 1
    assert sz.bytes >= len("hello\n")


def test_compute_status_in_sync_after_push(
    configured_vault: tuple[Path, str],
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    push(vault, target)
    report = compute_status(vault, target)
    assert report.local.count == report.remote.count
    assert report.local.bytes == report.remote.bytes
    assert report.file_delta == 0
    assert report.bytes_delta == 0
    assert report.state.last_push is not None


def test_compute_status_before_push(
    configured_vault: tuple[Path, str],
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    report = compute_status(vault, target)
    assert report.local.count > 0
    assert report.remote.count == 0
    assert report.file_delta == report.local.count
    assert report.state.last_push is None


def test_compute_status_in_sync_with_exclude_repos(
    configured_vault: tuple[Path, str],
) -> None:
    """Bug 5: a push that excludes codes/*/repo/ and a status that applies the
    same exclude must agree — no permanent false out-of-sync delta. Without
    the exclude the local walk would count the checkout file the remote lacks."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    _seed_codes_repo_file(vault, body="B" * 4096)
    extra = codes_ignore_patterns_to_rclone(("repo/",))
    push(vault, target, extra_excludes=extra)
    report = compute_status(vault, target, extra_excludes=extra)
    assert report.file_delta == 0
    assert report.bytes_delta == 0
    # cross-check: counting WITHOUT the exclude would (wrongly) show a delta
    naive = compute_status(vault, target)
    assert naive.file_delta != 0


# ---------------------------------------------------------------------------
# CLI: lit sync setup
# ---------------------------------------------------------------------------


def test_cli_sync_setup_with_remote_flag(
    vault: Path, fake_rclone_env: Path
) -> None:
    """--remote skips the interactive rclone config and writes the config."""
    target_path = str(fake_rclone_env)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "sync", "setup",
            "--remote", "fake-cloud",
            "--path", target_path,
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Sync configured" in result.output

    cfg = load_config(vault)
    assert cfg.sync is not None
    assert cfg.sync.remote == "fake-cloud"
    assert cfg.sync.path == target_path


def test_cli_sync_setup_rejects_unknown_remote(
    vault: Path, fake_rclone_env: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "sync", "setup",
            "--remote", "nonexistent",
            "--path", "p/",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, SyncError)
    assert "not registered" in str(result.exception)


def test_cli_sync_setup_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "setup", "--help"])
    assert result.exit_code == 0
    assert "--remote" in result.output
    assert "--path" in result.output


# ---------------------------------------------------------------------------
# CLI: lit sync push / pull / status
# ---------------------------------------------------------------------------


def test_cli_sync_push_uploads(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    runner = CliRunner()
    # First push triggers the size-preview prompt; --yes skips it.
    result = runner.invoke(
        cli, ["sync", "push", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Push complete" in result.output
    assert (fake_rclone_env / "papers" / "p1" / "summary.md").is_file()


def test_cli_sync_pull_restores(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1", body="payload\n")
    push(vault, target)
    shutil.rmtree(vault / "papers" / "p1")

    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "pull", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "Pull complete" in result.output
    assert (
        vault / "papers" / "p1" / "summary.md"
    ).read_text(encoding="utf-8") == "payload\n"


def test_cli_sync_status_shows_metrics(
    configured_vault: tuple[Path, str],
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    push(vault, target)
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "status", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "Target" in result.output
    assert "Last push" in result.output
    assert "Local" in result.output
    assert "Remote" in result.output
    assert "in sync" in result.output


def test_cli_sync_status_reports_delta_when_unpushed(
    configured_vault: tuple[Path, str],
) -> None:
    vault, _ = configured_vault
    _seed_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "status", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "local − remote" in result.output


def test_cli_sync_status_exclude_repos_no_false_delta(
    vault: Path, fake_rclone_env: Path
) -> None:
    """Bug 5 (layer 1): with ``sync.exclude_repos: true`` the status command
    must reuse the push exclude set. Previously it dropped it and reported a
    permanent ``local − remote`` delta right after a clean exclude_repos push."""
    target_path = str(fake_rclone_env)
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(remote="fake-cloud", path=target_path, exclude_repos=True),
    )
    target = f"fake-cloud:{target_path}"
    _seed_paper(vault, "p1")
    _seed_codes_repo_file(vault, body="B" * 4096)
    # Mirror what `lit sync push` does under exclude_repos (core push bypasses
    # the integrity gate, which is irrelevant to the status-side fix).
    push(vault, target, extra_excludes=codes_ignore_patterns_to_rclone(("repo/",)))
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "status", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "in sync" in result.output
    assert "local − remote" not in result.output


def test_cli_sync_push_without_setup_errors(
    vault: Path, fake_rclone_env: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "push", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, SyncError)
    assert "not configured" in str(result.exception)


def test_cli_sync_status_without_setup_errors(
    vault: Path, fake_rclone_env: Path
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "status", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, SyncError)


def test_cli_sync_push_with_unknown_remote_errors(
    vault: Path, fake_rclone_env: Path
) -> None:
    """Sync configured but the remote was deleted from rclone afterwards."""
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(remote="ghost-remote", path="p/"),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "push", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, SyncError)
    assert "no longer registered" in str(result.exception)


def test_cli_sync_group_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "--help"])
    assert result.exit_code == 0
    assert "setup" in result.output
    assert "push" in result.output
    assert "pull" in result.output
    assert "status" in result.output


# ---------------------------------------------------------------------------
# Behavior when rclone is absent on PATH (simulated via PATH override)
# ---------------------------------------------------------------------------


def test_sync_setup_without_rclone_errors(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/this/path/does/not/exist")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "sync", "setup",
            "--remote", "anything",
            "--path", "p/",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, SyncError)
    assert "rclone" in str(result.exception).lower()


# ===========================================================================
# M6.2 — size preview, --exclude-repos, --dry-run on CLI
# ===========================================================================


# ---------------------------------------------------------------------------
# largest_files
# ---------------------------------------------------------------------------


def test_largest_files_returns_top_n_sorted(vault: Path) -> None:
    """Three files of distinct sizes -> top-2 yields the two biggest."""
    # Add to whatever seed files already exist.
    (vault / "papers").mkdir(exist_ok=True)
    (vault / "papers" / "big").mkdir(exist_ok=True)
    (vault / "papers" / "big" / "huge.bin").write_bytes(b"X" * 10_000)
    (vault / "papers" / "big" / "medium.bin").write_bytes(b"Y" * 5_000)
    (vault / "papers" / "big" / "small.bin").write_bytes(b"Z" * 100)

    top2 = largest_files(vault, n=2)
    assert len(top2) == 2
    assert top2[0][1] == 10_000  # huge.bin first
    assert top2[1][1] == 5_000   # medium.bin second
    # Names propagate via relative path.
    assert top2[0][0].name == "huge.bin"


def test_largest_files_respects_excludes(vault: Path) -> None:
    """A massive file under .litman-staging/ should NOT appear in the top list."""
    staging = vault / ".litman-staging" / "op"
    staging.mkdir(parents=True)
    (staging / "should-not-appear").write_bytes(b"X" * 100_000)

    top = largest_files(vault, n=10)
    rels = [str(p) for p, _ in top]
    assert not any(".litman-staging" in r for r in rels)


def test_largest_files_truncates_to_n(vault: Path) -> None:
    """Vault with many files -> result list is at most n entries."""
    (vault / "scratch").mkdir(exist_ok=True)
    for i in range(20):
        (vault / "scratch" / f"f{i}").write_bytes(b"x" * (100 + i))
    top = largest_files(vault, n=3)
    assert len(top) == 3


def test_largest_files_empty_vault_returns_empty_list(tmp_path: Path) -> None:
    """No files at all -> empty result, no error."""
    empty = tmp_path / "void"
    empty.mkdir()
    assert largest_files(empty, n=5) == []


# ---------------------------------------------------------------------------
# codes_ignore_patterns_to_rclone
# ---------------------------------------------------------------------------


def test_codes_ignore_patterns_to_rclone_dir() -> None:
    """``repo/`` (trailing slash) -> ``codes/*/repo/**`` (rclone glob)."""
    from litman.core.sync import codes_ignore_patterns_to_rclone

    out = codes_ignore_patterns_to_rclone(["repo/"])
    assert out == ("codes/*/repo/**",)


def test_codes_ignore_patterns_to_rclone_file() -> None:
    """No trailing slash -> file-pattern form (no `/**` suffix)."""
    from litman.core.sync import codes_ignore_patterns_to_rclone

    out = codes_ignore_patterns_to_rclone(["foo.cache"])
    assert out == ("codes/*/foo.cache",)


def test_codes_ignore_patterns_to_rclone_drops_empty() -> None:
    from litman.core.sync import codes_ignore_patterns_to_rclone

    out = codes_ignore_patterns_to_rclone(["repo/", "  ", ""])
    assert out == ("codes/*/repo/**",)


def test_codes_ignore_patterns_to_rclone_multiple() -> None:
    from litman.core.sync import codes_ignore_patterns_to_rclone

    out = codes_ignore_patterns_to_rclone(["repo/", "node_modules/", "*.tmp"])
    assert out == (
        "codes/*/repo/**",
        "codes/*/node_modules/**",
        "codes/*/*.tmp",
    )


# ---------------------------------------------------------------------------
# push with extra_excludes through the core (codes/*/repo/ filter)
# ---------------------------------------------------------------------------


def _seed_codes(vault: Path, repo_name: str, payload: str = "code\n") -> None:
    """Drop a minimal codes/<name>/repo/<file> + repo-meta.yaml on disk."""
    repo_root = vault / "codes" / repo_name
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "repo-meta.yaml").write_text(
        f"name: {repo_name}\nupstream: file:///tmp/u\npapers: []\n",
        encoding="utf-8",
    )
    (repo_root / "notes.md").write_text("notes\n", encoding="utf-8")
    (repo_root / "repo").mkdir(exist_ok=True)
    (repo_root / "repo" / "code.py").write_text(payload, encoding="utf-8")


def test_push_excludes_repo_dirs_when_extra_excludes_set(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """``codes/*/repo/**`` exclude -> repo/ never lands on the remote, but
    repo-meta.yaml + notes.md still travel."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    _seed_codes(vault, "MyRepo")

    push(vault, target, extra_excludes=("codes/*/repo/**",))

    # repo-meta.yaml + notes.md transferred...
    assert (fake_rclone_env / "codes" / "MyRepo" / "repo-meta.yaml").is_file()
    assert (fake_rclone_env / "codes" / "MyRepo" / "notes.md").is_file()
    # ...but repo/ itself did not.
    assert not (fake_rclone_env / "codes" / "MyRepo" / "repo").exists()


def test_push_includes_repo_dirs_by_default(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """Without extra_excludes, codes/*/repo/ IS uploaded."""
    vault, target = configured_vault
    _seed_codes(vault, "MyRepo")

    push(vault, target)  # no extra_excludes

    assert (fake_rclone_env / "codes" / "MyRepo" / "repo" / "code.py").is_file()


# ---------------------------------------------------------------------------
# CLI: lit sync push --yes / --dry-run / --exclude-repos
# ---------------------------------------------------------------------------


def test_cli_sync_push_first_time_prompts(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """Without --yes, the first push surfaces the size-preview prompt; 'n'
    aborts and nothing is transferred."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "push", "--library", str(vault)], input="n\n"
    )
    assert result.exit_code != 0  # click.confirm(abort=True) on 'n'
    assert "First-push size preview" in result.output
    assert "Total:" in result.output
    # Nothing was uploaded.
    assert not (fake_rclone_env / "papers" / "p1" / "summary.md").exists()
    # No stamp was recorded either.
    assert read_sync_state(vault).last_push is None


def test_cli_sync_push_yes_skips_prompt(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """--yes skips the confirmation but still prints the preview."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "push", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "First-push size preview" in result.output
    assert "Push complete" in result.output
    assert (fake_rclone_env / "papers" / "p1" / "summary.md").is_file()


def test_cli_sync_push_second_push_no_prompt(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """Second push (last-push already stamped) skips the preview entirely."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    # First push (with --yes) to stamp the state file.
    runner = CliRunner()
    first = runner.invoke(
        cli, ["sync", "push", "--yes", "--library", str(vault)]
    )
    assert first.exit_code == 0

    # Second push without --yes should NOT prompt.
    _seed_paper(vault, "p2")
    second = runner.invoke(cli, ["sync", "push", "--library", str(vault)])
    assert second.exit_code == 0, second.output
    assert "First-push size preview" not in second.output
    assert (fake_rclone_env / "papers" / "p2" / "summary.md").is_file()


def test_cli_sync_push_dry_run_skips_preview_and_transfer(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """--dry-run on first push: no preview prompt, no transfer, no stamp."""
    vault, target = configured_vault
    _seed_paper(vault, "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "push", "--dry-run", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "First-push size preview" not in result.output
    assert "Dry-run complete" in result.output
    assert not (fake_rclone_env / "papers" / "p1").exists()
    assert read_sync_state(vault).last_push is None


def test_cli_sync_push_integrity_gate_blocks_corrupt_vault(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """A schema-broken paper aborts the push (exit 1) before any transfer."""
    vault, target = configured_vault
    _seed_paper(vault, "ok")
    _seed_broken_paper(vault, "bad")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "push", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 1, result.output
    assert "integrity gate" in result.output.lower()
    # Nothing transferred; no stamp.
    assert not (fake_rclone_env / "papers" / "ok" / "summary.md").exists()
    assert read_sync_state(vault).last_push is None


def test_cli_sync_push_yes_does_not_bypass_integrity_gate(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """--yes only skips the size confirm; it must NOT bypass the gate.

    This is the core of C-ops1: an unattended cron push (`--yes`) must still
    refuse to mirror a broken vault over the cloud backup.
    """
    vault, target = configured_vault
    _seed_broken_paper(vault, "bad")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "push", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 1, result.output
    assert not (fake_rclone_env / "papers" / "bad").exists()


def test_cli_sync_push_force_bypasses_integrity_gate(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """--force mirrors the vault even with integrity errors (escape hatch);
    --yes here only skips the first-push size confirm."""
    vault, target = configured_vault
    _seed_broken_paper(vault, "bad")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "push", "--force", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "bypassed" in result.output.lower()
    assert (fake_rclone_env / "papers" / "bad" / "metadata.yaml").is_file()
    assert read_sync_state(vault).last_push is not None


def test_cli_sync_push_dry_run_reports_gate_but_does_not_abort(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """--dry-run surfaces the gate errors but never aborts, and (being a
    dry-run) transfers nothing."""
    vault, target = configured_vault
    _seed_broken_paper(vault, "bad")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "push", "--dry-run", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "integrity gate (dry-run)" in result.output.lower()
    assert not (fake_rclone_env / "papers" / "bad").exists()


def test_cli_sync_push_exclude_repos_flag(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """--exclude-repos -> codes/*/repo/ excluded from the transfer."""
    vault, target = configured_vault
    _seed_codes(vault, "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "sync", "push",
            "--exclude-repos",
            "--yes",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "codes/*/repo/ excluded" in result.output
    # repo-meta.yaml made it, repo/ did not.
    assert (fake_rclone_env / "codes" / "MyRepo" / "repo-meta.yaml").is_file()
    assert not (fake_rclone_env / "codes" / "MyRepo" / "repo").exists()


def test_cli_sync_push_include_repos_overrides_config_default(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """sync.exclude_repos: true in config but --include-repos on CLI wins."""
    vault, target = configured_vault
    # Flip the config-file default to exclude_repos=True.
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(
            remote="fake-cloud",
            path=str(fake_rclone_env),
            exclude_repos=True,
        ),
    )
    _seed_codes(vault, "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "sync", "push",
            "--include-repos",
            "--yes",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "codes/*/repo/ excluded" not in result.output
    # repo/ should have been uploaded since CLI flag wins.
    assert (
        fake_rclone_env / "codes" / "MyRepo" / "repo" / "code.py"
    ).is_file()


def test_cli_sync_push_config_default_exclude_repos(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """sync.exclude_repos: true in config, no CLI flag -> excluded."""
    vault, target = configured_vault
    write_sync_to_config(
        vault / "lit-config.yaml",
        SetupPayload(
            remote="fake-cloud",
            path=str(fake_rclone_env),
            exclude_repos=True,
        ),
    )
    _seed_codes(vault, "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "push", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "codes/*/repo/ excluded" in result.output
    assert not (fake_rclone_env / "codes" / "MyRepo" / "repo").exists()


# ---------------------------------------------------------------------------
# CLI: lit sync pull --dry-run / --exclude-repos
# ---------------------------------------------------------------------------


def test_cli_sync_pull_dry_run_does_not_modify(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    vault, target = configured_vault
    _seed_paper(vault, "p1", body="local\n")
    push(vault, target)
    # Remove the local copy then pull --dry-run.
    shutil.rmtree(vault / "papers" / "p1")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["sync", "pull", "--dry-run", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Dry-run complete" in result.output
    # No file was restored.
    assert not (vault / "papers" / "p1").exists()
    # No last-pull stamp either.
    assert read_sync_state(vault).last_pull is None


def test_cli_sync_pull_exclude_repos_propagates(
    configured_vault: tuple[Path, str], fake_rclone_env: Path
) -> None:
    """Pull with --exclude-repos: remote-side codes/*/repo/ not materialised
    locally even if it exists on the remote."""
    vault, target = configured_vault
    _seed_codes(vault, "MyRepo")
    # First push everything (incl. repo/) so the remote holds it.
    push(vault, target)
    assert (fake_rclone_env / "codes" / "MyRepo" / "repo" / "code.py").is_file()
    # Wipe local codes/, then pull --exclude-repos.
    shutil.rmtree(vault / "codes" / "MyRepo")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "sync", "pull",
            "--exclude-repos",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    # Metadata files came back; repo/ did NOT.
    assert (vault / "codes" / "MyRepo" / "repo-meta.yaml").is_file()
    assert not (vault / "codes" / "MyRepo" / "repo").exists()


# ---------------------------------------------------------------------------
# Help text mentions the new flags
# ---------------------------------------------------------------------------


def test_cli_sync_push_help_lists_new_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "push", "--help"])
    assert result.exit_code == 0
    assert "--exclude-repos" in result.output
    assert "--include-repos" in result.output
    assert "--dry-run" in result.output
    assert "--yes" in result.output


def test_cli_sync_pull_help_lists_new_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "pull", "--help"])
    assert result.exit_code == 0
    assert "--exclude-repos" in result.output
    assert "--dry-run" in result.output
