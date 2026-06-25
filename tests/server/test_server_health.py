"""Health-check API endpoint tests for the webUI server (observability slice).

Covers ``GET /api/health`` — the pure-read mirror of ``lit health-check`` that
lets a GUI-only user self-audit library consistency (ADR-017). Every assertion
verifies the endpoint:

* returns the flat ``Issue[]`` list (five fields) ``run_all_checks`` produces,
* surfaces a real drift (``index_vs_disk``: an INDEX entry whose ``papers/<id>/``
  was deleted), and
* is *only* read (invariant #16): it never re-locks TRUTH, auto-fixes, or stamps
  the registry's ``last_health_check_at`` — so TRUTH file + registry mtimes are
  unchanged across the GET.

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.vault_registry import (
    add_vault,
    load_registry,
    registry_path,
    save_registry,
)
from litman.core.views import write_index
from litman.server import create_app

_yaml = YAML(typ="safe")

_ISSUE_FIELDS = {"category", "severity", "paper_id", "message", "hint"}


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _write_paper(vault: Path, paper_id: str, **fields: Any) -> None:
    """Write a minimal valid paper folder (canonical M2 schema)."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": paper_id,
        "title": fields.get("title", f"Title of {paper_id}"),
        "authors": ["Doe, Jane"],
        "year": 2024,
        "journal": "Test J.",
        "doi": f"10.0/{paper_id}",
        "arxiv-id": None,
        "github": None,
        "created-at": "2026-04-28T10:00:00+02:00",
        "updated-at": "2026-04-28T10:00:00+02:00",
        "projects": [],
        "topics": fields.get("topics", []),
        "methods": [],
        "data": [],
        "type": "research",
        "status": "inbox",
        "priority": "B",
        "read-date": None,
        "last-revisited": None,
        "related": fields.get("related", []),
        "contradicts": [],
        "extends": [],
        "code-clones": [],
    }
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.default_flow_style = False
    with (paper_dir / "metadata.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(payload, f)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ===========================================================================
# AC1(a) — clean vault: 200 + a list, no reconciliation/schema findings
# ===========================================================================


def test_health_clean_vault_is_clean_list(vault: Path) -> None:
    """Two well-formed papers with a matching INDEX → no drift/schema issues.

    A fresh ``create_vault`` + two canonical papers + a rebuilt INDEX is the
    consistent state; the endpoint must return a JSON list with no
    ``index_vs_disk`` (reconciliation) or ``schema`` (validity) findings. Other
    benign info-level findings may exist, so we assert on the absence of those
    two categories rather than total emptiness.
    """
    _write_paper(vault, "2024_Foo")
    _write_paper(vault, "2024_Bar")
    write_index(vault, list_papers(vault))

    resp = _client(vault).get("/api/health")
    assert resp.status_code == 200
    issues = resp.json()
    assert isinstance(issues, list)
    categories = {i["category"] for i in issues}
    assert "index_vs_disk" not in categories
    assert "schema" not in categories
    # Whatever did surface still carries the full Issue shape.
    for issue in issues:
        assert set(issue) == _ISSUE_FIELDS


# ===========================================================================
# AC1(b) — drift surfaces: an INDEX entry whose papers/<id>/ was deleted
# ===========================================================================


def test_health_reports_index_vs_disk_drift(vault: Path) -> None:
    """Delete a paper dir that INDEX still lists → an ``index_vs_disk`` error.

    Builds INDEX over both papers, then removes ``papers/2024_Bar/`` on disk
    (the manual ``rm -rf`` failure mode ``check_index_vs_disk`` exists for). The
    GET must report a finding with category ``index_vs_disk``, severity
    ``error``, the vanished id, and the five Issue fields populated.
    """
    _write_paper(vault, "2024_Foo")
    _write_paper(vault, "2024_Bar")
    write_index(vault, list_papers(vault))

    # INDEX still lists 2024_Bar, but its directory is gone.
    shutil.rmtree(vault / "papers" / "2024_Bar")

    resp = _client(vault).get("/api/health")
    assert resp.status_code == 200
    issues = resp.json()

    drift = [i for i in issues if i["category"] == "index_vs_disk"]
    assert drift, f"expected an index_vs_disk issue, got categories {[i['category'] for i in issues]}"
    bar = next(i for i in drift if i["paper_id"] == "2024_Bar")
    assert bar["severity"] == "error"
    # All five Issue fields are present (paper_id + hint set for this case).
    assert set(bar) == _ISSUE_FIELDS
    assert bar["paper_id"] == "2024_Bar"
    assert isinstance(bar["message"], str) and bar["message"]
    assert bar["hint"]  # check_index_vs_disk attaches a remediation hint


# ===========================================================================
# AC1(c) — read-only: no re-lock / fix / registry timestamp write
# ===========================================================================


def test_health_is_read_only_no_truth_or_registry_writes(vault: Path) -> None:
    """The GET mutates nothing: TRUTH file + registry mtimes unchanged.

    ``index_vs_disk`` is a klass-A (regen-fixable) drift, so the CLI's
    ``--fix`` path *would* rewrite INDEX and stamp the registry's
    ``last_health_check_at``. The endpoint must do neither. We register the
    vault (so ``vaults.yaml`` exists), snapshot the mtimes (ns) of every
    surviving ``metadata.yaml`` + ``TAXONOMY.md`` + the registry file, plus the
    registry file's bytes, run the GET against a vault with a live drift, and
    assert nothing changed.
    """
    _write_paper(vault, "2024_Foo")
    _write_paper(vault, "2024_Bar")
    write_index(vault, list_papers(vault))
    shutil.rmtree(vault / "papers" / "2024_Bar")  # a live drift to exercise

    # Register the vault so the per-machine registry file exists to snapshot.
    reg = load_registry()
    reg = add_vault(reg, "obs-test", vault, set_active=True)
    save_registry(reg)
    reg_file = registry_path()
    assert reg_file.is_file()

    truth_files = [vault / "papers" / "2024_Foo" / "metadata.yaml", vault / "TAXONOMY.md"]
    before_mtimes = {p: p.stat().st_mtime_ns for p in [*truth_files, reg_file]}
    before_registry_bytes = reg_file.read_bytes()

    resp = _client(vault).get("/api/health")
    assert resp.status_code == 200
    # The endpoint still reported the drift (so it really ran the checks)…
    assert any(i["category"] == "index_vs_disk" for i in resp.json())

    # …yet touched nothing: TRUTH + registry mtimes and registry bytes intact.
    after_mtimes = {p: p.stat().st_mtime_ns for p in [*truth_files, reg_file]}
    assert after_mtimes == before_mtimes
    assert reg_file.read_bytes() == before_registry_bytes
    # And no last_health_check_at was stamped on the registered vault.
    reloaded = load_registry()
    entry = next(v for v in reloaded.vaults if v.name == "obs-test")
    assert entry.last_health_check_at is None
    # INDEX was not regenerated (the dead entry is still listed — proof the GET
    # did not silently run the klass-A fix).
    payload = json.loads((vault / "INDEX.json").read_text())
    assert "2024_Bar" in [p["id"] for p in payload["papers"]]
