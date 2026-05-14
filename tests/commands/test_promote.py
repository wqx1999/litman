"""Tests for ``lit promote`` (M13 semantic sugar).

These tests lock the OQ4 decision: ``lit promote`` only changes
``status``; it MUST NOT also stamp ``read-date``. A future agent that
tries to "make promote more convenient" by silently writing today's date
to ``read-date`` would be overriding an explicit design decision — the
second test below catches that.
"""

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
    # Note: read-date is pre-populated to an OLD value so any accidental
    # overwrite by promote_cmd is detectable (would become today's date).
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
        "status: skim\n"
        "priority: B\n"
        "read-date: '2026-01-15'\n"
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


def test_promote_writes_status_deep_read(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(cli, ["promote", paper_id, "--library", str(vault)])
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["status"] == "deep-read"


def test_promote_does_not_touch_read_date(
    vault_with_paper: tuple[Path, str],
) -> None:
    """OQ4 lock: promote must leave read-date alone.

    The fixture pre-populates read-date='2026-01-15'. If a future change
    has promote_cmd write today's date to read-date, this assert flips
    and the regression is caught before the new behaviour ships.
    """
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    result = runner.invoke(cli, ["promote", paper_id, "--library", str(vault)])
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, paper_id)
    assert meta["read-date"] == "2026-01-15", (
        "lit promote must not modify read-date — see M13 OQ4 decision "
        "and dev_docs/proposals/archive/semantic-sugar-commands.md."
    )
