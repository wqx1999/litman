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


# ---------------------------------------------------------------------------
# OR-within-field (M31)
# ---------------------------------------------------------------------------


def test_list_status_or_within_field(vault: Path) -> None:
    result = _invoke(vault, "--status", "deep-read,skim")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output  # deep-read
    assert "2024_Doe_GNN" in result.output     # skim
    assert "2023_Pandi_Cellfree" not in result.output  # inbox
    assert "2 of 3" in result.output


def test_list_topic_or_within_field(vault: Path) -> None:
    # transformer hits 2024_Smith_BERT; GNN hits 2024_Doe_GNN.
    result = _invoke(vault, "--topic", "transformer,GNN")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "2024_Doe_GNN" in result.output
    assert "2023_Pandi_Cellfree" not in result.output
    assert "2 of 3" in result.output


def test_list_year_or_within_field(vault: Path) -> None:
    result = _invoke(vault, "--year", "2023,2024")
    assert result.exit_code == 0
    assert "3 of 3" in result.output


def test_list_author_or_within_field(vault: Path) -> None:
    # "pandi" hits only Pandi; "jane" (lowercase) hits the two Doe papers.
    result = _invoke(vault, "--author", "pandi,jane")
    assert result.exit_code == 0
    assert "2023_Pandi_Cellfree" in result.output
    assert "2024_Smith_BERT" in result.output  # Doe, Jane is 2nd author
    assert "2024_Doe_GNN" in result.output
    assert "3 of 3" in result.output


# ---------------------------------------------------------------------------
# Time queries: --read-since / --added-since (M31)
# ---------------------------------------------------------------------------


@pytest.fixture
def time_vault(tmp_path: Path) -> Path:
    """A vault whose papers carry real read-date / created-at values.

    Seeded on disk so list_papers' ruamel safe-loader produces genuine
    datetime.date / datetime.datetime objects (the M25 false-green trap was
    hand-building dicts that skipped that path).
    """
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2024_Early",
        year=2024, title="Read in April, added in March",
        **{"read-date": "2026-04-15",
           "created-at": "2026-03-10T09:00:00+08:00"},
    )
    _seed_paper(
        v, "2024_Boundary",
        year=2024, title="Read exactly on the boundary date",
        **{"read-date": "2026-05-01",
           "created-at": "2026-05-01T09:00:00+08:00"},
    )
    _seed_paper(
        v, "2024_Late",
        year=2024, title="Read in May, added in May",
        **{"read-date": "2026-05-20",
           "created-at": "2026-05-18T09:00:00+08:00"},
    )
    _seed_paper(
        v, "2024_Unread",
        year=2024, title="Never read, no read-date",
        **{"created-at": "2026-05-25T09:00:00+08:00"},
    )
    return v


def test_read_since_filters_by_read_date_with_boundary(time_vault: Path) -> None:
    result = _invoke(time_vault, "--read-since", "2026-05-01")
    assert result.exit_code == 0
    # Boundary (== date) included via >=, Late included, Early excluded,
    # Unread (no read-date) excluded.
    assert "2024_Boundary" in result.output
    assert "2024_Late" in result.output
    assert "2024_Early" not in result.output
    assert "2024_Unread" not in result.output
    assert "2 of 4" in result.output


def test_added_since_filters_by_created_at(time_vault: Path) -> None:
    result = _invoke(time_vault, "--added-since", "2026-05-01")
    assert result.exit_code == 0
    # created-at boundary 2026-05-01 included, Late (05-18) + Unread (05-25)
    # included, Early (03-10) excluded.
    assert "2024_Boundary" in result.output
    assert "2024_Late" in result.output
    assert "2024_Unread" in result.output
    assert "2024_Early" not in result.output
    assert "3 of 4" in result.output


def test_read_since_and_added_since_read_only_own_field(time_vault: Path) -> None:
    # invariant #11: --read-since must NOT see created-at and vice versa.
    # Early was added 2026-03-10 (before) but read 2026-04-15; with
    # --read-since 2026-04-01 it is included, proving read-since ignores
    # created-at.
    result = _invoke(time_vault, "--read-since", "2026-04-01")
    assert result.exit_code == 0
    assert "2024_Early" in result.output
    assert "3 of 4" in result.output  # Early + Boundary + Late, not Unread


def test_read_since_does_not_raise_on_date_objects(time_vault: Path) -> None:
    # Regression for the M25 trap: read-date safe-loads to a datetime.date;
    # the filter must compare cleanly, not crash.
    result = _invoke(time_vault, "--read-since", "2026-01-01", "--format", "json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    ids = {e["id"] for e in payload}
    assert ids == {"2024_Early", "2024_Boundary", "2024_Late"}


def test_read_since_excludes_bad_and_missing_values(tmp_path: Path) -> None:
    v = create_vault(tmp_path)
    _seed_paper(
        v, "2024_Good", year=2024, title="Valid read-date",
        **{"read-date": "2026-05-10"},
    )
    _seed_paper(
        v, "2024_Bad", year=2024, title="Unparseable read-date",
        **{"read-date": "not-a-date"},
    )
    _seed_paper(
        v, "2024_Missing", year=2024, title="No read-date at all",
    )
    result = _invoke(v, "--read-since", "2026-01-01")
    assert result.exit_code == 0  # bad/missing excluded, never raises
    assert "2024_Good" in result.output
    assert "2024_Bad" not in result.output
    assert "2024_Missing" not in result.output
    assert "1 of 3" in result.output


def test_read_since_rejects_non_zero_padded_date(time_vault: Path) -> None:
    result = _invoke(time_vault, "--read-since", "2026-5-1")
    assert result.exit_code != 0
    assert "2026-5-1" in result.output or "YYYY-MM-DD" in result.output


def test_added_since_rejects_iso_week_form(time_vault: Path) -> None:
    result = _invoke(time_vault, "--added-since", "2026-W22-1")
    assert result.exit_code != 0


def test_read_since_coexists_with_unread(time_vault: Path) -> None:
    # Both touch read-date but with independent semantics; combining them is
    # a natural AND that yields the empty set (no read-date can be both
    # >= a date AND empty), and must not error.
    result = _invoke(time_vault, "--read-since", "2026-01-01", "--unread")
    assert result.exit_code == 0
    assert "No papers match" in result.output


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


# ---------------------------------------------------------------------------
# --title (M33)
# ---------------------------------------------------------------------------


def test_list_title_substring(vault: Path) -> None:
    result = _invoke(vault, "--title", "BERT")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "2023_Pandi_Cellfree" not in result.output
    assert "1 of 3" in result.output


def test_list_title_case_insensitive(vault: Path) -> None:
    result = _invoke(vault, "--title", "bert")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output


def test_list_title_or_within_field(vault: Path) -> None:
    result = _invoke(vault, "--title", "bert,gnn")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "2024_Doe_GNN" in result.output
    assert "2 of 3" in result.output


def test_list_title_combines_with_other_filters(vault: Path) -> None:
    # AND across flags: title contains "review" AND year 2024.
    result = _invoke(vault, "--title", "review", "--year", "2024")
    assert result.exit_code == 0
    assert "2024_Smith_BERT" in result.output
    assert "1 of 3" in result.output


def test_list_title_json(vault: Path) -> None:
    result = _invoke(vault, "--title", "gnn", "--format", "json")
    payload = json.loads(result.output)
    assert [p["id"] for p in payload] == ["2024_Doe_GNN"]


# ---------------------------------------------------------------------------
# --limit (M33)
# ---------------------------------------------------------------------------


def test_list_limit_slices_table(vault: Path) -> None:
    result = _invoke(vault, "--limit", "1")
    assert result.exit_code == 0
    # id-asc order: 2023_Pandi_Cellfree is first.
    assert "2023_Pandi_Cellfree" in result.output
    assert "2024_Smith_BERT" not in result.output


def test_list_limit_slices_json(vault: Path) -> None:
    result = _invoke(vault, "--limit", "2", "--format", "json")
    payload = json.loads(result.output)
    assert len(payload) == 2
    assert [p["id"] for p in payload] == [
        "2023_Pandi_Cellfree",
        "2024_Doe_GNN",
    ]


def test_list_limit_with_sort_recent(vault: Path) -> None:
    """--limit applies after the recency sort, honored by json output."""
    result = _invoke(vault, "--sort", "recent", "--limit", "1", "--format", "json")
    payload = json.loads(result.output)
    assert len(payload) == 1


def test_list_limit_larger_than_count(vault: Path) -> None:
    result = _invoke(vault, "--limit", "99", "--format", "json")
    payload = json.loads(result.output)
    assert len(payload) == 3


def test_list_limit_table_title_reports_matched_count(vault: Path) -> None:
    # Regression: --limit truncates the displayed rows but the title's count
    # must stay the TRUE match count, not the limited/shown count masquerading
    # as it. All three papers are research/review across 2024+2023; --year
    # 2024 matches two of them, --limit 1 shows one — the title must say the
    # matched count (2), distinguished as a limit.
    result = _invoke(vault, "--year", "2024", "--limit", "1")
    assert result.exit_code == 0
    # True match count surfaces; the shown "1" never poses as the match count.
    assert "of 2" in result.output
    assert "Papers (1 of 3)" not in result.output


def test_list_limit_json_unaffected_by_title_fix(vault: Path) -> None:
    # The title fix must not change json behavior: --limit 1 still returns
    # exactly one item.
    result = _invoke(vault, "--year", "2024", "--limit", "1", "--format", "json")
    payload = json.loads(result.output)
    assert len(payload) == 1


# ---------------------------------------------------------------------------
# D7: interactive-TTY row cap for the default (id) sort
# ---------------------------------------------------------------------------


def _seed_n_papers(vault: Path, n: int) -> None:
    """Seed n papers with zero-padded, id-sortable names 2000_A_0001..000n."""
    for i in range(1, n + 1):
        _seed_paper(
            vault, f"2000_A_{i:04d}",
            year=2000, type="research", status="inbox", priority="B",
            title=f"Paper number {i}",
        )


def test_list_tty_cap_default_sort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (id) sort on an interactive TTY caps the table at 30 rows, with a
    title + caption pointing at --limit / --format json for the rest."""
    v = create_vault(tmp_path)
    _seed_n_papers(v, 35)
    monkeypatch.setattr("litman.commands.list._stdout_isatty", lambda: True)
    result = _invoke(v)
    assert result.exit_code == 0
    assert "showing 30 of 35" in result.output
    assert "--format json" in result.output  # caption hint
    # id-asc order: the first paper shows; rows 31-35 are cut.
    assert "2000_A_0001" in result.output
    assert "2000_A_0035" not in result.output


def test_list_no_tty_cap_when_piped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red line: non-TTY (piped / agent / redirected) output is never capped."""
    v = create_vault(tmp_path)
    _seed_n_papers(v, 35)
    monkeypatch.setattr("litman.commands.list._stdout_isatty", lambda: False)
    result = _invoke(v)
    assert result.exit_code == 0
    assert "showing 30 of 35" not in result.output
    assert "Papers (35 of 35)" in result.output
    assert "2000_A_0035" in result.output


def test_list_json_never_capped_on_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red line: --format json returns the full list even on a TTY."""
    v = create_vault(tmp_path)
    _seed_n_papers(v, 35)
    monkeypatch.setattr("litman.commands.list._stdout_isatty", lambda: True)
    result = _invoke(v, "--format", "json")
    payload = json.loads(result.output)
    assert len(payload) == 35


def test_list_explicit_limit_overrides_tty_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red line: an explicit --limit is the user's own bound and disables the
    30-row TTY cap (--limit 40 shows all 35)."""
    v = create_vault(tmp_path)
    _seed_n_papers(v, 35)
    monkeypatch.setattr("litman.commands.list._stdout_isatty", lambda: True)
    result = _invoke(v, "--limit", "40")
    assert result.exit_code == 0
    assert "showing 30 of 35" not in result.output
    assert "2000_A_0035" in result.output


# ---------------------------------------------------------------------------
# INDEX fast path: identical output, no vault scan, honest fallbacks
# ---------------------------------------------------------------------------


def _reconcile(v: Path) -> None:
    from litman.core.correctors import reconcile_derived

    reconcile_derived(v, project_refs=False)


def test_list_output_is_byte_identical_between_index_and_scan(
    vault: Path,
) -> None:
    """The red line: the fast path may change the cost, never the bytes.
    Same vault, --format json and the table, INDEX present vs deleted."""
    _reconcile(vault)
    fast_json = _invoke(vault, "--format", "json")
    fast_table = _invoke(vault)
    assert fast_json.exit_code == 0

    (vault / "INDEX.json").unlink()
    scan_json = _invoke(vault, "--format", "json")
    scan_table = _invoke(vault)

    assert fast_json.output == scan_json.output
    assert fast_table.output == scan_table.output
    assert len(json.loads(fast_json.output)) == 3


def test_list_fast_path_never_opens_metadata(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a fresh INDEX, both output formats are served without a single
    metadata.yaml read (the documented agent contract, now real)."""
    _reconcile(vault)

    def _boom(v: Path) -> list:
        raise AssertionError("full vault scan ran on the fast path")

    monkeypatch.setattr("litman.commands.list.list_papers", _boom)
    result = _invoke(vault, "--format", "json")
    assert result.exit_code == 0, result.output
    assert {p["id"] for p in json.loads(result.output)} == {
        "2023_Pandi_Cellfree", "2024_Smith_BERT", "2024_Doe_GNN",
    }
    filtered = _invoke(vault, "--status", "deep-read", "--format", "json")
    assert [p["id"] for p in json.loads(filtered.output)] == [
        "2024_Smith_BERT"
    ]
    table = _invoke(vault)
    assert table.exit_code == 0


def test_list_stale_index_falls_back_silently_and_sees_the_new_paper(
    vault: Path,
) -> None:
    """A paper seeded behind INDEX's back fails the freshness probe: the
    scan serves the truth (all 4 papers) and the JSON stays clean."""
    _reconcile(vault)
    _seed_paper(vault, "2025_New_Late", title="Late", authors=["N."])
    result = _invoke(vault, "--format", "json")
    assert result.exit_code == 0
    assert len(json.loads(result.output)) == 4


def test_list_added_since_reads_created_at_from_disk(vault: Path) -> None:
    """--added-since needs created-at, which the INDEX projection does not
    carry — the query must take the scan even when INDEX is fresh. (If it
    were wrongly served from INDEX, every paper would be excluded.)"""
    _seed_paper(
        vault, "2026_Fresh_Addition",
        title="Fresh", authors=["F."],
        **{"created-at": "'2026-06-01T10:00:00+02:00'"},
    )
    _reconcile(vault)
    kept = _invoke(vault, "--added-since", "2026-01-01", "--format", "json")
    assert [p["id"] for p in json.loads(kept.output)] == [
        "2026_Fresh_Addition"
    ]
    none = _invoke(vault, "--added-since", "2027-01-01", "--format", "json")
    assert json.loads(none.output) == []


def test_list_sort_recent_reads_updated_at_from_disk(vault: Path) -> None:
    """--sort recent ranks on updated-at (not in the projection): with a
    fresh INDEX the ranking must still reflect per-paper updated-at."""
    for paper_id, stamp in [
        ("2023_Pandi_Cellfree", "2026-01-01T10:00:00+02:00"),
        ("2024_Smith_BERT", "2026-03-01T10:00:00+02:00"),
        ("2024_Doe_GNN", "2026-02-01T10:00:00+02:00"),
    ]:
        meta = vault / "papers" / paper_id / "metadata.yaml"
        meta.write_text(
            meta.read_text(encoding="utf-8")
            + f"updated-at: '{stamp}'\n",
            encoding="utf-8",
        )
    _reconcile(vault)
    result = _invoke(vault, "--sort", "recent", "--format", "json")
    assert [p["id"] for p in json.loads(result.output)] == [
        "2024_Smith_BERT", "2024_Doe_GNN", "2023_Pandi_Cellfree",
    ]
