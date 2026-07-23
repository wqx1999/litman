"""Directory-picker endpoint tests — task-path-browser A1–A8.

Real filesystem (``tmp_path``), real FastAPI ``TestClient`` — nothing here
mocks the filesystem; only ``Path.home()`` is monkeypatched (A3/A4) so anchors
and the suggested start are pinned to a temp home. Every test drives
``create_app(None)`` — a server with no active vault — which also proves the
endpoint stays reachable without a vault (A8 makes that the explicit assertion).

Guarded with ``importorskip`` so the suite still collects without fastapi
(invariant #5).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.config import CONFIG_FILENAME
from litman.server import create_app


def _client() -> TestClient:
    # No vault served: the picker must work on the welcome page, before any
    # library exists — so every fs test also exercises the vaultless-allowed
    # path, and A8 asserts it explicitly.
    return TestClient(create_app(None))


# ---------------------------------------------------------------------------
# A1 — lists only directories, case-insensitive name sort, files excluded
# ---------------------------------------------------------------------------
def test_a1_lists_only_dirs_sorted_case_insensitive(tmp_path: Path) -> None:
    for name in ("Banana", "apple", "Cherry"):
        (tmp_path / name).mkdir()
    # Files must never appear — including one that would sort first.
    (tmp_path / "Aardvark.txt").write_text("x")
    (tmp_path / "zebra.md").write_text("y")

    resp = _client().get("/api/fs/list", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    body = resp.json()

    names = [e["name"] for e in body["entries"]]
    # case-insensitive ascending: apple < Banana < Cherry (ASCII sort would
    # put the two capitalized names before "apple").
    assert names == ["apple", "Banana", "Cherry"]
    assert "Aardvark.txt" not in names
    assert "zebra.md" not in names

    assert body["denied"] is False
    assert Path(body["entries"][0]["path"]).resolve() == (tmp_path / "apple").resolve()
    assert Path(body["path"]).resolve() == tmp_path.resolve()
    assert Path(body["parent"]).resolve() == tmp_path.parent.resolve()


# ---------------------------------------------------------------------------
# A2 — is_vault flag, plus the reverse (delete the sentinel → flips to false)
# ---------------------------------------------------------------------------
def test_a2_is_vault_flag_and_reverse(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    (tmp_path / "plain").mkdir()
    (lib / CONFIG_FILENAME).write_text("name: x\n")

    def is_vault_by_name() -> dict[str, bool]:
        resp = _client().get("/api/fs/list", params={"path": str(tmp_path)})
        assert resp.status_code == 200
        return {e["name"]: e["is_vault"] for e in resp.json()["entries"]}

    flags = is_vault_by_name()
    assert flags["lib"] is True
    assert flags["plain"] is False

    # Reverse: remove the sentinel → the SAME dir must flip to false. Proves the
    # badge is actually judging, not returning a constant true.
    (lib / CONFIG_FILENAME).unlink()
    flags_after = is_vault_by_name()
    assert flags_after["lib"] is False
    assert flags_after["plain"] is False


# ---------------------------------------------------------------------------
# A2b — top-level is_vault: the CURRENTLY-listed folder's own vault-ness.
#       Gates the picker's vault-dir mode; must stay correct when the user
#       lands via an anchor / address-bar paste (no clicked entry to remember).
# ---------------------------------------------------------------------------
def test_a2b_current_path_is_vault(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / CONFIG_FILENAME).write_text("name: x\n")
    plain = tmp_path / "plain"
    plain.mkdir()

    def current_is_vault(target: Path) -> bool:
        resp = _client().get("/api/fs/list", params={"path": str(target)})
        assert resp.status_code == 200
        return resp.json()["is_vault"]

    assert current_is_vault(lib) is True
    assert current_is_vault(plain) is False

    # Reverse: remove the sentinel → the same folder's top-level flag flips.
    (lib / CONFIG_FILENAME).unlink()
    assert current_is_vault(lib) is False


# ---------------------------------------------------------------------------
# A3 — anchors: only the standard locations that actually exist
# ---------------------------------------------------------------------------
def test_a3_anchors_only_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Desktop").mkdir()
    # Documents / Downloads deliberately absent.

    resp = _client().get("/api/fs/list", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    anchors = {a["label"]: a["path"] for a in resp.json()["anchors"]}

    # Home always present; Desktop present; the two absent ones are excluded.
    assert set(anchors) == {"Home", "Desktop"}
    assert Path(anchors["Home"]).resolve() == tmp_path.resolve()
    assert Path(anchors["Desktop"]).resolve() == (tmp_path / "Desktop").resolve()


# ---------------------------------------------------------------------------
# A4 — suggested start: first existing of Desktop → Documents → Home
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "make, expected",
    [
        (("Desktop",), "Desktop"),  # Desktop wins
        (("Documents",), "Documents"),  # Documents when no Desktop
        (("Desktop", "Documents"), "Desktop"),  # Desktop precedes Documents
        ((), None),  # neither → Home
    ],
)
def test_a4_suggested_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make: tuple[str, ...],
    expected: str | None,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in make:
        (tmp_path / name).mkdir()

    resp = _client().get("/api/fs/list")  # no path → suggested start
    assert resp.status_code == 200
    landed = Path(resp.json()["path"]).resolve()
    want = tmp_path if expected is None else tmp_path / expected
    assert landed == want.resolve()


# ---------------------------------------------------------------------------
# A5 — bad input → 400 with a human-readable detail (never a silent empty 200)
# ---------------------------------------------------------------------------
def test_a5_nonexistent_path_400(tmp_path: Path) -> None:
    resp = _client().get(
        "/api/fs/list", params={"path": str(tmp_path / "does_not_exist")}
    )
    assert resp.status_code == 400
    assert "does_not_exist" in resp.json()["detail"]


def test_a5_file_not_dir_400(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    resp = _client().get("/api/fs/list", params={"path": str(f)})
    assert resp.status_code == 400
    assert "file.txt" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# A6 — PermissionError listing children → 200 + denied, not a 500
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permission bits; chmod 000 would not deny",
)
def test_a6_permission_denied_degrades_not_500(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        resp = _client().get("/api/fs/list", params={"path": str(locked)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["denied"] is True
        assert body["entries"] == []
        # The top-level is_vault sentinel stat also hits the permission wall;
        # it must degrade to False here, not blow the whole request up to 500.
        assert body["is_vault"] is False
    finally:
        locked.chmod(0o755)  # let tmp_path teardown clean up


# ---------------------------------------------------------------------------
# A7 — hidden (dot-prefixed) directories: hidden by default, shown on request
# ---------------------------------------------------------------------------
def test_a7_hidden_dirs(tmp_path: Path) -> None:
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "visible").mkdir()
    client = _client()

    default = client.get("/api/fs/list", params={"path": str(tmp_path)}).json()
    default_names = [e["name"] for e in default["entries"]]
    assert ".hidden" not in default_names
    assert "visible" in default_names

    shown = client.get(
        "/api/fs/list", params={"path": str(tmp_path), "show_hidden": 1}
    ).json()
    shown_names = [e["name"] for e in shown["entries"]]
    assert ".hidden" in shown_names
    assert "visible" in shown_names


# ---------------------------------------------------------------------------
# A8 — reachable with NO active vault (proves the _VAULTLESS_ALLOWED wiring)
# ---------------------------------------------------------------------------
def test_a8_reachable_without_active_vault(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    # create_app(None) serves no vault: _guard_vault 409s every vault-dependent
    # route. fs/list is whitelisted, so it must answer 200 — not 409/410.
    resp = _client().get("/api/fs/list", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    assert resp.status_code not in (409, 410)
    names = [e["name"] for e in resp.json()["entries"]]
    assert "sub" in names
