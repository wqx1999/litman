"""``lit add --si PAPER`` without files: asking the open GUI page (M3).

Two kinds of GUI stand in for ``lit gui``:

* the real routes, in process: ``create_app(vault)`` behind
  ``tests/server/_client.TestClient``, recorded with ``register_gui`` so
  ``find_gui`` finds it, one page counted on ``app.state.presence``. The
  user's part — drop a file, press Done or Cancel — is played by a second
  client from inside the fake sleep, the way the page would between polls;
* ``httpx.MockTransport``, for the answers the real routes do not give on
  demand (timeouts, refusals, 5xx, garbage, a GUI that forgot the request).

Time is fake throughout: ``_si_gui._clock`` moves only when ``_si_gui._sleep``
is called, so the 90-second wait takes no time. Patches go on module objects,
never dotted strings: tests/commands/test_gui.py purges ``litman.cli*`` and
``litman.server*`` from ``sys.modules`` mid-suite.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner, Result

from litman.cli import cli
from litman.commands import _si_gui as si_gui
from litman.core import supplementary as si_mod
from litman.core.document import find_paper, list_papers
from litman.core.gui_registry import gui_dir, host_id, register_gui
from litman.core.library import create_vault
from litman.core.locking import unlock_truth_file
from litman.core.views import write_index
from litman.exceptions import PaperNotFoundError, SupplementaryError
from litman.server import create_app
from tests.conftest import _build_text_pdf
from tests.server._client import GUI_URL, TestClient

A_ID = "2019_Yang_Concepts"
A_DOI = "10.1021/acs.jcim.9b00237"
A_TITLE = "Concepts of Artificial Intelligence for Computer-Assisted Drug Discovery"
C_ID = "2020_Lee_Preprint"  # an arXiv id, no DOI
D_ID = "2018_Kim_Notes"  # neither
N_ID = "2017_Roe_Plain"  # its text points to "Appendix D", never to SI

NO_PAGE = "No litman GUI page is open for this library"
STILL_WAITING = "Still waiting in the GUI — run the same command again to keep waiting"
WAITING = "Waiting for the SI to be added in the litman GUI…"
A_LINK = "Download the SI from the article page: https://doi.org/10.1021/acs.jcim.9b00237"
A_FORM = f"Then add the files: lit add --si {A_ID} <file>..."

_REAL_NEW_CLIENT = si_gui._new_client

# A request id as the server mints them, for the mocked GUI.
RID = "0123456789abcdef0123456789abcdef"
MOCK_PORT = 9100
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}"


def _write_paper(
    vault: Path,
    paper_id: str,
    *,
    title: str,
    doi: str = "",
    arxiv: str = "",
    text: list[str] | None,
) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\n"
        f"title: {title}\n"
        "authors:\n  - Doe, Jane\n"
        "year: 2020\n"
        f"doi: '{doi}'\n"
        f"arxiv-id: '{arxiv}'\n"
        "status: inbox\n"
        "created-at: '2026-01-01T00:00:00+00:00'\n"
        "updated-at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    if text is not None:
        (paper_dir / "paper.pdf").write_bytes(_build_text_pdf([text]))


def _mentions(title: str) -> list[str]:
    return [
        title,
        "Jane Doe",
        "Abstract: machine learning reshapes how chemists discover new medicines.",
        "Supporting Information is available online.",
    ]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = create_vault(tmp_path)
    _write_paper(v, A_ID, title=A_TITLE, doi=A_DOI, text=_mentions(A_TITLE))
    _write_paper(
        v,
        C_ID,
        title="A preprint without a DOI",
        arxiv="2101.00001",
        text=_mentions("A preprint without a DOI"),
    )
    _write_paper(v, D_ID, title="Notes with no link", text=_mentions("Notes"))
    _write_paper(
        v,
        N_ID,
        title="A plain article",
        doi="10.1000/plain.1",
        text=["A plain article", "Jane Doe", "The ablations are in Appendix D."],
    )
    write_index(v, list_papers(v))
    return v


def _run(vault: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["add", "--si", *args, "--library", str(vault)])


def _si_pdf(title: str, table: str = "Table S1") -> bytes:
    return _build_text_pdf(
        [
            [
                "Supporting Information",
                title,
                f"{table} hyperparameters tuned per run",
                "Figure S2 training curves for every model variant",
            ]
        ]
    )


def _si_path(vault: Path, name: str, paper_id: str = A_ID) -> Path:
    return (vault / "papers" / paper_id / "supplementary" / name).resolve()


def _ready(lines: list[str]) -> list[tuple[Path, str]]:
    """The file lines after ``SI ready``: each path, resolved, and the original
    name after its tab."""
    rows: list[tuple[Path, str]] = []
    for line in lines:
        path, tab, original = line.partition("\t")
        assert tab, f"no tab in {line!r}"
        rows.append((Path(path).resolve(), original))
    return rows


def _add_si_by_file(vault: Path, tmp_path: Path, name: str, table: str) -> None:
    source = tmp_path / name
    source.write_bytes(_si_pdf(A_TITLE, table))
    result = _run(vault, A_ID, str(source))
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Fake time, and no network unless a test sets up a GUI
# ---------------------------------------------------------------------------


class FakeTime:
    """A clock that moves only when slept on; ``after(n, act)`` runs ``act``
    during the n-th sleep from now (the page acting between two polls)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps = 0
        self._acts: dict[int, Callable[[], None]] = {}

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert 0 < seconds <= si_gui.POLL_S
        self.sleeps += 1
        self.now += seconds
        act = self._acts.pop(self.sleeps, None)
        if act is not None:
            act()

    def after(self, n: int, act: Callable[[], None]) -> None:
        self._acts[self.sleeps + n] = act


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(si_gui, "_clock", fake.clock)
    monkeypatch.setattr(si_gui, "_sleep", fake.sleep)
    return fake


def _refuse_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"contacted a GUI: {request.method} {request.url}")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        si_gui,
        "_new_client",
        lambda: httpx.Client(transport=httpx.MockTransport(_refuse_network)),
    )


# ---------------------------------------------------------------------------
# The real routes, with one page open
# ---------------------------------------------------------------------------


class _Recording(TestClient):
    """The CLI's client: the real app, every request noted."""

    def __init__(self, app: Any, seen: list[tuple[str, str]]) -> None:
        super().__init__(app)
        self._seen = seen

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        self._seen.append((request.method, request.url.path))
        response: httpx.Response = super().send(request, **kwargs)
        return response

    def post(self, url: Any, **kwargs: Any) -> httpx.Response:
        # Starlette warns on a per-request timeout; the CLI's own is pinned
        # through MockTransport (test_the_request_and_its_polls_as_sent).
        kwargs.pop("timeout", None)
        response: httpx.Response = super().post(url, **kwargs)
        return response


class Gui:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.seen: list[tuple[str, str]] = []
        self.page = TestClient(app)

    def polls(self) -> list[str]:
        return [
            path
            for method, path in self.seen
            if method == "GET" and path.startswith("/api/si/requests/")
        ]

    def posts(self) -> int:
        return self.seen.count(("POST", "/api/si/requests"))

    def open_ids(self) -> list[str]:
        table = self.app.state.si_requests
        return [rid for rid, req in table._requests.items() if req.is_open]

    def shown(self) -> str:
        """The request the page would show now (its vault-version poll)."""
        answer = self.page.get("/api/vault-version")
        assert answer.status_code == 200, answer.text
        shown: dict[str, Any] | None = answer.json()["siRequest"]
        assert shown is not None, "no SI window to show"
        rid: str = shown["id"]
        return rid

    def done(self, name: str = "ci9b00237_si_001.pdf", table: str = "Table S1") -> None:
        rid = self.shown()
        dropped = self.page.post(
            f"/api/si/requests/{rid}/files",
            content=_si_pdf(A_TITLE, table),
            headers={"X-Litman-Filename": quote(name)},
        )
        assert dropped.status_code == 200, dropped.text
        assert dropped.json()["row"]["verdict"] == "ok"
        confirmed = self.page.post(f"/api/si/requests/{rid}/confirm", json={"addAnyway": []})
        assert confirmed.status_code == 200, confirmed.text

    def cancel(self) -> None:
        cancelled = self.page.post(f"/api/si/requests/{self.shown()}/cancel")
        assert cancelled.status_code == 200, cancelled.text


@pytest.fixture
def gui(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Gui:
    fake = Gui(create_app(vault))
    fake.app.state.presence.connect()
    register_gui(8765, vault)  # the port GUI_URL names
    monkeypatch.setattr(si_gui, "_new_client", lambda: _Recording(fake.app, fake.seen))
    return fake


# ---------------------------------------------------------------------------
# A mocked GUI, for the answers the real routes do not give on demand
# ---------------------------------------------------------------------------

Answer = Callable[[httpx.Request], httpx.Response]


def _snapshot(
    status: str = "pending",
    *,
    vault_served: bool = True,
    added: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "request": {
            "id": RID,
            "paperId": A_ID,
            "title": A_TITLE,
            "link": None,
            "status": status,
            "vaultServed": vault_served,
            "rows": [],
            "added": added or [],
            "warnings": warnings or [],
        }
    }


def _reply(code: int, body: Any = None, **kwargs: Any) -> Answer:
    if body is None:
        return lambda _request: httpx.Response(code, **kwargs)
    return lambda _request: httpx.Response(code, json=body, **kwargs)


def _fail(exc: Exception) -> Answer:
    def answer(_request: httpx.Request) -> httpx.Response:
        raise exc

    return answer


def _sequence(*answers: Answer) -> Answer:
    """One answer per call, the last repeated."""
    calls = [0]

    def answer(request: httpx.Request) -> httpx.Response:
        index = min(calls[0], len(answers) - 1)
        calls[0] += 1
        return answers[index](request)

    return answer


def _mock_transport(
    vault: Path,
    *,
    post: Answer,
    get: Answer | None = None,
    pages: int = 1,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A GUI recorded on :data:`MOCK_PORT` whose ping says it serves
    ``vault``; ``post`` / ``get`` answer the request calls. Returns the
    transport and the list each request it is sent lands in."""
    register_gui(MOCK_PORT, vault)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == MOCK_PORT
        path = request.url.path
        if path == "/api/si/ping":
            return httpx.Response(
                200,
                json={
                    "app": "litman",
                    "version": "1.3.8",
                    "pid": os.getpid(),
                    "host": host_id(),
                    "vault": str(vault.resolve()),
                    "pages": pages,
                },
            )
        if request.method == "POST" and path == "/api/si/requests":
            return post(request)
        if request.method == "GET" and path == f"/api/si/requests/{RID}":
            assert get is not None, "polled a GUI that should not be polled"
            assert request.url.params.get("collect") == "1"
            return get(request)
        raise AssertionError(f"unexpected {request.method} {path}")

    return httpx.MockTransport(handler), sent


def _mock_gui(
    monkeypatch: pytest.MonkeyPatch,
    vault: Path,
    *,
    post: Answer,
    get: Answer | None = None,
    pages: int = 1,
) -> list[httpx.Request]:
    """:func:`_mock_transport` as the CLI's client; returns what it sent."""
    transport, sent = _mock_transport(vault, post=post, get=get, pages=pages)
    monkeypatch.setattr(si_gui, "_new_client", lambda: httpx.Client(transport=transport))
    return sent


def _gets(sent: list[httpx.Request]) -> int:
    return sum(1 for r in sent if r.method == "GET" and r.url.path.startswith("/api/si/requests/"))


# ---------------------------------------------------------------------------
# Settled before any GUI is asked: 1, 0 (already), 13
# ---------------------------------------------------------------------------


def test_an_unknown_paper_is_an_error(vault: Path, gui: Gui) -> None:
    result = _run(vault, "2099_Nobody_Here")
    assert result.exit_code == 1
    assert isinstance(result.exception, PaperNotFoundError)
    assert gui.seen == []


def test_an_ambiguous_paper_is_an_error(vault: Path, gui: Gui) -> None:
    result = _run(vault, "20")  # every id in this library starts with it
    assert result.exit_code == 1
    assert isinstance(result.exception, PaperNotFoundError)
    assert gui.seen == []


def test_a_broken_si_field_is_an_error_and_the_gui_is_never_asked(vault: Path, gui: Gui) -> None:
    """Review S8: only a hand edit fixes it, so it must read as an error (1) —
    never as "no GUI" (12), which is what the GUI's 409 would turn into."""
    meta = vault / "papers" / A_ID / "metadata.yaml"
    meta.write_text(
        meta.read_text(encoding="utf-8") + "supplementary: not-a-list\n",
        encoding="utf-8",
    )

    result = _run(vault, A_ID, "--force")

    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert str(result.exception) == (
        f"papers/{A_ID}/metadata.yaml: 'supplementary' is not a list. Fix it by hand, then retry."
    )
    assert gui.seen == []
    assert gui.open_ids() == []


def test_si_already_in_the_library_is_0_without_asking(
    vault: Path, gui: Gui, tmp_path: Path
) -> None:
    _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")

    result = _run(vault, A_ID)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "SI ready: 1 file(s) (already in the library)"
    assert _ready(lines[1:]) == [(_si_path(vault, "si-01.pdf"), "ci9b00237_si_001.pdf")]
    assert gui.seen == []


def test_already_in_the_library_warns_about_a_missing_file(
    vault: Path, gui: Gui, tmp_path: Path
) -> None:
    _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")
    _add_si_by_file(vault, tmp_path, "ci9b00237_si_002.pdf", "Table S2")
    gone = _si_path(vault, "si-01.pdf")
    unlock_truth_file(gone)
    gone.unlink()

    result = _run(vault, A_ID)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "SI ready: 1 file(s) (already in the library)"
    assert _ready(lines[1:]) == [(_si_path(vault, "si-02.pdf"), "ci9b00237_si_002.pdf")]
    assert "si-01.pdf is missing" in result.stderr
    assert "lit health-check" in result.stderr
    assert gui.seen == []


def test_already_in_the_library_but_every_file_missing_is_an_error(
    vault: Path, gui: Gui, tmp_path: Path
) -> None:
    _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")
    gone = _si_path(vault, "si-01.pdf")
    unlock_truth_file(gone)
    gone.unlink()

    result = _run(vault, A_ID)

    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert "lit health-check" in str(result.exception)
    assert gui.seen == []


def test_force_asks_the_gui_although_si_is_there(
    vault: Path, gui: Gui, clock: FakeTime, tmp_path: Path
) -> None:
    _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")
    clock.after(2, lambda: gui.done("ci9b00237_si_002.pdf", "Table S2"))

    result = _run(vault, A_ID, "--force")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "SI ready: 1 file(s)"
    assert _ready(lines[1:]) == [(_si_path(vault, "si-02.pdf"), "ci9b00237_si_002.pdf")]


def test_a_text_that_never_mentions_si_still_asks_the_gui(
    vault: Path, gui: Gui, clock: FakeTime
) -> None:
    """No --force needed: what the article's text says is never a reason not
    to ask, since "see Appendix D" names a separate file as well as SI does."""
    clock.after(1, gui.cancel)
    result = _run(vault, N_ID)
    assert result.exit_code == 10, result.output
    assert gui.posts() == 1


@pytest.mark.parametrize("pdf", ["scanned", "missing"])
def test_no_text_layer_or_no_pdf_still_asks(vault: Path, pdf: str) -> None:
    """A scan (no text layer) or no paper.pdf at all: on to the GUI — here,
    none."""
    paper_pdf = vault / "papers" / N_ID / "paper.pdf"
    if pdf == "scanned":
        paper_pdf.write_bytes(_build_text_pdf([[]]))
    else:
        paper_pdf.unlink()

    result = _run(vault, N_ID)

    assert result.exit_code == 12, result.output
    assert result.stdout.splitlines()[0] == NO_PAGE


# ---------------------------------------------------------------------------
# 12 — no GUI page for this library
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paper_id", "where"),
    [
        (A_ID, A_LINK),
        (C_ID, "Download the SI from the article page: https://arxiv.org/abs/2101.00001"),
        (D_ID, "No link on record — download the SI from the article's page yourself"),
    ],
    ids=["doi", "arxiv", "no-link"],
)
def test_no_gui_is_12_with_the_link_and_the_file_form(
    vault: Path, paper_id: str, where: str
) -> None:
    result = _run(vault, paper_id)

    assert result.exit_code == 12, result.output
    assert result.stdout.splitlines() == [
        NO_PAGE,
        where,
        f"Then add the files: lit add --si {paper_id} <file>...",
    ]


def test_a_gui_without_a_page_is_12_naming_its_address(vault: Path, gui: Gui) -> None:
    gui.app.state.presence.disconnect()

    result = _run(vault, A_ID)

    assert result.exit_code == 12, result.output
    assert result.stdout.splitlines() == [
        NO_PAGE,
        f"The litman GUI runs at {GUI_URL} but has no page open — "
        "open it, then run the same command again",
        A_LINK,
        A_FORM,
    ]
    assert gui.seen == [("GET", "/api/si/ping")]
    assert gui.open_ids() == []


def test_a_gui_on_another_host_is_12_naming_the_host(vault: Path) -> None:
    """Read from its record alone — another host's 127.0.0.1 is another
    machine, so it is neither asked (the autouse client refuses any request)
    nor deleted."""
    folder = gui_dir()
    folder.mkdir(parents=True)
    record = folder / "node-b-77.json"
    record.write_text(
        json.dumps(
            {
                "app": "litman",
                "version": "1.3.8",
                "host": "node-b",
                "pid": 77,
                "port": 8765,
                "vault": str(vault.resolve()),
                "started": 1_760_000_000.0,
            }
        ),
        encoding="utf-8",
    )

    result = _run(vault, A_ID)

    assert result.exit_code == 12, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == NO_PAGE
    assert re.fullmatch(
        r"A litman GUI for this library may be running on host node-b "
        r"\(started \d{4}-\d{2}-\d{2} \d{2}:\d{2}\) — run the command there",
        lines[1],
    )
    assert len(lines[1]) <= 120
    assert lines[2:] == [A_LINK, A_FORM]
    assert record.exists()


# ---------------------------------------------------------------------------
# The page answers: 0 / 10, and 11 with the rerun picking the request up
# ---------------------------------------------------------------------------


def test_done_in_the_page_is_0_with_the_new_paths(vault: Path, gui: Gui, clock: FakeTime) -> None:
    clock.after(3, gui.done)

    result = _run(vault, A_ID)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "SI ready: 1 file(s)"
    assert len(lines) == 2
    path, original = lines[1].split("\t")
    assert Path(path).is_absolute()
    assert Path(path).resolve() == _si_path(vault, "si-01.pdf")
    assert original == "ci9b00237_si_001.pdf"
    assert WAITING in result.stderr
    assert "Waiting" not in result.stdout
    assert gui.posts() == 1
    assert len(gui.polls()) == 3
    assert [r["file"] for r in find_paper(vault, A_ID)["supplementary"]] == ["si-01.pdf"]


def test_cancel_in_the_page_is_10(vault: Path, gui: Gui, clock: FakeTime) -> None:
    clock.after(2, gui.cancel)

    result = _run(vault, A_ID)

    assert result.exit_code == 10, result.output
    assert result.stdout == "SI request cancelled in the GUI\n"
    assert not (vault / "papers" / A_ID / "supplementary").exists()


def test_no_answer_in_90_s_is_11_and_the_rerun_rejoins_the_same_request(
    vault: Path, gui: Gui, clock: FakeTime
) -> None:
    first = _run(vault, A_ID)

    assert first.exit_code == 11, first.output
    assert first.stdout == STILL_WAITING + "\n"
    rid = gui.shown()
    assert gui.polls() == [f"/api/si/requests/{rid}"] * 90
    assert clock.now == 1090.0

    def page_acts() -> None:
        # Still the one window: the rerun joined it, it opened no other.
        assert gui.open_ids() == [rid]
        gui.done()

    gui.seen.clear()
    clock.after(4, page_acts)
    second = _run(vault, A_ID)

    assert second.exit_code == 0, second.output
    assert second.stdout.splitlines()[0] == "SI ready: 1 file(s)"
    assert gui.posts() == 1
    assert set(gui.polls()) == {f"/api/si/requests/{rid}"}
    assert gui.open_ids() == []


def test_a_done_after_11_reaches_the_next_rerun_once(
    vault: Path, gui: Gui, clock: FakeTime
) -> None:
    """Deliver-once with --force (asking for a file the user missed, say):
    the rerun gets the result at once, the run after it a fresh window."""
    first = _run(vault, A_ID, "--force")
    assert first.exit_code == 11, first.output
    rid = gui.shown()
    gui.done()

    gui.seen.clear()
    sleeps = clock.sleeps
    second = _run(vault, A_ID, "--force")

    assert second.exit_code == 0, second.output
    lines = second.stdout.splitlines()
    assert lines[0] == "SI ready: 1 file(s)"
    assert _ready(lines[1:]) == [(_si_path(vault, "si-01.pdf"), "ci9b00237_si_001.pdf")]
    assert "Waiting" not in second.stderr
    assert clock.sleeps == sleeps
    assert gui.polls() == []

    third = _run(vault, A_ID, "--force")

    assert third.exit_code == 11, third.output
    assert len(gui.open_ids()) == 1 and gui.open_ids()[0] != rid


def test_without_force_a_done_after_11_reads_as_already_in_the_library(
    vault: Path, gui: Gui
) -> None:
    """The same command rerun after the user pressed Done finds the files in
    the library before it would ask the GUI — so it never collects the GUI's
    result. A later --force run (for a file the user missed) still gets a
    window, never that old result."""
    assert _run(vault, A_ID).exit_code == 11
    rid = gui.shown()
    gui.done()
    gui.seen.clear()

    result = _run(vault, A_ID)

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "SI ready: 1 file(s) (already in the library)"
    assert gui.seen == []

    forced = _run(vault, A_ID, "--force")

    assert forced.exit_code == 11, forced.output
    assert forced.stdout == STILL_WAITING + "\n"
    (fresh,) = gui.open_ids()
    assert fresh != rid
    assert set(gui.polls()) == {f"/api/si/requests/{fresh}"}


def test_after_the_si_is_removed_the_same_command_opens_a_fresh_window(
    vault: Path, gui: Gui, clock: FakeTime
) -> None:
    """Done after an 11, the rerun reads it from the library; then the user
    removes the files and the agent asks again. A new window — never the old
    result, whose files are gone."""
    assert _run(vault, A_ID).exit_code == 11
    rid = gui.shown()
    gui.done()
    assert _run(vault, A_ID).exit_code == 0
    removed = CliRunner().invoke(
        cli, ["rm", A_ID, "--si", "si-01", "--yes", "--library", str(vault)]
    )
    assert removed.exit_code == 0, removed.output
    gui.seen.clear()
    clock.after(2, lambda: gui.done(table="Table S3"))

    result = _run(vault, A_ID)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "SI ready: 1 file(s)"
    assert len(lines) == 2
    path, original = lines[1].split("\t")
    assert Path(path).is_file()
    assert original == "ci9b00237_si_001.pdf"
    assert gui.posts() == 1
    assert len(gui.polls()) == 2
    assert f"/api/si/requests/{rid}" not in gui.polls()


def test_switching_force_never_brings_back_an_older_result(
    vault: Path, gui: Gui, clock: FakeTime
) -> None:
    """Plain run, 11, the user cancels A; a --force run opens B, 11, the user
    cancels B and the --force rerun reads it (10). A later plain run opens a
    window rather than replaying A's cancel: the agent never read A, but B
    replaced it."""
    assert _run(vault, A_ID).exit_code == 11
    a = gui.shown()
    gui.cancel()
    assert _run(vault, A_ID, "--force").exit_code == 11
    b = gui.shown()
    assert b != a
    gui.cancel()
    assert _run(vault, A_ID, "--force").exit_code == 10

    sleeps = clock.sleeps
    later = _run(vault, A_ID)

    assert later.exit_code == 11, later.output
    assert later.stdout == STILL_WAITING + "\n"
    assert clock.sleeps - sleeps == 90
    (fresh,) = gui.open_ids()
    assert fresh not in (a, b)


def test_a_cancel_after_11_reaches_the_next_rerun_once(
    vault: Path, gui: Gui, clock: FakeTime
) -> None:
    assert _run(vault, A_ID).exit_code == 11
    rid = gui.shown()
    gui.cancel()

    sleeps = clock.sleeps
    second = _run(vault, A_ID)

    assert second.exit_code == 10, second.output
    assert second.stdout == "SI request cancelled in the GUI\n"
    assert "Waiting" not in second.stderr
    assert clock.sleeps == sleeps

    third = _run(vault, A_ID)

    assert third.exit_code == 11, third.output
    assert len(gui.open_ids()) == 1 and gui.open_ids() != [rid]


def test_ctrl_c_while_waiting_is_11_and_leaves_the_request_open(
    vault: Path, gui: Gui, clock: FakeTime
) -> None:
    def interrupt() -> None:
        raise KeyboardInterrupt

    clock.after(3, interrupt)

    result = _run(vault, A_ID)

    assert result.exit_code == 11, result.output
    assert result.stdout == STILL_WAITING + "\n"
    assert "Stopped waiting; the SI window stays open in the GUI." in result.stderr
    assert len(gui.open_ids()) == 1


def test_ctrl_c_during_the_request_is_11_and_cancels_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GUI may have opened the window already: the rerun picks it up."""

    def interrupt(_request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    sent = _mock_gui(monkeypatch, vault, post=interrupt)

    result = _run(vault, A_ID)

    assert result.exit_code == 11, result.output
    assert result.stdout == STILL_WAITING + "\n"
    # No request id came back: the window may not exist, so no promise.
    assert "Stopped waiting; the SI window may be open in the GUI." in result.stderr
    assert "stays open" not in result.stderr
    assert [r.method for r in sent] == ["GET", "POST"]  # the ping, the request


def test_ctrl_c_before_the_request_stays_an_abort(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(si_gui, "find_gui", interrupt)

    result = _run(vault, A_ID)

    assert result.exit_code == 1
    assert STILL_WAITING not in result.stdout


def test_the_gui_switching_library_mid_wait_is_12(
    vault: Path, gui: Gui, clock: FakeTime, tmp_path: Path
) -> None:
    (tmp_path / "other").mkdir()
    elsewhere = create_vault(tmp_path / "other")

    def switch() -> None:
        gui.app.state.vault = elsewhere

    clock.after(2, switch)

    result = _run(vault, A_ID)

    assert result.exit_code == 12, result.output
    assert result.stdout.splitlines() == [
        NO_PAGE,
        f"The litman GUI at {GUI_URL} shows another library — switch it to "
        f"{vault.resolve()}, then run the same command again",
        A_LINK,
        A_FORM,
    ]
    assert "The request stays open in the GUI" in result.stderr
    assert len(gui.open_ids()) == 1


# ---------------------------------------------------------------------------
# The 90-second budget runs from the start of the command
# ---------------------------------------------------------------------------


def _slow_precheck(monkeypatch: pytest.MonkeyPatch, clock: FakeTime, seconds: float) -> None:
    real = si_mod.si_write_problem

    def slow(vault: Path, paper_id: str) -> str | None:
        clock.now += seconds
        return real(vault, paper_id)

    monkeypatch.setattr(si_mod, "si_write_problem", slow)


def test_time_spent_before_the_request_counts(
    vault: Path, gui: Gui, clock: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow_precheck(monkeypatch, clock, 50.0)

    result = _run(vault, A_ID)

    assert result.exit_code == 11, result.output
    assert len(gui.polls()) == 40


def test_past_the_budget_before_the_request_it_still_opens_then_11(
    vault: Path, gui: Gui, clock: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window opens anyway, so the rerun finds it; no poll is made."""
    _slow_precheck(monkeypatch, clock, 100.0)

    result = _run(vault, A_ID)

    assert result.exit_code == 11, result.output
    assert result.stdout == STILL_WAITING + "\n"
    assert gui.posts() == 1
    assert gui.polls() == []
    assert clock.sleeps == 0
    assert len(gui.open_ids()) == 1


# ---------------------------------------------------------------------------
# What the GUI may answer instead (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
def test_the_request_and_its_polls_as_sent(
    vault: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    sent = _mock_gui(
        monkeypatch,
        vault,
        post=_reply(200, _snapshot()),
        get=_sequence(_reply(200, _snapshot()), _reply(200, _snapshot("cancelled"))),
    )

    result = _run(vault, A_ID, *(["--force"] if force else []))

    assert result.exit_code == 10, result.output
    ping, post, *polls = sent
    assert ping.url.path == "/api/si/ping"
    # force: the GUI hands a missed result only to a run asking the same way.
    assert json.loads(post.content) == {
        "paperId": A_ID,
        "source": "cli",
        "vault": str(vault.resolve()),
        "force": force,
    }
    # Opening a request reads metadata in the GUI, maybe on a slow mount.
    assert post.extensions["timeout"] == {
        "connect": 3.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 10.0,
    }
    assert len(polls) == 2


def test_the_real_default_client_ignores_proxies_and_dials_127_0_0_1(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_new_client`` itself, end to end: only the transport is swapped."""
    transport, sent = _mock_transport(
        vault, post=_reply(200, _snapshot()), get=_reply(200, _snapshot("cancelled"))
    )
    built: dict[str, Any] = {}
    real_client = httpx.Client

    def client_factory(**kwargs: Any) -> httpx.Client:
        built.update(kwargs)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(si_gui, "_new_client", _REAL_NEW_CLIENT)
    monkeypatch.setattr(httpx, "Client", client_factory)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")

    result = _run(vault, A_ID)

    assert result.exit_code == 10, result.output
    assert built["trust_env"] is False
    assert built["timeout"] == httpx.Timeout(2.0, connect=3.0)
    assert [r.url.host for r in sent] == ["127.0.0.1"] * 3
    assert sent[2].extensions["timeout"] == {
        "connect": 3.0,
        "read": 2.0,
        "write": 2.0,
        "pool": 2.0,
    }


def test_an_added_answer_prints_this_librarys_paths_and_the_warnings(
    vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The GUI may spell the library's path its own way: the paths printed
    are built from this command's library."""
    _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")
    added = [{"file": "si-01.pdf", "path": "/somewhere/else/si-01.pdf"}]
    _mock_gui(
        monkeypatch,
        vault,
        post=_reply(200, _snapshot()),
        get=_reply(200, _snapshot("added", added=added, warnings=["sync is behind"])),
    )

    result = _run(vault, A_ID, "--force")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "SI ready: 1 file(s)"
    assert _ready(lines[1:]) == [(_si_path(vault, "si-01.pdf"), "ci9b00237_si_001.pdf")]
    assert "/somewhere/else" not in result.stdout
    assert "Warning: sync is behind" in result.stderr


def test_an_unknown_status_keeps_waiting(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _mock_gui(
        monkeypatch,
        vault,
        post=_reply(200, _snapshot("confirming")),
        get=_sequence(
            _reply(200, _snapshot("confirming")),
            _reply(200, _snapshot("something-new")),
            _reply(200, _snapshot("cancelled")),
        ),
    )
    result = _run(vault, A_ID)
    assert result.exit_code == 10, result.output
    assert _gets(sent) == 3


def test_post_refused_by_the_gui_as_unwritable_is_an_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paper's SI field broke between the local check and the GUI's: the
    GUI's 409 is checked again here, and reads as the error it is."""
    problems = iter([None, "papers/x/metadata.yaml: 'supplementary' is not a list."])
    monkeypatch.setattr(si_mod, "si_write_problem", lambda _vault, _paper: next(problems))
    _mock_gui(monkeypatch, vault, post=_reply(409, {"detail": "refused"}))

    result = _run(vault, A_ID)

    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert str(result.exception) == "papers/x/metadata.yaml: 'supplementary' is not a list."


@pytest.mark.parametrize("code", [409, 410])
def test_post_refused_for_another_library_is_12(
    vault: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    sent = _mock_gui(monkeypatch, vault, post=_reply(code, {"detail": "another library"}))

    result = _run(vault, A_ID)

    assert result.exit_code == 12, result.output
    assert result.stdout.splitlines() == [
        NO_PAGE,
        f"The litman GUI at {MOCK_URL} shows another library — switch it to "
        f"{vault.resolve()}, then run the same command again",
        A_LINK,
        A_FORM,
    ]
    # No request was opened, so there is none to pick up.
    assert "stays open" not in result.stderr
    assert _gets(sent) == 0


def test_post_connection_refused_is_12(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_gui(monkeypatch, vault, post=_fail(httpx.ConnectError("refused")))

    result = _run(vault, A_ID)

    assert result.exit_code == 12, result.output
    assert result.stdout.splitlines() == [
        NO_PAGE,
        f"The litman GUI at {MOCK_URL} stopped answering — start it again (lit gui), "
        "then run the same command again",
        A_LINK,
        A_FORM,
    ]


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")],
    ids=["read", "connect"],
)
def test_post_timing_out_is_11(
    vault: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _mock_gui(monkeypatch, vault, post=_fail(error))

    result = _run(vault, A_ID)

    assert result.exit_code == 11, result.output
    assert result.stdout == STILL_WAITING + "\n"
    assert "The GUI did not answer in time; the rerun picks the request up." in result.stderr


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (_reply(404, {"detail": f"No paper with id {A_ID!r}."}), f"No paper with id {A_ID!r}."),
        (
            _reply(400, {"detail": "The request body is malformed."}),
            "The request body is malformed.",
        ),
        (_reply(500, text="Internal Server Error"), "unexpected answer (HTTP 500)"),
        (_reply(200, text="<html>not json</html>"), "unexpected answer (HTTP 200)"),
        (
            _reply(200, {"request": {"id": "../../x", "status": "pending"}}),
            "unexpected answer (HTTP 200)",
        ),
    ],
    ids=["404", "400", "500", "not-json", "bad-id"],
)
def test_post_answers_that_a_rerun_cannot_fix_are_errors(
    vault: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer, detail: str
) -> None:
    sent = _mock_gui(monkeypatch, vault, post=answer)

    result = _run(vault, A_ID)

    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert str(result.exception) == f"The litman GUI could not open the SI window: {detail}"
    assert _gets(sent) == 0


@pytest.mark.parametrize("status", ["added", "cancelled"])
def test_post_answering_with_a_finished_request_does_not_wait(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeTime,
    tmp_path: Path,
    status: str,
) -> None:
    if status == "added":
        _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")
    added = [{"file": "si-01.pdf", "path": "x"}] if status == "added" else None
    _mock_gui(monkeypatch, vault, post=_reply(200, _snapshot(status, added=added)))

    result = _run(vault, A_ID, "--force")

    assert result.exit_code == (0 if status == "added" else 10), result.output
    assert "Waiting" not in result.stderr
    assert clock.sleeps == 0


@pytest.mark.parametrize(
    "answer",
    [
        _reply(200, _snapshot(vault_served=False)),
        _reply(409, {"detail": "No active vault yet"}),
        _reply(410, {"detail": "The library is no longer at …"}),
    ],
    ids=["vault-not-served", "409", "410"],
)
def test_a_poll_saying_another_library_is_shown_is_12(
    vault: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer
) -> None:
    sent = _mock_gui(monkeypatch, vault, post=_reply(200, _snapshot()), get=answer)

    result = _run(vault, A_ID)

    assert result.exit_code == 12, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == NO_PAGE
    assert lines[1].startswith(f"The litman GUI at {MOCK_URL} shows another library")
    assert "The request stays open in the GUI" in result.stderr
    assert _gets(sent) == 1


@pytest.mark.parametrize("status", ["added", "cancelled"])
def test_a_finished_result_wins_over_another_library_shown(
    vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
) -> None:
    """Done or Cancel happened, then the GUI switched library: the result
    stands — the files are in this library either way."""
    added = None
    if status == "added":
        _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")
        added = [{"file": "si-01.pdf", "path": "/the/other/library/si-01.pdf"}]
    finished = _snapshot(status, vault_served=False, added=added)
    _mock_gui(monkeypatch, vault, post=_reply(200, _snapshot()), get=_reply(200, finished))

    result = _run(vault, A_ID, "--force")

    if status == "added":
        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        assert lines[0] == "SI ready: 1 file(s)"
        assert _ready(lines[1:]) == [(_si_path(vault, "si-01.pdf"), "ci9b00237_si_001.pdf")]
    else:
        assert result.exit_code == 10, result.output
        assert result.stdout == "SI request cancelled in the GUI\n"
    assert "another library" not in result.stdout


@pytest.mark.parametrize("where", ["post", "poll"])
@pytest.mark.parametrize(
    "added",
    [[], [{"path": "/x/si-01.pdf"}], [{"file": 7}]],
    ids=["empty", "no-file", "file-not-text"],
)
def test_an_added_answer_naming_no_file_is_an_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch, where: str, added: list[dict[str, Any]]
) -> None:
    """Not "SI ready: 0 file(s)" with exit 0: the agent would read nothing."""
    finished = _reply(200, _snapshot("added", added=added))
    if where == "post":
        _mock_gui(monkeypatch, vault, post=finished)
    else:
        _mock_gui(monkeypatch, vault, post=_reply(200, _snapshot()), get=finished)

    result = _run(vault, A_ID)

    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert str(result.exception) == (
        "The litman GUI answered that SI was added but named no file. "
        f"`lit show {A_ID}` lists the paper's SI."
    )
    assert "SI ready" not in result.stdout


def test_a_poll_the_gui_no_longer_knows_is_11(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_gui(
        monkeypatch,
        vault,
        post=_reply(200, _snapshot()),
        get=_reply(404, {"detail": "unknown"}),
    )

    result = _run(vault, A_ID)

    assert result.exit_code == 11, result.output
    assert result.stdout == STILL_WAITING + "\n"
    assert "The GUI no longer knows this request (restarted?)" in result.stderr


MISS_ANSWERS = [
    _fail(httpx.ConnectError("refused")),
    _fail(httpx.ReadTimeout("slow")),
    _reply(500, text="oops"),
    _reply(200, text="not json"),
]


@pytest.mark.parametrize("miss", MISS_ANSWERS, ids=["refused", "timeout", "500", "garbage"])
def test_three_failed_polls_in_a_row_are_12(
    vault: Path, monkeypatch: pytest.MonkeyPatch, miss: Answer
) -> None:
    sent = _mock_gui(monkeypatch, vault, post=_reply(200, _snapshot()), get=miss)

    result = _run(vault, A_ID)

    assert result.exit_code == 12, result.output
    assert result.stdout.splitlines()[:2] == [
        NO_PAGE,
        f"The litman GUI at {MOCK_URL} stopped answering — start it again (lit gui), "
        "then run the same command again",
    ]
    assert _gets(sent) == si_gui.MAX_MISSES


def test_a_good_poll_between_failures_starts_the_count_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    miss = _fail(httpx.ReadTimeout("slow"))
    ok = _reply(200, _snapshot())
    sent = _mock_gui(
        monkeypatch,
        vault,
        post=ok,
        get=_sequence(miss, miss, ok, miss, miss, ok, _reply(200, _snapshot("cancelled"))),
    )

    result = _run(vault, A_ID)

    assert result.exit_code == 10, result.output
    assert _gets(sent) == 7


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_help_describes_the_third_form_and_its_exit_codes() -> None:
    result = CliRunner().invoke(cli, ["add", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "lit add --si PAPER FILE... lit add --si PAPER" in text
    assert "Asking the GUI (--si PAPER, no FILE)" in text
    for line in (
        "0 SI ready: N file(s), then one line per file: its path, a tab and its "
        "original name (the path alone when none is recorded); the first line "
        "ends in (already in the library) when the paper had SI already",
        "1 an error, such as no such paper or a broken SI record",
        "10 the SI request was cancelled in the GUI",
        "11 still waiting: run the same command again to keep waiting",
        "12 no GUI page is open for this library",
    ):
        assert line in text
    assert "never mentions" not in text
    assert "With --si alone: ask the GUI" in text


# ---------------------------------------------------------------------------
# The SI ready lines: path, a tab, the name the file was added under
# ---------------------------------------------------------------------------


def _set_original(vault: Path, line: str) -> None:
    """Rewrite the one ``original:`` line of paper A's metadata by hand."""
    meta = vault / "papers" / A_ID / "metadata.yaml"
    unlock_truth_file(meta)
    text = meta.read_text(encoding="utf-8")
    old = next(x for x in text.splitlines() if x.strip().startswith("original:"))
    meta.write_text(text.replace(old + "\n", line), encoding="utf-8")


def test_ready_lines_carry_each_original_after_a_tab(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    si_gui.print_si_ready(
        [
            (tmp_path / "si-01.pdf", {"file": "si-01.pdf", "original": "ci9b00237_si_001.pdf"}),
            (tmp_path / "si-02.xlsx", {"file": "si-02.xlsx", "original": "Table S2 (v2).xlsx"}),
        ]
    )
    assert capsys.readouterr().out.splitlines() == [
        "SI ready: 2 file(s)",
        f"{tmp_path / 'si-01.pdf'}\tci9b00237_si_001.pdf",
        f"{tmp_path / 'si-02.xlsx'}\tTable S2 (v2).xlsx",
    ]


@pytest.mark.parametrize(
    "record",
    [
        {"file": "si-01.pdf"},
        {"file": "si-01.pdf", "original": None},
        {"file": "si-01.pdf", "original": ""},
        {"file": "si-01.pdf", "original": 7},
        {"file": "si-01.pdf", "original": "\t\n\u202e"},
    ],
    ids=["missing", "null", "empty", "not-text", "only-control-characters"],
)
def test_a_record_naming_no_original_prints_the_path_alone(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, record: dict[str, Any]
) -> None:
    si_gui.print_si_ready([(tmp_path / "si-01.pdf", record)], already=True)
    assert capsys.readouterr().out.splitlines() == [
        "SI ready: 1 file(s) (already in the library)",
        str(tmp_path / "si-01.pdf"),
    ]


@pytest.mark.parametrize(
    "original",
    ["Table\tS2.xlsx", "Table\nS2.xlsx", "Table\r\nS2.xlsx", "Table\u2028S2.xlsx"],
    ids=["tab", "newline", "crlf", "line-separator"],
)
def test_an_original_with_a_tab_or_newline_stays_on_its_line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, original: str
) -> None:
    si_gui.print_si_ready([(tmp_path / "si-02.xlsx", {"original": original})])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    path, name = lines[1].split("\t")
    assert path == str(tmp_path / "si-02.xlsx")
    assert name.split() == ["Table", "S2.xlsx"]


def test_an_undecodable_original_prints_without_raising(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A lone surrogate — what a non-UTF-8 byte in a POSIX file name becomes —
    cannot be written to a UTF-8 stdout; the line carries a space instead."""
    original = b"Table\xffS2.xlsx".decode("utf-8", "surrogateescape")

    si_gui.print_si_ready([(tmp_path / "si-02.xlsx", {"original": original})])

    assert capsys.readouterr().out.splitlines() == [
        "SI ready: 1 file(s)",
        f"{tmp_path / 'si-02.xlsx'}\tTable S2.xlsx",
    ]


def test_a_hand_edited_record_without_an_original_prints_the_path_alone(
    vault: Path, gui: Gui, tmp_path: Path
) -> None:
    _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")
    _set_original(vault, "")

    result = _run(vault, A_ID)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "SI ready: 1 file(s) (already in the library)"
    assert "\t" not in lines[1]
    assert Path(lines[1]).resolve() == _si_path(vault, "si-01.pdf")


def test_a_hand_edited_original_with_a_tab_cannot_break_the_line(
    vault: Path, gui: Gui, tmp_path: Path
) -> None:
    _add_si_by_file(vault, tmp_path, "ci9b00237_si_001.pdf", "Table S1")
    _set_original(vault, '    original: "Table\\tS1\\nfinal.pdf"\n')

    result = _run(vault, A_ID)

    assert result.exit_code == 0, result.output
    assert _ready(result.stdout.splitlines()[1:]) == [
        (_si_path(vault, "si-01.pdf"), "Table S1 final.pdf")
    ]
