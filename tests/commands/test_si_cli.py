"""CLI tests for SI: ``lit add --si``, ``lit rm --si``, ``lit show``, ``lit
trash`` and ``lit modify`` around a paper's supplementary files.

Files offered as SI sit in ``tmp_path`` (a download folder outside the vault);
PDFs come from the ``make_text_pdf`` fixture. The verification rules
themselves are covered in tests/core/test_supplementary.py — here it is the
command contract: stdout shape, exit codes, prompts, ``--force``, and that no
path writes anything when it refuses.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from litman.cli import cli
from litman.core import confirm as confirm_mod
from litman.core import supplementary as si_mod
from litman.core.document import find_paper, list_papers
from litman.core.library import create_vault
from litman.core.locking import unlock_truth_file
from litman.core.portable_link import make_portable_link
from litman.core.trash import TRASH_DIRNAME
from litman.core.views import write_index
from litman.exceptions import (
    ModifyError,
    PaperNotFoundError,
    SupplementaryError,
)

_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)

A_ID = "2019_Yang_Concepts"
A_DOI = "10.1021/acs.jcim.9b00237"
A_TITLE = "Concepts of Artificial Intelligence for Computer-Assisted Drug Discovery"
B_ID = "2023_Zhong_Peptides"
B_DOI = "10.1038/s41467-023-38851-5"
B_TITLE = "Deep generative design of cyclic peptides binding protein targets"

MakePdf = Callable[..., Path]


def _write_paper(
    vault: Path, make_text_pdf: MakePdf, paper_id: str, doi: str, title: str
) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\n"
        f"title: {title}\n"
        "authors:\n  - Doe, Jane\n"
        "year: 2020\n"
        f"doi: {doi}\n"
        "status: inbox\n"
        "created-at: '2026-01-01T00:00:00+00:00'\n"
        "updated-at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    article = make_text_pdf(
        [[title, "Jane Doe", "Abstract: an article body about its own topic."]],
        name=f"{paper_id}-article.pdf",
    )
    shutil.move(str(article), str(paper_dir / "paper.pdf"))


@pytest.fixture
def vault(tmp_path: Path, make_text_pdf: MakePdf) -> Path:
    v = create_vault(tmp_path)
    _write_paper(v, make_text_pdf, A_ID, A_DOI, A_TITLE)
    _write_paper(v, make_text_pdf, B_ID, B_DOI, B_TITLE)
    write_index(v, list_papers(v))
    return v


def _blob(tmp_path: Path, name: str, payload: bytes = b"PK\x03\x04 sheet") -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _si_pdf(make_text_pdf: MakePdf, name: str, title: str) -> Path:
    return make_text_pdf(
        [["Supporting Information", title, "Table S1 hyperparameters per run"]],
        name=name,
    )


def _run(vault: Path, *args: str, input: str | None = None) -> Result:
    return CliRunner().invoke(cli, [*args, "--library", str(vault)], input=input)


def _si_dir(vault: Path, paper_id: str = A_ID) -> Path:
    return vault / "papers" / paper_id / "supplementary"


def _records(vault: Path, paper_id: str = A_ID) -> list[dict]:
    return find_paper(vault, paper_id).get("supplementary") or []


# ---------------------------------------------------------------------------
# lit add --si PAPER FILE...
# ---------------------------------------------------------------------------


def test_add_si_moves_files_in_and_prints_only_the_paths(
    vault: Path, tmp_path: Path, make_text_pdf: MakePdf
) -> None:
    pdf = _si_pdf(make_text_pdf, "ci9b00237_si_001.pdf", A_TITLE)
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")

    result = _run(vault, "add", "--si", A_ID, str(sheet), str(pdf))

    assert result.exit_code == 0, result.output
    # Numbered in publisher order (_si_001 before _si_002), not argument
    # order — and listed by number, in stdout and in the metadata alike, each
    # path followed by a tab and the name the file was downloaded under.
    first, second = _si_dir(vault) / "si-01.pdf", _si_dir(vault) / "si-02.xlsx"
    assert result.stdout.splitlines() == [
        "SI ready: 2 file(s)",
        f"{first}\tci9b00237_si_001.pdf",
        f"{second}\tci9b00237_si_002.xlsx",
    ]
    assert "✓ ci9b00237_si_002.xlsx" in result.stderr
    assert first.is_file() and second.is_file()
    assert not pdf.exists() and not sheet.exists()  # moved, like `lit add`
    records = _records(vault)
    assert [r["file"] for r in records] == ["si-01.pdf", "si-02.xlsx"]
    assert all(
        set(r) == {"file", "original", "sha256", "bytes", "added", "check"}
        for r in records
    )


@_posix_only
def test_add_si_files_are_read_only(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    assert _run(vault, "add", "--si", A_ID, str(sheet)).exit_code == 0
    assert not os.access(_si_dir(vault) / "si-01.xlsx", os.W_OK)


def test_add_si_takes_a_paper_substring(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    result = _run(vault, "add", "--si", "yang_con", str(sheet))
    assert result.exit_code == 0, result.output
    assert [r["file"] for r in _records(vault)] == ["si-01.xlsx"]


def test_add_si_is_all_or_nothing(
    vault: Path, tmp_path: Path, make_text_pdf: MakePdf
) -> None:
    good = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    other = _si_pdf(make_text_pdf, "41467_2023_38851_MOESM1_ESM.pdf", B_TITLE)

    result = _run(vault, "add", "--si", A_ID, str(good), str(other), "--force")

    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert "nothing was added" in str(result.exception)
    assert "✕ 41467_2023_38851_MOESM1_ESM.pdf" in result.stderr
    assert f"looks like the SI of {B_ID}" in result.stderr
    assert result.stdout == ""
    assert not _si_dir(vault).exists()
    assert good.exists() and other.exists()
    assert _records(vault) == []


def test_add_si_cannot_confirm_needs_force_without_a_terminal(
    vault: Path, tmp_path: Path
) -> None:
    sheet = _blob(tmp_path, "Table_S2.xlsx")

    refused = _run(vault, "add", "--si", A_ID, str(sheet))
    assert refused.exit_code == 1
    assert "Pass --force" in str(refused.exception)
    assert "? Table_S2.xlsx" in refused.stderr
    assert sheet.exists() and not _si_dir(vault).exists()

    forced = _run(vault, "add", "--si", A_ID, str(sheet), "--force")
    assert forced.exit_code == 0, forced.output
    assert _records(vault)[0]["check"] == "user"


@pytest.mark.parametrize(("answer", "added"), [("y\n", True), ("n\n", False)])
def test_add_si_cannot_confirm_asks_at_a_terminal(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    added: bool,
) -> None:
    monkeypatch.setattr(confirm_mod, "_stdin_is_tty", lambda: True)
    good = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"good")
    sheet = _blob(tmp_path, "Table_S2.xlsx", b"unsure")

    result = _run(vault, "add", "--si", A_ID, str(good), str(sheet), input=answer)

    assert "Add Table_S2.xlsx anyway" in result.output
    if added:
        assert result.exit_code == 0, result.output
        assert [r["check"] for r in _records(vault)] == ["filename", "user"]
    else:
        # One "no" and the whole batch stays out — the confirmed file too.
        assert result.exit_code == 1
        assert str(result.exception) == "Nothing was added."
        assert _records(vault) == []
        assert good.exists() and sheet.exists()


def test_add_si_large_file_needs_a_yes(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(si_mod, "LARGE_FILE_BYTES", 4)
    big = _blob(tmp_path, "ci9b00237_si_003.zip", b"0123456789")

    refused = _run(vault, "add", "--si", A_ID, str(big))
    assert refused.exit_code == 1
    assert big.exists() and not _si_dir(vault).exists()

    monkeypatch.setattr(confirm_mod, "_stdin_is_tty", lambda: True)
    asked = _run(vault, "add", "--si", A_ID, str(big), input="n\n")
    assert asked.exit_code == 1
    assert "it is 10 B" in asked.output
    assert big.exists()

    forced = _run(vault, "add", "--si", A_ID, str(big), "--force")
    assert forced.exit_code == 0, forced.output
    assert _records(vault)[0]["check"] == "filename"


def test_add_si_force_never_releases_a_refused_file(
    vault: Path, tmp_path: Path
) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"same")
    assert _run(vault, "add", "--si", A_ID, str(sheet)).exit_code == 0
    copy = _blob(tmp_path, "copy.xlsx", b"same")

    result = _run(vault, "add", "--si", A_ID, str(copy), "--force")

    assert result.exit_code == 1
    assert "even with --force" in str(result.exception)
    assert "already in the library" in result.stderr
    assert copy.exists()
    assert len(_records(vault)) == 1


@pytest.mark.parametrize(
    "extra",
    [["--doi", "10.1/x"], ["--id", "2024_X_y"], ["--auto-suffix"], ["--from-llm-json"]],
)
def test_add_si_refuses_ingest_options(
    vault: Path, tmp_path: Path, extra: list[str]
) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    if extra == ["--from-llm-json"]:
        extra = [*extra, str(_blob(tmp_path, "meta.json", b"{}"))]

    result = _run(vault, "add", "--si", A_ID, str(sheet), *extra)

    assert result.exit_code == 2
    assert "--si adds SI to a paper already in the vault; drop" in result.output
    assert extra[0] in result.output
    assert sheet.exists()


def test_add_si_without_files_asks_the_gui_rather_than_a_usage_error(
    vault: Path,
) -> None:
    """No FILE is the third form now (tests/commands/test_si_gui_wait.py):
    with no GUI running it says so, exit 12, not Click's usage error 2.
    --force because this file's articles never mention SI (that is 13)."""
    result = _run(vault, "add", "--si", A_ID, "--force")
    assert result.exit_code == 12, result.output
    assert result.stdout.splitlines()[0] == "No litman GUI page is open for this library"


def test_add_si_missing_file(vault: Path, tmp_path: Path) -> None:
    result = _run(vault, "add", "--si", A_ID, str(tmp_path / "gone.pdf"))
    assert result.exit_code == 1
    assert "No such file" in str(result.exception)


def test_add_si_unknown_paper(vault: Path, tmp_path: Path) -> None:
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")
    result = _run(vault, "add", "--si", "2099_Nobody_Here", str(sheet))
    assert result.exit_code == 1
    assert isinstance(result.exception, PaperNotFoundError)
    assert sheet.exists()


def test_add_si_refuses_a_paper_file_reached_through_a_link(
    vault: Path, tmp_path: Path
) -> None:
    """A project's litman_reflib/<paper> is a folder link into the vault:
    taking B's PDF through it as A's SI would delete B's PDF (mv semantics).
    The guard resolves links before deciding what is inside the library."""
    reflib = tmp_path / "project" / "litman_reflib" / B_ID
    if not make_portable_link(reflib, vault / "papers" / B_ID):
        pytest.skip("this filesystem cannot hold folder links")

    result = _run(vault, "add", "--si", A_ID, str(reflib / "paper.pdf"), "--force")

    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert "inside the library" in str(result.exception)
    assert (vault / "papers" / B_ID / "paper.pdf").is_file()
    assert _records(vault) == []


def test_add_si_refuses_a_linked_supplementary_folder(
    vault: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "Downloads"
    outside.mkdir()
    if not make_portable_link(_si_dir(vault), outside):
        pytest.skip("this filesystem cannot hold folder links")
    sheet = _blob(tmp_path, "ci9b00237_si_002.xlsx")

    result = _run(vault, "add", "--si", A_ID, str(sheet))

    assert result.exit_code == 1
    assert str(result.exception) == (
        f"papers/{A_ID}/supplementary is a link, and litman does not write "
        "through links. Replace it with a real folder, then retry."
    )
    assert os.listdir(outside) == []
    assert sheet.exists()


def test_add_force_without_si_is_a_usage_error(vault: Path, tmp_path: Path) -> None:
    pdf = _blob(tmp_path, "paper.pdf", b"%PDF-1.4\n%%EOF\n")
    result = _run(vault, "add", str(pdf), "--doi", "10.1/x", "--force")
    assert result.exit_code == 2
    assert "Error: --force only applies to --si." in result.output
    assert pdf.exists()


def test_add_help_describes_both_modes() -> None:
    result = CliRunner().invoke(cli, ["add", "--help"])
    assert result.exit_code == 0
    assert "lit add PDF_PATH --doi DOI | --from-llm-json PATH" in result.output
    assert "lit add --si PAPER FILE..." in result.output
    assert "--force" in result.output


# ---------------------------------------------------------------------------
# lit rm PAPER --si NAME, the trash, and putting a file back
# ---------------------------------------------------------------------------


def _add_two(vault: Path, tmp_path: Path) -> None:
    one = _blob(tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    two = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"sheet")
    result = _run(vault, "add", "--si", A_ID, str(one), str(two))
    assert result.exit_code == 0, result.output


def test_rm_si_round_trip_through_the_trash(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)

    removed = _run(vault, "rm", A_ID, "--si", "si-02", "--yes")

    assert removed.exit_code == 0, removed.output
    assert "✓ Moved si-02.xlsx to the trash" in removed.output
    trashed = list((vault / TRASH_DIRNAME / "removed-si" / A_ID).glob("*-si-02.xlsx"))
    assert len(trashed) == 1
    assert f"lit add --si {A_ID} {trashed[0]}" in removed.output
    assert [r["file"] for r in _records(vault)] == ["si-01.csv"]
    assert (vault / "papers" / A_ID / "paper.pdf").is_file()  # the paper stays

    listed = _run(vault, "trash", "list")
    assert listed.exit_code == 0, listed.output
    assert "Removed SI files" in listed.output
    assert "si-02.xlsx" in listed.output
    assert "(ci9b00237_si_002.xlsx)" in listed.output
    assert f"lit add --si {A_ID} {trashed[0]}" in listed.output
    assert "(trash is empty)" not in listed.output

    back = _run(vault, "add", "--si", A_ID, str(trashed[0]))
    assert back.exit_code == 0, back.output
    # A new number: 02 stays retired. The name it was first added under
    # comes back with it.
    assert back.stdout.splitlines()[1] == (
        f"{_si_dir(vault) / 'si-03.xlsx'}\tci9b00237_si_002.xlsx"
    )
    assert not trashed[0].exists()
    assert "(trash is empty)" in _run(vault, "trash", "list").output


def test_rm_si_and_purge_are_exclusive(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    result = _run(vault, "rm", A_ID, "--si", "si-01", "--purge", "--yes")
    assert result.exit_code == 2
    assert "cannot be combined with --purge" in result.output
    assert len(_records(vault)) == 2


def test_rm_si_dry_run_touches_nothing(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    result = _run(vault, "rm", A_ID, "--si", "si-01", "--si", "si-02.xlsx", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Would move to .trash/removed-si/" in result.output
    assert "si-01.csv" in result.output and "si-02.xlsx" in result.output
    assert len(_records(vault)) == 2
    assert not (vault / TRASH_DIRNAME / "removed-si").exists()


def test_rm_si_needs_yes_without_a_terminal(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    result = _run(vault, "rm", A_ID, "--si", "si-01", input="y\n")
    assert result.exit_code != 0
    assert "pass --yes" in result.output
    assert len(_records(vault)) == 2


def test_rm_si_confirms_at_a_terminal(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(confirm_mod, "_stdin_is_tty", lambda: True)
    _add_two(vault, tmp_path)

    declined = _run(vault, "rm", A_ID, "--si", "si-01", input="n\n")
    assert declined.exit_code == 0
    assert "Aborted. No changes made." in declined.output
    assert len(_records(vault)) == 2

    accepted = _run(vault, "rm", A_ID, "--si", "si-01", input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert [r["file"] for r in _records(vault)] == ["si-02.xlsx"]


def test_rm_si_unknown_name(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    result = _run(vault, "rm", A_ID, "--si", "si-07", "--yes")
    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert "has no SI file 'si-07'" in str(result.exception)


def test_rm_si_of_a_file_already_gone_drops_the_record(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    gone = _si_dir(vault) / "si-01.csv"
    unlock_truth_file(gone)
    gone.unlink()

    result = _run(vault, "rm", A_ID, "--si", "si-01", "--yes")

    assert result.exit_code == 0, result.output
    assert "Dropped the record of si-01.csv" in result.output
    assert [r["file"] for r in _records(vault)] == ["si-02.xlsx"]


# ---------------------------------------------------------------------------
# lit show
# ---------------------------------------------------------------------------


def test_show_lists_si_after_pdf_and_notes(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    gone = _si_dir(vault) / "si-01.csv"
    unlock_truth_file(gone)
    gone.unlink()

    result = _run(vault, "show", A_ID)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    si_lines = [line for line in lines if line.startswith("SI:")]
    assert len(si_lines) == 2
    assert str(_si_dir(vault) / "si-01.csv") in si_lines[0]
    assert "(missing!)" in si_lines[0]
    assert str(_si_dir(vault) / "si-02.xlsx") in si_lines[1]
    assert "(ci9b00237_si_002.xlsx, 5 B)" in si_lines[1]
    assert lines.index(si_lines[0]) > next(
        i for i, line in enumerate(lines) if line.startswith("Notes:")
    )


def test_show_marks_an_si_file_it_cannot_look_at(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python 3.12+ lets is_file() raise for a file it may not stat (EACCES).
    That line says (unreadable!); the others print as usual. Played by
    raising for that one file rather than by chmod, which Windows ignores."""
    _add_two(vault, tmp_path)
    real_is_file = Path.is_file

    def is_file(self: Path, *args: object, **kwargs: object) -> bool:
        if self.parent.name == "supplementary" and self.name.startswith("si-01."):
            raise PermissionError(13, "Permission denied (mocked)", str(self))
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    result = _run(vault, "show", A_ID)

    assert result.exit_code == 0, result.output
    si_lines = [line for line in result.output.splitlines() if line.startswith("SI:")]
    assert len(si_lines) == 2
    assert str(_si_dir(vault) / "si-01.csv") in si_lines[0]
    assert "(unreadable!)" in si_lines[0]
    assert "(missing!)" not in si_lines[0]
    assert str(_si_dir(vault) / "si-02.xlsx") in si_lines[1]
    assert "(unreadable!)" not in si_lines[1]
    assert "(missing!)" not in si_lines[1]


def test_show_without_si_prints_no_si_line(vault: Path) -> None:
    result = _run(vault, "show", A_ID)
    assert result.exit_code == 0, result.output
    assert "SI:" not in result.output


def test_show_with_a_malformed_field_says_so(vault: Path) -> None:
    meta_path = vault / "papers" / A_ID / "metadata.yaml"
    with meta_path.open("a", encoding="utf-8") as f:
        f.write("supplementary: si-01.pdf\n")
    result = _run(vault, "show", A_ID)
    assert result.exit_code == 0, result.output
    assert "'supplementary' in metadata.yaml is malformed" in result.output


def test_show_json_carries_supplementary(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    result = _run(vault, "show", A_ID, "--format", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [r["file"] for r in payload["supplementary"]] == ["si-01.csv", "si-02.xlsx"]
    assert payload["supplementary"][1]["original"] == "ci9b00237_si_002.xlsx"


# ---------------------------------------------------------------------------
# lit modify cannot write the field
# ---------------------------------------------------------------------------


def test_modify_refuses_to_set_supplementary(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    before = (vault / "papers" / A_ID / "metadata.yaml").read_bytes()
    result = _run(vault, "modify", A_ID, "--set", "supplementary=")
    assert result.exit_code == 1
    assert isinstance(result.exception, ModifyError)
    assert "lit add --si" in str(result.exception)
    assert (vault / "papers" / A_ID / "metadata.yaml").read_bytes() == before


def test_modify_keeps_supplementary_through_other_edits(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    before = _records(vault)
    result = _run(vault, "modify", A_ID, "--set", "status=deep-read")
    assert result.exit_code == 0, result.output
    assert _records(vault) == before
