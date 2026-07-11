"""The bound vault vanishes mid-session — ``_guard_vault``'s 410 branch.

``lit gui`` binds one vault for the life of the process (``app.state.vault``).
Nothing in POSIX stops the user from moving or deleting that directory while the
browser is open: a rename acts on the parent's directory entry, so an open file
descriptor, a cwd, even a running server cannot veto it. The process gets no
notification. Before this guard the server kept serving the dead path, and both
halves of it lied:

* **reads** — a missing ``papers/`` makes ``list_papers`` return ``[]``, so
  ``GET /api/papers`` answered ``200 []`` and the GUI rendered *your library is
  empty*, which is a far worse thing to say to someone who owns 19 papers than
  *I can't find it*.
* **writes** — ``staged_write`` mkdirs its staging root with ``parents=True``
  and the commit mkdirs the paper's parent the same way, so a note saved after
  the move would *rebuild* a one-paper ghost library at the dead path, report
  success, and silently fork the user's data.

The guard is one ``stat`` per ``/api/`` request, placed before the route so the
write dies before it touches the disk. It answers **410 Gone** — never the 409
the welcome page keys off, because telling this user to "create your first
library" invites them to create a second one on top of the wreckage.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    load_registry,
    save_registry,
)
from litman.server import create_app


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _move_away(vault: Path, dest: Path) -> Path:
    """Move the whole vault, the way a user drags a folder in a file manager."""
    shutil.move(str(vault), str(dest))
    assert not vault.exists()
    return dest


# ===========================================================================
# reads: 410, not a 200 that claims the library is empty
# ===========================================================================


def test_moved_vault_read_is_410_not_empty_list(tmp_path: Path) -> None:
    """The regression that started this: ``200 []`` — *your library is empty*."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    assert client.get("/api/papers").status_code == 200  # alive

    _move_away(vault, tmp_path / "moved")

    resp = client.get("/api/papers")
    assert resp.status_code == 410
    assert resp.json() != []


def test_410_body_names_the_path_it_lost(tmp_path: Path) -> None:
    """The banner needs the dead path to tell the user *which* library went."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    body = client.get("/api/papers").json()
    assert Path(body["path"]) == vault
    assert str(vault) in body["detail"]


def test_deleted_vault_is_410(tmp_path: Path) -> None:
    """Deleted, not moved — same verdict, the server can no longer see it."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    shutil.rmtree(vault)

    assert client.get("/api/papers").status_code == 410


# ===========================================================================
# writes: refused BEFORE staged_write can rebuild the vault it lost
# ===========================================================================


def test_write_after_move_does_not_rebuild_a_ghost_vault(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """The data-forking one. ``staged_write`` mkdirs ``parents=True``; without the
    guard this write reconstructs the vault at the dead path, saves the note into
    it, and answers 200 — leaving the real library short one note."""
    vault, paper_id = vault_with_paper
    client = _client(vault)

    _move_away(vault, tmp_path / "moved")

    resp = client.put(f"/api/paper/{paper_id}/notes", json={"text": "# Notes\n\nbody\n"})
    assert resp.status_code == 410
    # Nothing was resurrected at the old path — not the vault root, not the
    # staging area, not the paper folder.
    assert not vault.exists()


def test_pdf_annotation_write_after_move_is_410(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """Every write goes through the same door, not just the markdown ones."""
    vault, paper_id = vault_with_paper
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    resp = client.put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=b"%PDF-1.4\n",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 410
    assert not vault.exists()


# ===========================================================================
# the sentinel is lit-config.yaml, not the directory
# ===========================================================================


def test_directory_without_lit_config_is_still_gone(tmp_path: Path) -> None:
    """A carcass at the old path does not count as the library coming back.

    This is exactly what a pre-guard write left behind (``staged_write`` never
    writes a ``lit-config.yaml``), and it is also what happens when the user moves
    the vault and then creates an unrelated folder with the same name.
    """
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    (vault / "papers").mkdir(parents=True)
    assert client.get("/api/papers").status_code == 410


def test_vault_moved_back_heals_itself(tmp_path: Path) -> None:
    """Undo the move and the next request just works — the guard holds no state,
    so the frontend's 5s retry recovers with no restart and no user action."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    moved = _move_away(vault, tmp_path / "moved")
    assert client.get("/api/papers").status_code == 410

    shutil.move(str(moved), str(vault))
    assert client.get("/api/papers").status_code == 200


# ===========================================================================
# 410 ≠ 409: the welcome page must never be offered to a user who HAS a library
# ===========================================================================


def test_no_vault_is_409_not_410(tmp_path: Path) -> None:
    """A server that never had a vault keeps its 409 — the SPA keys the welcome
    page ("create your first library") off that code, and a user whose library
    merely moved must not be sent down that path."""
    assert TestClient(create_app(None)).get("/api/papers").status_code == 409


def test_gone_vault_keeps_the_escape_doors_open(tmp_path: Path) -> None:
    """The recovery routes stay reachable in the gone state — otherwise the user
    is locked out of the very dialog that fixes it."""
    vault = create_vault(tmp_path, name="lib")
    client = _client(vault)
    _move_away(vault, tmp_path / "moved")

    resp = client.get("/api/vaults")
    assert resp.status_code == 200
    assert Path(resp.json()["served"]) == vault  # still bound to the dead path


def test_re_registering_the_new_path_recovers(tmp_path: Path) -> None:
    """End-to-end recovery, entirely through the doors the guard leaves open:
    register the vault where it now lives, switch to it, and the server is whole
    again in place — no restart."""
    vault = create_vault(tmp_path, name="lib")
    save_registry(add_vault(load_registry(), "lib", vault))
    client = _client(vault)

    moved = _move_away(vault, tmp_path / "moved")
    assert client.get("/api/papers").status_code == 410

    assert client.post("/api/vaults", json={"name": "lib2", "path": str(moved)}).status_code == 200
    assert client.put("/api/vaults/active", json={"name": "lib2"}).status_code == 200

    assert client.get("/api/papers").status_code == 200
