"""Tests for ``lit modify``."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.exceptions import ModifyError, PaperNotFoundError

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_with_paper(tmp_path: Path) -> tuple[Path, str]:
    """Vault containing one paper with the canonical M2.0 metadata schema."""
    vault = create_vault(tmp_path)
    paper_id = "2024_Foo_Bar"
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)

    # Hand-crafted minimal metadata matching what `lit add` writes today.
    meta = (paper_dir / "metadata.yaml")
    meta.write_text(
        "id: 2024_Foo_Bar\n"
        "title: Foo Bar\n"
        "authors:\n"
        "  - Foo, Alice\n"
        "year: 2024\n"
        "journal: Test J.\n"
        "doi: 10.1/x\n"
        "arxiv-id:\n"
        "github:\n"
        "created-at: '2026-04-28T10:00:00+02:00'\n"
        "updated-at: '2026-04-28T10:00:00+02:00'\n"
        "projects: []\n"
        "topics: []\n"
        "methods: []\n"
        "data: []\n"
        "type: research\n"
        "status: inbox\n"
        "priority: B\n"
        "read-date:\n"
        "last-revisited:\n"
        "related: []\n"
        "contradicts: []\n"
        "extends: []\n"
        "code-clones: []\n",
        encoding="utf-8",
    )
    return vault, paper_id


def _read_meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


def _read_index(vault: Path) -> dict[str, Any]:
    return json.loads((vault / "INDEX.json").read_text())


# ---------------------------------------------------------------------------
# --set: scalar fields
# ---------------------------------------------------------------------------


def test_modify_set_scalar(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "priority=A", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["priority"] == "A"
    # updated-at bumped past created-at.
    assert meta["updated-at"] != meta["created-at"]


def test_modify_set_int_coercion(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "year=2025", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["year"] == 2025  # int, not str
    assert isinstance(meta["year"], int)


def test_modify_set_empty_value_unsets(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "doi=", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["doi"] is None


def test_modify_set_arbitrary_field(vault_with_paper: tuple[Path, str]) -> None:
    """Schema-less metadata: --set on a previously-unknown scalar key works."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "custom-tag=draft", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["custom-tag"] == "draft"


def test_modify_set_multiple_in_one_invocation(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "modify", paper_id,
            "--set", "priority=A",
            "--set", "status=deep-read",
            "--set", "read-date=2026-04-28",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["priority"] == "A"
    assert meta["status"] == "deep-read"
    assert meta["read-date"] == "2026-04-28"


# ---------------------------------------------------------------------------
# --set: forbidden fields
# ---------------------------------------------------------------------------


def test_modify_set_id_forbidden(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "id=hacked", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "lit rename" in str(result.exception)


def test_modify_set_created_at_forbidden(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "modify", paper_id,
            "--set", "created-at=2020-01-01T00:00:00+00:00",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)


def test_modify_set_updated_at_forbidden(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "modify", paper_id,
            "--set", "updated-at=2020-01-01T00:00:00+00:00",
            "--library", str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)


def test_modify_set_on_list_field_rejected(
    vault_with_paper: tuple[Path, str]
) -> None:
    """--set topics=foo would clobber a list; force user to use --add-tag."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "topics=foo", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "--add-tag" in str(result.exception)


# ---------------------------------------------------------------------------
# --add-tag
# ---------------------------------------------------------------------------


def test_modify_add_tag_to_empty_list(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "topics=peptide", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["topics"] == ["peptide"]


def test_modify_add_tag_dedupes(vault_with_paper: tuple[Path, str]) -> None:
    """Adding a value already present is an idempotent no-op (no duplication)."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "topics=peptide", "--library", str(vault)],
    )
    # Second add of the same value
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "topics=peptide", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output

    meta = _read_meta(vault, paper_id)
    assert meta["topics"] == ["peptide"]  # not duplicated


def test_modify_add_tag_multiple_fields(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "modify", paper_id,
            "--add-tag", "topics=peptide",
            "--add-tag", "topics=AMP",
            "--add-tag", "methods=cell-free",
            "--add-tag", "projects=pepforge",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["topics"] == ["peptide", "AMP"]
    assert meta["methods"] == ["cell-free"]
    assert meta["projects"] == ["pepforge"]


def test_modify_add_tag_on_scalar_rejected(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "priority=A", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "list field" in str(result.exception)


def test_modify_add_tag_empty_value_rejected(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "topics=", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)


# ---------------------------------------------------------------------------
# --rm-tag
# ---------------------------------------------------------------------------


def test_modify_rm_tag_existing(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "modify", paper_id,
            "--add-tag", "topics=peptide",
            "--add-tag", "topics=AMP",
            "--library", str(vault),
        ],
    )
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--rm-tag", "topics=peptide", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["topics"] == ["AMP"]


def test_modify_rm_tag_absent_is_silent_noop(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--rm-tag", "topics=ghost", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output

    meta = _read_meta(vault, paper_id)
    assert meta["topics"] == []
    # updated-at not bumped because no real change.
    assert meta["updated-at"] == meta["created-at"]


def test_modify_rm_tag_on_scalar_rejected(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--rm-tag", "priority=B", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)


# ---------------------------------------------------------------------------
# Side effects: updated-at, INDEX.json, views/
# ---------------------------------------------------------------------------


def test_modify_bumps_updated_at(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    before_meta = _read_meta(vault, paper_id)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "priority=A", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    after_meta = _read_meta(vault, paper_id)
    assert after_meta["created-at"] == before_meta["created-at"]
    new_ts = datetime.fromisoformat(after_meta["updated-at"])
    old_ts = datetime.fromisoformat(before_meta["updated-at"])
    assert new_ts > old_ts


def test_modify_refreshes_index_json(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "modify", paper_id,
            "--set", "priority=A",
            "--add-tag", "topics=peptide",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    payload = _read_index(vault)
    assert payload["n_papers"] == 1
    p = payload["papers"][0]
    assert p["id"] == paper_id
    assert p["priority"] == "A"
    assert p["topics"] == ["peptide"]


def test_modify_rebuilds_views(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "modify", paper_id,
            "--add-tag", "topics=peptide",
            "--add-tag", "projects=pepforge",
            "--set", "status=deep-read",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    assert (vault / "views" / "by-topic" / "peptide" / paper_id).is_symlink()
    assert (vault / "views" / "by-project" / "pepforge" / paper_id).is_symlink()
    assert (vault / "views" / "by-status" / "deep-read" / paper_id).is_symlink()


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_modify_no_op_when_all_changes_redundant(
    vault_with_paper: tuple[Path, str]
) -> None:
    """All requested changes already in effect → don't bump updated-at."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--rm-tag", "topics=missing", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output

    meta = _read_meta(vault, paper_id)
    assert meta["updated-at"] == meta["created-at"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_modify_no_ops_provided(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", paper_id, "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "at least one" in str(result.exception)


def test_modify_unknown_paper(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", "9999_Ghost", "--set", "priority=A", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_modify_malformed_kv(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "no-equals-sign", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "KEY=VALUE" in str(result.exception)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_modify_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["modify", "--help"])
    assert result.exit_code == 0
    assert "--set" in result.output
    assert "--add-tag" in result.output
    assert "--rm-tag" in result.output
