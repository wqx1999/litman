"""Trash (recoverable-delete bin) API endpoint tests for the webUI server (A8).

Covers Phase 4.9: the read-only "trash library" endpoints and the one-click
restore endpoint. Every assertion verifies the endpoint is a thin wrapper over
``core.trash`` (invariant #16) — restore really moves the paper back, rebuilds
INDEX + reverse edges, returns ``missing_repos``, never re-clones — and that no
``empty``/``purge`` route exists (GUI never permanently deletes).

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.trash import list_trash, move_to_trash
from litman.server import create_app

_yaml = YAML(typ="safe")


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    """Write a minimal valid paper folder (canonical M2 schema)."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": fields.get("title", f"Title of {paper_id}"),
        "authors": ["Doe, Jane"],
        "year": 2024,
        "journal": "Test J.",
        "doi": f"10.0/{paper_id}",
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": [],
        "topics": fields.get("topics", []),
        "methods": [],
        "data": [],
        "type": "research",
        "status": "inbox",
        "priority": "B",
        "read-date": None,
        "last-revisited": None,
        "related": fields.get("related", []),
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


def _read_meta(vault: Path, paper_id: str) -> dict[str, Any]:
    return _yaml.load(
        (vault / "papers" / paper_id / "metadata.yaml").read_text()
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ===========================================================================
# GET /api/trash — list projection
# ===========================================================================


def test_list_trash_projection_newest_first(vault: Path) -> None:
    """Two trashed entries of the same id return newest-first, expected keys.

    Same paper id for both so the in-name UTC timestamp (not the id prefix)
    drives the newest-first ordering — mirrors the CLI ``list_trash`` ordering
    contract.
    """
    import time

    _write_paper(vault, "2024_Foo", title="Old version")
    move_to_trash(vault, "2024_Foo")
    time.sleep(1.1)  # distinct UTC-second timestamp in the entry name
    _write_paper(vault, "2024_Foo", title="New version")
    move_to_trash(vault, "2024_Foo")

    resp = _client(vault).get("/api/trash")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # Newest-first: the second-trashed ("New version") leads.
    assert body[0]["title"] == "New version"
    assert body[1]["title"] == "Old version"
    assert body[0]["entry_name"] > body[1]["entry_name"]

    row = body[0]
    assert set(row) == {
        "paper_id",
        "title",
        "deleted_at",
        "entry_name",
        "orphan_repo_count",
    }
    assert row["paper_id"] == "2024_Foo"
    assert row["orphan_repo_count"] == 0
    # entry_name is the on-disk dir name (id + timestamp), not just the id.
    assert row["entry_name"].startswith("2024_Foo-")


def test_list_trash_empty(vault: Path) -> None:
    resp = _client(vault).get("/api/trash")
    assert resp.status_code == 200
    assert resp.json() == []


# ===========================================================================
# trash-scoped reads — resolve via resolve_trash_entry, bad entry → 404
# ===========================================================================


def test_get_trash_metadata(vault: Path) -> None:
    _write_paper(vault, "2024_Foo", title="Trashed me")
    move_to_trash(vault, "2024_Foo")
    entry_name = list_trash(vault)[0].entry_name

    resp = _client(vault).get(f"/api/trash/{entry_name}")
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["id"] == "2024_Foo"
    assert meta["title"] == "Trashed me"


def test_get_trash_metadata_empty_is_404(vault: Path) -> None:
    """An empty / comment-only trashed metadata.yaml is a 404, not a silent {}.

    Mirrors routes_read.get_paper, whose find_paper treats a paper with no usable
    metadata as not-found rather than serving an empty object.
    """
    _write_paper(vault, "2024_Foo")
    move_to_trash(vault, "2024_Foo")
    entry = list_trash(vault)[0]
    # Blank out the trashed metadata to a comment-only file (read_metadata → {}).
    (entry.entry_path / "metadata.yaml").write_text("# (empty)\n", encoding="utf-8")

    resp = _client(vault).get(f"/api/trash/{entry.entry_name}")
    assert resp.status_code == 404


def test_get_trash_metadata_corrupt_is_500(vault: Path) -> None:
    """Unparseable trashed metadata.yaml is a 500 (CorruptMetadataError), matching
    routes_read.get_paper's corrupt-vs-missing split."""
    _write_paper(vault, "2024_Foo")
    move_to_trash(vault, "2024_Foo")
    entry = list_trash(vault)[0]
    (entry.entry_path / "metadata.yaml").write_text(
        "title: [unclosed\n", encoding="utf-8"
    )

    resp = _client(vault).get(f"/api/trash/{entry.entry_name}")
    assert resp.status_code == 500


def test_get_trash_notes_and_discussion(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    paper_dir = vault / "papers" / "2024_Foo"
    (paper_dir / "notes.md").write_text("# notes body", encoding="utf-8")
    (paper_dir / "discussion.md").write_text("disc body", encoding="utf-8")
    move_to_trash(vault, "2024_Foo")
    entry_name = list_trash(vault)[0].entry_name

    client = _client(vault)
    notes = client.get(f"/api/trash/{entry_name}/notes")
    assert notes.status_code == 200
    assert notes.json()["text"] == "# notes body"

    disc = client.get(f"/api/trash/{entry_name}/discussion")
    assert disc.status_code == 200
    assert disc.json()["text"] == "disc body"


def test_get_trash_notes_404_when_absent(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")  # no notes.md
    move_to_trash(vault, "2024_Foo")
    entry_name = list_trash(vault)[0].entry_name

    resp = _client(vault).get(f"/api/trash/{entry_name}/notes")
    assert resp.status_code == 404


def test_get_trash_pdf_with_range(vault: Path, make_text_pdf: Any) -> None:
    _write_paper(vault, "2024_Foo")
    src_pdf = make_text_pdf([["hello pdf"]])
    (vault / "papers" / "2024_Foo" / "paper.pdf").write_bytes(
        src_pdf.read_bytes()
    )
    move_to_trash(vault, "2024_Foo")
    entry_name = list_trash(vault)[0].entry_name

    client = _client(vault)
    full = client.get(f"/api/trash/{entry_name}/pdf")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"

    partial = client.get(
        f"/api/trash/{entry_name}/pdf", headers={"Range": "bytes=0-49"}
    )
    assert partial.status_code == 206
    assert len(partial.content) == 50


@pytest.mark.parametrize("suffix", ["", "/notes", "/discussion", "/pdf"])
def test_bad_entry_name_is_404_no_path_leak(
    vault: Path, tmp_path: Path, suffix: str
) -> None:
    """A bad entry_name 404s on every read endpoint and serves nothing outside.

    The handler resolves through ``resolve_trash_entry`` and reads only relative
    to the returned ``entry_path`` — it never synthesizes ``.trash/<param>/``
    from the URL, so a traversal-style param cannot escape the vault.
    """
    _write_paper(vault, "2024_Foo")
    move_to_trash(vault, "2024_Foo")  # non-empty trash, but wrong entry asked

    secret = tmp_path / "outside_secret"
    secret.write_text("TOP SECRET\n", encoding="utf-8")

    client = _client(vault)
    resp = client.get(f"/api/trash/no_such_entry{suffix}")
    assert resp.status_code == 404

    # A single-segment traversal param reaches the handler; it must not leak.
    traversal = client.get(f"/api/trash/..%2f..%2foutside_secret{suffix}")
    assert traversal.status_code == 404
    assert "TOP SECRET" not in traversal.text


def test_restore_bad_entry_name_404(vault: Path) -> None:
    _write_paper(vault, "2024_Foo")
    move_to_trash(vault, "2024_Foo")
    resp = _client(vault).post("/api/trash/no_such_entry/restore")
    assert resp.status_code == 404


# ===========================================================================
# POST /api/trash/{entry}/restore
# ===========================================================================


def test_restore_moves_paper_back_and_rebuilds_index(vault: Path) -> None:
    """Restore lands the paper in papers/<id>/ ∧ INDEX ∧ derived consistent."""
    _write_paper(vault, "2024_Foo", topics=["alpha"])
    CliRunner().invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])
    assert not (vault / "papers" / "2024_Foo").exists()

    entry_name = list_trash(vault)[0].entry_name
    resp = _client(vault).post(f"/api/trash/{entry_name}/restore")
    assert resp.status_code == 200

    summary = resp.json()
    assert summary["paper_id"] == "2024_Foo"
    # restore carried no orphan repo → missing_repos empty (endpoint never
    # re-cloned anything).
    assert summary["missing_repos"] == {}
    assert set(summary) == {
        "paper_id",
        "title",
        "reverse_edges_rebuilt",
        "repos_rebound",
        "projects_rebuilt",
        "missing_repos",
        "dead_edges_dropped",
    }

    # Paper is back on disk.
    assert (vault / "papers" / "2024_Foo" / "metadata.yaml").is_file()
    # Back in INDEX.json (derived recomputed by reconcile_derived).
    payload = json.loads((vault / "INDEX.json").read_text())
    assert "2024_Foo" in [p["id"] for p in payload["papers"]]
    # views rebuilt: by-topic/alpha/2024_Foo symlink is back.
    assert (vault / "views/by-topic/alpha/2024_Foo").is_symlink()
    # Trash is now empty.
    assert list_trash(vault) == []


def test_restore_rebuilds_reverse_edges(vault: Path) -> None:
    """Restoring a paper re-adds it to the opposite paper's paired field.

    Seed a symmetric `related` pair, trash one (cascade clears the opposite's
    reverse edge), then restore — the opposite's `related` must regain the id.
    """
    _write_paper(vault, "2024_Foo", related=["2024_Holder"])
    _write_paper(vault, "2024_Holder", related=["2024_Foo"])
    CliRunner().invoke(cli, ["rm", "2024_Foo", "--yes", "--library", str(vault)])
    # rm cascade dropped Foo from Holder.related.
    assert _read_meta(vault, "2024_Holder")["related"] == []

    entry_name = list_trash(vault)[0].entry_name
    resp = _client(vault).post(f"/api/trash/{entry_name}/restore")
    assert resp.status_code == 200
    assert resp.json()["reverse_edges_rebuilt"] == ["2024_Holder"]

    # The reverse edge is back in the holder's metadata.
    assert _read_meta(vault, "2024_Holder")["related"] == ["2024_Foo"]


def test_restore_live_id_collision_409(vault: Path) -> None:
    """A live paper at the trashed id → restore refuses with 409, verbatim."""
    _write_paper(vault, "2024_Foo", title="Original")
    move_to_trash(vault, "2024_Foo")
    # A live paper now occupies the id slot.
    _write_paper(vault, "2024_Foo", title="New content")

    entry_name = list_trash(vault)[0].entry_name
    resp = _client(vault).post(f"/api/trash/{entry_name}/restore")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    # Verbatim message from restore_from_trash (TrashError).
    assert "already exists" in detail
    assert "2024_Foo" in detail
    # The trashed entry is untouched; the live paper still has its content.
    assert _read_meta(vault, "2024_Foo")["title"] == "New content"


# ===========================================================================
# No empty / purge endpoint — GUI never permanently deletes
# ===========================================================================


def test_no_empty_or_purge_endpoint(vault: Path) -> None:
    """The GUI must never expose permanent deletion of trash."""
    _write_paper(vault, "2024_Foo")
    move_to_trash(vault, "2024_Foo")
    client = _client(vault)

    # No DELETE on the trash collection / entry.
    assert client.delete("/api/trash").status_code in (404, 405)
    entry_name = list_trash(vault)[0].entry_name
    assert client.delete(f"/api/trash/{entry_name}").status_code in (404, 405)
    # No empty / purge action route.
    assert client.post("/api/trash/empty").status_code in (404, 405)

    # The paper is still recoverable (nothing was permanently removed).
    assert len(list_trash(vault)) == 1
