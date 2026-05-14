"""Integration tests for ``lit export`` (M12.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault
from litman.exceptions import ExportError


def _seed_paper(vault: Path, paper_id: str, **fields: object) -> None:
    """Write a minimal metadata.yaml for the given paper id.

    Mirrors the helper in test_list.py — kept local to avoid coupling
    test files. List-typed values are serialised as YAML sequences,
    strings as scalars; everything else is stringified.
    """
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines: list[str] = [f"id: {paper_id}"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif isinstance(value, str):
            # Quote to be safe with colons / leading numbers.
            escaped = value.replace('"', '\\"')
            lines.append(f'{key}: "{escaped}"')
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    (paper_dir / "metadata.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Three-paper vault spanning two projects, three priorities, two years."""
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2023_Pandi_Cellfree",
        title="Cell-free biosynthesis paper",
        authors=["Pandi, Amir", "Smith, John"],
        year=2023,
        journal="Nature Communications",
        doi="10.1/cellfree",
        type="research",
        status="deep-read",
        priority="A",
        projects=["pepforge"],
        topics=["AMP"],
        methods=["cell-free"],
        data=[],
        related=[],
        contradicts=[],
        extends=[],
        **{"venue-type": "journal-article",
           "volume": "14",
           "pages": "1234-1240",
           "publisher": "Nature Portfolio",
           "issue": "",
           "booktitle": "",
           "arxiv-id": None,
           "github": None,
           "code-clones": []},
    )
    _seed_paper(
        v, "2024_Smith_BERT",
        title="A BERT review",
        authors=["Smith, John"],
        year=2024,
        journal="JMLR",
        doi="10.1/bert",
        type="review",
        status="skim",
        priority="B",
        projects=["pepcodec"],
        topics=["NLP"],
        methods=["BERT"],
        data=[],
        related=[],
        contradicts=[],
        extends=[],
        **{"venue-type": "journal-article",
           "volume": "",
           "pages": "",
           "publisher": "",
           "issue": "",
           "booktitle": "",
           "arxiv-id": None,
           "github": None,
           "code-clones": []},
    )
    _seed_paper(
        v, "2024_Doe_GNN",
        title="GNN survey",
        authors=["Doe, Jane"],
        year=2024,
        journal="",
        doi="10.1/gnn",
        type="research",
        status="inbox",
        priority="C",
        projects=["pepforge", "pepcodec"],
        topics=["GNN"],
        methods=["GNN"],
        data=[],
        related=[],
        contradicts=[],
        extends=[],
        **{"venue-type": "proceedings-article",
           "volume": "",
           "pages": "45-67",
           "publisher": "NeurIPS Foundation",
           "issue": "",
           "booktitle": "NeurIPS Proceedings",
           "arxiv-id": None,
           "github": None,
           "code-clones": []},
    )
    return v


def _invoke(vault: Path, *args: str, cwd: Path | None = None):
    runner = CliRunner()
    base = ["export", "--library", str(vault)]
    if cwd is not None:
        # Click's CliRunner doesn't expose chdir; use the
        # `with runner.isolated_filesystem(temp_dir=cwd)` only if cwd
        # is given. Otherwise rely on -o for explicit paths.
        with runner.isolated_filesystem(temp_dir=cwd):
            return runner.invoke(cli, [*base, *args])
    return runner.invoke(cli, [*base, *args])


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_no_scope_arg_fails(vault: Path, tmp_path: Path) -> None:
    """Neither --project nor --all -> ExportError."""
    result = _invoke(vault, "-o", str(tmp_path / "refs.bib"))
    assert result.exit_code != 0
    assert isinstance(result.exception, ExportError)
    assert "scope" in str(result.exception).lower() or \
           "--project" in str(result.exception)


def test_project_and_all_together_fail(vault: Path, tmp_path: Path) -> None:
    """--project and --all are mutually exclusive."""
    result = _invoke(vault, "--project", "pepforge", "--all",
                     "-o", str(tmp_path / "refs.bib"))
    assert result.exit_code != 0
    assert isinstance(result.exception, ExportError)
    assert "mutually exclusive" in str(result.exception)


# ---------------------------------------------------------------------------
# Output path + sentinel
# ---------------------------------------------------------------------------


def test_explicit_output_path_writes_sentinel(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "myrefs.bib"
    result = _invoke(vault, "--all", "-o", str(target))
    assert result.exit_code == 0, result.output
    assert target.is_file()
    content = target.read_text(encoding="utf-8")
    assert content.startswith("% Generated by litman ")
    # All three papers exported.
    assert "@article{2023_Pandi_Cellfree," in content
    assert "@article{2024_Smith_BERT," in content
    assert "@inproceedings{2024_Doe_GNN," in content


def test_export_all_writes_every_paper(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--all", "-o", str(target))
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    assert "Exported 3 papers" in result.output
    # Sorted by id (alphabetic), so 2023_Pandi precedes the 2024s.
    pos_pandi = content.index("2023_Pandi_Cellfree")
    pos_smith = content.index("2024_Smith_BERT")
    pos_doe = content.index("2024_Doe_GNN")
    assert pos_pandi < pos_doe < pos_smith


def test_export_project_filters(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--project", "pepforge", "-o", str(target))
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    assert "2023_Pandi_Cellfree" in content
    assert "2024_Doe_GNN" in content
    assert "2024_Smith_BERT" not in content
    assert "Exported 2 papers" in result.output


# ---------------------------------------------------------------------------
# Filters: priority / status / year (OR within field, AND across fields)
# ---------------------------------------------------------------------------


def test_priority_filter_or_within_field(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--all", "--priority", "A,B", "-o", str(target))
    assert result.exit_code == 0
    content = target.read_text(encoding="utf-8")
    assert "2023_Pandi_Cellfree" in content      # priority A
    assert "2024_Smith_BERT" in content          # priority B
    assert "2024_Doe_GNN" not in content         # priority C


def test_status_filter_single_value(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--all", "--status", "deep-read", "-o", str(target))
    assert result.exit_code == 0
    content = target.read_text(encoding="utf-8")
    assert "2023_Pandi_Cellfree" in content
    assert "2024_Smith_BERT" not in content
    assert "2024_Doe_GNN" not in content


def test_year_filter(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--all", "--year", "2024", "-o", str(target))
    assert result.exit_code == 0
    content = target.read_text(encoding="utf-8")
    assert "2023_Pandi_Cellfree" not in content
    assert "2024_Smith_BERT" in content
    assert "2024_Doe_GNN" in content


def test_filters_combined_and(vault: Path, tmp_path: Path) -> None:
    """--priority A AND --year 2024 -> 0 papers (Pandi is A but 2023)."""
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--all",
                     "--priority", "A",
                     "--year", "2024",
                     "-o", str(target))
    assert result.exit_code == 0
    content = target.read_text(encoding="utf-8")
    # Sentinel still written; no entries.
    assert "@article{" not in content
    assert "@inproceedings{" not in content
    assert "Exported 0 papers" in result.output


def test_project_plus_priority_and_combined(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--project", "pepforge",
                     "--priority", "A,C",
                     "-o", str(target))
    assert result.exit_code == 0
    content = target.read_text(encoding="utf-8")
    # pepforge contains Pandi (A) + Doe (C); both pass priority filter.
    assert "2023_Pandi_Cellfree" in content
    assert "2024_Doe_GNN" in content
    assert "2024_Smith_BERT" not in content


# ---------------------------------------------------------------------------
# Sentinel-guarded overwrite
# ---------------------------------------------------------------------------


def test_overwrite_existing_sentinel_file(vault: Path, tmp_path: Path) -> None:
    """Re-running export overwrites a previously-generated .bib."""
    target = tmp_path / "refs.bib"
    first = _invoke(vault, "--all", "-o", str(target))
    assert first.exit_code == 0, first.output
    first_content = target.read_text(encoding="utf-8")

    second = _invoke(vault, "--project", "pepforge", "-o", str(target))
    assert second.exit_code == 0, second.output
    second_content = target.read_text(encoding="utf-8")
    assert second_content != first_content
    assert "2024_Smith_BERT" not in second_content


def test_refuse_overwriting_hand_edited_file(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "refs.bib"
    target.write_text(
        "@article{handwritten,\n  title = {My own bibliography},\n}\n",
        encoding="utf-8",
    )
    result = _invoke(vault, "--all", "-o", str(target))
    assert result.exit_code != 0
    assert isinstance(result.exception, ExportError)
    assert "--force" in str(result.exception)
    # File untouched.
    assert "handwritten" in target.read_text(encoding="utf-8")


def test_force_overrides_sentinel_check(vault: Path, tmp_path: Path) -> None:
    target = tmp_path / "refs.bib"
    target.write_text("@article{handwritten,}\n", encoding="utf-8")
    result = _invoke(vault, "--all", "--force", "-o", str(target))
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    assert "handwritten" not in content
    assert content.startswith("% Generated by litman ")


# ---------------------------------------------------------------------------
# Field handling
# ---------------------------------------------------------------------------


def test_missing_venue_type_falls_back_to_misc(vault: Path, tmp_path: Path) -> None:
    """A paper without venue-type renders as @misc."""
    _seed_paper(
        vault, "2024_Legacy_X",
        title="Old paper without venue-type",
        authors=["Old, Author"],
        year=2024,
        journal="Some Journal",
        doi="10.1/legacy",
        type="research",
        status="inbox",
        priority="B",
        projects=["pepforge"],
        topics=[],
        methods=[],
        data=[],
        related=[],
        contradicts=[],
        extends=[],
    )
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--project", "pepforge", "-o", str(target))
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    assert "@misc{2024_Legacy_X," in content


def test_proceedings_paper_renders_with_booktitle(vault: Path, tmp_path: Path) -> None:
    """Doe_GNN has venue-type=proceedings-article + booktitle set."""
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--project", "pepforge", "-o", str(target))
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    assert "@inproceedings{2024_Doe_GNN," in content
    assert "booktitle = {NeurIPS Proceedings}," in content
    # Page range normalized to bibtex double-dash convention.
    assert "pages = {45--67}," in content


def test_sparse_paper_drops_empty_fields(vault: Path, tmp_path: Path) -> None:
    """A paper with empty volume/pages/publisher should not emit those keys."""
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--project", "pepcodec", "-o", str(target))
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    # 2024_Smith_BERT is in pepcodec, has no volume/pages/publisher.
    # Slice out its block using the trailing "\n}" line that closes the
    # entry — the inner title is wrapped in `{{...}}` so a naive
    # `}` search would land inside the title.
    block_start = content.index("@article{2024_Smith_BERT,")
    block_end = content.index("\n}", block_start)
    block = content[block_start:block_end]
    for absent_key in ("volume = ", "pages = ", "publisher = ", "number = "):
        assert absent_key not in block, f"unexpected {absent_key!r} in sparse block"
    # Required fields present.
    assert "title = " in block
    assert "author = " in block
    assert "journal = {JMLR}" in block


def test_empty_project_writes_sentinel_only(vault: Path, tmp_path: Path) -> None:
    """A project with no linked papers exports an empty (sentinel-only) .bib."""
    target = tmp_path / "refs.bib"
    result = _invoke(vault, "--project", "nonexistent", "-o", str(target))
    assert result.exit_code == 0, result.output
    content = target.read_text(encoding="utf-8")
    assert content.count("\n") <= 2  # sentinel + at most one trailing newline
    assert content.startswith("% Generated by litman ")
    assert "0 papers" in result.output


def test_default_output_is_cwd_refs_bib(vault: Path, tmp_path: Path) -> None:
    """Without -o the file lands at ./refs.bib inside the runner's cwd."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["export", "--all",
                                     "--library", str(vault)])
        assert result.exit_code == 0, result.output
        assert Path("refs.bib").is_file()
        content = Path("refs.bib").read_text(encoding="utf-8")
        assert content.startswith("% Generated by litman ")
