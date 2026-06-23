"""Structured-write endpoint tests for the litman webUI server (A4).

Covers the invariant #16 SECOND-class writes — the cockpit's structured
metadata changes that go through the ``lit`` command backends, never a second
write path:

* ``PUT  /api/paper/{id}/metadata`` → ``_apply_modify`` (set / addTag / rmTag)
* ``POST /api/paper/{id}/read``     → ``apply_read`` (idempotent first-read)
* ``POST /api/paper/{id}/revisit``  → ``apply_revisit`` (presupposes a read)
* ``GET  /api/fixed-enums``         → status/priority/type whitelists

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

from fastapi.testclient import TestClient

from litman.cli import cli
from litman.server import create_app

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
# PUT /metadata — set (status/priority/type dropdowns)
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


def test_put_metadata_unset_priority(vault_with_paper: tuple[Path, str]) -> None:
    """An empty value unsets an optional fixed enum to null (priority/type)."""
    vault, paper_id = vault_with_paper
    assert _meta(vault, paper_id)["priority"] == "B"  # fixture default

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/metadata", json={"set": {"priority": ""}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": True}
    assert _meta(vault, paper_id)["priority"] is None
    assert _index_paper(vault, paper_id)["priority"] is None


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
    only clear the optional enums (priority/type). The backend's required-field
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
    from datetime import datetime, timezone

    vault, paper_id = vault_with_paper
    resp = _client(vault).post(f"/api/paper/{paper_id}/read")
    assert resp.status_code == 200
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
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
    assert set(body) == {"status", "priority", "type"}

    # status: required (no unset), curation-lifecycle order.
    assert body["status"]["allowsNone"] is False
    assert body["status"]["values"] == ["inbox", "skim", "deep-read", "dropped"]

    # priority / type: optional (offer an unset), sorted values.
    assert body["priority"]["allowsNone"] is True
    assert body["priority"]["values"] == ["A", "B", "C"]
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
    assert (project_dir / "litman_reflib" / paper_id).is_symlink()
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
