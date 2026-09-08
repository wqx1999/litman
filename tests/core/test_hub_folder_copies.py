"""Project hub positions occupied by real folders instead of links.

Copying a project directory between machines (scp -r, WinSCP, unpacking a
tar, Explorer, cloud sync) expands every ``litman_reflib/<id>`` and
``litman_code/<repo>`` link into a real folder holding a full copy of the
vault entry. Until 1.3.6 the rebuild refused the position, printed one
``could not replace existing entry`` warning per folder, and left the user to
delete them by hand.

Covered here: :func:`settle_hub_entry` on its own (verbatim copy, differing
copy, no vault original, a junction, a drive that cannot hold links), then the
same states end-to-end through ``rebuild_all_project_links``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from litman.core.library import create_vault
from litman.core.portable_link import (
    is_portable_link,
    make_portable_link,
    remove_link_if_present,
)
from litman.core.project_link import (
    HubSettlement,
    rebuild_all_project_links,
    settle_hub_entry,
)


@pytest.fixture(autouse=True)
def _reset_link_probe_cache() -> Any:
    from litman.core.portable_link import reset_link_probe_cache

    reset_link_probe_cache()
    yield
    reset_link_probe_cache()


# --- scaffolding ------------------------------------------------------------


def _write_config_with_project(vault: Path, project: str, path: Path) -> None:
    (vault / "lit-config.yaml").write_text(
        f"library_name: {vault.name}\nprojects:\n  {project}: {path}\n",
        encoding="utf-8",
    )


def _make_paper(
    vault: Path,
    paper_id: str,
    *,
    projects: list[str],
    code_clones: list[str] | None = None,
) -> Path:
    from ruamel.yaml import YAML

    y = YAML()
    y.indent(mapping=2, sequence=4, offset=2)
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    meta = {
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
        "code-clones": list(code_clones or []),
        "created-at": "2026-05-11T10:00:00+02:00",
        "updated-at": "2026-05-11T10:00:00+02:00",
    }
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        y.dump(meta, f)
    (paper_dir / "notes.md").write_text("# Notes\n\nfirst\n", encoding="utf-8")
    return paper_dir


def _make_clone(vault: Path, repo_name: str) -> Path:
    """A minimal ``codes/<repo>/repo`` checkout, enough to link + compare."""
    repo = vault / "codes" / repo_name / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    (repo / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return repo


def _expand_to_copy(link_path: Path, source: Path) -> Path:
    """Replace a link with a real folder holding a copy of its target.

    What every copy tool does to a project directory; done by hand so the test
    reproduces the state on a machine where links DO work.
    """
    import shutil

    assert remove_link_if_present(link_path)
    shutil.copytree(source, link_path)
    assert not is_portable_link(link_path) and link_path.is_dir()
    return link_path


def _linked_project(
    tmp_path: Path, *, code_clones: list[str] | None = None
) -> tuple[Path, Path]:
    """A vault with one paper (optionally one repo) linked into one project."""
    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)
    project_dir = tmp_path / "pepforge"
    project_dir.mkdir()
    _write_config_with_project(vault, "pepforge", project_dir)
    for repo_name in code_clones or []:
        _make_clone(vault, repo_name)
    _make_paper(vault, "p1", projects=["pepforge"], code_clones=code_clones)
    rebuild_all_project_links(vault, {"pepforge": str(project_dir)})
    assert is_portable_link(project_dir / "litman_reflib" / "p1")
    return vault, project_dir


def _replaced_root(vault: Path, project: str, hub: str) -> Path:
    return vault / ".trash" / "replaced-folders" / project / hub


def _record_link_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect what ``core.portable_link`` says, free of Rich's word wrapping."""
    import litman.core.portable_link as portable_link

    said: list[str] = []

    class _RecordingConsole:
        def print(self, *args: object, **_kw: object) -> None:
            said.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(portable_link, "_console", _RecordingConsole())
    portable_link.reset_warning_state()
    return said


# --- settle_hub_entry, on its own -------------------------------------------


def test_settle_reports_clear_for_absent_link_and_file(tmp_path: Path) -> None:
    """Nothing to settle: an empty position, a live link, a real file."""
    vault = tmp_path / "vault"
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()

    absent = settle_hub_entry(
        hub / "nope", target, vault=vault, project="p", hub="litman_reflib"
    )
    assert absent == HubSettlement("clear", None)

    (hub / "afile").write_text("x", encoding="utf-8")
    a_file = settle_hub_entry(
        hub / "afile", target, vault=vault, project="p", hub="litman_reflib"
    )
    assert a_file == HubSettlement("clear", None)
    assert (hub / "afile").is_file()  # decision #4: the upsert still unlinks it

    assert make_portable_link(hub / "alink", target)
    a_link = settle_hub_entry(
        hub / "alink", target, vault=vault, project="p", hub="litman_reflib"
    )
    assert a_link == HubSettlement("clear", None)
    assert is_portable_link(hub / "alink")


def test_settle_leaves_a_junction_alone(
    tmp_path: Path, fake_junction: Any
) -> None:
    """A junction answers ``is_dir()`` True — predicate order is load-bearing.

    Checking ``is_dir()`` before ``is_portable_link()`` would file every
    healthy Windows link as a folder copy and move the whole hub into .trash/.
    """
    vault = tmp_path / "vault"
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    junction = fake_junction(hub / "p1")

    out = settle_hub_entry(
        junction, None, vault=vault, project="p", hub="litman_reflib"
    )
    assert out == HubSettlement("clear", None)
    assert junction.is_dir()


def test_settle_deletes_a_verbatim_copy(tmp_path: Path) -> None:
    import shutil

    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "papers" / "p1"
    (target / "sub").mkdir(parents=True)
    (target / "metadata.yaml").write_text("id: p1\n", encoding="utf-8")
    (target / "sub" / "notes.md").write_text("hello\n", encoding="utf-8")
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    shutil.copytree(target, hub / "p1")

    out = settle_hub_entry(
        hub / "p1", target, vault=vault, project="p", hub="litman_reflib"
    )
    assert out == HubSettlement("replaced-copy", None)
    assert not (hub / "p1").exists()
    assert (target / "metadata.yaml").is_file()  # the original is untouched
    assert not (vault / ".trash").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda copy: (copy / "metadata.yaml").write_text(
                "id: p1\nnote: edited\n", encoding="utf-8"
            ),
            id="content-differs",
        ),
        pytest.param(
            lambda copy: (copy / "extra.md").write_text("mine\n", encoding="utf-8"),
            id="extra-file",
        ),
        pytest.param(
            lambda copy: (copy / "sub" / "notes.md").unlink(),
            id="missing-file",
        ),
    ],
)
def test_settle_moves_a_differing_copy_aside(
    tmp_path: Path, mutate: Any
) -> None:
    import shutil

    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "papers" / "p1"
    (target / "sub").mkdir(parents=True)
    (target / "metadata.yaml").write_text("id: p1\n", encoding="utf-8")
    (target / "sub" / "notes.md").write_text("hello\n", encoding="utf-8")
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    copy = hub / "p1"
    shutil.copytree(target, copy)
    mutate(copy)

    out = settle_hub_entry(
        copy, target, vault=vault, project="pepforge", hub="litman_reflib"
    )
    assert out.verdict == "moved-aside"
    assert out.moved_to is not None
    assert not copy.exists()
    assert out.moved_to.is_dir()
    assert out.moved_to.parent == _replaced_root(vault, "pepforge", "litman_reflib")
    assert out.moved_to.name.startswith("p1-")
    assert (target / "metadata.yaml").is_file()


def test_settle_moves_aside_when_there_is_no_vault_original(
    tmp_path: Path,
) -> None:
    """No original to compare against ⇒ never provably redundant ⇒ preserved."""
    vault = tmp_path / "vault"
    vault.mkdir()
    hub = tmp_path / "proj" / "litman_code"
    hub.mkdir(parents=True)
    orphan = hub / "someRepo"
    orphan.mkdir()
    (orphan / "README.md").write_text("orphan\n", encoding="utf-8")

    out = settle_hub_entry(
        orphan, None, vault=vault, project="pepforge", hub="litman_code"
    )
    assert out.verdict == "moved-aside"
    assert out.moved_to is not None
    assert not orphan.exists()
    assert (out.moved_to / "README.md").read_text(encoding="utf-8") == "orphan\n"


def test_settle_disambiguates_a_second_move_of_the_same_name(
    tmp_path: Path,
) -> None:
    """Two settlements inside one second must not clobber each other."""
    vault = tmp_path / "vault"
    vault.mkdir()
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)

    landed: list[Path] = []
    for text in ("first\n", "second\n"):
        copy = hub / "p1"
        copy.mkdir()
        (copy / "notes.md").write_text(text, encoding="utf-8")
        out = settle_hub_entry(
            copy, None, vault=vault, project="pepforge", hub="litman_reflib"
        )
        assert out.moved_to is not None
        landed.append(out.moved_to)

    assert landed[0] != landed[1]
    assert (landed[0] / "notes.md").read_text(encoding="utf-8") == "first\n"
    assert (landed[1] / "notes.md").read_text(encoding="utf-8") == "second\n"


def test_settle_leaves_the_copy_alone_when_links_are_impossible(
    tmp_path: Path,
) -> None:
    """AC-11 (helper half): on a drive that cannot hold links, deleting the
    copy would take away the only browsable thing and give nothing back."""
    from litman.core.portable_link import (
        _LINK_MECHANISM,
        reset_link_probe_cache,
    )

    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "papers" / "p1"
    target.mkdir(parents=True)
    (target / "metadata.yaml").write_text("id: p1\n", encoding="utf-8")
    project_dir = tmp_path / "proj"
    hub = project_dir / "litman_reflib"
    hub.mkdir(parents=True)
    copy = hub / "p1"
    copy.mkdir()
    (copy / "metadata.yaml").write_text("id: p1\n", encoding="utf-8")

    reset_link_probe_cache()
    _LINK_MECHANISM[str(project_dir)] = "none"

    out = settle_hub_entry(
        copy, target, vault=vault, project="pepforge", hub="litman_reflib"
    )
    assert out == HubSettlement("blocked", None)
    assert (copy / "metadata.yaml").is_file()
    assert not (vault / ".trash").exists()


def test_settle_keeps_a_copy_whose_extra_content_is_a_link(
    tmp_path: Path,
) -> None:
    """Links inside a tree are skipped by the shape walk, so a copy carrying
    one extra symlink still reads as verbatim — deliberate: the vault entry
    holds none, and a link is not content we can compare."""
    import shutil

    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "papers" / "p1"
    target.mkdir(parents=True)
    (target / "metadata.yaml").write_text("id: p1\n", encoding="utf-8")
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    copy = hub / "p1"
    shutil.copytree(target, copy)
    assert make_portable_link(copy / "elsewhere", target)

    out = settle_hub_entry(
        copy, target, vault=vault, project="pepforge", hub="litman_reflib"
    )
    assert out.verdict == "replaced-copy"
    assert not copy.exists()


# --- end to end through rebuild_all_project_links ---------------------------


def test_verbatim_reflib_copy_becomes_a_link_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the whole Windows report — a dozen expanded folders — heals with
    no warning and nothing left for the user to delete."""
    vault, project_dir = _linked_project(tmp_path)
    link = project_dir / "litman_reflib" / "p1"
    _expand_to_copy(link, vault / "papers" / "p1")
    said = _record_link_warnings(monkeypatch)

    out = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    assert is_portable_link(link)
    assert (link / "notes.md").read_text(encoding="utf-8") == "# Notes\n\nfirst\n"
    assert out["pepforge"]["n_replaced_copies"] == 1
    assert out["pepforge"]["n_moved_aside"] == 0
    assert out["pepforge"]["aside_paths"] == []
    assert out["pepforge"]["n_paper_links"] == 1
    assert said == []
    assert not (vault / ".trash" / "replaced-folders").exists()


def test_differing_copy_is_moved_to_replaced_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a note written into the copy is a dead end nobody else holds —
    it goes to .trash/, not to /dev/null."""
    vault, project_dir = _linked_project(tmp_path)
    link = project_dir / "litman_reflib" / "p1"
    copy = _expand_to_copy(link, vault / "papers" / "p1")
    (copy / "notes.md").write_text(
        "# Notes\n\nfirst\nwritten on the other machine\n", encoding="utf-8"
    )
    said = _record_link_warnings(monkeypatch)

    out = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    assert is_portable_link(link)
    assert (link / "notes.md").read_text(encoding="utf-8") == "# Notes\n\nfirst\n"
    assert out["pepforge"]["n_replaced_copies"] == 0
    assert out["pepforge"]["n_moved_aside"] == 1
    aside = Path(out["pepforge"]["aside_paths"][0])
    assert aside.parent == _replaced_root(vault, "pepforge", "litman_reflib")
    assert aside.name.startswith("p1-")
    assert (aside / "notes.md").read_text(encoding="utf-8") == (
        "# Notes\n\nfirst\nwritten on the other machine\n"
    )
    assert (aside / "metadata.yaml").is_file()
    assert said == []


def test_orphan_copy_with_no_vault_original_is_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a folder for a paper that no longer exists — no membership, no
    vault entry. Nothing else would ever look at it again."""
    vault, project_dir = _linked_project(tmp_path)
    orphan = project_dir / "litman_reflib" / "2020_Gone_Paper"
    orphan.mkdir()
    (orphan / "notes.md").write_text("stale\n", encoding="utf-8")
    said = _record_link_warnings(monkeypatch)

    out = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    assert not orphan.exists()
    assert out["pepforge"]["n_moved_aside"] == 1
    aside = Path(out["pepforge"]["aside_paths"][0])
    assert aside.name.startswith("2020_Gone_Paper-")
    assert (aside / "notes.md").read_text(encoding="utf-8") == "stale\n"
    assert said == []


def test_code_hub_copy_replaced_and_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: litman_code/<repo> settles against codes/<repo>/repo the same way."""
    vault, project_dir = _linked_project(tmp_path, code_clones=["diffdock"])
    clone = vault / "codes" / "diffdock" / "repo"
    code_link = project_dir / "litman_code" / "diffdock"
    assert is_portable_link(code_link)

    _expand_to_copy(code_link, clone)
    said = _record_link_warnings(monkeypatch)
    out = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    assert is_portable_link(code_link)
    assert out["pepforge"]["n_replaced_copies"] == 1
    assert out["pepforge"]["n_moved_aside"] == 0
    assert out["pepforge"]["n_code_links"] == 1
    assert said == []

    _expand_to_copy(code_link, clone)
    (code_link / "src" / "main.py").write_text("print('mine')\n", encoding="utf-8")
    out2 = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    assert is_portable_link(code_link)
    assert out2["pepforge"]["n_moved_aside"] == 1
    aside = Path(out2["pepforge"]["aside_paths"][0])
    assert aside.parent == _replaced_root(vault, "pepforge", "litman_code")
    assert (aside / "src" / "main.py").read_text(encoding="utf-8") == (
        "print('mine')\n"
    )
    assert said == []


def test_references_md_survives_the_settling(tmp_path: Path) -> None:
    """REFERENCES.md is a real FILE living in the hub; settling must not move
    it (decision #4 — only folders are settled)."""
    vault, project_dir = _linked_project(tmp_path)
    refs = project_dir / "litman_reflib" / "REFERENCES.md"
    assert refs.is_file()

    rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    assert refs.is_file()
    assert not _replaced_root(vault, "pepforge", "litman_reflib").exists()


def test_skipped_project_still_reports_the_new_counters(tmp_path: Path) -> None:
    """Both result arms carry the same keys so consumers need no .get() dance."""
    parent = tmp_path / "vault_parent"
    parent.mkdir()
    vault = create_vault(parent)

    out = rebuild_all_project_links(
        vault, {"ghost": str(tmp_path / "not_there")}
    )

    assert out["ghost"]["status"] == "skipped"
    assert out["ghost"]["n_replaced_copies"] == 0
    assert out["ghost"]["n_moved_aside"] == 0
    assert out["ghost"]["aside_paths"] == []


def test_copy_untouched_when_project_drive_cannot_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-11: on a drive that cannot hold links the copy is the only browsable
    thing there is. Leave it, and stop telling the user to delete it."""
    from litman.core.checks import check_project_references
    from litman.core.document import list_papers
    from litman.core.portable_link import (
        _LINK_MECHANISM,
        reset_link_probe_cache,
    )

    vault, project_dir = _linked_project(tmp_path)
    link = project_dir / "litman_reflib" / "p1"
    copy = _expand_to_copy(link, vault / "papers" / "p1")
    said = _record_link_warnings(monkeypatch)

    reset_link_probe_cache()
    _LINK_MECHANISM[str(vault)] = "symlink"
    _LINK_MECHANISM[str(project_dir)] = "none"

    out = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    assert copy.is_dir() and not is_portable_link(copy)
    assert (copy / "metadata.yaml").is_file()
    assert out["pepforge"]["n_replaced_copies"] == 0
    assert out["pepforge"]["n_moved_aside"] == 0
    assert not (vault / ".trash" / "replaced-folders").exists()
    assert "could not replace existing entry" not in "\n".join(said)
    assert check_project_references(vault, list_papers(vault)) == []
