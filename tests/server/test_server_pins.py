"""Pin endpoint tests (task-gui-pin, AC-3/AC-4/AC-5).

Covers the four /api/pins routes: idempotent PUT/DELETE, order preservation
(list order IS pin order), dangling-pin pruning with write-back, the
traversal-id guard, and the every-write-returns-the-full-list contract the
SPA renders verbatim.

Pins persist in the machine-level ``ui-state.json`` (isolated per test by
the autouse ``_isolate_registry`` fixture via ``$LITMAN_REGISTRY_DIR``),
never in the vault. Guarded with ``importorskip`` so the suite still
collects when the optional ``web`` extra is absent (invariant #5)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.ui_state import load_pins, ui_state_path
from litman.server import create_app
from litman.core import locking


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _add_paper(vault: Path, paper_id: str) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\n", encoding="utf-8"
    )


def test_get_pins_empty(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/pins")
    assert resp.status_code == 200
    assert resp.json() == {"pins": []}


def test_put_pin_appends_and_persists(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    _add_paper(vault, "2020_Second_Paper")
    client = _client(vault)

    resp = client.put(f"/api/pins/{paper_id}")
    assert resp.status_code == 200
    assert resp.json() == {"pins": [paper_id]}

    resp = client.put("/api/pins/2020_Second_Paper")
    assert resp.json() == {"pins": [paper_id, "2020_Second_Paper"]}

    # Persisted to the machine-level state file, not anywhere in the vault.
    assert load_pins(vault) == [paper_id, "2020_Second_Paper"]
    assert not (vault / "ui-state.json").exists()


def test_put_pin_idempotent(vault_with_paper: tuple[Path, str]) -> None:
    """AC-3: re-pinning keeps length AND position (no move-to-end)."""
    vault, paper_id = vault_with_paper
    _add_paper(vault, "2020_Second_Paper")
    client = _client(vault)
    client.put(f"/api/pins/{paper_id}")
    client.put("/api/pins/2020_Second_Paper")

    resp = client.put(f"/api/pins/{paper_id}")  # re-pin the FIRST one
    assert resp.status_code == 200
    assert resp.json() == {"pins": [paper_id, "2020_Second_Paper"]}


def test_delete_pin_removes_and_is_idempotent(
    vault_with_paper: tuple[Path, str],
) -> None:
    """AC-3: unpin works; unpinning an unpinned id is a 200 no-op."""
    vault, paper_id = vault_with_paper
    client = _client(vault)
    client.put(f"/api/pins/{paper_id}")

    resp = client.delete(f"/api/pins/{paper_id}")
    assert resp.status_code == 200
    assert resp.json() == {"pins": []}

    resp = client.delete(f"/api/pins/{paper_id}")  # already gone
    assert resp.status_code == 200
    assert resp.json() == {"pins": []}


def test_clear_pins(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    _add_paper(vault, "2020_Second_Paper")
    client = _client(vault)
    client.put(f"/api/pins/{paper_id}")
    client.put("/api/pins/2020_Second_Paper")

    resp = client.delete("/api/pins")
    assert resp.status_code == 200
    assert resp.json() == {"pins": []}
    assert load_pins(vault) == []


def test_get_prunes_dangling_pin_and_writes_back(
    vault_with_paper: tuple[Path, str],
) -> None:
    """AC-4: a pin whose paper dir was removed disappears from GET, and the
    pruned list is persisted (a second GET needs no re-prune)."""
    vault, paper_id = vault_with_paper
    _add_paper(vault, "2020_Second_Paper")
    client = _client(vault)
    client.put(f"/api/pins/{paper_id}")
    client.put("/api/pins/2020_Second_Paper")

    locking.rmtree(vault / "papers" / "2020_Second_Paper")

    resp = client.get("/api/pins")
    assert resp.json() == {"pins": [paper_id]}
    # Write-back happened: the state file itself no longer holds the ghost.
    assert load_pins(vault) == [paper_id]
    # Pruning drops pin entries ONLY — the surviving paper is untouched.
    assert (vault / "papers" / paper_id / "metadata.yaml").is_file()


def test_put_unknown_paper_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).put("/api/pins/2099_Nobody_Nothing")
    assert resp.status_code == 404
    assert load_pins(vault) == []


def test_put_traversal_id_rejected_writes_nothing(
    vault_with_paper: tuple[Path, str],
) -> None:
    """AC-5: traversal-style ids are refused and no state file appears.

    Same two shapes as ``test_traversal_id_rejected`` in test_server_read:
    the percent-encoded ``../`` form dies in Starlette's router (any 4xx —
    routing, not our code), and the single-segment ``foo..bar`` form reaches
    the handler and proves the ``is_valid_id`` guard fires."""
    vault, _ = vault_with_paper
    client = _client(vault)

    encoded = client.put("/api/pins/..%2F..%2Fetc")
    assert 400 <= encoded.status_code < 500

    guarded = client.put("/api/pins/foo..bar")
    assert guarded.status_code == 404
    assert guarded.json()["detail"] == "Invalid paper id: 'foo..bar'."

    assert not ui_state_path().exists()
