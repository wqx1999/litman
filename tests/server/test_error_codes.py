"""Each failure the page can name answers with its code and params beside the full text.

The page picks its short line by ``code`` and fills it from ``params``; it
never renders ``detail``, which stays the CLI's full text (the log keeps it).
One row per code family from the audit: the real route, the real refusal.
A second test runs the same rows and fails on any param that is an absolute
path, which the page could show and must not.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from litman.core import agents, viewer
from litman.core.library import create_vault
from litman.core.project_link import add_project
from litman.core.supplementary import SiItem, add_supplementary
from litman.importers import crossref as crossref_mod
from litman.server import create_app
from litman.server import routes_ingest as ri
from tests.conftest import _build_text_pdf
from tests.server._client import TestClient

HEX = "0" * 32


@dataclass
class Env:
    vault: Path
    paper: str
    tmp: Path
    monkeypatch: pytest.MonkeyPatch

    @property
    def client(self) -> TestClient:
        return TestClient(create_app(self.vault))


@dataclass
class Outcome:
    status: int
    code: str | None
    params: dict[str, str]
    detail: str


def _error(resp: httpx.Response) -> Outcome:
    body = resp.json()
    return Outcome(resp.status_code, body["code"], body["params"], body["detail"])


# --- set-ups --------------------------------------------------------------


def _second_paper(env: Env, paper_id: str = "2023_Bar_Baz", doi: str = "10.1/y") -> str:
    folder = env.vault / "papers" / paper_id
    folder.mkdir(parents=True)
    (folder / "metadata.yaml").write_text(
        f"id: {paper_id}\ntitle: Bar Baz\nauthors:\n  - Bar, Bob\nyear: 2023\n"
        f"doi: {doi}\nstatus: inbox\n",
        encoding="utf-8",
    )
    return paper_id


def _project_with_missing_folder(env: Env) -> None:
    folder = env.tmp / "thesis"
    folder.mkdir()
    add_project(env.vault, "thesis", folder)
    folder.rmdir()


def _register(env: Env, name: str, path: Path) -> None:
    resp = env.client.post("/api/vaults", json={"name": name, "path": str(path)})
    assert resp.status_code == 200, resp.text


def _upload(env: Env) -> str:
    resp = env.client.post(
        "/api/ingest/pdf",
        content=_build_text_pdf([["A title on page one"]]),
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 200, resp.text
    handle: str = resp.json()["handle"]
    return handle


def _confirm_manual(env: Env, **extra: Any) -> httpx.Response:
    handle = _upload(env)
    metadata = {"title": "Foo Bar", "authors": ["Foo, Alice"], "year": 2024}
    metadata.update(extra.pop("metadata", {}))
    return env.client.post(
        "/api/ingest/confirm", json={"handle": handle, "metadata": metadata, **extra}
    )


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def json(self) -> dict[str, Any]:
        return {}


def _crossref_answers(env: Env, outcome: int | Exception) -> None:
    """CrossRef, as fetch_crossref meets it: an HTTP status, or a failure."""

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResponse(outcome)

        def close(self) -> None:
            pass

    env.monkeypatch.setattr(
        crossref_mod, "httpx", SimpleNamespace(Client=Client, HTTPError=httpx.HTTPError)
    )


def _si_recorded(env: Env) -> str:
    source = env.tmp / "table.xlsx"
    source.write_bytes(b"PK\x03\x04 sheet")
    result = add_supplementary(
        env.vault, env.paper, [SiItem(source, "table.xlsx", approved=True)],
        allow_unverified=True,
    )
    name: str = result.records[0]["file"]
    return name


# --- the rows -------------------------------------------------------------


def _paper_not_found(env: Env) -> Outcome:
    return _error(env.client.get("/api/paper/2099_Nope_X"))


def _metadata_unreadable(env: Env) -> Outcome:
    (env.vault / "papers" / env.paper / "metadata.yaml").write_text(
        "title: [unclosed\n", encoding="utf-8"
    )
    return _error(env.client.get(f"/api/paper/{env.paper}"))


def _doi_taken(env: Env) -> Outcome:
    _second_paper(env)
    return _error(
        env.client.put(f"/api/paper/{env.paper}/metadata", json={"set": {"doi": "10.1/y"}})
    )


def _duplicate_author(env: Env) -> Outcome:
    return _error(
        env.client.put(
            f"/api/paper/{env.paper}/metadata",
            json={"setList": {"authors": ["Foo, Alice", "Foo, Alice"]}},
        )
    )


def _authors_empty(env: Env) -> Outcome:
    return _error(
        env.client.put(f"/api/paper/{env.paper}/metadata", json={"setList": {"authors": []}})
    )


def _project_path_missing(env: Env) -> Outcome:
    _project_with_missing_folder(env)
    return _error(env.client.post(f"/api/paper/{env.paper}/project", json={"project": "thesis"}))


def _link_unknown_project(env: Env) -> Outcome:
    return _error(env.client.post(f"/api/paper/{env.paper}/project", json={"project": "nowhere"}))


def _project_exists(env: Env) -> Outcome:
    folder = env.tmp / "thesis"
    folder.mkdir()
    add_project(env.vault, "thesis", folder)
    return _error(env.client.post("/api/projects", json={"name": "thesis", "path": str(folder)}))


def _project_path_not_absolute(env: Env) -> Outcome:
    return _error(env.client.post("/api/projects", json={"name": "x", "path": "rel/dir"}))


def _project_path_missing_on_add(env: Env) -> Outcome:
    return _error(
        env.client.post("/api/projects", json={"name": "x", "path": str(env.tmp / "nope")})
    )


def _project_path_not_a_folder(env: Env) -> Outcome:
    file = env.tmp / "a-file"
    file.write_text("", encoding="utf-8")
    return _error(env.client.post("/api/projects", json={"name": "x", "path": str(file)}))


def _project_gone_on_delete(env: Env) -> Outcome:
    return _error(env.client.delete("/api/projects/nope"))


def _trash_entry_missing(env: Env) -> Outcome:
    return _error(env.client.post("/api/trash/2024_Foo_Bar-20260101T000000Z/restore"))


def _restore_target_exists(env: Env) -> Outcome:
    client = env.client
    assert client.delete(f"/api/paper/{env.paper}").status_code == 200
    entry = client.get("/api/trash").json()[0]["entry_name"]
    _second_paper(env, env.paper, "10.1/z")  # a new paper under the same id
    return _error(client.post(f"/api/trash/{entry}/restore"))


def _si_missing(env: Env) -> Outcome:
    return _error(env.client.delete(f"/api/paper/{env.paper}/si/si-01.pdf"))


def _si_cannot_open(env: Env) -> Outcome:
    name = _si_recorded(env)
    env.monkeypatch.setattr(viewer, "open_files_mode", lambda: "none")
    return _error(env.client.post(f"/api/paper/{env.paper}/si/{name}/open"))


def _si_request_gone(env: Env) -> Outcome:
    return _error(env.client.get(f"/api/si/requests/{HEX}"))


def _si_upload_failed(env: Env) -> Outcome:
    return _error(
        env.client.post(
            f"/api/si/requests/{HEX}/files", content=b"x", headers={"X-Litman-Filename": "%2F"}
        )
    )


def _si_add_failed(env: Env) -> Outcome:
    client = env.client
    rid = client.post(
        "/api/si/requests", json={"paperId": env.paper, "source": "page"}
    ).json()["request"]["id"]
    return _error(client.post(f"/api/si/requests/{rid}/confirm", json={}))


def _too_large(env: Env) -> Outcome:
    env.monkeypatch.setattr(ri, "_MAX_UPLOAD_BYTES", 10)
    return _error(env.client.post("/api/ingest/pdf", content=b"%PDF-" + b"x" * 64))


def _not_a_pdf(env: Env) -> Outcome:
    return _error(env.client.post("/api/ingest/pdf", content=b"hello, not a pdf"))


def _upload_expired(env: Env) -> Outcome:
    return _error(env.client.post("/api/ingest/confirm", json={"handle": HEX, "doi": "10.1/q"}))


def _id_invalid(env: Env) -> Outcome:
    return _error(
        env.client.post(
            "/api/ingest/confirm", json={"handle": HEX, "doi": "10.1/q", "id": "../evil"}
        )
    )


def _id_taken(env: Env) -> Outcome:
    return _error(_confirm_manual(env, id=env.paper))


def _doi_taken_on_add(env: Env) -> Outcome:
    return _error(_confirm_manual(env, metadata={"doi": "10.1/x"}))


def _crossref_unreachable(env: Env) -> Outcome:
    _crossref_answers(env, httpx.ConnectError("Temporary failure in name resolution"))
    return _error(env.client.get("/api/ingest/preview", params={"doi": "10.1/q"}))


def _doi_not_found(env: Env) -> Outcome:
    _crossref_answers(env, 404)
    return _error(env.client.get("/api/ingest/preview", params={"doi": "10.1/q"}))


def _new_vault(env: Env, name: str) -> Path:
    (env.tmp / name).mkdir()
    return create_vault(env.tmp / name)


def _vault_name_taken(env: Env) -> Outcome:
    other = _new_vault(env, "other")
    _register(env, "main", env.vault)
    return _error(env.client.post("/api/vaults", json={"name": "main", "path": str(other)}))


def _vault_bad_name(env: Env) -> Outcome:
    return _error(env.client.post("/api/vaults", json={"name": "a b", "path": str(env.vault)}))


def _vault_path_missing(env: Env) -> Outcome:
    return _error(
        env.client.post("/api/vaults", json={"name": "x", "path": str(env.tmp / "nope")})
    )


def _vault_not_a_vault(env: Env) -> Outcome:
    plain = env.tmp / "plain"
    plain.mkdir()
    return _error(env.client.post("/api/vaults", json={"name": "x", "path": str(plain)}))


def _vault_already_registered(env: Env) -> Outcome:
    _register(env, "main", env.vault)
    return _error(env.client.post("/api/vaults", json={"name": "other", "path": str(env.vault)}))


def _vault_missing_on_switch(env: Env) -> Outcome:
    gone = _new_vault(env, "gone")
    _register(env, "gone", gone)
    (gone / "lit-config.yaml").unlink()
    return _error(env.client.put("/api/vaults/active", json={"name": "gone"}))


def _vault_parent_missing(env: Env) -> Outcome:
    return _error(
        env.client.post(
            "/api/vaults/create", json={"parent_dir": str(env.tmp / "nope"), "name": "x"}
        )
    )


def _vault_target_exists(env: Env) -> Outcome:
    (env.tmp / "p" / "x").mkdir(parents=True)
    (env.tmp / "p" / "x" / "mine.txt").write_text("", encoding="utf-8")
    return _error(
        env.client.post("/api/vaults/create", json={"parent_dir": str(env.tmp / "p"), "name": "x"})
    )


def _vault_in_use(env: Env) -> Outcome:
    _register(env, "main", env.vault)
    return _error(env.client.delete("/api/vaults/main"))


def _fs_folder_missing(env: Env) -> Outcome:
    return _error(env.client.get("/api/fs/list", params={"path": str(env.tmp / "nope")}))


def _fs_not_a_folder(env: Env) -> Outcome:
    file = env.tmp / "a-file"
    file.write_text("", encoding="utf-8")
    return _error(env.client.get("/api/fs/list", params={"path": str(file)}))


def _mkdir(name: str) -> Callable[[Env], Outcome]:
    def run(env: Env) -> Outcome:
        return _error(env.client.post("/api/fs/mkdir", json={"parent": str(env.tmp), "name": name}))

    return run


def _mkdir_file_exists(env: Env) -> Outcome:
    (env.tmp / "taken").write_text("", encoding="utf-8")
    return _mkdir("taken")(env)


def _update_refused(env: Env) -> Outcome:
    env.monkeypatch.setattr(
        "litman.commands.self_update._is_editable_install", lambda: True
    )
    return _error(env.client.post("/api/self-update"))


def _no_vault(env: Env) -> Outcome:
    return _error(TestClient(create_app(None)).get("/api/papers"))


def _vault_gone(env: Env) -> Outcome:
    client = env.client
    (env.vault / "lit-config.yaml").unlink()
    return _error(client.get("/api/papers"))


def _agent_not_installed(env: Env) -> Outcome:
    env.monkeypatch.setattr(agents, "detect", lambda _spec: False)
    return _error(env.client.post("/api/agent/launch", json={"agent": "claude"}))


# (name, run, status, code, params, a fragment of the CLI's own text)
ROWS: list[tuple[str, Callable[[Env], Outcome], int, str, dict[str, str], str]] = [
    ("paper-not-found", _paper_not_found, 404, "paper-not-found",
     {"id": "2099_Nope_X"}, "Run `lit list`"),
    ("file-unreadable", _metadata_unreadable, 500, "file-unreadable",
     {"path": "papers/2024_Foo_Bar/metadata.yaml"}, "Cannot read YAML file"),
    ("doi-taken", _doi_taken, 400, "doi-taken", {"id": "2023_Bar_Baz"},
     "already belongs to another paper"),
    ("duplicate-author", _duplicate_author, 400, "duplicate-author",
     {"name": "Foo, Alice"}, "twice"),
    ("authors-empty", _authors_empty, 400, "authors-empty", {}, "cannot be emptied"),
    ("project-path-missing", _project_path_missing, 400, "project-path-missing",
     {"project": "thesis"}, "does not exist on this machine"),
    ("project-gone (link)", _link_unknown_project, 400, "project-gone",
     {"name": "nowhere"}, "lit project add"),
    ("project-exists", _project_exists, 400, "project-exists", {"name": "thesis"},
     "already registered"),
    ("path-not-absolute", _project_path_not_absolute, 400, "path-not-absolute", {},
     "is not absolute"),
    ("path-missing (project)", _project_path_missing_on_add, 400, "path-missing", {},
     "does not exist"),
    ("not-a-folder (project)", _project_path_not_a_folder, 400, "not-a-folder", {},
     "is not a directory"),
    ("project-gone (delete)", _project_gone_on_delete, 400, "project-gone",
     {"name": "nope"}, "lit project list"),
    ("trash-entry-missing", _trash_entry_missing, 404, "trash-entry-missing", {},
     "Trash is empty"),
    ("restore-target-exists", _restore_target_exists, 409, "restore-target-exists",
     {"id": "2024_Foo_Bar"}, "already exists"),
    ("si-missing", _si_missing, 404, "si-missing", {"file": "si-01.pdf"}, "lists them"),
    ("cannot-open", _si_cannot_open, 409, "cannot-open", {"file": "si-01.xlsx"},
     "download it instead"),
    ("si-request-gone", _si_request_gone, 404, "si-request-gone", {}, "has expired"),
    ("si-upload-failed", _si_upload_failed, 400, "si-upload-failed", {},
     "can't be used"),
    ("si-add-failed", _si_add_failed, 400, "si-add-failed", {}, "Nothing to add yet"),
    ("too-large", _too_large, 413, "too-large", {}, "100 MB"),
    ("not-a-pdf", _not_a_pdf, 400, "not-a-pdf", {}, "does not look like a PDF"),
    ("upload-expired", _upload_expired, 400, "upload-expired", {}, "drop the PDF again"),
    ("id-invalid", _id_invalid, 400, "id-invalid", {}, "not a usable paper id"),
    ("id-taken", _id_taken, 409, "id-taken", {"id": "2024_Foo_Bar"},
     "Paper folder already exists"),
    ("doi-taken (add)", _doi_taken_on_add, 409, "doi-taken", {"id": "2024_Foo_Bar"},
     "already registered in this vault"),
    ("crossref-unreachable", _crossref_unreachable, 400, "crossref-unreachable", {},
     "CrossRef request failed"),
    ("doi-not-found", _doi_not_found, 400, "doi-not-found", {}, "DOI not found in CrossRef"),
    ("name-taken", _vault_name_taken, 400, "name-taken", {"name": "main"},
     "already registered"),
    ("bad-name", _vault_bad_name, 400, "bad-name", {}, "Invalid vault name"),
    ("path-missing (library)", _vault_path_missing, 400, "path-missing", {},
     "is not an existing directory"),
    ("not-a-vault", _vault_not_a_vault, 400, "not-a-vault", {}, "has no lit-config.yaml"),
    ("already-registered", _vault_already_registered, 400, "already-registered",
     {"other": "main"}, "already registered as"),
    ("vault-missing", _vault_missing_on_switch, 400, "vault-missing", {"name": "gone"},
     "Not switching"),
    ("parent-missing", _vault_parent_missing, 400, "parent-missing", {},
     "Parent directory does not exist"),
    ("target-exists", _vault_target_exists, 400, "target-exists", {},
     "already exists and is not empty"),
    ("vault-in-use", _vault_in_use, 409, "vault-in-use", {"name": "main"},
     "currently serving"),
    ("folder-missing", _fs_folder_missing, 400, "folder-missing", {}, "No such folder"),
    ("not-a-folder (picker)", _fs_not_a_folder, 400, "not-a-folder", {}, "Not a folder"),
    ("name-empty", _mkdir("  "), 400, "name-empty", {}, "can't be empty"),
    ("name-dots", _mkdir(".."), 400, "name-dots", {}, "'.' or '..'"),
    ("name-separator", _mkdir("a/b"), 400, "name-separator", {}, "path separators"),
    ("name-control", _mkdir("a\tb"), 400, "name-control", {}, "control characters"),
    ("file-exists", _mkdir_file_exists, 400, "file-exists", {}, "already exists"),
    ("update-refused", _update_refused, 409, "update-refused", {}, "editable"),
    ("no-vault", _no_vault, 409, "no-vault", {}, "No active vault yet"),
    ("vault-gone", _vault_gone, 410, "vault-gone", {}, "no longer at"),
    ("agent-not-installed", _agent_not_installed, 409, "agent-not-installed",
     {"agent": "Claude Code"}, "installed"),
]


@pytest.fixture
def env(
    vault_with_paper: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Env:
    vault, paper = vault_with_paper
    work = tmp_path / "work"
    work.mkdir()
    return Env(vault, paper, work, monkeypatch)


@pytest.mark.parametrize(
    ("run", "status", "code", "params", "fragment"),
    [pytest.param(*row[1:], id=row[0]) for row in ROWS],
)
def test_the_answer_names_the_failure_and_keeps_the_full_text(
    env: Env,
    run: Callable[[Env], Outcome],
    status: int,
    code: str,
    params: dict[str, str],
    fragment: str,
) -> None:
    outcome = run(env)

    assert (outcome.status, outcome.code, outcome.params) == (status, code, params), (
        outcome.detail
    )
    assert fragment in outcome.detail


_ABSOLUTE = re.compile(r"^(/|\\\\|[A-Za-z]:[\\/])")


@pytest.mark.parametrize("run", [pytest.param(row[1], id=row[0]) for row in ROWS])
def test_no_param_is_an_absolute_path(env: Env, run: Callable[[Env], Outcome]) -> None:
    outcome = run(env)

    for key, value in outcome.params.items():
        assert not _ABSOLUTE.match(value), f"{key}={value!r} is an absolute path"


def test_the_absolute_path_check_bites() -> None:
    """The control for the check above: it does catch each spelling."""
    for value in ("/home/u/thesis", "C:\\Users\\u", "D:/lib", "\\\\server\\share"):
        assert _ABSOLUTE.match(value), value
    for value in ("papers/x/metadata.yaml", "thesis", "si-01.pdf", "2024_Foo_Bar"):
        assert not _ABSOLUTE.match(value), value


# ---------------------------------------------------------------------------
# Codes inside answers that succeed
# ---------------------------------------------------------------------------


def test_citation_caveats_carry_their_codes(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper = vault_with_paper

    body = TestClient(create_app(vault)).get(f"/api/paper/{paper}/cite").json()

    codes = [caveat["code"] for caveat in body["caveats"]]
    assert len(codes) == len(body["warnings"])
    # "Test J." is not in the abbreviation table; volume and pages are unset.
    assert codes == ["cite-abbrev-unknown", "cite-no-volume", "cite-no-pages"]


def test_an_id_that_cannot_be_derived_says_why_by_code(
    vault_with_paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _ = vault_with_paper
    monkeypatch.setattr(
        ri, "fetch_crossref",
        lambda doi, client=None: {"title": ["A title"], "author": [{"family": "Chen"}]},
    )
    client = TestClient(create_app(vault))

    preview = client.get("/api/ingest/preview", params={"doi": "10.1/q"}).json()
    derived = client.post(
        "/api/ingest/derive-id",
        json={"title": "A title", "authors": ["张三"], "year": 2020},
    ).json()

    assert (preview["proposedId"], preview["idErrorCode"]) == (None, "id-no-year")
    assert (derived["id"], derived["errorCode"]) == (None, "id-no-author")
    # The reverse: a record that names a paper has no code.
    ok = client.post(
        "/api/ingest/derive-id", json={"title": "A title", "authors": ["Chen, Yi"], "year": 2020}
    ).json()
    assert ok["id"] and ok["errorCode"] is None
