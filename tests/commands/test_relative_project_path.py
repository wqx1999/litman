"""A ``projects:`` value in lit-config.yaml that is not a full path.

litman 1.3.10's drift prompt could write one, and so can a hand edit. It counts as
missing whatever the current directory holds: reading it as it stands would judge,
and write into, a folder that merely has that name where the command was run.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

import click
import pytest
from click.testing import CliRunner, Result
from ruamel.yaml import YAML

from litman.cli import cli
from litman.commands import _drift
from litman.core.checks import projects as project_checks
from litman.core.config import project_probe_paths, stored_project_dir
from litman.core.correctors import reconcile_derived
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.portable_link import is_portable_link, make_portable_link
from litman.core.project_link import (
    find_dangling_bridges,
    rebuild_all_project_links,
    refresh_project_code_links,
)
from litman.core.project_refs import rebuild_all_project_refs
from litman.core.vault_registry import VaultEntry, VaultRegistry, save_registry
from litman.server import create_app
from tests.server._client import TestClient

STORED = "xcxcxc"
PAPER = "2024_Foo_Bar"
OTHER_PAPER = "2024_Foo_Baz"


def _write_paper(vault: Path, paper_id: str, projects: list[str]) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        YAML().dump(
            {
                "id": paper_id,
                "title": "Test paper",
                "authors": ["Doe, Jane"],
                "year": 2024,
                "doi": f"10.test/{paper_id}",
                "status": "inbox",
                "type": "research",
                "projects": projects,
                "topics": [],
                "methods": [],
                "code-clones": [],
                "created-at": "2026-05-11T10:00:00+02:00",
                "updated-at": "2026-05-11T10:00:00+02:00",
            },
            f,
        )


class Stray:
    """A vault with project ``p1`` stored as ``xcxcxc``, run from a directory."""

    def __init__(self, vault: Path, cwd: Path) -> None:
        self.vault = vault
        self.cwd = cwd

    def tree(self) -> dict[str, tuple[bool, bytes | None]]:
        return {
            str(p.relative_to(self.cwd)): (
                is_portable_link(p),
                p.read_bytes() if p.is_file() else None,
            )
            for p in self.cwd.rglob("*")
        }

    def lit(self, *args: str) -> Result:
        return CliRunner().invoke(cli, [*args, "--library", str(self.vault)])


@pytest.fixture(
    params=[True, False], ids=["same-name-folder-here", "no-such-folder-here"]
)
def stray(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Stray:
    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    save_registry(
        VaultRegistry(vaults=[VaultEntry(name="v", path=str(vault), is_active=True)])
    )
    real = tmp_path / "real"
    real.mkdir()
    added = CliRunner().invoke(
        cli, ["project", "add", "p1", "--path", str(real), "--library", str(vault)]
    )
    assert added.exit_code == 0, added.output
    config_file = vault / "lit-config.yaml"
    yaml = YAML()
    config = yaml.load(config_file.read_text(encoding="utf-8"))
    config["projects"]["p1"] = STORED
    with config_file.open("w", encoding="utf-8") as f:
        yaml.dump(config, f)
    _write_paper(vault, PAPER, ["p1"])
    _write_paper(vault, OTHER_PAPER, [])
    reconcile_derived(vault, project_refs=False)

    cwd = tmp_path / "somewhere"
    cwd.mkdir()
    if request.param:
        # What the commands would touch if they took the value as it stands.
        hub = cwd / STORED / "litman_reflib"
        hub.mkdir(parents=True)
        (hub / "REFERENCES.md").write_text("mine\n", encoding="utf-8")
        target = tmp_path / "link_target"
        target.mkdir()
        make_portable_link(hub / PAPER, target)
    monkeypatch.chdir(cwd)
    return Stray(vault, cwd)


def test_project_list_says_path_missing(stray: Stray) -> None:
    listed = json.loads(stray.lit("project", "list", "--format", "json").stdout)
    assert listed == [{"name": "p1", "path": STORED, "status": "path-missing"}]


def test_gui_project_list_says_path_missing(stray: Stray) -> None:
    response = TestClient(create_app(stray.vault)).get("/api/projects")
    assert response.json() == [
        {"name": "p1", "path": STORED, "status": "path-missing"}
    ]


def test_health_check_reports_it(stray: Stray) -> None:
    for issues in (
        project_checks.check_project_path_exists(stray.vault, []),
        # The hook's shared probe never held this value: still a finding.
        project_checks.check_project_path_exists(stray.vault, [], exists_status={}),
    ):
        assert [i.category for i in issues] == ["project_path_exists"]
        assert STORED in issues[0].message


def test_no_bounded_probe_is_ever_given_it(
    stray: Stray, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It takes no stat to know: the current directory never gets asked."""
    # By name: another test can purge and reimport the modules.
    drift = importlib.import_module("litman.commands._drift")
    real = drift._exists_bounded
    probed: list[str] = []

    def spy(paths: list[str], budget_s: float = 0.5) -> dict[str, bool | None]:
        probed.extend(paths)
        return real(paths, budget_s)

    monkeypatch.setattr(drift, "_exists_bounded", spy)
    # The control: a project stored properly is probed on each of these paths.
    proper = tmp_path / "proper"
    proper.mkdir()
    assert stray.lit("project", "add", "proper", "--path", str(proper)).exit_code == 0

    def seen_in(act: Callable[[], object]) -> list[str]:
        probed.clear()
        act()
        assert str(proper.resolve()) in probed
        return [p for p in probed if Path(p).name == STORED]

    client = TestClient(create_app(stray.vault))
    assert seen_in(lambda: stray.lit("project", "list")) == []
    assert seen_in(lambda: drift.check_and_prompt_bridge_drift(stdin_is_tty=lambda: False)) == []
    assert seen_in(lambda: project_checks.check_project_path_exists(stray.vault, [])) == []
    assert seen_in(lambda: project_checks.check_project_references(stray.vault, [])) == []
    assert seen_in(lambda: project_checks.check_project_bridge_dangling(stray.vault, [])) == []
    assert seen_in(lambda: client.put("/api/vaults/active", json={"name": "v"})) == []
    assert seen_in(
        lambda: client.put("/api/vaults/v/path", json={"path": str(stray.vault)})
    ) == []


def test_checks_about_the_folder_itself_leave_it_alone(
    stray: Stray, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_at: list[Path] = []

    def spy(path: Path) -> bool:
        looked_at.append(Path(path))
        return True

    monkeypatch.setattr(project_checks, "links_supported", spy)
    papers = list_papers(stray.vault)
    assert project_checks.check_project_references(stray.vault, papers) == []
    assert project_checks.check_link_support(stray.vault, []) == []
    assert looked_at and [p for p in looked_at if p.name == STORED] == []


def test_a_dangling_link_in_the_same_name_folder_is_not_a_finding(
    stray: Stray, tmp_path: Path
) -> None:
    target = tmp_path / "link_target"
    if target.exists():
        target.rmdir()
    here = str(Path(STORED).resolve())
    status: dict[str, bool | None] = {STORED: True, here: True}
    gone = find_dangling_bridges(
        {"p1": STORED}, status, lambda paths: dict.fromkeys(paths, False)
    )
    assert gone == {}
    assert project_checks.check_project_bridge_dangling(stray.vault, []) == []


@pytest.mark.parametrize("on_a_terminal", [True, False])
def test_drift_prompt_asks_without_a_stat(
    stray: Stray,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    on_a_terminal: bool,
) -> None:
    probed: list[str] = []

    def exists(paths: list[str]) -> dict[str, bool | None]:
        probed.extend(paths)
        return _drift._exists_bounded(paths)

    asked: list[str] = []

    def answer(question: str, **kwargs: object) -> str:
        asked.append(question)
        return ""

    monkeypatch.setattr(click, "prompt", answer)
    before = (stray.vault / "lit-config.yaml").read_bytes()

    _drift.check_and_prompt_project_drift(
        stdin_is_tty=lambda: on_a_terminal, exists_fn=exists
    )

    captured = capsys.readouterr()
    assert len(asked) == (1 if on_a_terminal else 0)
    assert STORED in (captured.out if on_a_terminal else captured.err)
    assert [p for p in probed if Path(p).name == STORED] == []
    assert (stray.vault / "lit-config.yaml").read_bytes() == before


@pytest.mark.parametrize(
    ("command", "goes_through"),
    [
        (("refresh-views",), True),
        (("health-check", "--fix"), False),
        (("link", OTHER_PAPER, "--project", "p1"), False),
        (("unlink", PAPER, "--project", "p1"), False),
        (("modify", PAPER, "--set", "title=Another title"), True),
        (("project", "rename", "p1", "p2"), True),
    ],
    ids=["refresh-views", "health-check-fix", "link", "unlink", "modify", "rename"],
)
def test_commands_write_nothing_into_the_same_name_folder(
    stray: Stray, command: tuple[str, ...], goes_through: bool
) -> None:
    before = stray.tree()
    result = stray.lit(*command)
    assert (result.exit_code == 0) is goes_through, result.output
    assert stray.tree() == before


def test_project_rm_tears_nothing_down_in_the_same_name_folder(stray: Stray) -> None:
    before = stray.tree()
    result = stray.lit("project", "rm", "p1", "--yes")
    assert result.exit_code == 0, result.output
    assert stray.tree() == before


def test_project_rm_warning_names_no_folder_it_would_not_touch(
    stray: Stray, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_cmd = importlib.import_module("litman.commands.project")
    warned: list[str] = []

    def decline(lines: list[str], *, yes: bool) -> bool:
        warned.extend(lines)
        return False

    monkeypatch.setattr(project_cmd, "_confirm_destructive", decline)
    project_cmd.project_rm_cmd.callback(
        name="p1", yes=False, library=stray.vault, vault_name=None
    )
    assert any("lit-config.yaml" in line for line in warned)
    assert not any("litman_reflib" in line for line in warned)


def test_paper_rm_and_restore_touch_nothing_in_the_same_name_folder(
    stray: Stray,
) -> None:
    before = stray.tree()
    assert stray.lit("rm", PAPER, "--yes").exit_code == 0
    assert not (stray.vault / "papers" / PAPER).exists()
    assert stray.tree() == before
    assert stray.lit("trash", "restore", PAPER).exit_code == 0
    assert (stray.vault / "papers" / PAPER).exists()
    assert stray.tree() == before


def test_rebuilds_skip_it_and_leave_the_config_as_written(stray: Stray) -> None:
    config_before = (stray.vault / "lit-config.yaml").read_bytes()
    tree_before = stray.tree()
    registry = {"p1": STORED}

    assert rebuild_all_project_links(stray.vault, registry)["p1"]["status"] == "skipped"
    assert rebuild_all_project_refs(stray.vault, registry)["p1"]["status"] == "skipped"
    assert refresh_project_code_links(stray.vault, PAPER) == {}
    assert reconcile_derived(stray.vault, project_refs=True)["link_errors"] == []

    assert stray.tree() == tree_before
    assert (stray.vault / "lit-config.yaml").read_bytes() == config_before


@pytest.mark.parametrize("stored", ["", "xcxcxc", "sub/dir", "./x", "../x"])
def test_stored_values_that_are_not_full_paths(stored: str) -> None:
    assert stored_project_dir(stored) is None
    assert project_probe_paths({"p": stored}) == []


def test_stored_full_path_and_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert stored_project_dir(str(tmp_path)) == tmp_path
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert stored_project_dir("~/moved") == tmp_path / "moved"
    assert project_probe_paths({"a": "~/moved", "b": "x"}) == [str(tmp_path / "moved")]


@pytest.mark.skipif(os.name == "nt", reason="Windows guesses a home folder for ~user")
def test_stored_home_of_nobody_here() -> None:
    assert stored_project_dir("~no_such_user_here/x") is None


@pytest.mark.parametrize(
    ("stored", "absolute"),
    [
        ("C:\\work\\p", True),
        ("C:/work/p", True),
        ("\\\\server\\share\\p", True),
        ("\\work\\p", False),
        ("/work/p", False),
        ("C:work", False),
        ("work", False),
    ],
)
def test_windows_meaning_of_a_full_path(stored: str, absolute: bool) -> None:
    assert PureWindowsPath(stored).is_absolute() is absolute
    if os.name == "nt":
        assert (stored_project_dir(stored) is not None) is absolute
