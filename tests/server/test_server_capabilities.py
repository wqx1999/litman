"""``GET /api/capabilities`` — the link advisory's only channel to a GUI user.

A vault on a drive that cannot hold folder links (FAT32 / exFAT, network
shares) gets none of them: ``views/`` stays empty and the ``litman_reflib`` /
``litman_code`` shortcuts never appear in project folders. The CLI says so on
stderr — but the desktop shortcut launches the console-less ``litw`` entry
point, so a GUI-only user is told by nobody. This endpoint is what the SPA
reads at boot to raise that notice; it reports which mechanism works
(``symlink`` on POSIX, ``junction`` on Windows) and the SPA stays silent for
both — only ``none`` raises the notice.

It exists separately from ``GET /api/health`` on purpose: health is Tier-2 (it
reads every ``metadata.yaml``) and is fetched only when the user opens the
panel, which is far too late and far too expensive for a boot-time banner.

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.library import create_vault
from litman.core.portable_link import reset_link_probe_cache
from litman.server import create_app


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> Any:
    reset_link_probe_cache()
    yield
    reset_link_probe_cache()


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def test_reports_the_working_mechanism_on_a_normal_filesystem(
    tmp_path: Path,
) -> None:
    vault = create_vault(tmp_path)
    r = _client(vault).get("/api/capabilities")

    assert r.status_code == 200
    body = r.json()
    assert body["links"] == "symlink"  # POSIX test host; Windows would say junction
    assert body["platform"] == sys.platform


def test_reports_none_when_the_drive_refuses_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = create_vault(tmp_path)

    def boom(self: Path, target: Any, target_is_directory: bool = False) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "symlink_to", boom)
    reset_link_probe_cache()

    body = _client(vault).get("/api/capabilities").json()
    assert body["links"] == "none"


def test_probe_runs_once_for_the_life_of_the_server(tmp_path: Path) -> None:
    """The SPA may boot many times against one long-lived server.

    Probing per request would write (and remove) a scratch entry in the vault
    on every page load — cheap, but pointless churn on the user's library
    directory.
    """
    vault = create_vault(tmp_path)
    calls: list[Path] = []
    real = Path.symlink_to

    def counting(self: Path, target: Any, target_is_directory: bool = False) -> None:
        calls.append(self)
        return real(self, target, target_is_directory)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "symlink_to", counting)
        client = _client(vault)
        for _ in range(4):
            assert client.get("/api/capabilities").json()["links"] == "symlink"

    assert len(calls) == 1


def test_probe_leaves_no_litter_in_the_vault(tmp_path: Path) -> None:
    """A probe that leaked would seed exactly the dangling links we hunt."""
    vault = create_vault(tmp_path)
    before = sorted(p.name for p in vault.iterdir())

    _client(vault).get("/api/capabilities")

    assert sorted(p.name for p in vault.iterdir()) == before
