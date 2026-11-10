"""A second library made with the dialog's defaults does not collide with the first.

The New-library dialogs offer ``literature_vault``. Every library made that
way registers under that name, so the second one used to be refused as
"already registered". The server now offers the first free name
(``GET /api/vaults``: ``suggestedName``) and uses it when the dialog sends
none, as ``lit setup`` does. A name the user typed is never changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from litman.core.library import DEFAULT_VAULT_NAME
from litman.core.vault_registry import load_registry, suggest_vault_name
from litman.server import create_app
from tests.server._client import TestClient


def _parent(tmp_path: Path, name: str) -> Path:
    parent = tmp_path / name
    parent.mkdir()
    return parent


def test_a_second_library_with_the_default_name_is_made_under_a_free_one(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(None))
    first = client.post("/api/vaults/create", json={"parent_dir": str(_parent(tmp_path, "one"))})
    assert first.status_code == 200, first.text
    assert first.json()["name"] == DEFAULT_VAULT_NAME
    assert client.get("/api/vaults").json()["suggestedName"] == f"{DEFAULT_VAULT_NAME}_2"

    second = client.post("/api/vaults/create", json={"parent_dir": str(_parent(tmp_path, "two"))})

    assert second.status_code == 200, second.text
    assert second.json()["name"] == f"{DEFAULT_VAULT_NAME}_2"
    assert Path(second.json()["path"]) == (tmp_path / "two" / f"{DEFAULT_VAULT_NAME}_2").resolve()
    assert [v.name for v in load_registry().vaults] == [
        DEFAULT_VAULT_NAME, f"{DEFAULT_VAULT_NAME}_2",
    ]


def test_a_name_the_user_typed_is_never_changed(tmp_path: Path) -> None:
    client = TestClient(create_app(None))
    client.post("/api/vaults/create", json={"parent_dir": str(_parent(tmp_path, "one"))})

    resp = client.post(
        "/api/vaults/create",
        json={"parent_dir": str(_parent(tmp_path, "two")), "name": DEFAULT_VAULT_NAME},
    )

    assert resp.status_code == 400
    assert (resp.json()["code"], resp.json()["params"]) == (
        "name-taken", {"name": DEFAULT_VAULT_NAME},
    )
    assert not (tmp_path / "two" / DEFAULT_VAULT_NAME).exists()


def test_the_suggestion_skips_names_taken_in_any_case_and_folders_in_the_way(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(None))
    first = client.post(
        "/api/vaults/create",
        json={"parent_dir": str(_parent(tmp_path, "one")), "name": "Literature_Vault"},
    )
    assert first.status_code == 200, first.text
    registry = load_registry()

    # Same name up to case: refused on Windows and macOS, so not offered.
    assert suggest_vault_name(registry, DEFAULT_VAULT_NAME) == f"{DEFAULT_VAULT_NAME}_2"
    # A non-empty folder of that name where the library would go: skipped too.
    parent = _parent(tmp_path, "two")
    (parent / f"{DEFAULT_VAULT_NAME}_2").mkdir()
    (parent / f"{DEFAULT_VAULT_NAME}_2" / "mine.txt").write_text("", encoding="utf-8")
    assert suggest_vault_name(registry, DEFAULT_VAULT_NAME, parent) == f"{DEFAULT_VAULT_NAME}_3"
    # The reverse: with nothing in the way, the default itself.
    assert suggest_vault_name(load_registry().model_copy(update={"vaults": []}),
                              DEFAULT_VAULT_NAME) == DEFAULT_VAULT_NAME
