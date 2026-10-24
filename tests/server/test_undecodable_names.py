"""Names that are not UTF-8 text, reaching the web UI.

Such a name (a POSIX file name with a byte no encoding was declared for)
cannot travel in a JSON response. The picker lists the folder without a path,
the health panel and search show the name with U+FFFD, and the live-refresh
token hashes its bytes — none of them answers 500 (``core/undecodable.py``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.views import write_index
from litman.server import create_app
from tests.server._client import TestClient

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or sys.getfilesystemencoding() != "utf-8",
    reason="needs Linux (APFS refuses a file name that is not UTF-8; a Windows "
    "name is UTF-16) and a UTF-8 file-name encoding, under which a bad byte "
    "becomes a lone surrogate",
)

PAPER = "2019_Yang_Concepts"
_META = (
    "id: {pid}\ntitle: Concepts\nauthors:\n  - Doe, Jane\nyear: 2019\n"
    "doi: 10.1021/acs.jcim.9b00237\nstatus: inbox\n"
    "created-at: '2026-01-01T00:00:00+00:00'\nupdated-at: '2026-01-01T00:00:00+00:00'\n"
)


def _mkdir(parent: Path, raw_name: bytes) -> Path:
    raw = os.fsencode(parent) + b"/" + raw_name
    os.mkdir(raw)
    return Path(os.fsdecode(raw))


def _vault(tmp_path: Path) -> Path:
    vault = create_vault(tmp_path)
    (vault / "papers" / PAPER).mkdir(parents=True)
    (vault / "papers" / PAPER / "metadata.yaml").write_text(
        _META.format(pid=PAPER), encoding="utf-8"
    )
    write_index(vault, list_papers(vault))
    return vault


def test_the_picker_lists_a_folder_whose_name_is_not_utf8_without_a_path(
    tmp_path: Path,
) -> None:
    browse = tmp_path / "browse"
    browse.mkdir()
    (browse / "okdir").mkdir()
    _mkdir(browse, b"caf\xe9_dir")

    resp = TestClient(create_app(None)).get("/api/fs/list", params={"path": str(browse)})

    assert resp.status_code == 200, resp.text
    first, second = resp.json()["entries"]
    assert first == {
        "name": "caf\ufffd_dir",
        "path": None,
        "is_vault": False,
        "undecodable": True,
    }
    assert second["name"] == "okdir" and "undecodable" not in second
    assert Path(second["path"]).resolve() == (browse / "okdir").resolve()


def test_a_folder_that_resolves_to_such_a_name_is_refused_not_a_500(
    tmp_path: Path,
) -> None:
    target = _mkdir(tmp_path, b"target_caf\xe9")
    link = tmp_path / "link"
    os.symlink(os.fsencode(target), os.fsencode(link))

    resp = TestClient(create_app(None)).get("/api/fs/list", params={"path": str(link)})

    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Folder name “target_caf\ufffd” is not UTF-8 text, so litman cannot record "
        "its path. Rename the folder, then retry."
    )


def test_health_lists_an_si_stray_whose_name_is_not_utf8(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    si = vault / "papers" / PAPER / "supplementary"
    si.mkdir()
    with open(os.fsencode(si) + b"/ci9b00237_si_003_caf\xe9.xlsx", "wb") as fh:
        fh.write(b"PK\x03\x04 sheet")

    resp = TestClient(create_app(vault)).get("/api/health")

    assert resp.status_code == 200, resp.text
    strays = [i["message"] for i in resp.json() if i["category"] == "supplementary_unrecorded"]
    assert strays == [
        f"papers/{PAPER}/supplementary/ci9b00237_si_003_caf\ufffd.xlsx has no record "
        "in metadata.yaml"
    ]


def test_health_search_and_live_refresh_survive_a_paper_folder_whose_name_is_not_utf8(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    folder = _mkdir(vault / "papers", b"2020_Caf\xe9_Paper")
    (folder / "metadata.yaml").write_text(_META.format(pid="2020_Caf_Paper"), encoding="utf-8")
    (folder / "notes.md").write_text("a probe line\n", encoding="utf-8")
    client = TestClient(create_app(vault))

    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    assert any("2020_Caf\ufffd_Paper" in i["message"] for i in health.json())

    search = client.get("/api/search", params={"q": "probe"})
    assert search.status_code == 200, search.text
    assert [h["id"] for h in search.json()["hits"]] == ["2020_Caf\ufffd_Paper"]

    first = client.get("/api/vault-version")
    assert first.status_code == 200, first.text
    with open(folder / "notes.md", "a", encoding="utf-8") as fh:
        fh.write("more\n")
    second = client.get("/api/vault-version")
    assert second.json()["version"] != first.json()["version"]  # the folder still counts
