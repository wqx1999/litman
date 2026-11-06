"""The auto sync seen from the page, and GUI saves while a sync may be running.

* A save carries the version it started from; a file that changed meanwhile
  (a sync brought another machine's copy) still loses to the save, but goes
  to the backup first, and the answer says where.
* While rclone runs on the library, saves wait for it, then give up with a
  retryable 503; writes that touch no synced file never wait.
* ``GET /api/sync/status``, ``POST /api/sync`` and the ``sync`` that rides on
  ``GET /api/vault-version``.

No rclone is run here: the locks and the routes are enough.
"""

from __future__ import annotations

import asyncio
import contextvars
import errno
import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from litman import server as server_mod
from litman.core import sync as core_sync
from litman.core.sync import SetupPayload, write_sync_to_config
from litman.core.sync_lock import FileLock
from litman.core.vault_token import fingerprint
from litman.server import create_app
from tests.server._client import TestClient

_TARGET_PATH = "lib"


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _with_sync(vault: Path, *, synced_here: bool) -> Path:
    """Set ``sync:`` up; with ``synced_here``, as if this machine had synced once."""
    write_sync_to_config(
        vault / "lit-config.yaml", SetupPayload(remote="fake-cloud", path=_TARGET_PATH)
    )
    work = core_sync.sync_workdir(vault.resolve(), f"fake-cloud:{_TARGET_PATH}")
    work.mkdir(parents=True, exist_ok=True)
    if synced_here:
        (work / "session.path1.lst").write_text("", encoding="utf-8")
    return work


def _notes(vault: Path, paper_id: str, text: str) -> Path:
    path = vault / "papers" / paper_id / "notes.md"
    path.write_bytes(text.encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# Saves over a file that changed underneath
# ---------------------------------------------------------------------------


def test_a_save_over_a_changed_note_wins_and_keeps_the_other_copy(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, pid = vault_with_paper
    path = _notes(vault, pid, "# Notes\nmine, as the page read it\n")
    client = _client(vault)
    read = client.get(f"/api/paper/{pid}/notes").json()
    assert read["version"] == hashlib.sha256(path.read_bytes()).hexdigest()

    # A sync brings the other machine's copy: same size, mtime put back, so
    # mtime and size alone could not tell.
    st = path.stat()
    stat_before = fingerprint(path)
    path.write_bytes(
        path.read_bytes().replace(b"mine, as the page read it", b"theirs, from the sync!!!!")
    )
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert fingerprint(path) == stat_before
    theirs = path.read_bytes()

    saved = client.put(
        f"/api/paper/{pid}/notes",
        json={"text": "# Notes\nmy edit\n", "base": read["version"]},
    )

    assert saved.status_code == 200, saved.text
    body = saved.json()
    backup = Path(body["replaced"]["backup"])
    assert backup.read_bytes() == theirs
    assert backup.parts[-3:] == ("papers", pid, "notes.md")
    assert "conflicts" in backup.parts
    assert "my edit" in path.read_text(encoding="utf-8")
    assert body["version"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_save_from_the_current_version_or_without_one_keeps_no_copy(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, pid = vault_with_paper
    _notes(vault, pid, "# Notes\nfirst\n")
    client = _client(vault)
    version = client.get(f"/api/paper/{pid}/notes").json()["version"]

    chained = client.put(
        f"/api/paper/{pid}/notes", json={"text": "# Notes\nsecond\n", "base": version}
    ).json()
    assert "replaced" not in chained
    again = client.put(
        f"/api/paper/{pid}/notes", json={"text": "# Notes\nthird\n", "base": chained["version"]}
    ).json()
    assert "replaced" not in again
    # No base: the old behaviour, a plain overwrite.
    _notes(vault, pid, "# Notes\nchanged by an agent\n")
    plain = client.put(f"/api/paper/{pid}/notes", json={"text": "# Notes\nfourth\n"}).json()
    assert "replaced" not in plain


def test_a_discussion_save_is_checked_the_same_way(vault_with_paper: tuple[Path, str]) -> None:
    vault, pid = vault_with_paper
    path = vault / "papers" / pid / "discussion.md"
    path.write_text("# Discussion\nold\n", encoding="utf-8")
    client = _client(vault)
    version = client.get(f"/api/paper/{pid}/discussion").json()["version"]
    path.write_text("# Discussion\nchanged elsewhere\n", encoding="utf-8")

    body = client.put(
        f"/api/paper/{pid}/discussion", json={"text": "# Discussion\nmine\n", "base": version}
    ).json()

    assert "changed elsewhere" in Path(body["replaced"]["backup"]).read_text(encoding="utf-8")


def test_an_annotation_save_over_a_changed_pdf_keeps_the_other_copy(
    vault_with_paper: tuple[Path, str], make_text_pdf: Callable[..., Path]
) -> None:
    vault, pid = vault_with_paper
    pdf = vault / "papers" / pid / "paper.pdf"
    pdf.write_bytes(make_text_pdf([["mine"]]).read_bytes())
    client = _client(vault)
    version = client.get(f"/api/paper/{pid}/pdf-version").json()["version"]
    assert version == fingerprint(pdf)
    theirs = make_text_pdf([["theirs, annotated on the other machine"]]).read_bytes()
    pdf.write_bytes(theirs)

    saved = client.put(
        f"/api/paper/{pid}/pdf-annotations",
        params={"base": version},
        content=make_text_pdf([["mine, annotated"]]).read_bytes(),
        headers={"Content-Type": "application/pdf"},
    )

    assert saved.status_code == 200, saved.text
    assert Path(saved.json()["replaced"]["backup"]).read_bytes() == theirs
    assert saved.json()["version"] == fingerprint(pdf)


def test_the_pdf_version_of_a_paper_without_a_pdf_is_a_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, pid = vault_with_paper
    assert _client(vault).get(f"/api/paper/{pid}/pdf-version").status_code == 404


# ---------------------------------------------------------------------------
# Saves wait while rclone runs
# ---------------------------------------------------------------------------


def test_a_save_waits_for_rclone_then_gives_up_with_a_retryable_503(
    vault_with_paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, pid = vault_with_paper
    work = _with_sync(vault, synced_here=True)
    _notes(vault, pid, "# Notes\n")
    monkeypatch.setattr(server_mod, "SAVE_WAIT_S", 0.3)
    client = _client(vault)
    rclone = FileLock(work / core_sync.PASS_LOCK_NAME)
    assert rclone.try_acquire()
    try:
        refused = client.put(f"/api/paper/{pid}/notes", json={"text": "# Notes\nmine\n"})
        assert refused.status_code == 503
        assert refused.headers["Retry-After"] == "5"
        assert "not saved" in refused.json()["detail"]
        assert "mine" not in (vault / "papers" / pid / "notes.md").read_text(encoding="utf-8")
        # Writes that touch no synced file go through meanwhile.
        assert client.put("/api/ui-lang", json={"lang": "zh"}).status_code != 503
        assert client.put(f"/api/pins/{pid}").status_code != 503
    finally:
        rclone.release()
    retried = client.put(f"/api/paper/{pid}/notes", json={"text": "# Notes\nmine\n"})
    assert retried.status_code == 200
    assert "mine" in (vault / "papers" / pid / "notes.md").read_text(encoding="utf-8")


def test_saves_do_not_wait_for_each_other_and_hold_rclone_off_while_they_run(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _pid = vault_with_paper
    work = _with_sync(vault, synced_here=True)
    app = create_app(vault)
    hold = app.state.save_gate.hold_for(vault)
    assert hold is not None
    assert hold.try_enter()
    assert hold.try_enter()
    rclone = FileLock(work / core_sync.PASS_LOCK_NAME)
    assert not rclone.try_acquire()
    hold.leave()
    hold.leave()
    assert rclone.try_acquire()
    rclone.release()


def test_a_library_without_sync_has_nothing_for_saves_to_wait_on(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _pid = vault_with_paper
    assert create_app(vault).state.save_gate.hold_for(vault) is None


@pytest.mark.parametrize(
    ("method", "path", "waits"),
    [
        ("PUT", "/api/paper/x/notes", True),
        ("PUT", "/api/paper/x/metadata", True),
        ("POST", "/api/paper/x/read", True),
        ("DELETE", "/api/paper/x", True),
        ("POST", "/api/ingest/confirm", True),
        ("POST", "/api/si/requests/r1/confirm", True),
        ("DELETE", "/api/paper/x/si/s.csv", True),
        ("POST", "/api/trash/e/restore", True),
        ("PUT", "/api/taxonomy/topics", True),
        ("POST", "/api/projects", True),
        ("PUT", "/api/ui-lang", False),
        ("PUT", "/api/pins/x", False),
        ("DELETE", "/api/pins", False),
        ("PUT", "/api/vaults/active", False),
        ("PUT", "/api/vaults/v/path", False),
        ("DELETE", "/api/vaults/v", False),
        ("POST", "/api/ingest/pdf", False),
        ("DELETE", "/api/ingest/h1", False),
        ("POST", "/api/si/requests/r1/files", False),
        ("DELETE", "/api/si/requests/r1/files/f1", False),
        ("POST", "/api/si/requests/r1/cancel", False),
        ("POST", "/api/paper/x/si/s.csv/open", False),
        ("POST", "/api/agent/launch", False),
        ("POST", "/api/self-update", False),
        ("POST", "/api/sync", False),
    ],
)
def test_only_writes_that_touch_no_synced_file_skip_the_wait(
    method: str, path: str, waits: bool
) -> None:
    assert server_mod._write_not_synced(method, path) is not waits


def _api_writes() -> set[tuple[str, str]]:
    """Every ``/api/`` write route the app serves, as ``(method, path)``.

    Read from the app's OpenAPI schema, FastAPI's public account of its
    routes: walking ``app.routes`` finds none on a FastAPI that keeps each
    included router there as one nested entry. A route left out of the schema
    would be missed, so none may be.
    """
    sources = Path(server_mod.__file__).parent.glob("*.py")
    hidden = [p.name for p in sources if "include_in_schema" in p.read_text(encoding="utf-8")]
    assert hidden == [], "a route left out of the OpenAPI schema escapes this check"
    paths = create_app(None).openapi()["paths"]
    return {
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith("/api/")
        for method in operations
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }


def test_every_write_route_is_either_held_or_listed_as_not_synced() -> None:
    """A new write route waits until it is listed: the list is checked against the app."""
    writes = _api_writes()
    assert writes, "read no /api/ write route from the app: every check below would pass"
    synced_writes = {
        (method, path)
        for method, path in writes
        if not server_mod._write_not_synced(method, path.replace("{", "").replace("}", ""))
    }
    assert ("PUT", "/api/paper/{paper_id}/notes") in synced_writes
    assert ("PUT", "/api/paper/{paper_id}/pdf-annotations") in synced_writes
    assert ("POST", "/api/ingest/confirm") in synced_writes
    assert ("POST", "/api/sync") not in synced_writes
    # Each write listed as touching no synced file is one the app serves.
    assert server_mod._WRITES_NOT_SYNCED - writes == set()


# ---------------------------------------------------------------------------
# The status the page reads, and "sync now"
# ---------------------------------------------------------------------------


def test_a_library_without_sync_shows_no_mark(vault_with_paper: tuple[Path, str]) -> None:
    vault, _pid = vault_with_paper
    client = _client(vault)
    assert client.get("/api/vault-version").json()["sync"] is None
    assert client.get("/api/sync/status").json()["state"] == "not-configured"
    refused = client.post("/api/sync")
    assert refused.status_code == 409
    assert "lit sync setup" in refused.json()["detail"]


def test_a_library_never_synced_here_points_to_lit_sync(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _pid = vault_with_paper
    _with_sync(vault, synced_here=False)
    client = _client(vault)
    status = client.get("/api/vault-version").json()["sync"]
    assert status["state"] == "not-initialised"
    assert status["remote"] == f"fake-cloud:{_TARGET_PATH}"
    refused = client.post("/api/sync")
    assert refused.status_code == 409
    assert "Run `lit sync` once" in refused.json()["detail"]


def test_sync_now_is_queued_for_the_auto_sync(vault_with_paper: tuple[Path, str]) -> None:
    vault, _pid = vault_with_paper
    _with_sync(vault, synced_here=True)
    app = create_app(vault)
    client = TestClient(app)

    queued = client.post("/api/sync")

    assert queued.status_code == 202
    assert queued.json()["queued"] is True
    status = client.get("/api/sync/status").json()
    assert status["remote"] == f"fake-cloud:{_TARGET_PATH}"
    assert set(status) >= {
        "state", "remote", "lastOkAt", "nextTryAt", "runningSince",
        "reason", "message", "seq", "notice",
    }


def test_a_test_app_never_starts_the_auto_sync(vault_with_paper: tuple[Path, str]) -> None:
    vault, _pid = vault_with_paper
    _with_sync(vault, synced_here=True)
    app = create_app(vault)
    with TestClient(app):
        assert not app.state.sync_scheduler.started


# ---------------------------------------------------------------------------
# A first note drafted while a sync brought one; a save made twice; the order
# of saves that waited; a file system without locks
# ---------------------------------------------------------------------------


def test_a_first_note_drafted_while_a_sync_brought_one_keeps_that_one(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, pid = vault_with_paper
    path = vault / "papers" / pid / "notes.md"
    path.unlink(missing_ok=True)
    client = _client(vault)
    assert client.get(f"/api/paper/{pid}/notes").status_code == 404
    # While the page drafts a first note, a sync brings the other machine's.
    path.write_text("# Notes\nfrom the other machine\n", encoding="utf-8")

    body = client.put(
        f"/api/paper/{pid}/notes", json={"text": "# Notes\nmy first note\n", "base": "absent"}
    ).json()

    backup = Path(body["replaced"]["backup"])
    assert "from the other machine" in backup.read_text(encoding="utf-8")
    assert "my first note" in path.read_text(encoding="utf-8")
    # The reverse: still no file when the save comes, so nothing to keep.
    path.unlink()
    again = client.put(
        f"/api/paper/{pid}/notes", json={"text": "# Notes\nmy first note\n", "base": "absent"}
    ).json()
    assert "replaced" not in again


def test_a_save_made_twice_keeps_no_copy(vault_with_paper: tuple[Path, str]) -> None:
    """A request sent again (its answer lost, a retry) still carries the
    version the edit began from; the file already holds what it saves."""
    vault, pid = vault_with_paper
    _notes(vault, pid, "# Notes\nfirst\n")
    client = _client(vault)
    start = client.get(f"/api/paper/{pid}/notes").json()["version"]
    url = f"/api/paper/{pid}/notes"

    first = client.put(url, json={"text": "# Notes\nmine\n", "base": start}).json()
    again = client.put(url, json={"text": "# Notes\nmine\n", "base": start}).json()

    assert "replaced" not in first
    assert "replaced" not in again
    # The reverse: other text from that old start replaces what was saved.
    other = client.put(url, json={"text": "# Notes\nsomething else\n", "base": start}).json()
    assert "mine" in Path(other["replaced"]["backup"]).read_text(encoding="utf-8")


def test_saves_that_wait_are_let_in_in_the_order_they_came(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two waiting changes to one thing (a status set to "reading", then to
    "read") must land in the order they were made. rclone lets go right after
    the first save's look: were both looking, the second would get in first."""
    from litman.server.sync_scheduler import SaveGate

    who: contextvars.ContextVar[str] = contextvars.ContextVar("who")
    looks: list[str] = []
    order: list[str] = []

    class _Rclone:
        free = False

        def try_enter(self) -> bool:
            name = who.get()
            looks.append(name)
            if self.free:
                order.append(name)
                return True
            if looks.count("first") == 2:
                self.free = True
            return False

    async def on_the_loop(func: Callable[..., object], *args: object) -> object:
        return func(*args)  # the look made here, so it sees whose it is

    monkeypatch.setattr(server_mod, "run_in_threadpool", on_the_loop)
    monkeypatch.setattr(server_mod, "SAVE_POLL_S", 0.05)

    async def two_saves() -> list[bool | None]:
        gate, hold = SaveGate(), _Rclone()
        loop = asyncio.get_running_loop()

        async def save(name: str, after: float) -> bool | None:
            await asyncio.sleep(after)
            who.set(name)
            return await server_mod._admit(gate, hold, loop.time() + 10)  # type: ignore[arg-type]

        return list(await asyncio.gather(save("first", 0), save("second", 0.01)))

    assert asyncio.run(two_saves()) == [True, True]
    assert order == ["first", "second"]
    # The second waited its turn instead of looking.
    assert looks == ["first", "first", "first", "second"]


def test_a_file_system_without_locks_does_not_hold_saves_up(
    vault_with_paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No lock can be taken there, so no sync runs from this machine either."""
    from litman.core import sync_lock

    vault, pid = vault_with_paper
    _with_sync(vault, synced_here=True)
    _notes(vault, pid, "# Notes\n")

    def refuse(fd: int) -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(sync_lock, "_os_lock", refuse)

    saved = _client(vault).put(f"/api/paper/{pid}/notes", json={"text": "# Notes\nmine\n"})

    assert saved.status_code == 200, saved.text
    assert "mine" in (vault / "papers" / pid / "notes.md").read_text(encoding="utf-8")
