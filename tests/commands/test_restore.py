"""Tests for ``lit trash restore`` symmetric rebuild (M23.2).

The restore is the inverse of M23.1's rm cascade: from the paper's OWN sealed
fields it rebuilds every opposite paper's paired reverse edge, re-binds
surviving repos, re-creates project symlinks + REFERENCES.md, re-clones a 1:1
hard-deleted repo (post-transaction), prunes dead edges silently, refuses an
id-slot collision, and appends a ``restored`` row to the deletion log.

Covers the four M23.2 acceptance criteria:
  1. related / code / project rebuilt; extends/contradicts reverse edges
     (incl. inbound) written back.
  2. dead-edge silent-drop + self-heal (restore order does not matter).
  3. code re-clone: success → repo-meta.papers=[A]; refuse/fail → binding
     kept + warning, NOT deleted (clone uses a local git upstream — no
     network I/O).
  4. id-slot occupied → refuse, live paper untouched; -y non-interactive;
     .deletion-log.jsonl gets a restored row.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.trash import TRASH_DIRNAME
from litman.exceptions import TrashError

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
        "priority": fields.get("priority", "B"),
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


def _make_fake_repo(
    vault: Path,
    repo_name: str,
    *,
    papers: list[str],
    upstream: str = "file:///fake",
) -> Path:
    repo_root = vault / "codes" / repo_name
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "repo").mkdir(exist_ok=True)
    (repo_root / "repo" / "README.md").write_text("# fake\n", encoding="utf-8")
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
    assert (project_dir / "literature" / "2024_Target").is_symlink()

    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])
    assert not (project_dir / "literature" / "2024_Target").exists()

    result = runner.invoke(
        cli, ["trash", "restore", "2024_Target", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    # Symlink re-created and REFERENCES re-rendered with the paper.
    assert (project_dir / "literature" / "2024_Target").is_symlink()
    refs = (project_dir / "literature" / "REFERENCES.md").read_text()
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


def test_restore_reclone_refused_keeps_binding(vault: Path, tmp_path: Path) -> None:
    upstream = _git_local_upstream(tmp_path, "upstream-y")
    _write_paper(vault, "2024_Target", code_clones=["RepoY"])
    _make_fake_repo(
        vault, "RepoY", papers=["2024_Target"], upstream=str(upstream)
    )
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Target", "-y", "--library", str(vault)])

    # Interactive: answer "n" to the re-clone prompt → keep binding, warn.
    result = runner.invoke(
        cli,
        ["trash", "restore", "2024_Target", "--library", str(vault)],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    # Repo NOT re-cloned, binding on the paper KEPT.
    assert not (vault / "codes" / "RepoY").exists()
    assert _read_meta(vault, "2024_Target")["code-clones"] == ["RepoY"]
    assert "Kept binding" in result.output


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
    assert not (vault / "codes" / "RepoZ").exists()
    assert _read_meta(vault, "2024_Target")["code-clones"] == ["RepoZ"]
    assert "failed" in result.output.lower()


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


def test_restore_writes_restored_log_row(vault: Path) -> None:
    _write_paper(vault, "2024_Foo_Bar", title="The Foo")
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo_Bar", "-y", "--library", str(vault)])
    runner.invoke(
        cli, ["trash", "restore", "2024_Foo_Bar", "--library", str(vault)]
    )
    log = (vault / ".deletion-log.jsonl").read_text().strip().splitlines()
    # One trashed row + one restored row.
    actions = [json.loads(line)["action"] for line in log]
    assert "trashed" in actions
    assert "restored" in actions
    restored_row = json.loads(
        next(line for line in log if json.loads(line)["action"] == "restored")
    )
    assert restored_row["id"] == "2024_Foo_Bar"
    assert restored_row["title"] == "The Foo"
    assert "at" in restored_row


def test_restore_refreshes_index_and_views(vault: Path) -> None:
    _write_paper(vault, "2024_Foo", topics=["alpha"])
    runner = CliRunner()
    runner.invoke(cli, ["rm", "2024_Foo", "-y", "--library", str(vault)])
    runner.invoke(cli, ["trash", "restore", "2024_Foo", "--library", str(vault)])

    payload = json.loads((vault / "INDEX.json").read_text())
    assert "2024_Foo" in [p["id"] for p in payload["papers"]]
    assert (vault / "views/by-topic/alpha/2024_Foo").is_symlink()


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
