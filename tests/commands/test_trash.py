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
from litman.core.library import create_vault
from litman.core.trash import TRASH_DIRNAME, list_trash, move_to_trash
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
    assert data["cascade_was_used"] is False
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


def test_rm_cascade_records_flag_in_sidecar(vault: Path) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(vault, "2024_Holder", related=["2024_Target"])
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rm", "2024_Target", "--cascade", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    trash_root = vault / TRASH_DIRNAME
    sidecar = next(trash_root.glob("*.meta.yaml"))
    data = _yaml.load(sidecar.read_text())
    assert data["cascade_was_used"] is True


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
        move_to_trash(vault, "9999_Ghost", cascade_was_used=False)


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
