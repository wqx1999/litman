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
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from rich.console import Console

import litman
from litman.cli import cli
from litman.core import confirm as confirm_mod
from litman.core import supplementary as si_mod
from litman.core.document import find_paper, list_papers
from litman.core.library import create_vault
from litman.core.locking import unlock_truth_file
from litman.core.portable_link import make_portable_link
from litman.core.views import write_index
from litman.exceptions import (
    ModifyError,
    PaperNotFoundError,
    SupplementaryError,
)

_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX read-only bit semantics"
)
_undecodable_names = pytest.mark.skipif(
    sys.platform != "linux" or sys.getfilesystemencoding() != "utf-8",
    reason="needs Linux (APFS refuses a file name that is not UTF-8; a Windows "
    "name is UTF-16) and a UTF-8 file-name encoding, under which a bad byte "
    "becomes a lone surrogate",
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


@_undecodable_names
def test_add_si_records_an_undecodable_name_as_text_that_reads_back(
    vault: Path, tmp_path: Path
) -> None:
    """A file name that is not UTF-8 reaches Python with a lone surrogate per
    bad byte. Recorded as is, it left a metadata.yaml that no longer parsed
    (lit show failed, the GUI's paper page answered 500). The record, the
    verdict and the SI ready line carry U+FFFD instead; the move still finds
    the file by its real name."""
    raw = b"ci9b00237_si_002_caf\xe9.xlsx"
    sheet = _blob(tmp_path, os.fsdecode(raw))
    assert raw in os.listdir(os.fsencode(tmp_path))  # the name on disk is not UTF-8
    shown = "ci9b00237_si_002_caf\ufffd.xlsx"

    result = _run(vault, "add", "--si", A_ID, str(sheet))

    assert result.exit_code == 0, result.output
    assert [r["original"] for r in _records(vault)] == [shown]
    assert result.stdout.splitlines() == [
        "SI ready: 1 file(s)",
        f"{_si_dir(vault) / 'si-01.xlsx'}\t{shown}",
    ]
    assert f"✓ {shown} (" in result.stderr
    assert not sheet.exists()


def _run_lit(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``lit ARGS --library VAULT`` as its own process, through the real
    ``main()`` and the streams it sets up (CliRunner swaps both out): piped,
    80 columns, no forced colour."""
    env = os.environ.copy()
    env.pop("FORCE_COLOR", None)
    env["COLUMNS"] = "80"
    src_dir = Path(litman.__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    # Decoded strictly: what lit writes must be valid UTF-8.
    return subprocess.run(
        [sys.executable, "-m", "litman", *args, "--library", str(vault)],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )


@_undecodable_names
def test_add_si_of_an_undecodable_name_prints_through_the_real_streams(
    vault: Path, tmp_path: Path
) -> None:
    """Through the real main(), which re-encodes both streams as UTF-8: the
    verdict line used to raise on the lone surrogate there — a traceback, and
    nothing added. It prints the name the record keeps."""
    sheet = _blob(tmp_path, os.fsdecode(b"ci9b00237_si_002_caf\xe9.xlsx"))
    shown = "ci9b00237_si_002_caf\ufffd.xlsx"

    proc = _run_lit(vault, "add", "--si", A_ID, str(sheet))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    assert f"✓ {shown} (" in proc.stderr
    assert proc.stdout.splitlines() == [
        "SI ready: 1 file(s)",
        f"{_si_dir(vault) / 'si-01.xlsx'}\t{shown}",
    ]


@_undecodable_names
def test_an_error_through_the_real_main_prints_as_one_line(
    vault: Path, tmp_path: Path
) -> None:
    """main()'s error line, piped: not folded at column 80, a lowercase [tag]
    in the path kept as text rather than eaten as markup, and the path's
    undecodable byte printed as an escape rather than raising in the handler."""
    missing = (
        tmp_path / "Downloads" / "supporting-information-2019_Yang_Concepts"
        / os.fsdecode(b"ci9b00237_si_001[final]_caf\xe9.pdf")
    )
    shown = str(missing).encode("utf-8", "backslashreplace").decode("utf-8")
    assert shown.endswith("[final]_caf\\udce9.pdf")
    assert len(shown) > 80

    proc = _run_lit(vault, "add", "--si", A_ID, str(missing))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.stdout == f"error: No such file: {shown}\n"
    assert "Traceback" not in proc.stderr


@_undecodable_names
def test_health_check_reports_an_undecodable_stray_through_the_real_streams(
    vault: Path,
) -> None:
    """The report names a stray in supplementary/ by its name on disk. With
    stdout strict UTF-8, the line naming one with an undecodable byte raised
    in the middle of the report; it shows the byte as U+FFFD instead, the way
    ``lit add --si`` names such a file."""
    si = _si_dir(vault)
    si.mkdir()
    raw = b"ci9b00237_si_003_caf\xe9.xlsx"
    _blob(si, os.fsdecode(raw))
    assert raw in os.listdir(os.fsencode(si))  # the name on disk is not UTF-8

    proc = _run_lit(vault, "health-check")

    # No exit-code check: that is the business of whatever else it finds.
    assert "Traceback" not in proc.stdout + proc.stderr
    assert "ci9b00237_si_003_caf\ufffd.xlsx has no record" in proc.stdout


# ---------------------------------------------------------------------------
# lit rm PAPER --si NAME: deleted for good, added again to get it back
# ---------------------------------------------------------------------------


def _add_two(vault: Path, tmp_path: Path) -> None:
    one = _blob(tmp_path, "ci9b00237_si_001.csv", b"1,2\n")
    two = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"sheet")
    result = _run(vault, "add", "--si", A_ID, str(one), str(two))
    assert result.exit_code == 0, result.output


def _cite_si_02(vault: Path) -> None:
    """notes.md cites si-02 once, discussion.md on two lines."""
    paper = vault / "papers" / A_ID
    (paper / "notes.md").write_text(
        "# Notes\n\nsi-02.xlsx（ci9b00237_si_002.xlsx）\n", encoding="utf-8"
    )
    (paper / "discussion.md").write_text(
        "# Discussion log\n\n## 2026-10-20 10:00\n"
        "si-02.xlsx（ci9b00237_si_002.xlsx）, Table 3\nand si-02 again\n",
        encoding="utf-8",
    )


def test_rm_si_deletes_and_says_how_to_add_it_again(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)

    removed = _run(vault, "rm", A_ID, "--si", "si-02", "--yes")

    assert removed.exit_code == 0, removed.output
    assert removed.stdout.splitlines() == [
        "✓ Deleted si-02.xlsx (ci9b00237_si_002.xlsx)",
        f"To add it again: lit add --si {A_ID}",
        "metadata.yaml + INDEX.json updated.",
    ]
    assert not (_si_dir(vault) / "si-02.xlsx").exists()
    assert [r["file"] for r in _records(vault)] == ["si-01.csv"]
    assert (vault / "papers" / A_ID / "paper.pdf").is_file()  # the paper stays
    # No trash for SI.
    assert "(trash is empty)" in _run(vault, "trash", "list").output

    again = _blob(tmp_path, "ci9b00237_si_002.xlsx", b"sheet")
    back = _run(vault, "add", "--si", A_ID, str(again))
    assert back.exit_code == 0, back.output
    # The number is free again, and the same file gets it back.
    assert back.stdout.splitlines()[1] == (
        f"{_si_dir(vault) / 'si-02.xlsx'}\tci9b00237_si_002.xlsx"
    )


def test_rm_si_with_yes_still_says_where_the_notes_cite_it(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    _cite_si_02(vault)

    removed = _run(vault, "rm", A_ID, "--si", "si-02", "--si", "si-01", "--yes")

    assert removed.exit_code == 0, removed.output
    assert removed.stdout.splitlines() == [
        "✓ Deleted si-02.xlsx (ci9b00237_si_002.xlsx)",
        "  still cited 1× in notes.md and 2× in discussion.md — lit health-check "
        "lists the lines",
        "✓ Deleted si-01.csv (ci9b00237_si_001.csv)",
        f"To add them again: lit add --si {A_ID}",
        "metadata.yaml + INDEX.json updated.",
    ]
    assert "supplementary" not in find_paper(vault, A_ID)


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
    assert result.stdout.splitlines() == [
        f"Would delete from {A_ID} (dry-run):",
        "  si-01.csv (ci9b00237_si_001.csv)",
        "  si-02.xlsx (ci9b00237_si_002.xlsx)",
        "Dry-run only — nothing deleted. Drop --dry-run to delete for real.",
    ]
    assert len(_records(vault)) == 2
    assert sorted(os.listdir(_si_dir(vault))) == ["si-01.csv", "si-02.xlsx"]


def test_rm_si_with_yes_says_when_a_notes_file_went_unchecked(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    (vault / "papers" / A_ID / "notes.md").write_bytes(b"si-02 caf\xe9\n")

    removed = _run(vault, "rm", A_ID, "--si", "si-02", "--yes")

    assert removed.exit_code == 0, removed.output
    assert removed.stdout.splitlines() == [
        "✓ Deleted si-02.xlsx (ci9b00237_si_002.xlsx)",
        "  couldn't check notes.md for citations (UnicodeDecodeError)",
        f"To add it again: lit add --si {A_ID}",
        "metadata.yaml + INDEX.json updated.",
    ]


def test_rm_si_dry_run_lists_citations_and_a_notes_file_it_cannot_read(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    _cite_si_02(vault)
    (vault / "papers" / A_ID / "notes.md").write_bytes(b"si-02 caf\xe9\n")

    result = _run(vault, "rm", A_ID, "--si", "si-02", "--dry-run")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"Would delete from {A_ID} (dry-run):",
        "  si-02.xlsx (ci9b00237_si_002.xlsx)",
        "  si-02.xlsx is cited 2× in discussion.md",
        "  couldn't check notes.md for citations (UnicodeDecodeError)",
        "Dry-run only — nothing deleted. Drop --dry-run to delete for real.",
    ]
    assert len(_records(vault)) == 2


def test_rm_si_names_a_file_with_no_original_bare(
    vault: Path, tmp_path: Path
) -> None:
    _add_two(vault, tmp_path)
    meta_file = vault / "papers" / A_ID / "metadata.yaml"
    unlock_truth_file(meta_file)
    lines = meta_file.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if "original: ci9b00237_si_001.csv" not in line]
    assert len(kept) == len(lines) - 1
    meta_file.write_text("".join(kept), encoding="utf-8")

    result = _run(vault, "rm", A_ID, "--si", "si-01", "--dry-run")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[1] == "  si-01.csv"


def test_rm_si_needs_yes_without_a_terminal(vault: Path, tmp_path: Path) -> None:
    _add_two(vault, tmp_path)
    result = _run(vault, "rm", A_ID, "--si", "si-01", input="y\n")
    assert result.exit_code != 0
    assert "pass --yes" in result.output
    assert len(_records(vault)) == 2


def test_rm_si_confirm_lists_the_citations(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(confirm_mod, "_stdin_is_tty", lambda: True)
    # Pinned to a stock terminal's 80 columns (rich caches a console's width
    # the first time anything reads it): the command must not fold.
    monkeypatch.setattr(confirm_mod, "console", Console(width=80))
    _add_two(vault, tmp_path)
    _cite_si_02(vault)

    declined = _run(vault, "rm", A_ID, "--si", "si-02", input="n\n")

    assert declined.exit_code == 0, declined.output
    assert declined.stdout.splitlines()[:5] == [
        f"About to delete from {A_ID}:",
        "  si-02.xlsx (ci9b00237_si_002.xlsx)",
        "  si-02.xlsx is cited 1× in notes.md and 2× in discussion.md",
        "This can't be undone — to get a file back, add it again:",
        f"  lit add --si {A_ID}",
    ]
    assert "Continue? [y/N]" in declined.stdout
    assert "Aborted. No changes made." in declined.stdout
    assert len(_records(vault)) == 2


def test_rm_si_refuses_a_folder_at_a_recorded_name_before_asking(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(confirm_mod, "_stdin_is_tty", lambda: True)
    _add_two(vault, tmp_path)
    si = _si_dir(vault)
    unlock_truth_file(si / "si-02.xlsx")
    (si / "si-02.xlsx").unlink()
    (si / "si-02.xlsx").mkdir()

    result = _run(vault, "rm", A_ID, "--si", "si-02", input="y\n")

    assert result.exit_code == 1
    assert isinstance(result.exception, SupplementaryError)
    assert "is a folder, not the recorded file — nothing was deleted" in str(
        result.exception
    )
    assert "Continue?" not in result.output
    assert len(_records(vault)) == 2
    assert (si / "si-02.xlsx").is_dir()


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
