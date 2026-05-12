"""Library / vault filesystem operations.

`create_vault()` builds the on-disk skeleton for a new literature vault and is
the primary action of ``lit init``. It is also exposed as a pure function so
tests and future programmatic callers can drive it without going through Click.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from litman.core.seeds import TAXONOMY_SEED, render_lit_config_seed
from litman.core.views import write_index
from litman.exceptions import (
    LibraryNotFoundError,
    ParentNotFoundError,
    VaultExistsError,
)

# Subdirectories created inside every vault. Order is irrelevant; mkdir handles
# parents=True so nested paths (e.g. notes/methods) work directly.
VAULT_SUBDIRS: tuple[str, ...] = (
    "papers",
    "notes/methods",
    "notes/ideas",
    "notes/debates",
    "views/by-project",
    "views/by-topic",
    "views/by-method",
    "views/by-status",
    "inbox",
    "codes",
    ".litman-staging",
)

DEFAULT_VAULT_NAME = "literature_vault"


def create_vault(parent_dir: Path, name: str = DEFAULT_VAULT_NAME) -> Path:
    """Create a new vault at ``parent_dir / name``.

    On any failure after directory creation, the partially-built vault is
    removed (only when this function created the root directory itself; a
    pre-existing empty target is preserved).

    Args:
        parent_dir: Existing directory inside which the vault subdir is built.
        name: Vault subdirectory name. Defaults to ``literature_vault``.

    Returns:
        Absolute path to the newly created vault.

    Raises:
        ParentNotFoundError: ``parent_dir`` does not exist or is not a directory.
        VaultExistsError: ``parent_dir / name`` already exists and is non-empty.
    """
    parent = parent_dir.resolve()
    if not parent.is_dir():
        raise ParentNotFoundError(
            f"Parent directory does not exist or is not a directory: {parent}. "
            "Create it first or pass a different path."
        )

    vault = parent / name
    if vault.exists() and any(vault.iterdir()):
        raise VaultExistsError(
            f"Target vault path already exists and is non-empty: {vault}. "
            "Pick a different --name or remove the existing vault."
        )

    # Track whether we created the root so we know whether to remove it on
    # rollback. A pre-existing empty directory belongs to the user.
    created_root = not vault.exists()

    try:
        vault.mkdir(exist_ok=True)
        for sub in VAULT_SUBDIRS:
            (vault / sub).mkdir(parents=True, exist_ok=True)

        (vault / "TAXONOMY.md").write_text(TAXONOMY_SEED, encoding="utf-8")
        (vault / "lit-config.yaml").write_text(
            render_lit_config_seed(library_name=name), encoding="utf-8"
        )
        # INDEX.json seeded as the canonical empty form via the same renderer
        # that `lit refresh-views` uses, so the seed never drifts from the
        # regenerated output.
        write_index(vault, [])
    except Exception:
        # Roll back: if we created the root, remove the half-built tree.
        if created_root and vault.exists():
            shutil.rmtree(vault, ignore_errors=True)
        raise

    return vault


def find_vault(explicit: Path | None = None) -> Path:
    """Locate the active vault using the standard discovery chain.

    Resolution order (M8.1 added step 2):
        1. ``explicit`` argument if provided (e.g. from ``--library`` flag,
           ``LIT_LIBRARY`` environment variable surfaced through Click, or
           ``--vault NAME`` after M8.3 resolves the name to a path).
        2. The active vault from ``~/.config/litman/vaults.yaml`` if the
           registry exists and has an entry with ``is_active=true``.
           A *malformed* registry (corrupt yaml, schema mismatch) falls
           through to step 3 silently — we don't want a broken registry
           to brick every command. A *valid* registry whose active entry
           points at a directory that no longer holds a lit-config.yaml,
           however, raises explicitly: that's a misconfiguration the user
           needs to see, not silently route around.
        3. Walk up from the current working directory looking for a
           directory that contains ``lit-config.yaml``.

    Args:
        explicit: Optional caller-supplied vault path.

    Returns:
        Absolute path to the discovered vault.

    Raises:
        LibraryNotFoundError: No ``lit-config.yaml`` discoverable, or the
            registry's active entry points at a stale path.
    """
    if explicit is not None:
        candidate = explicit.resolve()
        if not (candidate / "lit-config.yaml").is_file():
            raise LibraryNotFoundError(
                f"No lit-config.yaml at {candidate}. "
                "Pass --library <vault-path> or run `lit init` first."
            )
        return candidate

    # Step 2: registry active vault. Local import avoids a circular dep
    # because vault_registry only imports stdlib + exceptions + pydantic.
    from litman.core.vault_registry import (
        VaultRegistryError,
        find_active,
        load_registry,
    )

    try:
        registry = load_registry()
    except VaultRegistryError:
        # Corrupt registry — fall through. The user will see the parse
        # error the next time they run a `lit vault` command, which is
        # the right surface for that diagnostic.
        registry = None

    if registry is not None:
        active = find_active(registry)
        if active is not None:
            active_path = Path(active.path)
            if (active_path / "lit-config.yaml").is_file():
                return active_path
            raise LibraryNotFoundError(
                f"Active vault {active.name!r} points at {active_path} "
                "but that directory no longer holds a lit-config.yaml. "
                "Fix the path (move the vault back, or `lit vault remove` "
                "and re-add), or `lit vault use <other-name>` to switch."
            )

    here = Path.cwd().resolve()
    for parent in [here, *here.parents]:
        if (parent / "lit-config.yaml").is_file():
            return parent

    raise LibraryNotFoundError(
        "No lit-config.yaml found in the current directory or any parent. "
        "Set LIT_LIBRARY, pass --library <vault-path>, register a vault "
        "with `lit vault add`, or run `lit init` first."
    )
