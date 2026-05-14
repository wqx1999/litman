"""Tests for ``lit skim`` (M13 semantic sugar)."""

from __future__ import annotations

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


def test_skim_writes_status_skim(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(cli, ["skim", paper_id, "--library", str(vault)])
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["status"] == "skim"
