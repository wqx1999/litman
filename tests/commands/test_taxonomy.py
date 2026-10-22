"""Tests for `lit taxonomy` and the underlying TAXONOMY.md helpers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.locking import lock_truth_file, unlock_truth_file
from litman.core.taxonomy import (
    ALL_DICTS,
    FIXED_DICTS,
    USER_DICTS,
    add_taxonomy_values,
    parse_taxonomy,
    read_taxonomy_text,
    update_user_dict_section,
)
from litman.exceptions import (
    TaxonomyError,
    TaxonomySimilarError,
    UnreadableTaxonomyError,
)
from litman.core.portable_link import is_portable_link

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    """Materialise a paper with full M2.0-schema metadata (audit + code-clones).

    Lets taxonomy ripple operations re-roundtrip the file cleanly.
    """
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": fields.get("title", paper_id),
        "authors": fields.get("authors", ["Doe, Jane"]),
        "year": fields.get("year", 2024),
        "journal": fields.get("journal", "Test J."),
        "doi": fields.get("doi", f"10.0/{paper_id}"),
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": fields.get("projects", []),
        "topics": fields.get("topics", []),
        "methods": fields.get("methods", []),
        "data": fields.get("data", []),
        "type": fields.get("type", "research"),
        "status": fields.get("status", "inbox"),
        "read-date": None,
        "last-revisited": None,
        "related": [],
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _read_meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


def _read_taxonomy(vault: Path) -> dict[str, list[str]]:
    return parse_taxonomy((vault / "TAXONOMY.md").read_text())


def _vault_snapshot(vault: Path) -> dict[str, bytes]:
    """Byte-exact map of every vault file (skips the staging scratch dir)."""
    snap: dict[str, bytes] = {}
    for path in sorted(vault.rglob("*")):
        if path.is_dir() or ".litman-staging" in path.parts:
            continue
        snap[str(path.relative_to(vault))] = path.read_bytes()
    return snap


# ===========================================================================
# core/taxonomy.py — pure helpers
# ===========================================================================


def test_parse_seed_yields_all_known_dicts(vault: Path) -> None:
    parsed = _read_taxonomy(vault)
    # All known dicts present as keys.
    assert set(parsed.keys()) == set(ALL_DICTS)
    # User dicts start empty.
    for d in USER_DICTS:
        assert parsed[d] == []
    # Fixed enums populated from the seed.
    assert "research" in parsed["type"]
    assert "deep-read" in parsed["status"]
    # `priority` is no longer a dict at all (ADR-025) — a fresh seed does not
    # write the section and the parser does not know the name.
    assert "priority" not in parsed


# ---------------------------------------------------------------------------
# A vault seeded before ADR-025 keeps its `## priority` section forever.
# Retiring the dict must be tolerated, not "cleaned up": rewriting every
# user's TAXONOMY.md would be a second migration carrying its own risk, and
# the leftover section is inert. This pins all three behaviors that make it
# inert, so a later refactor cannot quietly turn it into a finding.
# ---------------------------------------------------------------------------

_LEGACY_PRIORITY_SECTION = """
## priority (fixed enum, not extensible)

- A
- B
- C
"""


def test_legacy_priority_section_is_parsed_as_no_dict_at_all(
    vault: Path,
) -> None:
    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    parsed = parse_taxonomy(text + _LEGACY_PRIORITY_SECTION)

    # Not a known dict, and — the part that matters — its `- A/B/C` items are
    # not swept into whichever section happened to precede it.
    assert set(parsed) == set(ALL_DICTS)
    assert "priority" not in parsed
    assert all("A" not in values for values in parsed.values())


def test_legacy_priority_section_is_not_taxonomy_drift(vault: Path) -> None:
    from litman.core.checks import check_taxonomy_drift
    from litman.core.document import list_papers
    from litman.core.locking import lock_truth_file, unlock_truth_file

    # TAXONOMY.md is a chmod-locked TRUTH file (read-only on every platform,
    # not just Windows) — a bare write_text raises PermissionError.
    path = vault / "TAXONOMY.md"
    unlock_truth_file(path)
    path.write_text(
        path.read_text(encoding="utf-8") + _LEGACY_PRIORITY_SECTION,
        encoding="utf-8",
    )
    lock_truth_file(path)
    assert check_taxonomy_drift(vault, list_papers(vault)) == []

    # Control: the check IS live on this vault — an unregistered topic fires.
    _write_paper(vault, "2024_Foo_Bar", topics=["never-registered"])
    issues = check_taxonomy_drift(vault, list_papers(vault))
    assert issues, "control failed: the drift check saw nothing at all"
    assert all("priority" not in i.message for i in issues)


def test_legacy_priority_section_survives_a_user_dict_rewrite(
    vault: Path,
) -> None:
    """`update_user_dict_section` is surgical: unknown sections are copied
    verbatim, so editing topics never silently deletes the leftover."""
    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    with_legacy = text + _LEGACY_PRIORITY_SECTION
    rewritten = update_user_dict_section(with_legacy, "topics", ["alpha"])

    assert _LEGACY_PRIORITY_SECTION.strip() in rewritten
    assert parse_taxonomy(rewritten)["topics"] == ["alpha"]


def test_update_user_dict_replaces_only_target_section(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text()
    new_text = update_user_dict_section(text, "topics", ["alpha", "beta"])
    parsed = parse_taxonomy(new_text)
    assert parsed["topics"] == ["alpha", "beta"]
    # Other user dicts unchanged.
    assert parsed["projects"] == []
    assert parsed["methods"] == []
    assert parsed["data"] == []
    # Fixed enums preserved.
    assert "research" in parsed["type"]
    assert "deep-read" in parsed["status"]


def test_update_user_dict_emits_sorted_values(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text()
    new_text = update_user_dict_section(text, "topics", ["zeta", "alpha", "mu"])
    # Body must contain values in sorted order.
    body = new_text.split("## topics")[1].split("## methods")[0]
    a = body.index("- alpha")
    m = body.index("- mu")
    z = body.index("- zeta")
    assert a < m < z


def test_update_user_dict_empty_marker(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text()
    populated = update_user_dict_section(text, "topics", ["alpha"])
    cleared = update_user_dict_section(populated, "topics", [])
    parsed = parse_taxonomy(cleared)
    assert parsed["topics"] == []
    assert "(empty)" in cleared.split("## topics")[1].split("## methods")[0]


def test_update_user_dict_rejects_fixed_enum(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text()
    with pytest.raises(ValueError):
        update_user_dict_section(text, "type", ["evil"])


# ===========================================================================
# CLI: list
# ===========================================================================


def test_taxonomy_list_all(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["taxonomy", "list", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "projects" in result.output
    assert "topics" in result.output
    assert "research" in result.output  # fixed-enum value


def test_taxonomy_list_single_user_dict(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "list", "topics", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "user-extensible" in result.output
    assert "(empty)" in result.output


def test_taxonomy_list_fixed_enum(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "list", "status", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "fixed enum" in result.output
    assert "deep-read" in result.output


def test_taxonomy_list_unknown(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "list", "ghosts", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)


# ===========================================================================
# CLI: add
# ===========================================================================


def test_taxonomy_add_single(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "peptide", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert _read_taxonomy(vault)["topics"] == ["peptide"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_taxonomy_writes_penetrate_locked_taxonomy(vault: Path) -> None:
    """add / rename / merge succeed on a locked TAXONOMY.md and re-lock it (AC#2)."""
    tax = vault / "TAXONOMY.md"
    # create_vault already locked it.
    assert not os.access(tax, os.W_OK)
    runner = CliRunner()

    r = runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "alpha", "beta", "--library", str(vault)],
    )
    assert r.exit_code == 0, r.output
    assert _read_taxonomy(vault)["topics"] == ["alpha", "beta"]
    assert not os.access(tax, os.W_OK)

    r = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "alpha", "gamma",
         "--library", str(vault)],
    )
    assert r.exit_code == 0, r.output
    assert "gamma" in _read_taxonomy(vault)["topics"]
    assert not os.access(tax, os.W_OK)

    r = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "gamma", "beta",
         "--into", "delta", "--yes", "--library", str(vault)],
    )
    assert r.exit_code == 0, r.output
    assert "delta" in _read_taxonomy(vault)["topics"]
    assert not os.access(tax, os.W_OK)


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
def test_taxonomy_rm_penetrates_locked_taxonomy(vault: Path) -> None:
    """rm of an unreferenced value succeeds on a locked TAXONOMY.md and re-locks it (AC#2)."""
    tax = vault / "TAXONOMY.md"
    # create_vault already locked it.
    assert not os.access(tax, os.W_OK)
    runner = CliRunner()

    runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "alpha", "beta", "--library", str(vault)],
    )
    assert not os.access(tax, os.W_OK)

    r = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "alpha", "--library", str(vault)]
    )
    assert r.exit_code == 0, r.output
    assert _read_taxonomy(vault)["topics"] == ["beta"]
    assert not os.access(tax, os.W_OK)


def test_taxonomy_add_multiple(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "peptide", "AMP", "diffusion",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    # Sorted in TAXONOMY.md
    assert _read_taxonomy(vault)["topics"] == ["AMP", "diffusion", "peptide"]


def test_taxonomy_add_dedupes_with_existing(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide", "--library", str(vault)])
    # Re-add same value
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "topics", "peptide", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "No-op" in result.output
    assert _read_taxonomy(vault)["topics"] == ["peptide"]


def test_taxonomy_add_rejects_fixed_enum(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "status", "rejected", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "fixed-enum" in str(result.exception)


def test_taxonomy_add_rejects_unknown_dict(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "add", "unknown", "x", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)


def test_taxonomy_add_does_not_touch_papers(vault: Path) -> None:
    """Registering a value doesn't ripple into existing metadata.yaml."""
    _write_paper(vault, "2024_Foo_X", topics=["existing"])
    before = _read_meta(vault, "2024_Foo_X")
    runner = CliRunner()
    runner.invoke(
        cli, ["taxonomy", "add", "topics", "newtopic", "--library", str(vault)]
    )
    after = _read_meta(vault, "2024_Foo_X")
    # topics list unchanged on the paper itself.
    assert after["topics"] == before["topics"]
    assert after["updated-at"] == before["updated-at"]


# ===========================================================================
# CLI: rename
# ===========================================================================


def test_taxonomy_rename_ripples_to_papers(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["peptide", "diffusion"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    _write_paper(vault, "2024_C", topics=["other"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide", "diffusion", "other",
                        "--library", str(vault)])

    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "peptide", "AMP-design",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated 2 paper" in result.output

    parsed = _read_taxonomy(vault)
    assert "peptide" not in parsed["topics"]
    assert "AMP-design" in parsed["topics"]

    # Affected papers updated.
    a = _read_meta(vault, "2024_A")
    b = _read_meta(vault, "2024_B")
    c = _read_meta(vault, "2024_C")
    assert "peptide" not in a["topics"]
    assert "AMP-design" in a["topics"]
    assert b["topics"] == ["AMP-design"]
    # Untouched paper unchanged.
    assert c["topics"] == ["other"]
    assert c["updated-at"] == "2026-04-28T10:00:00+02:00"


def test_taxonomy_rename_refreshes_index_and_views(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert is_portable_link(vault / "views/by-topic/peptide/2024_A")

    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "peptide", "AMP",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads((vault / "INDEX.json").read_text())
    p = payload["papers"][0]
    assert p["topics"] == ["AMP"]
    # Old view bucket gone, new one present.
    assert not (vault / "views/by-topic/peptide").exists()
    assert is_portable_link(vault / "views/by-topic/AMP/2024_A")


def test_taxonomy_rename_missing_old(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "ghost", "spirit",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "not registered" in str(result.exception)


def test_taxonomy_rename_collision_redirects_to_merge(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha", "beta",
                        "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "alpha", "beta",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert "merge" in str(result.exception)


def test_taxonomy_rename_identity_rejected(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha",
                        "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "alpha", "alpha",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)


# ===========================================================================
# CLI: merge
# ===========================================================================


def test_taxonomy_merge_into_new_value(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["AMP", "peptide"])
    _write_paper(vault, "2024_B", topics=["AMP"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "AMP", "peptide",
                        "--library", str(vault)])

    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "AMP", "peptide",
         "--into", "AMP-design", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated 2 paper" in result.output

    parsed = _read_taxonomy(vault)
    assert "AMP" not in parsed["topics"]
    assert "peptide" not in parsed["topics"]
    assert "AMP-design" in parsed["topics"]

    a = _read_meta(vault, "2024_A")
    # Both AMP and peptide collapsed into a single AMP-design.
    assert a["topics"] == ["AMP-design"]
    b = _read_meta(vault, "2024_B")
    assert b["topics"] == ["AMP-design"]


def test_taxonomy_merge_into_existing_source(vault: Path) -> None:
    """`merge a b --into a` keeps a, drops b."""
    _write_paper(vault, "2024_A", topics=["alpha", "beta"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha", "beta",
                        "--library", str(vault)])

    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "alpha", "beta",
         "--into", "alpha", "--yes", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    parsed = _read_taxonomy(vault)
    assert parsed["topics"] == ["alpha"]
    a = _read_meta(vault, "2024_A")
    assert a["topics"] == ["alpha"]


def test_taxonomy_merge_missing_source(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha",
                        "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "alpha", "ghost",
         "--into", "merged", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "Sources not registered" in str(result.exception)


def test_taxonomy_merge_all_sources_equal_dest(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha",
                        "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "alpha",
         "--into", "alpha", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert "nothing to merge" in str(result.exception)


# ===========================================================================
# CLI: rm
# ===========================================================================


def test_taxonomy_rm_unreferenced_value(vault: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "alpha", "beta",
                        "--library", str(vault)])
    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "alpha", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_taxonomy(vault)["topics"] == ["beta"]


def test_taxonomy_rm_referenced_non_tty_aborts(vault: Path) -> None:
    """M15: rm of a referenced value in a non-tty without --yes aborts.

    Replaces the pre-M15 "refuse if referenced" contract. The value is
    now removable via cascade-with-confirm, but a non-interactive run
    that supplies no confirmation must NOT silently destroy data.
    """
    _write_paper(vault, "2024_A", topics=["peptide"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])

    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "peptide", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert "Non-interactive environment" in result.output
    # Taxonomy + papers NOT modified.
    assert _read_taxonomy(vault)["topics"] == ["peptide"]
    assert _read_meta(vault, "2024_A")["topics"] == ["peptide"]


def test_taxonomy_rm_unknown_value(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "ghost", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert "not registered" in str(result.exception)


def test_taxonomy_rm_fixed_enum(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "rm", "status", "inbox", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)


# ===========================================================================
# CLI: smoke
# ===========================================================================


def test_taxonomy_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["taxonomy", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "add", "rename", "merge", "rm"):
        assert sub in result.output


def test_taxonomy_atomicity_metadata_and_index_match(vault: Path) -> None:
    """After rename, every paper's metadata and INDEX.json topics agree."""
    _write_paper(vault, "2024_A", topics=["peptide"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])
    runner.invoke(cli, ["taxonomy", "rename", "topics", "peptide", "AMP",
                        "--library", str(vault)])

    payload = json.loads((vault / "INDEX.json").read_text())
    for p in payload["papers"]:
        meta = _read_meta(vault, p["id"])
        assert meta["topics"] == p["topics"]
        assert "peptide" not in meta["topics"]


# ===========================================================================
# M15: projects hard-deprecation + cascade-with-confirm rm/merge
# ===========================================================================


def test_taxonomy_add_projects_hard_error(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "add", "projects", "pepforge",
              "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    msg = str(result.exception)
    assert "lit project add" in msg
    assert "path binding" in msg


def test_taxonomy_rename_projects_hard_error(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "rename", "projects", "a", "b",
              "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "lit project rename" in str(result.exception)


def test_taxonomy_rm_projects_hard_error(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "rm", "projects", "x", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "lit project rm" in str(result.exception)


def test_taxonomy_merge_projects_hard_error(vault: Path) -> None:
    # Review F12: merge was the one writer missing the projects guard, so
    # `lit taxonomy merge projects ...` would drift TAXONOMY.md away from the
    # lit-config.yaml projects map. It must reject like add/rename/rm.
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "projects", "a", "--into", "b",
         "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomyError)
    assert "path binding" in str(result.exception)


def test_taxonomy_list_projects_still_works(vault: Path) -> None:
    """list is read-only — must NOT be deprecated."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["taxonomy", "list", "projects", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "projects" in result.output


def test_taxonomy_rm_referenced_prompt_then_cascade(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "litman.core.confirm._stdin_is_tty", lambda: True
    )
    _write_paper(vault, "2024_A", topics=["peptide"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])
    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "peptide", "--library", str(vault)],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "untag" in result.output.lower()
    assert _read_taxonomy(vault)["topics"] == []
    assert _read_meta(vault, "2024_A")["topics"] == []
    assert _read_meta(vault, "2024_B")["topics"] == []


def test_taxonomy_rm_yes_skips_prompt(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])
    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "peptide", "--yes",
              "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert _read_taxonomy(vault)["topics"] == []
    assert _read_meta(vault, "2024_A")["topics"] == []


def test_taxonomy_merge_referenced_prompt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "litman.core.confirm._stdin_is_tty", lambda: True
    )
    _write_paper(vault, "2024_A", topics=["transformers"])
    runner = CliRunner()
    # The pair is a near-duplicate by design (it is what merge is for), so
    # registering both takes --force.
    runner.invoke(cli, ["taxonomy", "add", "topics", "transformers",
                        "transformer", "--force", "--library", str(vault)])
    result = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "transformers", "--into",
         "transformer", "--library", str(vault)],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "rewrite" in result.output.lower()
    assert _read_meta(vault, "2024_A")["topics"] == ["transformer"]


def test_taxonomy_rm_stray_stdin_no_yes_aborts_unmutated(
    vault: Path,
) -> None:
    """C1 regression (taxonomy rm mirror): a non-tty stdin carrying a
    stray ``y\\n`` must NOT satisfy the confirmation. The spec
    ("不读不存在的 stdin") forbids reading stdin at all in a
    non-interactive environment without --yes.
    """
    _write_paper(vault, "2024_A", topics=["peptide"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])

    before = _vault_snapshot(vault)
    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "peptide", "--library", str(vault)],
        input="y\n",
    )
    assert result.exit_code != 0
    assert "Non-interactive environment" in result.output
    assert _vault_snapshot(vault) == before


def test_taxonomy_rm_atomicity_rollback_on_staged_write_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §测试覆盖 'rm atomicity rollback' + §边界 case line 164
    (taxonomy rm mirror).

    Injects a failure in the SECOND ``StagedWrite.write_text`` call and
    asserts TAXONOMY.md + every paper metadata.yaml + INDEX.json are
    byte-identical to the pre-op state, with non-zero exit.
    """
    from litman.core.atomic import StagedWrite

    _write_paper(vault, "2024_A", topics=["peptide"])
    _write_paper(vault, "2024_B", topics=["peptide"])
    runner = CliRunner()
    runner.invoke(cli, ["taxonomy", "add", "topics", "peptide",
                        "--library", str(vault)])

    before = _vault_snapshot(vault)

    real_write_text = StagedWrite.write_text
    calls = {"n": 0}

    def flaky_write_text(
        self: StagedWrite, relpath: str, content: str
    ) -> Path:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("injected mid-staged_write failure")
        return real_write_text(self, relpath, content)

    monkeypatch.setattr(StagedWrite, "write_text", flaky_write_text)

    result = runner.invoke(
        cli, ["taxonomy", "rm", "topics", "peptide", "--yes",
              "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert calls["n"] >= 2
    assert _vault_snapshot(vault) == before


# ===========================================================================
# add: near-duplicate guard
# ===========================================================================


def _add(vault: Path, *args: str) -> Any:
    return CliRunner().invoke(
        cli, ["taxonomy", "add", *args, "--library", str(vault)]
    )


def _taxonomy_bytes(vault: Path) -> bytes:
    return (vault / "TAXONOMY.md").read_bytes()


def test_add_refuses_a_near_duplicate_and_writes_nothing(vault: Path) -> None:
    assert _add(vault, "topics", "peptide").exit_code == 0
    before = _taxonomy_bytes(vault)

    result = _add(vault, "topics", "peptidedrug")

    assert result.exit_code != 0
    assert isinstance(result.exception, TaxonomySimilarError)
    message = str(result.exception)
    assert "'peptidedrug' looks like 'peptide' (prefix)." in message
    assert "--force" in message
    assert _taxonomy_bytes(vault) == before


def test_add_refuses_two_near_duplicates_given_together(vault: Path) -> None:
    before = _taxonomy_bytes(vault)

    result = _add(vault, "topics", "transformer", "transformers")

    assert isinstance(result.exception, TaxonomySimilarError)
    assert "'transformers' looks like 'transformer' (same after normalisation)." in str(
        result.exception
    )
    assert _taxonomy_bytes(vault) == before


def test_refusal_within_one_command_does_not_call_the_match_existing(
    vault: Path,
) -> None:
    """The match is the other value given in the same command, so nothing
    the message says may claim it is already registered."""
    with pytest.raises(TaxonomySimilarError) as caught:
        add_taxonomy_values(vault, "topics", ("transformer", "transformers"))

    assert str(caught.value).splitlines() == [
        "'transformers' looks like 'transformer' (same after normalisation).",
        "Use the matching value, or add --force to register it anyway.",
    ]
    assert "existing" not in str(caught.value)
    assert _read_taxonomy(vault)["topics"] == []


def test_add_is_all_or_nothing(vault: Path) -> None:
    """A clean value given beside a refused one is not registered either."""
    _add(vault, "topics", "peptide")
    before = _taxonomy_bytes(vault)

    result = _add(vault, "topics", "docking", "peptidedrug")

    assert isinstance(result.exception, TaxonomySimilarError)
    assert result.exception.similar == {"peptidedrug": [("peptide", "prefix")]}
    assert _taxonomy_bytes(vault) == before
    assert _read_taxonomy(vault)["topics"] == ["peptide"]


def test_add_force_registers_a_near_duplicate(vault: Path) -> None:
    _add(vault, "topics", "peptide")

    result = _add(vault, "topics", "peptidedrug", "--force")

    assert result.exit_code == 0, result.output
    assert _read_taxonomy(vault)["topics"] == ["peptide", "peptidedrug"]


def test_add_force_is_documented() -> None:
    result = CliRunner().invoke(cli, ["taxonomy", "add", "--help"])
    assert result.exit_code == 0
    assert "--force" in result.output


def test_add_exact_duplicate_stays_a_no_op_beside_look_alikes(vault: Path) -> None:
    """Re-adding a registered value is skipped before the look-alike check."""
    _add(vault, "topics", "peptide", "peptidedrug", "--force")
    before = _taxonomy_bytes(vault)

    result = _add(vault, "topics", "peptide")

    assert result.exit_code == 0, result.output
    assert "No-op" in result.output
    assert _taxonomy_bytes(vault) == before


def test_refusal_names_the_closest_match_and_counts_the_rest(vault: Path) -> None:
    for value in (
        "peptide-design",
        "peptide-descriptors",
        "peptide-permeability",
        "peptide-property-prediction",
        "peptide-synthesis",
    ):
        _add(vault, "topics", value)

    with pytest.raises(TaxonomySimilarError) as caught:
        add_taxonomy_values(vault, "topics", ("peptide",))

    lines = str(caught.value).splitlines()
    assert lines == [
        "'peptide' looks like 'peptide-design' (prefix) (+4 more).",
        "Use the matching value, or add --force to register it anyway.",
    ]
    assert all(len(line) <= 120 for line in lines)
    assert caught.value.similar == {
        "peptide": [
            ("peptide-design", "prefix"),
            ("peptide-synthesis", "prefix"),
            ("peptide-descriptors", "prefix"),
            ("peptide-permeability", "prefix"),
            ("peptide-property-prediction", "prefix"),
        ]
    }


def test_tagging_with_registered_look_alikes_is_not_guarded(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_X")
    _add(vault, "topics", "peptide", "peptidedrug", "--force")

    result = CliRunner().invoke(
        cli,
        ["modify", "2024_Foo_X", "--add-tag", "topics=peptide",
         "--add-tag", "topics=peptidedrug", "--library", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert _read_meta(vault, "2024_Foo_X")["topics"] == ["peptide", "peptidedrug"]


def test_rename_and_merge_onto_a_look_alike_are_not_guarded(vault: Path) -> None:
    _add(vault, "topics", "peptide", "alpha", "beta")
    runner = CliRunner()

    renamed = runner.invoke(
        cli,
        ["taxonomy", "rename", "topics", "alpha", "peptides",
         "--library", str(vault)],
    )
    merged = runner.invoke(
        cli,
        ["taxonomy", "merge", "topics", "beta", "--into", "peptidedrug",
         "--yes", "--library", str(vault)],
    )

    assert renamed.exit_code == 0, renamed.output
    assert merged.exit_code == 0, merged.output
    assert _read_taxonomy(vault)["topics"] == ["peptide", "peptidedrug", "peptides"]


# ===========================================================================
# list: usage counts
# ===========================================================================


def _usage_library(vault: Path) -> None:
    _write_paper(vault, "2024_A", topics=["peptide", "docking"], methods=["MD"])
    _write_paper(vault, "2024_B", topics=["peptide"], status="deep-read")
    _write_paper(vault, "2024_C", topics=["ghost"], type="review")
    _add(vault, "topics", "docking", "peptide", "spps")
    _add(vault, "methods", "MD")


def _list_json(vault: Path, *args: str) -> list[dict[str, Any]]:
    result = CliRunner().invoke(
        cli,
        ["taxonomy", "list", *args, "--format", "json", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)
    return rows


def test_list_json_counts_the_papers_using_each_value(vault: Path) -> None:
    from litman.core.views import load_index_papers

    _usage_library(vault)
    # No fresh INDEX here: the counts come from the metadata scan.
    assert load_index_papers(vault) is None

    rows = {r["dict"]: r for r in _list_json(vault)}

    assert rows["topics"]["used_by"] == {"docking": 1, "peptide": 2, "spps": 0}
    assert rows["methods"]["used_by"] == {"MD": 1}
    assert rows["data"]["used_by"] == {}
    assert rows["projects"]["used_by"] == {}
    # Scalar fields count the papers set to each value.
    assert rows["status"]["used_by"] == {
        "deep-read": 1, "skim": 0, "inbox": 2, "dropped": 0,
    }
    assert rows["type"]["used_by"]["research"] == 2
    assert rows["type"]["used_by"]["review"] == 1
    assert set(rows["type"]["used_by"]) == set(rows["type"]["values"])
    # An unregistered value in a paper is taxonomy_drift's, not a key here.
    assert "ghost" not in rows["topics"]["used_by"]


def test_list_json_counts_agree_with_the_index(vault: Path) -> None:
    from litman.core.correctors import regen

    _usage_library(vault)
    regen(vault)
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))

    row = _list_json(vault, "topics")[0]

    for value in row["values"]:
        expected = sum(1 for p in index["papers"] if value in p["topics"])
        assert row["used_by"][value] == expected, value


def test_list_counts_come_from_a_fresh_index_without_a_scan(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.commands import taxonomy as taxonomy_cmd
    from litman.core.correctors import regen

    _usage_library(vault)
    regen(vault)

    def _no_scan(vault: Path) -> list[dict[str, Any]]:
        raise AssertionError("metadata scanned although INDEX.json is fresh")

    monkeypatch.setattr(taxonomy_cmd, "list_papers", _no_scan)

    rows = {r["dict"]: r for r in _list_json(vault)}

    assert rows["topics"]["used_by"] == {"docking": 1, "peptide": 2, "spps": 0}


def test_list_table_shows_each_value_with_its_count(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    from litman.commands import taxonomy as taxonomy_cmd

    monkeypatch.setattr(taxonomy_cmd, "console", Console(width=200))
    _usage_library(vault)

    result = CliRunner().invoke(cli, ["taxonomy", "list", "--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert "docking·1, peptide·2, spps·0" in result.output
    assert "MD·1" in result.output


def test_list_single_dict_shows_each_value_with_its_count(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    from litman.commands import taxonomy as taxonomy_cmd

    monkeypatch.setattr(taxonomy_cmd, "console", Console(width=200))
    _usage_library(vault)

    result = CliRunner().invoke(
        cli, ["taxonomy", "list", "topics", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "  - docking  1" in lines
    assert "  - peptide  2" in lines
    assert "  - spps     0" in lines


# ===========================================================================
# A TAXONOMY.md that is not UTF-8
# ===========================================================================


def _resave_with_latin1_topic(vault: Path) -> bytes:
    """TAXONOMY.md after an editor set to Latin-1 added the topic ``peptidé``:
    line 14, its é the single byte 0xE9, the rest of the file still UTF-8.
    Returns the bytes on disk."""
    path = vault / "TAXONOMY.md"
    head, tail = path.read_text(encoding="utf-8").split("## topics\n\n(empty)\n")
    data = head.encode("utf-8") + b"## topics\n\n- peptid\xe9\n" + tail.encode("utf-8")
    unlock_truth_file(path)
    path.write_bytes(data)
    lock_truth_file(path)
    return data


def _not_utf8_message(vault: Path, line: int) -> str:
    return (
        f"Cannot read {vault / 'TAXONOMY.md'}: line {line} is not UTF-8. "
        "Re-save it as UTF-8, then retry."
    )


def _write_taxonomy_bytes(vault: Path, data: bytes) -> None:
    path = vault / "TAXONOMY.md"
    unlock_truth_file(path)
    path.write_bytes(data)


def test_read_taxonomy_text_names_the_line_that_is_not_utf8(vault: Path) -> None:
    _resave_with_latin1_topic(vault)

    with pytest.raises(UnreadableTaxonomyError) as info:
        read_taxonomy_text(vault)

    assert str(info.value) == _not_utf8_message(vault, 14)
    assert info.value.reason == "line 14 is not UTF-8"
    assert info.value.not_utf8
    assert info.value.path == vault / "TAXONOMY.md"


def test_read_taxonomy_text_counts_the_line_from_the_top_of_a_long_file(
    vault: Path,
) -> None:
    # Several times a text reader's 8 KiB buffer: the line is counted from
    # the top of the file, should a reader ever decode it piece by piece.
    lines = ["# Literature Taxonomy", "", "## topics", ""]
    lines += [f"- topic-{i:03d}-{'x' * 50}" for i in range(500)]
    data = ("\n".join(lines) + "\n").encode("utf-8") + b"- caf\xe9\n"
    assert len(data) > 3 * 8192
    _write_taxonomy_bytes(vault, data)

    with pytest.raises(UnreadableTaxonomyError) as info:
        read_taxonomy_text(vault)

    assert str(info.value) == _not_utf8_message(vault, len(lines) + 1)


def test_read_taxonomy_text_refuses_utf16_at_line_1(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8")
    _write_taxonomy_bytes(vault, text.encode("utf-16"))

    with pytest.raises(UnreadableTaxonomyError) as info:
        read_taxonomy_text(vault)

    assert str(info.value) == _not_utf8_message(vault, 1)


def test_read_taxonomy_text_reads_utf8_with_a_bom(vault: Path) -> None:
    text = (vault / "TAXONOMY.md").read_text(encoding="utf-8").replace(
        "## topics\n\n(empty)", "## topics\n\n- peptidé"
    )
    _write_taxonomy_bytes(vault, b"\xef\xbb\xbf" + text.encode("utf-8"))

    assert parse_taxonomy(read_taxonomy_text(vault))["topics"] == ["peptidé"]


def test_read_taxonomy_text_names_a_missing_file(vault: Path) -> None:
    path = vault / "TAXONOMY.md"
    unlock_truth_file(path)
    path.unlink()

    with pytest.raises(UnreadableTaxonomyError) as info:
        read_taxonomy_text(vault)

    message = str(info.value)
    assert message.startswith(f"Cannot read {path} (")
    assert message.endswith("). Run `lit health-check` to see how to fix it.")
    assert not info.value.not_utf8


@pytest.mark.parametrize(
    "args",
    [
        ["taxonomy", "list"],
        ["taxonomy", "add", "topics", "spps"],
        ["taxonomy", "rename", "topics", "peptide", "peptides"],
        ["taxonomy", "merge", "topics", "peptide", "--into", "spps", "--yes"],
        ["taxonomy", "rm", "topics", "peptide", "--yes"],
        ["project", "list"],
        ["project", "add", "pepforge", "--path", "<dir>"],
        ["project", "rename", "pepforge", "pepforge2"],
        ["project", "rm", "pepforge", "--yes"],
        ["modify", "2024_Foo_Bar", "--add-tag", "topics=spps"],
    ],
    ids=lambda args: "-".join(args[:2]),
)
def test_a_command_on_a_taxonomy_that_is_not_utf8_names_the_line(
    vault: Path, tmp_path: Path, args: list[str]
) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    data = _resave_with_latin1_topic(vault)
    args = [str(tmp_path) if a == "<dir>" else a for a in args]

    result = CliRunner().invoke(cli, [*args, "--library", str(vault)])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, UnreadableTaxonomyError)
    assert str(result.exception) == _not_utf8_message(vault, 14)
    assert (vault / "TAXONOMY.md").read_bytes() == data


def test_a_taxonomy_that_is_not_utf8_is_one_error_line_through_the_real_main(
    vault: Path,
) -> None:
    from tests.commands.test_si_cli import _run_lit

    data = _resave_with_latin1_topic(vault)

    proc = _run_lit(vault, "taxonomy", "add", "topics", "spps")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == [f"error: {_not_utf8_message(vault, 14)}"]
    assert (vault / "TAXONOMY.md").read_bytes() == data
