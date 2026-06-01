"""Tests for ``lit read`` (M13 semantic sugar)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.exceptions import PaperNotFoundError

_yaml = YAML(typ="safe")


@pytest.fixture
def vault_with_paper(tmp_path: Path) -> tuple[Path, str]:
    vault = create_vault(tmp_path)
    paper_id = "2024_Foo_Bar"
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_Foo_Bar\n"
        "title: Foo Bar\n"
        "authors:\n"
        "  - Foo, Alice\n"
        "year: 2024\n"
        "doi: 10.1/x\n"
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


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def test_read_writes_today_by_default(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(cli, ["read", paper_id, "--library", str(vault)])
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["read-date"] == _today_iso()
    # updated-at bumped past created-at.
    assert meta["updated-at"] != meta["created-at"]


def test_read_with_explicit_date_override(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["read", paper_id, "--date", "2026-05-11", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["read-date"] == "2026-05-11"


def test_read_same_day_is_noop(vault_with_paper: tuple[Path, str]) -> None:
    """Re-running with the same date must not bump updated-at."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    runner.invoke(
        cli,
        ["read", paper_id, "--date", "2026-05-11", "--library", str(vault)],
    )
    meta_after_first = _read_meta(vault, paper_id)
    first_updated_at = meta_after_first["updated-at"]

    result = runner.invoke(
        cli,
        ["read", paper_id, "--date", "2026-05-11", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output

    meta_after_second = _read_meta(vault, paper_id)
    assert meta_after_second["read-date"] == "2026-05-11"
    # updated-at unchanged — invariant #11: no real change → no timestamp bump.
    assert meta_after_second["updated-at"] == first_updated_at


def test_read_paper_not_found(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli, ["read", "9999_Ghost", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_read_accepts_paper_doi(vault_with_paper: tuple[Path, str]) -> None:
    """M11 smoke: --paper-doi reverse-lookup resolves to the right paper."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "read",
            "--paper-doi", "10.1/x",
            "--date", "2026-05-11",
            "--library", str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["read-date"] == "2026-05-11"


def test_read_rejects_malformed_date(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["read", paper_id, "--date", "foo", "--library", str(vault)],
    )
    assert result.exit_code != 0
    # click.BadParameter renders as a UsageError; verify metadata untouched.
    meta = _read_meta(vault, paper_id)
    assert meta["read-date"] is None
    assert meta["updated-at"] == meta["created-at"]


def test_read_rejects_non_zero_padded_date(
    vault_with_paper: tuple[Path, str],
) -> None:
    """`--date 2026-5-11` (non zero-padded) must be rejected — strict ISO 8601.

    Guards against the historical strptime path that silently accepted
    `2026-5-11`; downstream string comparisons in INDEX.json depend on
    canonical zero-padded YYYY-MM-DD.
    """
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["read", paper_id, "--date", "2026-5-11", "--library", str(vault)],
    )
    assert result.exit_code != 0
    meta = _read_meta(vault, paper_id)
    assert meta["read-date"] is None
    assert meta["updated-at"] == meta["created-at"]


@pytest.mark.parametrize("bad_date", ["20260530", "2026-W22-1", "2026W221"])
def test_read_rejects_relaxed_iso_forms(
    vault_with_paper: tuple[Path, str], bad_date: str
) -> None:
    """Review F28: Python 3.11+ relaxed date.fromisoformat to accept ISO basic
    (20260530) and week dates (2026-W22-1). Those parse to a real date but sort
    wrong stored as a string, so the shape gate must reject them."""
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["read", paper_id, "--date", bad_date, "--library", str(vault)],
    )
    assert result.exit_code != 0
    meta = _read_meta(vault, paper_id)
    assert meta["read-date"] is None
    assert meta["updated-at"] == meta["created-at"]
