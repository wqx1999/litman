"""A Windows save refused because another program holds the file says so first.

Windows refuses to replace a file another program has open, and often says
"access denied" (code 5) for it, as a read-only file or a permission does. An
agent reading the log line ("... (拒绝访问) ... or make it writable") went
after ACLs and the read-only bit. litman clears the read-only bit of a file it
locks before replacing it, so a refused replace of such a file is a program
holding it, and the line now says that first and keeps Windows' words as the
evidence. Anything else keeps the general line.

The platform is faked where litman reads it (``replace._platform``), never
through ``sys.platform``; the refusals are built by hand, so every case runs on
every platform.
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
from litman.core import atomic
from litman.core import replace as replace_mod
from litman.core.locking import lock_truth_file
from litman.server import create_app
from litman.server.errors import write_refused
from tests.server._client import TestClient

PDF = b"%PDF-1.4\n% annotated\n"
ORIGINAL = b"%PDF-1.4\n% original\n"
PAPER = "2024_Foo_Bar"


def _held(target: str, reason: str, again: str = "save again") -> str:
    return (
        f"Another program has {target} open, so it could not be saved (Windows said: "
        f"{reason}); nothing was changed. Close that program, then {again}."
    )


def _general(target: str, reason: str, again: str = "save again") -> str:
    return (
        f"{target} could not be saved ({reason}); nothing was changed. Close any program "
        f"that has it open, or make it writable, then {again}."
    )


def _refusal(
    winerror: int | None,
    src: str | None = None,
    dst: str | None = None,
    *,
    reason: str = "Access is denied",
    refused: str | None = None,
) -> PermissionError:
    """A refusal as Windows raises it; ``refused`` as a staged write marks it."""
    err = PermissionError(errno.EACCES, reason, src, None, dst)
    if winerror is not None:
        err.winerror = winerror  # type: ignore[attr-defined]  # Windows sets it itself
    if refused is not None:
        setattr(err, atomic._REFUSED_FILE_ATTR, refused)
    return err


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replace_mod, "_platform", lambda: "win32")
    monkeypatch.setattr(replace_mod, "_BUDGET_SECONDS", 0)


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "papers" / PAPER).mkdir(parents=True)
    return tmp_path


PDF_REL = f"papers/{PAPER}/paper.pdf"
NOTES_REL = f"papers/{PAPER}/notes.md"


@pytest.mark.usefixtures("on_windows")
@pytest.mark.parametrize("winerror", [32, 33])
def test_a_sharing_or_lock_violation_says_another_program_has_it(
    tmp_path: Path, winerror: int
) -> None:
    """32 / 33 come only from a program holding the file, whichever file it is."""
    err = _refusal(winerror, reason="The process cannot access the file", refused=NOTES_REL)

    out = write_refused(err, _vault(tmp_path), NOTES_REL, again="save again")

    assert out.detail == _held(NOTES_REL, "The process cannot access the file")


@pytest.mark.usefixtures("on_windows")
def test_access_denied_replacing_a_file_litman_unlocked_says_another_program_has_it(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    err = _refusal(5, "staged", str(vault / PDF_REL), reason="拒绝访问", refused=PDF_REL)

    out = write_refused(err, vault, PDF_REL, "pdf-not-saved", again="save again")

    assert (out.status_code, out.code) == (500, "pdf-not-saved")
    assert out.detail == _held(PDF_REL, "拒绝访问")


@pytest.mark.usefixtures("on_windows")
def test_access_denied_on_a_file_litman_does_not_lock_keeps_the_general_line(
    tmp_path: Path,
) -> None:
    """notes.md has no lock of litman's: its read-only bit may be the user's."""
    vault = _vault(tmp_path)
    err = _refusal(5, "staged", str(vault / NOTES_REL), refused=NOTES_REL)

    out = write_refused(err, vault, NOTES_REL, again="save again")

    assert out.detail == _general(NOTES_REL, "Access is denied")


@pytest.mark.usefixtures("on_windows")
def test_access_denied_outside_the_replace_keeps_the_general_line(tmp_path: Path) -> None:
    """An error naming one file is not the replace: clearing the lock itself failed."""
    vault = _vault(tmp_path)
    err = _refusal(5, str(vault / PDF_REL), refused=PDF_REL)

    out = write_refused(err, vault, PDF_REL, again="save again")

    assert out.detail == _general(PDF_REL, "Access is denied")


@pytest.mark.usefixtures("on_windows")
def test_a_plain_permission_error_keeps_the_general_line(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    err = _refusal(None, "staged", str(vault / PDF_REL), reason="Permission denied",
                   refused=PDF_REL)

    out = write_refused(err, vault, PDF_REL, again="save again")

    assert out.detail == _general(PDF_REL, "Permission denied")


@pytest.mark.parametrize("winerror", [5, 32])
def test_off_windows_the_line_is_the_general_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    monkeypatch.setattr(replace_mod, "_platform", lambda: "linux")
    vault = _vault(tmp_path)
    err = _refusal(winerror, "staged", str(vault / PDF_REL), refused=PDF_REL)

    out = write_refused(err, vault, PDF_REL, again="save again")

    assert out.detail == _general(PDF_REL, "Access is denied")


# ------------------------------------------------------------- through the route


def _log() -> list[dict[str, Any]]:
    result = CliRunner().invoke(cli, ["gui-log", "--format", "json"])
    assert result.exit_code == 0, result.output
    entries: list[dict[str, Any]] = json.loads(result.stdout)
    return entries


@pytest.mark.usefixtures("on_windows")
def test_a_pdf_save_windows_refuses_as_held_says_so_in_the_log(
    vault_with_paper: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real save path: staged write, lock cleared, the replace refused with code 5."""
    vault, paper_id = vault_with_paper
    pdf = vault / "papers" / paper_id / "paper.pdf"
    pdf.write_bytes(ORIGINAL)
    lock_truth_file(pdf)
    resolved = pdf.resolve()
    real = os.replace

    def replace(src: Any, dst: Any) -> None:
        if ".litman-staging" in str(src) and Path(dst).resolve() == resolved:
            raise _refusal(5, str(src), str(dst))
        real(src, dst)

    with monkeypatch.context() as held:
        held.setattr(os, "replace", replace)
        resp = TestClient(create_app(vault)).put(
            f"/api/paper/{paper_id}/pdf-annotations", content=PDF
        )

    rel = f"papers/{paper_id}/paper.pdf"
    assert (resp.status_code, resp.json()["code"]) == (500, "pdf-not-saved")
    assert resp.json()["detail"] == _held(rel, "Access is denied")
    [entry] = _log()
    assert (entry["code"], entry["text"]) == ("pdf-not-saved", resp.json()["detail"])
    assert pdf.read_bytes() == ORIGINAL
    assert not pdf.stat().st_mode & stat.S_IWRITE
