"""Project hub positions occupied by real folders instead of links.

Copying a project directory between machines (scp -r, WinSCP, unpacking a
tar, Explorer, cloud sync) expands every ``litman_reflib/<id>`` and
``litman_code/<repo>`` link into a real folder holding a full copy of the
vault entry. The rebuild used to refuse the position, print one
``could not replace existing entry`` warning per folder, and leave the user to
delete them by hand.

Covered here: :func:`settle_hub_entry` on its own (verbatim copy, differing
copy, no vault original, a junction, a drive that cannot hold links), then the
same states end-to-end through ``rebuild_all_project_links``.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

import litman.core.project_link as project_link
from litman.core.library import create_vault
from litman.core.locking import rmtree as _real_rmtree
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

# Permission bits only mean what these tests need them to mean on POSIX, and
# only for a user who is not root. Captured once, at import (never by patching
# a module's ``sys``).
_NO_PERMISSION_TESTS = sys.platform == "win32" or (
    hasattr(os, "geteuid") and os.geteuid() == 0
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
    assert remove_link_if_present(link_path)
    shutil.copytree(source, link_path)
    assert not is_portable_link(link_path) and link_path.is_dir()
    return link_path


def _linked_project(
    tmp_path: Path,
    *,
    code_clones: list[str] | None = None,
    extra_papers: list[str] | None = None,
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
    for pid in extra_papers or []:
        _make_paper(vault, pid, projects=["pepforge"])
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
    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "papers" / "p1"
    (target / "sub").mkdir(parents=True)
    (target / "metadata.yaml").write_text("id: p1\n", encoding="utf-8")
    (target / "sub" / "notes.md").write_text("hello\n", encoding="utf-8")
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    shutil.copytree(target, hub / "p1")
    # A copy inherits the vault's read-only TRUTH files (ADR-005 dim F). On
    # Windows a plain shutil.rmtree stops dead on one; locking.rmtree clears
    # the bit and retries. No-op on POSIX, where deletion never consults it.
    (hub / "p1" / "metadata.yaml").chmod(stat.S_IRUSR)

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


def test_settle_deletes_a_copy_whose_only_extra_is_a_link(
    tmp_path: Path,
) -> None:
    """Links inside a tree are skipped by the shape walk, so a copy carrying
    one extra symlink still reads as verbatim — deliberate: the vault entry
    holds none, and a link is not content we can compare."""
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
    thing there is. Leave it, and stop telling the user to delete it.

    p2 is the positive control: its position is NOT obstructed, so the link is
    still attempted there and the one legitimate "this filesystem cannot hold
    folder links" warning fires. Without it, "no warning about the copy" would
    be indistinguishable from "this test prints nothing at all". With it, the
    count is what does the work: dropping the create loop's blocked-set guard
    adds a second warning about the copy and this fails.
    """
    from litman.core import portable_link
    from litman.core.checks import check_project_references
    from litman.core.document import list_papers
    from litman.core.portable_link import reset_link_probe_cache

    vault, project_dir = _linked_project(tmp_path, extra_papers=["p2"])
    link = project_dir / "litman_reflib" / "p1"
    copy = _expand_to_copy(link, vault / "papers" / "p1")
    assert is_portable_link(project_dir / "litman_reflib" / "p2")

    said = _record_link_warnings(monkeypatch)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(1, "Operation not permitted (mocked)")

    # Poison the OS boundary, not litman's own helpers: the probe, the
    # settle and the create loop all run for real against a drive that
    # genuinely refuses both mechanisms.
    monkeypatch.setattr(Path, "symlink_to", boom)
    monkeypatch.setattr(portable_link, "_create_junction", boom)
    reset_link_probe_cache()

    out = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    assert copy.is_dir() and not is_portable_link(copy)
    assert (copy / "metadata.yaml").is_file()
    assert out["pepforge"]["n_replaced_copies"] == 0
    assert out["pepforge"]["n_moved_aside"] == 0
    assert not (vault / ".trash" / "replaced-folders").exists()
    assert len(said) == 1
    assert "cannot hold folder links" in said[0]
    assert str(copy) not in said[0]
    assert check_project_references(vault, list_papers(vault)) == []



# --- a filesystem that refuses ----------------------------------------------


def test_settle_reports_failure_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that cannot be moved costs its own position and nothing else.

    Letting the OSError out would take the rest of the rebuild, every other
    project and every other ``--fix`` category with it — strictly worse than
    the one warning per folder this replaced.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    copy = hub / "p1"
    copy.mkdir()
    (copy / "notes.md").write_text("mine\n", encoding="utf-8")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(32, "The process cannot access the file (mocked)")

    monkeypatch.setattr(Path, "rename", boom)
    monkeypatch.setattr(shutil, "copytree", boom)
    said = _record_link_warnings(monkeypatch)

    out = settle_hub_entry(
        copy, None, vault=vault, project="pepforge", hub="litman_reflib"
    )

    assert out == HubSettlement("failed", None)
    assert out.position_occupied is True
    assert (copy / "notes.md").read_text(encoding="utf-8") == "mine\n"
    joined = "\n".join(said)
    assert "could not clear" in joined
    assert str(copy) in joined
    assert "re-run `lit health-check --fix`" in joined
    # The destination directory gets created before the move is attempted;
    # what matters is that nothing landed in it.
    assert list(_replaced_root(vault, "pepforge", "litman_reflib").iterdir()) == []


@pytest.mark.skipif(
    _NO_PERMISSION_TESTS, reason="POSIX permission bits, and not as root"
)
def test_settle_reports_failure_on_a_real_read_only_hub(tmp_path: Path) -> None:
    """The same, driven by the filesystem rather than a stub.

    A read-only ``.trash/`` is the failure that leaves everything exactly
    where it was: the destination cannot even be created, so the source is
    never read and never removed.
    """
    vault = tmp_path / "vault"
    trash = vault / ".trash"
    trash.mkdir(parents=True)
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    copy = hub / "p1"
    copy.mkdir()
    (copy / "notes.md").write_text("mine\n", encoding="utf-8")

    trash.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        out = settle_hub_entry(
            copy, None, vault=vault, project="pepforge", hub="litman_reflib"
        )
        assert out.verdict == "failed"
    finally:
        trash.chmod(stat.S_IRWXU)
    assert (copy / "notes.md").read_text(encoding="utf-8") == "mine\n"


def test_one_failed_position_does_not_stop_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two copies, one undeletable: the other still becomes a link."""
    vault, project_dir = _linked_project(tmp_path, extra_papers=["p2"])
    for pid in ("p1", "p2"):
        _expand_to_copy(
            project_dir / "litman_reflib" / pid, vault / "papers" / pid
        )

    def flaky_rmtree(path: Path, **kwargs: Any) -> None:
        if Path(path).name == "p1":
            raise OSError(16, "Device or resource busy (mocked)")
        _real_rmtree(path, **kwargs)

    monkeypatch.setattr(project_link, "rmtree", flaky_rmtree)
    said = _record_link_warnings(monkeypatch)

    out = rebuild_all_project_links(vault, {"pepforge": str(project_dir)})

    stuck = project_dir / "litman_reflib" / "p1"
    healed = project_dir / "litman_reflib" / "p2"
    assert stuck.is_dir() and not is_portable_link(stuck)
    assert (stuck / "metadata.yaml").is_file()
    assert is_portable_link(healed)
    assert out["pepforge"]["n_replaced_copies"] == 1
    assert out["pepforge"]["n_paper_links"] == 1
    # Exactly one warning, about the one folder — the create loop must not add
    # a second for the position it was told to leave alone.
    assert len(said) == 1
    assert "p1" in said[0]


@pytest.mark.skipif(
    _NO_PERMISSION_TESTS, reason="POSIX permission bits, and not as root"
)
def test_unreadable_subtree_is_never_called_verbatim(tmp_path: Path) -> None:
    """A subtree neither side can list is not "the same" — it is unknown.

    os.walk's default onerror swallows a failed scandir and yields the
    directory as empty, which would make two different unreadable trees
    compare equal and hand a delete decision a false yes.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "papers" / "p1"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "a.txt").write_text("theirs\n", encoding="utf-8")
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    copy = hub / "p1"
    (copy / "sub").mkdir(parents=True)
    (copy / "sub" / "b.txt").write_text("mine — different\n", encoding="utf-8")

    (target / "sub").chmod(0)
    (copy / "sub").chmod(0)
    moved: Path | None = None
    try:
        assert project_link._is_verbatim_copy(copy, target) is False
        out = settle_hub_entry(
            copy, target, vault=vault, project="pepforge", hub="litman_reflib"
        )
        moved = out.moved_to
    finally:
        # The subtree may have travelled; tmp_path cleanup cannot remove a
        # 0-mode directory, so every place it might be has to be restored.
        candidates = [target / "sub", copy / "sub"]
        if moved is not None:
            candidates.append(moved / "sub")
        for d in candidates:
            if d.exists():
                d.chmod(stat.S_IRWXU)
    # Preserved, not deleted — the whole point of refusing to guess.
    assert out.verdict == "moved-aside"
    assert out.moved_to is not None
    assert (out.moved_to / "sub" / "b.txt").read_text(encoding="utf-8") == (
        "mine — different\n"
    )


def test_verbatim_copy_is_deleted_through_the_read_only_aware_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete has to clear read-only bits on the way.

    A copy carries the vault's read-only metadata.yaml / paper.pdf (ADR-005
    dim F) and a plain ``shutil.rmtree`` stops dead on one — but only on
    Windows, where the write bit governs deletion. POSIX would pass either
    way, so the contract is pinned on the call itself: whatever reaches
    shutil must carry the retry handler.
    """
    real_rmtree = shutil.rmtree
    seen: list[dict[str, Any]] = []

    def spy(path: Any, *args: Any, **kwargs: Any) -> None:
        seen.append(kwargs)
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", spy)

    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "papers" / "p1"
    target.mkdir(parents=True)
    (target / "metadata.yaml").write_text("id: p1\n", encoding="utf-8")
    hub = tmp_path / "proj" / "litman_reflib"
    hub.mkdir(parents=True)
    real_copytree = shutil.copytree
    real_copytree(target, hub / "p1")
    (hub / "p1" / "metadata.yaml").chmod(stat.S_IRUSR)

    out = settle_hub_entry(
        hub / "p1", target, vault=vault, project="p", hub="litman_reflib"
    )

    assert out.verdict == "replaced-copy"
    # Empty when the delete bypassed core.locking.rmtree (a module-level
    # `from shutil import rmtree` binds past the spy); onexc missing when it
    # called shutil directly.
    assert seen, "the delete did not go through core.locking.rmtree"
    assert seen[-1].get("onexc") is not None
