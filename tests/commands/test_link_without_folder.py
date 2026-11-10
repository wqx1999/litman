"""A project with no folder on this machine still takes links: the link is the paper's metadata.

Two-way sync carries TAXONOMY.md and never lit-config.yaml, so a project
added on computer A reaches computer B listed but with no folder. Linking a
paper to it on B used to be refused, in the GUI and in ``lit link`` alike, so
B could neither link nor unlink that project's papers. Now the paper's record
changes and nothing else does on B; A, which has the folder, builds the link
and the REFERENCES.md row at its next sync. A project in neither file, and a
registered folder that is gone, are still refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import locking
from litman.core.checks.registry import run_all_checks
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.portable_link import is_portable_link
from litman.core.project_link import (
    LinkError,
    add_project,
    link_paper_to_project,
    unlink_paper_from_project,
)
from litman.core.project_refs import LITERATURE_SUBDIR, REFERENCES_FILENAME
from tests.commands.test_sync_bisync import (
    _sync,
    _two_synced_machines,
    cloud,
    needs_rclone,
    tmp_path,
)

__all__ = ["cloud", "tmp_path"]  # fixtures, used by name

_yaml = YAML(typ="safe")


def _listed_without_folder(vault: Path, project: str) -> None:
    """TAXONOMY.md lists ``project``; this machine's lit-config.yaml has no folder for it."""
    taxonomy = vault / "TAXONOMY.md"
    text = taxonomy.read_text(encoding="utf-8")
    assert "## projects\n\n(empty)\n" in text
    locking.unlock_truth_file(taxonomy)
    taxonomy.write_text(
        text.replace("## projects\n\n(empty)\n", f"## projects\n\n- {project}\n"),
        encoding="utf-8",
    )
    locking.lock_truth_file(taxonomy)


def _paper(vault: Path, paper_id: str = "2024_Foo_Bar") -> str:
    folder = vault / "papers" / paper_id
    folder.mkdir(parents=True)
    (folder / "metadata.yaml").write_text(
        f"id: {paper_id}\ntitle: Foo Bar\nauthors:\n  - Foo, Alice\nyear: 2024\n"
        "status: inbox\nprojects: []\n",
        encoding="utf-8",
    )
    return paper_id


def _meta(vault: Path, paper_id: str) -> dict[str, object]:
    data = _yaml.load((vault / "papers" / paper_id / "metadata.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    return create_vault(tmp_path / "home")


def test_linking_writes_the_record_and_touches_no_folder(vault: Path, tmp_path: Path) -> None:
    paper = _paper(vault)
    _listed_without_folder(vault, "thesis")
    before = sorted(p.name for p in tmp_path.iterdir())

    summary = link_paper_to_project(vault, paper, "thesis", {}, priority="A")

    assert summary["no_folder"] is True and summary["project_dir"] is None
    meta = _meta(vault, paper)
    assert meta["projects"] == ["thesis"]
    assert meta["priority-thesis"] == "A"
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # no folder made anywhere

    summary = unlink_paper_from_project(vault, paper, "thesis", {})

    assert summary["no_folder"] is True
    meta = _meta(vault, paper)
    assert meta["projects"] == [] and "priority-thesis" not in meta


def test_still_refused_a_project_in_neither_file_or_a_folder_gone(
    vault: Path, tmp_path: Path
) -> None:
    paper = _paper(vault)

    with pytest.raises(LinkError, match="not registered") as unknown:
        link_paper_to_project(vault, paper, "nowhere", {})
    assert unknown.value.code == "project-gone"

    gone = tmp_path / "gone"
    with pytest.raises(LinkError, match="does not exist on this machine") as missing:
        link_paper_to_project(vault, paper, "thesis", {"thesis": str(gone)})
    assert missing.value.code == "project-path-missing"
    # The way out is a command, never a hand edit of lit-config.yaml.
    assert 'Run `lit project set-path thesis "<new-path>"`' in str(missing.value)
    assert "lit-config.yaml" not in str(missing.value)
    assert _meta(vault, paper)["projects"] == []


def test_lit_link_and_unlink_say_only_the_record_changed(vault: Path) -> None:
    paper = _paper(vault)
    _listed_without_folder(vault, "thesis")

    linked = CliRunner().invoke(
        cli, ["link", paper, "--project", "thesis", "--library", str(vault)]
    )
    assert linked.exit_code == 0, linked.output
    said = " ".join(linked.output.split())
    assert "has no folder on this machine, so only the paper's record changed" in said
    assert "lit project set-path" in said
    assert "Project dir:" not in said

    unlinked = CliRunner().invoke(
        cli, ["unlink", paper, "--project", "thesis", "--library", str(vault)]
    )
    assert unlinked.exit_code == 0, unlinked.output
    said = " ".join(unlinked.output.split())
    assert "has no folder on this machine, so only the paper's record changed" in said
    assert "REFERENCES.md:" not in said


def test_the_gui_links_it_too(vault: Path) -> None:
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    paper = _paper(vault)
    _listed_without_folder(vault, "thesis")
    client = TestClient(create_app(vault))

    assert client.post(f"/api/paper/{paper}/project", json={"project": "thesis"}).status_code == 200
    assert _meta(vault, paper)["projects"] == ["thesis"]
    assert client.delete(f"/api/paper/{paper}/project/thesis").status_code == 200
    assert _meta(vault, paper)["projects"] == []


@needs_rclone
def test_the_computer_with_the_folder_builds_the_link_at_its_next_sync(
    tmp_path: Path, cloud: Path
) -> None:
    a, b = _two_synced_machines(tmp_path, cloud, n_papers=1)
    folder = tmp_path / "thesis-on-a"
    folder.mkdir()
    add_project(a, "thesis", folder)
    assert _sync(a).exit_code == 0
    assert _sync(b).exit_code == 0  # B now lists thesis, with no folder of its own
    checked_before = {(i.category, i.message) for i in run_all_checks(b, list_papers(b))}

    linked = CliRunner().invoke(cli, ["link", "p1", "--project", "thesis", "--library", str(b)])

    assert linked.exit_code == 0, linked.output
    # B's health check finds nothing new: the linked project is just a listed one.
    checked_after = {(i.category, i.message) for i in run_all_checks(b, list_papers(b))}
    assert checked_after == checked_before
    assert not (folder / LITERATURE_SUBDIR / "p1").exists()

    assert _sync(b).exit_code == 0
    assert _sync(a).exit_code == 0

    assert _meta(a, "p1")["projects"] == ["thesis"]
    assert is_portable_link(folder / LITERATURE_SUBDIR / "p1")
    references = (folder / LITERATURE_SUBDIR / REFERENCES_FILENAME).read_text(encoding="utf-8")
    assert "[[p1]]" in references
