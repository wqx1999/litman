"""Tests for the M8.1 vault registry + the find_vault() discovery chain.

Every test redirects ``$HOME`` via monkeypatch so ``Path.home()`` resolves
inside ``tmp_path``. Without the redirect, ``registry_path()`` would
point at the real ``~/.config/litman/vaults.yaml`` and the tests would
either fail (HOME read-only on CI) or worse, scribble over wangq's real
registry on a dev machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from litman.core.library import create_vault, find_vault
from litman.core.vault_registry import (
    REGISTRY_FILENAME,
    VaultEntry,
    VaultRegistry,
    add_vault,
    find_active,
    find_by_name,
    is_valid_vault_name,
    load_registry,
    registry_path,
    remove_vault,
    resolve_vault_param,
    save_registry,
    set_active,
)
from litman.exceptions import LibraryNotFoundError, VaultRegistryError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME and clear cross-cutting env vars so the registry file
    deterministically lands at ``tmp_path/home/.config/litman/``.

    Clears:
    * ``LITMAN_REGISTRY_DIR`` — explicit override that would shadow HOME-based
      resolution.
    * ``XDG_CONFIG_HOME`` — platformdirs respects it ahead of $HOME on Linux,
      so leaking it from the user's shell would break ``registry_path()``
      assertions.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LITMAN_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.fixture
def vault_a(tmp_path: Path) -> Path:
    """A fresh vault at tmp_path/parent_a/literature_vault/."""
    parent = tmp_path / "parent_a"
    parent.mkdir()
    return create_vault(parent, name="vault_a")


@pytest.fixture
def vault_b(tmp_path: Path) -> Path:
    """A second fresh vault for multi-vault scenarios."""
    parent = tmp_path / "parent_b"
    parent.mkdir()
    return create_vault(parent, name="vault_b")


# ---------------------------------------------------------------------------
# is_valid_vault_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,valid",
    [
        ("my-main", True),
        ("zhang_shared", True),
        ("repo.with.dots", True),
        ("Vault123", True),
        ("_leading-under", True),
        ("", False),
        ("-leading-hyphen", False),  # would parse as a shell flag
        ("name:with:colon", False),  # reserved for cross-vault wikilinks
        ("with/slash", False),
        ("with\\backslash", False),
        (".hidden", False),
        ("with space", False),
    ],
)
def test_is_valid_vault_name(name: str, valid: bool) -> None:
    assert is_valid_vault_name(name) is valid


# ---------------------------------------------------------------------------
# registry_path
# ---------------------------------------------------------------------------


def test_registry_path_under_home(fake_home: Path) -> None:
    """Default (no env override) resolves under $HOME/.config/litman on Linux.

    On non-Linux this test is informational; CI runs Linux, where
    platformdirs' user_config_dir defaults to XDG (=$HOME/.config).
    """
    import sys
    if sys.platform.startswith("linux"):
        assert (
            registry_path()
            == fake_home / ".config" / "litman" / REGISTRY_FILENAME
        )


def test_registry_path_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$LITMAN_REGISTRY_DIR redirects the registry irrespective of HOME."""
    target = tmp_path / "cloud-synced"
    target.mkdir()
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", str(target))
    assert registry_path() == target / REGISTRY_FILENAME


def test_registry_path_env_override_blank_ignored(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty / whitespace-only env values fall through to platformdirs."""
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", "   ")
    # Should NOT redirect — strip() yields empty.
    import sys
    if sys.platform.startswith("linux"):
        assert (
            registry_path()
            == fake_home / ".config" / "litman" / REGISTRY_FILENAME
        )


def test_registry_path_env_override_expands_user(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``~`` in the env value is expanded against $HOME."""
    monkeypatch.setenv("LITMAN_REGISTRY_DIR", "~/my-registry")
    assert (
        registry_path()
        == fake_home / "my-registry" / REGISTRY_FILENAME
    )


# ---------------------------------------------------------------------------
# VaultEntry / VaultRegistry schema
# ---------------------------------------------------------------------------


def test_vault_entry_rejects_bad_name() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VaultEntry(name="bad:name", path="/tmp/x")


def test_vault_entry_rejects_empty_path() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VaultEntry(name="ok", path="")


def test_vault_registry_rejects_duplicate_names() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="[Dd]uplicate"):
        VaultRegistry(
            vaults=[
                VaultEntry(name="dup", path="/a"),
                VaultEntry(name="dup", path="/b"),
            ]
        )


def test_vault_registry_rejects_multiple_actives() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="active"):
        VaultRegistry(
            vaults=[
                VaultEntry(name="a", path="/x", is_active=True),
                VaultEntry(name="b", path="/y", is_active=True),
            ]
        )


def test_vault_registry_allows_zero_actives() -> None:
    """A registry with no active is legal — produced e.g. after removing
    the active vault. The next `lit vault use` re-establishes one."""
    reg = VaultRegistry(
        vaults=[
            VaultEntry(name="a", path="/x", is_active=False),
            VaultEntry(name="b", path="/y", is_active=False),
        ]
    )
    assert find_active(reg) is None


# ---------------------------------------------------------------------------
# load_registry / save_registry
# ---------------------------------------------------------------------------


def test_load_registry_missing_returns_empty(fake_home: Path) -> None:
    """No file at all is normal (fresh install)."""
    assert load_registry() == VaultRegistry()


def test_load_registry_empty_file_returns_empty(fake_home: Path) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True)
    p.write_text("", encoding="utf-8")
    assert load_registry() == VaultRegistry()


def test_save_then_load_round_trip(fake_home: Path, vault_a: Path) -> None:
    reg = VaultRegistry(
        vaults=[
            VaultEntry(
                name="my-main",
                path=str(vault_a),
                imported_from=None,
                imported_at=None,
                is_active=True,
            )
        ]
    )
    save_registry(reg)
    loaded = load_registry()
    assert loaded == reg


def test_save_creates_config_dir(fake_home: Path, vault_a: Path) -> None:
    """First save creates ~/.config/litman/ on the fly."""
    assert not (fake_home / ".config").exists()
    reg = VaultRegistry(
        vaults=[VaultEntry(name="x", path=str(vault_a), is_active=True)]
    )
    save_registry(reg)
    assert registry_path().is_file()


def test_load_registry_malformed_yaml_raises(fake_home: Path) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True)
    p.write_text("vaults: [name: oops\n  this is broken", encoding="utf-8")
    with pytest.raises(VaultRegistryError, match="parse"):
        load_registry()


def test_load_registry_top_level_not_dict_raises(fake_home: Path) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True)
    p.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(VaultRegistryError, match="mapping"):
        load_registry()


def test_load_registry_schema_violation_raises(fake_home: Path) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True)
    p.write_text(
        "vaults:\n"
        "  - name: dup\n"
        "    path: /a\n"
        "  - name: dup\n"
        "    path: /b\n",
        encoding="utf-8",
    )
    with pytest.raises(VaultRegistryError, match="[Dd]uplicate"):
        load_registry()


def test_save_adds_human_warning_header(fake_home: Path, vault_a: Path) -> None:
    """The header comment lands in the file so hand-editors are warned off."""
    reg = VaultRegistry(
        vaults=[VaultEntry(name="x", path=str(vault_a), is_active=True)]
    )
    save_registry(reg)
    body = registry_path().read_text(encoding="utf-8")
    assert "Do not hand-edit" in body
    assert "lit vault" in body


# ---------------------------------------------------------------------------
# add_vault
# ---------------------------------------------------------------------------


def test_add_first_vault_auto_active(vault_a: Path) -> None:
    reg = VaultRegistry()
    out = add_vault(reg, "main", vault_a)
    assert len(out.vaults) == 1
    assert out.vaults[0].name == "main"
    assert out.vaults[0].is_active is True


def test_add_second_vault_not_active_by_default(
    vault_a: Path, vault_b: Path
) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    out = add_vault(reg, "second", vault_b)
    assert len(out.vaults) == 2
    by_name = {v.name: v for v in out.vaults}
    assert by_name["main"].is_active is True
    assert by_name["second"].is_active is False


def test_add_second_vault_set_active_transfers(
    vault_a: Path, vault_b: Path
) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    out = add_vault(reg, "second", vault_b, set_active=True)
    by_name = {v.name: v for v in out.vaults}
    assert by_name["main"].is_active is False
    assert by_name["second"].is_active is True


def test_add_rejects_duplicate_name(vault_a: Path, vault_b: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    with pytest.raises(VaultRegistryError, match="already registered"):
        add_vault(reg, "main", vault_b)


def test_add_rejects_case_fold_collision(vault_a: Path, vault_b: Path) -> None:
    """``my-main`` and ``My-Main`` collide on Windows/macOS — reject ADR-005."""
    reg = add_vault(VaultRegistry(), "my-main", vault_a)
    with pytest.raises(VaultRegistryError, match="differs only in case"):
        add_vault(reg, "My-Main", vault_b)


def test_add_case_fold_collision_message_names_existing(
    vault_a: Path, vault_b: Path
) -> None:
    """The error must point at which existing vault clashes."""
    reg = add_vault(VaultRegistry(), "Zhang-shared", vault_a)
    with pytest.raises(VaultRegistryError, match="Zhang-shared"):
        add_vault(reg, "zhang-shared", vault_b)


def test_add_rejects_bad_name(vault_a: Path) -> None:
    with pytest.raises(VaultRegistryError, match="Invalid vault name"):
        add_vault(VaultRegistry(), "bad:name", vault_a)


def test_add_rejects_missing_directory(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does" / "not" / "exist"
    with pytest.raises(VaultRegistryError, match="not an existing directory"):
        add_vault(VaultRegistry(), "main", nonexistent)


def test_add_rejects_directory_without_lit_config(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(VaultRegistryError, match="no lit-config.yaml"):
        add_vault(VaultRegistry(), "main", plain)


def test_add_provenance_fields_persist(
    fake_home: Path, vault_a: Path
) -> None:
    reg = add_vault(
        VaultRegistry(),
        "zhang-shared",
        vault_a,
        imported_from="Zhang via USB drop",
        imported_at="2026-05-12",
    )
    entry = reg.vaults[0]
    assert entry.imported_from == "Zhang via USB drop"
    assert entry.imported_at == "2026-05-12"


def test_add_resolves_path_to_absolute(vault_a: Path, tmp_path: Path) -> None:
    """add_vault stores the resolved absolute path, not whatever wangq passed."""
    # Build a relative-ish path that resolves to vault_a.
    relative = Path(vault_a)  # already absolute, but resolve is idempotent
    reg = add_vault(VaultRegistry(), "main", relative)
    stored = Path(reg.vaults[0].path)
    assert stored.is_absolute()
    assert stored == vault_a.resolve()


# ---------------------------------------------------------------------------
# remove_vault
# ---------------------------------------------------------------------------


def test_remove_existing_vault(vault_a: Path, vault_b: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    out = remove_vault(reg, "second")
    assert [v.name for v in out.vaults] == ["main"]


def test_remove_active_leaves_no_active(
    vault_a: Path, vault_b: Path
) -> None:
    """Removing the active vault yields a registry with zero actives —
    user picks the next active explicitly via `lit vault use`."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    # main is active. Remove it.
    out = remove_vault(reg, "main")
    assert find_active(out) is None
    assert out.vaults[0].name == "second"


def test_remove_missing_vault_raises(vault_a: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    with pytest.raises(VaultRegistryError, match="No vault named"):
        remove_vault(reg, "ghost")


# ---------------------------------------------------------------------------
# set_active
# ---------------------------------------------------------------------------


def test_set_active_switches(vault_a: Path, vault_b: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = add_vault(reg, "second", vault_b)
    assert find_active(reg).name == "main"

    out = set_active(reg, "second")
    assert find_active(out).name == "second"
    by_name = {v.name: v for v in out.vaults}
    assert by_name["main"].is_active is False


def test_set_active_idempotent(vault_a: Path) -> None:
    """Setting the already-active vault active again is a no-op state-wise."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    out = set_active(reg, "main")
    assert find_active(out).name == "main"


def test_set_active_missing_name_raises(vault_a: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    with pytest.raises(VaultRegistryError, match="No vault named"):
        set_active(reg, "ghost")


# ---------------------------------------------------------------------------
# find_active / find_by_name / resolve_vault_param
# ---------------------------------------------------------------------------


def test_find_active_empty_registry() -> None:
    assert find_active(VaultRegistry()) is None


def test_find_by_name_returns_entry(vault_a: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    entry = find_by_name(reg, "main")
    assert entry is not None
    assert entry.name == "main"


def test_find_by_name_missing_returns_none(vault_a: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    assert find_by_name(reg, "ghost") is None


def test_resolve_vault_param_returns_path(vault_a: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    path = resolve_vault_param(reg, "main")
    assert path == vault_a.resolve()


def test_resolve_vault_param_unknown_raises(vault_a: Path) -> None:
    reg = add_vault(VaultRegistry(), "main", vault_a)
    with pytest.raises(VaultRegistryError, match="No vault named"):
        resolve_vault_param(reg, "ghost")


# ---------------------------------------------------------------------------
# find_vault() — discovery chain integration
# ---------------------------------------------------------------------------


def test_find_vault_explicit_wins_over_registry(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """--library / $LIT_LIBRARY (explicit) should win over the active vault."""
    save_registry(
        add_vault(VaultRegistry(), "active-one", vault_a),
    )
    # vault_b is NOT in the registry, but explicit should still pick it.
    assert find_vault(explicit=vault_b) == vault_b.resolve()


def test_find_vault_falls_back_to_registry_active(
    fake_home: Path, vault_a: Path
) -> None:
    """No explicit arg and no cwd-walk hit → use registry active."""
    save_registry(add_vault(VaultRegistry(), "main", vault_a))
    # Move cwd somewhere with no lit-config.yaml in any ancestor.
    import os
    os.chdir(fake_home)
    try:
        assert find_vault() == vault_a.resolve()
    finally:
        os.chdir(Path(__file__).parent)


def test_find_vault_registry_active_stale_raises(
    fake_home: Path, vault_a: Path
) -> None:
    """Active vault directory moved/deleted → LibraryNotFoundError with hint."""
    save_registry(add_vault(VaultRegistry(), "main", vault_a))
    # Simulate the directory disappearing.
    import shutil
    shutil.rmtree(vault_a)
    import os
    os.chdir(fake_home)
    try:
        with pytest.raises(LibraryNotFoundError, match="stale|no longer"):
            find_vault()
    finally:
        os.chdir(Path(__file__).parent)


def test_find_vault_falls_through_corrupt_registry(
    fake_home: Path, vault_a: Path
) -> None:
    """A corrupt registry should not brick find_vault; cwd-walk still wins."""
    # Drop a malformed registry.
    p = registry_path()
    p.parent.mkdir(parents=True)
    p.write_text("totally\nnot: yaml: : :", encoding="utf-8")
    # cd into vault_a so cwd-walk hits it.
    import os
    os.chdir(vault_a)
    try:
        assert find_vault() == vault_a.resolve()
    finally:
        os.chdir(Path(__file__).parent)


def test_find_vault_empty_registry_falls_through_to_cwd_walk(
    fake_home: Path, vault_a: Path
) -> None:
    """Registry exists but has no entries → cwd-walk still runs."""
    save_registry(VaultRegistry())
    import os
    os.chdir(vault_a)
    try:
        assert find_vault() == vault_a.resolve()
    finally:
        os.chdir(Path(__file__).parent)


def test_find_vault_registry_no_active_falls_through(
    fake_home: Path, vault_a: Path, vault_b: Path
) -> None:
    """Registry has entries but none active → cwd-walk runs."""
    reg = add_vault(VaultRegistry(), "main", vault_a)
    reg = remove_vault(reg, "main")  # leaves empty
    reg = add_vault(reg, "second", vault_b)
    # second is active (became so as the only entry). Manually clear actives.
    reg = VaultRegistry(
        vaults=[v.model_copy(update={"is_active": False}) for v in reg.vaults]
    )
    save_registry(reg)
    import os
    os.chdir(vault_a)
    try:
        assert find_vault() == vault_a.resolve()
    finally:
        os.chdir(Path(__file__).parent)


def test_find_vault_no_explicit_no_registry_no_cwd_raises(
    fake_home: Path, tmp_path: Path
) -> None:
    """Truly nothing to find → LibraryNotFoundError with helpful message."""
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    import os
    os.chdir(nowhere)
    try:
        with pytest.raises(LibraryNotFoundError, match="No lit-config.yaml"):
            find_vault()
    finally:
        os.chdir(Path(__file__).parent)
