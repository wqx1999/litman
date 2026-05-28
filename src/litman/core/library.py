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
    VaultRegistryError,
)

# Subdirectories created inside every vault. Order is irrelevant; mkdir handles
# parents=True so nested paths (e.g. views/by-project) work directly.
VAULT_SUBDIRS: tuple[str, ...] = (
    "papers",
    "views/by-project",
    "views/by-topic",
    "views/by-method",
    "views/by-status",
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
    # Local import: atomic only depends on stdlib + rich, but importing it
    # at module top would couple the library skeleton builder to the
    # recovery machinery. recover_staging never calls find_vault, so this
    # introduces no recursion.
    from litman.core.atomic import ensure_vault_recovered

    if explicit is not None:
        candidate = explicit.resolve()
        if not (candidate / "lit-config.yaml").is_file():
            raise LibraryNotFoundError(
                f"No lit-config.yaml at {candidate}. "
                "Pass --library <vault-path> or run `lit init` first."
            )
        ensure_vault_recovered(candidate)
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
                ensure_vault_recovered(active_path)
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
            ensure_vault_recovered(parent)
            return parent

    raise LibraryNotFoundError(
        "No vault found. Run `lit init <parent-dir>` to create one (it "
        "registers automatically), or `lit vault add <name> <path>` to "
        "register an existing vault. Advanced: set $LIT_LIBRARY or pass "
        "--library <vault-path>."
    )


def resolve_library_or_vault(
    library: Path | None,
    vault_name: str | None,
) -> Path | None:
    """Collapse the ``--library`` / ``--vault`` CLI pair into one Path.

    M8.3 adds ``--vault NAME`` to every command that currently accepts
    ``--library``. The two are mutually exclusive: ``--library`` is a
    direct filesystem path, ``--vault`` is a name in the user-level
    registry. This helper enforces the mutual-exclusion rule and
    resolves the name when applicable.

    Args:
        library: Value passed to ``--library`` (or surfaced via the
            ``LIT_LIBRARY`` envvar through Click). ``None`` when not given.
        vault_name: Value passed to ``--vault NAME``. ``None`` when not given.

    Returns:
        A Path suitable to feed into ``find_vault(explicit=...)``, or
        ``None`` when neither option was supplied (find_vault then falls
        through to its own discovery chain: registry active → cwd-walk).

    Raises:
        VaultRegistryError: both options were passed, or ``vault_name``
            is not registered in ``~/.config/litman/vaults.yaml``.
    """
    if library is not None and vault_name is not None:
        raise VaultRegistryError(
            "--library and --vault are mutually exclusive. Pass one or "
            "the other, not both."
        )
    if vault_name is not None:
        # Local import keeps the module-load graph identical to find_vault's
        # registry-active branch and avoids a circular import in any future
        # rearrangement where vault_registry grows a dependency on library.
        from litman.core.vault_registry import (
            load_registry,
            resolve_vault_param,
        )

        return resolve_vault_param(load_registry(), vault_name)
    return library
