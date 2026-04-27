"""Library / vault filesystem operations.

`create_vault()` builds the on-disk skeleton for a new literature vault and is
the primary action of ``lit init``. It is also exposed as a pure function so
tests and future programmatic callers can drive it without going through Click.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from litman.core.seeds import (
    TAXONOMY_SEED,
    render_index_seed,
    render_lit_config_seed,
)
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
)

DEFAULT_VAULT_NAME = "literature_vault"


def _git_init_and_commit(vault_path: Path) -> None:
    """Initialize git inside the vault and make an initial commit."""
    subprocess.run(["git", "init", "--quiet"], cwd=vault_path, check=True)
    subprocess.run(["git", "add", "."], cwd=vault_path, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "Vault initialized by `lit init`"],
        cwd=vault_path,
        check=True,
    )


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
        (vault / "INDEX.md").write_text(
            render_index_seed(datetime.now().strftime("%Y-%m-%d %H:%M")),
            encoding="utf-8",
        )
        (vault / "lit-config.yaml").write_text(
            render_lit_config_seed(library_name=name), encoding="utf-8"
        )

        _git_init_and_commit(vault)
    except Exception:
        # Roll back: if we created the root, remove the half-built tree.
        if created_root and vault.exists():
            shutil.rmtree(vault, ignore_errors=True)
        raise

    return vault


def find_vault(explicit: Path | None = None) -> Path:
    """Locate the active vault using the standard discovery chain.

    Resolution order:
        1. ``explicit`` argument if provided (e.g. from ``--library`` flag or
           ``LIT_LIBRARY`` environment variable surfaced through Click).
        2. Walk up from the current working directory looking for a directory
           that contains ``lit-config.yaml``.

    Args:
        explicit: Optional caller-supplied vault path.

    Returns:
        Absolute path to the discovered vault.

    Raises:
        LibraryNotFoundError: No ``lit-config.yaml`` discoverable.
    """
    if explicit is not None:
        candidate = explicit.resolve()
        if not (candidate / "lit-config.yaml").is_file():
            raise LibraryNotFoundError(
                f"No lit-config.yaml at {candidate}. "
                "Pass --library <vault-path> or run `lit init` first."
            )
        return candidate

    here = Path.cwd().resolve()
    for parent in [here, *here.parents]:
        if (parent / "lit-config.yaml").is_file():
            return parent

    raise LibraryNotFoundError(
        "No lit-config.yaml found in the current directory or any parent. "
        "Set LIT_LIBRARY, pass --library <vault-path>, or run `lit init` first."
    )
