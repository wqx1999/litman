"""No-vault (welcome-page) server tests — task-gui-welcome AC1/AC3/AC4.

Two surfaces:

* ``create_app(None)`` — a server that started with no vault to serve. The
  ``_guard_vault`` middleware 409s every vault-dependent ``/api/`` route while
  a short whitelist (vault list / create / open / version) stays reachable so the
  welcome page can bootstrap and create a library.
* ``POST /api/vaults/create`` — the create-and-register endpoint, same core path
  as ``lit init`` (:func:`litman.commands.init.apply_init`). The first vault
  becomes active and repoints the running server in place (no restart).

The registry is redirected to a per-test temp dir by the autouse
``_isolate_registry`` fixture (tests/conftest.py), so ``create_app(None)`` starts
from a genuinely empty registry.

The final test is a REAL end-to-end (inject-seam lesson): it launches an actual
``lit gui`` process with no vault and creates a library over real HTTP — the live
``find_vault → None → create_app(None) → uvicorn`` path a bare TestClient skips.

Guarded with ``importorskip`` so the suite still collects without fastapi
(invariant #5).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import litman
from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    find_by_name,
    load_registry,
    save_registry,
)
from litman.server import create_app


def _register(vault: Path, name: str) -> None:
    reg = add_vault(load_registry(), name, vault)
    save_registry(reg)


# ===========================================================================
# no-vault middleware guard (AC1)
# ===========================================================================


def test_no_vault_blocks_vault_dependent_route_409() -> None:
    """A vault-dependent route returns a clean 409 (not a 500) with no vault."""
    resp = TestClient(create_app(None)).get("/api/papers")
    assert resp.status_code == 409
    assert "detail" in resp.json()


@pytest.mark.parametrize("path", ["/api/vaults", "/api/version"])
def test_no_vault_allows_whitelisted_gets(path: str) -> None:
    """The welcome page's bootstrap reads (vault list + version) stay reachable."""
    resp = TestClient(create_app(None)).get(path)
    assert resp.status_code == 200


def test_no_vault_vaults_list_reports_served_null() -> None:
    """With no vault served, ``served`` is null — the frontend's welcome signal."""
    resp = TestClient(create_app(None)).get("/api/vaults")
    assert resp.status_code == 200
    assert resp.json()["served"] is None


def test_served_field_is_the_bound_vault(tmp_path: Path) -> None:
    """With a vault served, ``served`` is its path (distinct from ``active``)."""
    vault = create_vault(tmp_path, name="lib")
    resp = TestClient(create_app(vault)).get("/api/vaults")
    assert resp.status_code == 200
    assert Path(resp.json()["served"]).resolve() == vault.resolve()


def test_served_vault_does_not_block_routes(tmp_path: Path) -> None:
    """The guard only fires when the vault is None — a served vault passes."""
    vault = create_vault(tmp_path, name="lib")
    resp = TestClient(create_app(vault)).get("/api/papers")
    assert resp.status_code == 200


# ===========================================================================
# POST /api/vaults/create (AC1 / AC3)
# ===========================================================================


def test_create_first_vault_becomes_active_and_repoints(tmp_path: Path) -> None:
    """First vault → active → server repointed in place → routes unblock."""
    app = create_app(None)
    with TestClient(app) as client:
        assert client.get("/api/papers").status_code == 409  # blocked before

        resp = client.post(
            "/api/vaults/create",
            json={"parent_dir": str(tmp_path), "name": "firstlib"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["active"] is True
        assert Path(body["path"]).resolve() == (tmp_path / "firstlib").resolve()

        # Registered, active, and health-checked at creation (AC1: not stale).
        entry = find_by_name(load_registry(), "firstlib")
        assert entry is not None and entry.is_active
        assert entry.last_health_check_at is not None

        # Server repointed in place — the same client now serves the new vault.
        assert app.state.vault == tmp_path / "firstlib"
        assert client.get("/api/papers").status_code == 200
        assert client.get("/api/vaults").json()["served"] is not None


def test_create_default_name_when_omitted(tmp_path: Path) -> None:
    """An omitted name defaults to ``literature_vault`` (matches ``lit init``)."""
    resp = TestClient(create_app(None)).post(
        "/api/vaults/create", json={"parent_dir": str(tmp_path)}
    )
    assert resp.status_code == 200
    assert Path(resp.json()["path"]).name == "literature_vault"


def test_create_second_vault_does_not_repoint(tmp_path: Path) -> None:
    """A create while a vault is already active leaves the served vault put."""
    served = create_vault(tmp_path, name="served")
    _register(served, "served")
    app = create_app(served)
    resp = TestClient(app).post(
        "/api/vaults/create",
        json={"parent_dir": str(tmp_path), "name": "second"},
    )
    assert resp.status_code == 200
    assert resp.json()["active"] is False
    assert app.state.vault == served  # unchanged


def test_create_missing_parent_dir_400(tmp_path: Path) -> None:
    resp = TestClient(create_app(None)).post(
        "/api/vaults/create", json={"parent_dir": str(tmp_path / "nope"), "name": "x"}
    )
    assert resp.status_code == 400
    assert "does not exist" in resp.json()["detail"]


def test_create_target_non_empty_400(tmp_path: Path) -> None:
    """A non-empty target vault path is a 400 (never clobbered)."""
    create_vault(tmp_path, name="lib")  # first create leaves lib/ populated
    resp = TestClient(create_app(None)).post(
        "/api/vaults/create", json={"parent_dir": str(tmp_path), "name": "lib"}
    )
    assert resp.status_code == 400


def test_create_duplicate_registry_name_400(tmp_path: Path) -> None:
    existing = create_vault(tmp_path, name="dupe")
    _register(existing, "dupe")
    # A different directory but the same registry name.
    resp = TestClient(create_app(None)).post(
        "/api/vaults/create", json={"parent_dir": str(tmp_path), "name": "dupe"}
    )
    assert resp.status_code == 400


def test_create_no_parent_dir_key_400() -> None:
    resp = TestClient(create_app(None)).post("/api/vaults/create", json={"name": "x"})
    assert resp.status_code == 400


def test_create_non_string_parent_dir_400() -> None:
    resp = TestClient(create_app(None)).post(
        "/api/vaults/create", json={"parent_dir": ["/tmp"]}
    )
    assert resp.status_code == 400


def test_create_non_dict_body_400() -> None:
    resp = TestClient(create_app(None)).post("/api/vaults/create", json=["nope"])
    assert resp.status_code == 400


# ===========================================================================
# real end-to-end: a live `lit gui` with no vault creates a library (AC4)
# ===========================================================================


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get_json(url: str, timeout: float = 5.0) -> tuple[int, object]:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def _post_json(url: str, body: dict, timeout: float = 5.0) -> tuple[int, object]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_e2e_lit_gui_no_vault_creates_library(tmp_path: Path) -> None:
    """Launch a REAL ``lit gui`` with no vault, then create a library over HTTP.

    Drives the live ``find_vault → None → create_app(None) → uvicorn`` path that a
    TestClient cannot: if gui.py mis-wires no-vault startup, this is the test that
    breaks (inject-seam lesson — a monkeypatched default would hide it).
    """
    home = tmp_path / "home"
    home.mkdir()
    parent = tmp_path / "libs"
    parent.mkdir()

    env = os.environ.copy()
    src_dir = Path(litman.__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    # Empty, isolated registry so find_vault discovers nothing → no-vault mode.
    env["LITMAN_REGISTRY_DIR"] = str(home / "litman-registry")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "litman", "gui", "--no-browser", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Poll until the server answers (or the process died).
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"lit gui exited early: {proc.stdout.read() if proc.stdout else ''}"
                )
            try:
                status, payload = _get_json(f"{base}/api/vaults", timeout=1.0)
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
                continue
            if status == 200:
                assert isinstance(payload, dict)
                assert payload["served"] is None  # started with no vault
                ready = True
                break
        assert ready, "server never became ready"

        # A vault-dependent route is guarded until we create one.
        status, _ = _get_json(f"{base}/api/version")  # whitelisted, works
        assert status == 200

        # Create a library over real HTTP.
        status, body = _post_json(
            f"{base}/api/vaults/create",
            {"parent_dir": str(parent), "name": "mylib"},
        )
        assert status == 200, body
        assert isinstance(body, dict)
        assert body["active"] is True
        assert (parent / "mylib" / "lit-config.yaml").is_file()

        # Server repointed in place → the vault is now served, no restart.
        status, payload = _get_json(f"{base}/api/vaults")
        assert isinstance(payload, dict)
        assert Path(payload["served"]).resolve() == (parent / "mylib").resolve()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
