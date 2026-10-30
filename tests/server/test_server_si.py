"""SI window endpoints (task-supplementary-info M2): ping, requests, dropped
files, Done / Cancel, and serving / removing a recorded SI file — or opening
it with this machine's default app.

Every write is checked on disk: TRUTH (``papers/<id>/supplementary/`` +
``metadata.yaml``) and the upload stash. Patches go on module objects
(``routes_si._disk_free`` …), never dotted strings: tests/commands/test_gui.py
purges ``litman.server*`` from ``sys.modules`` mid-suite.
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import os
import shutil
import stat
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")

from starlette import requests as starlette_requests

from litman.core import supplementary as si_mod
from litman.core import viewer as viewer_mod
from litman.core.config import CONFIG_FILENAME
from litman.core.document import find_paper, list_papers
from litman.core.gui_registry import host_id
from litman.core.library import create_vault
from litman.core.locking import unlock_truth_file
from litman.core.portable_link import make_portable_link
from litman.core.supplementary import SiItem, add_supplementary
from litman.core.views import write_index
from litman.server import create_app, routes_si
from tests.conftest import _build_text_pdf
from tests.server._client import GUI_URL, TestClient

A_ID = "2019_Yang_Concepts"
A_DOI = "10.1021/acs.jcim.9b00237"
A_TITLE = "Concepts of Artificial Intelligence for Computer-Assisted Drug Discovery"
B_ID = "2023_Zhong_Peptides"
B_DOI = "10.1038/s41467-023-38851-5"
B_TITLE = "Deep generative design of cyclic peptides binding protein targets"
C_ID = "2020_Lee_Preprint"  # an arXiv id, no DOI
D_ID = "2018_Kim_Notes"  # neither
FOREIGN = "http://evil.example"
UPLOAD_DIR = ".litman-upload"

SHEET = b"PK\x03\x04 sheet one"
OTHER_SHEET = b"PK\x03\x04 sheet two"


def _write_paper(
    vault: Path,
    paper_id: str,
    *,
    title: str,
    doi: str | None = None,
    arxiv: str | None = None,
    page1: list[str] | None = None,
) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines = [
        f"id: {paper_id}",
        f"title: {title}",
        "authors:\n  - Doe, Jane",
        "year: 2020",
        f"doi: {doi or ''}",
        f"arxiv-id: {arxiv or ''}",
        "status: inbox",
        "created-at: '2026-01-01T00:00:00+00:00'",
        "updated-at: '2026-01-01T00:00:00+00:00'",
    ]
    (paper_dir / "metadata.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if page1 is not None:
        (paper_dir / "paper.pdf").write_bytes(_build_text_pdf([page1]))


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = create_vault(tmp_path)
    _write_paper(
        v, A_ID, title=A_TITLE, doi=A_DOI,
        page1=[A_TITLE, "Jane Doe", "Abstract: machine learning reshapes discovery."],
    )
    _write_paper(
        v, B_ID, title=B_TITLE, doi=B_DOI,
        page1=[B_TITLE, "We design macrocycles with a diffusion model."],
    )
    _write_paper(v, C_ID, title="A preprint without a DOI", arxiv="2101.00001")
    _write_paper(v, D_ID, title="")
    write_index(v, list_papers(v))
    return v


@pytest.fixture
def app(vault: Path) -> Any:
    return create_app(vault)


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_real_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may start an app on this machine: the opener under the
    open route fails the test unless the test puts its own in place."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a test reached a real opener: {args!r}")

    monkeypatch.setattr(viewer_mod, "_spawn", refuse)
    monkeypatch.setattr(viewer_mod, "_startfile", refuse)


def _si_pdf(title: str) -> bytes:
    return _build_text_pdf(
        [
            ["Supporting Information", title, "Table S1 lists every hyperparameter tuned"],
            ["Table S2 dataset statistics and split sizes"],
        ]
    )


def _open(
    client: TestClient, paper: str = A_ID, source: str = "page", **extra: Any
) -> dict[str, Any]:
    response = client.post(
        "/api/si/requests", json={"paperId": paper, "source": source, **extra}
    )
    assert response.status_code == 200, response.text
    request: dict[str, Any] = response.json()["request"]
    return request


def _drop(
    client: TestClient,
    rid: str,
    name: str,
    body: bytes | Iterable[bytes],
    **headers: str,
) -> Any:
    return client.post(
        f"/api/si/requests/{rid}/files",
        content=body,
        headers={"X-Litman-Filename": quote(name), **headers},
    )


def _row(client: TestClient, rid: str, name: str, body: bytes) -> dict[str, Any]:
    response = _drop(client, rid, name, body)
    assert response.status_code == 200, response.text
    row: dict[str, Any] = response.json()["row"]
    return row


def _stashes(vault: Path) -> list[str]:
    folder = vault / UPLOAD_DIR
    return sorted(os.listdir(folder)) if folder.is_dir() else []


def _meta_bytes(vault: Path, paper: str = A_ID) -> bytes:
    return (vault / "papers" / paper / "metadata.yaml").read_bytes()


def _records(vault: Path, paper: str = A_ID) -> list[dict[str, Any]]:
    return find_paper(vault, paper).get("supplementary") or []


def _set_doi(vault: Path, paper: str, doi: str) -> None:
    meta_path = vault / "papers" / paper / "metadata.yaml"
    text = meta_path.read_text(encoding="utf-8")
    old = next(line for line in text.splitlines() if line.startswith("doi:"))
    meta_path.write_text(text.replace(old, f"doi: {doi}"), encoding="utf-8")


def _second_vault(tmp_path: Path) -> Path:
    parent = tmp_path / "second"
    parent.mkdir()
    other = create_vault(parent)
    _write_paper(other, A_ID, title=A_TITLE, doi=A_DOI)
    write_index(other, list_papers(other))
    return other


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------


def test_ping_says_who_answers(client: TestClient, vault: Path) -> None:
    payload = client.get("/api/si/ping").json()
    assert payload["app"] == "litman"
    assert payload["pid"] == os.getpid()
    assert payload["host"] == host_id()
    assert payload["vault"] == str(vault.resolve())
    assert payload["pages"] == 0
    assert isinstance(payload["version"], str)


def test_ping_counts_open_pages(client: TestClient) -> None:
    with client.websocket_connect("/api/presence"):
        assert client.get("/api/si/ping").json()["pages"] == 1
    assert client.get("/api/si/ping").json()["pages"] == 0


def test_ping_answers_without_a_library() -> None:
    client = TestClient(create_app(None))
    response = client.get("/api/si/ping")
    assert response.status_code == 200
    assert response.json()["vault"] is None
    # …where the rest of the SI API is closed.
    assert client.get(f"/api/si/requests/{'a' * 32}").status_code == 409


def test_ping_runs_off_the_event_loop() -> None:
    """It stats and resolves the library path: on a stalled network mount an
    async handler would freeze every other request with it."""
    assert not inspect.iscoroutinefunction(routes_si.get_si_ping)


def test_ping_answers_when_the_library_is_gone(client: TestClient, vault: Path) -> None:
    (vault / CONFIG_FILENAME).unlink()
    response = client.get("/api/si/ping")
    assert response.status_code == 200
    assert response.json()["vault"] is None
    assert client.get(f"/api/si/requests/{'a' * 32}").status_code == 410


# ---------------------------------------------------------------------------
# Opening a request
# ---------------------------------------------------------------------------


def test_open_a_request(client: TestClient, vault: Path) -> None:
    request = _open(client, source="cli", vault=str(vault))
    assert len(request["id"]) == 32
    assert request["paperId"] == A_ID
    assert request["title"] == A_TITLE
    assert request["link"] == "https://doi.org/10.1021/acs.jcim.9b00237"
    assert request["status"] == "pending"
    assert request["vaultServed"] is True
    assert (request["rows"], request["added"], request["warnings"]) == ([], [], [])


def test_the_cli_rerun_joins_the_same_request(client: TestClient) -> None:
    """The agent reruns `lit add --si` after exit 11: same request, and the
    page keeps the one window it already shows."""
    first = _open(client, source="cli")
    shown = client.get("/api/vault-version").json()["siRequest"]
    again = _open(client, source="cli")
    assert again["id"] == first["id"]
    assert client.get("/api/vault-version").json()["siRequest"]["id"] == shown["id"]


@pytest.mark.parametrize(
    ("paper", "title", "link"),
    [
        (A_ID, A_TITLE, "https://doi.org/10.1021/acs.jcim.9b00237"),
        (C_ID, "A preprint without a DOI", "https://arxiv.org/abs/2101.00001"),
        (D_ID, D_ID, None),  # no title on record: the id stands in
    ],
)
def test_the_page_learns_of_a_request_on_its_poll(
    client: TestClient, paper: str, title: str, link: str | None
) -> None:
    before = client.get("/api/vault-version").json()
    assert before["siRequest"] is None
    request = _open(client, paper=paper, source="cli")
    after = client.get("/api/vault-version").json()
    assert after["siRequest"] == {
        "id": request["id"], "paperId": paper, "title": title, "link": link,
    }
    assert after["version"] == before["version"]  # not part of the token


def test_another_library_shown_hides_the_request(
    client: TestClient, app: Any, vault: Path, tmp_path: Path
) -> None:
    request = _open(client, source="cli")
    other = _second_vault(tmp_path)
    app.state.vault = other
    assert client.get("/api/vault-version").json()["siRequest"] is None
    status = client.get(f"/api/si/requests/{request['id']}").json()["request"]
    assert status["vaultServed"] is False
    app.state.vault = vault
    assert client.get("/api/vault-version").json()["siRequest"]["id"] == request["id"]


@pytest.mark.parametrize(
    "body",
    [
        {"source": "cli"},
        {"paperId": A_ID},
        {"paperId": A_ID, "source": "agent"},
        {"paperId": 7, "source": "cli"},
        {"paperId": A_ID, "source": "cli", "vault": 3},
        {"paperId": A_ID, "source": "cli", "force": "yes"},
        {"paperId": A_ID, "source": "cli", "force": 1},
        {"paperId": A_ID, "source": "cli", "force": None},
        ["not", "an", "object"],
    ],
)
def test_open_refuses_a_bad_body(client: TestClient, body: Any) -> None:
    assert client.post("/api/si/requests", json=body).status_code == 400


def test_open_refuses_a_body_that_is_not_json(client: TestClient) -> None:
    response = client.post("/api/si/requests", content=b"{nope")
    assert response.status_code == 400


@pytest.mark.parametrize("paper", ["2000_Nobody_Here", "../etc", "a/b"])
def test_open_an_unknown_paper(client: TestClient, paper: str) -> None:
    response = client.post("/api/si/requests", json={"paperId": paper, "source": "cli"})
    assert response.status_code == 404


def test_open_for_another_library_is_a_conflict(
    client: TestClient, tmp_path: Path
) -> None:
    other = _second_vault(tmp_path)
    response = client.post(
        "/api/si/requests", json={"paperId": A_ID, "source": "cli", "vault": str(other)}
    )
    assert response.status_code == 409
    assert "another library" in response.json()["detail"]


def test_open_for_the_same_library_by_another_path(
    client: TestClient, vault: Path, tmp_path: Path
) -> None:
    alias = tmp_path / "alias-of-the-library"
    if not make_portable_link(alias, vault):
        pytest.skip("this filesystem cannot hold folder links")
    assert _open(client, source="cli", vault=str(alias))["vaultServed"] is True


def test_open_refuses_a_paper_whose_si_cannot_be_written(
    client: TestClient, vault: Path
) -> None:
    (vault / "papers" / A_ID / "supplementary").write_text("a file", encoding="utf-8")
    response = client.post("/api/si/requests", json={"paperId": A_ID, "source": "cli"})
    assert response.status_code == 409
    assert "is a file, not a folder" in response.json()["detail"]


def test_status_and_collect(client: TestClient) -> None:
    rid = _open(client, source="cli")["id"]
    assert client.get(f"/api/si/requests/{rid}?collect=1").json()["request"]["status"] == "pending"
    client.post(f"/api/si/requests/{rid}/cancel")
    done = client.get(f"/api/si/requests/{rid}?collect=1").json()["request"]
    assert done["status"] == "cancelled"
    # Collected: the next CLI run opens a new window instead of a stale cancel.
    assert _open(client, source="cli")["id"] != rid


def test_a_result_goes_back_to_a_run_asking_the_same_way(client: TestClient) -> None:
    """No ``force`` in the body is a plain run, the same as ``force: false``."""
    rid = _open(client, source="cli")["id"]
    client.post(f"/api/si/requests/{rid}/cancel")
    handed = _open(client, source="cli", force=False)
    assert handed["id"] == rid and handed["status"] == "cancelled"


def test_a_force_run_after_a_plain_one_opens_a_fresh_window(client: TestClient) -> None:
    rid = _open(client, source="cli")["id"]
    client.post(f"/api/si/requests/{rid}/cancel")
    fresh = _open(client, source="cli", force=True)
    assert fresh["id"] != rid and fresh["status"] == "pending"


def test_a_page_request_joined_by_a_force_run_goes_back_to_one(client: TestClient) -> None:
    rid = _open(client, source="page", force=True)["id"]
    assert _open(client, source="cli", force=True)["id"] == rid
    client.post(f"/api/si/requests/{rid}/cancel")
    handed = _open(client, source="cli", force=True)
    assert handed["id"] == rid and handed["status"] == "cancelled"


def _done(client: TestClient, rid: str, *drops: tuple[str, bytes]) -> list[str]:
    for name, body in drops:
        _row(client, rid, name, body)
    response = client.post(f"/api/si/requests/{rid}/confirm", json={})
    assert response.status_code == 200, response.text
    return [entry["file"] for entry in response.json()["request"]["added"]]


def test_an_empty_drop_is_refused_as_empty(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]

    one = _row(client, rid, "test.dat", b"")
    two = _row(client, rid, "New Microsoft Word Document.docx", b"")
    sheet = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)

    assert [(r["verdict"], r["reason"]) for r in (one, two)] == [
        ("rejected", "empty file"),
        ("rejected", "empty file"),
    ]
    assert sheet["verdict"] == "ok"
    assert _stashes(vault) == [f"si-{rid}-{sheet['id']}.xlsx"]


def test_done_never_adds_an_empty_drop_even_ticked(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]
    empty = _row(client, rid, "test.dat", b"")

    ticked = client.post(f"/api/si/requests/{rid}/confirm", json={"addAnyway": [empty["id"]]})
    assert (ticked.status_code, ticked.json()["detail"]) == (
        400,
        "test.dat was refused (empty file). Untick it, then press Done.",
    )
    assert _records(vault) == []

    added = _done(client, rid, ("ci9b00237_si_001.xlsx", SHEET))
    assert added == ["si-01.xlsx"]
    assert [r["original"] for r in _records(vault)] == ["ci9b00237_si_001.xlsx"]


def test_an_added_result_whose_files_were_all_removed_is_not_handed_over(
    client: TestClient,
) -> None:
    rid = _open(client, source="cli")["id"]
    for name in _done(client, rid, ("ci9b00237_si_001.xlsx", SHEET)):
        assert client.delete(f"/api/paper/{A_ID}/si/{name}").status_code == 200

    fresh = _open(client, source="cli")

    assert fresh["id"] != rid and fresh["status"] == "pending"


def test_an_added_result_with_a_file_still_recorded_is_handed_over(
    client: TestClient,
) -> None:
    rid = _open(client, source="cli")["id"]
    first, _second = _done(
        client,
        rid,
        ("ci9b00237_si_001.xlsx", SHEET),
        ("ci9b00237_si_002.xlsx", OTHER_SHEET),
    )
    assert client.delete(f"/api/paper/{A_ID}/si/{first}").status_code == 200

    handed = _open(client, source="cli")

    assert handed["id"] == rid and handed["status"] == "added"


def _page_adds(client: TestClient, name: str, body: bytes) -> list[str]:
    """The page's own Add SI, Done included."""
    return _done(client, _open(client, source="page")["id"], (name, body))


def test_a_result_whose_number_now_holds_another_file_is_not_handed_over(
    client: TestClient,
) -> None:
    """A deleted file's number is given again: the result names si-01.xlsx,
    the library has an si-01.xlsx, but it is another file."""
    rid = _open(client, source="cli", force=True)["id"]
    (first,) = _done(client, rid, ("ci9b00237_si_001.xlsx", SHEET))
    assert client.delete(f"/api/paper/{A_ID}/si/{first}").status_code == 200
    assert _page_adds(client, "ci9b00237_si_002.xlsx", OTHER_SHEET) == [first]

    fresh = _open(client, source="cli", force=True)

    assert fresh["id"] != rid and fresh["status"] == "pending"


def test_a_result_re_added_unchanged_is_handed_over(client: TestClient) -> None:
    rid = _open(client, source="cli", force=True)["id"]
    (first,) = _done(client, rid, ("ci9b00237_si_001.xlsx", SHEET))
    assert client.delete(f"/api/paper/{A_ID}/si/{first}").status_code == 200
    assert _page_adds(client, "ci9b00237_si_001.xlsx", SHEET) == [first]

    handed = _open(client, source="cli", force=True)

    assert handed["id"] == rid and handed["status"] == "added"


@pytest.mark.parametrize("rid", ["f" * 32, "not-an-id", "F" * 32, "a" * 31])
def test_status_of_an_unknown_request(client: TestClient, rid: str) -> None:
    assert client.get(f"/api/si/requests/{rid}").status_code == 404


# ---------------------------------------------------------------------------
# Dropped files
# ---------------------------------------------------------------------------


def test_each_drop_is_verified_into_a_row(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]

    by_name = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    by_title = _row(client, rid, "media-1.pdf", _si_pdf(A_TITLE))
    unsure = _row(client, rid, "Table_S2.xlsx", OTHER_SHEET)
    others = _row(client, rid, "41467_2023_38851_MOESM1_ESM.pdf", _si_pdf(B_TITLE))
    twin = _row(client, rid, "copy of it.xlsx", SHEET)

    assert (by_name["verdict"], by_name["check"]) == ("ok", "filename")
    assert (by_title["verdict"], by_title["check"]) == ("ok", "title")
    assert (unsure["verdict"], unsure["check"]) == ("unverified", None)
    assert (others["verdict"], others["otherId"]) == ("rejected", B_ID)
    assert others["reason"] == f"looks like the SI of {B_ID}"
    assert (twin["verdict"], twin["reason"]) == ("rejected", "same file as ci9b00237_si_001.xlsx")
    assert by_name["bytes"] == len(SHEET)
    assert by_name["name"] == "ci9b00237_si_001.xlsx"

    # Kept: what can still go in. A refused file's stash goes at once.
    assert _stashes(vault) == sorted(
        [
            f"si-{rid}-{by_name['id']}.xlsx",
            f"si-{rid}-{by_title['id']}.pdf",
            f"si-{rid}-{unsure['id']}.xlsx",
        ]
    )
    stash = vault / UPLOAD_DIR / f"si-{rid}-{by_name['id']}.xlsx"
    assert stash.read_bytes() == SHEET
    status = client.get(f"/api/si/requests/{rid}").json()["request"]
    assert [row["id"] for row in status["rows"]] == [
        by_name["id"], by_title["id"], unsure["id"], others["id"], twin["id"],
    ]
    assert not (vault / "papers" / A_ID / "supplementary").exists()


def test_a_chunked_upload_arrives_byte_for_byte(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]
    pieces = [b"PK\x03\x04", os.urandom(70_000), b"tail" * 1000]
    response = _drop(client, rid, "ci9b00237_si_004.zip", iter(pieces))
    assert response.status_code == 200, response.text
    row = response.json()["row"]
    assert row["bytes"] == sum(map(len, pieces))
    stash = vault / UPLOAD_DIR / f"si-{rid}-{row['id']}.zip"
    assert stash.read_bytes() == b"".join(pieces)


def test_the_upload_is_streamed_never_read_whole(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No size limit (D13) is only safe if no copy of the body sits in memory:
    with Request.body unusable, the upload still lands."""
    rid = _open(client)["id"]

    async def no_body(self: Any) -> bytes:
        raise AssertionError("the upload must be streamed, not read whole")

    monkeypatch.setattr(starlette_requests.Request, "body", no_body)
    monkeypatch.setattr(routes_si, "_FLUSH_BYTES", 1000)
    payload = os.urandom(10_000)
    response = _drop(client, rid, "ci9b00237_si_005.bin", iter([payload[:4000], payload[4000:]]))
    assert response.status_code == 200, response.text
    row = response.json()["row"]
    assert (vault / UPLOAD_DIR / f"si-{rid}-{row['id']}.bin").read_bytes() == payload


def test_a_name_in_any_script(client: TestClient) -> None:
    rid = _open(client)["id"]
    row = _row(client, rid, "ci9b00237_补充材料_Übersicht.xlsx", SHEET)
    assert row["name"] == "ci9b00237_补充材料_Übersicht.xlsx"
    assert row["verdict"] == "ok"


def test_only_the_last_path_segment_is_the_name(client: TestClient) -> None:
    rid = _open(client)["id"]
    row = _row(client, rid, "C:\\Users\\me\\Downloads/ci9b00237_si_001.xlsx  ", SHEET)
    assert row["name"] == "ci9b00237_si_001.xlsx"


@pytest.mark.parametrize(
    "header",
    [None, "", "%20%20", "..", "a%07b.xlsx", "x" * 256, "%ff%fe.xlsx", "dir/"],
    ids=["missing", "empty", "blank", "dotdot", "control", "too-long", "not-utf8", "no-name"],
)
def test_an_unusable_file_name(client: TestClient, vault: Path, header: str | None) -> None:
    rid = _open(client)["id"]
    headers = {} if header is None else {"X-Litman-Filename": header}
    response = client.post(f"/api/si/requests/{rid}/files", content=SHEET, headers=headers)
    assert response.status_code == 400
    assert response.json()["detail"]
    assert _stashes(vault) == []


@pytest.mark.parametrize(
    "name",
    [
        "ci9b00237_si_001\u202excod.exe",  # right-to-left override (Cf)
        "ci9b00237_si_001\u202a.xlsx",  # left-to-right embedding (Cf)
        "ci9b00237_si_001\u2066.xlsx",  # left-to-right isolate (Cf)
        "ci9b00237_si_001\u2069.xlsx",  # pop directional isolate (Cf)
        "ci9b00237_si_001\u200e.xlsx",  # left-to-right mark (Cf)
        "ci9b00237_si_001\u200f.xlsx",  # right-to-left mark (Cf)
        "ci9b00237_si_001\u061c.xlsx",  # Arabic letter mark (Cf)
        "ci9b00237\u200b_si_001.xlsx",  # zero-width space (Cf)
        "ci9b00237_si_001\ufeff.xlsx",  # byte-order mark (Cf)
        "ci9b00237_si\u00ad_001.xlsx",  # soft hyphen (Cf)
        "ci9b00237\u2028si_001.xlsx",  # line separator (Zl)
        "ci9b00237\u2029si_001.xlsx",  # paragraph separator (Zp)
    ],
    ids=[
        "rlo", "lre", "lri", "pdi", "lrm", "rlm", "alm",
        "zwsp", "bom", "soft-hyphen", "line-sep", "para-sep",
    ],
)
def test_invisible_marks_in_a_name_are_refused(
    client: TestClient, vault: Path, name: str
) -> None:
    """The name is recorded and becomes the download name: a hidden
    right-to-left override would make "…\u202excod.exe" read as "…exe.docx"."""
    rid = _open(client)["id"]
    response = _drop(client, rid, name, SHEET)
    assert response.status_code == 400
    assert _stashes(vault) == []
    assert client.get(f"/api/si/requests/{rid}").json()["request"]["rows"] == []


@pytest.mark.parametrize(
    "name",
    [
        "ci9b00237_si_001_\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645.xlsx",  # Persian, ZWNJ
        "ci9b00237_si_002_\u0915\u094d\u200d\u0937.xlsx",  # Devanagari, ZWJ
        "ci9b00237_si_003_\U0001f469\u200d\U0001f52c.xlsx",  # emoji sequence, ZWJ
    ],
    ids=["zwnj-persian", "zwj-devanagari", "zwj-emoji"],
)
def test_joiners_in_a_name_are_kept(client: TestClient, vault: Path, name: str) -> None:
    """ZWNJ / ZWJ are part of how these names are spelled: accepted, recorded
    and served back exactly as dropped."""
    rid = _open(client)["id"]
    row = _row(client, rid, name, SHEET)
    assert row["name"] == name
    assert row["verdict"] == "ok"

    done = client.post(f"/api/si/requests/{rid}/confirm", json={})
    assert done.status_code == 200, done.text
    (added,) = done.json()["request"]["added"]
    assert added["original"] == name
    assert _records(vault)[0]["original"] == name

    served = client.get(f"/api/paper/{A_ID}/si/{added['file']}")
    assert served.status_code == 200
    assert served.headers["content-disposition"] == (
        "attachment; filename*=utf-8''" + quote(name)
    )


def test_drops_into_a_finished_request_are_refused(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]
    client.post(f"/api/si/requests/{rid}/cancel")
    response = _drop(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    assert response.status_code == 409
    assert response.json()["request"]["status"] == "cancelled"
    assert _stashes(vault) == []
    assert _drop(client, "f" * 32, "x.xlsx", SHEET).status_code == 404


def test_a_drop_for_a_paper_that_is_gone(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]
    shutil.rmtree(vault / "papers" / A_ID)
    assert _drop(client, rid, "ci9b00237_si_001.xlsx", SHEET).status_code == 404
    assert _stashes(vault) == []


def test_no_room_for_the_file(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _open(client)["id"]
    monkeypatch.setattr(routes_si, "_disk_free", lambda path: 2 * len(SHEET) + (64 << 20) - 1)
    response = _drop(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    assert response.status_code == 507
    assert _stashes(vault) == []
    assert client.get(f"/api/si/requests/{rid}").json()["request"]["rows"] == []


def test_no_room_noticed_midway_without_a_length(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunked upload declares no size: free space is checked as it grows,
    and the half-written file is deleted when it runs short."""
    rid = _open(client)["id"]
    free = {"bytes": 10**12}
    monkeypatch.setattr(routes_si, "_disk_free", lambda path: free["bytes"])
    monkeypatch.setattr(routes_si, "_FLUSH_BYTES", 100)
    monkeypatch.setattr(routes_si, "_FREE_CHECK_BYTES", 200)
    real_append = routes_si._append

    def append(handle: Any, data: bytes) -> None:
        real_append(handle, data)
        free["bytes"] = 1000  # the disk fills up after the first piece

    monkeypatch.setattr(routes_si, "_append", append)
    response = _drop(client, rid, "ci9b00237_si_001.bin", iter([b"x" * 150] * 6))
    assert response.status_code == 507
    assert _stashes(vault) == []


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO], ids=["disk-full", "io-error"])
def test_a_write_failing_halfway_leaves_no_half_file(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    rid = _open(client)["id"]
    real_append = routes_si._append
    calls: list[int] = []

    def append(handle: Any, data: bytes) -> None:
        calls.append(len(data))
        real_append(handle, data[: len(data) // 2])
        handle.flush()
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(routes_si, "_append", append)
    response = _drop(client, rid, "ci9b00237_si_001.bin", iter([b"x" * 150] * 4))
    assert response.status_code == (507 if code == errno.ENOSPC else 500)
    assert calls == [600]
    assert _stashes(vault) == []
    assert client.get(f"/api/si/requests/{rid}").json()["request"]["rows"] == []


@pytest.mark.parametrize(
    ("code", "status"),
    [(errno.ENOSPC, 507), (errno.EACCES, 500)],
    ids=["disk-full", "not-writable"],
)
def test_the_stash_cannot_be_created(
    client: TestClient,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    status: int,
) -> None:
    """Making the upload folder or opening the stash fails: an answer in
    words with the right status, never a bare 500, and nothing left behind."""
    rid = _open(client)["id"]

    def cannot_create(stash: Path) -> Any:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(routes_si, "_create_stash", cannot_create)
    response = _drop(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    assert response.status_code == status
    assert response.json()["detail"]
    assert _stashes(vault) == []
    assert client.get(f"/api/si/requests/{rid}").json()["request"]["rows"] == []


def test_a_file_where_the_upload_folder_belongs(client: TestClient, vault: Path) -> None:
    """No seam: the real mkdir meets a file named .litman-upload."""
    rid = _open(client)["id"]
    blocker = vault / UPLOAD_DIR
    blocker.write_bytes(b"not a folder")
    response = _drop(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    assert response.status_code == 500
    assert "Could not save the file" in response.json()["detail"]
    assert blocker.read_bytes() == b"not a folder"


def test_free_space_that_cannot_be_read(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _open(client)["id"]

    def unreadable(path: Path) -> int:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(routes_si, "_disk_free", unreadable)
    response = _drop(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    assert response.status_code == 500
    assert response.json()["detail"]
    assert _stashes(vault) == []


def test_a_dropped_connection_leaves_no_half_file(
    app: Any, client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser tab closes mid-upload: the ASGI server reports a
    disconnect, and the partial stash goes with it."""
    rid = _open(client)["id"]
    monkeypatch.setattr(routes_si, "_FLUSH_BYTES", 100)
    path = f"/api/si/requests/{rid}/files"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"x-litman-filename", b"ci9b00237_si_001.bin"),
            (b"content-length", b"100000"),
        ],
        # TestClient's name rather than an address: no socket stands behind
        # this scope, and the guard takes a real address with none for a
        # client that has hung up.
        "client": ("testclient", 50000),
        "server": ("127.0.0.1", 8765),
    }
    incoming: list[dict[str, Any]] = [
        {"type": "http.request", "body": b"x" * 500, "more_body": True},
        {"type": "http.disconnect"},
    ]
    written: list[int] = []
    real_append = routes_si._append

    def append(handle: Any, data: bytes) -> None:
        written.append(len(data))
        real_append(handle, data)

    monkeypatch.setattr(routes_si, "_append", append)

    async def receive() -> dict[str, Any]:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))

    assert written == [500]  # a partial file did reach the disk…
    assert _stashes(vault) == []  # …and is gone again
    assert client.get(f"/api/si/requests/{rid}").json()["request"]["rows"] == []


def test_removing_a_row_removes_its_stash(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]
    row = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    response = client.delete(f"/api/si/requests/{rid}/files/{row['id']}")
    assert response.status_code == 200
    assert response.json()["removed"] is True
    assert response.json()["request"]["rows"] == []
    assert _stashes(vault) == []
    again = client.delete(f"/api/si/requests/{rid}/files/{row['id']}")
    assert (again.status_code, again.json()["removed"]) == (200, False)
    assert client.delete(f"/api/si/requests/{rid}/files/nope").status_code == 404


def test_rows_change_only_while_pending(client: TestClient) -> None:
    rid = _open(client)["id"]
    row = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    assert client.post(f"/api/si/requests/{rid}/confirm", json={}).status_code == 200
    response = client.delete(f"/api/si/requests/{rid}/files/{row['id']}")
    assert response.status_code == 409
    assert response.json()["request"]["status"] == "added"


# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------


def test_done_adds_in_one_commit(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """✓ always, "?" only when ticked (recorded as the user's), ✕ never; all
    through one call to the core function the CLI uses."""
    rid = _open(client, source="cli")["id"]
    by_name = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    by_title = _row(client, rid, "media-1.pdf", _si_pdf(A_TITLE))
    ticked = _row(client, rid, "Table_S2.xlsx", OTHER_SHEET)
    unticked = _row(client, rid, "Table_S3.csv", b"a,b\n1,2\n")
    refused = _row(client, rid, "41467_2023_38851_MOESM1_ESM.pdf", _si_pdf(B_TITLE))
    calls: list[list[SiItem]] = []
    real_add = si_mod.add_supplementary

    def counting_add(*args: Any, **kwargs: Any) -> Any:
        calls.append(list(args[2]))
        return real_add(*args, **kwargs)

    monkeypatch.setattr(routes_si, "add_supplementary", counting_add)

    response = client.post(
        f"/api/si/requests/{rid}/confirm", json={"addAnyway": [ticked["id"]]}
    )

    assert response.status_code == 200, response.text
    request = response.json()["request"]
    assert request["status"] == "added"
    assert len(calls) == 1 and len(calls[0]) == 3
    records = _records(vault)
    assert [(r["original"], r["check"]) for r in records] == [
        ("ci9b00237_si_001.xlsx", "filename"),
        ("media-1.pdf", "title"),
        ("Table_S2.xlsx", "user"),
    ]
    assert len({r["added"] for r in records}) == 1
    si_dir = vault / "papers" / A_ID / "supplementary"
    assert sorted(os.listdir(si_dir)) == ["si-01.xlsx", "si-02.pdf", "si-03.xlsx"]
    for name in os.listdir(si_dir):
        # Locked like paper.pdf. The owner-write bit is what both the POSIX
        # 0o444 and the Windows read-only attribute clear (root-proof too).
        assert not os.stat(si_dir / name).st_mode & stat.S_IWRITE, name
    assert request["added"] == [
        {
            "file": r["file"],
            "path": str(si_dir / r["file"]),
            "original": r["original"],
            "bytes": r["bytes"],
            "check": r["check"],
        }
        for r in records
    ]
    assert _stashes(vault) == []  # used, or dropped with the unticked row
    assert unticked["verdict"] == "unverified" and refused["verdict"] == "rejected"
    assert by_name["verdict"] == by_title["verdict"] == "ok"


def test_done_twice_answers_with_the_result(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]
    _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    first = client.post(f"/api/si/requests/{rid}/confirm", json={})
    second = client.post(f"/api/si/requests/{rid}/confirm", json={})
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert len(_records(vault)) == 1


def test_a_refused_row_can_never_be_ticked_in(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]
    _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    refused = _row(client, rid, "41467_2023_38851_MOESM1_ESM.pdf", _si_pdf(B_TITLE))
    response = client.post(
        f"/api/si/requests/{rid}/confirm", json={"addAnyway": [refused["id"]]}
    )
    assert response.status_code == 400
    assert "supplementary" not in find_paper(vault, A_ID)
    status = client.get(f"/api/si/requests/{rid}").json()["request"]
    assert status["status"] == "pending"


@pytest.mark.parametrize(
    "body",
    [{"addAnyway": "x"}, {"addAnyway": [1]}, ["x"]],
)
def test_done_refuses_a_bad_body(client: TestClient, body: Any) -> None:
    rid = _open(client)["id"]
    _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    assert client.post(f"/api/si/requests/{rid}/confirm", json=body).status_code == 400


def test_done_with_nothing_addable(client: TestClient, vault: Path) -> None:
    rid = _open(client)["id"]
    _row(client, rid, "Table_S2.xlsx", OTHER_SHEET)
    for body in ({}, {"addAnyway": ["f" * 32]}):
        assert client.post(f"/api/si/requests/{rid}/confirm", json=body).status_code == 400
    assert "supplementary" not in find_paper(vault, A_ID)


def test_a_check_that_changed_since_the_drop_stops_done(
    client: TestClient, vault: Path
) -> None:
    """The DOI was edited after the drop: the file no longer verifies. Core
    refuses it rather than record it as the user's; the row comes back as
    "?" for the user to decide, and nothing was written."""
    rid = _open(client)["id"]
    row = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    assert row["verdict"] == "ok"
    _set_doi(vault, A_ID, "10.1000/zz.edited")
    meta_before = _meta_bytes(vault)
    index_before = (vault / "INDEX.json").read_bytes()

    response = client.post(f"/api/si/requests/{rid}/confirm", json={})

    assert response.status_code == 409
    assert "changed" in response.json()["detail"]
    (refreshed,) = response.json()["request"]["rows"]
    assert (refreshed["id"], refreshed["verdict"]) == (row["id"], "unverified")
    assert response.json()["request"]["status"] == "pending"
    assert _meta_bytes(vault) == meta_before
    assert (vault / "INDEX.json").read_bytes() == index_before
    assert not (vault / "papers" / A_ID / "supplementary").exists()

    ok = client.post(f"/api/si/requests/{rid}/confirm", json={"addAnyway": [row["id"]]})
    assert ok.status_code == 200, ok.text
    assert _records(vault)[0]["check"] == "user"


def test_a_stash_that_vanished_drops_its_row(client: TestClient, vault: Path) -> None:
    """A second `lit gui` on the same library sweeps stashes at startup."""
    rid = _open(client)["id"]
    gone = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    kept = _row(client, rid, "ci9b00237_si_002.xlsx", OTHER_SHEET)
    (vault / UPLOAD_DIR / f"si-{rid}-{gone['id']}.xlsx").unlink()

    response = client.post(f"/api/si/requests/{rid}/confirm", json={})

    assert response.status_code == 409
    assert [r["id"] for r in response.json()["request"]["rows"]] == [kept["id"]]
    assert "supplementary" not in find_paper(vault, A_ID)
    assert client.post(f"/api/si/requests/{rid}/confirm", json={}).status_code == 200
    assert [r["original"] for r in _records(vault)] == ["ci9b00237_si_002.xlsx"]


def test_done_in_another_library_is_refused(
    client: TestClient, app: Any, vault: Path, tmp_path: Path
) -> None:
    rid = _open(client)["id"]
    row = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    other = _second_vault(tmp_path)
    app.state.vault = other

    for response in (
        client.post(f"/api/si/requests/{rid}/confirm", json={}),
        _drop(client, rid, "ci9b00237_si_002.xlsx", OTHER_SHEET),
        client.delete(f"/api/si/requests/{rid}/files/{row['id']}"),
    ):
        assert response.status_code == 409
        assert response.json()["request"]["vaultServed"] is False
    assert "supplementary" not in find_paper(other, A_ID)
    assert "supplementary" not in find_paper(vault, A_ID)
    # Cancel still ends it, whichever library is shown.
    assert client.post(f"/api/si/requests/{rid}/cancel").status_code == 200
    assert _stashes(vault) == []


def test_a_concurrent_write_to_the_paper_is_a_conflict(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _open(client)["id"]
    _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)

    def busy(*_args: Any, **_kwargs: Any) -> Any:
        raise FileExistsError(errno.EEXIST, "staging dir taken")

    monkeypatch.setattr(routes_si, "add_supplementary", busy)
    response = client.post(f"/api/si/requests/{rid}/confirm", json={})
    assert response.status_code == 409
    assert response.json()["request"]["status"] == "pending"


def test_a_full_disk_at_done(
    client: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _open(client)["id"]
    _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)

    def full(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(routes_si, "add_supplementary", full)
    response = client.post(f"/api/si/requests/{rid}/confirm", json={})
    assert response.status_code == 507
    status = client.get(f"/api/si/requests/{rid}").json()["request"]
    assert status["status"] == "pending"


def test_any_failure_puts_the_request_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _open(client)["id"]
    _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(routes_si, "add_supplementary", broken)
    with pytest.raises(RuntimeError):
        client.post(f"/api/si/requests/{rid}/confirm", json={})
    assert client.get(f"/api/si/requests/{rid}").json()["request"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_leaves_no_trace(client: TestClient, vault: Path) -> None:
    meta_before = _meta_bytes(vault)
    rid = _open(client, source="cli")["id"]
    _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    _row(client, rid, "Table_S2.xlsx", OTHER_SHEET)

    response = client.post(f"/api/si/requests/{rid}/cancel")

    assert response.status_code == 200
    assert response.json()["request"]["status"] == "cancelled"
    assert not (vault / "papers" / A_ID / "supplementary").exists()
    assert _meta_bytes(vault) == meta_before
    assert _stashes(vault) == []
    assert client.post(f"/api/si/requests/{rid}/cancel").status_code == 200
    assert client.get("/api/vault-version").json()["siRequest"] is None


def test_cancel_after_done_is_a_conflict(client: TestClient) -> None:
    rid = _open(client)["id"]
    _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    client.post(f"/api/si/requests/{rid}/confirm", json={})
    response = client.post(f"/api/si/requests/{rid}/cancel")
    assert response.status_code == 409
    assert response.json()["request"]["status"] == "added"
    assert client.post(f"/api/si/requests/{'f' * 32}/cancel").status_code == 404


# ---------------------------------------------------------------------------
# A paper's recorded SI files
# ---------------------------------------------------------------------------


def _add(vault: Path, tmp_path: Path, original: str, payload: bytes) -> str:
    source = tmp_path / "downloads" / uuid.uuid4().hex / original
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    result = add_supplementary(
        vault, A_ID, [SiItem(source, original, approved=True)], allow_unverified=False
    )
    name: str = result.records[0]["file"]
    return name


def test_a_pdf_opens_in_the_browser(client: TestClient, vault: Path, tmp_path: Path) -> None:
    payload = _si_pdf(A_TITLE)
    name = _add(vault, tmp_path, "ci9b00237_si_001.pdf", payload)
    response = client.get(f"/api/paper/{A_ID}/si/{name}")
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'inline; filename="ci9b00237_si_001.pdf"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_anything_else_downloads_under_its_own_name(
    client: TestClient, vault: Path, tmp_path: Path
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_Übersicht.xlsx", SHEET)
    response = client.get(f"/api/paper/{A_ID}/si/{name}")
    assert response.status_code == 200
    assert response.content == SHEET
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == (
        "attachment; filename*=utf-8''" + quote("ci9b00237_Übersicht.xlsx")
    )
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("original", "payload"),
    [
        ("ci9b00237_si_002.pdf", b"<html><script>alert(1)</script></html>"),
        ("ci9b00237_si_003.html", b"<html><script>alert(1)</script></html>"),
        ("ci9b00237_si_004.svg", b"<svg onload='alert(1)'/>"),
    ],
    ids=["not-really-a-pdf", "html", "svg"],
)
def test_nothing_but_a_real_pdf_is_shown_inline(
    client: TestClient, vault: Path, tmp_path: Path, original: str, payload: bytes
) -> None:
    name = _add(vault, tmp_path, original, payload)
    response = client.get(f"/api/paper/{A_ID}/si/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_only_recorded_files_are_served(client: TestClient, vault: Path, tmp_path: Path) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.pdf", _si_pdf(A_TITLE))
    si_dir = vault / "papers" / A_ID / "supplementary"
    (si_dir / "si-09.csv").write_bytes(b"a stray nobody recorded")
    for path in (
        f"/api/paper/{A_ID}/si/si-09.csv",
        f"/api/paper/{A_ID}/si/{name.upper()}",
        f"/api/paper/{A_ID}/si/si-01",
        f"/api/paper/{A_ID}/si/%2e%2e",
        f"/api/paper/{A_ID}/si/..%2Fpaper.pdf",
        f"/api/paper/{A_ID}/si/..%5Cpaper.pdf",
        f"/api/paper/{A_ID}/si/%2e%2e%2f%2e%2e%2fINDEX.json",
        f"/api/paper/..%2F{A_ID}/si/{name}",
        f"/api/paper/2000_Nobody_Here/si/{name}",
        f"/api/paper/{B_ID}/si/{name}",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.content != b"a stray nobody recorded"


def test_a_recorded_file_replaced_by_a_link_is_not_served(
    client: TestClient, vault: Path, tmp_path: Path
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    target = vault / "papers" / A_ID / "supplementary" / name
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"private")
    unlock_truth_file(target)
    target.unlink()
    try:
        os.symlink(secret, target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this system cannot make a symlink here: {exc}")
    response = client.get(f"/api/paper/{A_ID}/si/{name}")
    assert response.status_code == 404
    assert b"private" not in response.content


def test_a_linked_folder_is_not_served(client: TestClient, vault: Path, tmp_path: Path) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    si_dir = vault / "papers" / A_ID / "supplementary"
    moved = tmp_path / "moved-away"
    shutil.move(str(si_dir), str(moved))
    if not make_portable_link(si_dir, moved):
        pytest.skip("this filesystem cannot hold folder links")
    assert client.get(f"/api/paper/{A_ID}/si/{name}").status_code == 404
    response = client.delete(f"/api/paper/{A_ID}/si/{name}")
    assert response.status_code == 409
    assert sorted(os.listdir(moved)) == [name]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (FileExistsError(errno.EEXIST, "staging dir taken"), 409),
        (PermissionError(errno.EACCES, "file in use"), 500),
        (OSError(errno.ENOSPC, "No space left on device"), 507),
    ],
    ids=["concurrent-write", "commit-failed", "disk-full"],
)
def test_a_failed_removal_answers_in_words(
    client: TestClient,
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    status: int,
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")

    def failing(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(routes_si, "remove_supplementary", failing)
    response = client.delete(f"/api/paper/{A_ID}/si/{name}")
    assert response.status_code == status
    assert response.json()["detail"]
    assert [r["file"] for r in _records(vault)] == [name]
    if status == 500:
        assert response.json()["detail"] == (
            f"Could not delete {name} (PermissionError); nothing was changed. "
            "Check that the library's disk is writable, then try again."
        )


def test_a_file_that_cannot_be_deleted_keeps_its_record(
    client: TestClient, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")

    def in_use(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(errno.EACCES, "in use (mocked)")

    monkeypatch.setattr(si_mod, "_delete_si_entry", in_use)
    response = client.delete(f"/api/paper/{A_ID}/si/{name}")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Could not delete {name} (PermissionError); its record is back. "
        "Close anything that has it open, then retry."
    )
    assert [r["file"] for r in _records(vault)] == [name]
    assert (vault / "papers" / A_ID / "supplementary" / name).exists()


# ---------------------------------------------------------------------------
# rm-preview: where the notes cite a file, for the delete dialog
# ---------------------------------------------------------------------------


def _write_notes(vault: Path, notes: str | bytes, discussion: str = "") -> None:
    paper = vault / "papers" / A_ID
    if isinstance(notes, bytes):
        (paper / "notes.md").write_bytes(notes)
    else:
        (paper / "notes.md").write_text(notes, encoding="utf-8")
    (paper / "discussion.md").write_text(discussion, encoding="utf-8")


def test_rm_preview_counts_the_lines_citing_only_this_file(
    client: TestClient, vault: Path, tmp_path: Path
) -> None:
    first = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    second = _add(vault, tmp_path, "ci9b00237_si_002.xlsx", SHEET)
    _write_notes(
        vault,
        "# Notes\nsi-02.xlsx（ci9b00237_si_002.xlsx）\nsi-01 only\n",
        "# Discussion log\n\n## 2026-10-20 10:00\n"
        "si-02 and si-02.xlsx on one line\n"
        "si-02.xlsx (Table_S9.xlsx) is some other file\n"
        "si-02 once more\n",
    )

    response = client.get(f"/api/paper/{A_ID}/si/{second}/rm-preview")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "file": second,
        "cited": [
            {"name": "notes.md", "count": 1},
            {"name": "discussion.md", "count": 2},
        ],
        "unchecked": [],
    }
    assert client.get(f"/api/paper/{A_ID}/si/{first}/rm-preview").json()["cited"] == [
        {"name": "notes.md", "count": 1}
    ]


def test_rm_preview_names_a_notes_file_it_cannot_read(
    client: TestClient, vault: Path, tmp_path: Path
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    _write_notes(vault, b"si-01 caf\xe9\n")

    response = client.get(f"/api/paper/{A_ID}/si/{name}/rm-preview")

    assert response.json() == {"file": name, "cited": [], "unchecked": ["notes.md"]}


def test_rm_preview_answers_only_for_a_recorded_name(
    client: TestClient, vault: Path, tmp_path: Path
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    for path in (
        f"/api/paper/{A_ID}/si/si-01/rm-preview",
        f"/api/paper/{A_ID}/si/si-09.csv/rm-preview",
        f"/api/paper/2000_Nobody_Here/si/{name}/rm-preview",
        f"/api/paper/..x/si/{name}/rm-preview",
    ):
        assert client.get(path).status_code == 404, path


def test_rm_preview_refuses_as_the_delete_would(
    client: TestClient, vault: Path, tmp_path: Path
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    target = vault / "papers" / A_ID / "supplementary" / name
    unlock_truth_file(target)
    target.unlink()
    target.mkdir()

    response = client.get(f"/api/paper/{A_ID}/si/{name}/rm-preview")

    assert response.status_code == 409
    assert "is a folder, not the recorded file" in response.json()["detail"]
    assert client.delete(f"/api/paper/{A_ID}/si/{name}").status_code == 409
    assert target.is_dir()


def test_removing_a_file_deletes_it(
    client: TestClient, vault: Path, tmp_path: Path
) -> None:
    first = _add(vault, tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    second = _add(vault, tmp_path, "ci9b00237_si_002.xlsx", SHEET)

    response = client.delete(f"/api/paper/{A_ID}/si/{second}")

    assert response.status_code == 200
    assert response.json() == {"file": second, "warnings": []}
    assert not (vault / "papers" / A_ID / "supplementary" / second).exists()
    assert not (vault / ".trash" / "removed-si").exists()
    assert [r["file"] for r in _records(vault)] == [first]
    for path in (
        f"/api/paper/{A_ID}/si/{second}",
        f"/api/paper/{A_ID}/si/si-01",
        f"/api/paper/2000_Nobody_Here/si/{first}",
        f"/api/paper/..x/si/{first}",
    ):
        assert client.delete(path).status_code == 404, path
    assert [r["file"] for r in _records(vault)] == [first]


# ---------------------------------------------------------------------------
# Only the litman page writes; GET never does
# ---------------------------------------------------------------------------


def _write_calls(
    client: TestClient, vault: Path, tmp_path: Path
) -> list[tuple[str, str, dict[str, Any]]]:
    rid = _open(client)["id"]
    row = _row(client, rid, "ci9b00237_si_001.xlsx", SHEET)
    name = _add(vault, tmp_path, "ci9b00237_si_002.csv", b"1,2\n")
    return [
        ("POST", "/api/si/requests", {"json": {"paperId": B_ID, "source": "page"}}),
        ("POST", f"/api/si/requests/{rid}/files", {
            "content": OTHER_SHEET, "headers": {"X-Litman-Filename": "Table_S9.xlsx"},
        }),
        ("DELETE", f"/api/si/requests/{rid}/files/{row['id']}", {}),
        ("POST", f"/api/si/requests/{rid}/cancel", {}),
        ("DELETE", f"/api/paper/{A_ID}/si/{name}", {}),
        ("POST", f"/api/si/requests/{_open(client, paper=C_ID)['id']}/confirm", {"json": {}}),
    ]


def test_another_website_cannot_write(client: TestClient, vault: Path, tmp_path: Path) -> None:
    meta_before = _meta_bytes(vault)
    stashes_before = _stashes(vault)
    for method, path, kwargs in _write_calls(client, vault, tmp_path):
        meta_before = _meta_bytes(vault)
        stashes_before = _stashes(vault)
        headers = {**kwargs.pop("headers", {}), "Origin": FOREIGN}
        response = client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == 403, (method, path)
        assert _meta_bytes(vault) == meta_before
        assert _stashes(vault) == stashes_before


def test_the_litman_page_itself_can_write(client: TestClient, vault: Path, tmp_path: Path) -> None:
    for method, path, kwargs in _write_calls(client, vault, tmp_path):
        headers = {**kwargs.pop("headers", {}), "Origin": GUI_URL}
        response = client.request(method, path, headers=headers, **kwargs)
        assert response.status_code != 403, (method, path, response.text)


def _tree(root: Path) -> dict[str, tuple[bool, int, int]]:
    out: dict[str, tuple[bool, int, int]] = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder) / name
            st = path.lstat()
            out[str(path.relative_to(root))] = (path.is_dir(), st.st_size, st.st_mtime_ns)
    return out


def test_reading_writes_nothing(client: TestClient, vault: Path, tmp_path: Path) -> None:
    pdf = _add(vault, tmp_path, "ci9b00237_si_001.pdf", _si_pdf(A_TITLE))
    sheet = _add(vault, tmp_path, "ci9b00237_si_002.xlsx", SHEET)
    rid = _open(client, source="cli")["id"]
    _row(client, rid, "Table_S2.xlsx", OTHER_SHEET)
    done = _open(client, paper=B_ID, source="cli")["id"]
    _row(client, done, "41467_2023_38851_MOESM2_ESM.xlsx", SHEET)
    client.post(f"/api/si/requests/{done}/cancel")
    before = _tree(vault)

    for path in (
        "/api/si/ping",
        f"/api/si/requests/{rid}",
        f"/api/si/requests/{rid}?collect=1",
        f"/api/si/requests/{done}?collect=1",
        f"/api/si/requests/{'f' * 32}",
        "/api/vault-version",
        f"/api/paper/{A_ID}/si/{pdf}",
        f"/api/paper/{A_ID}/si/{sheet}",
        f"/api/paper/{A_ID}/si/si-77.pdf",
        f"/api/paper/{A_ID}/si/{sheet}/rm-preview",
        f"/api/paper/{A_ID}/si/si-77.pdf/rm-preview",
        f"/api/paper/{A_ID}",
    ):
        assert client.get(path).status_code in (200, 404), path

    assert _tree(vault) == before


# ---------------------------------------------------------------------------
# Opening a recorded SI file with this machine's default app
# ---------------------------------------------------------------------------


class _Launch:
    """Stands in for ``viewer.launch_file``: records what it was asked to
    open, with which configured viewer, and fails when told to."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str | None]] = []
        self.error: Exception | None = None

    def __call__(self, path: Path, configured_viewer: str | None = None) -> tuple[str, str]:
        self.calls.append((path, configured_viewer))
        if self.error is not None:
            raise self.error
        return ("xdg-open", "platform")


@pytest.fixture
def launch(monkeypatch: pytest.MonkeyPatch) -> _Launch:
    """This machine has a desktop, and opening a file is recorded."""
    fake = _Launch()
    monkeypatch.setattr(viewer_mod, "open_files_mode", lambda: "desktop")
    monkeypatch.setattr(viewer_mod, "launch_file", fake)
    return fake


def _post_open(client: TestClient, name: str, paper: str = A_ID, **kwargs: Any) -> Any:
    return client.post(f"/api/paper/{paper}/si/{name}/open", **kwargs)


def _si_file(vault: Path, name: str) -> Path:
    return vault / "papers" / A_ID / "supplementary" / name


def _configure_viewer(vault: Path, command: str) -> None:
    config = vault / CONFIG_FILENAME
    text = config.read_text(encoding="utf-8")
    assert "default_pdf_viewer: null" in text
    config.write_text(
        text.replace("default_pdf_viewer: null", f"default_pdf_viewer: {command}"),
        encoding="utf-8",
    )


def test_open_starts_the_default_app_on_this_machine(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.xlsx", SHEET)

    response = _post_open(client, name)

    assert response.status_code == 200, response.text
    assert response.json() == {"opened": True, "file": name, "host": host_id()}
    assert launch.calls == [(_si_file(vault, name), None)]


def test_only_a_pdf_goes_to_the_configured_viewer(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch
) -> None:
    _configure_viewer(vault, "zathura")
    pdf = _add(vault, tmp_path, "ci9b00237_si_001.pdf", _si_pdf(A_TITLE))
    sheet = _add(vault, tmp_path, "ci9b00237_si_002.xlsx", SHEET)

    assert _post_open(client, pdf).status_code == 200
    assert _post_open(client, sheet).status_code == 200
    assert launch.calls == [
        (_si_file(vault, pdf), "zathura"),
        (_si_file(vault, sheet), None),
    ]


def test_without_a_desktop_nothing_starts(
    client: TestClient,
    vault: Path,
    tmp_path: Path,
    launch: _Launch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(viewer_mod, "open_files_mode", lambda: "none")
    name = _add(vault, tmp_path, "ci9b00237_si_001.xlsx", SHEET)

    response = _post_open(client, name)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"{host_id()} has no desktop to open {name} on — download it instead"
    )
    assert launch.calls == []


@pytest.mark.parametrize(
    "original",
    ["ci9b00237_tool.exe", "ci9b00237_setup.MSI", "ci9b00237_run.sh", "README", "x.appref-ms"],
)
def test_a_file_the_default_app_would_run_is_refused(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch, original: str
) -> None:
    name = _add(vault, tmp_path, original, b"MZ program")

    response = _post_open(client, name)

    assert response.status_code == 403
    assert response.json()["detail"] == (
        f"{name} could be run, not viewed — download it instead"
    )
    assert launch.calls == []
    # It still downloads.
    assert client.get(f"/api/paper/{A_ID}/si/{name}").status_code == 200


def test_the_last_suffix_decides(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch
) -> None:
    archive = _add(vault, tmp_path, "ci9b00237_si_001.tar.gz", b"\x1f\x8b archive")
    assert archive == "si-01.tar.gz"
    # Recorded by hand: two suffixes, the last one runs.
    disguised = "si-02.pdf.exe"
    _si_file(vault, disguised).write_bytes(b"MZ program")
    meta_path = vault / "papers" / A_ID / "metadata.yaml"
    unlock_truth_file(meta_path)
    text = meta_path.read_text(encoding="utf-8")
    meta_path.write_text(
        text.replace(
            "supplementary:\n",
            f"supplementary:\n  - file: {disguised}\n    original: tool.pdf.exe\n",
        ),
        encoding="utf-8",
    )
    assert disguised in [r["file"] for r in _records(vault)]

    assert _post_open(client, disguised).status_code == 403
    assert _post_open(client, archive).status_code == 200
    assert launch.calls == [(_si_file(vault, archive), None)]


def test_only_a_recorded_file_is_opened(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.xlsx", SHEET)
    _si_file(vault, "si-09.csv").write_bytes(b"a stray nobody recorded")
    for paper, file in (
        (A_ID, "si-09.csv"),
        (A_ID, "si-01"),
        (A_ID, name.upper()),
        ("2000_Nobody_Here", name),
        (B_ID, name),
    ):
        response = _post_open(client, file, paper=paper)
        assert response.status_code == 404, (paper, file)
    # A path in disguise never reaches the route at all.
    for file in ("..%2Fpaper.pdf", "%2e%2e", "..%5Cpaper.pdf"):
        response = _post_open(client, file)
        assert response.status_code in (404, 405), file
    assert launch.calls == []


def test_an_invalid_paper_id_is_404(client: TestClient, launch: _Launch) -> None:
    response = _post_open(client, "si-01.pdf", paper="..x")
    assert response.status_code == 404
    assert response.json()["detail"].startswith("Invalid paper id: '..x'.")
    assert launch.calls == []


@pytest.mark.parametrize(
    ("reason", "detail"),
    [
        ("no-app", "No app on {host} opened {file} — download it instead"),
        ("not-started", "{host} could not start xdg-open to open {file} — download it instead"),
    ],
)
def test_an_opener_that_fails_is_409_in_words(
    client: TestClient,
    vault: Path,
    tmp_path: Path,
    launch: _Launch,
    reason: Literal["no-app", "not-started"],
    detail: str,
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.xlsx", SHEET)
    launch.error = viewer_mod.OpenFailed("no", command="xdg-open", reason=reason)

    response = _post_open(client, name)

    assert response.status_code == 409
    assert response.json()["detail"] == detail.format(host=host_id(), file=name)


def test_a_broken_config_stops_only_a_pdf(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch
) -> None:
    pdf = _add(vault, tmp_path, "ci9b00237_si_001.pdf", _si_pdf(A_TITLE))
    sheet = _add(vault, tmp_path, "ci9b00237_si_002.xlsx", SHEET)
    with (vault / CONFIG_FILENAME).open("a", encoding="utf-8") as f:
        f.write("no_such_setting: 1\n")

    response = _post_open(client, pdf)

    assert response.status_code == 500
    detail = response.json()["detail"]
    # One line for a toast: the field at fault and the way out, no path.
    assert detail.startswith("lit-config.yaml is invalid (no_such_setting: ")
    assert detail.endswith(") — fix it, or download the file instead")
    assert "\n" not in detail and str(vault) not in detail
    assert len(detail) <= 130
    assert _post_open(client, sheet).status_code == 200
    assert launch.calls == [(_si_file(vault, sheet), None)]


def test_an_unparsable_config_says_so_in_one_line(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch
) -> None:
    pdf = _add(vault, tmp_path, "ci9b00237_si_001.pdf", _si_pdf(A_TITLE))
    (vault / CONFIG_FILENAME).write_text("library_name: [unclosed\n", encoding="utf-8")

    response = _post_open(client, pdf)

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "lit-config.yaml cannot be read (lit config show says why) "
        "— fix it, or download the file instead"
    )
    assert launch.calls == []


def test_another_website_cannot_open_a_file(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.xlsx", SHEET)

    refused = _post_open(client, name, headers={"Origin": FOREIGN})
    assert refused.status_code == 403
    assert refused.json()["detail"] == (
        "Only the litman page itself can send this request, not another website."
    )
    assert launch.calls == []

    allowed = _post_open(client, name, headers={"Origin": GUI_URL})
    assert allowed.status_code == 200, allowed.text
    assert len(launch.calls) == 1


def test_a_get_never_opens_anything(
    client: TestClient, vault: Path, tmp_path: Path, launch: _Launch
) -> None:
    name = _add(vault, tmp_path, "ci9b00237_si_001.xlsx", SHEET)
    response = client.get(f"/api/paper/{A_ID}/si/{name}/open")
    assert response.status_code in (404, 405)
    assert launch.calls == []


def test_open_waits_off_the_event_loop() -> None:
    """The opener gets up to OPEN_WAIT_S to fail; on the event loop that wait
    would stall every other request."""
    assert not inspect.iscoroutinefunction(routes_si.post_si_file_open)


def test_open_goes_through_the_real_launch_file(
    client: TestClient, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's own default path: open_files_mode from the environment,
    launch_file choosing xdg-open and waiting on its exit — only the process
    itself is faked."""
    for variable in ("SSH_CONNECTION", "SSH_CLIENT", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(viewer_mod, "_platform", lambda: "linux")
    monkeypatch.setattr(
        viewer_mod, "_which", lambda cmd: "/usr/bin/xdg-open" if cmd == "xdg-open" else None
    )
    argvs: list[list[str]] = []
    exit_code = [0]

    class _Process:
        def wait(self, timeout: float | None = None) -> int:
            assert timeout == viewer_mod.OPEN_WAIT_S
            return exit_code[0]

    def spawn(argv: list[str]) -> _Process:
        argvs.append(list(argv))
        return _Process()

    monkeypatch.setattr(viewer_mod, "_spawn", spawn)
    name = _add(vault, tmp_path, "ci9b00237_si_001.xlsx", SHEET)

    assert client.get("/api/capabilities").json()["open_files"] == "desktop"
    assert _post_open(client, name).status_code == 200
    assert argvs == [["xdg-open", str(_si_file(vault, name))]]

    exit_code[0] = 3
    failed = _post_open(client, name)
    assert failed.status_code == 409
    assert failed.json()["detail"] == (
        f"No app on {host_id()} opened {name} — download it instead"
    )
