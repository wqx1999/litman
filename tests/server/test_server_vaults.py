"""Vault-manager API endpoint tests for the webUI server (vault-manager slice).

Covers the two new registry-write routes that let a GUI-only user register an
existing vault and unregister one without dropping to the CLI:

* ``POST /api/vaults`` — pure registry append via ``core.vault_registry.add_vault``;
  never touches ``app.state.vault`` / the active flag (invariant #16, no second
  write path). Happy register, non-vault dir → 400, duplicate name → 400.
* ``DELETE /api/vaults/{name}`` — unregister via ``remove_vault``; GUARDS the
  served vault (== ``app.state.vault``) with a 409 before any mutation, never
  deletes the directory on disk, unknown name → 400.

Every assertion verifies the routes are thin wrappers over core and produce
zero side effects beyond ``vaults.yaml``: ``app.state.vault`` is unchanged and
no vault directory is removed.

The registry file is redirected to a per-test temp dir by the autouse
``_isolate_registry`` fixture in ``tests/conftest.py``, so these tests start
from an empty registry and register their own vaults.

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    find_by_name,
    load_registry,
    save_registry,
)
from litman.server import create_app


def _register(vault: Path, name: str) -> None:
    """Append ``vault`` to the registry under ``name`` (persisted)."""
    reg = add_vault(load_registry(), name, vault)
    save_registry(reg)


# ===========================================================================
# POST /api/vaults — register an existing vault (AC1)
# ===========================================================================


def test_post_vault_registers_existing_vault(tmp_path: Path) -> None:
    """A directory with lit-config.yaml registers → 200 and lands in vaults.yaml."""
    served = create_vault(tmp_path, name="served")
    target = create_vault(tmp_path, name="target")
    _register(served, "served")  # the served vault is the one create_app gets
    app = create_app(served)

    resp = TestClient(app).post(
        "/api/vaults", json={"name": "target", "path": str(target)}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["name"] == "target"
    assert Path(body["path"]).resolve() == target.resolve()

    # The new entry is on disk.
    entry = find_by_name(load_registry(), "target")
    assert entry is not None
    assert Path(entry.path).resolve() == target.resolve()


def test_post_vault_non_vault_dir_400(tmp_path: Path) -> None:
    """A directory without lit-config.yaml is not a vault → 400 (verbatim core)."""
    served = create_vault(tmp_path, name="served")
    _register(served, "served")
    not_a_vault = tmp_path / "plain-dir"
    not_a_vault.mkdir()
    app = create_app(served)

    resp = TestClient(app).post(
        "/api/vaults", json={"name": "bogus", "path": str(not_a_vault)}
    )
    assert resp.status_code == 400
    assert "lit-config.yaml" in resp.json()["detail"]
    # Nothing registered beyond the served vault.
    assert find_by_name(load_registry(), "bogus") is None


def test_post_vault_duplicate_name_400(tmp_path: Path) -> None:
    """Re-registering an already-registered name → 400."""
    served = create_vault(tmp_path, name="served")
    other = create_vault(tmp_path, name="other")
    _register(served, "served")
    _register(other, "dupe")
    app = create_app(served)

    again = create_vault(tmp_path, name="again")
    resp = TestClient(app).post(
        "/api/vaults", json={"name": "dupe", "path": str(again)}
    )
    assert resp.status_code == 400
    assert "already registered" in resp.json()["detail"]
    # The original "dupe" still points at the first dir, not the second.
    entry = find_by_name(load_registry(), "dupe")
    assert entry is not None
    assert Path(entry.path).resolve() == other.resolve()


def test_post_vault_missing_path_400(tmp_path: Path) -> None:
    """A body without a path is a client bug → 400 (no core call)."""
    served = create_vault(tmp_path, name="served")
    _register(served, "served")
    resp = TestClient(create_app(served)).post("/api/vaults", json={"name": "x"})
    assert resp.status_code == 400


def test_post_vault_does_not_change_active_or_app_state(tmp_path: Path) -> None:
    """Registration is active-agnostic: served stays active, app.state unchanged."""
    served = create_vault(tmp_path, name="served")
    target = create_vault(tmp_path, name="target")
    _register(served, "served")  # first entry → auto-active
    app = create_app(served)
    before = Path(app.state.vault).resolve()

    resp = TestClient(app).post(
        "/api/vaults", json={"name": "target", "path": str(target)}
    )
    assert resp.status_code == 200
    # The registered entry is NOT active (served keeps the active flag).
    new_entry = find_by_name(load_registry(), "target")
    assert new_entry is not None and new_entry.is_active is False
    served_entry = find_by_name(load_registry(), "served")
    assert served_entry is not None and served_entry.is_active is True
    # The live server still serves the original vault.
    assert Path(app.state.vault).resolve() == before
    # Registration never deletes the registered directory (AC4, register half).
    assert target.is_dir()


# ===========================================================================
# DELETE /api/vaults/{name} — unregister + served-vault guard (AC2/AC3/AC4)
# ===========================================================================


def test_delete_vault_unregisters_non_served_and_keeps_dir(tmp_path: Path) -> None:
    """Unregistering a non-served vault → 200, dropped from registry, dir kept."""
    served = create_vault(tmp_path, name="served")
    target = create_vault(tmp_path, name="target")
    _register(served, "served")
    _register(target, "target")
    app = create_app(served)

    resp = TestClient(app).delete("/api/vaults/target")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Registry no longer lists it.
    assert find_by_name(load_registry(), "target") is None
    # AC2: the vault directory on disk still exists.
    assert target.is_dir()
    assert (target / "lit-config.yaml").is_file()


def test_delete_vault_served_guard_409(tmp_path: Path) -> None:
    """Deleting the served vault (== app.state.vault) → 409, registry unchanged."""
    served = create_vault(tmp_path, name="served")
    other = create_vault(tmp_path, name="other")
    _register(served, "served")
    _register(other, "other")
    app = create_app(served)

    resp = TestClient(app).delete("/api/vaults/served")
    assert resp.status_code == 409
    assert "switch to another vault first" in resp.json()["detail"].lower()
    # Registry unchanged: the served entry is still there.
    assert find_by_name(load_registry(), "served") is not None
    # No side effects: served still serves, dir still present.
    assert Path(app.state.vault).resolve() == served.resolve()
    assert served.is_dir()


def test_delete_vault_unknown_name_400(tmp_path: Path) -> None:
    """An unregistered name → remove_vault raises VaultRegistryError → 400."""
    served = create_vault(tmp_path, name="served")
    _register(served, "served")
    resp = TestClient(create_app(served)).delete("/api/vaults/ghost")
    assert resp.status_code == 400
    # Served vault untouched.
    assert find_by_name(load_registry(), "served") is not None


def test_delete_vault_no_app_state_change(tmp_path: Path) -> None:
    """AC4: a successful unregister leaves app.state.vault unchanged."""
    served = create_vault(tmp_path, name="served")
    target = create_vault(tmp_path, name="target")
    _register(served, "served")
    _register(target, "target")
    app = create_app(served)
    before = Path(app.state.vault).resolve()

    resp = TestClient(app).delete("/api/vaults/target")
    assert resp.status_code == 200
    assert Path(app.state.vault).resolve() == before
