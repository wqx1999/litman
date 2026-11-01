"""The GUI's delete shares ``execute_rm``: a folder that stayed is not a success."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

from litman.core.library import create_vault
from litman.core.trash import list_trash
from litman.server import create_app
from tests.commands.test_link import _make_paper
from tests.server._client import TestClient

PAPER = "2024_Foo_Bar"


def test_a_refused_delete_is_an_error_and_deleting_again_finishes_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    vault = create_vault(tmp_path)
    _make_paper(vault, PAPER)
    client = TestClient(create_app(vault))

    def rename(_src: Any, _dst: Any) -> None:
        raise PermissionError(errno.EACCES, "Access is denied")

    with monkeypatch.context() as held:
        held.setattr(os, "rename", rename)
        refused = client.delete(f"/api/paper/{PAPER}")

    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        f"{PAPER} stayed in papers/, not the trash (Access is denied). "
        "Close any program using it and delete it again."
    )
    assert (vault / "papers" / PAPER / "metadata.yaml").is_file()
    assert list_trash(vault) == []

    again = client.delete(f"/api/paper/{PAPER}")

    assert again.status_code == 200
    assert again.json()["ok"] is True
    assert not (vault / "papers" / PAPER).exists()
    assert [entry.paper_id for entry in list_trash(vault)] == [PAPER]
