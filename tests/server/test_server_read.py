"""Read-endpoint tests for the litman webUI server (A2).

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.cli import cli
from litman.core.document import list_papers
from litman.core.query import recency_key
from litman.core.views import project_paper, write_index
from litman.server import create_app


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def test_get_papers_matches_core_projection(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get("/api/papers")
    assert resp.status_code == 200

    expected = [project_paper(p) for p in list_papers(vault)]
    body = resp.json()
    assert [p["id"] for p in body] == [paper_id]
    assert body == expected


def test_get_paper_returns_full_metadata(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get(f"/api/paper/{paper_id}")
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["id"] == paper_id
    assert meta["title"] == "Foo Bar"
    # full metadata carries fields the thin INDEX projection drops
    assert "authors" in meta
    assert "journal" in meta


def test_get_paper_unknown_id_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/paper/does_not_exist")
    assert resp.status_code == 404


def test_get_pdf_supports_range(
    vault_with_paper: tuple[Path, str],
    make_text_pdf: Callable[..., Path],
) -> None:
    vault, paper_id = vault_with_paper
    pdf_src = make_text_pdf([["hello pdf"]])
    (vault / "papers" / paper_id / "paper.pdf").write_bytes(pdf_src.read_bytes())

    client = _client(vault)

    full = client.get(f"/api/paper/{paper_id}/pdf")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"

    partial = client.get(
        f"/api/paper/{paper_id}/pdf", headers={"Range": "bytes=0-99"}
    )
    assert partial.status_code == 206
    assert partial.headers["accept-ranges"] == "bytes"
    assert len(partial.content) == 100


def test_get_pdf_missing_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get(f"/api/paper/{paper_id}/pdf")
    assert resp.status_code == 404


@pytest.mark.parametrize("suffix", ["pdf", "notes"])
def test_traversal_id_rejected(
    vault_with_paper: tuple[Path, str],
    tmp_path: Path,
    suffix: str,
) -> None:
    """A traversal-style id must never reach outside the vault.

    Two id shapes are exercised on each file-serving endpoint:

    1. A percent-encoded ``../../`` form — the httpx test client normalizes the
       escaped slashes, so Starlette's router rejects it before our handler.
       We only assert it 404s and serves nothing outside the vault.
    2. A single-segment id containing ``..`` (e.g. ``foo..bar``) that DOES
       reach the handler as one path param. ``is_valid_id`` rejects it, so the
       defense-in-depth guard fires and returns 404 with our own detail — this
       proves the guard code path runs, not just a routing accident.
    """
    vault, _ = vault_with_paper

    # Plant a sentinel file outside the vault that a successful traversal could
    # leak; assert it is never served.
    secret = tmp_path / "outside_secret"
    secret.write_text("TOP SECRET\n", encoding="utf-8")

    client = _client(vault)

    encoded = client.get(f"/api/paper/..%2f..%2foutside_secret/{suffix}")
    assert encoded.status_code == 404
    assert "TOP SECRET" not in encoded.text

    guarded = client.get(f"/api/paper/foo..bar/{suffix}")
    assert guarded.status_code == 404
    assert guarded.json()["detail"] == "Invalid paper id: 'foo..bar'."


def test_get_paper_corrupt_metadata_500(
    vault_with_paper: tuple[Path, str],
) -> None:
    """An existing paper with unparseable YAML is a 500, not a 404.

    The paper folder is present, so the resource exists; its metadata being
    broken is a server-side data problem. ``find_paper`` raises
    ``CorruptMetadataError`` (carrying the offending path), which the handler
    surfaces as 500 rather than masking it as 'not found'.
    """
    vault, paper_id = vault_with_paper
    # Overwrite the fixture's valid YAML with a syntactically broken document.
    (vault / "papers" / paper_id / "metadata.yaml").write_text(
        "title: [unbalanced\n  : : :\n", encoding="utf-8"
    )

    resp = _client(vault).get(f"/api/paper/{paper_id}")
    assert resp.status_code == 500
    # The path-bearing message from CorruptMetadataError is carried through.
    assert "metadata.yaml" in resp.json()["detail"]


def test_get_paper_missing_is_404_not_500(
    vault_with_paper: tuple[Path, str],
) -> None:
    """A genuinely-absent paper stays a 404 (regression guard for Fix 2)."""
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/paper/2024_Nope_Missing")
    assert resp.status_code == 404


def test_get_taxonomy_returns_dict_keys(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/taxonomy")
    assert resp.status_code == 200
    tax = resp.json()
    for key in ("projects", "topics", "methods", "data"):
        assert key in tax
        assert isinstance(tax[key], list)


def test_get_projects_empty_vault(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_vaults_reports_active(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).get("/api/vaults")
    assert resp.status_code == 200
    payload = resp.json()
    assert "active" in payload
    assert "vaults" in payload
    assert isinstance(payload["vaults"], list)


def test_get_notes_404_when_absent(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    resp = _client(vault).get(f"/api/paper/{paper_id}/notes")
    assert resp.status_code == 404


def test_get_notes_returns_text(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    (vault / "papers" / paper_id / "notes.md").write_text(
        "# notes\n", encoding="utf-8"
    )
    resp = _client(vault).get(f"/api/paper/{paper_id}/notes")
    assert resp.status_code == 200
    assert resp.json()["text"] == "# notes\n"


def test_root_route_serves_spa_or_placeholder(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Root either serves the built SPA index.html or the not-built placeholder.

    Which one depends on whether ``frontend/build.sh`` has vendored the SPA
    into ``assets/webui/`` (Phase 1 onward it has). Both are a 200; we assert
    the right body for whichever state the working tree is in, so the test
    passes pre- and post-build rather than pinning one transient state.
    """
    from litman.server import _WEBUI_ASSETS

    vault, _ = vault_with_paper
    resp = _client(vault).get("/")
    assert resp.status_code == 200
    if _WEBUI_ASSETS.is_dir():
        # Built SPA: StaticFiles serves the HTML shell with the root mount.
        assert "<div id=\"root\">" in resp.text
        assert "not built" not in resp.text
    else:
        assert "not built" in resp.text


# ---------------------------------------------------------------------------
# A5 — smart-list ?view= query
# ---------------------------------------------------------------------------


def _seed_paper(vault: Path, paper_id: str, **fields: object) -> None:
    """Write one minimal metadata.yaml under ``papers/<id>/`` for A5 tests."""
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines: list[str] = [f"id: {paper_id}"]
    for key, value in fields.items():
        if value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    (paper_dir / "metadata.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


@pytest.fixture
def smartlist_vault(
    vault_with_paper: tuple[Path, str],
) -> tuple[Path, dict[str, str]]:
    """The fixture vault plus a mix spanning every smart-list bucket.

    The base ``vault_with_paper`` paper (``2024_Foo_Bar``) is unread + inbox,
    so it is a ``reading``/``backlog`` member already. We add:

    - an unread + skim paper (still a reading member — skim is NOT dropped),
    - a read (read-date set) + deep-read paper (a recent-read member),
    - a dropped paper (excluded from every smart-list).

    ``updated-at`` is staggered so the recency order is deterministic without
    touching pdf mtimes. INDEX.json is rebuilt so the no-view path stays sane.
    """
    vault, base_id = vault_with_paper
    ids = {
        "unread_inbox": base_id,
        "unread_skim": "2023_Skim_Paper",
        "read_deep": "2022_Read_Paper",
        "dropped": "2021_Dropped_Paper",
    }
    _seed_paper(
        vault, ids["unread_skim"],
        title="Skim Paper", year=2023, status="skim", priority="C",
        **{"read-date": None, "updated-at": "'2026-05-01T10:00:00+02:00'"},
    )
    _seed_paper(
        vault, ids["read_deep"],
        title="Read Paper", year=2022, status="deep-read", priority="A",
        **{"read-date": "2026-05-20", "updated-at": "'2026-05-20T10:00:00+02:00'"},
    )
    _seed_paper(
        vault, ids["dropped"],
        title="Dropped Paper", year=2021, status="dropped", priority="C",
        **{"read-date": None, "updated-at": "'2026-05-10T10:00:00+02:00'"},
    )
    write_index(vault, list_papers(vault))
    return vault, ids


def test_view_reading_excludes_read_and_dropped(
    smartlist_vault: tuple[Path, dict[str, str]],
) -> None:
    vault, ids = smartlist_vault
    resp = _client(vault).get("/api/papers?view=reading")
    assert resp.status_code == 200
    got = [p["id"] for p in resp.json()]

    # membership: unread inbox + unread skim, NOT the read one, NOT the dropped.
    assert ids["unread_inbox"] in got
    assert ids["unread_skim"] in got  # skim is reading, not dropped
    assert ids["read_deep"] not in got
    assert ids["dropped"] not in got

    # order: recency DESC over the same recency_key the CLI uses.
    papers = {p["id"]: p for p in list_papers(vault)}
    expected = sorted(
        got,
        key=lambda pid: recency_key(vault, papers[pid]),
        reverse=True,
    )
    assert got == expected


def test_view_recent_read_only_read_non_dropped_date_desc(
    smartlist_vault: tuple[Path, dict[str, str]],
) -> None:
    vault, ids = smartlist_vault
    resp = _client(vault).get("/api/papers?view=recent-read")
    assert resp.status_code == 200
    got = [p["id"] for p in resp.json()]

    # only the read, non-dropped paper qualifies.
    assert got == [ids["read_deep"]]


def test_view_backlog_is_reading_reversed(
    smartlist_vault: tuple[Path, dict[str, str]],
) -> None:
    vault, _ = smartlist_vault
    client = _client(vault)
    reading = [p["id"] for p in client.get("/api/papers?view=reading").json()]
    backlog = [p["id"] for p in client.get("/api/papers?view=backlog").json()]
    # same membership, reverse order (recency ASC vs DESC).
    assert set(reading) == set(backlog)
    assert backlog == list(reversed(reading))


def test_view_reading_matches_cli_unread_recent_minus_dropped(
    smartlist_vault: tuple[Path, dict[str, str]],
) -> None:
    """A5 equivalence: ``view=reading`` == ``lit list --unread --sort recent``
    on the same vault, after removing dropped papers from the CLI output.

    ``lit list --unread`` keeps every unread paper (including dropped ones,
    which still have an empty read-date), so we drop status==dropped from the
    CLI id order before comparing -- that is the literal "same result, dropped
    pruned" contract.
    """
    vault, _ = smartlist_vault

    runner = CliRunner()
    cli_res = runner.invoke(
        cli,
        ["list", "--library", str(vault), "--unread", "--sort", "recent",
         "--format", "json"],
    )
    assert cli_res.exit_code == 0, cli_res.output
    cli_papers = json.loads(cli_res.output)
    cli_ids = [p["id"] for p in cli_papers if p.get("status") != "dropped"]

    api_ids = [
        p["id"] for p in _client(vault).get("/api/papers?view=reading").json()
    ]
    assert api_ids == cli_ids


def test_view_absent_returns_index_projection_unchanged(
    smartlist_vault: tuple[Path, dict[str, str]],
) -> None:
    """No ``?view=`` → exactly the Phase 0 INDEX projection (regression)."""
    vault, _ = smartlist_vault
    resp = _client(vault).get("/api/papers")
    assert resp.status_code == 200
    expected = [project_paper(p) for p in list_papers(vault)]
    assert resp.json() == expected


def test_view_invalid_value_is_400(
    smartlist_vault: tuple[Path, dict[str, str]],
) -> None:
    vault, _ = smartlist_vault
    resp = _client(vault).get("/api/papers?view=bogus")
    assert resp.status_code == 400


def test_recency_key_ranks_pdf_mtime_vs_updated_at(
    tmp_path: Path,
    make_text_pdf: Callable[..., Path],
) -> None:
    """Behavior-preserving extraction check: recency_key still ranks a
    fresh-pdf-mtime paper above an older-updated-at paper, and uses the later
    of the two signals.
    """
    from litman.core.library import create_vault

    vault = create_vault(tmp_path)

    # Paper A: only an old updated-at, no pdf.
    a_dir = vault / "papers" / "2020_A"
    a_dir.mkdir(parents=True)
    a = {"id": "2020_A", "updated-at": "2020-01-01T00:00:00+00:00"}

    # Paper B: a pdf on disk (its mtime is "now", far newer than A's date).
    b_dir = vault / "papers" / "2021_B"
    b_dir.mkdir(parents=True)
    (b_dir / "paper.pdf").write_bytes(make_text_pdf([["x"]]).read_bytes())
    b = {"id": "2021_B", "updated-at": "2019-01-01T00:00:00+00:00"}

    key_a = recency_key(vault, a)
    key_b = recency_key(vault, b)
    # B wins on the (recent) pdf mtime, which dominates its older updated-at.
    assert key_b > key_a
    # A with neither signal sinks to 0.0.
    c = {"id": "missing"}
    assert recency_key(vault, c) == 0.0
