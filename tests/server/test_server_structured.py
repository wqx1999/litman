"""Structured-write endpoint tests for the litman webUI server (A4).

Covers the invariant #16 SECOND-class writes — the cockpit's structured
metadata changes that go through the ``lit`` command backends, never a second
write path:

* ``PUT  /api/paper/{id}/metadata`` → ``_apply_modify`` (set / addTag / rmTag)
* ``POST /api/paper/{id}/read``     → ``apply_read`` (idempotent first-read)
* ``POST /api/paper/{id}/revisit``  → ``apply_revisit`` (presupposes a read)
* ``GET  /api/fixed-enums``         → status/type whitelists

The A4 assertion is "the backend actually ran": after a write we read both
``metadata.yaml`` (TRUTH) AND ``INDEX.json`` (DERIVED) and assert the index was
reprojected to match — proof the structured write went through the backend's
atomic validate + write + derive, with no drift.

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

pytest.importorskip("fastapi")

from datetime import UTC

from fastapi.testclient import TestClient

from litman.cli import cli
from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    find_active,
    load_registry,
    save_registry,
)
from litman.server import create_app
from litman.core.portable_link import is_portable_link
from litman.core import locking

_yaml = YAML(typ="safe")


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _meta(vault: Path, paper_id: str) -> dict:
    return _yaml.load((vault / "papers" / paper_id / "metadata.yaml").read_text())


def _index_paper(vault: Path, paper_id: str) -> dict:
    payload = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    for p in payload["papers"]:
        if p["id"] == paper_id:
            return p
    raise AssertionError(f"{paper_id} not in INDEX.json")


def _register_topic(vault: Path, value: str) -> None:
    """Register a topics value through the real CLI so --add-tag can use it.

    TAXONOMY.md is seeded empty + read-only locked (M32); the only legitimate
    way to add a value is `lit taxonomy add` (invariant #2), which is what the
    GUI's 3c inline-create will eventually call too.
    """
    result = CliRunner().invoke(
        cli, ["taxonomy", "add", "topics", value, "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# PUT /metadata — set (status/type dropdowns)
# ---------------------------------------------------------------------------


def test_put_metadata_set_status_writes_backend_and_reprojects_index(
    vault_with_paper: tuple[Path, str],
) -> None:
    """A4 core: a status set writes metadata.yaml AND the backend reprojects
    INDEX.json to match (the derived artifact is recomputed, no drift)."""
    vault, paper_id = vault_with_paper
    assert _meta(vault, paper_id)["status"] == "inbox"  # fixture default

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"status": "deep-read"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": True}

    # TRUTH updated …
    assert _meta(vault, paper_id)["status"] == "deep-read"
    # … and the DERIVED projection was recomputed by the backend to match.
    assert _index_paper(vault, paper_id)["status"] == "deep-read"


def test_put_metadata_unset_type(vault_with_paper: tuple[Path, str]) -> None:
    """An empty value unsets an optional fixed enum to null (type)."""
    vault, paper_id = vault_with_paper
    assert _meta(vault, paper_id)["type"] == "research"  # fixture default

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"type": ""}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": True}
    assert _meta(vault, paper_id)["type"] is None
    assert _index_paper(vault, paper_id)["type"] is None


def test_put_metadata_set_same_value_is_noop(
    vault_with_paper: tuple[Path, str],
) -> None:
    """skip_set_noop=True: re-selecting the current value does not bump
    updated-at (changed: False)."""
    vault, paper_id = vault_with_paper
    before_updated = _meta(vault, paper_id)["updated-at"]

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"status": "inbox"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": False}
    assert _meta(vault, paper_id)["updated-at"] == before_updated


def test_put_metadata_invalid_enum_400_with_backend_message(
    vault_with_paper: tuple[Path, str],
) -> None:
    """An out-of-range enum value is rejected by _apply_modify (not bypassed):
    400 carrying the backend's raw message; metadata untouched."""
    vault, paper_id = vault_with_paper
    before = _meta(vault, paper_id)["status"]

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"status": "bogus"}}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Invalid status" in detail
    assert "'bogus'" in detail
    # Nothing was written.
    assert _meta(vault, paper_id)["status"] == before


def test_put_metadata_unset_required_status_400(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Unsetting a REQUIRED fixed enum (status) is rejected — an empty value may
    only clear the optional enums (type). The backend's required-field
    guard is enforced through the endpoint, not bypassed; status untouched."""
    vault, paper_id = vault_with_paper
    before = _meta(vault, paper_id)["status"]

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"status": ""}}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Cannot unset" in detail
    assert "required field" in detail
    # A required field can never be silently blanked.
    assert _meta(vault, paper_id)["status"] == before


def test_put_metadata_set_nonscalar_value_400(
    vault_with_paper: tuple[Path, str],
) -> None:
    """A list / object `set` value is rejected at the boundary (400) rather than
    written as its Python repr — symmetric with the addTag/rmTag value check.
    Guards the generic endpoint even though the cockpit only sends scalars."""
    vault, paper_id = vault_with_paper
    before = _meta(vault, paper_id)["year"]

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"year": [1, 2]}}
    )
    assert resp.status_code == 400
    assert "scalar" in resp.json()["detail"]
    # Nothing coerced-to-repr was written.
    assert _meta(vault, paper_id)["year"] == before


# ---------------------------------------------------------------------------
# PUT /metadata — addTag / rmTag (topics/methods/data chips)
# ---------------------------------------------------------------------------


def test_put_metadata_add_then_remove_topic_roundtrip(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    _register_topic(vault, "peptide")

    client = _client(vault)
    add = client.put(
        f"/api/paper/{paper_id}/metadata", json={"addTag": {"topics": ["peptide"]}}
    )
    assert add.status_code == 200
    assert add.json() == {"ok": True, "changed": True}
    assert _meta(vault, paper_id)["topics"] == ["peptide"]
    assert _index_paper(vault, paper_id)["topics"] == ["peptide"]

    rm = client.put(
        f"/api/paper/{paper_id}/metadata", json={"rmTag": {"topics": ["peptide"]}}
    )
    assert rm.status_code == 200
    assert rm.json() == {"ok": True, "changed": True}
    assert _meta(vault, paper_id)["topics"] == []
    assert _index_paper(vault, paper_id)["topics"] == []


def test_put_metadata_add_unregistered_topic_400(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Register-first (invariant #2): an unregistered tag value is rejected by
    the backend with a hint, not silently written (3b only attaches existing
    values; inline-create is 3c)."""
    vault, paper_id = vault_with_paper

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata",
        json={"addTag": {"topics": ["not-registered"]}},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "not registered" in detail
    assert _meta(vault, paper_id)["topics"] == []


@pytest.mark.parametrize("flag", ["addTag", "rmTag"])
def test_put_metadata_rejects_projects_tag_400(
    vault_with_paper: tuple[Path, str], flag: str
) -> None:
    """The generic /metadata endpoint must NOT write `projects` — that would
    append the metadata list entry without the litman_reflib symlink +
    REFERENCES.md (a half-linked drift). Projects route through the dedicated
    link/unlink endpoints; the generic write rejects the key (400), nothing
    written."""
    vault, paper_id = vault_with_paper
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={flag: {"projects": ["whatever"]}}
    )
    assert resp.status_code == 400
    assert "projects" in resp.json()["detail"]
    assert _meta(vault, paper_id)["projects"] == []


def test_put_metadata_empty_body_400(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).put(f"/api/paper/{paper_id}/metadata", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT /metadata — setList (ordered author rewrite, task-gui-metadata-edit)
# ---------------------------------------------------------------------------


def test_put_metadata_setlist_reorders_authors_and_reprojects_index(
    vault_with_paper: tuple[Path, str],
) -> None:
    """The A4 assertion for the ordered rewrite: TRUTH and DERIVED both hold
    the new order after one request."""
    vault, paper_id = vault_with_paper
    client = _client(vault)
    resp = client.put(
        f"/api/paper/{paper_id}/metadata",
        json={"setList": {"authors": ["Bar, Bob", "Foo, Alice"]}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": True}
    assert _meta(vault, paper_id)["authors"] == ["Bar, Bob", "Foo, Alice"]
    assert _index_paper(vault, paper_id)["authors"] == ["Bar, Bob", "Foo, Alice"]


def test_put_metadata_setlist_same_order_is_noop(
    vault_with_paper: tuple[Path, str],
) -> None:
    """The GUI save button sends the whole list whether or not it was touched;
    an untouched list must not bump updated-at (recency_key would move the
    paper to the top of the browse list for a no-edit)."""
    vault, paper_id = vault_with_paper
    before = _meta(vault, paper_id)["updated-at"]
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata",
        json={"setList": {"authors": ["Foo, Alice"]}},
    )
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    assert _meta(vault, paper_id)["updated-at"] == before


def test_put_metadata_setlist_combined_with_set_one_transaction(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata",
        json={
            "set": {"journal": "Real J."},
            "setList": {"authors": ["Bar, Bob", "Foo, Alice"]},
        },
    )
    assert resp.status_code == 200
    meta = _meta(vault, paper_id)
    assert meta["authors"] == ["Bar, Bob", "Foo, Alice"]
    assert meta["journal"] == "Real J."


def test_put_metadata_setlist_whitelist_rejects_other_fields_400(
    vault_with_paper: tuple[Path, str],
) -> None:
    """AC-7: the endpoint must not become a generic list-overwrite channel.
    Server-side whitelist, not frontend restraint."""
    vault, paper_id = vault_with_paper
    for field in ("topics", "projects", "related", "extended-by"):
        resp = _client(vault).put(
            f"/api/paper/{paper_id}/metadata",
            json={"setList": {field: ["x"]}},
        )
        assert resp.status_code == 400, field
        assert "setList" in resp.json()["detail"]
    # Nothing written by any of the rejected calls.
    assert _meta(vault, paper_id)["topics"] == []


def test_put_metadata_setlist_invalid_shape_400(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    client = _client(vault)
    for bad in (
        {"setList": "authors"},
        {"setList": {"authors": "Foo, Alice"}},
        {"setList": {"authors": [1, 2]}},
    ):
        resp = client.put(f"/api/paper/{paper_id}/metadata", json=bad)
        assert resp.status_code == 400, bad


def test_put_metadata_setlist_backend_rejection_leaves_paper_intact(
    vault_with_paper: tuple[Path, str],
) -> None:
    """A duplicate entry is refused by _apply_set_list (400, raw message),
    and the paper is untouched — the GUI shows the message, nothing saved."""
    vault, paper_id = vault_with_paper
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata",
        json={"setList": {"authors": ["Foo, Alice", "Foo, Alice"]}},
    )
    assert resp.status_code == 400
    assert "twice" in resp.json()["detail"]
    assert _meta(vault, paper_id)["authors"] == ["Foo, Alice"]


def test_put_metadata_bad_id_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).put(
        "/api/paper/foo..bar/metadata", json={"set": {"status": "skim"}}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invalid paper id: 'foo..bar'."


def test_put_metadata_unknown_paper_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).put(
        "/api/paper/2099_Nobody_Missing/metadata", json={"set": {"status": "skim"}}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /read — idempotent first-read stamp
# ---------------------------------------------------------------------------


def test_post_read_stamps_read_date(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    assert _meta(vault, paper_id)["read-date"] is None

    resp = _client(vault).post(
        f"/api/paper/{paper_id}/read", json={"date": "2026-05-11"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["changed"] is True
    assert _meta(vault, paper_id)["read-date"] == "2026-05-11"
    assert _index_paper(vault, paper_id)["read-date"] == "2026-05-11"


def test_post_read_second_call_is_noop(vault_with_paper: tuple[Path, str]) -> None:
    """read-date is the immutable first-read stamp: a second read with a
    different date is a no-op (changed: False, message names the original)."""
    vault, paper_id = vault_with_paper
    client = _client(vault)
    client.post(f"/api/paper/{paper_id}/read", json={"date": "2026-05-11"})

    resp = client.post(f"/api/paper/{paper_id}/read", json={"date": "2026-06-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert "already read on 2026-05-11" in body["message"]
    # read-date unchanged.
    assert _meta(vault, paper_id)["read-date"] == "2026-05-11"


def test_post_read_default_today(vault_with_paper: tuple[Path, str]) -> None:
    """No body → today (matches `lit read` with no --date)."""
    from datetime import datetime

    vault, paper_id = vault_with_paper
    resp = _client(vault).post(f"/api/paper/{paper_id}/read")
    assert resp.status_code == 200
    today = datetime.now(UTC).astimezone().date().isoformat()
    assert _meta(vault, paper_id)["read-date"] == today


def test_post_read_unknown_paper_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/paper/2099_Nobody_Missing/read")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /revisit — presupposes a first read
# ---------------------------------------------------------------------------


def test_post_revisit_stamps_when_read(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    client = _client(vault)
    client.post(f"/api/paper/{paper_id}/read", json={"date": "2026-05-11"})

    resp = client.post(
        f"/api/paper/{paper_id}/revisit", json={"date": "2026-06-01"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert _meta(vault, paper_id)["last-revisited"] == "2026-06-01"
    assert _meta(vault, paper_id)["read-date"] == "2026-05-11"  # untouched


def test_post_revisit_without_read_400(vault_with_paper: tuple[Path, str]) -> None:
    """The mutually-exclusive state machine, server-side: no read-date → the
    backend's date-ordering guard raises ModifyError → 400 with the raw
    message the GUI toasts verbatim."""
    vault, paper_id = vault_with_paper
    assert _meta(vault, paper_id)["read-date"] is None

    resp = _client(vault).post(
        f"/api/paper/{paper_id}/revisit", json={"date": "2026-06-01"}
    )
    assert resp.status_code == 400
    assert "a revisit presupposes a first read" in resp.json()["detail"]
    # Nothing written.
    assert _meta(vault, paper_id)["last-revisited"] is None


def test_post_revisit_unknown_paper_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/paper/2099_Nobody_Missing/revisit")
    assert resp.status_code == 404


def test_post_revisit_bad_id_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/paper/foo..bar/revisit")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /unread — guarded reversal of read (clears read-date + last-revisited)
# ---------------------------------------------------------------------------


def test_post_unread_clears_read_date(vault_with_paper: tuple[Path, str]) -> None:
    """A4: unread clears read-date through the backend and reprojects INDEX."""
    vault, paper_id = vault_with_paper
    client = _client(vault)
    client.post(f"/api/paper/{paper_id}/read", json={"date": "2026-05-11"})
    assert _meta(vault, paper_id)["read-date"] == "2026-05-11"

    resp = client.post(f"/api/paper/{paper_id}/unread")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": True, "message": ""}
    # TRUTH cleared …
    assert _meta(vault, paper_id)["read-date"] is None
    # … DERIVED reprojected to match.
    assert _index_paper(vault, paper_id)["read-date"] is None
    # M32: metadata.yaml re-locked read-only after the atomic write.
    assert not os.access(vault / "papers" / paper_id / "metadata.yaml", os.W_OK)


def test_post_unread_clears_read_and_revisit(
    vault_with_paper: tuple[Path, str],
) -> None:
    """A revisit record cannot outlive its read-date (date-ordering), so unread
    clears BOTH stamps in one atomic write — exactly what the confirm dialog
    warns about."""
    vault, paper_id = vault_with_paper
    client = _client(vault)
    client.post(f"/api/paper/{paper_id}/read", json={"date": "2026-05-11"})
    client.post(f"/api/paper/{paper_id}/revisit", json={"date": "2026-06-01"})
    assert _meta(vault, paper_id)["last-revisited"] == "2026-06-01"

    resp = client.post(f"/api/paper/{paper_id}/unread")
    assert resp.status_code == 200
    assert resp.json()["changed"] is True
    meta = _meta(vault, paper_id)
    assert meta["read-date"] is None
    assert meta["last-revisited"] is None


def test_post_unread_not_read_is_noop(vault_with_paper: tuple[Path, str]) -> None:
    """An already-unread paper is a no-op (changed: False), not an error — the
    backend never writes a spurious update."""
    vault, paper_id = vault_with_paper
    assert _meta(vault, paper_id)["read-date"] is None

    resp = _client(vault).post(f"/api/paper/{paper_id}/unread")
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert "not marked read" in body["message"]
    assert _meta(vault, paper_id)["read-date"] is None


def test_post_unread_unknown_paper_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/paper/2099_Nobody_Missing/unread")
    assert resp.status_code == 404


def test_post_unread_bad_id_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/paper/foo..bar/unread")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /fixed-enums
# ---------------------------------------------------------------------------


def test_get_fixed_enums(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/fixed-enums")
    assert resp.status_code == 200
    body = resp.json()
    # `priority` is gone: retired from the schema (ADR-025), and this route is
    # the reason all_fixed_enums must be derived from the table, not indexed.
    assert set(body) == {"status", "type"}

    # status: required (no unset), curation-lifecycle order.
    assert body["status"]["allowsNone"] is False
    assert body["status"]["values"] == ["inbox", "skim", "deep-read", "dropped"]

    # type: optional (offer an unset), sorted values.
    assert body["type"]["allowsNone"] is True
    assert "research" in body["type"]["values"]
    assert body["type"]["values"] == sorted(body["type"]["values"])


# ---------------------------------------------------------------------------
# 3c-1 helpers: register a project through the real CLI backend
# ---------------------------------------------------------------------------


def _register_project(vault: Path, name: str, project_dir: Path) -> None:
    """Register a project via the real CLI (dual-write TAXONOMY + config).

    Mirrors ``_register_topic``: the only legitimate way to register a project
    is the atomic command path (invariant #2), which is exactly what the GUI's
    3c-1 ``POST /api/projects`` calls into via the shared ``add_project`` core.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    result = CliRunner().invoke(
        cli,
        ["project", "add", name, "--path", str(project_dir),
         "--library", str(vault)],
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# POST /paper/{id}/project — link  (+ DELETE … — unlink)
# ---------------------------------------------------------------------------


def test_post_project_links_paper_writes_backend_and_reprojects_index(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A4 core: a project link writes metadata.yaml ``projects`` AND the backend
    reprojects INDEX.json to match AND runs the project-side side effects
    (litman_reflib symlink + REFERENCES.md)."""
    vault, paper_id = vault_with_paper
    project_dir = tmp_path / "pepforge"
    _register_project(vault, "pepforge", project_dir)
    assert _meta(vault, paper_id)["projects"] == []  # fixture default

    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project", json={"project": "pepforge"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # TRUTH updated …
    assert _meta(vault, paper_id)["projects"] == ["pepforge"]
    # … DERIVED projection recomputed to match …
    assert _index_paper(vault, paper_id)["projects"] == ["pepforge"]
    # … and the project-side side effects ran (symlink + REFERENCES.md).
    assert is_portable_link(project_dir / "litman_reflib" / paper_id)
    assert (project_dir / "litman_reflib" / "REFERENCES.md").is_file()


def test_post_project_with_relevance(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, paper_id = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")

    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project",
        json={"project": "pepforge", "relevance": "core baseline"},
    )
    assert resp.status_code == 200
    assert _meta(vault, paper_id)["relevance-pepforge"] == "core baseline"


def test_delete_project_unlinks_roundtrip(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, paper_id = vault_with_paper
    project_dir = tmp_path / "pepforge"
    _register_project(vault, "pepforge", project_dir)
    client = _client(vault)

    client.post(f"/api/paper/{paper_id}/project", json={"project": "pepforge"})
    assert _meta(vault, paper_id)["projects"] == ["pepforge"]

    resp = client.delete(f"/api/paper/{paper_id}/project/pepforge")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # TRUTH + DERIVED both reflect the unlink, symlink torn down.
    assert _meta(vault, paper_id)["projects"] == []
    assert _index_paper(vault, paper_id)["projects"] == []
    assert not (project_dir / "litman_reflib" / paper_id).exists()


def test_post_project_unregistered_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """Link to an unregistered project → LinkError → 400 (raw message)."""
    vault, paper_id = vault_with_paper
    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project", json={"project": "ghost"}
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["detail"]
    assert _meta(vault, paper_id)["projects"] == []


def test_post_project_unknown_paper_404(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, _ = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")
    resp = _client(vault).post(
        "/api/paper/2099_Nobody_Missing/project", json={"project": "pepforge"}
    )
    assert resp.status_code == 404


def test_post_project_empty_name_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project", json={"project": "   "}
    )
    assert resp.status_code == 400


def test_post_project_bad_id_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).post(
        "/api/paper/foo..bar/project", json={"project": "pepforge"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /projects — create / register a project (A7)
# ---------------------------------------------------------------------------


def test_post_projects_registers_with_real_dir(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, _ = vault_with_paper
    project_dir = tmp_path / "newproj"
    project_dir.mkdir()

    resp = _client(vault).post(
        "/api/projects", json={"name": "newproj", "path": str(project_dir)}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["name"] == "newproj"

    # Registered in both truth sources (visible via the read endpoint).
    projects = _client(vault).get("/api/projects").json()
    names = {p["name"] for p in projects}
    assert "newproj" in names


def test_post_projects_nonexistent_path_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A7: the path must exist; a missing path → TaxonomyError → 400."""
    vault, _ = vault_with_paper
    missing = tmp_path / "does-not-exist"
    resp = _client(vault).post(
        "/api/projects", json={"name": "ghost", "path": str(missing)}
    )
    assert resp.status_code == 400
    assert "does not exist" in resp.json()["detail"]
    # Not registered.
    names = {p["name"] for p in _client(vault).get("/api/projects").json()}
    assert "ghost" not in names


def test_post_projects_path_is_file_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A7: the path must be a directory; a file → TaxonomyError → 400."""
    vault, _ = vault_with_paper
    f = tmp_path / "afile.txt"
    f.write_text("x")
    resp = _client(vault).post(
        "/api/projects", json={"name": "p", "path": str(f)}
    )
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["detail"]


def test_post_projects_empty_name_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, _ = vault_with_paper
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    resp = _client(vault).post(
        "/api/projects", json={"name": "   ", "path": str(project_dir)}
    )
    assert resp.status_code == 400


def test_post_projects_duplicate_name_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, _ = vault_with_paper
    _register_project(vault, "dup", tmp_path / "dup")
    resp = _client(vault).post(
        "/api/projects", json={"name": "dup", "path": str(tmp_path / "dup")}
    )
    assert resp.status_code == 400
    assert "already registered" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /taxonomy/{key} — register-first inline-create (invariant #2)
# ---------------------------------------------------------------------------


def test_post_taxonomy_registers_value_then_addtag_succeeds(
    vault_with_paper: tuple[Path, str]
) -> None:
    """Register-first round-trip: POST /api/taxonomy/topics registers the value
    in TAXONOMY, then the existing PUT /metadata addTag attaches it (the two-step
    inline-create the cockpit performs)."""
    from litman.core.taxonomy import parse_taxonomy

    vault, paper_id = vault_with_paper
    client = _client(vault)

    reg = client.post("/api/taxonomy/topics", json={"value": "peptide"})
    assert reg.status_code == 200
    assert reg.json()["added"] == ["peptide"]
    # The value is now in TAXONOMY but NOT attached to any paper.
    parsed = parse_taxonomy((vault / "TAXONOMY.md").read_text())
    assert "peptide" in parsed["topics"]
    assert _meta(vault, paper_id)["topics"] == []
    # M32: the GUI write went through the atomic backend, so TAXONOMY.md is
    # re-locked read-only afterwards (not left writable by a second write path).
    assert not os.access(vault / "TAXONOMY.md", os.W_OK)

    # Step two: attach via the existing addTag path — now it is registered.
    attach = client.put(
        f"/api/paper/{paper_id}/metadata", json={"addTag": {"topics": ["peptide"]}}
    )
    assert attach.status_code == 200
    assert _meta(vault, paper_id)["topics"] == ["peptide"]


def test_post_taxonomy_unknown_key_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/taxonomy/bogus", json={"value": "x"})
    assert resp.status_code == 400
    assert "Unknown dict" in resp.json()["detail"]


def test_post_taxonomy_fixed_enum_key_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """A fixed-enum dict (status) cannot be extended via inline-create."""
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/taxonomy/status", json={"value": "rejected"})
    assert resp.status_code == 400
    assert "fixed-enum" in resp.json()["detail"]


def test_post_taxonomy_projects_key_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """projects is path-bound: inline-create must redirect to POST /api/projects."""
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/taxonomy/projects", json={"value": "p"})
    assert resp.status_code == 400
    assert "lit project" in resp.json()["detail"]


def test_post_taxonomy_empty_value_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).post("/api/taxonomy/topics", json={"value": "   "})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/taxonomy/{key}?value=X — remove a controlled value (P2)
# ---------------------------------------------------------------------------


def _seed_second_paper(vault: Path, paper_id: str) -> None:
    """Write a second minimal paper into the vault, mirroring the fixture seed.

    The taxonomy-delete cascade must rewrite EVERY referencing paper, so the
    A4 assertion needs ≥2 papers tagged with the same value.
    """
    from litman.core.document import list_papers
    from litman.core.views import write_index

    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\n"
        "title: Second Paper\n"
        "authors:\n"
        "  - Baz, Carol\n"
        "year: 2025\n"
        "journal: Test J.\n"
        "doi: 10.1/y\n"
        "arxiv-id:\n"
        "github:\n"
        "created-at: '2026-04-28T10:00:00+02:00'\n"
        "updated-at: '2026-04-28T10:00:00+02:00'\n"
        "projects: []\n"
        "topics: []\n"
        "methods: []\n"
        "data: []\n"
        "type: research\n"
        "status: inbox\n"
        "read-date:\n"
        "last-revisited:\n"
        "related: []\n"
        "contradicts: []\n"
        "extends: []\n"
        "code-clones: []\n",
        encoding="utf-8",
    )
    write_index(vault, list_papers(vault))


def test_delete_taxonomy_cascades_to_all_referencing_papers(
    vault_with_paper: tuple[Path, str]
) -> None:
    """A4 core: deleting a tag value drops it from EVERY referencing paper's
    metadata.yaml (TRUTH) AND the backend reprojects each INDEX entry to match
    (DERIVED) AND the value is gone from TAXONOMY.md."""
    from litman.core.taxonomy import parse_taxonomy

    vault, paper_id = vault_with_paper
    paper2 = "2025_Baz_Qux"
    _seed_second_paper(vault, paper2)
    _register_topic(vault, "peptide")

    client = _client(vault)
    # Tag both papers through the existing addTag write path.
    for pid in (paper_id, paper2):
        r = client.put(
            f"/api/paper/{pid}/metadata", json={"addTag": {"topics": ["peptide"]}}
        )
        assert r.status_code == 200, r.text
    assert _meta(vault, paper_id)["topics"] == ["peptide"]
    assert _meta(vault, paper2)["topics"] == ["peptide"]

    resp = client.delete("/api/taxonomy/topics", params={"value": "peptide"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": 2}

    # TRUTH: both papers untagged …
    assert _meta(vault, paper_id)["topics"] == []
    assert _meta(vault, paper2)["topics"] == []
    # … DERIVED: both INDEX projections reprojected to match …
    assert _index_paper(vault, paper_id)["topics"] == []
    assert _index_paper(vault, paper2)["topics"] == []
    # … and the value is gone from TAXONOMY.md.
    parsed = parse_taxonomy((vault / "TAXONOMY.md").read_text())
    assert "peptide" not in parsed["topics"]


def test_delete_taxonomy_unregistered_value_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """An unregistered value → TaxonomyError → 400 (the frontend treats it as
    benign: the value is already gone)."""
    vault, _ = vault_with_paper
    resp = _client(vault).delete(
        "/api/taxonomy/topics", params={"value": "ghost"}
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["detail"]


def test_delete_taxonomy_projects_key_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """projects is path-bound: deleting it must redirect to DELETE /api/projects."""
    vault, _ = vault_with_paper
    resp = _client(vault).delete(
        "/api/taxonomy/projects", params={"value": "whatever"}
    )
    assert resp.status_code == 400
    assert "lit project" in resp.json()["detail"]


def test_delete_taxonomy_missing_value_param_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """The value is a required query param; omitting it is a client bug → 400."""
    vault, _ = vault_with_paper
    resp = _client(vault).delete("/api/taxonomy/topics")
    assert resp.status_code == 400
    assert "value query parameter is required" in resp.json()["detail"]


def test_delete_taxonomy_value_with_slash_cascades(
    vault_with_paper: tuple[Path, str]
) -> None:
    """The value is a query param SPECIFICALLY so a value containing '/' survives
    routing (a path segment would not). Guard that motivating edge case end-to-
    end: a slash value registers, tags, and cascade-deletes with TRUTH + DERIVED
    both dropping it."""
    from litman.core.taxonomy import parse_taxonomy

    vault, paper_id = vault_with_paper
    slash_value = "deep-learning/transformers"
    _register_topic(vault, slash_value)

    client = _client(vault)
    r = client.put(
        f"/api/paper/{paper_id}/metadata",
        json={"addTag": {"topics": [slash_value]}},
    )
    assert r.status_code == 200, r.text
    assert _meta(vault, paper_id)["topics"] == [slash_value]

    resp = client.delete("/api/taxonomy/topics", params={"value": slash_value})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": 1}

    # TRUTH + DERIVED both drop the slash value.
    assert _meta(vault, paper_id)["topics"] == []
    assert _index_paper(vault, paper_id)["topics"] == []
    assert (
        slash_value
        not in parse_taxonomy((vault / "TAXONOMY.md").read_text())["topics"]
    )


# ---------------------------------------------------------------------------
# DELETE /api/projects/{name} — delete a project (P2)
# ---------------------------------------------------------------------------


def test_delete_project_cascades_and_keeps_dir(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A4 core: deleting a project drops it from the linked paper's metadata
    (TRUTH) + both truth sources, reprojects INDEX, tears down the
    litman_reflib symlink + REFERENCES.md (DERIVED) — but NEVER removes the
    project directory itself."""
    from litman.core.config import load_config
    from litman.core.taxonomy import parse_taxonomy

    vault, paper_id = vault_with_paper
    project_dir = tmp_path / "pepforge"
    _register_project(vault, "pepforge", project_dir)

    # Seed two non-default config fields so the full-config rewrite inside
    # remove_project is guarded against silently dropping unrelated keys (the
    # highest data-loss risk in P2 — it rewrites the entire lit-config.yaml).
    import io

    cfg_path = vault / "lit-config.yaml"
    _raw = _yaml.load(cfg_path.read_text())
    _raw["default_pdf_viewer"] = "zathura"
    _raw["default_clone_depth"] = 0
    _buf = io.StringIO()
    _yaml.dump(_raw, _buf)
    cfg_path.write_text(_buf.getvalue(), encoding="utf-8")

    client = _client(vault)

    # Link the paper so the cascade has something to untag + symlinks to tear down.
    client.post(f"/api/paper/{paper_id}/project", json={"project": "pepforge"})
    assert _meta(vault, paper_id)["projects"] == ["pepforge"]
    assert is_portable_link(project_dir / "litman_reflib" / paper_id)
    assert (project_dir / "litman_reflib" / "REFERENCES.md").is_file()

    resp = client.delete("/api/projects/pepforge")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": 1}

    # TRUTH: paper untagged + dropped from both truth sources …
    assert _meta(vault, paper_id)["projects"] == []
    assert "pepforge" not in parse_taxonomy((vault / "TAXONOMY.md").read_text())["projects"]
    assert "pepforge" not in load_config(vault).projects
    # … DERIVED: INDEX reprojected, symlink + REFERENCES.md torn down …
    assert _index_paper(vault, paper_id)["projects"] == []
    assert not (project_dir / "litman_reflib" / paper_id).exists()
    assert not (project_dir / "litman_reflib" / "REFERENCES.md").exists()
    # … but the project directory itself is preserved (never rmdir'd).
    assert project_dir.is_dir()
    # … and the full-config rewrite preserved every non-projects field
    # (only the one project entry dropped — no collateral data loss).
    cfg_after = load_config(vault)
    assert cfg_after.default_pdf_viewer == "zathura"
    assert cfg_after.default_clone_depth == 0


def test_delete_project_unregistered_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """An unregistered project name → TaxonomyError → 400."""
    vault, _ = vault_with_paper
    resp = _client(vault).delete("/api/projects/ghost")
    assert resp.status_code == 400
    assert "not registered" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# PUT /api/taxonomy/{key} — rename a controlled value (tag rename)
# ---------------------------------------------------------------------------


def test_put_taxonomy_renames_and_cascades(
    vault_with_paper: tuple[Path, str]
) -> None:
    """A4 core: renaming a tag value rewrites it in EVERY referencing paper's
    metadata.yaml (TRUTH), reprojects each INDEX entry (DERIVED), and replaces
    it in TAXONOMY.md — old gone, new present."""
    from litman.core.taxonomy import parse_taxonomy

    vault, paper_id = vault_with_paper
    paper2 = "2025_Baz_Qux"
    _seed_second_paper(vault, paper2)
    _register_topic(vault, "peptied")  # deliberate typo to fix

    client = _client(vault)
    for pid in (paper_id, paper2):
        r = client.put(
            f"/api/paper/{pid}/metadata", json={"addTag": {"topics": ["peptied"]}}
        )
        assert r.status_code == 200, r.text

    resp = client.put("/api/taxonomy/topics", json={"old": "peptied", "new": "peptide"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": 2}

    # TRUTH: both papers re-tagged with the corrected value …
    assert _meta(vault, paper_id)["topics"] == ["peptide"]
    assert _meta(vault, paper2)["topics"] == ["peptide"]
    # … DERIVED: both INDEX projections reprojected to match …
    assert _index_paper(vault, paper_id)["topics"] == ["peptide"]
    assert _index_paper(vault, paper2)["topics"] == ["peptide"]
    # … and TAXONOMY.md swapped old → new.
    parsed = parse_taxonomy((vault / "TAXONOMY.md").read_text())
    assert "peptied" not in parsed["topics"]
    assert "peptide" in parsed["topics"]


def test_put_taxonomy_unregistered_old_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """Renaming a value that is not registered → TaxonomyError → 400."""
    vault, _ = vault_with_paper
    resp = _client(vault).put(
        "/api/taxonomy/topics", json={"old": "ghost", "new": "spirit"}
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["detail"]


def test_put_taxonomy_new_already_exists_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """Renaming onto an existing value is a merge, not a rename → 400 that points
    the user at `lit taxonomy merge`."""
    vault, _ = vault_with_paper
    _register_topic(vault, "alpha")
    _register_topic(vault, "beta")
    resp = _client(vault).put(
        "/api/taxonomy/topics", json={"old": "alpha", "new": "beta"}
    )
    assert resp.status_code == 400
    assert "merge" in resp.json()["detail"]


def test_put_taxonomy_projects_key_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """projects is path-bound: renaming it must redirect to PUT /api/projects."""
    vault, _ = vault_with_paper
    resp = _client(vault).put(
        "/api/taxonomy/projects", json={"old": "a", "new": "b"}
    )
    assert resp.status_code == 400
    assert "lit project" in resp.json()["detail"]


def test_put_taxonomy_empty_new_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """An empty/blank new value is a client bug → 400 (no write)."""
    vault, _ = vault_with_paper
    _register_topic(vault, "alpha")
    resp = _client(vault).put(
        "/api/taxonomy/topics", json={"old": "alpha", "new": "   "}
    )
    assert resp.status_code == 400


def test_put_taxonomy_value_with_slash_renames(
    vault_with_paper: tuple[Path, str]
) -> None:
    """old/new ride in the BODY (not the path) specifically so a value containing
    '/' survives routing. Guard that end-to-end: a slash value renames with TRUTH
    + DERIVED both following."""
    from litman.core.taxonomy import parse_taxonomy

    vault, paper_id = vault_with_paper
    old = "deep-learning/cnn"
    new = "deep-learning/transformers"
    _register_topic(vault, old)
    client = _client(vault)
    r = client.put(
        f"/api/paper/{paper_id}/metadata", json={"addTag": {"topics": [old]}}
    )
    assert r.status_code == 200, r.text

    resp = client.put("/api/taxonomy/topics", json={"old": old, "new": new})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": 1}
    assert _meta(vault, paper_id)["topics"] == [new]
    parsed = parse_taxonomy((vault / "TAXONOMY.md").read_text())
    assert old not in parsed["topics"]
    assert new in parsed["topics"]


# ---------------------------------------------------------------------------
# PUT /api/projects/{name} — rename a project
# ---------------------------------------------------------------------------


def test_put_project_renames_across_truth_and_relevance(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A4 core: renaming a project rewrites BOTH truth sources (TAXONOMY +
    config key, path carried over), the linked paper's projects field + the
    paired relevance-<name> annotation, and reprojects INDEX."""
    from litman.core.config import load_config
    from litman.core.taxonomy import parse_taxonomy

    vault, paper_id = vault_with_paper
    project_dir = tmp_path / "pepforge"
    _register_project(vault, "pepforge", project_dir)
    client = _client(vault)

    client.post(
        f"/api/paper/{paper_id}/project",
        json={"project": "pepforge", "relevance": "core baseline"},
    )
    assert _meta(vault, paper_id)["projects"] == ["pepforge"]
    assert _meta(vault, paper_id)["relevance-pepforge"] == "core baseline"

    resp = client.put("/api/projects/pepforge", json={"new": "pepcodec"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": 1}

    meta = _meta(vault, paper_id)
    # TRUTH: paper field renamed + relevance annotation carried over under the
    # new key (no orphan left behind) …
    assert meta["projects"] == ["pepcodec"]
    assert meta["relevance-pepcodec"] == "core baseline"
    assert "relevance-pepforge" not in meta
    # … both registry truth sources renamed, path carried over unchanged …
    cfg = load_config(vault)
    assert "pepforge" not in cfg.projects
    assert cfg.projects["pepcodec"] == str(project_dir)
    parsed = parse_taxonomy((vault / "TAXONOMY.md").read_text())
    assert "pepforge" not in parsed["projects"]
    assert "pepcodec" in parsed["projects"]
    # … and DERIVED reprojected.
    assert _index_paper(vault, paper_id)["projects"] == ["pepcodec"]


def test_put_project_unregistered_400(
    vault_with_paper: tuple[Path, str]
) -> None:
    """Renaming a project that is not registered → TaxonomyError → 400."""
    vault, _ = vault_with_paper
    resp = _client(vault).put("/api/projects/ghost", json={"new": "spirit"})
    assert resp.status_code == 400
    assert "not registered" in resp.json()["detail"]


def test_put_project_duplicate_new_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """Renaming onto an already-registered project name → 400 (no half-merge)."""
    vault, _ = vault_with_paper
    _register_project(vault, "alpha", tmp_path / "alpha")
    _register_project(vault, "beta", tmp_path / "beta")
    resp = _client(vault).put("/api/projects/alpha", json={"new": "beta"})
    assert resp.status_code == 400
    assert "already registered" in resp.json()["detail"]


def test_put_project_empty_new_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """An empty/blank new name is a client bug → 400."""
    vault, _ = vault_with_paper
    _register_project(vault, "alpha", tmp_path / "alpha")
    resp = _client(vault).put("/api/projects/alpha", json={"new": "   "})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT /api/projects/{name}/path — re-point a project's on-disk path (set-path)
# ---------------------------------------------------------------------------


def test_put_project_path_updates_config_only(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A4 core: re-pointing a project's path is a config-only write — the
    registry now points at the new dir, and papers (which store the NAME) are
    untouched."""
    from litman.core.config import load_config

    vault, paper_id = vault_with_paper
    old_dir = tmp_path / "old_loc"
    new_dir = tmp_path / "new_loc"
    new_dir.mkdir()
    _register_project(vault, "pepforge", old_dir)
    client = _client(vault)
    client.post(f"/api/paper/{paper_id}/project", json={"project": "pepforge"})

    resp = client.put(
        "/api/projects/pepforge/path", json={"path": str(new_dir)}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["changed"] is True
    assert body["path"] == str(new_dir)

    # Config re-pointed; the paper's membership (by NAME) is unchanged.
    assert load_config(vault).projects["pepforge"] == str(new_dir)
    assert _meta(vault, paper_id)["projects"] == ["pepforge"]


def test_put_project_path_noop_when_same(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """Re-pointing to the path it already has is a benign no-op (changed=False),
    not an error."""
    vault, _ = vault_with_paper
    project_dir = tmp_path / "pepforge"
    _register_project(vault, "pepforge", project_dir)
    resp = _client(vault).put(
        "/api/projects/pepforge/path", json={"path": str(project_dir)}
    )
    assert resp.status_code == 200
    assert resp.json()["changed"] is False


def test_put_project_path_unregistered_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """Re-pointing an unregistered project → TaxonomyError → 400."""
    vault, _ = vault_with_paper
    target = tmp_path / "somewhere"
    target.mkdir()
    resp = _client(vault).put(
        "/api/projects/ghost/path", json={"path": str(target)}
    )
    assert resp.status_code == 400
    assert "not registered" in resp.json()["detail"]


def test_put_project_path_nonexistent_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A path that does not exist → 400 (no silent registration of a dead path)."""
    vault, _ = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")
    resp = _client(vault).put(
        "/api/projects/pepforge/path", json={"path": str(tmp_path / "missing")}
    )
    assert resp.status_code == 400
    assert "does not exist" in resp.json()["detail"]


def test_put_project_path_relative_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A relative path is rejected (the server cwd is opaque), not resolved."""
    vault, _ = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")
    resp = _client(vault).put(
        "/api/projects/pepforge/path", json={"path": "relative/dir"}
    )
    assert resp.status_code == 400
    assert "absolute" in resp.json()["detail"]


def test_put_project_path_is_file_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """A path that exists but is a file, not a directory → 400."""
    vault, _ = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")
    resp = _client(vault).put(
        "/api/projects/pepforge/path", json={"path": str(f)}
    )
    assert resp.status_code == 400
    assert "not a directory" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# PUT /api/vaults/active — switch the active vault (3c-2)
# ---------------------------------------------------------------------------


def _two_registered_vaults(tmp_path: Path) -> tuple[Path, Path]:
    """Create + register two vaults; return (v1, v2). v1 is active (added first,
    so auto-active under add_vault); v2 is registered but inactive."""
    v1 = create_vault(tmp_path, name="one")
    v2 = create_vault(tmp_path, name="two")
    reg = load_registry()
    reg = add_vault(reg, "one", v1)  # registry empty → forced active
    reg = add_vault(reg, "two", v2)  # inactive
    save_registry(reg)
    return v1, v2


def test_put_active_vault_switches_registry_and_live_server(tmp_path: Path) -> None:
    """PUT flips the registry's global active AND repoints the live server."""
    v1, v2 = _two_registered_vaults(tmp_path)
    app = create_app(v1)
    client = TestClient(app)

    resp = client.put("/api/vaults/active", json={"name": "two"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["active"] == "two"
    assert Path(body["path"]).resolve() == v2.resolve()

    # The live server is repointed in place (no restart).
    assert Path(app.state.vault).resolve() == v2.resolve()
    # The registry's global active flipped (same effect as `lit vault use`).
    active = find_active(load_registry())
    assert active is not None and active.name == "two"


def test_put_active_vault_unknown_name_400(tmp_path: Path) -> None:
    """An unregistered name → VaultRegistryError → 400, nothing switched."""
    v1, _v2 = _two_registered_vaults(tmp_path)
    app = create_app(v1)
    resp = TestClient(app).put("/api/vaults/active", json={"name": "nope"})
    assert resp.status_code == 400
    assert Path(app.state.vault).resolve() == v1.resolve()
    active = find_active(load_registry())
    assert active is not None and active.name == "one"


def test_put_active_vault_missing_path_refused_400(tmp_path: Path) -> None:
    """A stale registry entry (vault dir gone) is refused BEFORE persisting, so
    the global active + the live server stay on the old vault."""
    import shutil

    v1, v2 = _two_registered_vaults(tmp_path)
    app = create_app(v1)
    locking.rmtree(v2)  # vault two moved / deleted out from under the registry

    resp = TestClient(app).put("/api/vaults/active", json={"name": "two"})
    assert resp.status_code == 400
    assert Path(app.state.vault).resolve() == v1.resolve()
    active = find_active(load_registry())
    assert active is not None and active.name == "one"


def test_put_active_vault_empty_name_400(tmp_path: Path) -> None:
    """A blank name is a client bug → 400."""
    v1, _v2 = _two_registered_vaults(tmp_path)
    resp = TestClient(create_app(v1)).put("/api/vaults/active", json={"name": ""})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# ADR-025 — the per-project grade over the API
#
# The theme of this block: the server adds NO validation of its own. Every
# refusal below is core's, arriving verbatim, because the GUI must never open
# a second write path (invariant #16). Each test therefore asserts the
# message TEXT, not just the status code — a server-side reimplementation
# would pass a 400-only assertion while drifting from what the CLI says.
# ---------------------------------------------------------------------------


def _linked_paper(vault: Path, paper_id: str, project_dir: Path) -> None:
    _register_project(vault, "pepforge", project_dir)
    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project", json={"project": "pepforge"}
    )
    assert resp.status_code == 200, resp.text


def test_put_metadata_retired_priority_is_400_with_the_cli_wording(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"priority": "A"}}
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Cannot --set 'priority': retired" in detail
    assert "priority-<project>" in detail
    assert "priority" not in _meta(vault, paper_id)


def test_put_metadata_priority_for_an_unlinked_project_is_400(
    vault_with_paper: tuple[Path, str],
) -> None:
    """The membership refusal is core's (`_apply_set_op`), not a copy here."""
    vault, paper_id = vault_with_paper
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"priority-nosuch": "A"}}
    )
    assert resp.status_code == 400
    assert "not linked to 'nosuch'" in resp.json()["detail"]
    assert "priority-nosuch" not in _meta(vault, paper_id)


def test_put_metadata_priority_out_of_range_is_400(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, paper_id = vault_with_paper
    _linked_paper(vault, paper_id, tmp_path / "pepforge")
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"priority-pepforge": "Q"}}
    )
    assert resp.status_code == 400
    assert "Invalid priority-pepforge 'Q'" in resp.json()["detail"]


def test_put_metadata_empty_priority_is_400_not_a_null_grade(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """`{"priority-P": ""}` is the shape the cockpit would send for an "unset"
    dropdown entry. There is no such entry by design: absence is the ungraded
    form, so the API must refuse to write a null rather than invent one."""
    vault, paper_id = vault_with_paper
    _linked_paper(vault, paper_id, tmp_path / "pepforge")
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"priority-pepforge": ""}}
    )
    assert resp.status_code == 400
    assert "absent key" in resp.json()["detail"]
    assert "priority-pepforge" not in _meta(vault, paper_id)


def test_put_metadata_priority_for_a_linked_project_is_written(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, paper_id = vault_with_paper
    _linked_paper(vault, paper_id, tmp_path / "pepforge")
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"priority-pepforge": "B"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": True}
    assert _meta(vault, paper_id)["priority-pepforge"] == "B"
    # Not in the INDEX projection — variable-width keys never are (ADR-025).
    assert "priority-pepforge" not in _index_paper(vault, paper_id)


def test_post_project_links_and_grades_in_one_request(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """Decision 5's gesture: picking a letter on an UNLINKED panel row is one
    write, not a link followed by a metadata PUT."""
    vault, paper_id = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")

    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project",
        json={"project": "pepforge", "priority": "A"},
    )
    assert resp.status_code == 200, resp.text

    meta = _meta(vault, paper_id)
    assert meta["projects"] == ["pepforge"]
    assert meta["priority-pepforge"] == "A"


def test_post_project_without_priority_links_ungraded(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, paper_id = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")

    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project", json={"project": "pepforge"}
    )
    assert resp.status_code == 200, resp.text
    meta = _meta(vault, paper_id)
    assert meta["projects"] == ["pepforge"]
    assert not any(k.startswith("priority") for k in meta)


def test_post_project_out_of_range_priority_is_400_from_core(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """Only the TYPE is checked in the handler; the A/B/C range is core's, so
    the client sees exactly what `lit link --priority Q` prints."""
    vault, paper_id = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")

    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project",
        json={"project": "pepforge", "priority": "Q"},
    )
    assert resp.status_code == 400
    assert "Invalid priority 'Q'" in resp.json()["detail"]
    assert "A, B, C" in resp.json()["detail"]
    # Refused whole: no half-made link left behind.
    assert _meta(vault, paper_id)["projects"] == []


def test_post_project_non_string_priority_is_400_at_the_boundary(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """The one check the handler DOES own, mirroring `relevance`: a non-string
    would reach core as a type it cannot compare."""
    vault, paper_id = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")

    resp = _client(vault).post(
        f"/api/paper/{paper_id}/project",
        json={"project": "pepforge", "priority": 3},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "priority must be a string."


def test_delete_project_link_drops_the_grade(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    vault, paper_id = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")
    client = _client(vault)
    client.post(
        f"/api/paper/{paper_id}/project",
        json={"project": "pepforge", "priority": "A"},
    )

    resp = client.delete(f"/api/paper/{paper_id}/project/pepforge")
    assert resp.status_code == 200, resp.text
    meta = _meta(vault, paper_id)
    assert meta["projects"] == []
    assert "priority-pepforge" not in meta


def test_get_paper_passes_the_grade_through_untouched(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """`GET /api/paper/{id}` is the ONLY endpoint that serves the grade: it is
    variable-width, so it can never enter the INDEX projection `/api/papers`
    returns. The cockpit reads it from here."""
    vault, paper_id = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")
    client = _client(vault)
    client.post(
        f"/api/paper/{paper_id}/project",
        json={"project": "pepforge", "priority": "C", "relevance": "core baseline"},
    )

    body = client.get(f"/api/paper/{paper_id}").json()
    assert body["priority-pepforge"] == "C"
    assert body["relevance-pepforge"] == "core baseline"
    assert "priority" not in body
    # Control: the same key is absent from the list projection.
    listed = client.get("/api/papers").json()
    rows = listed["papers"] if isinstance(listed, dict) else listed
    row = next(r for r in rows if r["id"] == paper_id)
    assert not any(k.startswith("priority") for k in row)


def test_project_rm_and_rename_cascade_the_grade_over_the_api(
    vault_with_paper: tuple[Path, str], tmp_path: Path
) -> None:
    """The route docstrings claim both per-project keys cascade. 3.2 did that
    work in core; this pins that the API paths inherit it rather than the
    comments merely asserting it."""
    vault, paper_id = vault_with_paper
    _register_project(vault, "pepforge", tmp_path / "pepforge")
    client = _client(vault)
    client.post(
        f"/api/paper/{paper_id}/project",
        json={"project": "pepforge", "priority": "A"},
    )

    assert client.put("/api/projects/pepforge", json={"new": "pepcodec"}).status_code == 200
    meta = _meta(vault, paper_id)
    assert "priority-pepforge" not in meta
    assert meta["priority-pepcodec"] == "A"     # carried over, value preserved

    assert client.delete("/api/projects/pepcodec").status_code == 200
    assert "priority-pepcodec" not in _meta(vault, paper_id)


# ---------------------------------------------------------------------------
# PUT /metadata — rmTag on relation fields (the Relations remove button)
# ---------------------------------------------------------------------------
# The GUI's relation removal adds NO server code: it reuses this endpoint's
# rmTag, so the ADR-012 double-write happens inside `_apply_modify`'s single
# staged_write. These pin the endpoint-level contract the button relies on —
# both sides cleared in one transaction, a reverse field refused, and a repeat
# reported as `changed: false` rather than an error.


def _relate(client: TestClient, paper_id: str, field: str, other: str) -> None:
    """Create a relation through the same endpoint the GUI adds with."""
    resp = client.put(
        f"/api/paper/{paper_id}/metadata", json={"addTag": {field: [other]}}
    )
    assert resp.status_code == 200, resp.text


def _meta_bytes(vault: Path, paper_id: str) -> bytes:
    return (vault / "papers" / paper_id / "metadata.yaml").read_bytes()


def _index_bytes(vault: Path) -> bytes:
    return (vault / "INDEX.json").read_bytes()


def _distinct_stamps(monkeypatch) -> None:
    """Give every `_apply_modify` call its own second.

    `now_iso` truncates to whole seconds, so an addTag and the rmTag that
    follows it normally land on the SAME timestamp. That makes "both papers
    share an updated-at" true by accident, and a test asserting it would pass
    even if the opposite paper were never rewritten. With this clock the two
    requests are distinguishable, so the assertion can say the sharper thing:
    both papers carry the *removal's* stamp, not the *addition's*.

    Patched on the module object rather than by dotted string. That is the
    house rule for patching a module this test does not own: it binds to the
    object the code under test actually calls, so it cannot be defeated by
    whatever else the session did to sys.modules. The rule was written after a
    real bite, but the bite was specific — tests/commands/test_gui.py drops
    `fastapi*`, `litman.cli*` and `litman.server*` from sys.modules, so a
    string patch aimed at one of THOSE lands on a dead object and silently
    no-ops (green alone, red in the full suite). `litman.commands.modify` is
    not in that set and would survive a string patch; the form below is used
    because it is correct everywhere, not because this module is at risk.
    """
    from litman.commands import modify as modify_module

    calls = [0]

    def fake_now_iso() -> str:
        calls[0] += 1
        return f"2026-09-07T12:00:{calls[0]:02d}+02:00"

    monkeypatch.setattr(modify_module, "now_iso", fake_now_iso)


def test_put_metadata_rm_related_removes_both_sides(
    vault_with_paper: tuple[Path, str], monkeypatch
) -> None:
    """Removing a `related:` id clears the symmetric edge on both papers, in
    one transaction — both carrying the REMOVAL's timestamp is the evidence."""
    vault, paper_a = vault_with_paper
    paper_b = "2025_Baz_Qux"
    _seed_second_paper(vault, paper_b)
    _distinct_stamps(monkeypatch)

    client = _client(vault)
    _relate(client, paper_a, "related", paper_b)
    assert _meta(vault, paper_a)["related"] == [paper_b]
    assert _meta(vault, paper_b)["related"] == [paper_a]
    add_stamp = _meta(vault, paper_a)["updated-at"]

    rm = client.put(
        f"/api/paper/{paper_a}/metadata", json={"rmTag": {"related": [paper_b]}}
    )
    assert rm.status_code == 200, rm.text
    assert rm.json() == {"ok": True, "changed": True}

    meta_a = _meta(vault, paper_a)
    meta_b = _meta(vault, paper_b)
    assert meta_a["related"] == []
    assert meta_b["related"] == []
    # One staged_write → one timestamp for both sides. Control first: the clock
    # really did move between the two requests, so the equality below cannot be
    # satisfied by B still holding the timestamp the ADD gave it.
    assert meta_a["updated-at"] != add_stamp
    assert meta_b["updated-at"] == meta_a["updated-at"]


def test_put_metadata_rm_extends_originating_on_other_paper_clears_reverse(
    vault_with_paper: tuple[Path, str], monkeypatch
) -> None:
    """The path a reverse row's remove button takes: standing on B and removing
    `extended-by: A` must PUT to A's forward `extends`, because a reverse field
    is not writable (see the 400 test below). Both sides end up clear."""
    vault, paper_a = vault_with_paper
    paper_b = "2025_Baz_Qux"
    _seed_second_paper(vault, paper_b)
    _distinct_stamps(monkeypatch)

    client = _client(vault)
    _relate(client, paper_a, "extends", paper_b)
    assert _meta(vault, paper_b)["extended-by"] == [paper_a]
    add_stamp = _meta(vault, paper_b)["updated-at"]

    # User is looking at B and removes `extended-by: A`; the cockpit flips the
    # originator and writes A's forward field with B's id.
    rm = client.put(
        f"/api/paper/{paper_a}/metadata", json={"rmTag": {"extends": [paper_b]}}
    )
    assert rm.status_code == 200, rm.text
    assert rm.json() == {"ok": True, "changed": True}

    meta_a = _meta(vault, paper_a)
    meta_b = _meta(vault, paper_b)
    assert meta_a["extends"] == []
    assert meta_b["extended-by"] == []
    # Same one-transaction evidence, with the clock control: B carries the
    # removal's stamp, not the one the addition left on it.
    assert meta_b["updated-at"] != add_stamp
    assert meta_b["updated-at"] == meta_a["updated-at"]


def test_put_metadata_rm_reverse_field_400(
    vault_with_paper: tuple[Path, str], monkeypatch
) -> None:
    """Naming the reverse field directly is refused (ADR-012: reverse edges are
    maintained only by the paired write) and nothing is written — neither TRUTH
    nor the derived INDEX. This is why the cockpit flips the originator instead
    of removing in place; regressing that flip lands here."""
    vault, paper_a = vault_with_paper
    paper_b = "2025_Baz_Qux"
    _seed_second_paper(vault, paper_b)
    # Relation fields are not in the INDEX projection, so the only bytes a
    # wrongly-accepted write could move in INDEX.json are its timestamps — and
    # on the real second-granularity clock this request would share one with
    # the addTag above, making the comparison below pass by collision.
    _distinct_stamps(monkeypatch)

    client = _client(vault)
    _relate(client, paper_a, "extends", paper_b)
    before_a = _meta_bytes(vault, paper_a)
    before_b = _meta_bytes(vault, paper_b)
    before_index = _index_bytes(vault)

    resp = client.put(
        f"/api/paper/{paper_b}/metadata",
        json={"rmTag": {"extended-by": [paper_a]}},
    )
    assert resp.status_code == 400
    assert "extended-by" in resp.json()["detail"]
    assert _meta_bytes(vault, paper_a) == before_a
    assert _meta_bytes(vault, paper_b) == before_b
    assert _index_bytes(vault) == before_index


def test_put_metadata_rm_relation_idempotent(
    vault_with_paper: tuple[Path, str], monkeypatch
) -> None:
    """A second removal of an edge another writer already dropped is
    `changed: false`, HTTP 200 — not an error the GUI has to special-case."""
    vault, paper_a = vault_with_paper
    paper_b = "2025_Baz_Qux"
    _seed_second_paper(vault, paper_b)
    # Without a moving clock the repeat lands in the same second as the first
    # removal, so the byte-compares below would hold even if the no-op
    # short-circuit were gone and both files had been rewritten.
    _distinct_stamps(monkeypatch)

    client = _client(vault)
    _relate(client, paper_a, "related", paper_b)
    first = client.put(
        f"/api/paper/{paper_a}/metadata", json={"rmTag": {"related": [paper_b]}}
    )
    assert first.json() == {"ok": True, "changed": True}
    after_a = _meta_bytes(vault, paper_a)
    after_b = _meta_bytes(vault, paper_b)

    second = client.put(
        f"/api/paper/{paper_a}/metadata", json={"rmTag": {"related": [paper_b]}}
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"ok": True, "changed": False}
    assert _meta_bytes(vault, paper_a) == after_a
    assert _meta_bytes(vault, paper_b) == after_b
