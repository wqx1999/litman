"""Tests for `lit show <id>`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault
from litman.exceptions import PaperNotFoundError


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = create_vault(tmp_path)
    paper_dir = v / "papers" / "2024_X_Foo"
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_X_Foo\n"
        "year: 2024\n"
        "title: Foo paper\n"
        "doi: 10.1234/test-foo\n"
        "authors:\n"
        "  - Smith, J\n",
        encoding="utf-8",
    )
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4 fake\n")
    (paper_dir / "notes.md").write_text("# notes\n", encoding="utf-8")
    return v


def test_show_existing_paper_prints_metadata_and_paths(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0
    assert "2024_X_Foo" in result.output
    assert "Foo paper" in result.output
    assert "Smith, J" in result.output
    assert "PDF:" in result.output
    assert "Notes:" in result.output
    assert "paper.pdf" in result.output
    assert "notes.md" in result.output


def test_show_missing_paper_raises_paper_not_found(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Missing", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_show_warns_about_missing_pdf(vault: Path) -> None:
    (vault / "papers" / "2024_X_Foo" / "paper.pdf").unlink()
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0
    assert "missing!" in result.output


def test_show_warns_about_missing_notes(vault: Path) -> None:
    (vault / "papers" / "2024_X_Foo" / "notes.md").unlink()
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0
    assert "missing!" in result.output


def test_show_rejects_invalid_id_with_path_traversal(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "../../../etc/passwd", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_show_accepts_fuzzy_substring(vault: Path) -> None:
    """M11: a unique case-insensitive substring resolves to the paper id."""
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "foo", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "2024_X_Foo" in result.output


def test_show_accepts_paper_doi(vault: Path) -> None:
    """M11: --paper-doi reverse-looks-up the id via INDEX-less linear scan."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["show", "--paper-doi", "10.1234/test-foo", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "2024_X_Foo" in result.output


def test_show_paper_id_and_doi_mutually_exclusive(vault: Path) -> None:
    """M11: setting both is an error from the unified XOR helper."""
    from litman.exceptions import LitmanError

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "show",
            "2024_X_Foo",
            "--paper-doi",
            "10.1234/test-foo",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, LitmanError)
    assert "mutually exclusive" in str(result.exception)


def test_show_with_no_id_falls_back_to_the_most_recent_paper(vault: Path) -> None:
    """Was "No paper specified."; now the paper you last engaged with.

    The fallback and its guarantees live in test_open_show_default.py — this
    only pins that `lit show` no longer refuses the no-argument form.
    """
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "2024_X_Foo" in result.stdout
    assert "most recently engaged" in result.stderr


# ---------------------------------------------------------------------------
# --format json (M33)
# ---------------------------------------------------------------------------


def test_show_json_emits_full_metadata(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["show", "2024_X_Foo", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    meta = json.loads(result.output)
    assert meta["id"] == "2024_X_Foo"
    assert meta["title"] == "Foo paper"
    assert meta["doi"] == "10.1234/test-foo"
    assert meta["authors"] == ["Smith, J"]
    # Full metadata, not the INDEX projection: `doi` + `authors` are present
    # together (authors is NOT in the INDEX projection).


def test_show_json_serializes_date_and_datetime(vault: Path) -> None:
    """The §9 trap: a paper WITH read-date / created-at must not raise.

    The YAML safe-loader parses created-at into a datetime and read-date into a
    date; json.dumps cannot serialize either natively, so default=str must
    bridge them.
    """
    paper_dir = vault / "papers" / "2024_X_Foo"
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_X_Foo\n"
        "year: 2024\n"
        "title: Foo paper\n"
        "created-at: 2026-05-26T10:00:00+02:00\n"
        "updated-at: 2026-05-27T11:00:00+02:00\n"
        "read-date: 2026-05-28\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["show", "2024_X_Foo", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert result.exception is None
    meta = json.loads(result.output)
    assert meta["read-date"] == "2026-05-28"
    assert meta["created-at"].startswith("2026-05-26")


def test_show_json_tolerates_missing_fields(vault: Path) -> None:
    """A minimal metadata file (only identity fields) serializes fine."""
    paper_dir = vault / "papers" / "2024_X_Foo"
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_X_Foo\nyear: 2024\ntitle: Foo\n", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(
        cli, ["show", "2024_X_Foo", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    meta = json.loads(result.output)
    meta.pop("_paths")
    assert meta == {"id": "2024_X_Foo", "year": 2024, "title": "Foo"}


def test_show_table_is_default(vault: Path) -> None:
    """Default (no --format) renders the Panel view, not JSON."""
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "PDF:" in result.output
    assert "Notes:" in result.output


def test_show_ambiguous_substring_lists_candidates(vault: Path) -> None:
    """M11: 2+ matches must surface the candidate ids in the error message."""
    (vault / "papers" / "2025_X_Foobar").mkdir(parents=True)
    (vault / "papers" / "2025_X_Foobar" / "metadata.yaml").write_text(
        "id: 2025_X_Foobar\nyear: 2025\ntitle: Foobar\nauthors:\n  - Jones, K\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["show", "foo", "--library", str(vault)])
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)
    msg = str(result.exception)
    assert "Ambiguous" in msg
    assert "2024_X_Foo" in msg
    assert "2025_X_Foobar" in msg


# ---------------------------------------------------------------------------
# --format json carries the paths; the table does not fold them
# ---------------------------------------------------------------------------


def _show_json(vault: Path, paper_id: str = "2024_X_Foo") -> dict:
    result = CliRunner().invoke(
        cli, ["show", paper_id, "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_show_json_carries_the_paths_an_agent_opens_next(vault: Path) -> None:
    paper_dir = vault / "papers" / "2024_X_Foo"
    (paper_dir / "discussion.md").write_text("# log\n", encoding="utf-8")
    si_dir = paper_dir / "supplementary"
    si_dir.mkdir()
    (si_dir / "si-01.pdf").write_bytes(b"%PDF-1.4 si\n")
    (paper_dir / "metadata.yaml").write_text(
        "id: 2024_X_Foo\nyear: 2024\ntitle: Foo paper\n"
        "supplementary:\n"
        "  - file: si-01.pdf\n    original: s1.pdf\n"
        "  - file: si-02.xlsx\n    original: s2.xlsx\n"
        "code-clones:\n  - here\n  - gone\n",
        encoding="utf-8",
    )
    (vault / "codes" / "here").mkdir(parents=True)
    (vault / "codes" / "here" / "repo-meta.yaml").write_text("name: here\n", encoding="utf-8")

    paths = _show_json(vault)["_paths"]
    assert paths["folder"] == str(paper_dir)
    assert paths["pdf"] == str(paper_dir / "paper.pdf")
    assert paths["notes"] == str(paper_dir / "notes.md")
    assert paths["discussion"] == str(paper_dir / "discussion.md")
    assert paths["si"] == [str(si_dir / "si-01.pdf"), str(si_dir / "si-02.xlsx")]
    assert paths["code"] == [str(vault / "codes" / "here"), str(vault / "codes" / "gone")]
    # Exactly what is not on disk, the table's (missing!).
    assert paths["missing"] == [str(si_dir / "si-02.xlsx"), str(vault / "codes" / "gone")]


def test_show_json_paths_name_a_missing_pdf_and_discussion(vault: Path) -> None:
    paper_dir = vault / "papers" / "2024_X_Foo"
    (paper_dir / "paper.pdf").unlink()
    paths = _show_json(vault)["_paths"]
    assert paths["missing"] == [
        str(paper_dir / "paper.pdf"),
        str(paper_dir / "discussion.md"),
    ]
    assert paths["si"] == []
    assert paths["code"] == []


def test_show_paths_are_not_folded_on_an_80_column_console(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-TTY console (an agent's) is 80 columns wide: each path must stay on
    one line so it can be used as is."""
    from rich.console import Console

    import litman.commands.show as show_mod

    deep = vault / "papers" / "2024_X_Foo"
    (deep / "metadata.yaml").write_text(
        "id: 2024_X_Foo\nyear: 2024\ntitle: Foo\ncode-clones:\n"
        "  - a-code-folder-name-long-enough-to-overflow-eighty-columns\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(show_mod, "console", Console(width=80))
    result = CliRunner().invoke(cli, ["show", "2024_X_Foo", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    for label, path in (
        ("PDF:", deep / "paper.pdf"),
        ("Notes:", deep / "notes.md"),
        ("Code:", vault / "codes" / "a-code-folder-name-long-enough-to-overflow-eighty-columns"),
    ):
        assert len(f"{label} {path}") > 80
        assert any(
            line.startswith(label) and str(path) in line for line in lines
        ), label


def test_show_ambiguous_substring_lists_at_most_ten_ids(vault: Path) -> None:
    for i in range(12):
        pid = f"2025_Y_Foo{i:02d}"
        (vault / "papers" / pid).mkdir(parents=True)
        (vault / "papers" / pid / "metadata.yaml").write_text(
            f"id: {pid}\nyear: 2025\ntitle: T\n", encoding="utf-8"
        )
    result = CliRunner().invoke(cli, ["show", "foo", "--library", str(vault)])
    msg = str(result.exception)
    assert "matched 13 papers" in msg
    assert "2024_X_Foo" in msg and "2025_Y_Foo08" in msg
    assert "2025_Y_Foo09" not in msg
    assert "and 3 more" in msg

