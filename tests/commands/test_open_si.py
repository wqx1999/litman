"""``lit open PAPER --si NAME``: one of the paper's SI files, in this machine's
default app.

The host is faked through ``core.viewer``'s seams — ``_platform``, ``_which``,
``_spawn`` — and the four variables ``open_files_mode`` reads are set
explicitly, whatever the machine running the suite has. No test reaches a real
opener: ``_spawn`` records the argv and stands in for the process, and
``_startfile`` fails the test if reached.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from rich.console import Console

from litman.cli import cli
from litman.commands import open as open_mod
from litman.core import viewer
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.locking import unlock_truth_file
from litman.core.supplementary import SiItem, add_supplementary
from litman.core.views import write_index

A_ID = "2019_Yang_Concepts"
A_DOI = "10.1021/acs.jcim.9b00237"
B_ID = "2023_Zhong_Peptides"
_SSH = "10.0.0.1 50022 10.0.0.2 22"

# original name → what the paper's SI ends up holding
_SI = {
    "ci9b00237_si_001.pdf": b"%PDF-1.4 supporting information\n",
    "ci9b00237_si_002.xlsx": b"PK\x03\x04 sheet",
    "ci9b00237_si_003.exe": b"MZ program",
    "README": b"read me",
}


class _Opener:
    """Stands in for ``viewer._spawn``: records each argv; the process exits
    with ``code`` (``None``: still running when the wait ends)."""

    def __init__(self) -> None:
        self.code: int | None = 0
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str]) -> _Opener:
        self.argvs.append(list(argv))
        return self

    def wait(self, timeout: float | None = None) -> int:
        if self.code is None:
            raise subprocess.TimeoutExpired("opener", timeout or 0)
        return self.code


def _write_paper(vault: Path, paper_id: str, doi: str) -> None:
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    (paper_dir / "metadata.yaml").write_text(
        f"id: {paper_id}\n"
        f"title: Title for {paper_id}\n"
        "authors:\n  - Doe, Jane\n"
        "year: 2020\n"
        f"doi: {doi}\n"
        "status: inbox\n"
        "created-at: '2026-01-01T00:00:00+00:00'\n"
        "updated-at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    pdf = paper_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 the article\n")
    # A zeroed mtime leaves updated-at to say which paper is the most recent:
    # it has whole seconds, and a PDF written within the same second as the
    # SI was added would otherwise outrank it.
    os.utime(pdf, (0, 0))


def _make_vault(parent: Path, paper_id: str = A_ID) -> Path:
    vault = create_vault(parent)
    _write_paper(vault, paper_id, A_DOI)
    _write_paper(vault, B_ID, "10.1038/s41467-023-38851-5")
    write_index(vault, list_papers(vault))
    downloads = parent / "downloads"
    downloads.mkdir()
    items = []
    for original, payload in _SI.items():
        (downloads / original).write_bytes(payload)
        items.append(SiItem(downloads / original, original, approved=True))
    added = add_supplementary(vault, paper_id, items, allow_unverified=False)
    assert [r["file"] for r in added.records] == [
        "si-01.pdf",
        "si-02.xlsx",
        "si-03.exe",
        "si-04",
    ]
    return vault


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return _make_vault(tmp_path)


@pytest.fixture(autouse=True)
def opener(monkeypatch: pytest.MonkeyPatch) -> _Opener:
    """A Linux desktop: a display, xdg-open installed, no SSH session."""
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(viewer, "_platform", lambda: "linux")
    monkeypatch.setattr(
        viewer, "_which", lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None
    )
    fake = _Opener()
    monkeypatch.setattr(viewer, "_spawn", fake)

    def refuse(*args: object) -> None:
        raise AssertionError(f"a test reached os.startfile: {args!r}")

    monkeypatch.setattr(viewer, "_startfile", refuse)
    return fake


def _si(vault: Path, name: str, paper_id: str = A_ID) -> Path:
    return vault / "papers" / paper_id / "supplementary" / name


def _open(vault: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, ["open", *args, "--library", str(vault)])


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("name", ["si-02", "si-2", "si-002", "SI-02", "si-02.xlsx"])
def test_opens_the_si_file_in_the_default_app(
    vault: Path, opener: _Opener, name: str
) -> None:
    result = _open(vault, A_ID, "--si", name)

    assert result.exit_code == 0, result.output
    assert opener.argvs == [["xdg-open", str(_si(vault, "si-02.xlsx"))]]
    assert f"Opened {A_ID} si-02.xlsx (viewer: xdg-open, platform)" in _flat(
        result.output
    )


def test_an_unknown_name_lists_the_papers_si_files(
    vault: Path, opener: _Opener
) -> None:
    meta = vault / "papers" / A_ID / "metadata.yaml"
    unlock_truth_file(meta)
    text = meta.read_text(encoding="utf-8")
    meta.write_text(text.replace("    original: README\n", ""), encoding="utf-8")

    result = _open(vault, A_ID, "--si", "si-99")

    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert lines[0] == f"error: {A_ID} has no SI file 'si-99'. Its SI files:"
    assert lines[1:] == [
        "  si-01.pdf (ci9b00237_si_001.pdf)",
        "  si-02.xlsx (ci9b00237_si_002.xlsx)",
        "  si-03.exe (ci9b00237_si_003.exe)",
        "  si-04 (?)",
    ]
    assert opener.argvs == []


def test_a_paper_without_si_says_how_to_add_it(
    vault: Path, opener: _Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 80 columns, as piped: the line is longer, and must not fold inside the
    # command it hands over.
    monkeypatch.setattr(open_mod, "console", Console(width=80))

    result = _open(vault, B_ID, "--si", "si-01")

    assert result.exit_code == 1
    line = f"error: {B_ID} has no SI files. Run lit add --si {B_ID} to add them."
    assert len(line) > 80
    assert result.output.splitlines() == [line]
    assert opener.argvs == []


@pytest.mark.parametrize("name", ["si-03.exe", "si-04"])
def test_a_file_the_default_app_would_run_is_never_opened(
    vault: Path, opener: _Opener, name: str
) -> None:
    result = _open(vault, A_ID, "--si", name)

    assert result.exit_code == 1
    assert f"error: {name} could be run, not viewed." in result.output
    assert f"SI path: {_si(vault, name)}" in result.output.splitlines()
    assert opener.argvs == []


@pytest.mark.parametrize("variable", ["SSH_CONNECTION", "SSH_CLIENT"])
def test_an_ssh_session_prints_the_path_and_exits_2(
    vault: Path, opener: _Opener, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, _SSH)

    result = _open(vault, A_ID, "--si", "si-02")

    assert result.exit_code == 2
    assert "error: This session has no desktop to open si-02.xlsx on." in result.output
    assert f"SI path: {_si(vault, 'si-02.xlsx')}" in result.output.splitlines()
    assert "Opened" not in result.output
    assert opener.argvs == []


def test_without_a_display_the_opener_says_so(
    vault: Path, opener: _Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside SSH, what is missing is named — here the display."""
    monkeypatch.delenv("DISPLAY")

    result = _open(vault, A_ID, "--si", "si-02")

    assert result.exit_code == 2
    assert result.output.splitlines() == [
        "error: No graphical display detected, so xdg-open cannot open si-02.xlsx. "
        "Run from a session with a display.",
        f"SI path: {_si(vault, 'si-02.xlsx')}",
    ]
    assert opener.argvs == []


def test_without_an_opener_the_opener_is_named(
    vault: Path, opener: _Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Linux desktop without xdg-utils: a display, nothing to open with."""
    monkeypatch.setattr(viewer, "_which", lambda name: None)

    result = _open(vault, A_ID, "--si", "si-02")

    assert result.exit_code == 2
    assert (
        "error: No platform app opener available (xdg-open / wslview missing)."
    ) in result.output
    assert f"SI path: {_si(vault, 'si-02.xlsx')}" in result.output.splitlines()
    assert opener.argvs == []


def test_an_app_that_reports_failure_prints_the_path_and_exits_2(
    vault: Path, opener: _Opener
) -> None:
    opener.code = 3

    result = _open(vault, A_ID, "--si", "si-02")

    assert result.exit_code == 2
    assert "No app opened si-02.xlsx (xdg-open exited 3)." in _flat(result.output)
    assert f"SI path: {_si(vault, 'si-02.xlsx')}" in result.output.splitlines()
    assert "Opened" not in result.output


def test_an_app_still_running_when_the_wait_ends_opened_it(
    vault: Path, opener: _Opener
) -> None:
    opener.code = None
    result = _open(vault, A_ID, "--si", "si-02")
    assert result.exit_code == 0, result.output


def _configure_viewer(vault: Path, command: str) -> None:
    config = vault / "lit-config.yaml"
    text = config.read_text(encoding="utf-8")
    assert "default_pdf_viewer: null" in text
    config.write_text(
        text.replace("default_pdf_viewer: null", f"default_pdf_viewer: {command}"),
        encoding="utf-8",
    )


def test_a_pdf_goes_to_the_configured_viewer_even_over_ssh(
    vault: Path, opener: _Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As for the paper's own PDF: a configured viewer may work where the
    default app cannot (``ssh -X`` to a machine with zathura)."""
    _configure_viewer(vault, "zathura")
    monkeypatch.setenv("SSH_CONNECTION", _SSH)

    result = _open(vault, A_ID, "--si", "si-01")

    assert result.exit_code == 0, result.output
    assert opener.argvs == [["zathura", str(_si(vault, "si-01.pdf"))]]
    assert "(viewer: zathura, configured)" in _flat(result.output)


def test_the_configured_viewer_is_for_pdfs_only(vault: Path, opener: _Opener) -> None:
    _configure_viewer(vault, "zathura")

    result = _open(vault, A_ID, "--si", "si-02")

    assert result.exit_code == 0, result.output
    assert opener.argvs == [["xdg-open", str(_si(vault, "si-02.xlsx"))]]


def test_the_papers_pdf_need_not_be_there(vault: Path, opener: _Opener) -> None:
    pdf = vault / "papers" / A_ID / "paper.pdf"
    unlock_truth_file(pdf)
    pdf.unlink()

    result = _open(vault, A_ID, "--si", "si-02")

    assert result.exit_code == 0, result.output
    assert opener.argvs == [["xdg-open", str(_si(vault, "si-02.xlsx"))]]


def test_without_a_paper_it_opens_the_most_recent_papers_si(
    vault: Path, opener: _Opener
) -> None:
    # Adding the SI made paper A the one engaged with most recently.
    result = _open(vault, "--si", "si-02")

    assert result.exit_code == 0, result.output
    assert "most recently engaged" in result.stderr
    assert A_ID in result.stderr
    assert opener.argvs == [["xdg-open", str(_si(vault, "si-02.xlsx"))]]


def test_the_paper_can_be_named_by_doi(vault: Path, opener: _Opener) -> None:
    result = _open(vault, "--paper-doi", A_DOI, "--si", "si-02")

    assert result.exit_code == 0, result.output
    assert opener.argvs == [["xdg-open", str(_si(vault, "si-02.xlsx"))]]


def test_the_path_line_is_never_folded(
    tmp_path: Path, opener: _Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent's console is 80 columns wide; a folded path is one it cannot
    paste. A Console of its own: rich fixes a console's width at first use."""
    deep = tmp_path / "a-library-folder-with-a-long-name" / "and-another-level-below-it"
    deep.mkdir(parents=True)
    vault = _make_vault(deep)
    path = _si(vault, "si-02.xlsx")
    assert len(f"SI path: {path}") > 80
    monkeypatch.setattr(open_mod, "console", Console(width=80))
    monkeypatch.setenv("SSH_CONNECTION", _SSH)

    result = _open(vault, A_ID, "--si", "si-02")

    assert result.exit_code == 2
    assert f"SI path: {path}" in result.output.splitlines()


# The paper id an end-to-end run saw folded mid-message at 80 columns.
LONG_ID = "2019_Yang_Analyzing-Learned-Molecular"


def test_messages_stay_one_line_each_when_piped(
    tmp_path: Path, opener: _Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Piped, the console is 80 columns wide: a message folded in two reads
    as two messages to whoever takes the output line by line."""
    vault = _make_vault(tmp_path, LONG_ID)
    monkeypatch.setattr(open_mod, "console", Console(width=80))

    opened = _open(vault, LONG_ID, "--si", "si-02")
    line = f"Opened {LONG_ID} si-02.xlsx (viewer: xdg-open, platform)"
    assert len(line) > 80
    assert line in opened.output.splitlines()

    opener.code = 3
    failed = _open(vault, LONG_ID, "--si", "si-02")
    assert failed.exit_code == 2
    assert (
        "error: No app opened si-02.xlsx (xdg-open exited 3). "
        "Install an app for .xlsx files, or open the path below yourself."
    ) in failed.output.splitlines()

    pdf_failed = _open(vault, LONG_ID)
    assert pdf_failed.exit_code == 2
    lines = pdf_failed.output.splitlines()
    assert (
        "error: No app opened paper.pdf (xdg-open exited 3). "
        "Set default_pdf_viewer in lit-config.yaml to a specific viewer."
    ) in lines
    pdf = vault / "papers" / LONG_ID / "paper.pdf"
    assert len(f"PDF path: {pdf}") > 80
    assert f"PDF path: {pdf}" in lines


def test_help_names_the_option() -> None:
    result = CliRunner().invoke(cli, ["open", "--help"])
    assert result.exit_code == 0
    text = _flat(result.output)
    assert "--si NAME" in text
    assert "Open one of the paper's SI files (si-02, or si-02.xlsx)" in text
