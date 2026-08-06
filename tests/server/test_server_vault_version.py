"""Tests for GET /api/vault-version (the change token the GUI polls).

The contract has exactly two halves, and both are load-bearing:

* it MOVES for every kind of on-disk change the resync sweep would surface —
  otherwise the GUI silently misses that change forever (it only sweeps when
  the token moved);
* it STAYS PUT when nothing changed — otherwise every poll triggers a
  7-request sweep and the whole point (a cheap guard) is gone.

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.config import CONFIG_FILENAME
from litman.server import create_app


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _token(client: TestClient) -> str:
    resp = client.get("/api/vault-version")
    assert resp.status_code == 200
    version = resp.json()["version"]
    assert isinstance(version, str) and version
    return version


def _bump_mtime(path: Path, *, by: float = 10.0) -> None:
    """Push a path's mtime forward without sleeping.

    Every "did the token move" test needs a change the filesystem's timestamp
    granularity cannot swallow; a rewrite inside the same clock tick may leave
    st_mtime_ns untouched, which would make these tests flaky rather than wrong.
    """
    st = os.stat(path)
    os.utime(path, (st.st_atime + by, st.st_mtime + by))


# ---------------------------------------------------------------------------
# Stability: no change ⇒ no sweep
# ---------------------------------------------------------------------------


def test_token_is_stable_across_repeated_calls(
    vault_with_paper: tuple[Path, str],
) -> None:
    """AC-3's server half: an untouched vault must answer the same token every
    time, or the GUI's idle poll would sweep on every tick."""
    vault, _ = vault_with_paper
    client = _client(vault)
    first = _token(client)
    assert all(_token(client) == first for _ in range(5))


def test_token_is_stable_across_app_instances(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Derived purely from on-disk state — no per-process salt. A server
    restart under a still-open page must not read as "the vault changed"."""
    vault, _ = vault_with_paper
    assert _token(_client(vault)) == _token(_client(vault))


def test_reading_the_token_performs_no_writes(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Pure read (invariant #16). This one is polled on a timer, so a write
    here would be a write every few seconds for as long as the GUI is open."""
    vault, paper_id = vault_with_paper
    paper_dir = vault / "papers" / paper_id
    meta = paper_dir / "metadata.yaml"
    index = vault / "INDEX.json"
    meta_before = meta.stat().st_mtime_ns
    index_before = index.stat().st_mtime_ns
    assert not (paper_dir / "notes.md").exists()

    _token(_client(vault))

    assert meta.stat().st_mtime_ns == meta_before
    assert index.stat().st_mtime_ns == index_before
    # Never materializes the absent docs it stats.
    assert not (paper_dir / "notes.md").exists()
    assert not (paper_dir / "discussion.md").exists()
    assert not (vault / ".trash").exists()


# ---------------------------------------------------------------------------
# Sensitivity: every source of change must move it
# ---------------------------------------------------------------------------


def test_index_change_moves_the_token(vault_with_paper: tuple[Path, str]) -> None:
    """Stands in for every structured write — `lit modify`, `promote`, `add`,
    tag edits — all of which re-derive INDEX.json (AC-1 / AC-2)."""
    vault, _ = vault_with_paper
    client = _client(vault)
    before = _token(client)
    _bump_mtime(vault / "INDEX.json")
    assert _token(client) != before


def test_taxonomy_change_moves_the_token(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    client = _client(vault)
    before = _token(client)
    _bump_mtime(vault / "TAXONOMY.md")
    assert _token(client) != before


def test_config_change_moves_the_token(vault_with_paper: tuple[Path, str]) -> None:
    """lit-config.yaml holds the project map GET /api/projects serves, so a
    project registered from the terminal has to move the token."""
    vault, _ = vault_with_paper
    client = _client(vault)
    before = _token(client)
    _bump_mtime(vault / CONFIG_FILENAME)
    assert _token(client) != before


def test_notes_written_in_place_move_the_token(
    vault_with_paper: tuple[Path, str],
) -> None:
    """The motivating case: an agent overwrites notes.md directly. Nothing in
    INDEX.json moves for this, which is exactly why the token walks the papers."""
    vault, paper_id = vault_with_paper
    client = _client(vault)
    before = _token(client)
    (vault / "papers" / paper_id / "notes.md").write_text("draft\n", encoding="utf-8")
    after = _token(client)
    assert after != before

    # ...and a *second* write to the same file moves it again (a token that only
    # noticed creation would go deaf after the first agent edit).
    notes = vault / "papers" / paper_id / "notes.md"
    notes.write_text("draft, revised\n", encoding="utf-8")
    _bump_mtime(notes)
    assert _token(client) != after


def test_discussion_written_in_place_moves_the_token(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    client = _client(vault)
    before = _token(client)
    (vault / "papers" / paper_id / "discussion.md").write_text(
        "## 2026-08-06\n", encoding="utf-8"
    )
    assert _token(client) != before


def test_new_paper_directory_moves_the_token(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Independent of INDEX.json: even a paper folder dropped in by hand (before
    any refresh-views) is visible to the walk."""
    vault, _ = vault_with_paper
    client = _client(vault)
    before = _token(client)
    (vault / "papers" / "2025_New_Paper").mkdir()
    (vault / "papers" / "2025_New_Paper" / "notes.md").write_text(
        "x\n", encoding="utf-8"
    )
    assert _token(client) != before


def test_trash_activity_moves_the_token(vault_with_paper: tuple[Path, str]) -> None:
    """The .trash directory's own mtime flips when an entry lands in it, so a
    `lit rm` in the terminal reaches the GUI's trash view."""
    vault, _ = vault_with_paper
    client = _client(vault)
    before = _token(client)
    trash = vault / ".trash"
    trash.mkdir()
    assert _token(client) != before

    # An entry moving in flips it again (directory mtime, no content read).
    after_create = _token(client)
    (trash / "2024_Foo_Bar__20260806").mkdir()
    assert _token(client) != after_create


def test_registry_change_moves_the_token(
    vault_with_paper: tuple[Path, str],
) -> None:
    """The vault registry lives outside the vault and drives the switcher the
    resync sweep re-pulls (`fetchVaults`)."""
    from litman.core.vault_registry import registry_path

    vault, _ = vault_with_paper
    client = _client(vault)
    before = _token(client)
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _bump_mtime(path)
    else:
        path.write_text("vaults: []\n", encoding="utf-8")
    assert _token(client) != before


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_missing_papers_dir_still_answers(tmp_path: Path) -> None:
    """A vault whose papers/ is absent answers a token instead of a 500 — the
    poll must never be the thing that breaks a half-built library."""
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)
    papers = vault / "papers"
    if papers.exists():
        papers.rmdir()
    assert _token(_client(vault))


def test_no_vault_is_refused_not_answered(tmp_path: Path) -> None:
    """Vault-dependent, so the no-vault guard owns it: the welcome page gets a
    409 (the frontend gates the poll on a served vault and never asks). It must
    NOT be whitelisted — a token for "no vault" would be meaningless."""
    client = TestClient(create_app(None))
    assert client.get("/api/vault-version").status_code == 409
