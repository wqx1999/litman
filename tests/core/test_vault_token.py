"""The stat tokens: what moves the auto sync's token, and what must not.

The auto sync notices a change here with :func:`synced_token`; the page notices
one with :func:`gui_token`. The sync's own rebuild of the derived files must
never move the first, or every sync would set off the next.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from litman.core import locking
from litman.core.atomic import staged_write
from litman.core.correctors import reconcile_derived
from litman.core.vault_registry import registry_path
from litman.core.vault_token import fingerprint, gui_token, md_version, synced_token


def _paper(vault_with_paper: tuple[Path, str]) -> tuple[Path, str]:
    """The fixture's paper, with the notes.md and paper.pdf a real one has."""
    vault, pid = vault_with_paper
    paper = vault / "papers" / pid
    (paper / "notes.md").write_text("# Foo Bar\n", encoding="utf-8")
    (paper / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    return vault, pid


def _add_si(vault: Path, paper_id: str, name: str, data: bytes) -> Path:
    si = vault / "papers" / paper_id / "supplementary"
    si.mkdir(exist_ok=True)
    path = si / name
    path.write_bytes(data)
    return path


def _add_code(vault: Path, name: str) -> Path:
    folder = vault / "codes" / name
    (folder / "repo").mkdir(parents=True)
    (folder / "repo-meta.yaml").write_text("name: x\n", encoding="utf-8")
    (folder / "notes.md").write_text("# x\n", encoding="utf-8")
    return folder


def _bump(path: Path, seconds: int = 7) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def test_every_synced_write_moves_the_token(vault_with_paper: tuple[Path, str]) -> None:
    vault, pid = _paper(vault_with_paper)
    _add_code(vault, "tool")
    paper = vault / "papers" / pid

    writes = {
        "an annotation save into paper.pdf": lambda: _replace(vault, f"papers/{pid}/paper.pdf"),
        "an SI file added": lambda: _add_si(vault, pid, "si-01.csv", b"a,b\n"),
        "an SI file changed": lambda: _bump(paper / "supplementary" / "si-01.csv"),
        "a code repo's repo-meta.yaml": lambda: _bump(vault / "codes/tool/repo-meta.yaml"),
        "a code repo added": lambda: _add_code(vault, "other"),
        "notes.md": lambda: _bump(paper / "notes.md"),
        "TAXONOMY.md": lambda: _bump(vault / "TAXONOMY.md"),
        "a code folder removed": lambda: locking.rmtree(vault / "codes" / "other"),
    }
    for what, write in writes.items():
        before = synced_token(vault)
        write()
        assert synced_token(vault) != before, what


def _replace(vault: Path, rel: str) -> None:
    with staged_write(vault) as sw:
        sw.write_bytes(rel, b"%PDF-1.4\n% annotated\n")


def test_an_edit_in_place_moves_the_token_though_its_folder_stays(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, pid = _paper(vault_with_paper)
    meta = vault / "papers" / pid / "metadata.yaml"
    folder_before = fingerprint(meta.parent)
    before = synced_token(vault)
    locking.unlock_truth_file(meta)
    text = meta.read_bytes()
    with open(meta, "r+b") as f:  # what an editor saving in place does
        f.truncate(0)
        f.write(text.replace(b"title:", b"title: Edited"))
    _bump(meta, 3)

    assert synced_token(vault) != before
    # The reverse: a token made of folder stats alone would miss it.
    assert fingerprint(meta.parent) == folder_before


def test_a_rebuild_and_files_that_never_travel_leave_the_token_alone(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, pid = _paper(vault_with_paper)
    folder = _add_code(vault, "tool")
    reconcile_derived(vault, project_refs=True)
    before = synced_token(vault)

    reconcile_derived(vault, project_refs=True)
    _bump(vault / "INDEX.json")
    _bump(vault / "lit-config.yaml")
    (vault / "papers" / pid / ".DS_Store").write_bytes(b"\0")
    (vault / "papers" / pid / "notes.md.sync-conflict1").write_text("x", encoding="utf-8")
    (folder / "repo" / "main.py").write_text("print()\n", encoding="utf-8")

    assert synced_token(vault) == before


def test_the_page_token_moves_on_its_inputs_and_not_on_a_pdf(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, pid = _paper(vault_with_paper)
    registry = registry_path()
    before = gui_token(vault, registry)
    _bump(vault / "papers" / pid / "paper.pdf")
    assert gui_token(vault, registry) == before
    _bump(vault / "papers" / pid / "notes.md")
    assert gui_token(vault, registry) != before


def test_the_md_version_sees_a_same_size_change_that_mtime_and_size_miss(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, pid = _paper(vault_with_paper)
    notes = vault / "papers" / pid / "notes.md"
    notes.write_bytes(b"# Notes\nversion one\n")
    st = notes.stat()
    version, stat_version = md_version(notes.read_bytes()), fingerprint(notes)

    notes.write_bytes(b"# Notes\nversion two\n")
    os.utime(notes, ns=(st.st_atime_ns, st.st_mtime_ns))

    assert fingerprint(notes) == stat_version
    assert md_version(notes.read_bytes()) != version
    assert version == hashlib.sha256(b"# Notes\nversion one\n").hexdigest()
