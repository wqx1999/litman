"""`--format json` on the five enumerate commands.

taxonomy / vault / project / code / trash `list` were table-only, so an agent
had to parse a Rich table (which folds long cells) or read TAXONOMY.md and
vaults.yaml behind the CLI's back — the thing ADR-007 exists to prevent.

Two rules bind every one of them:

* the table output is byte-for-byte what it always was, and
* stdout under `--format json` is *only* JSON — an empty result is `[]`,
  never the human "(trash is empty)" line, or an agent's parser breaks on
  the one case it is most likely to hit first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.code import CODES_DIRNAME, make_repo_meta, write_repo_meta
from litman.core.library import create_vault
from litman.core.taxonomy import ALL_DICTS

_ENUMERATE_COMMANDS = [
    ["taxonomy", "list"],
    ["project", "list"],
    ["code", "list"],
    ["trash", "list"],
]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _rows(result) -> list[dict]:
    """Parse the command's *stdout*.

    Deliberately not result.output, which folds stderr in: the contract is
    that JSON owns stdout while warnings (the drift hook, the registry
    nudge) go to stderr, and reading the combined stream would hide a
    regression that breaks every agent's parser.
    """
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    return payload


def _add_repo(vault: Path, name: str, papers: list[str] | None = None) -> None:
    repo_root = vault / CODES_DIRNAME / name
    repo_root.mkdir(parents=True)
    write_repo_meta(
        repo_root,
        make_repo_meta(name=name, upstream="https://x/y", papers=papers or []),
    )


# ---------------------------------------------------------------------------
# the rule that binds all of them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", _ENUMERATE_COMMANDS, ids=lambda c: " ".join(c))
def test_empty_result_is_an_empty_json_array_not_prose(
    vault: Path, command: list[str]
) -> None:
    """The case an agent hits first: nothing registered yet."""
    result = CliRunner().invoke(
        cli, [*command, "--format", "json", "--library", str(vault)]
    )
    rows = _rows(result)
    # taxonomy always carries its seven dicts; the other three start empty.
    if command[0] != "taxonomy":
        assert rows == []


@pytest.mark.parametrize("command", _ENUMERATE_COMMANDS, ids=lambda c: " ".join(c))
def test_json_stdout_carries_no_human_hint_line(
    vault: Path, command: list[str]
) -> None:
    """Each of these prints a trailing hint or count after its table.

    json.loads rejects trailing data, so this fails the moment one leaks.
    """
    result = CliRunner().invoke(
        cli, [*command, "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)


def test_vault_list_json_is_an_empty_array_when_nothing_is_registered() -> None:
    # The registry is isolated and empty per-test (conftest._isolate_registry).
    assert _rows(CliRunner().invoke(cli, ["vault", "list", "--format", "json"])) == []


def test_a_registry_warning_does_not_corrupt_the_json_on_stdout(
    vault: Path, tmp_path: Path
) -> None:
    """A moved vault makes the drift hook warn on *every* command.

    It is the most common untidy state there is, so if that warning shared
    stdout with the payload, `--format json` would be broken for exactly the
    users most likely to be automating their way out of the mess.
    """
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault)])
    registry = tmp_path / "litman-registry" / "vaults.yaml"
    registry.write_text(
        registry.read_text().replace(str(vault), str(tmp_path / "not-here"))
    )

    result = runner.invoke(cli, ["vault", "list", "--format", "json"])

    assert "dangling registration" in result.stderr, "expected the drift warning"
    json.loads(result.stdout)  # and yet stdout still parses whole


# ---------------------------------------------------------------------------
# taxonomy
# ---------------------------------------------------------------------------


def test_taxonomy_json_lists_every_dict(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "docking", "--library", str(vault)])

    rows = _rows(
        runner.invoke(
            cli, ["taxonomy", "list", "--format", "json", "--library", str(vault)]
        )
    )
    assert [r["dict"] for r in rows] == list(ALL_DICTS)
    assert next(r for r in rows if r["dict"] == "topics") == {
        "dict": "topics",
        "kind": "user",
        "count": 1,
        "values": ["docking"],
    }
    assert next(r for r in rows if r["dict"] == "priority")["kind"] == "fixed"


def test_taxonomy_json_for_one_dict_keeps_the_same_row_shape(vault: Path) -> None:
    """So an agent reads the named and unnamed forms with one parser."""
    runner = CliRunner()
    runner.invoke(
        cli, ["taxonomy", "add", "methods", "transformer", "--library", str(vault)]
    )
    rows = _rows(
        runner.invoke(
            cli,
            ["taxonomy", "list", "methods", "--format", "json", "--library", str(vault)],
        )
    )
    assert rows == [
        {"dict": "methods", "kind": "user", "count": 1, "values": ["transformer"]}
    ]


def test_taxonomy_json_still_rejects_an_unknown_dict(vault: Path) -> None:
    """The name is validated before the format branch, not after."""
    result = CliRunner().invoke(
        cli, ["taxonomy", "list", "nope", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert "Unknown dict" in str(result.exception)
    assert result.stdout == "", "an error must not also emit a JSON body"


# ---------------------------------------------------------------------------
# project — drift markers as machine tokens rather than ✓ / ⚠
# ---------------------------------------------------------------------------


def test_project_json_reports_ok_for_a_registered_project(
    vault: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    proj = tmp_path / "work"
    proj.mkdir()
    runner.invoke(
        cli, ["project", "add", "work", "--path", str(proj), "--library", str(vault)]
    )
    rows = _rows(
        runner.invoke(
            cli, ["project", "list", "--format", "json", "--library", str(vault)]
        )
    )
    assert rows == [{"name": "work", "path": str(proj), "status": "ok"}]


def test_project_json_reports_path_missing_when_the_folder_is_gone(
    vault: Path, tmp_path: Path
) -> None:
    """Cross-machine drift: the bound folder is not on this machine."""
    runner = CliRunner()
    proj = tmp_path / "work"
    proj.mkdir()
    runner.invoke(
        cli, ["project", "add", "work", "--path", str(proj), "--library", str(vault)]
    )
    proj.rmdir()

    rows = _rows(
        runner.invoke(
            cli, ["project", "list", "--format", "json", "--library", str(vault)]
        )
    )
    assert rows == [{"name": "work", "path": str(proj), "status": "path-missing"}]


def test_project_json_status_carries_no_display_glyphs(
    vault: Path, tmp_path: Path
) -> None:
    runner = CliRunner()
    proj = tmp_path / "work"
    proj.mkdir()
    runner.invoke(
        cli, ["project", "add", "work", "--path", str(proj), "--library", str(vault)]
    )
    raw = runner.invoke(
        cli, ["project", "list", "--format", "json", "--library", str(vault)]
    ).output
    for glyph in ("✓", "⚠", "[green]", "[yellow]"):
        assert glyph not in raw


# ---------------------------------------------------------------------------
# code — the repo-meta.yaml itself, not the table's summarised cells
# ---------------------------------------------------------------------------


def test_code_json_carries_the_paper_ids_not_a_summary_string(vault: Path) -> None:
    """The table renders Papers as "2 (id, ...)"; an agent needs the ids."""
    _add_repo(vault, "demo", papers=["a", "b"])

    rows = _rows(
        CliRunner().invoke(
            cli, ["code", "list", "--format", "json", "--library", str(vault)]
        )
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "demo"
    assert rows[0]["papers"] == ["a", "b"]
    assert rows[0]["upstream"] == "https://x/y"


def test_code_json_drops_the_private_path_key(vault: Path) -> None:
    """list_repos injects a synthetic _path; it is not part of the contract."""
    _add_repo(vault, "demo")

    rows = _rows(
        CliRunner().invoke(
            cli, ["code", "list", "--format", "json", "--library", str(vault)]
        )
    )
    assert rows
    assert not [k for k in rows[0] if k.startswith("_")], rows[0]


def test_code_json_honours_the_orphan_filter(vault: Path) -> None:
    _add_repo(vault, "bound", papers=["p1"])
    _add_repo(vault, "loose")

    rows = _rows(
        CliRunner().invoke(
            cli,
            ["code", "list", "--orphan", "--format", "json", "--library", str(vault)],
        )
    )
    assert [r["name"] for r in rows] == ["loose"]


# ---------------------------------------------------------------------------
# vault — the registry's own key names
# ---------------------------------------------------------------------------


def test_vault_json_uses_the_registry_field_names(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault)])

    rows = _rows(runner.invoke(cli, ["vault", "list", "--format", "json"]))
    assert len(rows) == 1
    assert set(rows[0]) == {
        "name",
        "path",
        "is_active",
        "papers",
        "imported_from",
        "imported_at",
    }
    assert rows[0]["name"] == "main"
    assert rows[0]["papers"] == 0


def test_vault_json_reports_an_unreachable_vault_as_null_papers(
    vault: Path, tmp_path: Path
) -> None:
    """The table's red "?" means "cannot know" — 0 would read as "empty"."""
    runner = CliRunner()
    runner.invoke(cli, ["vault", "add", "main", str(vault)])

    # A vaults.yaml synced from another laptop names a path that is not here.
    registry = tmp_path / "litman-registry" / "vaults.yaml"
    registry.write_text(
        registry.read_text().replace(str(vault), str(tmp_path / "not-here"))
    )

    rows = _rows(runner.invoke(cli, ["vault", "list", "--format", "json"]))
    assert rows[0]["papers"] is None


# ---------------------------------------------------------------------------
# trash
# ---------------------------------------------------------------------------


def test_trash_json_lists_a_deleted_paper(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    runner = CliRunner()
    rm = runner.invoke(cli, ["rm", paper_id, "--yes", "--library", str(vault)])
    assert rm.exit_code == 0, rm.output

    rows = _rows(
        runner.invoke(
            cli, ["trash", "list", "--format", "json", "--library", str(vault)]
        )
    )
    assert len(rows) == 1
    assert set(rows[0]) == {
        "paper_id",
        "deleted_at",
        "cascade_was_used",
        "title",
        "entry_name",
        "entry_path",
        "orphan_repos",
    }
    assert rows[0]["paper_id"] == paper_id
    assert rows[0]["cascade_was_used"] is False
    assert Path(rows[0]["entry_path"]).is_dir()
