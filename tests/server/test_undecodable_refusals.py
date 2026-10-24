"""The web UI's vault and project routes, handed a path that resolves to a name
that is not UTF-8 text.

The path in the request is plain text, but a symlink can lead to such a name.
The refusal comes first, so the messages that show the path (which no JSON
response can carry) are never reached: a 400 that says why, never a 500
(``core/undecodable.py``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from litman.core.library import create_vault
from litman.core.vault_registry import registry_path
from litman.server import create_app
from tests.server._client import TestClient

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or sys.getfilesystemencoding() != "utf-8",
    reason="needs Linux (APFS refuses a file name that is not UTF-8; a Windows "
    "name is UTF-16) and a UTF-8 file-name encoding, under which a bad byte "
    "becomes a lone surrogate",
)

_REFUSED = (
    "Folder name “target_caf\ufffd” is not UTF-8 text, so litman cannot record "
    "its path. Rename the folder, then retry."
)


def _link_to_undecodable(tmp_path: Path, *, make_target: bool) -> Path:
    """``tmp_path/link`` → ``tmp_path/target_caf\\xe9`` (created or dangling)."""
    target = os.fsencode(tmp_path) + b"/target_caf\xe9"
    if make_target:
        os.mkdir(target)
    link = tmp_path / "link"
    os.symlink(target, os.fsencode(link))
    return link


@pytest.mark.parametrize("make_target", [True, False], ids=["folder", "dangling"])
def test_vault_add_and_set_path_refuse_it_with_a_400(
    tmp_path: Path, make_target: bool
) -> None:
    vault = create_vault(tmp_path, name="lib")
    client = TestClient(create_app(vault))
    assert client.post("/api/vaults", json={"name": "lib", "path": str(vault)}).status_code == 200
    registry_before = registry_path().read_bytes()
    link = _link_to_undecodable(tmp_path, make_target=make_target)

    added = client.post("/api/vaults", json={"name": "other", "path": str(link)})
    moved = client.put("/api/vaults/lib/path", json={"path": str(link)})

    assert (added.status_code, added.json()["detail"]) == (400, _REFUSED)
    assert (moved.status_code, moved.json()["detail"]) == (400, _REFUSED)
    assert registry_path().read_bytes() == registry_before


def test_vault_create_refuses_it_with_a_400_and_creates_nothing(tmp_path: Path) -> None:
    link = _link_to_undecodable(tmp_path, make_target=True)

    resp = TestClient(create_app(None)).post("/api/vaults/create", json={"parent_dir": str(link)})

    assert (resp.status_code, resp.json()["detail"]) == (400, _REFUSED)
    assert os.listdir(os.fsencode(link)) == []
    assert not registry_path().exists()


@pytest.mark.parametrize("make_target", [True, False], ids=["folder", "dangling"])
def test_project_add_and_set_path_refuse_it_with_a_400(
    tmp_path: Path, make_target: bool
) -> None:
    vault = create_vault(tmp_path)
    good = tmp_path / "proj"
    good.mkdir()
    client = TestClient(create_app(vault))
    assert client.post("/api/projects", json={"name": "p", "path": str(good)}).status_code == 200
    config_before = (vault / "lit-config.yaml").read_bytes()
    link = _link_to_undecodable(tmp_path, make_target=make_target)

    added = client.post("/api/projects", json={"name": "q", "path": str(link)})
    moved = client.put("/api/projects/p/path", json={"path": str(link)})

    assert (added.status_code, added.json()["detail"]) == (400, _REFUSED)
    assert (moved.status_code, moved.json()["detail"]) == (400, _REFUSED)
    assert (vault / "lit-config.yaml").read_bytes() == config_before
