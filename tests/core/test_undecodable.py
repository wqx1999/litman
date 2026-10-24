"""``core/undecodable.py`` and the YAML backstops behind every path check.

No such file name is made: a lone surrogate is built in Python, so these run
everywhere.
The folders themselves are exercised in ``tests/commands/test_undecodable_paths.py``.
"""

from __future__ import annotations

import io
from pathlib import Path, PurePosixPath

import pytest

from litman.commands.project import _dump_config_yaml
from litman.core.code import make_repo_meta, write_notes
from litman.core.project_link import _dump_yaml_to_string
from litman.core.sync import SetupPayload, write_sync_to_config
from litman.core.undecodable import (
    config_refusal,
    find_undecodable,
    has_undecodable,
    printable,
    printable_tree,
    refusal,
    text_refusal,
    undecodable_part,
)
from litman.core.vault_registry import (
    VaultEntry,
    VaultRegistry,
    registry_path,
    save_registry,
)
from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import SyncError, TaxonomyError, VaultRegistryError

BAD = "/data/archive_caf\udce9/proj"


def test_printable_turns_each_undecodable_byte_into_a_replacement_character() -> None:
    assert has_undecodable("caf\udce9") and not has_undecodable("café")
    assert printable("caf\udce9 na\udcefve") == "caf\ufffd na\ufffdve"
    assert printable("café") == "café"


def test_the_refusal_names_the_component_that_is_not_utf8() -> None:
    assert undecodable_part(PurePosixPath(BAD)) == "archive_caf\udce9"
    assert undecodable_part("/data/café/proj") is None
    assert refusal(BAD) == (
        "Folder name “archive_caf\ufffd” is not UTF-8 text, so litman cannot record "
        "its path. Rename the folder, then retry."
    )
    assert refusal("/data/café/proj") is None


def test_find_and_printable_tree_reach_keys_values_and_nesting() -> None:
    tree = {"projects": {"ok": "/a", "caf\udce9": "/b"}, "list": [("x", "/c\udce9")]}
    found = find_undecodable(tree)
    assert found in {"caf\udce9", "/c\udce9"}
    assert find_undecodable({"projects": {"ok": "/a"}, "n": 3}) is None
    assert printable_tree(tree) == {
        "projects": {"ok": "/a", "caf\ufffd": "/b"},
        "list": [("x", "/c\ufffd")],
    }


@pytest.mark.parametrize(
    "dump", [_dump_yaml_to_string, _dump_config_yaml], ids=["core", "commands"]
)
def test_no_config_writer_renders_a_value_that_is_not_utf8(dump) -> None:
    with pytest.raises(TaxonomyError) as raised:
        dump({"projects": {"probe": BAD}})
    assert str(raised.value) == (
        "lit-config.yaml cannot hold “archive_caf\ufffd”: it is not UTF-8 text. "
        "Nothing was written; rename or retype it, then retry."
    )
    assert config_refusal({"projects": {"probe": "/data/café"}}) is None
    assert "probe: /data/café" in dump({"projects": {"probe": "/data/café"}})


def test_a_project_name_that_is_not_utf8_is_refused_by_the_backstop_too() -> None:
    with pytest.raises(TaxonomyError) as raised:
        _dump_yaml_to_string({"projects": {"caf\udce9": "/data/proj"}})
    assert "“caf\ufffd”" in str(raised.value)


def test_text_refusal_names_the_word_that_failed_to_encode() -> None:
    text = 'title: "Concepts caf\udce9", year: 2019\n'
    assert text_refusal(text, text.index("\udce9")) == (
        "“caf\ufffd” is not UTF-8 text, so litman cannot store it. Change it, then retry."
    )
    lone = "Probe \ud835 J."  # a JSON field: the byte alone between spaces
    assert text_refusal(lone, 6).startswith("“Probe \ufffd J.”")
    long = "/home/u/" + "x" * 80 + "/archive_caf\udce9/" + "y" * 40
    shown = text_refusal(long, long.index("\udce9")).split("”")[0]
    assert "archive_caf\ufffd" in shown and shown.startswith("“…") and shown.endswith("…")


def test_no_yaml_file_is_written_with_text_that_is_not_utf8() -> None:
    """Every YAML writer goes through ThreadLocalYAML: ruamel would write the
    lone surrogate as an escape the C loader then rejects."""
    buf = io.StringIO()

    with pytest.raises(UnicodeEncodeError) as raised:
        ThreadLocalYAML().dump({"title": "Concepts", "journal": "J. caf\udce9"}, buf)

    assert buf.getvalue() == ""
    assert text_refusal(raised.value.object, raised.value.start).startswith("“caf\ufffd”")
    ThreadLocalYAML().dump({"journal": "J. café"}, buf)
    assert "journal: J. café" in buf.getvalue()


def test_sync_setup_writes_no_path_that_is_not_utf8(tmp_path: Path) -> None:
    config = tmp_path / "lit-config.yaml"
    config.write_text("# kept\nprojects: {}\n", encoding="utf-8")
    before = config.read_bytes()

    with pytest.raises(SyncError) as raised:
        write_sync_to_config(config, SetupPayload(remote="usb", path=BAD))

    assert "“archive_caf\ufffd”" in str(raised.value)
    assert config.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lit-config.yaml"]
    write_sync_to_config(config, SetupPayload(remote="usb", path="/data/café"))
    assert "path: /data/café" in config.read_text(encoding="utf-8")


def test_the_vault_registry_writes_no_value_that_is_not_utf8() -> None:
    # model_copy skips validation: how set-path once wrote such a path.
    entry = VaultEntry(name="probe", path="/data/lib").model_copy(
        update={"path": "/data/caf\udce9"}
    )

    with pytest.raises(VaultRegistryError) as raised:
        save_registry(VaultRegistry(vaults=[entry]))

    assert str(raised.value) == (
        "vaults.yaml cannot hold “caf\ufffd”: it is not UTF-8 text. "
        "Nothing was written; rename or retype it, then retry."
    )
    assert not registry_path().exists()


def test_a_vaults_provenance_note_keeps_such_a_byte_as_a_replacement_character() -> None:
    entry = VaultEntry(name="probe", path="/data/lib", imported_from="caf\udce9")
    assert entry.imported_from == "caf\ufffd"
    assert VaultEntry(name="probe", path="/data/lib").imported_from is None


def test_a_code_note_keeps_an_undecodable_upstream_as_a_replacement_character(
    tmp_path: Path,
) -> None:
    upstream = "file:///data/caf\udce9/r.git"

    assert make_repo_meta("probe", upstream)["upstream"] == "file:///data/caf\ufffd/r.git"
    assert make_repo_meta("probe", None)["upstream"] is None
    notes = write_notes(tmp_path, "probe", upstream).read_text(encoding="utf-8")
    assert "Upstream: file:///data/caf\ufffd/r.git" in notes
