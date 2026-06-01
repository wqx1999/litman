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
from litman.exceptions import (
    CorruptMetadataError,
    ModifyError,
    PaperNotFoundError,
)

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
# --set fixed-enum validation (review F37)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    ["status=foo", "type=bogus", "priority=Z", "priority=1"],
)
def test_modify_set_rejects_invalid_fixed_enum(
    vault_with_paper: tuple[Path, str], spec: str
) -> None:
    # --set on a fixed-enum field must reject values outside the controlled
    # set, mirroring read-side check_schema (invariant #1).
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", paper_id, "--set", spec, "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "Allowed values" in str(result.exception)
    # Nothing was written — the original value is preserved.
    assert _read_meta(vault, paper_id)["status"] == "inbox"


@pytest.mark.parametrize("field", ["priority", "type"])
def test_modify_set_optional_enum_may_be_unset(
    vault_with_paper: tuple[Path, str], field: str
) -> None:
    # priority / type are "not yet evaluated" until the user fills them, so
    # --set field= (empty → None) is legal (M29).
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", paper_id, "--set", f"{field}=", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, paper_id)[field] is None


def test_modify_set_status_may_not_be_unset(
    vault_with_paper: tuple[Path, str]
) -> None:
    # status is required: its unevaluated state is the explicit value "inbox",
    # so clearing it to None is rejected.
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", paper_id, "--set", "status=", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "required" in str(result.exception)


def test_modify_set_valid_fixed_enum_succeeds(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", paper_id, "--set", "status=skim", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, paper_id)["status"] == "skim"


# ---------------------------------------------------------------------------
# --add-tag
# ---------------------------------------------------------------------------


def test_modify_add_tag_to_empty_list(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    runner.invoke(
        cli, ["taxonomy", "add", "topics", "peptide", "--library", str(vault)]
    )
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
        cli, ["taxonomy", "add", "topics", "peptide", "--library", str(vault)]
    )
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
    runner.invoke(
        cli, ["taxonomy", "add", "topics", "peptide", "AMP",
              "--library", str(vault)]
    )
    runner.invoke(
        cli, ["taxonomy", "add", "methods", "cell-free",
              "--library", str(vault)]
    )
    proj = vault / "_proj_pepforge"
    proj.mkdir()
    runner.invoke(
        cli, ["project", "add", "pepforge", "--path", str(proj),
              "--library", str(vault)]
    )
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
        cli, ["taxonomy", "add", "topics", "peptide", "AMP",
              "--library", str(vault)]
    )
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
    runner.invoke(
        cli, ["taxonomy", "add", "topics", "peptide", "--library", str(vault)]
    )
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
    runner.invoke(
        cli, ["taxonomy", "add", "topics", "peptide", "--library", str(vault)]
    )
    proj = vault / "_proj_pepforge"
    proj.mkdir()
    runner.invoke(
        cli, ["project", "add", "pepforge", "--path", str(proj),
              "--library", str(vault)]
    )
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
    assert "--paper-doi" in result.output


def test_modify_accepts_fuzzy_substring(
    vault_with_paper: tuple[Path, str],
) -> None:
    """M11 smoke: substring of the id resolves correctly through modify."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", "Foo", "--set", "priority=A", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    meta = _read_meta(vault, paper_id)
    assert meta["priority"] == "A"


def test_modify_accepts_paper_doi(
    vault_with_paper: tuple[Path, str],
) -> None:
    """M11 smoke: --paper-doi reverse-looks-up the id."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "modify",
            "--paper-doi",
            "10.1/x",
            "--set",
            "priority=A",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    meta = _read_meta(vault, paper_id)
    assert meta["priority"] == "A"


# ---------------------------------------------------------------------------
# M15: register-first validation on --add-tag
# ---------------------------------------------------------------------------


def test_modify_add_tag_unregistered_topic_rejected(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "topics=brand-new",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    msg = str(result.exception)
    assert "not registered" in msg
    assert "lit taxonomy add topics brand-new" in msg


def test_modify_add_tag_registered_topic_allowed(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "peptide", "--library", str(vault)],
    )
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "topics=peptide",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, paper_id)["topics"] == ["peptide"]


def test_modify_add_tag_unregistered_project_redirects_to_lit_project(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "projects=pepforge",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    msg = str(result.exception)
    assert "not registered" in msg
    assert "lit project add pepforge --path" in msg


def test_modify_rm_tag_not_validated(
    vault_with_paper: tuple[Path, str],
) -> None:
    """--rm-tag of a never-registered value is a legitimate cleanup, no reject."""
    vault, paper_id = vault_with_paper
    # Seed an unregistered value directly so we can prove --rm-tag clears it
    # without register-first complaining.
    runner = CliRunner()
    runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "stale", "--library", str(vault)],
    )
    runner.invoke(
        cli,
        ["modify", paper_id, "--add-tag", "topics=stale",
         "--library", str(vault)],
    )
    runner.invoke(
        cli,
        ["taxonomy", "rm", "topics", "stale", "--yes",
         "--library", str(vault)],
    )
    # 'stale' is gone from TAXONOMY now but the rm-tag must still work.
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--rm-tag", "topics=stale",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output


def test_modify_set_scalar_not_register_validated(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Schemaless scalar fields are NOT register-first checked (invariant #7)."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", paper_id, "--set", "read-date=2026-05-16",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, paper_id)["read-date"] == "2026-05-16"


# ---------------------------------------------------------------------------
# M23.0: ADR-012 symmetric relations — auto double-write
# ---------------------------------------------------------------------------


def _write_relation_paper(vault: Path, paper_id: str) -> None:
    """Hand-write a paper carrying the full relation schema incl. reverse
    fields (extended-by / contradicted-by)."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\n"
        f"title: {paper_id}\n"
        "authors:\n"
        "  - Foo, Alice\n"
        "year: 2024\n"
        "journal: Test J.\n"
        f"doi: 10.0/{paper_id}\n"
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
        "contradicted-by: []\n"
        "extends: []\n"
        "extended-by: []\n"
        "code-clones: []\n",
        encoding="utf-8",
    )


@pytest.fixture
def vault_two_papers(tmp_path: Path) -> tuple[Path, str, str]:
    vault = create_vault(tmp_path)
    a, b = "2024_Paper_A", "2024_Paper_B"
    _write_relation_paper(vault, a)
    _write_relation_paper(vault, b)
    return vault, a, b


def test_modify_extends_double_writes_reverse(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC1: --add-tag extends=B writes A.extends:[B] AND B.extended-by:[A]."""
    vault, a, b = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={b}", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["extends"] == [b]
    assert _read_meta(vault, b)["extended-by"] == [a]
    # opposite paper's updated-at also bumped.
    assert _read_meta(vault, b)["updated-at"] != "2026-04-28T10:00:00+02:00"


def test_modify_relation_corrupt_opposite_friendly_error(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    # Review A1: `lit modify A --add-tag extends=B` round-trip-loads B to
    # mirror the reverse edge. A corrupt B must raise a friendly,
    # path-naming CorruptMetadataError, not a raw ruamel YAMLError — modifying
    # A should not crash on an *unrelated* paper's bad YAML.
    vault, a, b = vault_two_papers
    (vault / "papers" / b / "metadata.yaml").write_text(
        "extended-by: [unterminated\n: : :\n", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={b}", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CorruptMetadataError)
    assert b in str(result.exception)


def test_modify_directed_self_reference_rejected(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    # Review F21: `--add-tag extends=A` on A creates a permanent asymmetry
    # (the paired extended-by self-edge cannot be represented). Reject up-front.
    vault, a, _ = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={a}", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "self-reference" in str(result.exception)
    # Nothing was written — no half-edge persisted.
    assert _read_meta(vault, a)["extends"] == []


def test_modify_symmetric_self_reference_allowed(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    # `related` is symmetric (self-paired), so a self-loop is consistent and
    # must still be allowed (only directed relations are rejected).
    vault, a, _ = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--add-tag", f"related={a}", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["related"] == [a]


def test_modify_relation_to_empty_opposite_rejected(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    # Review A4: adding a relation to a paper whose metadata.yaml is empty
    # would write an invisible one-directional edge (list_papers drops the
    # empty paper, so check_bidirectional_refs never sees the missing reverse).
    vault, a, b = vault_two_papers
    (vault / "papers" / b / "metadata.yaml").write_text("", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={b}", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "empty" in str(result.exception)
    assert _read_meta(vault, a)["extends"] == []


def test_modify_extends_rm_double_removes_reverse(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC1: --rm-tag extends=B removes both sides symmetrically."""
    vault, a, b = vault_two_papers
    runner = CliRunner()
    runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={b}", "--library", str(vault)]
    )
    result = runner.invoke(
        cli, ["modify", a, "--rm-tag", f"extends={b}", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["extends"] == []
    assert _read_meta(vault, b)["extended-by"] == []


def test_modify_contradicts_double_writes_reverse(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC2: contradicts ↔ contradicted-by."""
    vault, a, b = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", a, "--add-tag", f"contradicts={b}", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["contradicts"] == [b]
    assert _read_meta(vault, b)["contradicted-by"] == [a]

    result = runner.invoke(
        cli,
        ["modify", a, "--rm-tag", f"contradicts={b}", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["contradicts"] == []
    assert _read_meta(vault, b)["contradicted-by"] == []


def test_modify_related_double_writes_symmetric(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC2: related ↔ related (self-paired)."""
    vault, a, b = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--add-tag", f"related={b}", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["related"] == [b]
    assert _read_meta(vault, b)["related"] == [a]

    result = runner.invoke(
        cli, ["modify", a, "--rm-tag", f"related={b}", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["related"] == []
    assert _read_meta(vault, b)["related"] == []


def test_modify_relation_passes_bidirectional_check(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """Auto double-write leaves the vault green under check_bidirectional_refs."""
    from litman.core.checks import check_bidirectional_refs
    from litman.core.document import list_papers

    vault, a, b = vault_two_papers
    runner = CliRunner()
    runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={b}", "--library", str(vault)]
    )
    issues = check_bidirectional_refs(vault, list_papers(vault))
    assert issues == []


def test_modify_opposite_missing_single_side_only(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC6 boundary: opposite paper absent → write originating side only,
    no error, no opposite created."""
    vault, a, _b = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", a, "--add-tag", "extends=2099_Ghost",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["extends"] == ["2099_Ghost"]
    # ghost paper was NOT created.
    assert not (vault / "papers" / "2099_Ghost").exists()


def test_modify_self_reference_skips_double_write(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC6 boundary: related=<self> records the forward edge but does not
    attempt a separate opposite write (no crash, no extra field)."""
    vault, a, _b = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--add-tag", f"related={a}", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_meta(vault, a)["related"] == [a]


def test_modify_opposite_dedup_no_duplicate(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC6 boundary: re-adding an existing edge does not duplicate the
    reverse entry on the opposite paper."""
    vault, a, b = vault_two_papers
    runner = CliRunner()
    runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={b}", "--library", str(vault)]
    )
    # Add the same edge again.
    runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={b}", "--library", str(vault)]
    )
    assert _read_meta(vault, a)["extends"] == [b]
    assert _read_meta(vault, b)["extended-by"] == [a]  # not [a, a]


def test_modify_reverse_field_rejected_for_user(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC7: user cannot set a reverse field via --add-tag extended-by=..."""
    vault, a, b = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", a, "--add-tag", f"extended-by={b}", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    msg = str(result.exception)
    assert "extended-by" in msg
    assert "reverse relation field" in msg
    # nothing written.
    assert _read_meta(vault, a)["extended-by"] == []


def test_modify_reverse_field_rejected_rm_tag(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC7: --rm-tag contradicted-by=... is rejected too."""
    vault, a, b = vault_two_papers
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["modify", a, "--rm-tag", f"contradicted-by={b}",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ModifyError)
    assert "contradicted-by" in str(result.exception)


def test_modify_opposite_only_change_not_treated_as_noop(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """When the originating side already has the edge but the opposite is
    missing its reverse entry, the command must still reconcile the
    opposite rather than short-circuiting as a no-op."""
    vault, a, b = vault_two_papers
    # Seed A.extends:[B] directly WITHOUT the reverse on B (simulates a
    # pre-feature vault).
    meta_a = (vault / "papers" / a / "metadata.yaml")
    meta_a.write_text(
        meta_a.read_text().replace("extends: []\n", f"extends:\n  - {b}\n"),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--add-tag", f"extends={b}", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # A unchanged (already had it), but B's reverse now reconciled.
    assert _read_meta(vault, a)["extends"] == [b]
    assert _read_meta(vault, b)["extended-by"] == [a]


def test_modify_rm_tag_opposite_lacks_item_is_noop(
    vault_two_papers: tuple[Path, str, str],
) -> None:
    """AC6 boundary (d): --rm-tag when the opposite paper never had the
    reverse entry must be a true no-op on the opposite (no spurious write,
    no updated-at bump). Seeds A.extends:[B] with B.extended-by empty (a
    hand-edited / pre-feature vault) then removes the forward edge."""
    vault, a, b = vault_two_papers
    seed_updated = "2026-04-28T10:00:00+02:00"
    meta_a = vault / "papers" / a / "metadata.yaml"
    meta_a.write_text(
        meta_a.read_text().replace("extends: []\n", f"extends:\n  - {b}\n"),
        encoding="utf-8",
    )
    # B's extended-by deliberately stays [] (asymmetric seed).
    assert _read_meta(vault, b)["extended-by"] == []
    assert _read_meta(vault, b)["updated-at"] == seed_updated
    runner = CliRunner()
    result = runner.invoke(
        cli, ["modify", a, "--rm-tag", f"extends={b}", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Originating side dropped the edge.
    assert _read_meta(vault, a)["extends"] == []
    # Opposite untouched: still empty AND not re-stamped.
    assert _read_meta(vault, b)["extended-by"] == []
    assert _read_meta(vault, b)["updated-at"] == seed_updated
