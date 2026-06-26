"""Regression tests: ``lit modify`` keeps a project's REFERENCES.md in sync.

REFERENCES.md renders more than membership — it groups by ``priority`` and
prints each member's ``title``/``authors``/``year`` plus the per-project
``relevance-<project>`` annotation. Editing any of those on a *member* paper
used to leave the project's REFERENCES.md stale (the old gate only rebuilt on
a ``projects`` membership change), which surfaced as a ``project_references``
drift in ``lit health-check``. These tests pin the surgical fix: only the
affected paper's own projects are regenerated, and a non-member edit pays
nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.checks import check_project_references
from litman.core.config import load_config
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.project_link import add_project, link_paper_to_project

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_paper(
    vault: Path,
    paper_id: str,
    *,
    title: str = "Test paper",
    year: int | None = 2024,
    priority: str | None = "B",
) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": paper_id,
        "title": title,
        "authors": ["Doe, Jane"],
        "year": year,
        "doi": f"10.test/{paper_id}",
        "status": "inbox",
        "priority": priority,
        "type": "research",
        "projects": [],
        "topics": [],
        "methods": [],
        "code-clones": [],
        "created-at": "2026-05-11T10:00:00+02:00",
        "updated-at": "2026-05-11T10:00:00+02:00",
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        _yaml.dump(meta, f)


def _setup_linked(tmp_path: Path) -> tuple[Path, Path, str]:
    """Vault + registered project ``pepcodec`` with one linked member ``p1``.

    Returns ``(vault, refs_file, paper_id)``; REFERENCES.md exists and is in
    sync at this point.
    """
    vault = create_vault(tmp_path)
    project_dir = tmp_path / "pepcodec_proj"
    project_dir.mkdir()
    add_project(vault, "pepcodec", project_dir)
    _make_paper(vault, "p1", priority="B")
    registry = load_config(vault).projects
    link_paper_to_project(vault, "p1", "pepcodec", registry)
    refs_file = project_dir / "litman_reflib" / "REFERENCES.md"
    assert refs_file.is_file()
    # Sanity: freshly linked → no drift.
    assert check_project_references(vault, list_papers(vault)) == []
    return vault, refs_file, "p1"


def _drift_categories(vault: Path) -> list[str]:
    return [
        i.category for i in check_project_references(vault, list_papers(vault))
    ]


# ---------------------------------------------------------------------------
# Member edits on REFERENCES-rendered fields must regenerate REFERENCES.md
# ---------------------------------------------------------------------------


def test_priority_change_on_member_keeps_refs_in_sync(tmp_path: Path) -> None:
    vault, refs_file, paper_id = _setup_linked(tmp_path)

    result = CliRunner().invoke(
        cli, ["modify", paper_id, "--set", "priority=A", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output

    # The health check the user saw must now be clean...
    assert _drift_categories(vault) == []
    # ...and the file content actually reflects the new priority bucket.
    body = refs_file.read_text(encoding="utf-8")
    assert "## Priority A" in body
    assert "## Priority B" not in body


def test_title_change_on_member_keeps_refs_in_sync(tmp_path: Path) -> None:
    vault, refs_file, paper_id = _setup_linked(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["modify", paper_id, "--set", "title=Brand New Title", "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output

    assert _drift_categories(vault) == []
    assert "Brand New Title" in refs_file.read_text(encoding="utf-8")


def test_relevance_change_on_member_keeps_refs_in_sync(tmp_path: Path) -> None:
    vault, refs_file, paper_id = _setup_linked(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "modify",
            paper_id,
            "--set",
            "relevance-pepcodec=core baseline",
            "--library",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output

    assert _drift_categories(vault) == []
    assert "core baseline" in refs_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Cost guard: a non-member edit must NOT touch any project's REFERENCES.md
# ---------------------------------------------------------------------------


def test_nonmember_priority_change_does_not_touch_refs(tmp_path: Path) -> None:
    vault, refs_file, _ = _setup_linked(tmp_path)
    _make_paper(vault, "p2", priority="C")  # not linked to any project
    mtime_before = refs_file.stat().st_mtime_ns

    result = CliRunner().invoke(
        cli, ["modify", "p2", "--set", "priority=A", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output

    # p2 is in no project → the surgical path skips entirely; the project's
    # REFERENCES.md is not rewritten (no needless rebuild-all cost).
    assert refs_file.stat().st_mtime_ns == mtime_before
    assert _drift_categories(vault) == []
