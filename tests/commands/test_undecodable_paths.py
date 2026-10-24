"""A folder whose name is not UTF-8 text, handed to a command that records it.

On Linux such a name reaches Python with each bad byte as a lone surrogate
(``caf\\udce9``), which no YAML litman keeps can hold. A project's or a
library's path is refused before anything is written; ``lit code add`` only
notes where a folder came from, so it goes ahead with the byte shown as
U+FFFD (``core/undecodable.py``). Any other value typed with such a byte stops
``lit`` with one line instead of a traceback.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

import litman
from litman.cli import cli
from litman.core.document import list_papers
from litman.core.library import create_vault
from litman.core.vault_registry import load_registry, registry_path
from litman.core.views import write_index
from litman.exceptions import TaxonomyError, UndecodablePathError, VaultRegistryError

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or sys.getfilesystemencoding() != "utf-8",
    reason="needs Linux (APFS refuses a file name that is not UTF-8; a Windows "
    "name is UTF-16) and a UTF-8 file-name encoding, under which a bad byte "
    "becomes a lone surrogate",
)

_REFUSED = "is not UTF-8 text, so litman cannot record its path. Rename the folder, then retry."


def _undecodable_dir(parent: Path, stem: str) -> Path:
    """Create ``<parent>/<stem>\\xe9`` and return it as Python sees it."""
    raw = os.fsencode(parent) + b"/" + os.fsencode(stem) + b"\xe9"
    os.mkdir(raw)
    return Path(os.fsdecode(raw))


def test_project_add_refuses_a_folder_whose_name_is_not_utf8(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    folder = _undecodable_dir(tmp_path, "proj_caf")
    config_before = (vault / "lit-config.yaml").read_bytes()
    taxonomy_before = (vault / "TAXONOMY.md").read_bytes()

    result = CliRunner().invoke(
        cli, ["project", "add", "probe", "--path", str(folder), "--library", str(vault)]
    )

    assert isinstance(result.exception, TaxonomyError), result.output
    assert str(result.exception) == f"Folder name “proj_caf\ufffd” {_REFUSED}"
    assert (vault / "lit-config.yaml").read_bytes() == config_before
    assert (vault / "TAXONOMY.md").read_bytes() == taxonomy_before
    listed = CliRunner().invoke(cli, ["project", "list", "--library", str(vault)])
    assert listed.exit_code == 0, listed.output


def test_project_set_path_refuses_a_folder_whose_name_is_not_utf8(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    good = tmp_path / "proj"
    good.mkdir()
    added = CliRunner().invoke(
        cli, ["project", "add", "p", "--path", str(good), "--library", str(vault)]
    )
    assert added.exit_code == 0, added.output
    config_before = (vault / "lit-config.yaml").read_bytes()
    folder = _undecodable_dir(tmp_path, "moved_caf")

    result = CliRunner().invoke(
        cli, ["project", "set-path", "p", str(folder), "--library", str(vault)]
    )

    assert isinstance(result.exception, TaxonomyError), result.output
    assert str(result.exception) == f"Folder name “moved_caf\ufffd” {_REFUSED}"
    assert (vault / "lit-config.yaml").read_bytes() == config_before


def test_a_folder_under_one_whose_name_is_not_utf8_is_refused_by_that_name(
    tmp_path: Path,
) -> None:
    vault = create_vault(tmp_path)
    child = _undecodable_dir(tmp_path, "archive_caf") / "proj"
    child.mkdir()

    result = CliRunner().invoke(
        cli, ["project", "add", "probe", "--path", str(child), "--library", str(vault)]
    )

    assert isinstance(result.exception, TaxonomyError), result.output
    assert str(result.exception) == f"Folder name “archive_caf\ufffd” {_REFUSED}"


def _vault_at_undecodable_path(tmp_path: Path) -> Path:
    built = create_vault(tmp_path, name="built")
    target = os.fsencode(tmp_path) + b"/vault_caf\xe9"
    os.rename(os.fsencode(built), target)
    return Path(os.fsdecode(target))


def test_vault_add_refuses_a_library_whose_path_is_not_utf8(tmp_path: Path) -> None:
    vault = _vault_at_undecodable_path(tmp_path)

    result = CliRunner().invoke(cli, ["vault", "add", "probe", str(vault)])

    assert isinstance(result.exception, VaultRegistryError), result.output
    assert str(result.exception) == f"Folder name “vault_caf\ufffd” {_REFUSED}"
    assert load_registry().vaults == []
    assert not registry_path().exists()


def test_vault_set_path_refuses_a_library_whose_path_is_not_utf8(tmp_path: Path) -> None:
    good = create_vault(tmp_path, name="good")
    added = CliRunner().invoke(cli, ["vault", "add", "good", str(good)])
    assert added.exit_code == 0, added.output
    registry_before = registry_path().read_bytes()
    vault = _vault_at_undecodable_path(tmp_path)

    result = CliRunner().invoke(cli, ["vault", "set-path", "good", str(vault)])

    assert isinstance(result.exception, VaultRegistryError), result.output
    assert str(result.exception) == f"Folder name “vault_caf\ufffd” {_REFUSED}"
    assert registry_path().read_bytes() == registry_before


def test_vault_add_keeps_a_provenance_note_with_such_a_byte_as_u_fffd(
    tmp_path: Path,
) -> None:
    vault = create_vault(tmp_path)

    result = CliRunner().invoke(
        cli, ["vault", "add", "probe", str(vault), "--import-from", os.fsdecode(b"caf\xe9")]
    )

    assert result.exit_code == 0, result.output
    assert [v.imported_from for v in load_registry().vaults] == ["caf\ufffd"]


def test_a_registry_an_earlier_version_wrote_such_a_note_into_heals(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    registry_path().parent.mkdir(parents=True, exist_ok=True)
    registry_path().write_text(
        f"vaults:\n  - name: probe\n    path: '{vault}'\n"
        '    imported_from: "caf\\uDCE9"\n    is_active: true\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["vault", "use", "probe"])

    assert result.exit_code == 0, result.output
    assert "\\uDC" not in registry_path().read_text(encoding="utf-8")
    assert [v.imported_from for v in load_registry().vaults] == ["caf\ufffd"]


@pytest.mark.parametrize("flags", [[], ["--no-register"]], ids=["register", "no-register"])
def test_init_under_a_folder_whose_name_is_not_utf8_creates_nothing(
    tmp_path: Path, flags: list[str]
) -> None:
    parent = _undecodable_dir(tmp_path, "parent_caf")

    result = CliRunner().invoke(cli, ["init", str(parent), *flags])

    assert isinstance(result.exception, UndecodablePathError), result.output
    assert str(result.exception) == f"Folder name “parent_caf\ufffd” {_REFUSED}"
    assert os.listdir(os.fsencode(parent)) == []
    assert not registry_path().exists()


def test_init_refuses_a_vault_name_that_is_not_utf8_by_that_name(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["init", str(tmp_path), "--name", os.fsdecode(b"caf\xe9"), "--no-register"]
    )

    assert isinstance(result.exception, UndecodablePathError), result.output
    assert str(result.exception) == (
        "Vault name “caf\ufffd” is not UTF-8 text. Pass another --name, then retry."
    )
    assert os.listdir(tmp_path) == []


def test_init_at_a_terminal_refuses_before_offering_to_create_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litman.commands import init as init_mod

    monkeypatch.setattr(init_mod, "_stdin_isatty", lambda: True)
    parent = _undecodable_dir(tmp_path, "parent_caf")

    result = CliRunner().invoke(cli, ["init", str(parent / "new")], input="\n")

    assert isinstance(result.exception, UndecodablePathError), result.output
    assert "Create it?" not in result.output
    assert os.listdir(os.fsencode(parent)) == []


def test_create_vault_refuses_such_a_parent_whether_or_not_it_exists(
    tmp_path: Path,
) -> None:
    parent = _undecodable_dir(tmp_path, "parent_caf")

    for target in (parent, parent / "missing"):
        with pytest.raises(UndecodablePathError) as raised:
            create_vault(target)
        assert str(raised.value) == f"Folder name “parent_caf\ufffd” {_REFUSED}"
    assert os.listdir(os.fsencode(parent)) == []


def test_code_add_imports_a_plain_folder_whose_name_is_not_utf8(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    source = _undecodable_dir(tmp_path, "src_caf")
    (source / "README").write_text("hello\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli, ["code", "add", str(source), "--name", "probe", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    repo = vault / "codes" / "probe"
    noted = "local:" + str(source.resolve()).replace("\udce9", "\ufffd")
    meta = YAML(typ="safe").load((repo / "repo-meta.yaml").read_text(encoding="utf-8"))
    assert meta["upstream"] == noted
    assert f"Upstream: {noted}" in (repo / "notes.md").read_text(encoding="utf-8")
    assert (repo / "repo" / "README").read_text(encoding="utf-8") == "hello\n"
    assert (source / "README").is_file()  # a copy: the source stays
    listed = CliRunner().invoke(cli, ["code", "list", "--library", str(vault)])
    assert listed.exit_code == 0, listed.output
    assert "probe" in listed.output


def _git(*args: str | bytes) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        check=True,
        capture_output=True,
    )


def test_code_add_imports_a_git_folder_whose_origin_is_not_utf8(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "README").write_text("hello\n", encoding="utf-8")
    _git("init", "-q", str(source))
    _git("-C", str(source), "config", "remote.origin.url", os.fsdecode(b"/data/caf\xe9/r.git"))

    result = CliRunner().invoke(
        cli, ["code", "add", str(source), "--name", "probe", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    meta = YAML(typ="safe").load(
        (vault / "codes" / "probe" / "repo-meta.yaml").read_text(encoding="utf-8")
    )
    assert meta["upstream"] == "/data/caf\ufffd/r.git"


def test_code_add_clones_a_url_whose_path_is_not_utf8(tmp_path: Path) -> None:
    vault = create_vault(tmp_path)
    origin = _undecodable_dir(tmp_path, "origin_caf")
    (origin / "README").write_text("hello\n", encoding="utf-8")
    _git("init", "-q", str(origin))
    _git("-C", str(origin), "add", "README")
    _git("-C", str(origin), "commit", "-q", "-m", "init")
    url = "file://" + str(origin)

    result = CliRunner().invoke(
        cli, ["code", "add", url, "--name", "probe", "--library", str(vault)]
    )

    assert result.exit_code == 0, result.output
    repo = vault / "codes" / "probe"
    noted = "file://" + str(origin).replace("\udce9", "\ufffd")
    meta = YAML(typ="safe").load((repo / "repo-meta.yaml").read_text(encoding="utf-8"))
    assert meta["upstream"] == noted
    assert f"Upstream: {noted}" in (repo / "notes.md").read_text(encoding="utf-8")
    assert (repo / "repo" / "README").read_text(encoding="utf-8") == "hello\n"


_META = (
    "id: 2019_Yang_Concepts\ntitle: Concepts\nauthors:\n  - Doe, Jane\nyear: 2019\n"
    "status: inbox\ncreated-at: '2026-01-01T00:00:00+00:00'\n"
    "updated-at: '2026-01-01T00:00:00+00:00'\n"
)


def test_a_typed_value_that_is_not_utf8_stops_lit_with_one_line(tmp_path: Path) -> None:
    """Through the real main(). ``journal`` is not in INDEX.json, so nothing
    else stopped it: metadata.yaml was written with an escape the C loader
    rejects, and the paper dropped out of every listing. The YAML writer now
    raises first, and lit names the word; the file is untouched."""
    vault = create_vault(tmp_path)
    paper = vault / "papers" / "2019_Yang_Concepts"
    paper.mkdir(parents=True)
    (paper / "metadata.yaml").write_text(_META, encoding="utf-8")
    write_index(vault, list_papers(vault))
    before = (paper / "metadata.yaml").read_bytes()
    env = os.environ.copy()
    env.pop("FORCE_COLOR", None)
    env["COLUMNS"] = "200"
    src_dir = Path(litman.__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    journal = "journal=Probe " + os.fsdecode(b"caf\xe9")

    proc = subprocess.run(
        [sys.executable, "-m", "litman", "modify", "2019_Yang_Concepts", "--set", journal,
         "--library", str(vault)],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr
    assert proc.stdout.splitlines()[-1] == (
        "error: “caf\ufffd” is not UTF-8 text, so litman cannot store it. "
        "Change it, then retry."
    )
    assert (paper / "metadata.yaml").read_bytes() == before
