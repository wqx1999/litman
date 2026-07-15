"""Whitelist write-endpoint tests for the litman webUI server (A3).

Covers the three invariant #16 first-class direct writes, each atomically
writing one authored file via ``staged_write`` (notes/discussion are
create-or-overwrite, paper.pdf is overwrite-only):

* ``PUT /api/paper/{id}/pdf-annotations`` → paper.pdf
* ``PUT /api/paper/{id}/notes``          → notes.md (with wikilink reminder)
* ``PUT /api/paper/{id}/discussion``     → discussion.md (with format header)

Guarded with ``importorskip`` so the suite still collects when the optional
``web`` extra is absent (invariant #5)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from litman.server import create_app


def _client(vault: Path) -> TestClient:
    return TestClient(create_app(vault))


def _plant_pdf(
    vault: Path, paper_id: str, make_text_pdf: Callable[..., Path]
) -> Path:
    pdf_path = vault / "papers" / paper_id / "paper.pdf"
    pdf_path.write_bytes(make_text_pdf([["original"]]).read_bytes())
    return pdf_path


def test_put_overwrites_pdf_on_disk(
    vault_with_paper: tuple[Path, str],
    make_text_pdf: Callable[..., Path],
) -> None:
    vault, paper_id = vault_with_paper
    pdf_path = _plant_pdf(vault, paper_id, make_text_pdf)

    new_bytes = make_text_pdf([["annotated"], ["page two"]]).read_bytes()
    assert new_bytes != pdf_path.read_bytes()

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=new_bytes,
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "bytes": len(new_bytes)}

    # The overwrite actually happened on disk.
    assert pdf_path.read_bytes() == new_bytes


def test_put_leaves_no_staging_residue(
    vault_with_paper: tuple[Path, str],
    make_text_pdf: Callable[..., Path],
) -> None:
    """A clean staged_write commit removes its op dir; .litman-staging is empty.

    ``create_vault`` creates an empty ``.litman-staging/`` directory, so the dir
    itself persists; what must NOT remain is any per-op subdirectory (which
    would indicate a torn / uncommitted write).
    """
    vault, paper_id = vault_with_paper
    _plant_pdf(vault, paper_id, make_text_pdf)

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=make_text_pdf([["annotated"]]).read_bytes(),
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 200

    staging = vault / ".litman-staging"
    leftover = [p for p in staging.iterdir()] if staging.is_dir() else []
    assert leftover == []


def test_put_bad_id_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).put(
        "/api/paper/foo..bar/pdf-annotations",
        content=b"%PDF-1.4 fake",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invalid paper id: 'foo..bar'."


def test_put_missing_pdf_404(
    vault_with_paper: tuple[Path, str],
) -> None:
    """Whitelist is OVERWRITE-only: no paper.pdf on disk → 404, never create."""
    vault, paper_id = vault_with_paper
    pdf_path = vault / "papers" / paper_id / "paper.pdf"
    assert not pdf_path.exists()

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=b"%PDF-1.4 fake",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 404
    # And nothing was created.
    assert not pdf_path.exists()


def test_put_empty_body_400(
    vault_with_paper: tuple[Path, str],
    make_text_pdf: Callable[..., Path],
) -> None:
    vault, paper_id = vault_with_paper
    pdf_path = _plant_pdf(vault, paper_id, make_text_pdf)
    before = pdf_path.read_bytes()

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/pdf-annotations",
        content=b"",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code == 400
    # The existing pdf is untouched.
    assert pdf_path.read_bytes() == before


# ---------------------------------------------------------------------------
# notes.md / discussion.md overwrites (Phase 3a)
# ---------------------------------------------------------------------------

from litman.core.notes import (
    WIKILINK_REMINDER,
    discussion_scaffold,
    has_discussion_reminder,
)


def _plant_md(vault: Path, paper_id: str, name: str, body: str) -> Path:
    md_path = vault / "papers" / paper_id / name
    md_path.write_text(body, encoding="utf-8")
    return md_path


def test_put_notes_overwrites_and_keeps_reminder(
    vault_with_paper: tuple[Path, str],
) -> None:
    """notes PUT overwrites the file AND the wikilink reminder is present after.

    The posted text omits the reminder; the server must run
    ``ensure_wikilink_reminder`` so the nudge survives a human rewrite.
    """
    vault, paper_id = vault_with_paper
    md_path = _plant_md(vault, paper_id, "notes.md", "# Old\n\nold body\n")

    new_text = "# Notes\n\nrewritten body with [[2024_Other_Ref]]\n"
    resp = _client(vault).put(f"/api/paper/{paper_id}/notes", json={"text": new_text})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    on_disk = md_path.read_text(encoding="utf-8")
    # Old content is gone (full overwrite), the new body is in, and the
    # wikilink reminder was re-inserted by the server.
    assert "old body" not in on_disk
    assert "rewritten body" in on_disk
    assert WIKILINK_REMINDER in on_disk
    # Reported byte count is the healed (post-reminder) length.
    assert resp.json()["bytes"] == len(on_disk.encode("utf-8"))


def test_put_notes_keeps_existing_reminder_once(
    vault_with_paper: tuple[Path, str],
) -> None:
    """When the posted text already carries the reminder it is not duplicated."""
    vault, paper_id = vault_with_paper
    md_path = _plant_md(vault, paper_id, "notes.md", "# Notes\n\nseed\n")

    new_text = f"# Notes\n\n{WIKILINK_REMINDER}\n\nbody\n"
    resp = _client(vault).put(f"/api/paper/{paper_id}/notes", json={"text": new_text})
    assert resp.status_code == 200

    on_disk = md_path.read_text(encoding="utf-8")
    assert on_disk.count(WIKILINK_REMINDER) == 1


def test_put_notes_bad_id_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).put("/api/paper/foo..bar/notes", json={"text": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invalid paper id: 'foo..bar'."


def test_put_notes_creates_when_absent(vault_with_paper: tuple[Path, str]) -> None:
    """notes.md is CREATE-or-overwrite: an absent file is created, not 404'd.

    (``lit add`` scaffolds notes.md, but a paper can still lack it — a hand-built
    or pre-scaffold paper folder — so the GUI must be able to start one.)
    """
    vault, paper_id = vault_with_paper
    notes_path = vault / "papers" / paper_id / "notes.md"
    assert not notes_path.exists()

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/notes", json={"text": "# Notes\n\nfirst note\n"}
    )
    assert resp.status_code == 200
    on_disk = notes_path.read_text(encoding="utf-8")
    assert "first note" in on_disk
    assert WIKILINK_REMINDER in on_disk  # reminder injected on create too


def test_put_notes_empty_text_400(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    md_path = _plant_md(vault, paper_id, "notes.md", "# Notes\n\nseed\n")
    before = md_path.read_text(encoding="utf-8")

    resp = _client(vault).put(f"/api/paper/{paper_id}/notes", json={"text": ""})
    assert resp.status_code == 400
    assert md_path.read_text(encoding="utf-8") == before


def test_put_notes_missing_text_key_400(vault_with_paper: tuple[Path, str]) -> None:
    vault, paper_id = vault_with_paper
    _plant_md(vault, paper_id, "notes.md", "# Notes\n\nseed\n")

    resp = _client(vault).put(f"/api/paper/{paper_id}/notes", json={"body": "x"})
    assert resp.status_code == 400


def test_put_md_whitespace_only_text_400(vault_with_paper: tuple[Path, str]) -> None:
    """A whitespace-only body is rejected like an empty one (shared parser).

    ``_md_text_body`` strips before the empty check, so an accidental
    select-all + delete + save (which leaves only a newline) cannot blank an
    authored note. One case suffices since both endpoints share the parser.
    """
    vault, paper_id = vault_with_paper
    md_path = _plant_md(vault, paper_id, "notes.md", "# Notes\n\nseed\n")
    before = md_path.read_text(encoding="utf-8")

    resp = _client(vault).put(f"/api/paper/{paper_id}/notes", json={"text": "  \n\t"})
    assert resp.status_code == 400
    assert md_path.read_text(encoding="utf-8") == before


def test_put_discussion_full_overwrite(
    vault_with_paper: tuple[Path, str],
) -> None:
    """discussion PUT is a FULL overwrite — old content is replaced, not appended.

    This is the locked decision-2 difference: discussion writes mirror notes
    (whole-file overwrite), they do NOT read-existing-and-concat.
    """
    vault, paper_id = vault_with_paper
    md_path = _plant_md(
        vault,
        paper_id,
        "discussion.md",
        discussion_scaffold(paper_id) + "\nold turn\n",
    )

    new_text = discussion_scaffold(paper_id) + "\nfresh single turn\n"
    resp = _client(vault).put(
        f"/api/paper/{paper_id}/discussion", json={"text": new_text}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "bytes": len(new_text.encode("utf-8"))}

    on_disk = md_path.read_text(encoding="utf-8")
    assert on_disk == new_text  # exact overwrite
    assert "old turn" not in on_disk  # NOT old+new concatenation


def test_put_discussion_heals_stripped_format_header(
    vault_with_paper: tuple[Path, str],
) -> None:
    """An edit that dropped the append-format header gets it back on save.

    Mirrors notes' wikilink-reminder heal: the header is the contract the next
    writer reads, so the GUI must not be the way a paper loses it.
    """
    vault, paper_id = vault_with_paper
    md_path = _plant_md(
        vault, paper_id, "discussion.md", discussion_scaffold(paper_id)
    )

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/discussion",
        json={"text": "# Discussion log for x\n\nheader torn out\n"},
    )
    assert resp.status_code == 200
    on_disk = md_path.read_text(encoding="utf-8")
    assert has_discussion_reminder(on_disk)
    assert "header torn out" in on_disk
    # The notes nudge stays a notes thing — discussion carries its own.
    assert WIKILINK_REMINDER not in on_disk


def test_put_discussion_bad_id_404(vault_with_paper: tuple[Path, str]) -> None:
    vault, _ = vault_with_paper
    resp = _client(vault).put("/api/paper/foo..bar/discussion", json={"text": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invalid paper id: 'foo..bar'."


def test_put_discussion_creates_when_absent(
    vault_with_paper: tuple[Path, str],
) -> None:
    """discussion.md is CREATE-or-overwrite. ``lit add`` scaffolds it for every
    new paper, but a paper predating the scaffold (and not yet backfilled by
    ``lit health-check --fix``) still has none — the first GUI save creates it,
    format header and all.
    """
    vault, paper_id = vault_with_paper
    disc_path = vault / "papers" / paper_id / "discussion.md"
    assert not disc_path.exists()

    resp = _client(vault).put(
        f"/api/paper/{paper_id}/discussion",
        json={"text": "# Discussion\n\nfirst thought\n"},
    )
    assert resp.status_code == 200
    on_disk = disc_path.read_text(encoding="utf-8")
    assert "first thought" in on_disk
    assert has_discussion_reminder(on_disk)
    assert WIKILINK_REMINDER not in on_disk


def test_put_md_unknown_paper_404(vault_with_paper: tuple[Path, str]) -> None:
    """A valid-format id with no ``papers/{id}/`` dir → 404 for both md endpoints:
    create-or-overwrite still requires the paper to exist (no stray files)."""
    vault, _ = vault_with_paper
    ghost = "2099_Nobody_Missing"
    assert not (vault / "papers" / ghost).exists()
    for doc in ("notes", "discussion"):
        resp = _client(vault).put(f"/api/paper/{ghost}/{doc}", json={"text": "x"})
        assert resp.status_code == 404, doc
        assert resp.json()["detail"] == f"No such paper: {ghost!r}."
    assert not (vault / "papers" / ghost).exists()


def test_put_discussion_empty_text_400(
    vault_with_paper: tuple[Path, str],
) -> None:
    vault, paper_id = vault_with_paper
    md_path = _plant_md(vault, paper_id, "discussion.md", "# Discussion\n\nseed\n")
    before = md_path.read_text(encoding="utf-8")

    resp = _client(vault).put(f"/api/paper/{paper_id}/discussion", json={"text": ""})
    assert resp.status_code == 400
    assert md_path.read_text(encoding="utf-8") == before


def test_put_md_leaves_no_staging_residue(
    vault_with_paper: tuple[Path, str],
) -> None:
    """A clean staged_write commit removes its op dir for the md writes too."""
    vault, paper_id = vault_with_paper
    _plant_md(vault, paper_id, "notes.md", "# Notes\n\nseed\n")
    _plant_md(vault, paper_id, "discussion.md", "# Discussion\n\nseed\n")

    client = _client(vault)
    assert (
        client.put(
            f"/api/paper/{paper_id}/notes", json={"text": "# N\n\nbody\n"}
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/paper/{paper_id}/discussion", json={"text": "# D\n\nbody\n"}
        ).status_code
        == 200
    )

    staging = vault / ".litman-staging"
    leftover = [p for p in staging.iterdir()] if staging.is_dir() else []
    assert leftover == []
