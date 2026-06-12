"""Tests for ``lit revisit`` (M13 semantic sugar)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault

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
        "read-date: '2026-05-01'\n"
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


def test_revisit_writes_today_by_default(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(cli, ["revisit", paper_id, "--library", str(vault)])
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["last-revisited"] == _today_iso()
    # read-date untouched — revisit is its own semantic field.
    assert meta["read-date"] == "2026-05-01"


def test_revisit_with_explicit_date_override(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["revisit", paper_id, "--date", "2026-05-11", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["last-revisited"] == "2026-05-11"


@pytest.mark.parametrize(
    "bad_date", ["foo", "2026-5-11", "20260530", "2026-W22-1"]
)
def test_revisit_rejects_malformed_and_relaxed_dates(
    vault_with_paper: tuple[Path, str], bad_date: str
) -> None:
    # Review F28: --date must be strict YYYY-MM-DD. Mirrors lit read; both
    # share core.dates.validate_iso_date.
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["revisit", paper_id, "--date", bad_date, "--library", str(vault)],
    )
    assert result.exit_code != 0
    meta = _read_meta(vault, paper_id)
    assert meta["last-revisited"] is None
    assert meta["updated-at"] == meta["created-at"]


def test_revisit_requires_read_date(tmp_path: Path) -> None:
    """A revisit presupposes a first read: revisiting a paper that has no
    read-date is refused, and nothing is written (invariant #11)."""
    vault = create_vault(tmp_path)
    paper_id = "2024_Unread"
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_Unread\n"
        "title: Unread\n"
        "authors:\n"
        "  - Foo, Alice\n"
        "year: 2024\n"
        "created-at: '2026-04-28T10:00:00+02:00'\n"
        "updated-at: '2026-04-28T10:00:00+02:00'\n"
        "status: inbox\n"
        "read-date:\n"
        "last-revisited:\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["revisit", paper_id, "--library", str(vault)])
    assert result.exit_code != 0
    # nothing written — last-revisited stays empty.
    assert _read_meta(vault, paper_id)["last-revisited"] is None
