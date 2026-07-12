"""`lit open` / `lit show` with no argument open the paper you last engaged with.

After a reading session the paper you want back is nearly always the one you
just closed, and retyping its id to get to it is pure friction. Both commands
used to answer "No paper specified."

The paper they pick MUST be the one `lit list --sort recent` puts at the top —
one ranking, `query.recency_key`, never a second sort path (invariant #16 in
spirit: the same key backs the CLI, the web UI's reading list, and this).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from litman.cli import cli
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.paper_lookup import most_recent_paper_id
from litman.core.views import write_index

# (id, updated-at) — Bbb is the most recent, and it is neither first nor last
# by id, so an id-ordering bug cannot pass by accident.
_PAPERS = [
    ("2024_Aaa", "2026-01-01T10:00:00+02:00"),
    ("2024_Bbb", "2026-05-05T10:00:00+02:00"),
    ("2024_Ccc", "2026-03-03T10:00:00+02:00"),
]
_MOST_RECENT = "2024_Bbb"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = create_vault(tmp_path)
    for paper_id, updated in _PAPERS:
        d = v / "papers" / paper_id
        d.mkdir(parents=True)
        (d / "metadata.yaml").write_text(
            f"id: {paper_id}\n"
            f"title: Paper {paper_id}\n"
            "authors: []\n"
            "year: 2024\n"
            "type: research\n"
            "status: inbox\n"
            "priority: B\n"
            "topics: []\n"
            "methods: []\n"
            "data: []\n"
            "projects: []\n"
            "doi:\n"
            "read-date:\n"
            "created-at: '2026-01-01T00:00:00+02:00'\n"
            f"updated-at: '{updated}'\n",
            encoding="utf-8",
        )
        # A zeroed pdf mtime keeps updated-at the deciding signal; recency_key
        # takes the LATER of the two, and a just-written file would win.
        pdf = d / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        os.utime(pdf, (0, 0))

    write_index(v, list_papers(v))
    return v


@pytest.fixture
def empty_vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


# ---------------------------------------------------------------------------
# it agrees with `lit list --sort recent`, which is the whole contract
# ---------------------------------------------------------------------------


def test_the_paper_picked_is_the_one_list_sort_recent_ranks_first(
    vault: Path,
) -> None:
    ranked = json.loads(
        CliRunner()
        .invoke(
            cli,
            ["list", "--sort", "recent", "--format", "json",
             "--library", str(vault)],
        )
        .stdout
    )
    assert ranked[0]["id"] == _MOST_RECENT  # guards the fixture itself
    assert most_recent_paper_id(vault) == ranked[0]["id"]


def test_the_index_and_the_scan_pick_the_same_paper(vault: Path) -> None:
    """The fast path may not disagree with the source of truth."""
    from_index = most_recent_paper_id(vault)

    (vault / "INDEX.json").unlink()  # force the fallback scan
    from_scan = most_recent_paper_id(vault)

    assert from_index == from_scan == _MOST_RECENT


def test_it_does_not_scan_the_vault_when_the_index_is_good(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """updated-at is in the INDEX projection, so this is a single JSON read."""

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("scanned the vault despite a healthy INDEX")

    monkeypatch.setattr("litman.core.paper_lookup.list_papers", _boom)
    assert most_recent_paper_id(vault) == _MOST_RECENT


# ---------------------------------------------------------------------------
# lit show
# ---------------------------------------------------------------------------


def test_show_with_no_argument_shows_the_most_recent(vault: Path) -> None:
    result = CliRunner().invoke(cli, ["show", "--library", str(vault)])
    assert result.exit_code == 0, result.output
    assert _MOST_RECENT in result.stdout


def test_show_says_which_paper_it_picked(vault: Path) -> None:
    """Never leave the user guessing which paper they are looking at."""
    result = CliRunner().invoke(cli, ["show", "--library", str(vault)])
    assert _MOST_RECENT in result.stderr
    assert "most recently engaged" in result.stderr


def test_show_json_with_no_argument_keeps_stdout_pure(vault: Path) -> None:
    """The note goes to stderr: --format json owns stdout."""
    result = CliRunner().invoke(
        cli, ["show", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == _MOST_RECENT


def test_show_with_an_explicit_id_is_unchanged(vault: Path) -> None:
    result = CliRunner().invoke(
        cli, ["show", "2024_Aaa", "--format", "json", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == "2024_Aaa"
    assert result.stderr == "", "an explicit id must not be second-guessed"


def test_show_on_an_empty_vault_still_explains_itself(empty_vault: Path) -> None:
    result = CliRunner().invoke(cli, ["show", "--library", str(empty_vault)])
    assert result.exit_code != 0
    assert "no papers yet" in str(result.exception)


# ---------------------------------------------------------------------------
# lit open
# ---------------------------------------------------------------------------


def test_open_with_no_argument_launches_the_most_recent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[Path] = []

    def _fake_launch(pdf_path: Path, configured: str | None) -> tuple[str, str]:
        launched.append(pdf_path)
        return ("fake-viewer", "config")

    monkeypatch.setattr("litman.commands.open.launch_pdf", _fake_launch)

    result = CliRunner().invoke(cli, ["open", "--library", str(vault)])

    assert result.exit_code == 0, result.output
    assert len(launched) == 1
    assert launched[0] == vault / "papers" / _MOST_RECENT / "paper.pdf"


def test_open_says_which_paper_it_picked(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "litman.commands.open.launch_pdf",
        lambda p, c: ("fake-viewer", "config"),
    )
    result = CliRunner().invoke(cli, ["open", "--library", str(vault)])
    assert "most recently engaged" in result.stderr
    assert _MOST_RECENT in result.stderr


def test_open_with_an_explicit_id_is_unchanged(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[Path] = []

    def _fake_launch(pdf_path: Path, configured: str | None) -> tuple[str, str]:
        launched.append(pdf_path)
        return ("fake-viewer", "config")

    monkeypatch.setattr("litman.commands.open.launch_pdf", _fake_launch)

    result = CliRunner().invoke(
        cli, ["open", "2024_Aaa", "--library", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert launched == [vault / "papers" / "2024_Aaa" / "paper.pdf"]
    assert result.stderr == "", "an explicit id must not be second-guessed"


def test_open_on_an_empty_vault_still_explains_itself(empty_vault: Path) -> None:
    result = CliRunner().invoke(cli, ["open", "--library", str(empty_vault)])
    assert result.exit_code != 0
    assert "no papers yet" in str(result.exception)
