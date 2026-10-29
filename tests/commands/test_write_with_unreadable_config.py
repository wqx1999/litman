"""A write that lands while ``lit-config.yaml`` cannot be read.

Several commands save their change first and only then read the config, to
bring the project folders (``litman_reflib/``, ``litman_code/``,
``REFERENCES.md``) up to date. With the config broken that last step cannot
run. The change is saved all the same, so the command must finish — exit 0, its
usual success line — and say the project folders were not updated, and how to
catch up. The way out it names has to work: once the file is fixed,
``lit health-check`` reports the stale folders and ``--fix`` repairs them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import project_link as project_link_mod
from litman.core.checks import check_project_references
from litman.core.code import bind_paper_to_repo
from litman.core.config import CONFIG_FILENAME, load_config
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.portable_link import is_portable_link
from litman.core.project_link import add_project, link_paper_to_project

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False

WARNING = (
    "warning: lit-config.yaml cannot be read, so the project folders were not "
    "updated. Fix it, then run `lit health-check --fix`."
)
REFS_REFRESHED = "REFERENCES.md refreshed"
KINDS = ["unparsable", "invalid"]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@dataclass
class Library:
    vault: Path
    project_dir: Path
    source: Path  # a local folder for `lit code add` to import

    def meta(self, paper_id: str) -> dict:
        path = self.vault / "papers" / paper_id / "metadata.yaml"
        with path.open(encoding="utf-8") as f:
            return dict(_yaml.load(f))

    def references(self) -> str:
        path = self.project_dir / "litman_reflib" / "REFERENCES.md"
        return path.read_text(encoding="utf-8")

    def reflib_link(self, paper_id: str) -> bool:
        return is_portable_link(self.project_dir / "litman_reflib" / paper_id)

    def code_link(self, repo_name: str) -> bool:
        return is_portable_link(self.project_dir / "litman_code" / repo_name)


def _make_paper(vault: Path, paper_id: str) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    meta = {
        "id": paper_id,
        "title": "Test paper",
        "authors": ["Doe, Jane"],
        "year": 2024,
        "doi": f"10.test/{paper_id}",
        "status": "inbox",
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


def _make_repo(vault: Path, repo_name: str) -> None:
    repo_root = vault / "codes" / repo_name
    (repo_root / "repo").mkdir(parents=True)
    (repo_root / "repo" / "README.md").write_text("# fake\n", encoding="utf-8")
    (repo_root / "repo-meta.yaml").write_text(
        f"name: {repo_name}\nupstream: file:///fake\npapers: []\n",
        encoding="utf-8",
    )


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    """Project ``pepcodec`` with member ``p1``; ``p2`` is in no project.

    ``repo1`` is in the vault, bound to nothing; ``source/`` is a folder
    ``lit code add`` can import.
    """
    vault = create_vault(tmp_path)
    project_dir = tmp_path / "pepcodec_proj"
    project_dir.mkdir()
    add_project(vault, "pepcodec", project_dir)
    _make_paper(vault, "p1")
    _make_paper(vault, "p2")
    link_paper_to_project(vault, "p1", "pepcodec", load_config(vault).projects)
    _make_repo(vault, "repo1")
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("# source\n", encoding="utf-8")
    assert check_project_references(vault, list_papers(vault)) == []
    return Library(vault, project_dir, source)


def _damage_config(vault: Path, kind: str) -> bytes:
    """Break lit-config.yaml the way a hand edit would; return the good bytes."""
    path = vault / CONFIG_FILENAME
    good = path.read_bytes()
    if kind == "unparsable":
        path.write_text("library_name: [unclosed\n", encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write("no_such_setting: 1\n")
    return good


def _run(lib: Library, argv: list[str]) -> tuple[int, str, str]:
    """Exit code, output with one space between words (so a line Rich wrapped
    still matches), and what to show when the exit code is wrong."""
    result = CliRunner().invoke(cli, [*argv, "--library", str(lib.vault)])
    output = " ".join(result.output.split())
    return result.exit_code, output, f"{output} {result.exception!r}"


def _drift(lib: Library) -> list[str]:
    return [i.message for i in check_project_references(lib.vault, list_papers(lib.vault))]


# ---------------------------------------------------------------------------
# A paper in a project: the write finishes and says what it could not update
# ---------------------------------------------------------------------------


@dataclass
class Case:
    name: str
    argv: Callable[[Library], list[str]]
    success: str
    written: Callable[[Library], bool]
    repaired: Callable[[Library], bool]
    setup: Callable[[Library], object] | None = None


CASES = [
    Case(
        "modify-title",
        lambda lib: ["modify", "p1", "--set", "title=Brand New Title"],
        "✓ Modified p1",
        written=lambda lib: lib.meta("p1")["title"] == "Brand New Title",
        repaired=lambda lib: "Brand New Title" in lib.references(),
    ),
    Case(
        "modify-projects",
        lambda lib: ["modify", "p2", "--add-tag", "projects=pepcodec"],
        "✓ Modified p2",
        written=lambda lib: lib.meta("p2")["projects"] == ["pepcodec"],
        repaired=lambda lib: lib.reflib_link("p2") and "p2" in lib.references(),
    ),
    Case(
        "rename",
        lambda lib: ["rename", "p1", "p1-renamed"],
        "✓ Renamed p1 → p1-renamed",
        written=lambda lib: (lib.vault / "papers" / "p1-renamed").is_dir(),
        repaired=lambda lib: lib.reflib_link("p1-renamed") and not lib.reflib_link("p1"),
    ),
    Case(
        "code-link",
        lambda lib: ["code", "link", "repo1", "--paper", "p1"],
        "Linked: p1 ↔ repo1",
        written=lambda lib: lib.meta("p1")["code-clones"] == ["repo1"],
        repaired=lambda lib: lib.code_link("repo1"),
    ),
    Case(
        "code-unlink",
        lambda lib: ["code", "unlink", "repo1", "--paper", "p1"],
        "Unlinked: p1 ✕ repo1",
        written=lambda lib: lib.meta("p1")["code-clones"] == [],
        repaired=lambda lib: not lib.code_link("repo1"),
        setup=lambda lib: bind_paper_to_repo(lib.vault, "p1", "repo1"),
    ),
    Case(
        "code-add",
        lambda lib: ["code", "add", str(lib.source), "--name", "repo2", "--paper", "p1"],
        "Code added: repo2",
        written=lambda lib: (
            (lib.vault / "codes" / "repo2" / "repo").is_dir()
            and lib.meta("p1")["code-clones"] == ["repo2"]
        ),
        repaired=lambda lib: lib.code_link("repo2"),
    ),
    Case(
        "code-rm",
        lambda lib: ["code", "rm", "repo1", "--cascade", "--yes"],
        "Removed: repo1",
        written=lambda lib: (
            not (lib.vault / "codes" / "repo1").exists()
            and lib.meta("p1")["code-clones"] == []
        ),
        repaired=lambda lib: not lib.code_link("repo1"),
        setup=lambda lib: bind_paper_to_repo(lib.vault, "p1", "repo1"),
    ),
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_a_write_finishes_and_warns_when_the_config_cannot_be_read(
    lib: Library, case: Case, kind: str
) -> None:
    if case.setup is not None:
        case.setup(lib)
    good = _damage_config(lib.vault, kind)

    code, output, shown = _run(lib, case.argv(lib))

    assert code == 0, shown
    assert case.success in output
    assert WARNING in output
    # The project folders were left as they were, so no line may say otherwise.
    assert REFS_REFRESHED not in output
    assert case.written(lib)

    # The way out the warning names: with the file fixed, health-check sees
    # the stale project folders and --fix brings them up to date.
    (lib.vault / CONFIG_FILENAME).write_bytes(good)
    assert _drift(lib)
    CliRunner().invoke(cli, ["health-check", "--fix", "--library", str(lib.vault)])
    assert _drift(lib) == []
    assert case.repaired(lib)


def test_a_projects_change_with_a_readable_config_reports_the_refresh(lib: Library) -> None:
    """Control for ``REFS_REFRESHED not in output`` above: with the file intact
    the same command does print it, so its absence there means something."""
    code, output, shown = _run(lib, ["modify", "p2", "--add-tag", "projects=pepcodec"])

    assert code == 0, shown
    assert REFS_REFRESHED in output
    assert WARNING not in output


# ---------------------------------------------------------------------------
# A paper in no project: nothing was due, so nothing to warn about
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "argv",
    [
        ["modify", "p2", "--set", "title=Brand New Title"],
        ["code", "link", "repo1", "--paper", "p2"],
        ["rename", "p2", "p2-renamed"],
    ],
    ids=["modify-title", "code-link", "rename"],
)
def test_a_write_outside_any_project_does_not_warn(
    lib: Library, argv: list[str], kind: str
) -> None:
    _damage_config(lib.vault, kind)

    code, output, shown = _run(lib, argv)

    assert code == 0, shown
    assert "lit-config.yaml cannot be read, so the project folders" not in output


# ---------------------------------------------------------------------------
# `lit code rm --cascade`: a link that cannot be updated does not stop it
# ---------------------------------------------------------------------------


def test_code_rm_cascade_deletes_the_repo_when_a_project_link_fails(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bindings are committed before the links are touched, so a link
    failure must not leave the repo behind half-removed — the same rule
    ``lit code link`` / ``unlink`` follow."""
    bind_paper_to_repo(lib.vault, "p1", "repo1")

    def refuse(vault: Path, paper_id: str) -> dict:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(project_link_mod, "refresh_project_code_links", refuse)

    code, output, shown = _run(lib, ["code", "rm", "repo1", "--cascade", "--yes"])

    assert code == 0, shown
    assert "Removed: repo1" in output
    assert not (lib.vault / "codes" / "repo1").exists()
    assert lib.meta("p1")["code-clones"] == []
