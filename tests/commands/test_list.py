"""Tests for `lit list`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.taxonomy import USER_DICTS
from litman.core.views import INDEX_PAPER_FIELDS


def _seed_paper(vault: Path, paper_id: str, **fields: object) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines: list[str] = [f"id: {paper_id}"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    (paper_dir / "metadata.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault with three papers spanning diverse field values."""
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2023_Pandi_Cellfree",
        year=2023, type="research", status="inbox", priority="B",
        topics=["AMP-prediction", "deep-learning"],
        methods=["transformer"],
        projects=["PepForge"],
        authors=["Pandi, Amir", "Smith, John"],
        title="Cell-free biosynthesis paper",
    )
    _seed_paper(
        v, "2024_Smith_BERT",
        year=2024, type="review", status="deep-read", priority="A",
        topics=["NLP", "transformer"],
        methods=["BERT-style"],
        projects=["PepCodec"],
        authors=["Smith, John", "Doe, Jane"],
        title="A review of BERT-based methods",
    )
    _seed_paper(
        v, "2024_Doe_GNN",
        year=2024, type="research", status="skim", priority="C",
        topics=["GNN"],
        methods=["GNN"],
        projects=["PepForge", "PepCodec"],
        authors=["Doe, Jane"],
        title="GNN survey",
    )
    return v


def _invoke(vault: Path, *args: str):
    runner = CliRunner()
    return runner.invoke(cli, ["list", "--library", str(vault), *args])


# ---------------------------------------------------------------------------
# Basic listing
# ---------------------------------------------------------------------------


def test_list_no_filter_shows_all(vault: Path) -> None:
    result = _invoke(vault)
    assert result.exit_code == 0
    assert "2023_Pandi_Cellfree" in result.output
    assert "2024_Smith_BERT" in result.output
    assert "2024_Doe_GNN" in result.output
    assert "3 of 3" in result.output


def test_list_empty_vault(tmp_path: Path) -> None:
    empty_vault = create_vault(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--library", str(empty_vault)])
    assert result.exit_code == 0
    assert "No papers in vault yet" in result.output


def test_list_no_match_with_papers_present(vault: Path) -> None:
    result = _invoke(vault, "--year", "1900")
    assert result.exit_code == 0
    assert "No papers match" in result.output
    assert "3 total" in result.output


# ---------------------------------------------------------------------------
# Equality filters
# ---------------------------------------------------------------------------


def test_list_year_filter(vault: Path) -> None:
    result = _invoke(vault, "--year", "2024")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "2024_Doe_GNN" in result.output
    assert "2023_Pandi_Cellfree" not in result.output
    assert "2 of 3" in result.output


def test_list_type_filter(vault: Path) -> None:
    result = _invoke(vault, "--type", "review")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "2024_Doe_GNN" not in result.output
    assert "1 of 3" in result.output


def test_list_status_filter(vault: Path) -> None:
    result = _invoke(vault, "--status", "deep-read")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "1 of 3" in result.output


def test_list_priority_filter(vault: Path) -> None:
    result = _invoke(vault, "--priority", "C")
    assert result.exit_code == 0
    assert "2024_Doe_GNN" in result.output
    assert "1 of 3" in result.output


# ---------------------------------------------------------------------------
# Multi-valued list-membership filters
# ---------------------------------------------------------------------------


def test_list_topic_filter(vault: Path) -> None:
    result = _invoke(vault, "--topic", "transformer")
    assert result.exit_code == 0
    # 2024_Smith_BERT has 'transformer' in topics
    assert "2024_Smith_BERT" in result.output
    # 2023_Pandi has 'deep-learning', not 'transformer'
    assert "2023_Pandi_Cellfree" not in result.output


def test_list_method_filter(vault: Path) -> None:
    result = _invoke(vault, "--method", "GNN")
    assert result.exit_code == 0
    assert "2024_Doe_GNN" in result.output
    assert "1 of 3" in result.output


def test_list_project_filter_returns_multiple(vault: Path) -> None:
    result = _invoke(vault, "--project", "PepForge")
    assert result.exit_code == 0
    assert "2023_Pandi_Cellfree" in result.output
    assert "2024_Doe_GNN" in result.output
    assert "2024_Smith_BERT" not in result.output
    assert "2 of 3" in result.output


# ---------------------------------------------------------------------------
# Author substring (case-insensitive)
# ---------------------------------------------------------------------------


def test_list_author_substring_case_insensitive(vault: Path) -> None:
    result = _invoke(vault, "--author", "doe")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output  # Doe is 2nd author
    assert "2024_Doe_GNN" in result.output     # Doe is sole
    assert "2023_Pandi_Cellfree" not in result.output


def test_list_author_substring_partial(vault: Path) -> None:
    result = _invoke(vault, "--author", "andi")  # part of "Pandi"
    assert result.exit_code == 0
    assert "2023_Pandi_Cellfree" in result.output
    assert "1 of 3" in result.output


# ---------------------------------------------------------------------------
# Combined AND filters
# ---------------------------------------------------------------------------


def test_list_combined_and_filter(vault: Path) -> None:
    result = _invoke(vault, "--year", "2024", "--type", "research")
    assert result.exit_code == 0
    # only 2024_Doe_GNN matches both
    assert "2024_Doe_GNN" in result.output
    assert "2024_Smith_BERT" not in result.output
    assert "1 of 3" in result.output


def test_list_combined_three_filters(vault: Path) -> None:
    result = _invoke(
        vault,
        "--project", "PepForge",
        "--year", "2024",
        "--method", "GNN",
    )
    assert result.exit_code == 0
    assert "2024_Doe_GNN" in result.output
    assert "1 of 3" in result.output


# ---------------------------------------------------------------------------
# --format json (M18 — agent bounded-retrieval exit)
# ---------------------------------------------------------------------------


def test_list_format_json_is_valid_array_with_index_schema(vault: Path) -> None:
    result = _invoke(vault, "--format", "json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 3
    for entry in payload:
        # Key set must be byte-identical to the INDEX.json single-paper
        # projection — no extra, no missing.
        assert set(entry.keys()) == set(INDEX_PAPER_FIELDS)
    ids = {entry["id"] for entry in payload}
    assert ids == {
        "2023_Pandi_Cellfree",
        "2024_Smith_BERT",
        "2024_Doe_GNN",
    }


def test_list_format_json_normalizes_absent_fields(tmp_path: Path) -> None:
    # AC1 (invariant #10 / ADR-007): the json projection must be
    # byte-identical to INDEX.json's single-paper projection, including
    # the absent-field normalization that is most likely to silently
    # drift — absent scalar -> JSON null, absent list -> [].
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2024_With_Doi",
        year=2024,
        doi="10.1000/withdoi",
        topics=["AMP-prediction"],
        methods=["transformer"],
        projects=["PepForge"],
        title="Paper that has a doi",
    )
    _seed_paper(
        v, "2024_No_Doi_No_Lists",
        year=2024,
        title="Paper missing doi and all list fields",
    )

    result = _invoke(v, "--format", "json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    by_id = {entry["id"]: entry for entry in payload}

    with_doi = by_id["2024_With_Doi"]
    assert with_doi["doi"] == "10.1000/withdoi"

    no_doi = by_id["2024_No_Doi_No_Lists"]
    # Absent scalar field -> JSON null (Python None after json.loads),
    # key present (not missing).
    assert "doi" in no_doi
    assert no_doi["doi"] is None
    # Absent list fields -> [] (not null, not missing).
    for list_field in ("topics", "projects", "methods"):
        assert no_doi[list_field] == []
        assert no_doi[list_field] is not None


def test_user_dicts_are_synced_into_index_projection() -> None:
    # Sync contract (M19, invariant #10 / ADR-007): every controlled
    # user-dict must surface in the per-paper INDEX.json / `lit list
    # --format json` projection, otherwise the agent can never retrieve
    # papers by that dimension. INDEX_PAPER_FIELDS (views.py) and
    # USER_DICTS (taxonomy.py) are two independently hardcoded tuples with
    # zero code linkage — this test is the only thing keeping them from
    # silently drifting apart.
    missing = set(USER_DICTS) - set(INDEX_PAPER_FIELDS)
    assert not missing, (
        f"user-dict(s) {sorted(missing)} are in USER_DICTS "
        f"(taxonomy.py) but missing from INDEX_PAPER_FIELDS (views.py). "
        f"A new user-dict must EITHER be synced into INDEX_PAPER_FIELDS "
        f"(so the agent can retrieve papers by it) OR have its exclusion "
        f"explicitly recorded under invariant #10. 不允许靠人记得 — "
        f"this contract is enforced here, not in anyone's memory."
    )


def test_list_format_json_passes_through_data_values(tmp_path: Path) -> None:
    # M19 positive coverage: a paper WITH `data` values carries the `data`
    # key with its values passed through, identical to topics/methods.
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2024_With_Data",
        year=2024,
        data=["GDP-2", "SignalP-6.0"],
        topics=["AMP-prediction"],
        methods=["transformer"],
        title="Paper that declares datasets",
    )
    result = _invoke(v, "--format", "json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    entry = next(e for e in payload if e["id"] == "2024_With_Data")
    assert entry["data"] == ["GDP-2", "SignalP-6.0"]


def test_list_format_json_absent_data_normalizes_to_empty_list(
    tmp_path: Path,
) -> None:
    # M19 AC2: a paper WITHOUT `data` must project `data` to [] — not
    # null, not missing — symmetric with topics/projects/methods.
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2024_No_Data",
        year=2024,
        title="Paper missing the data field",
    )
    result = _invoke(v, "--format", "json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    entry = next(e for e in payload if e["id"] == "2024_No_Data")
    assert "data" in entry
    assert entry["data"] == []
    assert entry["data"] is not None


def test_list_format_json_respects_filters(vault: Path) -> None:
    result = _invoke(vault, "--topic", "transformer", "--format", "json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    ids = {entry["id"] for entry in payload}
    # Only 2024_Smith_BERT has 'transformer' in topics.
    assert ids == {"2024_Smith_BERT"}
    assert "transformer" in payload[0]["topics"]


def test_list_format_json_empty_hit_is_empty_array(vault: Path) -> None:
    result = _invoke(vault, "--topic", "zzz", "--format", "json")
    assert result.exit_code == 0
    assert result.output.strip() == "[]"
    assert json.loads(result.output) == []
    # No human-facing "No papers match" text leaks into json mode.
    assert "No papers" not in result.output


def test_list_format_json_is_loads_parseable_no_markup(vault: Path) -> None:
    result = _invoke(vault, "--format", "json")
    assert result.exit_code == 0
    # Directly parseable: no Rich markup, no stray stderr mixed in
    # (CliRunner mixes stderr into output by default, so a clean parse
    # also proves nothing was written to stderr in the json path).
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert "[dim]" not in result.output
    assert "\x1b[" not in result.output  # no ANSI escape sequences


def test_list_default_still_renders_rich_table(vault: Path) -> None:
    # Regression: omitting --format keeps the original human table.
    result = _invoke(vault)
    assert result.exit_code == 0
    assert "3 of 3" in result.output
    assert "Papers (3 of 3)" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_list_displays_dash_for_none_priority_and_type(tmp_path: Path) -> None:
    # M29: lit add writes priority/type as None by default; the table must
    # render them as "-", never the literal "None".
    v = create_vault(tmp_path)
    _seed_paper(
        v,
        "2024_Solid_Paper",
        year=2024,
        type="research",
        status="inbox",
        priority="B",
        title="Has both fields set",
    )
    _seed_paper(
        v,
        "2024_Unset_Paper",
        year=2024,
        type=None,
        status="inbox",
        priority=None,
        title="Has neither priority nor type",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--library", str(v)])
    assert result.exit_code == 0
    assert "2024_Unset_Paper" in result.output
    # The literal string "None" must not appear (would mean str(None) leaked).
    assert "None" not in result.output
