"""A write the disk refuses, through the routes: what the page and the log are told,
and what is on disk afterwards.

The failure the screenshot run found: a save into a read-only paper folder
answered "nothing was changed", lost the file's read-only lock, and the next
server start landed the save after all. Now a write refused at its first file
changes nothing and stays that way; one refused after part of it landed says
so (``write-partial``), and the next start finishes it.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner

from litman.cli import cli
from litman.core.atomic import ensure_vault_recovered
from litman.core.locking import lock_truth_file
from litman.server import create_app
from tests.server._client import TestClient

PDF = b"%PDF-1.4\n% annotated\n"
ORIGINAL = b"%PDF-1.4\n% original\n"


def _log() -> list[dict[str, Any]]:
    result = CliRunner().invoke(cli, ["gui-log", "--format", "json"])
    assert result.exit_code == 0, result.output
    entries: list[dict[str, Any]] = json.loads(result.stdout)
    return entries


def _locked(path: Path) -> bool:
    return not path.stat().st_mode & stat.S_IWRITE


def _ops(vault: Path) -> list[str]:
    staging = vault / ".litman-staging"
    return sorted(p.name for p in staging.iterdir()) if staging.is_dir() else []


def _refuse(monkeypatch: Any, target: Path) -> None:
    """The disk refuses to put ``target`` in place, as a read-only folder does."""
    resolved = target.resolve()
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if ".litman-staging" in str(src) and Path(dst).resolve() == resolved:
            raise PermissionError(errno.EACCES, "Permission denied", str(dst))
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)


@pytest.fixture
def paper(vault_with_paper: tuple[Path, str]) -> tuple[Path, str]:
    vault, paper_id = vault_with_paper
    folder = vault / "papers" / paper_id
    (folder / "paper.pdf").write_bytes(ORIGINAL)
    lock_truth_file(folder / "paper.pdf")
    lock_truth_file(folder / "metadata.yaml")
    return vault, paper_id


def test_a_refused_pdf_save_keeps_the_lock_and_the_next_start_lands_nothing(
    paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, paper_id = paper
    pdf = vault / "papers" / paper_id / "paper.pdf"

    with monkeypatch.context() as held:
        _refuse(held, pdf)
        resp = TestClient(create_app(vault)).put(
            f"/api/paper/{paper_id}/pdf-annotations", content=PDF
        )

    assert (resp.status_code, resp.json()["code"]) == (500, "pdf-not-saved")
    assert resp.json()["detail"] == (
        f"papers/{paper_id}/paper.pdf could not be saved (Permission denied); nothing was "
        "changed. Close any program that has it open, or make it writable, then save again."
    )
    assert _locked(pdf)
    assert _ops(vault) == []
    # The next server start recovers the library first: nothing is replayed.
    assert ensure_vault_recovered(vault) == []
    assert pdf.read_bytes() == ORIGINAL


def test_a_metadata_write_refused_at_its_first_file_changes_nothing(
    paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, paper_id = paper
    meta = vault / "papers" / paper_id / "metadata.yaml"
    before = meta.read_bytes()

    with monkeypatch.context() as held:
        _refuse(held, meta)
        resp = TestClient(create_app(vault)).put(
            f"/api/paper/{paper_id}/metadata", json={"set": {"status": "skim"}}
        )

    # No code: the page shows its own fixed line (改动未保存).
    assert (resp.status_code, resp.json()["code"]) == (500, None)
    assert resp.json()["detail"] == (
        f"papers/{paper_id}/metadata.yaml could not be saved (Permission denied); nothing "
        "was changed. Close any program that has it open, or make it writable, then try "
        "again."
    )
    [entry] = _log()
    assert (entry["kind"], entry["code"], entry["text"]) == ("error", None, resp.json()["detail"])
    assert _locked(meta)
    assert _ops(vault) == []
    assert ensure_vault_recovered(vault) == []
    assert meta.read_bytes() == before


def test_a_metadata_write_refused_after_part_of_it_landed_says_write_partial(
    paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, paper_id = paper
    meta = vault / "papers" / paper_id / "metadata.yaml"

    with monkeypatch.context() as held:
        _refuse(held, vault / "INDEX.json")
        resp = TestClient(create_app(vault)).put(
            f"/api/paper/{paper_id}/metadata", json={"set": {"status": "skim"}}
        )

    assert (resp.status_code, resp.json()["code"]) == (500, "write-partial")
    text = (
        "The change was only partly saved: INDEX.json could not be written (Permission "
        "denied). litman completes it the next time it opens this library."
    )
    assert resp.json()["detail"] == text
    [entry] = _log()
    assert (entry["route"], entry["code"], entry["text"]) == (
        f"PUT /api/paper/{paper_id}/metadata", "write-partial", text,
    )
    # metadata.yaml landed and is read-only again; the rest waits for the next start.
    assert "status: skim" in meta.read_text(encoding="utf-8")
    assert _locked(meta)
    assert _ops(vault) == [f"modify-{paper_id}"]

    [result] = ensure_vault_recovered(vault)

    assert result.kind == "rolled_forward"
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert '"status": "skim"' in json.dumps(index)
