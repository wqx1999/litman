"""A list field that is not a list: reported, never read character by character.

``metadata.yaml`` is plain text that people and agents edit by hand, so
``topics: peptide`` (the list dropped) will happen. Read as it stands, a string
iterates as its characters. Before the readers refused such a file, one hand
edit made litman build ``views/by-topic/p/``, ``e/``, ``i/``…, report each
letter as an unregistered topic, white-screen the GUI on that paper, and —
the one that wrote damage back — turn ``related: X`` into the characters of
``X`` on the next ``--add-tag``. A number (``topics: 42``) crashed
``lit health-check`` itself and every later write to any paper.

Now the file counts as broken, the way one whose YAML does not parse does:
left out of the library, named by ``lit health-check`` with the fix, refused
by every write, and harmless to the other papers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from ruamel.yaml import YAML

from litman.cli import cli
from litman.core.code import unbind_repo_from_all_papers
from litman.core.library import create_vault
from litman.core.trash import TRASH_DIRNAME
from litman.exceptions import CorruptMetadataError
from litman.server import create_app
from tests.server._client import TestClient

GOOD, BAD, OTHER = "2024_Good_Paper", "2024_Bad_Paper", "2024_Other_Paper"


def _paper(vault: Path, paper_id: str, **raw: str) -> Path:
    """A complete paper whose fields default to empty lists; ``raw`` values are
    written into the YAML verbatim, so a test can drop a list by hand."""
    fields = {
        "authors": "['Doe, Jane']", "projects": "[]", "topics": "[]",
        "methods": "[]", "data": "[]", "related": "[]", "contradicts": "[]",
        "contradicted-by": "[]", "extends": "[]", "extended-by": "[]",
        "code-clones": "[]",
    }
    fields.update({k.replace("_", "-"): v for k, v in raw.items()})
    paper_dir = vault / "papers" / paper_id
    paper_dir.mkdir(parents=True)
    lines = [
        f"id: {paper_id}", f"title: {paper_id}", "year: 2024",
        "type: research", "status: inbox", f"doi: 10.1/{paper_id}",
        "created-at: '2026-10-02T10:00:00+02:00'",
        "updated-at: '2026-10-02T10:00:00+02:00'",
        *(f"{k}: {v}" for k, v in fields.items()),
    ]
    meta = paper_dir / "metadata.yaml"
    with meta.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    (paper_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    return meta


def _hand_edit(meta: Path, field: str, value: str) -> None:
    """Rewrite ``field`` in a file litman wrote, the way a person would."""
    meta.chmod(0o644)  # litman may have left it read-only
    text, n = re.subn(
        rf"(?m)^{re.escape(field)}:.*\n(?:[ -].*\n)*",
        f"{field}: {value}\n",
        meta.read_text(encoding="utf-8"),
    )
    assert n == 1
    with meta.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _related(meta: Path) -> object:
    return YAML(typ="safe").load(meta.read_text(encoding="utf-8"))["related"]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return create_vault(tmp_path)


def _lit(vault: Path, *args: str) -> Result:
    return CliRunner().invoke(cli, [*args, "--library", str(vault)])


def _flat(text: str) -> str:
    """Rich wraps at the terminal width; compare with the wrapping undone."""
    return re.sub(r"\s+", " ", text)


def test_refresh_views_files_no_paper_under_a_letter(vault: Path) -> None:
    _paper(vault, GOOD, topics="[peptide]")
    _paper(vault, BAD, topics="peptide")

    result = _lit(vault, "refresh-views")

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (vault / "views" / "by-topic").iterdir()) == [
        "peptide"
    ]
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in index["papers"]] == [GOOD]


@pytest.mark.parametrize("value", ["peptide", "42"])
def test_health_check_reports_it_and_runs_to_the_end(
    vault: Path, value: str
) -> None:
    _paper(vault, GOOD)
    _paper(vault, BAD, topics=value)

    result = _lit(vault, "health-check")

    assert result.exception is None or isinstance(result.exception, SystemExit)
    out = _flat(result.output)
    assert f"papers/{BAD}/metadata.yaml: 'topics' is not a list" in out
    assert f"write it as `topics: [{value if value == '42' else repr(value)}]`" in out
    assert "not in TAXONOMY.md" not in out  # no letter reported as a topic


def test_a_broken_paper_does_not_stop_a_write_to_another(vault: Path) -> None:
    _paper(vault, GOOD)
    _paper(vault, BAD, topics="42")

    result = _lit(vault, "modify", GOOD, "--set", "status=skim")

    assert result.exit_code == 0, result.output
    assert "status: skim" in (
        vault / "papers" / GOOD / "metadata.yaml"
    ).read_text(encoding="utf-8")


def test_add_tag_on_a_scalar_relation_is_refused_and_writes_nothing(
    vault: Path,
) -> None:
    """The write that used to save `related: X` back as X's characters."""
    good = _paper(vault, GOOD)
    bad = _paper(vault, BAD, related=OTHER)
    other = _paper(vault, OTHER)
    before = [m.read_bytes() for m in (good, bad, other)]

    result = _lit(vault, "modify", BAD, "--add-tag", f"related={GOOD}")

    assert isinstance(result.exception, CorruptMetadataError)
    message = str(result.exception)
    assert str(bad) in message
    assert f"'related' is not a list; write it as `related: ['{OTHER}']`" in message
    assert [m.read_bytes() for m in (good, bad, other)] == before


def test_pairing_a_relation_with_a_broken_paper_is_refused(vault: Path) -> None:
    """The reverse edge would land in the broken file, so nothing is written —
    not even the forward edge on the paper the user named."""
    good = _paper(vault, GOOD)
    bad = _paper(vault, BAD, topics="peptide")
    before = [good.read_bytes(), bad.read_bytes()]

    result = _lit(vault, "modify", GOOD, "--add-tag", f"related={BAD}")

    assert isinstance(result.exception, CorruptMetadataError)
    assert str(bad) in str(result.exception)
    assert [good.read_bytes(), bad.read_bytes()] == before


def test_show_names_the_file_and_the_fix(vault: Path) -> None:
    bad = _paper(vault, BAD, authors="Doe, Jane")

    result = _lit(vault, "show", BAD)

    assert isinstance(result.exception, CorruptMetadataError)
    assert str(bad) in str(result.exception)
    assert "write it as `authors: ['Doe, Jane']`" in str(result.exception)


def test_rename_refuses_while_a_paper_it_links_to_is_broken(vault: Path) -> None:
    """That paper's edge must be renamed too. Left naming the old id, it
    would read as dangling once the file is fixed, and `--fix` would drop it."""
    good = _paper(vault, GOOD, related=f"[{OTHER}]")
    other = _paper(vault, OTHER, related=f"[{GOOD}]", topics="peptide")
    before = [good.read_bytes(), other.read_bytes()]

    result = _lit(vault, "rename", GOOD, "2024_New_Paper")

    assert isinstance(result.exception, CorruptMetadataError)
    assert str(other) in str(result.exception)
    assert [good.read_bytes(), other.read_bytes()] == before
    assert (vault / "papers" / GOOD).is_dir()


def test_rename_is_not_blocked_by_a_broken_paper_it_does_not_link_to(
    vault: Path,
) -> None:
    _paper(vault, GOOD)
    _paper(vault, BAD, topics="peptide")

    result = _lit(vault, "rename", GOOD, "2024_New_Paper")

    assert result.exit_code == 0, result.output
    assert (vault / "papers" / "2024_New_Paper").is_dir()


def test_restore_waits_until_a_paper_it_links_to_is_fixed(vault: Path) -> None:
    """Restore writes the reverse edge back into each linked paper: into a
    broken one it would save a misread list, and pruning the edge instead
    would drop a link to a paper that is still there. So nothing moves."""
    _paper(vault, GOOD, related=f"[{OTHER}]")
    other = _paper(vault, OTHER, related=f"[{GOOD}]")
    assert _lit(vault, "rm", GOOD, "-y").exit_code == 0
    _hand_edit(other, "related", "2024_Zed_Paper")
    before = other.read_bytes()

    result = _lit(vault, "trash", "restore", GOOD)

    assert isinstance(result.exception, CorruptMetadataError)
    assert str(other) in str(result.exception)
    assert other.read_bytes() == before
    assert not (vault / "papers" / GOOD).exists()

    _hand_edit(other, "related", "['2024_Zed_Paper']")
    result = _lit(vault, "trash", "restore", GOOD)

    assert result.exit_code == 0, result.output
    assert _related(other) == ["2024_Zed_Paper", GOOD]
    assert _related(vault / "papers" / GOOD / "metadata.yaml") == [OTHER]


def test_restore_refuses_a_trash_entry_that_is_itself_broken(vault: Path) -> None:
    """An entry trashed before litman refused such files, or edited in the
    trash: restored as it stands, its topics would come back as letters."""
    _paper(vault, GOOD, related=f"[{OTHER}]")
    other = _paper(vault, OTHER, related=f"[{GOOD}]")
    assert _lit(vault, "rm", GOOD, "-y").exit_code == 0
    (entry,) = [d for d in (vault / TRASH_DIRNAME).iterdir() if d.is_dir()]
    sealed = entry / "metadata.yaml"
    _hand_edit(sealed, "topics", "peptide")
    before = other.read_bytes()

    result = _lit(vault, "trash", "restore", GOOD)

    assert isinstance(result.exception, CorruptMetadataError)
    assert str(sealed) in str(result.exception)
    assert not (vault / "papers" / GOOD).exists()
    assert other.read_bytes() == before
    assert not (vault / "views" / "by-topic" / "p").exists()


def test_code_rm_cascade_leaves_a_broken_paper_alone(vault: Path) -> None:
    """`code-clones: myrepo-fork` matched `myrepo` as a substring and was
    saved back as its characters; `code-clones: 42` crashed the cascade."""
    good = _paper(vault, GOOD, code_clones="[myrepo]")
    bad = _paper(vault, BAD, code_clones="myrepo-fork")
    other = _paper(vault, OTHER, topics="peptide", code_clones="[myrepo]")
    num = _paper(vault, "2024_Num_Paper", code_clones="42")
    before = [m.read_bytes() for m in (bad, other, num)]

    assert unbind_repo_from_all_papers(vault, "myrepo") == [GOOD]

    assert "myrepo" not in good.read_text(encoding="utf-8")
    assert [m.read_bytes() for m in (bad, other, num)] == before
    index = json.loads((vault / "INDEX.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in index["papers"]] == [GOOD]


# --- the GUI's server ---------------------------------------------------------


def test_the_server_leaves_it_out_and_names_it_when_asked(vault: Path) -> None:
    _paper(vault, GOOD)
    _paper(vault, BAD, related=GOOD)
    assert _lit(vault, "refresh-views").exit_code == 0
    client = TestClient(create_app(vault))

    assert [p["id"] for p in client.get("/api/papers").json()] == [GOOD]
    resp = client.get(f"/api/paper/{BAD}")
    assert resp.status_code == 500
    assert "'related' is not a list" in resp.json()["detail"]


def test_a_gui_write_that_reaches_it_says_which_file(vault: Path) -> None:
    """Pairing a relation loads the other paper; the toast must name that
    file and the fix, not say "Internal Server Error"."""
    good = _paper(vault, GOOD)
    bad = _paper(vault, BAD, topics="peptide")
    before = good.read_bytes()
    client = TestClient(create_app(vault))

    resp = client.put(
        f"/api/paper/{GOOD}/metadata", json={"addTag": {"related": [BAD]}}
    )

    assert resp.status_code == 500
    assert str(bad) in resp.json()["detail"]
    assert "'topics' is not a list" in resp.json()["detail"]
    assert resp.headers["Cache-Control"] == "no-store"
    assert good.read_bytes() == before


def test_a_gui_restore_of_a_broken_entry_says_which_file(vault: Path) -> None:
    _paper(vault, GOOD)
    assert _lit(vault, "rm", GOOD, "-y").exit_code == 0
    (entry,) = [d for d in (vault / TRASH_DIRNAME).iterdir() if d.is_dir()]
    _hand_edit(entry / "metadata.yaml", "topics", "peptide")
    client = TestClient(create_app(vault))

    resp = client.post(f"/api/trash/{entry.name}/restore")

    assert resp.status_code == 500
    assert str(entry / "metadata.yaml") in resp.json()["detail"]
    assert not (vault / "papers" / GOOD).exists()
