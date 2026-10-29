"""Tests for ``lit trash restore`` symmetric rebuild (M23.2 + M24 de-annotate).

The restore is the inverse of M23.1's rm cascade: from the paper's OWN sealed
fields it rebuilds every opposite paper's paired reverse edge, re-binds
surviving repos, re-creates project symlinks + REFERENCES.md, re-clones a 1:1
hard-deleted repo (post-transaction), prunes dead edges silently, refuses an
id-slot collision, and (M24) strips the ``[[A]] (deleted)`` tag back to
``[[A]]`` on referencing notes. The M23 deletion log is gone — restore writes
no ``.deletion-log.jsonl``.

Covers the four M23.2 acceptance criteria plus the M24 de-annotation:
  1. related / code / project rebuilt; extends/contradicts reverse edges
     (incl. inbound) written back.
  2. dead-edge silent-drop + self-heal (restore order does not matter).
  3. code re-clone: success → repo-meta.papers=[A]; refuse/fail → binding
     kept + warning, NOT deleted (clone uses a local git upstream — no
     network I/O).
  4. id-slot occupied → refuse, live paper untouched; -y non-interactive;
     no .deletion-log.jsonl generated; [[A]] (deleted) de-annotated.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core import project_link as project_link_mod
from litman.core import trash as trash_mod
from litman.core.checks import check_project_references
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.portable_link import is_portable_link
from litman.core.trash import TRASH_DIRNAME
from litman.exceptions import RmError, TrashError
from tests._git_clone import clean_clone

_yaml = YAML(typ="safe")


# ---------------------------------------------------------------------------
# Fixture helpers (mirror tests/commands/test_rm.py)
# ---------------------------------------------------------------------------


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
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
        "code-clones": fields.get("code_clones", []),
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)
    notes = fields.get("notes")
    if notes is not None:
        (paper_dir / "notes.md").write_text(notes, encoding="utf-8")


def _make_fake_repo(
    vault: Path,
    repo_name: str,
    *,
    papers: list[str],
    upstream: str = "file:///fake",
) -> Path:
    repo_root = vault / "codes" / repo_name
    repo_root.mkdir(parents=True, exist_ok=True)
    if not (repo_root / "repo").exists():
        # A real clone: rm deletes only a checkout git reads as clean.
        clean_clone(vault, repo_root / "repo")
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    payload = {
        "name": repo_name,
        "upstream": upstream,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "papers": list(papers),
        "framework": None,
        "runs-on": None,
        "status": None,
    }
    with (repo_root / "repo-meta.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)
    return repo_root


def _read_meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


def _read_repo_meta(vault: Path, repo_name: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "codes" / repo_name / "repo-meta.yaml").read_text()
    )


def _git_local_upstream(tmp_path: Path, name: str) -> Path:
    """A real local git repo usable as an offline `git clone` source.

    ``git clone <local-path>` does no network I/O, so the re-clone test
    stays hermetic.
    """
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=test@example.com",
            "-c", "user.name=test",
            "commit", "-q", "-m", "init",
        ],
        check=True,
    )
    return repo


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ===========================================================================
# AC1 — related / extends / contradicts (incl. inbound) reverse edges
# ===========================================================================


def test_restore_rebuilds_symmetric_related(vault: Path) -> None:
    _write_paper(vault, "2024_Target", related=["2024_B"])
    _write_paper(vault, "2024_B", related=["2024_Target"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    # rm cleared B.related.
    assert _read_meta(vault, "2024_B")["related"] == []

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # B.related has Target again.
    assert _read_meta(vault, "2024_B")["related"] == ["2024_Target"]
    # Target's own field survived in trash and is back.
    assert _read_meta(vault, "2024_Target")["related"] == ["2024_B"]


def test_restore_rebuilds_extends_forward_to_reverse(vault: Path) -> None:
    # Target EXTENDS X (forward). Restore must put extended-by:[Target] on X.
    _write_paper(vault, "2024_Target", extends=["2024_X"])
    _write_paper(vault, "2024_X", extended_by=["2024_Target"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    assert _read_meta(vault, "2024_X")["extended-by"] == []

    runner.invoke(cli, ["trash", "restore", "2024_Target", "--library", str(vault)])
    assert _read_meta(vault, "2024_X")["extended-by"] == ["2024_Target"]


def test_restore_rebuilds_inbound_extends(vault: Path) -> None:
    # X EXTENDS Target: Target.extended-by:[X], X.extends:[Target]. Restoring
    # Target must write Target back into X.extends (the inbound edge, reached
    # via Target's own reverse field).
    _write_paper(vault, "2024_Target", extended_by=["2024_X"])
    _write_paper(vault, "2024_X", extends=["2024_Target"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    assert _read_meta(vault, "2024_X")["extends"] == []

    runner.invoke(cli, ["trash", "restore", "2024_Target", "--library", str(vault)])
    assert _read_meta(vault, "2024_X")["extends"] == ["2024_Target"]


def test_restore_rebuilds_contradicts_pair(vault: Path) -> None:
    # C contradicts Target: Target.contradicted-by:[C], C.contradicts:[Target].
    _write_paper(vault, "2024_Target", contradicted_by=["2024_C"])
    _write_paper(vault, "2024_C", contradicts=["2024_Target"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    assert _read_meta(vault, "2024_C")["contradicts"] == []

    runner.invoke(cli, ["trash", "restore", "2024_Target", "--library", str(vault)])
    assert _read_meta(vault, "2024_C")["contradicts"] == ["2024_Target"]


def test_restore_rebuilds_code_1toN_binding(vault: Path) -> None:
    # 1:N repo: SharedLib survives rm (Keeper still binds). Restore re-binds
    # Target into repo-meta.papers.
    _write_paper(vault, "2024_Target", code_clones=["SharedLib"])
    _write_paper(vault, "2024_Keeper", code_clones=["SharedLib"])
    _make_fake_repo(vault, "SharedLib", papers=["2024_Target", "2024_Keeper"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    assert _read_repo_meta(vault, "SharedLib")["papers"] == ["2024_Keeper"]

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert sorted(_read_repo_meta(vault, "SharedLib")["papers"]) == [
        "2024_Keeper",
        "2024_Target",
    ]


def test_restore_rebuilds_project_symlink_and_references(
    vault: Path, tmp_path: Path
) -> None:
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "projects: {}", f"projects:\n  myproj: {project_dir}"
        ),
        encoding="utf-8",
    )
    _write_paper(vault, "2024_Target", projects=["myproj"])
    runner = CliRunner()
    runner.invoke(
        cli, ["link", "2024_Target", "--project", "myproj", "--library", str(vault)]
    )
    assert is_portable_link(project_dir / "litman_reflib" / "2024_Target")

    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    assert not (project_dir / "litman_reflib" / "2024_Target").exists()

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Symlink re-created and REFERENCES re-rendered with the paper.
    assert is_portable_link(project_dir / "litman_reflib" / "2024_Target")
    refs = (project_dir / "litman_reflib" / "REFERENCES.md").read_text()
    assert "2024_Target" in refs


# ===========================================================================
# AC2 — dead-edge silent-drop + self-heal (restore order independent)
# ===========================================================================


def test_restore_dead_edge_silent_drop(vault: Path) -> None:
    # Target relates to B. Delete B (purge, gone forever), then Target.
    # Restoring Target alone must SILENTLY drop the dead edge to B.
    _write_paper(vault, "2024_Target", related=["2024_B"])
    _write_paper(vault, "2024_B", related=["2024_Target"])
    runner = CliRunner()
    # Purge B so it is permanently gone (not recoverable from trash).
    runner.invoke(
        cli, ["rm", "2024_B", "--purge", "-y", "--library", str(vault)]
    )
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Dead edge silently pruned out of Target.related.
    assert _read_meta(vault, "2024_Target")["related"] == []
    # No warning surfaced for the drop.
    assert "2024_B" not in result.output


def test_restore_self_heal_converges_either_order(vault: Path) -> None:
    # Self-heal: deleting B first clears A.related (A still live) but seals
    # B.related:[A] intact in trash. Then delete A (A.related already []).
    # Restore A FIRST: A's sealed field is now empty (nothing to rebuild).
    # Restore B SECOND: B.related:[A] sealed → the reverse write re-adds B
    # into A.related. The graph self-heals to symmetric — the edge reappears
    # purely from the surviving endpoint's sealed field, so restore order
    # does not lose the relation.
    _write_paper(vault, "2024_A", related=["2024_B"])
    _write_paper(vault, "2024_B", related=["2024_A"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_B", "-y", "--library", str(vault)])
    # A (still live) lost B from its related during B's rm cascade.
    assert _read_meta(vault, "2024_A")["related"] == []
    runner.invoke(cli, ["rm", "2024_A", "-y", "--library", str(vault)])

    # Restore A first — its sealed related is empty, nothing to rebuild.
    runner.invoke(cli, ["trash", "restore", "2024_A", "--library", str(vault)])
    assert _read_meta(vault, "2024_A")["related"] == []

    # Restore B — B.related:[A] sealed → reverse write re-adds B into
    # A.related. The dead edge reappears; the graph is symmetric again.
    runner.invoke(cli, ["trash", "restore", "2024_B", "--library", str(vault)])
    assert _read_meta(vault, "2024_B")["related"] == ["2024_A"]
    assert _read_meta(vault, "2024_A")["related"] == ["2024_B"]


# ===========================================================================
# AC3 — code re-clone (offline local upstream); refuse/fail keeps binding
# ===========================================================================


def test_restore_reclone_success_rebuilds_repo_meta(
    vault: Path, tmp_path: Path
) -> None:
    upstream = _git_local_upstream(tmp_path, "upstream-x")
    _write_paper(vault, "2024_Target", code_clones=["RepoX"])
    _make_fake_repo(
        vault, "RepoX", papers=["2024_Target"], upstream=str(upstream)
    )
    runner = CliRunner()
    # rm 1:1 hard-deletes codes/RepoX and records the local upstream url.
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    assert not (vault / "codes" / "RepoX").exists()

    # -y auto-attempts the re-clone (no prompt, no network — local path).
    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Repo re-cloned; repo-meta.papers rebuilt to [Target].
    assert (vault / "codes" / "RepoX" / "repo").is_dir()
    assert (vault / "codes" / "RepoX" / "repo-meta.yaml").is_file()
    assert _read_repo_meta(vault, "RepoX")["papers"] == ["2024_Target"]
    assert "Re-cloned" in result.output


def test_restore_reclone_refused_keeps_binding(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = _git_local_upstream(tmp_path, "upstream-y")
    _write_paper(vault, "2024_Target", code_clones=["RepoY"])
    _make_fake_repo(
        vault, "RepoY", papers=["2024_Target"], upstream=str(upstream)
    )
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    # Interactive: answer "n" to the re-clone prompt → keep binding, warn.
    monkeypatch.setattr("litman.core.confirm._stdin_is_tty", lambda: True)
    result = runner.invoke(
        cli,
        ["trash", "restore", "2024_Target", "--library", str(vault)],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    # Repo NOT re-cloned, binding on the paper KEPT — and its record is back,
    # upstream included, so the clone can still be made later.
    assert not (vault / "codes" / "RepoY" / "repo").exists()
    assert _read_repo_meta(vault, "RepoY")["upstream"] == str(upstream)
    assert _read_repo_meta(vault, "RepoY")["papers"] == ["2024_Target"]
    assert _read_meta(vault, "2024_Target")["code-clones"] == ["RepoY"]
    assert "Kept binding" in result.output
    assert "lit code restore-all" in " ".join(result.output.split())


def test_restore_without_a_terminal_skips_the_clone_and_prints_it(
    vault: Path, tmp_path: Path
) -> None:
    """Nobody to ask: the paper is restored (exit 0), the clone is skipped as on
    a "no" — a stray "y" on stdin is not an answer — and the line names the
    upstream and the command that clones it from the record the restore put
    back."""
    upstream = _git_local_upstream(tmp_path, "upstream-nt")
    _write_paper(vault, "2024_Target", code_clones=["RepoNT"])
    _make_fake_repo(
        vault, "RepoNT", papers=["2024_Target"], upstream=str(upstream)
    )
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    result = runner.invoke(
        cli,
        ["trash", "restore", "2024_Target", "--library", str(vault)],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_Target").is_dir()
    assert "re-clone from" not in result.output  # the question was not asked
    assert not (vault / "codes" / "RepoNT" / "repo").exists()
    assert _read_meta(vault, "2024_Target")["code-clones"] == ["RepoNT"]
    flat = " ".join(result.output.split())
    assert str(upstream) in flat
    assert "(run `lit code restore-all`)" in flat

    # The printed command does the clone that was skipped.
    restored = runner.invoke(cli, ["code", "restore-all", "--library", str(vault)])
    assert restored.exit_code == 0, restored.output
    assert (vault / "codes" / "RepoNT" / "repo" / "README.md").is_file()
    assert _read_repo_meta(vault, "RepoNT")["papers"] == ["2024_Target"]
    assert _read_meta(vault, "2024_Target")["code-clones"] == ["RepoNT"]


def test_a_local_import_comes_back_through_restore_all(
    vault: Path, tmp_path: Path
) -> None:
    """A repo added from a folder without git is recorded as ``local:<path>``,
    which git cannot clone: `lit code restore-all` copies the folder again,
    as `lit code add` did."""
    source = tmp_path / "my code"
    source.mkdir()
    (source / "run.py").write_text("print('hi')\n", encoding="utf-8")
    _write_paper(vault, "2024_Target", code_clones=["RepoL"])
    _make_fake_repo(
        vault, "RepoL", papers=["2024_Target"], upstream=f"local:{source}"
    )
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "(run `lit code restore-all`)" in " ".join(result.output.split())
    assert _read_repo_meta(vault, "RepoL")["upstream"] == f"local:{source}"

    restored = runner.invoke(cli, ["code", "restore-all", "--library", str(vault)])
    assert restored.exit_code == 0, restored.output
    assert (vault / "codes" / "RepoL" / "repo" / "run.py").is_file()


def test_restore_reclone_failure_keeps_binding(vault: Path) -> None:
    # Sidecar records an unreachable upstream → clone fails. Binding kept,
    # warning emitted, restore still succeeds (re-clone is not a precondition).
    _write_paper(vault, "2024_Target", code_clones=["RepoZ"])
    _make_fake_repo(
        vault,
        "RepoZ",
        papers=["2024_Target"],
        upstream="/nonexistent/path/to/repo.git",
    )
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # The failed clone is rolled back; the record the restore put back stays.
    assert not (vault / "codes" / "RepoZ" / "repo").exists()
    assert _read_repo_meta(vault, "RepoZ")["upstream"] == "/nonexistent/path/to/repo.git"
    assert _read_meta(vault, "2024_Target")["code-clones"] == ["RepoZ"]
    assert "failed" in result.output.lower()
    # Review F20: the upstream URL is echoed on failure too.
    assert "/nonexistent/path/to/repo.git" in result.output
    assert "lit code restore-all" in " ".join(result.output.split())


def test_restore_tolerates_non_utf8_note_elsewhere(vault: Path) -> None:
    # Review F22: a non-UTF-8 note anywhere in the vault must not abort restore
    # between the folder move and the rollback try (which left a half-restored
    # paper). The de-annotation read now lives inside the transaction and skips
    # an unreadable note rather than crashing.
    _write_paper(vault, "2024_Target")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    # An unrelated active paper carrying a non-UTF-8 notes.md.
    _write_paper(vault, "2024_Other")
    (vault / "papers" / "2024_Other" / "notes.md").write_bytes(b"\xff\xfe bad\n")

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Fully restored, not a half-restore: paper back on disk, trash drained.
    assert (vault / "papers" / "2024_Target" / "metadata.yaml").is_file()
    from litman.core.trash import list_trash

    assert list_trash(vault) == []
    # The unreadable note was left untouched.
    assert (vault / "papers" / "2024_Other" / "notes.md").read_bytes() == (
        b"\xff\xfe bad\n"
    )


# ===========================================================================
# AC4 — id-slot occupied refusal; -y non-interactive; restored log row
# ===========================================================================


def test_restore_refuses_id_slot_occupied(vault: Path) -> None:
    _write_paper(vault, "2024_Foo", title="original")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "-y", "--library", str(vault)])
    # A new live paper takes the id slot.
    _write_paper(vault, "2024_Foo", title="live-replacement")
    live_before = (vault / "papers/2024_Foo/metadata.yaml").read_text()

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Foo", "--library", str(vault)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, TrashError)
    assert "already exists" in str(result.exception)
    # Live paper untouched.
    assert (vault / "papers/2024_Foo/metadata.yaml").read_text() == live_before


def test_restore_yes_is_non_interactive(vault: Path, tmp_path: Path) -> None:
    upstream = _git_local_upstream(tmp_path, "upstream-noninteractive")
    _write_paper(vault, "2024_Target", code_clones=["RepoNI"])
    _make_fake_repo(
        vault, "RepoNI", papers=["2024_Target"], upstream=str(upstream)
    )
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    # No stdin at all — -y must not block on the re-clone prompt.
    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert (vault / "codes" / "RepoNI" / "repo").is_dir()


def test_restore_writes_no_deletion_log(vault: Path) -> None:
    # AC89: the M23 deletion log is removed; restore never generates it.
    _write_paper(vault, "2024_Foo_Bar", title="The Foo")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo_Bar", "-y", "--library", str(vault)])
    runner.invoke(
        cli, ["trash", "restore", "2024_Foo_Bar", "--library", str(vault)]
    )
    assert not (vault / ".deletion-log.jsonl").exists()


def test_restore_deannotates(vault: Path) -> None:
    # AC88: after rm tags [[A]] (deleted), restore strips it back to [[A]].
    _write_paper(vault, "2024_Target")
    _write_paper(
        vault, "2024_Other", notes="Discussed in [[2024_Target]] earlier.\n"
    )
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    # rm tagged the referencing note.
    assert (
        vault / "papers/2024_Other/notes.md"
    ).read_text() == "Discussed in [[2024_Target]] (deleted) earlier.\n"

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # restore de-annotated it back to the bare link.
    assert (
        vault / "papers/2024_Other/notes.md"
    ).read_text() == "Discussed in [[2024_Target]] earlier.\n"


def test_restore_keeps_windows_line_endings_in_the_notes_it_untags(
    vault: Path,
) -> None:
    _write_paper(vault, "2024_Target")
    _write_paper(vault, "2024_Other")
    notes = vault / "papers/2024_Other/notes.md"
    notes.write_bytes(b"# Other\r\n\r\ncf [[2024_Target]] (deleted).\r\n")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    assert notes.read_bytes() == b"# Other\r\n\r\ncf [[2024_Target]].\r\n"


def test_restore_refreshes_index_and_views(vault: Path) -> None:
    _write_paper(vault, "2024_Foo", topics=["alpha"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "-y", "--library", str(vault)])
    runner.invoke(cli, ["trash", "restore", "2024_Foo", "--library", str(vault)])

    payload = json.loads((vault / "INDEX.json").read_text())
    assert "2024_Foo" in [p["id"] for p in payload["papers"]]
    assert is_portable_link(vault / "views/by-topic/alpha/2024_Foo")


# ===========================================================================
# Restore help smoke
# ===========================================================================


def test_restore_help_mentions_yes() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["trash", "restore", "--help"])
    assert result.exit_code == 0
    assert "--yes" in result.output or "-y" in result.output


# ===========================================================================
# Atomicity — a staged-write failure rolls A back into trash (invariant #9)
# ===========================================================================


def test_restore_staged_write_failure_rolls_back(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Steps 1-2 (folder move + opposite reverse-edge rebuild + INDEX) are one
    # logical transaction: if the staged write fails, A returns to trash and
    # no opposite paper is mutated.
    _write_paper(vault, "2024_Target", related=["2024_B"])
    _write_paper(vault, "2024_B", related=["2024_Target"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    # rm cleared B.related; the restore would re-add it on success.
    assert _read_meta(vault, "2024_B")["related"] == []
    trash_entry = next(
        p for p in (vault / TRASH_DIRNAME).glob("2024_Target-*") if p.is_dir()
    )
    assert trash_entry.is_dir()

    class _BoomStage:
        def write_text(self, *_a: Any, **_k: Any) -> Path:
            raise RuntimeError("staged write boom")

        def write_bytes(self, *_a: Any, **_k: Any) -> Path:
            raise RuntimeError("staged write boom")

    @contextlib.contextmanager
    def _boom_staged_write(_vault: Path, op_id: str | None = None):  # type: ignore[no-untyped-def]
        yield _BoomStage()

    monkeypatch.setattr("litman.core.trash.staged_write", _boom_staged_write)

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )
    assert result.exit_code != 0
    # A rolled back into trash; no half-restored paper on the live path.
    assert not (vault / "papers" / "2024_Target").exists()
    assert next(
        p for p in (vault / TRASH_DIRNAME).glob("2024_Target-*") if p.is_dir()
    ).is_dir()
    # The opposite paper was NOT mutated (transaction never committed).
    assert _read_meta(vault, "2024_B")["related"] == []


def test_trash_restore_simple_no_relations(vault: Path) -> None:
    # A paper with no relations restores cleanly (regression: the rebuild path
    # must be a no-op when there is nothing to rebuild).
    _write_paper(vault, "2024_Solo", title="Solo")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Solo", "-y", "--library", str(vault)])
    result = runner.invoke(
        cli, ["trash", "restore", "2024_Solo", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_Solo" / "metadata.yaml").is_file()
    trash_dirs = [
        e for e in (vault / TRASH_DIRNAME).iterdir() if e.is_dir()
    ]
    assert trash_dirs == []


def test_restore_replaces_folder_copy_in_hub(
    vault: Path, tmp_path: Path
) -> None:
    """Restore rebuilds the project hubs, so it settles an expanded folder
    sitting in the link position the same way every other rebuild does."""
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "projects: {}", f"projects:\n  myproj: {project_dir}"
        ),
        encoding="utf-8",
    )
    _write_paper(vault, "2024_Target", projects=["myproj"])
    runner = CliRunner()
    runner.invoke(
        cli, ["link", "2024_Target", "--project", "myproj", "--library", str(vault)]
    )
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    stale = project_dir / "litman_reflib" / "2024_Target"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "notes.md").write_text("from the other machine\n", encoding="utf-8")

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    assert is_portable_link(stale)
    aside_root = (
        vault / TRASH_DIRNAME / "replaced-folders" / "myproj" / "litman_reflib"
    )
    kept = sorted(aside_root.iterdir())
    assert len(kept) == 1
    assert (kept[0] / "notes.md").read_text(encoding="utf-8") == (
        "from the other machine\n"
    )


def test_restore_reports_a_moved_aside_folder(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore re-links into the project hubs, so it can move a folder out of
    the user's own directory — and then it has to say where it went."""
    monkeypatch.setenv("COLUMNS", "400")
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "projects: {}", f"projects:\n  myproj: {project_dir}"
        ),
        encoding="utf-8",
    )
    _write_paper(vault, "2024_Target", projects=["myproj"])
    runner = CliRunner()
    runner.invoke(
        cli, ["link", "2024_Target", "--project", "myproj", "--library", str(vault)]
    )
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    stale = project_dir / "litman_reflib" / "2024_Target"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "MY_NOTES.md").write_text("hand-written\n", encoding="utf-8")

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "kept 1 folder that does not match the vault:" in flat
    aside_root = (
        vault / TRASH_DIRNAME / "replaced-folders" / "myproj" / "litman_reflib"
    )
    kept = sorted(aside_root.iterdir())
    assert len(kept) == 1
    assert str(kept[0]) in flat
    assert (kept[0] / "MY_NOTES.md").is_file()


# ===========================================================================
# A project folder that cannot be changed: the restore finishes and says so
# ===========================================================================

RESTORE_WARNING = (
    "warning: could not update a project folder (Access is denied). "
    "Fix that, then run `lit health-check --fix`."
)


def _paper_with_both_repos(vault: Path, tmp_path: Path) -> Path:
    """Target in project myproj, binding Shared (Keeper binds it too, so rm
    keeps it) and Solo (Target's alone, so rm deletes the clone and only the
    trash entry remembers where it came from). Target is in the trash on
    return; the project folder is returned."""
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    config_path = vault / "lit-config.yaml"
    config_path.write_text(
        config_path.read_text().replace(
            "projects: {}", f"projects:\n  myproj: {project_dir}"
        ),
        encoding="utf-8",
    )
    upstream = _git_local_upstream(tmp_path, "upstream-solo")
    _write_paper(
        vault, "2024_Target", projects=["myproj"], code_clones=["Shared", "Solo"]
    )
    _write_paper(vault, "2024_Keeper", code_clones=["Shared"])
    _make_fake_repo(vault, "Shared", papers=["2024_Target", "2024_Keeper"])
    _make_fake_repo(vault, "Solo", papers=["2024_Target"], upstream=str(upstream))
    runner = CliRunner()
    runner.invoke(
        cli, ["link", "2024_Target", "--project", "myproj", "--library", str(vault)]
    )
    assert is_portable_link(project_dir / "litman_code" / "Shared")
    assert is_portable_link(project_dir / "litman_code" / "Solo")

    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    assert (vault / "codes" / "Shared" / "repo").is_dir()
    assert not (vault / "codes" / "Solo").exists()
    assert not (project_dir / "litman_code" / "Shared").exists()
    return project_dir


def _restore(vault: Path) -> tuple[int, str, str]:
    """Exit code, output with one space between words (so a line Rich wrapped
    still matches), and what to show when the exit code is wrong."""
    result = CliRunner().invoke(
        cli, ["trash", "restore", "2024_Target", "-y", "--library", str(vault)]
    )
    output = " ".join(result.output.split())
    return result.exit_code, output, f"{output} {result.exception!r}"


def _drift(vault: Path) -> list[str]:
    return [i.message for i in check_project_references(vault, list_papers(vault))]


def _refuse(*args: object, **kwargs: object) -> None:
    raise PermissionError(13, "Access is denied")


def _refusing_hub(hub: str) -> Callable[..., Any]:
    """settle_hub_entry, except that one hub folder of the project refuses."""
    real = trash_mod.settle_hub_entry

    def settle(link: Path, target: Path, **kwargs: Any) -> Any:
        if kwargs["hub"] == hub:
            _refuse()
        return real(link, target, **kwargs)

    return settle


REFUSALS = {
    # The re-link of the repo that survived (the step that crashed before).
    "relink-code": (trash_mod, "settle_hub_entry", _refusing_hub("litman_code")),
    "relink-reflib": (
        trash_mod, "settle_hub_entry", _refusing_hub("litman_reflib")
    ),
    "references": (trash_mod, "write_references_md", _refuse),
    # The link to the clone made again (the step that was silent before).
    "reclone-code": (project_link_mod, "refresh_project_code_links", _refuse),
}


@pytest.mark.parametrize("refusal", list(REFUSALS))
def test_restore_says_when_a_project_folder_could_not_be_updated(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    project_dir = _paper_with_both_repos(vault, tmp_path)
    module, name, fake = REFUSALS[refusal]

    with monkeypatch.context() as patch:
        patch.setattr(module, name, fake)
        code, output, shown = _restore(vault)

    assert code == 0, shown
    assert "Restored 2024_Target" in output
    assert output.count(RESTORE_WARNING) == 1
    # A project is claimed re-linked only when its re-link all went through.
    assert ("Re-linked into 1 project (myproj)" in output) == (
        refusal == "reclone-code"
    )
    # The rest of the restore still ran: the clone only the trash entry knew
    # the address of is back.
    assert "Re-cloned 'Solo'" in output
    assert (vault / "codes" / "Solo" / "repo").is_dir()
    assert (vault / "papers" / "2024_Target" / "metadata.yaml").is_file()

    # The way out it names.
    CliRunner().invoke(cli, ["health-check", "--fix", "--library", str(vault)])
    assert _drift(vault) == []
    assert is_portable_link(project_dir / "litman_reflib" / "2024_Target")
    assert is_portable_link(project_dir / "litman_code" / "Shared")
    assert is_portable_link(project_dir / "litman_code" / "Solo")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod does not take read access away from a folder on Windows",
)
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads a folder whatever its permission bits",
)
def test_restore_finishes_when_litman_code_cannot_be_read(
    vault: Path, tmp_path: Path
) -> None:
    """The real failure, nothing injected: both the re-link of Shared and the
    link to the new Solo clone meet a litman_code/ that cannot be read."""
    project_dir = _paper_with_both_repos(vault, tmp_path)
    code_dir = project_dir / "litman_code"

    code_dir.chmod(0)
    try:
        code, output, shown = _restore(vault)
    finally:
        code_dir.chmod(0o755)

    assert code == 0, shown
    assert output.count(
        "warning: could not update a project folder (Permission denied). "
        "Fix that, then run `lit health-check --fix`."
    ) == 1
    assert "Re-cloned 'Solo'" in output
    assert "Re-linked into" not in output
    assert (vault / "codes" / "Solo" / "repo").is_dir()
    # The parts of the project outside litman_code/ were brought up to date.
    assert is_portable_link(project_dir / "litman_reflib" / "2024_Target")
    refs = (project_dir / "litman_reflib" / "REFERENCES.md").read_text(
        encoding="utf-8"
    )
    assert "2024_Target" in refs
    assert not (code_dir / "Shared").exists()

    CliRunner().invoke(cli, ["health-check", "--fix", "--library", str(vault)])
    assert _drift(vault) == []
    assert is_portable_link(code_dir / "Shared")
    assert is_portable_link(code_dir / "Solo")


# ===========================================================================
# A restore that does not re-clone keeps the upstream on disk (web UI)
# ===========================================================================


def test_a_web_ui_restore_keeps_the_upstream_of_a_repo_it_did_not_clone(
    vault: Path, tmp_path: Path
) -> None:
    """The web UI restores without cloning. Solo's upstream was recorded only
    in the trash entry, which the restore consumes; it must come back with the
    paper, as codes/Solo/repo-meta.yaml without a checkout, so that
    health-check names the fix and `lit code restore-all` clones it — and
    links it into the paper's project again."""
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    project_dir = _paper_with_both_repos(vault, tmp_path)
    upstream = str(tmp_path / "upstream-solo")
    (entry,) = trash_mod.list_trash(vault)

    resp = TestClient(create_app(vault)).post(
        f"/api/trash/{entry.entry_name}/restore"
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["missing_repos"] == {"Solo": upstream}
    assert trash_mod.list_trash(vault) == []
    meta = _read_repo_meta(vault, "Solo")
    assert (meta["name"], meta["upstream"], meta["papers"]) == (
        "Solo", upstream, ["2024_Target"]
    )
    assert not (vault / "codes" / "Solo" / "repo").exists()

    health = CliRunner().invoke(cli, ["health-check", "--library", str(vault)])
    flat = " ".join(health.output.split())
    assert "codes/Solo/ has repo-meta.yaml but no repo/ checkout" in flat
    assert "`lit code restore-all` to re-clone Solo" in flat

    restored = CliRunner().invoke(
        cli, ["code", "restore-all", "--library", str(vault)]
    )
    assert restored.exit_code == 0, restored.output
    assert (vault / "codes" / "Solo" / "repo" / "README.md").is_file()
    assert is_portable_link(project_dir / "litman_code" / "Solo")
    assert _drift(vault) == []


def test_restore_all_says_when_it_cannot_link_a_restored_repo(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone is back but a project's litman_code/ could not take the link:
    restore-all still reports the clone, and says so in one line."""
    _paper_with_both_repos(vault, tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["trash", "restore", "2024_Target", "--library", str(vault)])
    monkeypatch.setattr(project_link_mod, "refresh_project_code_links", _refuse)

    result = runner.invoke(cli, ["code", "restore-all", "--library", str(vault)])

    flat = " ".join(result.output.split())
    assert result.exit_code == 0, flat
    assert (vault / "codes" / "Solo" / "repo").is_dir()
    assert flat.count(
        "warning: could not update a project's litman_code/ folder (Access is "
        "denied). Fix that, then run `lit health-check --fix`."
    ) == 1


# ===========================================================================
# What a re-clone cannot bring back: the repo's notes and fields, and code
# that exists only in the vault
# ===========================================================================

_REPO_NOTES = "# Solo\r\n\r\nconda env: torch 2.3 — run `make train` first\r\n".encode()


def _fill_in_solo(vault: Path, notes: bytes = _REPO_NOTES) -> None:
    """Give Solo what only the user wrote: a field, a comment, a note."""
    repo_root = vault / "codes" / "Solo"
    meta_path = repo_root / "repo-meta.yaml"
    meta_path.write_text(
        meta_path.read_text(encoding="utf-8").replace(
            "framework:", "# tested on the lab GPU\nframework: pytorch"
        ),
        encoding="utf-8",
    )
    (repo_root / "notes.md").write_bytes(notes)


def _rm_solo(vault: Path, tmp_path: Path, notes: bytes = _REPO_NOTES) -> str:
    """Target alone binds Solo, which has a clone URL: rm deletes Solo."""
    upstream = str(_git_local_upstream(tmp_path, "upstream-solo"))
    _write_paper(vault, "2024_Target", code_clones=["Solo"])
    _make_fake_repo(vault, "Solo", papers=["2024_Target"], upstream=upstream)
    _fill_in_solo(vault, notes)
    result = CliRunner().invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert not (vault / "codes" / "Solo").exists()
    return upstream


def test_restore_brings_back_the_notes_and_fields_of_a_deleted_repo(
    vault: Path, tmp_path: Path
) -> None:
    """The web UI restores without cloning: Solo comes back with the notes and
    the fields its user wrote, not blank ones — comments and line endings
    kept, bound to the restored paper."""
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    _rm_solo(vault, tmp_path)
    (entry,) = trash_mod.list_trash(vault)

    resp = TestClient(create_app(vault)).post(
        f"/api/trash/{entry.entry_name}/restore"
    )

    assert resp.status_code == 200, resp.text
    repo_root = vault / "codes" / "Solo"
    assert (repo_root / "notes.md").read_bytes() == _REPO_NOTES
    meta_text = (repo_root / "repo-meta.yaml").read_text(encoding="utf-8")
    assert "# tested on the lab GPU" in meta_text
    meta = _read_repo_meta(vault, "Solo")
    assert (meta["framework"], meta["papers"]) == ("pytorch", ["2024_Target"])
    assert meta["updated-at"] != "2026-04-28T10:00:00+02:00"
    assert not (repo_root / "repo").exists()


def test_restore_brings_back_a_repo_note_that_is_not_utf8(
    vault: Path, tmp_path: Path
) -> None:
    """A note saved in another encoding cannot be text in the trash entry; it
    comes back byte for byte all the same."""
    notes = "# Solo\r\n环境: torch\r\n".encode("gbk")
    _rm_solo(vault, tmp_path, notes)

    result = CliRunner().invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    assert (vault / "codes" / "Solo" / "notes.md").read_bytes() == notes


def test_a_reclone_keeps_the_restored_repo_notes(
    vault: Path, tmp_path: Path
) -> None:
    """`-y` clones Solo again into the restored record: the clone adds repo/
    and leaves the user's notes and fields as they came back."""
    _rm_solo(vault, tmp_path)

    code, output, why = _restore(vault)

    assert code == 0, why
    assert "Re-cloned" in output
    repo_root = vault / "codes" / "Solo"
    assert (repo_root / "repo" / "README.md").is_file()
    assert (repo_root / "notes.md").read_bytes() == _REPO_NOTES
    assert _read_repo_meta(vault, "Solo")["framework"] == "pytorch"


def test_restore_of_an_entry_that_saved_no_repo_files_rebuilds_the_record(
    vault: Path, tmp_path: Path
) -> None:
    """A paper trashed before rm saved the repo's files: its restore still
    puts back the repo's address, as before."""
    upstream = _rm_solo(vault, tmp_path)
    (entry,) = trash_mod.list_trash(vault)
    sidecar = entry.entry_path.parent / f"{entry.entry_name}.meta.yaml"
    data = _yaml.load(sidecar.read_text(encoding="utf-8"))
    del data["orphan_repo_files"]
    sidecar.write_text(json.dumps(data), encoding="utf-8")

    result = CliRunner().invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    meta = _read_repo_meta(vault, "Solo")
    assert (meta["upstream"], meta["papers"], meta["framework"]) == (
        upstream, ["2024_Target"], None
    )
    assert not (vault / "codes" / "Solo" / "notes.md").exists()


def test_a_trash_entry_restores_only_the_two_repo_files(vault: Path) -> None:
    """The trash entry is a file anyone can edit: a restore writes no path it
    names besides repo-meta.yaml and notes.md."""
    _write_paper(vault, "2024_Target")
    edited: dict[str, Any] = {
        "notes.md": "kept", "../../evil.md": "x", "repo-meta.yaml": 7
    }
    entry_path = trash_mod.move_to_trash(
        vault,
        "2024_Target",
        orphan_repos={"Solo": "https://example.org/solo.git"},
        orphan_repo_files={"Solo": edited},
    )

    (entry,) = trash_mod.list_trash(vault)

    assert entry.entry_path == entry_path
    assert entry.orphan_repo_files == {"Solo": {"notes.md": "kept"}}


@pytest.mark.parametrize("source", ["none", "moved folder"])
def test_rm_keeps_a_repo_it_has_nothing_to_clone_again_from(
    vault: Path, tmp_path: Path, source: str
) -> None:
    """`lit code add --move` leaves the vault holding the only copy of the
    code; a repo with no upstream has nothing to clone from either. rm only
    unbinds it, says so, and a restore binds it again."""
    upstream = "" if source == "none" else f"local:{tmp_path / 'moved away'}"
    _write_paper(vault, "2024_Target", code_clones=["Only"])
    repo_root = _make_fake_repo(
        vault, "Only", papers=["2024_Target"], upstream=upstream
    )
    runner = CliRunner()

    preview = runner.invoke(
        cli, ["rm", "2024_Target", "--dry-run", "--library", str(vault)]
    )
    result = runner.invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )

    assert "Would keep repos with no copy to clone again: Only" in " ".join(
        preview.output.split()
    )
    assert "orphan" not in preview.output
    assert result.exit_code == 0, result.output
    assert "Kept 1 repo with no copy to clone again (Only;" in " ".join(
        result.output.split()
    )
    assert "Removed" not in result.output
    assert (repo_root / "repo" / "README.md").is_file()
    assert _read_repo_meta(vault, "Only")["papers"] == []
    (entry,) = trash_mod.list_trash(vault)
    assert entry.orphan_repos == {}

    restored = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )

    assert restored.exit_code == 0, restored.output
    assert _read_repo_meta(vault, "Only")["papers"] == ["2024_Target"]
    assert (repo_root / "repo" / "README.md").is_file()


def test_the_web_ui_delete_names_a_repo_it_keeps(vault: Path, tmp_path: Path) -> None:
    """The delete dialog and its result list the kept repo apart from the ones
    the delete removes."""
    pytest.importorskip("fastapi")
    from litman.server import create_app
    from tests.server._client import TestClient

    _write_paper(vault, "2024_Target", code_clones=["Only", "Solo"])
    _make_fake_repo(vault, "Only", papers=["2024_Target"], upstream="")
    _make_fake_repo(vault, "Solo", papers=["2024_Target"])
    client = TestClient(create_app(vault))

    preview = client.get("/api/paper/2024_Target/rm-preview").json()
    deleted = client.delete("/api/paper/2024_Target").json()

    assert (preview["reposKept"], preview["reposRemoved"]) == (["Only"], ["Solo"])
    assert preview["reposUnbound"] == []
    assert (deleted["reposKept"], deleted["reposRemoved"]) == (["Only"], ["Solo"])
    assert deleted["reposUnbound"] == 0
    assert (vault / "codes" / "Only" / "repo").is_dir()
    assert not (vault / "codes" / "Solo").exists()


def test_rm_stops_when_it_cannot_read_a_repo_note(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note rm cannot read is one it cannot keep for a restore: nothing is
    deleted, and the message names the file."""
    _write_paper(vault, "2024_Target", code_clones=["Solo"])
    repo_root = _make_fake_repo(vault, "Solo", papers=["2024_Target"])
    notes = repo_root / "notes.md"
    notes.write_text("mine\n", encoding="utf-8")
    real_read_bytes = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        # By name: rm reaches the file through the vault path it resolved.
        if (self.parent.name, self.name) == ("Solo", "notes.md"):
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    result = CliRunner().invoke(
        cli, ["rm", "2024_Target", "-y", "--library", str(vault)]
    )

    assert isinstance(result.exception, RmError)
    message = str(result.exception)
    assert message.startswith("Cannot read ") and "notes.md: " in message
    assert message.endswith("Fix its permissions, then retry.")
    assert (vault / "papers" / "2024_Target").is_dir()
    assert (repo_root / "repo").is_dir()
    assert trash_mod.list_trash(vault) == []
