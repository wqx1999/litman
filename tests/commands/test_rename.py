"""Tests for ``lit rename``.

Covers paper-id change, metadata roundtrip, ref-list ripple across other
papers, ``[[id]]`` wikilink rewrite in notes.md files, INDEX.json refresh,
views/ rebuild, atomic directory rename, and pre-flight validation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.commands import _drift
from litman.commands import rename as rename_mod
from litman.core import atomic as atomic_mod
from litman.core import locking
from litman.core import viewer as viewer_mod
from litman.core import views as views_mod
from litman.core.atomic import StagedWrite
from litman.core.checks import run_all_checks
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.notes import discussion_scaffold
from litman.core.vault_registry import VaultEntry, VaultRegistry, save_registry
from litman.exceptions import (
    CorruptMetadataError,
    PaperNotFoundError,
    RenameError,
)
from litman.core.portable_link import is_portable_link

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    """Materialise a paper with the canonical M2.0-schema metadata."""
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
        "related": fields.get("related", []),
        "contradicts": fields.get("contradicts", []),
        "contradicted-by": fields.get("contradicted_by", []),
        "extends": fields.get("extends", []),
        "extended-by": fields.get("extended_by", []),
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)
    notes = fields.get("notes")
    if notes is not None:
        (paper_dir / "notes.md").write_text(notes, encoding="utf-8")


def _read_meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ===========================================================================
# Happy path
# ===========================================================================


def test_rename_directory_and_id_field(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert not (vault / "papers" / "2024_Foo_Bar").exists()
    new_dir = vault / "papers" / "2024_Foo_Baz"
    assert new_dir.is_dir()
    meta = _read_meta(vault, "2024_Foo_Baz")
    assert meta["id"] == "2024_Foo_Baz"
    # updated-at bumped past the seed value.
    assert meta["updated-at"] != "2026-04-28T10:00:00+02:00"


def test_rename_ripples_to_back_references(vault: Path) -> None:
    _write_paper(vault, "2023_Old_Source")
    _write_paper(
        vault, "2024_Holder_A",
        related=["2023_Old_Source"],
        extends=["2023_Old_Source", "2022_Other"],
    )
    _write_paper(vault, "2024_Holder_B", contradicts=["2023_Old_Source"])
    _write_paper(vault, "2024_NoRef")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2023_Old_Source", "2023_New_Source",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated 2 back-referencing" in result.output

    a = _read_meta(vault, "2024_Holder_A")
    assert a["related"] == ["2023_New_Source"]
    assert a["extends"] == ["2023_New_Source", "2022_Other"]
    assert a["updated-at"] != "2026-04-28T10:00:00+02:00"

    b = _read_meta(vault, "2024_Holder_B")
    assert b["contradicts"] == ["2023_New_Source"]

    no_ref = _read_meta(vault, "2024_NoRef")
    assert no_ref["updated-at"] == "2026-04-28T10:00:00+02:00"


def test_rename_rewrites_reverse_fields(vault: Path) -> None:
    """ADR-012: renaming an id rewrites it inside reverse relation fields
    (extended-by / contradicted-by) too, not just the forward fields."""
    _write_paper(vault, "2023_Old_Source")
    _write_paper(
        vault, "2024_Downstream",
        extended_by=["2023_Old_Source"],
        contradicted_by=["2023_Old_Source"],
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2023_Old_Source", "2023_New_Source",
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    d = _read_meta(vault, "2024_Downstream")
    assert d["extended-by"] == ["2023_New_Source"]
    assert d["contradicted-by"] == ["2023_New_Source"]


def test_rename_self_reference_updated(vault: Path) -> None:
    """A paper that references itself in related/extends has the self-ref
    rewritten too."""
    _write_paper(vault, "2024_Self", related=["2024_Self"])

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Self", "2024_Selfie", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    meta = _read_meta(vault, "2024_Selfie")
    assert meta["related"] == ["2024_Selfie"]


def test_rename_dedupes_collisions(vault: Path) -> None:
    """If a paper already references both <old> and <new>, the post-rename
    list collapses the duplicate."""
    _write_paper(vault, "2024_Old")
    _write_paper(vault, "2024_New")
    _write_paper(vault, "2024_Holder", related=["2024_Old", "2024_New"])

    # Rename Old → some other name to avoid the new_dir-exists check
    runner = CliRunner()
    # Move 2024_New out of the way first, then rename Old→New_X to create
    # a synthetic dedupe scenario instead. Simpler: rename Old→Other and
    # check the simple path.
    result = runner.invoke(
        cli,
        ["rename", "2024_Old", "2024_Other", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    holder = _read_meta(vault, "2024_Holder")
    assert holder["related"] == ["2024_Other", "2024_New"]


def test_rename_dedupes_when_new_already_present(vault: Path) -> None:
    """Holder lists [Old, New]; renaming Old→New (where New is also a
    valid paper id chain) — exercise via rename of an unrelated id to
    `New` so no dir collision but ref-list dedupe."""
    _write_paper(vault, "2024_A")
    _write_paper(vault, "2024_B")
    _write_paper(vault, "2024_Holder", related=["2024_A", "2024_B"])

    # Rename A → C (fresh name). Holder.related should become [C, B] (no dedupe).
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rename", "2024_A", "2024_C", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    holder = _read_meta(vault, "2024_Holder")
    assert holder["related"] == ["2024_C", "2024_B"]


def test_rename_rewrites_paper_notes_md(vault: Path) -> None:
    _write_paper(
        vault, "2024_Foo_Bar",
        notes="# Foo Bar\n\nSee [[2024_Foo_Bar]] for context.\n",
    )
    _write_paper(
        vault, "2024_Other",
        notes="Compare with [[2024_Foo_Bar]] and [[2024_Foo_Bar]].\n",
    )
    _write_paper(vault, "2024_Untouched", notes="No refs here.\n")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Updated 2 notes" in result.output

    assert "[[2024_Foo_Baz]]" in (
        vault / "papers/2024_Foo_Baz/notes.md"
    ).read_text()
    assert "[[2024_Foo_Baz]]" in (
        vault / "papers/2024_Other/notes.md"
    ).read_text()
    assert "[[2024_Foo_Bar]]" not in (
        vault / "papers/2024_Other/notes.md"
    ).read_text()
    untouched = (vault / "papers/2024_Untouched/notes.md").read_text()
    assert untouched == "No refs here.\n"


def test_rename_rewrites_paper_discussion_md(vault: Path) -> None:
    """Q1 widening: enumerate_markdown_files now yields discussion.md, so a
    ``[[old-id]]`` inside a referencing paper's discussion.md is rewritten to
    ``[[new-id]]`` (mirrors the notes.md rewrite path)."""
    _write_paper(vault, "2024_Foo_Bar")
    _write_paper(vault, "2024_Other")
    (vault / "papers/2024_Other/discussion.md").write_text(
        "Discussed alongside [[2024_Foo_Bar]] in the lab meeting.\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    discussion = (vault / "papers/2024_Other/discussion.md").read_text()
    assert "[[2024_Foo_Baz]]" in discussion
    assert "[[2024_Foo_Bar]]" not in discussion


# ---------------------------------------------------------------------------
# The renamed paper's discussion.md heading follows the id; plain-text
# mentions of the old id anywhere are listed, never rewritten.
# ---------------------------------------------------------------------------


def _discussion(vault: Path, paper_id: str) -> Path:
    return vault / "papers" / paper_id / "discussion.md"


def _wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """One whole line per assertion: the console width is fixed at first use."""
    from rich.console import Console

    monkeypatch.setattr(rename_mod, "console", Console(width=200))


def _rename_foo(vault: Path) -> Any:
    return CliRunner().invoke(
        cli, ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)]
    )


def test_rename_retitles_the_discussion_heading_lit_add_wrote(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    _discussion(vault, "2024_Foo_Bar").write_text(
        discussion_scaffold("2024_Foo_Bar") + "\n## 2026-10-01 10:00\n\nMine.\n",
        encoding="utf-8",
    )

    result = _rename_foo(vault)

    assert result.exit_code == 0, result.output
    assert _discussion(vault, "2024_Foo_Baz").read_text(encoding="utf-8") == (
        discussion_scaffold("2024_Foo_Baz") + "\n## 2026-10-01 10:00\n\nMine.\n"
    )


def test_rename_overwrites_a_discussion_heading_the_user_rewrote(
    vault: Path,
) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    rest = b"\n\n<!-- format -->\n\n## 2026-10-01 10:00\n\nMine.\n"
    _discussion(vault, "2024_Foo_Bar").write_bytes(b"# My own log title" + rest)

    result = _rename_foo(vault)

    assert result.exit_code == 0, result.output
    assert _discussion(vault, "2024_Foo_Baz").read_bytes() == (
        b"# Discussion log for 2024_Foo_Baz" + rest
    )
    assert "heading" not in result.output


def test_rename_puts_a_heading_on_a_discussion_that_lost_it(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    body = b"## 2026-10-01 10:00\n\n**Question:** why?\n"
    _discussion(vault, "2024_Foo_Bar").write_bytes(body)

    result = _rename_foo(vault)

    assert result.exit_code == 0, result.output
    assert _discussion(vault, "2024_Foo_Baz").read_bytes() == (
        b"# Discussion log for 2024_Foo_Baz\n\n" + body
    )


def test_rename_of_a_paper_without_discussion_md_creates_none(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", notes="# Foo Bar\n")

    result = _rename_foo(vault)

    assert result.exit_code == 0, result.output
    assert not _discussion(vault, "2024_Foo_Baz").exists()


def test_rename_keeps_windows_line_endings_in_the_files_it_rewrites(
    vault: Path,
) -> None:
    """A library written on Windows has CRLF notes: the wikilink rewrite and
    the heading change must not turn them into LF."""
    _write_paper(vault, "2024_Foo_Bar")
    _write_paper(vault, "2024_Other")
    _discussion(vault, "2024_Foo_Bar").write_bytes(
        discussion_scaffold("2024_Foo_Bar").replace("\n", "\r\n").encode()
    )
    other_notes = vault / "papers" / "2024_Other" / "notes.md"
    other_notes.write_bytes(b"# Other\r\n\r\nSee [[2024_Foo_Bar]].\r\n")

    result = _rename_foo(vault)

    assert result.exit_code == 0, result.output
    assert _discussion(vault, "2024_Foo_Baz").read_bytes() == (
        discussion_scaffold("2024_Foo_Baz").replace("\n", "\r\n").encode()
    )
    assert other_notes.read_bytes() == b"# Other\r\n\r\nSee [[2024_Foo_Baz]].\r\n"


def test_rename_lists_plain_text_mentions_of_the_old_id_and_leaves_them(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wide(monkeypatch)
    _write_paper(
        vault, "2024_Foo_Bar",
        notes="# Foo Bar\n\nAdded as 2024_Foo_Bar.\n",
    )
    _write_paper(
        vault, "2024_Other",
        notes="Ran `lit show 2024_Foo_Bar`.\n\nSee [[2024_Foo_Bar]].\n"
        "Again 2024_Foo_Bar here.\n",
    )
    _write_paper(vault, "2024_Quiet", notes="Only 2024_Foo_Bar-2 and [[2024_Foo_Bar]].\n")
    plain_only = vault / "papers" / "2024_Third" / "discussion.md"
    _write_paper(vault, "2024_Third")
    plain_only.write_bytes(b"# Discussion log for 2024_Third\n\nvs 2024_Foo_Bar\n")

    result = _rename_foo(vault)

    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    assert lines.count(
        "plain text papers/2024_Foo_Baz/notes.md:3 still says 2024_Foo_Bar, left as is"
    ) == 1
    assert lines.count(
        "plain text papers/2024_Other/notes.md:1,4 still says 2024_Foo_Bar, left as is"
    ) == 1
    assert lines.count(
        "plain text papers/2024_Third/discussion.md:3 still says 2024_Foo_Bar, left as is"
    ) == 1
    assert "2024_Quiet" not in result.output
    assert result.output.count("plain text") == 3
    # Only the [[ ]] links changed; the plain text is as the user wrote it.
    assert (vault / "papers/2024_Other/notes.md").read_text(encoding="utf-8") == (
        "Ran `lit show 2024_Foo_Bar`.\n\nSee [[2024_Foo_Baz]].\n"
        "Again 2024_Foo_Bar here.\n"
    )
    assert plain_only.read_bytes() == b"# Discussion log for 2024_Third\n\nvs 2024_Foo_Bar\n"


def test_rename_says_which_notes_file_it_could_not_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wide(monkeypatch)
    _write_paper(vault, "2024_Foo_Bar")
    _write_paper(vault, "2024_Other")
    unreadable = vault / "papers" / "2024_Other" / "notes.md"
    # Bytes: the text-mode fixture would write CRLF on Windows.
    unreadable.write_bytes(b"See [[2024_Foo_Bar]].\n")
    real_read_bytes = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        # By name, not by path: the vault may come back resolved.
        if (self.parent.name, self.name) == ("2024_Other", "notes.md"):
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    result = _rename_foo(vault)

    assert result.exit_code == 0, result.output
    assert (
        "could not read notes file papers/2024_Other/notes.md "
        "(PermissionError) — skipped"
    ) in result.output
    assert real_read_bytes(unreadable) == b"See [[2024_Foo_Bar]].\n"


def test_rename_refreshes_index_and_views(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", topics=["alpha"])

    runner = CliRunner()
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    assert is_portable_link(vault / "views/by-topic/alpha/2024_Foo_Bar")

    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads((vault / "INDEX.json").read_text())
    assert payload["n_papers"] == 1
    assert payload["papers"][0]["id"] == "2024_Foo_Baz"

    assert not (vault / "views/by-topic/alpha/2024_Foo_Bar").exists()
    new_link = vault / "views/by-topic/alpha/2024_Foo_Baz"
    assert is_portable_link(new_link)
    assert new_link.resolve() == (vault / "papers/2024_Foo_Baz").resolve()


def test_rename_paper_pdf_preserved(vault: Path) -> None:
    """Binary files inside the paper dir survive the directory rename."""
    _write_paper(vault, "2024_Foo_Bar")
    pdf = vault / "papers/2024_Foo_Bar/paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n%%EOF\n")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    new_pdf = vault / "papers/2024_Foo_Baz/paper.pdf"
    assert new_pdf.is_file()
    assert new_pdf.read_bytes().startswith(b"%PDF-1.4")


# ===========================================================================
# Pre-flight rejection
# ===========================================================================


def test_rename_identical_old_new(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Bar", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RenameError)
    assert "identical" in str(result.exception)


def test_rename_invalid_new_id(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "../escape", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RenameError)
    assert "Invalid new id" in str(result.exception)


def test_rename_unknown_old_id(vault: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["rename", "9999_Ghost", "9999_Spirit", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PaperNotFoundError)


def test_rename_corrupt_metadata_friendly_error(vault: Path) -> None:
    # Review A1: renaming a paper whose own metadata.yaml is corrupt must
    # surface a friendly, path-naming CorruptMetadataError instead of a raw
    # ruamel YAMLError traceback. resolve_paper_id matches by directory name
    # (no YAML parse), so the corrupt file is first read at the round-trip
    # load — exactly the site the shared helper now guards.
    _write_paper(vault, "2024_Foo_Bar")
    bad = vault / "papers/2024_Foo_Bar/metadata.yaml"
    bad.write_text("title: [unterminated\n: : :\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, CorruptMetadataError)
    assert "2024_Foo_Bar" in str(result.exception)
    # No partial state — the folder is not renamed.
    assert (vault / "papers/2024_Foo_Bar").is_dir()
    assert not (vault / "papers/2024_Foo_Baz").exists()


def test_rename_target_already_taken(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    _write_paper(vault, "2024_Foo_Baz")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, RenameError)
    assert "already exists" in str(result.exception)
    # No partial state — both papers still present.
    assert (vault / "papers/2024_Foo_Bar").is_dir()
    assert (vault / "papers/2024_Foo_Baz").is_dir()


# ===========================================================================
# Atomicity
# ===========================================================================


def test_rename_does_not_touch_unrelated_papers(vault: Path) -> None:
    _write_paper(vault, "2024_Renamed")
    _write_paper(vault, "2024_Untouched", topics=["x"])

    untouched_meta_path = vault / "papers/2024_Untouched/metadata.yaml"
    before_text = untouched_meta_path.read_text()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rename", "2024_Renamed", "2024_NewName", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert untouched_meta_path.read_text() == before_text


def test_rename_post_state_consistent(vault: Path) -> None:
    """After rename, every paper's metadata id matches its dir name and
    INDEX.json agrees with the on-disk state."""
    _write_paper(vault, "2024_A", related=["2024_B"])
    _write_paper(vault, "2024_B", related=["2024_A"])

    runner = CliRunner()
    result = runner.invoke(
        cli, ["rename", "2024_A", "2024_AA", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output

    payload = json.loads((vault / "INDEX.json").read_text())
    ids_in_index = sorted(p["id"] for p in payload["papers"])
    ids_on_disk = sorted(
        d.name for d in (vault / "papers").iterdir() if d.is_dir()
    )
    assert ids_in_index == ids_on_disk == ["2024_AA", "2024_B"]

    # Each metadata id matches its dir.
    for pid in ids_on_disk:
        meta = _read_meta(vault, pid)
        assert meta["id"] == pid

    # B's reverse ref now points to AA.
    b = _read_meta(vault, "2024_B")
    assert b["related"] == ["2024_AA"]


# ===========================================================================
# CLI smoke
# ===========================================================================


def test_rename_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["rename", "--help"])
    assert result.exit_code == 0
    assert "rename" in result.output.lower()


def test_rename_accepts_fuzzy_old(vault: Path) -> None:
    """M11 smoke: old id accepts a substring; new is NOT fuzzy-matched."""
    _write_paper(vault, "2024_Pandi_Cellfree")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "rename",
            "Pandi",
            "2024_Pandi_CellfreeV2",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_Pandi_CellfreeV2").is_dir()
    assert not (vault / "papers" / "2024_Pandi_Cellfree").exists()


def test_rename_cascades_into_repo_meta(vault: Path) -> None:
    """Renaming a paper rewrites its id inside every bound repo's
    repo-meta.yaml::papers (invariant #12 bidirectional binding), not just
    other papers' ref lists. Regression for bug-report 2026-06-01 #3.
    """
    old, new = "2023_Pandi_Protein", "2024_Pandi_Folding"
    _write_paper(vault, old)

    rt = YAML()
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.default_flow_style = False

    # Paper side: bind to repo "myrepo" via code-clones.
    meta_path = vault / "papers" / old / "metadata.yaml"
    meta = rt.load(meta_path.read_text())
    meta["code-clones"] = ["myrepo"]
    with meta_path.open("w", encoding="utf-8") as f:
        rt.dump(meta, f)

    # Repo side: codes/myrepo/repo-meta.yaml mirrors the binding (papers=[old]).
    repo_dir = vault / "codes" / "myrepo"
    repo_dir.mkdir(parents=True)
    stale_ts = "2026-04-28T10:00:00+02:00"
    repo_meta = {
        "name": "myrepo",
        "upstream": "https://example.com/myrepo.git",
        "created-at": stale_ts,
        "updated-at": stale_ts,
        "papers": [old],
        "framework": None,
        "runs-on": None,
        "status": None,
    }
    with (repo_dir / "repo-meta.yaml").open("w", encoding="utf-8") as f:
        rt.dump(repo_meta, f)

    runner = CliRunner()
    result = runner.invoke(cli, ["rename", old, new, "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert "repo binding" in result.output

    # repo-meta back-reference rewritten old -> new, updated-at bumped.
    after = _yaml.load((repo_dir / "repo-meta.yaml").read_text())
    assert after["papers"] == [new]
    assert after["updated-at"] != stale_ts
    # Paper-side binding preserved (repo name is unchanged by a paper rename).
    assert _read_meta(vault, new)["code-clones"] == ["myrepo"]


def test_rename_reports_a_moved_aside_folder(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming a linked paper rebuilds the project hubs, which can move a
    folder out of the user's own directory — never in silence."""
    import shutil

    from litman.core.portable_link import remove_link_if_present

    monkeypatch.setenv("COLUMNS", "400")
    proj = tmp_path / "myproj"
    proj.mkdir()
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  myproj: {proj}\n",
        encoding="utf-8",
    )
    _write_paper(vault, "2024_Foo_Bar", projects=["myproj"])
    runner = CliRunner()
    runner.invoke(cli, ["refresh-views", "--library", str(vault)])
    link = proj / "litman_reflib" / "2024_Foo_Bar"
    assert remove_link_if_present(link)
    shutil.copytree(vault / "papers" / "2024_Foo_Bar", link)
    (link / "MY_NOTES.md").write_text("hand-written\n", encoding="utf-8")

    result = runner.invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "kept 1 folder that does not match the vault:" in flat
    kept_root = (
        vault / ".trash" / "replaced-folders" / "myproj" / "litman_reflib"
    )
    kept = sorted(kept_root.iterdir())
    assert len(kept) == 1
    assert (kept[0] / "MY_NOTES.md").is_file()
    assert is_portable_link(proj / "litman_reflib" / "2024_Foo_Baz")


def test_rename_leaves_a_leftover_removed_si_folder_alone(vault: Path) -> None:
    """``lit rm --si`` deletes for good, so ``.trash/removed-si/`` means
    nothing: a folder an earlier development build left there under the old
    id is neither moved nor reported."""
    _write_paper(vault, "2024_Foo_Bar")
    folder = vault / ".trash" / "removed-si" / "2024_Foo_Bar"
    folder.mkdir(parents=True)
    leftover = folder / "20261020T083000Z-si-02.xlsx"
    leftover.write_bytes(b"sheet")

    result = CliRunner().invoke(
        cli,
        ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "✓ Renamed 2024_Foo_Bar → 2024_Foo_Baz",
        "INDEX.json + views/ refreshed.",
    ]
    assert leftover.read_bytes() == b"sheet"
    assert sorted(p.name for p in (vault / ".trash" / "removed-si").iterdir()) == [
        "2024_Foo_Bar"
    ]


# ===========================================================================
# The folder move is part of the transaction
#
# A rename whose folder cannot be moved (on Windows, a file in it open in
# another program) must change nothing, and a rename killed at any step must
# leave the library entirely old or entirely new once the next command has
# run its recovery. Time is pinned on the module objects so two libraries
# that went through the same rename compare byte for byte.
# ===========================================================================

_PINNED = "2026-10-23T12:00:00+02:00"
_OLD, _NEW, _OTHER = "2024_Foo_Bar", "2024_Foo_Baz", "2024_Other"
# What a crash may never leave behind, once the next command has recovered.
_TORN = {
    "stale_staging",
    "stale_staging_unrecoverable",
    "paper_dir_validity",
    "unfinished_rename",
    "index_vs_disk",
    "dangling_refs",
    "bidirectional_refs",
    "dangling_wikilinks",
}


class _Killed(BaseException):
    """A hard kill: nothing after the crash point runs, _cleanup included."""


def _library_of_two(parent: Path) -> Path:
    """A and B relate to each other, B's notes link A, A's notes link itself."""
    vault = create_vault(parent)
    _write_paper(
        vault, _OLD, related=[_OTHER], status="deep-read",
        notes=f"# Foo Bar\n\nSee [[{_OLD}]] for context.\n",
    )
    _write_paper(
        vault, _OTHER, related=[_OLD], status="deep-read",
        notes=f"Compare with [[{_OLD}]].\n",
    )
    for pid in (_OLD, _OTHER):
        (vault / "papers" / pid / "paper.pdf").write_bytes(b"%PDF-1.4 stub\n")
        (vault / "papers" / pid / "discussion.md").write_text(
            discussion_scaffold(pid), encoding="utf-8"
        )
    result = CliRunner().invoke(cli, ["refresh-views", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    return vault


def _library(vault: Path) -> dict[str, Any]:
    """Every file outside views/ and the staging area, byte for byte.

    INDEX.json is compared parsed: the post-commit rebuild writes it in text
    mode and a staged write as bytes, so on Windows the two differ in line
    endings alone.
    """
    snap: dict[str, Any] = {}
    for path in sorted(vault.rglob("*")):
        rel = path.relative_to(vault)
        if rel.parts[0] in ("views", ".litman-staging") or path.is_dir():
            continue
        if rel.as_posix() == "INDEX.json":
            snap["INDEX.json"] = json.loads(path.read_text(encoding="utf-8"))
        else:
            snap[rel.as_posix()] = path.read_bytes()
    return snap


def _pin_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rename_mod, "now_iso", lambda: _PINNED)
    monkeypatch.setattr(views_mod, "now_iso", lambda: _PINNED)


def _rename(vault: Path) -> Any:
    return CliRunner().invoke(cli, ["rename", _OLD, _NEW, "--library", str(vault)])


def _staging_is_empty(vault: Path) -> bool:
    return list((vault / ".litman-staging").iterdir()) == []


def test_rename_whose_folder_cannot_be_moved_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _library_of_two(tmp_path)
    before = _library(vault)

    def refuse(src: Path, dst: Path) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(atomic_mod, "_rename_dir", refuse)
    result = _rename(vault)

    assert result.exit_code == 1
    assert isinstance(result.exception, RenameError)
    assert str(result.exception) == (
        f"Could not rename papers/{_OLD}/ (Access is denied). "
        "Close any file open from it, then retry."
    )
    assert _library(vault) == before
    assert (vault / "papers" / _OLD / "discussion.md").read_text(
        encoding="utf-8"
    ) == discussion_scaffold(_OLD)
    assert (vault / "papers" / _OLD).is_dir()
    assert not (vault / "papers" / _NEW).exists()
    assert _staging_is_empty(vault)


def test_rename_stages_the_papers_own_files_at_the_new_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its metadata and the notes holding its self-link land in the moved
    folder; nothing is left at, or recreates, papers/<old>/."""
    vault = _library_of_two(tmp_path)
    _pin_time(monkeypatch)

    result = _rename(vault)

    assert result.exit_code == 0, result.output
    assert not (vault / "papers" / _OLD).exists()
    new = vault / "papers" / _NEW
    assert (new / "notes.md").read_text(encoding="utf-8") == (
        f"# Foo Bar\n\nSee [[{_NEW}]] for context.\n"
    )
    assert (new / "discussion.md").read_text(encoding="utf-8") == (
        discussion_scaffold(_NEW)
    )
    assert _read_meta(vault, _NEW)["id"] == _NEW
    assert _read_meta(vault, _NEW)["updated-at"] == _PINNED
    assert _staging_is_empty(vault)


def _crash_at(point: str, m: pytest.MonkeyPatch) -> None:
    """Patch StagedWrite so the rename dies at ``point`` like a killed process."""
    m.setattr(StagedWrite, "_cleanup", lambda self: None)

    def kill(self: StagedWrite) -> None:
        raise _Killed

    if point == "move fails":

        def refuse(src: Path, dst: Path) -> None:
            raise PermissionError(13, "Access is denied")

        m.setattr(atomic_mod, "_rename_dir", refuse)
    elif point == "after manifest":
        m.setattr(StagedWrite, "_move_dirs", kill)
    elif point == "after move":
        m.setattr(StagedWrite, "_write_sentinel", kill)
    elif point == "after commit":
        m.setattr(StagedWrite, "_promote", kill)
    else:
        real_promote = StagedWrite._promote

        def half_promote(self: StagedWrite) -> None:
            self._staged = dict(list(self._staged.items())[:2])
            real_promote(self)
            raise _Killed

        m.setattr(StagedWrite, "_promote", half_promote)


@pytest.mark.parametrize(
    ("point", "outcome"),
    [
        ("move fails", "old"),
        ("after manifest", "old"),
        ("after move", "old"),
        ("after commit", "new"),
        ("mid promote", "new"),
    ],
)
def test_a_rename_killed_anywhere_leaves_the_library_old_or_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str, outcome: str
) -> None:
    _pin_time(monkeypatch)
    ref_parent = tmp_path / "ref"
    ref_parent.mkdir()
    reference = _library_of_two(ref_parent)
    assert _rename(reference).exit_code == 0
    new_library = _library(reference)

    vault = _library_of_two(tmp_path)
    old_library = _library(vault)

    with monkeypatch.context() as m:
        _crash_at(point, m)
        if point == "move fails":
            assert isinstance(_rename(vault).exception, RenameError)
        else:
            with pytest.raises(_Killed):
                _rename(vault)
    assert not _staging_is_empty(vault)

    result = CliRunner().invoke(cli, ["list", "--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert _staging_is_empty(vault)
    assert _library(vault) == (old_library if outcome == "old" else new_library)
    # A clean abort is silent; a folder moved back or a commit finished is not.
    recovered = [
        line for line in result.stderr.splitlines()
        if line.startswith("recovered:")
    ]
    silent = point in ("move fails", "after manifest")
    assert len(recovered) == (0 if silent else 1)
    found = {i.category for i in run_all_checks(vault, list_papers(vault))}
    assert not found & _TORN
    if outcome == "old":
        assert "views_vs_metadata" not in found
        return
    # Views and project links are rebuilt by --fix, as after any write
    # command that died past its commit.
    assert "views_vs_metadata" in found
    fixed = CliRunner().invoke(
        cli, ["health-check", "--fix", "--library", str(vault)]
    )
    assert fixed.exception is None or isinstance(fixed.exception, SystemExit), (
        fixed.output
    )
    found = {i.category for i in run_all_checks(vault, list_papers(vault))}
    assert not found & (_TORN | {"views_vs_metadata"})


def _folder_with_open_file_can_be_renamed(where: Path) -> bool:
    """Probed, not read off sys.platform: Windows refuses to rename a folder
    while a file in it is open; Linux and macOS allow it."""
    probe = where / "lock-probe"
    probe.mkdir()
    held = probe / "held.txt"
    held.write_text("x", encoding="utf-8")
    with held.open("rb"):
        try:
            os.rename(probe, where / "lock-probe-moved")
        except OSError:
            return False
    os.rename(where / "lock-probe-moved", probe)
    return True


def test_rename_with_a_file_open_in_the_folder_is_refused_cleanly(
    tmp_path: Path,
) -> None:
    """The real lock, not a simulated one: a file in papers/<old>/ held open
    by this process while lit rename runs."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    if _folder_with_open_file_can_be_renamed(probe_dir):
        pytest.skip("this OS lets a folder with an open file be renamed")
    vault = _library_of_two(tmp_path)
    before = _library(vault)

    with (vault / "papers" / _OLD / "paper.pdf").open("rb"):
        result = _rename(vault)

    assert result.exit_code == 1
    assert isinstance(result.exception, RenameError)
    message = str(result.exception)
    assert "\n" not in message
    assert message.startswith(f"Could not rename papers/{_OLD}/ (")
    assert message.endswith("Close any file open from it, then retry.")
    assert _library(vault) == before
    assert _staging_is_empty(vault)


def test_rename_onto_a_dangling_link_is_refused_as_taken(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar")
    try:
        os.symlink(vault / "nowhere", vault / "papers" / "2024_Foo_Baz")
    except OSError:
        pytest.skip("this machine cannot create a symlink")

    result = CliRunner().invoke(
        cli, ["rename", "2024_Foo_Bar", "2024_Foo_Baz", "--library", str(vault)]
    )

    assert isinstance(result.exception, RenameError)
    assert "already exists" in str(result.exception)
    assert (vault / "papers" / "2024_Foo_Bar").is_dir()


# ===========================================================================
# The startup INDEX repair at a terminal
#
# Before every command, at a terminal, litman drops ids INDEX.json lists but
# papers/ no longer holds, and tags their links "(deleted)". A rename caught
# mid-move, or left half-done by an older litman, looks exactly like that,
# so the repair only reports while a write is unfinished or while a folder
# INDEX does not know is on disk.
# ===========================================================================


def _make_active(vault: Path) -> None:
    save_registry(
        VaultRegistry(vaults=[VaultEntry(name="lib", path=str(vault), is_active=True)])
    )


def _at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_drift, "_default_tty_probe", lambda: True)
    # A terminal also gets the once-a-day update check; keep it off the net.
    monkeypatch.setenv("LITMAN_NO_UPDATE_CHECK", "1")


def _notes(vault: Path) -> str:
    return "".join(
        p.read_text(encoding="utf-8")
        for p in sorted((vault / "papers").rglob("*.md"))
    )


def test_a_terminal_command_after_a_crash_mid_rename_tags_nothing_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _library_of_two(tmp_path)
    _make_active(vault)
    old_library = _library(vault)
    with monkeypatch.context() as m:
        _crash_at("after move", m)
        with pytest.raises(_Killed):
            _rename(vault)
    _at_a_terminal(monkeypatch)

    result = CliRunner().invoke(cli, ["list"])

    assert result.exit_code == 0, result.output
    assert _library(vault) == old_library
    assert "(deleted)" not in _notes(vault)
    # The recovery says what happened; the INDEX repair says nothing.
    assert "recovered: Undid an unfinished command" in result.stderr
    assert "INDEX.json lists" not in result.stderr


def test_a_terminal_command_leaves_index_alone_while_a_write_is_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with no stray folder: the recovery that runs after this repair
    settles the write, and only then is INDEX worth correcting."""
    vault = _library_of_two(tmp_path)
    _make_active(vault)
    locking.rmtree(vault / "papers" / _OTHER)
    leftover = StagedWrite(vault, op_id="op-unfinished")
    leftover.__enter__()
    leftover.write_text("INDEX.json", "{}\n")
    leftover._fsync_staged_files()
    leftover._write_manifest()
    _at_a_terminal(monkeypatch)

    def index_ids() -> list[str]:
        payload = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
        return sorted(p["id"] for p in payload["papers"])

    first = CliRunner().invoke(cli, ["list"])

    assert first.exit_code == 0, first.output
    assert index_ids() == [_OLD, _OTHER]
    assert "INDEX.json lists" not in first.stderr
    assert _staging_is_empty(vault)
    second = CliRunner().invoke(cli, ["list"])
    assert second.exit_code == 0, second.output
    assert index_ids() == [_OLD]


def test_a_terminal_command_tags_nothing_in_a_half_renamed_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Left by an older lit rename: metadata, links and INDEX say the new id,
    the folder kept the old name. Then --fix puts it right."""
    vault = _library_of_two(tmp_path)
    _make_active(vault)
    assert _rename(vault).exit_code == 0
    os.rename(vault / "papers" / _NEW, vault / "papers" / _OLD)
    before = _library(vault)
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(viewer_mod, "detect_platform_viewer", lambda: "open")

    listed = CliRunner().invoke(cli, ["list"])

    assert listed.exit_code == 0, listed.output
    assert _library(vault) == before
    assert "(deleted)" not in _notes(vault)
    assert (
        f"INDEX.json lists 1 paper(s) no longer on disk: {_NEW}."
        in " ".join(listed.stderr.split())
    )
    fixed = CliRunner().invoke(cli, ["health-check", "--fix"])
    assert fixed.exit_code == 0, fixed.output
    again = CliRunner().invoke(cli, ["health-check"])
    assert again.exit_code == 0, again.output
    assert "All checks passed" in again.output



# ===========================================================================
# A finished rename whose teardown stops partway
#
# Removing the op dir can stop partway: a file in it held open by another
# program (Windows), a crash. What is left must never be read as a folder
# move still to make or undo. _remove_tree is the module's one deletion seam.
# ===========================================================================


def _tear_down_all_but(m: pytest.MonkeyPatch, *kept: str) -> None:
    """Make every op-dir removal leave the named top-level files behind."""

    def remove(path: Path) -> None:
        for child in path.iterdir():
            if child.name in kept:
                continue
            if child.is_dir():
                locking.rmtree(child)
            else:
                child.unlink()

    m.setattr(atomic_mod, "_remove_tree", remove)


def _folders(vault: Path) -> dict[str, str]:
    return {
        p.name: _read_meta(vault, p.name)["id"]
        for p in sorted((vault / "papers").iterdir())
    }


def test_a_left_manifest_never_moves_a_renamed_folder_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_time(monkeypatch)
    ref_parent = tmp_path / "ref"
    ref_parent.mkdir()
    reference = _library_of_two(ref_parent)
    assert _rename(reference).exit_code == 0
    vault = _library_of_two(tmp_path)
    op_dir = vault / ".litman-staging" / f"rename-{_OLD}-to-{_NEW}"

    with monkeypatch.context() as m:
        _tear_down_all_but(m, "MANIFEST.json")
        assert _rename(vault).exit_code == 0
    manifest = json.loads((op_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    assert "dirs" not in manifest

    result = CliRunner().invoke(cli, ["list", "--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert not op_dir.exists()
    assert _library(vault) == _library(reference)


@pytest.mark.parametrize("stuck_for", ["every command", "the first rename"])
def test_a_left_commit_record_never_moves_a_folder_after_a_rename_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stuck_for: str
) -> None:
    """COMMITTED and MANIFEST.json cannot be deleted. Renaming back must not
    be followed by the first rename's folder move."""
    vault = _library_of_two(tmp_path)

    with monkeypatch.context() as m:
        _tear_down_all_but(m, "COMMITTED", "MANIFEST.json")
        assert _rename(vault).exit_code == 0
    if stuck_for == "every command":
        _tear_down_all_but(monkeypatch, "COMMITTED", "MANIFEST.json")
    assert _folders(vault) == {_NEW: _NEW, _OTHER: _OTHER}

    CliRunner().invoke(cli, ["list", "--library", str(vault)])
    assert _folders(vault) == {_NEW: _NEW, _OTHER: _OTHER}
    back = CliRunner().invoke(cli, ["rename", _NEW, _OLD, "--library", str(vault)])
    assert back.exit_code == 0, back.output
    assert _folders(vault) == {_OLD: _OLD, _OTHER: _OTHER}
    CliRunner().invoke(cli, ["list", "--library", str(vault)])
    assert _folders(vault) == {_OLD: _OLD, _OTHER: _OTHER}
    assert _read_meta(vault, _OTHER)["related"] == [_OLD]


def test_a_commit_record_that_stays_for_good_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COMMITTED and MANIFEST.json never go away. After a rename and a rename
    back, later commands report nothing and move nothing."""
    vault = _library_of_two(tmp_path)
    _tear_down_all_but(monkeypatch, "COMMITTED", "MANIFEST.json")
    assert _rename(vault).exit_code == 0
    back = CliRunner().invoke(cli, ["rename", _NEW, _OLD, "--library", str(vault)])
    assert back.exit_code == 0, back.output

    for _ in range(2):
        result = CliRunner().invoke(cli, ["list", "--library", str(vault)])
        assert result.exit_code == 0, result.output
        assert "error:" not in result.stderr
        assert _folders(vault) == {_OLD: _OLD, _OTHER: _OTHER}


def _kept_as_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A rename killed after its commit point, one of its files then lost on
    both sides: recovery moves the folder, then keeps the op as evidence."""
    vault = _library_of_two(tmp_path)
    with monkeypatch.context() as m:
        _crash_at("after commit", m)
        with pytest.raises(_Killed):
            _rename(vault)
    op_dir = vault / ".litman-staging" / f"rename-{_OLD}-to-{_NEW}"
    (op_dir / "papers" / _OTHER / "notes.md").unlink()
    (vault / "papers" / _OTHER / "notes.md").unlink()
    CliRunner().invoke(cli, ["list", "--library", str(vault)])
    assert op_dir.is_dir()
    return vault


def test_retrying_a_rename_whose_commit_is_kept_is_refused_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _kept_as_evidence(tmp_path, monkeypatch)
    os.rename(vault / "papers" / _NEW, vault / "papers" / _OLD)
    before = _library(vault)

    result = _rename(vault)

    assert result.exit_code == 1
    assert isinstance(result.exception, RenameError)
    assert str(result.exception) == (
        "An unfinished command still moves this paper's folder. "
        "Settle it with `lit health-check` first."
    )
    assert _library(vault) == before


def test_a_rename_is_refused_while_a_commit_record_cannot_be_read(
    tmp_path: Path,
) -> None:
    """Its folder moves cannot be read, so it may move either folder."""
    from litman.core.checks import check_stale_staging

    vault = _library_of_two(tmp_path)
    op_dir = vault / ".litman-staging" / "op-garbled"
    op_dir.mkdir()
    (op_dir / "MANIFEST.json").write_text("{not json", encoding="utf-8")
    (op_dir / "COMMITTED").write_bytes(b"")

    result = _rename(vault)

    assert isinstance(result.exception, RenameError)
    assert str(result.exception) == (
        "An unfinished command still moves this paper's folder. "
        "Settle it with `lit health-check` first."
    )
    # health-check names the op dir to settle.
    assert any(
        ".litman-staging/op-garbled/" in issue.hint
        for issue in check_stale_staging(vault, [])
    )
    assert (vault / "papers" / _OLD).is_dir()
