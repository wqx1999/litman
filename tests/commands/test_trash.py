"""Tests for the .trash/ recoverable-delete bin and ``lit trash`` group.

Covers default rm-into-trash, --purge skip, sidecar metadata, list,
restore (simple + collision + ambiguity), empty (with prompt + --yes),
and trash-dir exclusion from active views.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import trash as trash_mod
from litman.core.library import create_vault
from litman.core.trash import (
    TRASH_DIRNAME,
    TRASH_MAX_ENTRIES,
    enforce_cap,
    list_trash,
    move_to_trash,
)
from litman.exceptions import TrashError

_yaml = YAML(typ="safe")


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": fields.get("title", f"Title of {paper_id}"),
        "authors": ["Doe, Jane"],
        "year": 2024,
        "journal": "Test J.",
        "doi": f"10.0/{paper_id}",
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": [],
        "topics": fields.get("topics", []),
        "methods": [],
        "data": [],
        "type": "research",
        "status": "inbox",
        "priority": "B",
        "read-date": None,
        "last-revisited": None,
        "related": fields.get("related", []),
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ===========================================================================
# rm default → trash
# ===========================================================================


def test_rm_default_moves_to_trash(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Trashed 2024_Foo" in result.output
    assert not (vault / "papers" / "2024_Foo").exists()

    trash_root = vault / TRASH_DIRNAME
    assert trash_root.is_dir()
    entries = [e for e in trash_root.iterdir() if e.is_dir()]
    assert len(entries) == 1
    assert entries[0].name.startswith("2024_Foo-")
    # The paper PDF/metadata are inside the trash entry.
    assert (entries[0] / "metadata.yaml").is_file()


def test_rm_writes_sidecar_metadata(vault: Path) -> None:
    _write_paper(vault, "2024_Foo", title="The Foo Paper")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])

    trash_root = vault / TRASH_DIRNAME
    sidecars = list(trash_root.glob("*.meta.yaml"))
    assert len(sidecars) == 1
    data = _yaml.load(sidecars[0].read_text())
    assert data["paper_id"] == "2024_Foo"
    assert data["title"] == "The Foo Paper"
    # M23.1: cascade_was_used no longer written; orphan_repos is the new key.
    assert "cascade_was_used" not in data
    assert data["orphan_repos"] == {}
    assert "deleted_at" in data


def test_rm_purge_skips_trash(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rm", "2024_Foo", "--purge", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Purged 2024_Foo" in result.output
    assert not (vault / "papers" / "2024_Foo").exists()
    # No trash entry created.
    trash_root = vault / TRASH_DIRNAME
    assert not trash_root.exists() or not any(trash_root.iterdir())


def test_rm_legacy_sidecar_tolerated_by_list(vault: Path) -> None:
    """list_trash defaults cascade_was_used to False when the key is absent."""
    _write_paper(vault, "2024_Foo")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])

    entries = list_trash(vault)
    assert len(entries) == 1
    # New writer omits cascade_was_used; reader defaults it to False.
    assert entries[0].cascade_was_used is False
    assert entries[0].orphan_repos == {}


def test_trash_excluded_from_index_and_list(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    _write_paper(vault, "2024_Bar")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])

    payload = json.loads((vault / "INDEX.json").read_text())
    ids = [p["id"] for p in payload["papers"]]
    assert ids == ["2024_Bar"]

    result = runner.invoke(cli, ["list", "--library", str(vault)])
    assert "2024_Foo" not in result.output
    assert "2024_Bar" in result.output


# ===========================================================================
# lit trash list
# ===========================================================================


def test_trash_list_empty_vault(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["trash", "list", "--library", str(vault)])
    assert result.exit_code == 0
    assert "trash is empty" in result.output


def test_trash_list_shows_entries(vault: Path) -> None:
    _write_paper(vault, "2024_Foo", title="The Foo Paper")
    _write_paper(vault, "2024_Bar", title="The Bar Paper")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])
    runner.invoke(cli, ["rm", "2024_Bar", "--yes", "--library", str(vault)])

    result = runner.invoke(cli, ["trash", "list", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "2024_Foo" in result.output
    assert "2024_Bar" in result.output
    assert "Foo Paper" in result.output


# ===========================================================================
# lit trash restore
# ===========================================================================


def test_trash_restore_simple(vault: Path) -> None:
    _write_paper(vault, "2024_Foo", title="Restore me")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])
    assert not (vault / "papers" / "2024_Foo").exists()

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Foo", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Restored 2024_Foo" in result.output
    assert (vault / "papers" / "2024_Foo" / "metadata.yaml").is_file()
    # Trash now empty (no remaining dirs).
    trash_root = vault / TRASH_DIRNAME
    remaining_dirs = [e for e in trash_root.iterdir() if e.is_dir()]
    assert remaining_dirs == []


def test_trash_restore_refreshes_index(vault: Path) -> None:
    _write_paper(vault, "2024_Foo", topics=["alpha"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])
    runner.invoke(cli, ["trash", "restore", "2024_Foo", "--library", str(vault)])

    payload = json.loads((vault / "INDEX.json").read_text())
    ids = [p["id"] for p in payload["papers"]]
    assert "2024_Foo" in ids
    # views rebuilt: by-topic/alpha/2024_Foo symlink is back.
    assert (vault / "views/by-topic/alpha/2024_Foo").is_symlink()


def test_trash_restore_refuses_when_active_collision(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])

    # Re-create an active paper at the same id.
    _write_paper(vault, "2024_Foo", title="Different content")

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Foo", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TrashError)
    assert "already exists" in str(result.exception)


def test_trash_restore_ambiguous_id(vault: Path) -> None:
    """Two trash entries with the same paper_id force the user to disambiguate."""
    runner = CliRunner()
    # First incarnation
    _write_paper(vault, "2024_Foo", title="v1")
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])
    # Sleep to ensure the second trash entry has a different timestamp suffix
    time.sleep(1.1)
    # Second incarnation, then trashed
    _write_paper(vault, "2024_Foo", title="v2")
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Foo", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TrashError)
    assert "Multiple trash entries" in str(result.exception)


def test_trash_restore_by_full_entry_name(vault: Path) -> None:
    runner = CliRunner()
    _write_paper(vault, "2024_Foo", title="v1")
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])
    time.sleep(1.1)
    _write_paper(vault, "2024_Foo", title="v2")
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])

    # Pick the older entry by name.
    entries = list_trash(vault)
    assert len(entries) == 2
    older = entries[-1]  # list_trash sorts newest-first
    result = runner.invoke(
        cli, ["trash", "restore", older.entry_name, "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_Foo" / "metadata.yaml").is_file()
    # Restored paper has the older title.
    restored_meta = _yaml.load(
        (vault / "papers" / "2024_Foo" / "metadata.yaml").read_text()
    )
    assert restored_meta["title"] == "v1"


def test_trash_restore_unknown_id(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["trash", "restore", "9999_Ghost", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TrashError)


# ===========================================================================
# lit trash empty
# ===========================================================================


def test_trash_empty_removes_all(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    _write_paper(vault, "2024_Bar")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])
    runner.invoke(cli, ["rm", "2024_Bar", "--yes", "--library", str(vault)])

    result = runner.invoke(
        cli, ["trash", "empty", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    trash_root = vault / TRASH_DIRNAME
    assert list(trash_root.iterdir()) == []


def test_trash_empty_already_empty(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["trash", "empty", "--yes", "--library", str(vault)]
    )
    assert result.exit_code == 0
    assert "already empty" in result.output


def test_trash_empty_prompt_aborts(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])

    result = runner.invoke(
        cli, ["trash", "empty", "--library", str(vault)], input="n\n"
    )
    assert result.exit_code == 0
    assert "Aborted" in result.output
    # Trash entry still there.
    trash_root = vault / TRASH_DIRNAME
    assert any(p.is_dir() for p in trash_root.iterdir())


# ===========================================================================
# core/trash.py direct API
# ===========================================================================


def test_move_to_trash_missing_paper(vault: Path) -> None:
    with pytest.raises(TrashError):
        move_to_trash(vault, "9999_Ghost")


def test_list_trash_tolerates_missing_sidecar(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])

    # Drop the sidecar.
    sidecar = next((vault / TRASH_DIRNAME).glob("*.meta.yaml"))
    sidecar.unlink()

    entries = list_trash(vault)
    assert len(entries) == 1
    assert entries[0].paper_id == "2024_Foo"
    assert entries[0].deleted_at == "(unknown)"


# ===========================================================================
# CLI smoke
# ===========================================================================


def test_trash_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["trash", "--help"])
    assert result.exit_code == 0
    assert "trash" in result.output.lower()
    for sub in ("list", "restore", "empty"):
        assert sub in result.output


def test_rm_help_mentions_purge() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["rm", "--help"])
    assert result.exit_code == 0
    assert "--purge" in result.output
    assert ".trash" in result.output


# ===========================================================================
# enforce_cap (ring eviction)
# ===========================================================================


def _fabricate_trash_entry(
    vault: Path, paper_id: str, ts: str, *, with_sidecar: bool = True
) -> Path:
    """Build a trash entry directly on disk in the format `list_trash` parses.

    ``ts`` is the compact UTC timestamp portion of the entry name
    (``YYYYMMDDTHHMMSSZ``); newest-first ordering keys off it. Bypasses the
    full `lit add` + `lit rm` path so cap tests stay fast.
    """
    trash_root = vault / TRASH_DIRNAME
    trash_root.mkdir(parents=True, exist_ok=True)
    entry_name = f"{paper_id}-{ts}"
    entry_path = trash_root / entry_name
    entry_path.mkdir()
    (entry_path / "metadata.yaml").write_text(
        f"id: {paper_id}\ntitle: Title of {paper_id}\n", encoding="utf-8"
    )
    if with_sidecar:
        (trash_root / f"{entry_name}.meta.yaml").write_text(
            f"paper_id: {paper_id}\ndeleted_at: '2026-05-01T00:00:00+00:00'\n"
            f"cascade_was_used: false\ntitle: Title of {paper_id}\n",
            encoding="utf-8",
        )
    return entry_path


def _make_entries(vault: Path, n: int, *, prefix: str = "2024_P") -> list[str]:
    """Create ``n`` trash entries with strictly increasing timestamps.

    Returns the paper ids in creation order (oldest first). Entry i gets a
    later timestamp than i-1, so `list_trash` (newest-first) reverses them.
    """
    ids: list[str] = []
    for i in range(n):
        pid = f"{prefix}{i:03d}"
        ts = f"202605{(i + 1):02d}T000000Z"  # day component encodes order
        _fabricate_trash_entry(vault, pid, ts)
        ids.append(pid)
    return ids


def test_enforce_cap_under_cap_no_eviction(vault: Path) -> None:
    _make_entries(vault, 3)
    evicted = enforce_cap(vault, cap=5)
    assert evicted == []
    # All three still on disk.
    assert len([e for e in list_trash(vault)]) == 3


def test_enforce_cap_at_cap_no_eviction(vault: Path) -> None:
    _make_entries(vault, 5)
    assert enforce_cap(vault, cap=5) == []
    assert len(list_trash(vault)) == 5


def test_enforce_cap_one_over_evicts_oldest(vault: Path) -> None:
    ids = _make_entries(vault, 6)  # oldest first
    evicted = enforce_cap(vault, cap=5)
    # Exactly the oldest one is removed.
    assert evicted == [ids[0]]
    remaining = {e.paper_id for e in list_trash(vault)}
    assert remaining == set(ids[1:])
    # Oldest entry dir + sidecar gone.
    assert not (vault / TRASH_DIRNAME / f"{ids[0]}-20260501T000000Z").exists()
    assert not (
        vault / TRASH_DIRNAME / f"{ids[0]}-20260501T000000Z.meta.yaml"
    ).exists()


def test_enforce_cap_evicts_tail_in_order(vault: Path) -> None:
    ids = _make_entries(vault, 8)  # oldest first: ids[0] oldest, ids[7] newest
    evicted = enforce_cap(vault, cap=3)
    # Keep newest 3 (ids[7..5]); evict the rest. list_trash is newest-first,
    # so entries[cap:] runs ids[4], ids[3], ids[2], ids[1], ids[0].
    assert evicted == [ids[4], ids[3], ids[2], ids[1], ids[0]]
    remaining = {e.paper_id for e in list_trash(vault)}
    assert remaining == set(ids[5:])


def test_enforce_cap_orders_missing_sidecar_by_name(vault: Path) -> None:
    # Three entries, the middle (by timestamp) one missing its sidecar.
    _fabricate_trash_entry(vault, "2024_A", "20260501T000000Z")
    _fabricate_trash_entry(
        vault, "2024_B", "20260503T000000Z", with_sidecar=False
    )
    _fabricate_trash_entry(vault, "2024_C", "20260505T000000Z")
    # cap=2 → evict the single oldest by in-name timestamp (2024_A).
    evicted = enforce_cap(vault, cap=2)
    assert evicted == ["2024_A"]
    remaining = {e.paper_id for e in list_trash(vault)}
    assert remaining == {"2024_B", "2024_C"}


def test_enforce_cap_best_effort_on_delete_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _make_entries(vault, 5)  # cap=2 → evict oldest 3: ids[2], ids[1], ids[0]
    failing_id = ids[1]
    real_rmtree = trash_mod.shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path).name.startswith(failing_id):
            raise OSError("simulated delete failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(trash_mod.shutil, "rmtree", flaky_rmtree)

    # Does not raise; the failed entry is skipped, others still evicted.
    evicted = enforce_cap(vault, cap=2)
    assert failing_id not in evicted
    assert ids[2] in evicted and ids[0] in evicted
    remaining = {e.paper_id for e in list_trash(vault)}
    # The two newest survive plus the entry whose delete failed.
    assert remaining == {ids[4], ids[3], failing_id}


def test_enforce_cap_default_cap_is_100(vault: Path) -> None:
    assert TRASH_MAX_ENTRIES == 100
    # Under default cap with a handful of entries → no-op.
    _make_entries(vault, 4)
    assert enforce_cap(vault) == []
