"""Read commands pick their output by where stdout goes (ADR-021: the CLI is
the agents' API, people read the GUI).

No ``--format``: JSON when stdout is not a terminal (an agent's shell, a pipe,
a file), the table when a person is at one. An explicit ``--format`` wins
either way. CliRunner's stdout is not a terminal; the terminal side is
reached by patching the one check every read command shares.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import litman.commands._options as options_mod
from litman.cli import cli
from litman.core.library import create_vault

PAPER = "2024_X_Foo"

READ_COMMANDS: list[list[str]] = [
    ["list"],
    ["show", PAPER],
    ["project", "list"],
    ["taxonomy", "list"],
    ["code", "list"],
    ["trash", "list"],
    ["vault", "list"],
]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = create_vault(tmp_path)
    paper_dir = v / "papers" / PAPER
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {PAPER}\nyear: 2024\ntitle: Foo paper\n", encoding="utf-8"
    )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4 fake\n")
    (paper_dir / "notes.md").write_text("# Foo paper\n", encoding="utf-8")
    return v


def _run(vault: Path, command: list[str], *extra: str):
    args = [*command, *extra]
    if command != ["vault", "list"]:
        args += ["--library", str(vault)]
    return CliRunner().invoke(cli, args)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("command", READ_COMMANDS, ids=lambda c: " ".join(c))
def test_read_commands_default_to_json_off_a_terminal(
    vault: Path, command: list[str]
) -> None:
    result = _run(vault, command)
    assert result.exit_code == 0, result.output
    assert _is_json(result.stdout), result.stdout


@pytest.mark.parametrize("command", READ_COMMANDS, ids=lambda c: " ".join(c))
def test_read_commands_default_to_the_table_at_a_terminal(
    vault: Path, command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(options_mod, "is_interactive", lambda _stream: True)
    result = _run(vault, command)
    assert result.exit_code == 0, result.output
    assert not _is_json(result.stdout), result.stdout


@pytest.mark.parametrize("command", READ_COMMANDS, ids=lambda c: " ".join(c))
def test_an_explicit_format_wins_either_way(
    vault: Path, command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    piped_table = _run(vault, command, "--format", "table")
    assert piped_table.exit_code == 0, piped_table.output
    assert not _is_json(piped_table.stdout)

    monkeypatch.setattr(options_mod, "is_interactive", lambda _stream: True)
    terminal_json = _run(vault, command, "--format", "json")
    assert terminal_json.exit_code == 0, terminal_json.output
    assert _is_json(terminal_json.stdout)


def test_the_check_reads_stdout_through_is_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real default, not a stub: is_interactive is asked about sys.stdout,
    so Windows' console check (core/confirm) applies too."""
    seen: list[object] = []

    def fake(stream: object) -> bool:
        seen.append(stream)
        return False

    import sys

    monkeypatch.setattr(options_mod, "is_interactive", fake)
    assert options_mod.table_or_json(None, None, None) == "json"
    assert seen == [sys.stdout]
    assert options_mod.table_or_json(None, None, "table") == "table"
