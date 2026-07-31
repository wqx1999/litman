"""Machine-level GUI-state tests (task-gui-pin, AC-1/AC-2/AC-6).

Covers the ``ui-state.json`` round-trip, per-vault key isolation (pinning in
one vault never clears another's), ``$LITMAN_REGISTRY_DIR`` redirection (the
same env var the autouse ``_isolate_registry`` fixture sets, so these run
against a tmp dir automatically), the []-on-missing/garbage contract of
``load_pins``, registry-name vs path keying, and ``remove_ui_state`` as the
``lit uninstall`` counterpart.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from litman.core import ui_state
from litman.core.vault_registry import (
    REGISTRY_ENV_VAR,
    VaultEntry,
    VaultRegistry,
    save_registry,
)


def _vault(tmp_path: Path, name: str = "v") -> Path:
    vault = tmp_path / name
    vault.mkdir(exist_ok=True)
    return vault


def test_ui_state_path_honors_registry_env() -> None:
    """The autouse fixture points $LITMAN_REGISTRY_DIR at a tmp dir; the state
    file must resolve under it (isolation + registry symmetry)."""
    override = os.environ[REGISTRY_ENV_VAR]
    assert ui_state.ui_state_path() == Path(override) / "ui-state.json"


def test_round_trip_preserves_order(tmp_path: Path) -> None:
    """AC-1: save → load round-trips, and list order (= pin order) holds."""
    vault = _vault(tmp_path)
    assert ui_state.load_pins(vault) == []  # fresh tmp dir, no file
    ui_state.save_pins(vault, ["b_paper", "a_paper", "c_paper"])
    assert ui_state.ui_state_path().is_file()
    assert ui_state.load_pins(vault) == ["b_paper", "a_paper", "c_paper"]


def test_second_vault_does_not_clobber_first(tmp_path: Path) -> None:
    """AC-1: writing vault B's pins keeps vault A's intact."""
    a, b = _vault(tmp_path, "a"), _vault(tmp_path, "b")
    ui_state.save_pins(a, ["paper_one"])
    ui_state.save_pins(b, ["paper_two", "paper_three"])
    assert ui_state.load_pins(a) == ["paper_one"]
    assert ui_state.load_pins(b) == ["paper_two", "paper_three"]


def test_empty_save_removes_the_vault_key(tmp_path: Path) -> None:
    """Clearing pins leaves no husk entry behind."""
    vault = _vault(tmp_path)
    ui_state.save_pins(vault, ["p1"])
    ui_state.save_pins(vault, [])
    raw = json.loads(ui_state.ui_state_path().read_text(encoding="utf-8"))
    assert raw["pins"] == {}
    assert ui_state.load_pins(vault) == []


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert not ui_state.ui_state_path().exists()
    assert ui_state.load_pins(_vault(tmp_path)) == []


def test_load_garbage_inputs_return_empty(tmp_path: Path) -> None:
    """AC-2: four bad shapes — unparseable, top-level list, pins not a dict,
    vault entry not a list — all degrade to [] without raising."""
    vault = _vault(tmp_path)
    path = ui_state.ui_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for bad in (
        "not json",
        '["top", "level", "list"]',
        '{"pins": "a string"}',
        json.dumps({"pins": {ui_state.vault_key(vault): "not-a-list"}}),
    ):
        path.write_text(bad, encoding="utf-8")
        assert ui_state.load_pins(vault) == []


def test_load_drops_non_string_entries(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    path = ui_state.ui_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = ui_state.vault_key(vault)
    path.write_text(
        json.dumps({"pins": {key: ["good", 42, None, "also_good"]}}),
        encoding="utf-8",
    )
    assert ui_state.load_pins(vault) == ["good", "also_good"]


def test_save_over_garbage_file_recovers(tmp_path: Path) -> None:
    """A corrupt state file is replaced, not appended to and not fatal."""
    vault = _vault(tmp_path)
    path = ui_state.ui_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    ui_state.save_pins(vault, ["p1"])
    assert ui_state.load_pins(vault) == ["p1"]


def test_vault_key_prefers_registry_name(tmp_path: Path) -> None:
    """A registered vault keys by NAME (pins survive lit vault set-path);
    an unregistered one keys by resolved path."""
    registered = _vault(tmp_path, "registered")
    stray = _vault(tmp_path, "stray")
    save_registry(
        VaultRegistry(
            vaults=[
                VaultEntry(name="mylib", path=str(registered), is_active=True)
            ]
        )
    )
    assert ui_state.vault_key(registered) == "mylib"
    assert ui_state.vault_key(stray) == str(stray.resolve())


def test_pins_survive_registry_repath(tmp_path: Path) -> None:
    """The point of name-keying: moving a registered vault keeps its pins."""
    old_home = _vault(tmp_path, "old-home")
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="mylib", path=str(old_home), is_active=True)]
        )
    )
    ui_state.save_pins(old_home, ["p1"])

    new_home = _vault(tmp_path, "new-home")
    save_registry(
        VaultRegistry(
            vaults=[VaultEntry(name="mylib", path=str(new_home), is_active=True)]
        )
    )
    assert ui_state.load_pins(new_home) == ["p1"]


def test_remove_ui_state_deletes_file_and_empty_dir() -> None:
    """AC-6 (core half): remove deletes the file and rmdirs a now-empty
    config dir; a second call is a no-op with removed=False."""
    path = ui_state.ui_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    result = ui_state.remove_ui_state()
    assert result["removed"] is True
    assert result["dir_removed"] is True
    assert not path.exists()
    assert not path.parent.exists()

    again = ui_state.remove_ui_state()
    assert again["removed"] is False


def test_remove_ui_state_keeps_nonempty_config_dir(tmp_path: Path) -> None:
    """A sibling (e.g. vaults.yaml still present) blocks the rmdir."""
    path = ui_state.ui_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    (path.parent / "vaults.yaml").write_text("vaults: []\n", encoding="utf-8")

    result = ui_state.remove_ui_state()
    assert result["removed"] is True
    assert result["dir_removed"] is False
    assert path.parent.exists()
