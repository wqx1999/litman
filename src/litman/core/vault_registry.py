"""User-level vault registry (M8.1).

litman supports multiple registered vaults on the same machine — one
"main" vault wangq curates plus any number of forks (snapshots received
from colleagues, archived projects, DR-restored backups). The registry
file lives in a per-user config directory; ``registry_path()`` resolves
it via the cross-platform precedence chain below. At most one entry has
``is_active: true`` at a time, and that entry is the fallback
``find_vault()`` resolves to when no explicit ``--library`` / ``--vault``
/ ``$LIT_LIBRARY`` is given.

Registry location resolution (precedence, highest first):

1. ``$LITMAN_REGISTRY_DIR/vaults.yaml`` when the env var is set. Use this
   to redirect the registry to a cloud-synced directory for backup /
   cross-machine sync. Persist the env var via your shell's startup file
   (``.bashrc`` / ``.zshrc`` / ``config.fish``). Beware: registry stores
   absolute vault paths, so cross-machine sync only works when each vault
   lives at the same path on every machine.
2. ``platformdirs.user_config_dir("litman") / vaults.yaml``. On Linux
   that's ``~/.config/litman/vaults.yaml`` (XDG); on macOS
   ``~/Library/Application Support/litman/vaults.yaml``; on Windows
   ``%APPDATA%\\litman\\vaults.yaml``.

Design choices baked in:

- **Registry lives at the user level**, not inside any single vault: a
  vault should not know which name it was registered under, and the
  same vault path may legitimately appear in multiple users' registries
  with different local names.
- **Names exclude ``:``** because M8.4 introduces cross-vault wikilinks
  of the form ``[[<vault-name>:<paper-id>]]`` — using ``:`` inside a
  name would make the parser ambiguous.
- **Names are checked case-fold-unique** so ``my-main`` and ``My-Main``
  cannot both register on case-insensitive filesystems (Windows, default
  macOS) where the two would collide if ever used as folder names.
- **At-most-one-active is enforced at the model layer**, not just by
  convention in the writers. A hand-edit that violates the invariant
  fails ``load_registry`` rather than silently confusing later commands.
- **Path is stored as an absolute string**, not ``pathlib.Path``, so
  yaml round-trip is plain text and the file is human-readable.
- **add_vault rejects paths that don't exist or lack lit-config.yaml.**
  No "register now, mount later" workflow — keeps the invariant clean
  that ``find_active()`` returns either ``None`` or a working vault.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML

from litman.core.id import find_case_fold_collision
from litman.exceptions import VaultRegistryError

# Env var that, when set, overrides the platformdirs default. Point at a
# cloud-synced directory to get backup + cross-machine sync for free.
REGISTRY_ENV_VAR = "LITMAN_REGISTRY_DIR"

# Application name fed to platformdirs. Resolves to ``~/.config/litman``
# on Linux, ``~/Library/Application Support/litman`` on macOS, and
# ``%APPDATA%\litman`` on Windows.
REGISTRY_APP_NAME = "litman"

REGISTRY_FILENAME = "vaults.yaml"

# Vault names share the shape rule with repo names (filesystem-safe,
# shell-friendly, no leading hyphen). The colon exclusion is M8.4-specific
# and enforced both by this regex and by an explicit check in
# ``is_valid_vault_name`` so a future reader sees the reason.
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")

_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False


def registry_path() -> Path:
    """Return the path to the vaults.yaml registry file.

    Computed on each call (not cached at import) so tests that monkeypatch
    ``HOME`` / ``$LITMAN_REGISTRY_DIR`` see the redirected location.

    Resolution chain:

    1. ``$LITMAN_REGISTRY_DIR / vaults.yaml`` when the env var is set
       (and non-empty after strip). Use to point at a cloud-synced
       directory for backup or cross-machine sync.
    2. Otherwise, ``platformdirs.user_config_dir("litman") / vaults.yaml``
       — XDG on Linux, ``~/Library/Application Support`` on macOS,
       ``%APPDATA%`` on Windows.
    """
    override = os.environ.get(REGISTRY_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser() / REGISTRY_FILENAME
    return Path(user_config_dir(REGISTRY_APP_NAME)) / REGISTRY_FILENAME


def registry_path_default() -> Path:
    """Return the default registry path ignoring ``$LITMAN_REGISTRY_DIR``.

    Used by the first-time prompt to show the user what the default
    location *would* be, even when they have already chosen to override it.
    """
    return Path(user_config_dir(REGISTRY_APP_NAME)) / REGISTRY_FILENAME


def is_valid_vault_name(name: str) -> bool:
    """Filesystem-safe + cross-vault-wikilink-safe name check.

    Rules:
    - Non-empty.
    - No ``:`` (would collide with the ``[[vault:id]]`` wikilink prefix).
    - No path separators (``/`` / ``\\``).
    - No leading ``-`` (would parse as a shell flag).
    - Subsequent characters are alphanumeric plus ``._-``.
    """
    if not name:
        return False
    if ":" in name or "/" in name or "\\" in name:
        return False
    return bool(_VALID_NAME_RE.match(name))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class VaultEntry(BaseModel):
    """One vault registered in the user-level registry.

    Frozen — every operation returns a new ``VaultEntry`` / ``VaultRegistry``
    rather than mutating in place. The immutable update pattern keeps
    invariant validation centralized in the ``model_validator`` below.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        ...,
        description="Unique handle for this vault in the registry.",
    )
    path: str = Field(
        ...,
        min_length=1,
        description="Absolute filesystem path to the vault root.",
    )
    imported_from: str | None = Field(
        default=None,
        description=(
            "Free-form provenance for forked vaults (e.g. 'Zhang via USB "
            "drop 2026-05'). None for vaults wangq created locally."
        ),
    )
    imported_at: str | None = Field(
        default=None,
        description=(
            "ISO 8601 date string when the vault was added to the registry. "
            "None when not provided to ``add_vault``."
        ),
    )
    is_active: bool = Field(
        default=False,
        description=(
            "True if this is the default vault selected when no explicit "
            "--vault / --library / $LIT_LIBRARY is given."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not is_valid_vault_name(v):
            raise ValueError(
                f"Invalid vault name {v!r}: must match "
                "[A-Za-z0-9_][A-Za-z0-9._-]* (filesystem-safe, no leading "
                "hyphen, no ':' which is reserved for cross-vault wikilinks)."
            )
        return v


class VaultRegistry(BaseModel):
    """Root of ``~/.config/litman/vaults.yaml``.

    Invariants enforced at validation time:
    - All vault names are unique.
    - At most one vault is marked ``is_active``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vaults: list[VaultEntry] = Field(
        default_factory=list,
        description="Ordered list of registered vaults.",
    )

    @model_validator(mode="after")
    def _check_invariants(self) -> "VaultRegistry":
        names = [v.name for v in self.vaults]
        if len(names) != len(set(names)):
            dups = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(
                f"Duplicate vault name(s) in registry: {', '.join(dups)}"
            )
        actives = [v.name for v in self.vaults if v.is_active]
        if len(actives) > 1:
            raise ValueError(
                "At most one vault may be active at a time; found "
                f"{len(actives)}: {', '.join(actives)}"
            )
        return self


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_registry() -> VaultRegistry:
    """Read ``~/.config/litman/vaults.yaml`` or return an empty registry.

    A *missing* file is normal (fresh install) and resolves to an empty
    registry — not an error. A *malformed* file (parse failure, invariant
    violation, schema mismatch) is treated as an explicit corruption and
    raises ``VaultRegistryError`` so the caller can surface a clear
    message rather than silently dropping the user's data.
    """
    path = registry_path()
    if not path.is_file():
        return VaultRegistry()
    try:
        raw = _yaml.load(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise VaultRegistryError(
            f"Failed to parse {path} as YAML: {e}"
        ) from e
    if raw is None:
        return VaultRegistry()
    if not isinstance(raw, dict):
        raise VaultRegistryError(
            f"{path} must contain a YAML mapping at the top level, got "
            f"{type(raw).__name__}."
        )
    try:
        return VaultRegistry.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise VaultRegistryError(
            f"Invalid registry at {path}:\n  field '{loc}': "
            f"{first['msg']}\n(full report: {e})"
        ) from e


def save_registry(reg: VaultRegistry) -> None:
    """Persist ``reg`` to ``~/.config/litman/vaults.yaml`` atomically.

    Creates the parent directory if it does not yet exist (fresh install
    case). Writes via tmp-file + ``Path.replace`` so a crash mid-write
    cannot leave a half-serialized registry on disk.
    """
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "vaults": [v.model_dump(mode="python") for v in reg.vaults],
    }
    buf = io.StringIO()
    buf.write(
        "# litman vault registry. Managed by `lit vault {add,use,remove}`.\n"
        "# Do not hand-edit — the CLI preserves invariants (unique names,\n"
        "# at most one active vault) that a careless edit can break.\n\n"
    )
    _yaml.dump(payload, buf)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Mutation helpers (pure — return a new VaultRegistry; caller persists)
# ---------------------------------------------------------------------------


def _clear_all_active(entries: list[VaultEntry]) -> list[VaultEntry]:
    """Return copies with ``is_active=False`` for every entry."""
    return [v.model_copy(update={"is_active": False}) for v in entries]


def ensure_name_registrable(reg: VaultRegistry, name: str) -> None:
    """Raise ``VaultRegistryError`` if ``name`` cannot be added to ``reg``.

    Checks name shape, exact-duplicate, and case-fold collision — the three
    name-level constraints that do not depend on the vault path existing yet.
    ``lit init`` calls this *before* creating the vault directory so a name
    clash aborts cleanly (no orphan vault left on disk); ``add_vault`` calls
    it too so the single source of truth for name rules lives here.
    """
    if not is_valid_vault_name(name):
        raise VaultRegistryError(
            f"Invalid vault name {name!r}: must match "
            "[A-Za-z0-9_][A-Za-z0-9._-]* (filesystem-safe, no leading "
            "hyphen, no ':' which is reserved for cross-vault wikilinks)."
        )
    if any(v.name == name for v in reg.vaults):
        raise VaultRegistryError(
            f"Vault {name!r} is already registered. Run `lit vault list` "
            "to see existing vaults or pick a different name."
        )
    # Cross-platform safety (ADR-005): forbid names that case-fold to an
    # existing entry. Vault names appear in cross-vault wikilinks
    # ``[[<vault>:<id>]]`` and may end up as folder names in user-side
    # tooling; same-fold names break when the registry or any derived
    # artifact moves to Windows / default macOS.
    case_clash = find_case_fold_collision([v.name for v in reg.vaults], name)
    if case_clash is not None:
        raise VaultRegistryError(
            f"Vault name {name!r} differs only in case from existing "
            f"vault {case_clash!r}. Same-fold names collide on Windows "
            "/ default macOS filesystems. Pick a distinct name."
        )


def add_vault(
    reg: VaultRegistry,
    name: str,
    path: Path | str,
    *,
    imported_from: str | None = None,
    imported_at: str | None = None,
    set_active: bool = False,
) -> VaultRegistry:
    """Return a new registry with ``name`` added.

    Validates name shape, name uniqueness, and that ``path`` resolves to
    an existing directory containing a ``lit-config.yaml``. Behavioral
    rules around the active flag:

    - If the registry is currently empty, the new entry is forced active
      (a registry with no active entry is allowed but immediately
      unusable, so we save the user the extra ``lit vault use`` step).
    - If ``set_active=True``, every existing entry's ``is_active`` is
      cleared before the new entry is appended with ``is_active=True``.
    - Otherwise the new entry's ``is_active`` defaults to ``False``.

    Raises:
        VaultRegistryError: invalid name shape, duplicate name, missing
            directory, or directory not a vault (no lit-config.yaml).
    """
    # Name-level checks (shape / duplicate / case-fold) live in the shared
    # helper so ``lit init`` can run them before creating the vault dir.
    ensure_name_registrable(reg, name)

    abs_path = Path(path).expanduser().resolve()
    if not abs_path.is_dir():
        raise VaultRegistryError(
            f"Cannot register {name!r}: {abs_path} is not an existing "
            "directory. Did you mean a different path? Run `lit init` "
            "if you need to create a new vault."
        )
    if not (abs_path / "lit-config.yaml").is_file():
        raise VaultRegistryError(
            f"Cannot register {name!r}: {abs_path} has no lit-config.yaml. "
            "That directory is not a litman vault."
        )

    auto_active = len(reg.vaults) == 0
    will_be_active = set_active or auto_active

    base_entries = _clear_all_active(reg.vaults) if will_be_active else list(reg.vaults)
    new_entry = VaultEntry(
        name=name,
        path=str(abs_path),
        imported_from=imported_from,
        imported_at=imported_at,
        is_active=will_be_active,
    )
    return VaultRegistry(vaults=base_entries + [new_entry])


def remove_vault(reg: VaultRegistry, name: str) -> VaultRegistry:
    """Return a new registry with ``name`` unregistered.

    Removing the active vault leaves the registry with no active entry
    until the user runs ``lit vault use <other>``. We deliberately do NOT
    auto-promote another vault — picking which one is the user's call,
    not a guess we should make.

    Raises:
        VaultRegistryError: ``name`` is not in the registry.
    """
    if not any(v.name == name for v in reg.vaults):
        raise VaultRegistryError(
            f"No vault named {name!r} in the registry. Run `lit vault list` "
            "to see what's registered."
        )
    remaining = [v for v in reg.vaults if v.name != name]
    return VaultRegistry(vaults=remaining)


def set_active(reg: VaultRegistry, name: str) -> VaultRegistry:
    """Return a new registry with ``name`` marked active and all others not.

    Raises:
        VaultRegistryError: ``name`` is not in the registry.
    """
    if not any(v.name == name for v in reg.vaults):
        raise VaultRegistryError(
            f"No vault named {name!r} in the registry. Run `lit vault list` "
            "to see what's registered."
        )
    updated = [
        v.model_copy(update={"is_active": (v.name == name)}) for v in reg.vaults
    ]
    return VaultRegistry(vaults=updated)


# ---------------------------------------------------------------------------
# Read helpers (no mutation)
# ---------------------------------------------------------------------------


def find_active(reg: VaultRegistry) -> VaultEntry | None:
    """Return the active vault entry, or ``None`` if no entry is active."""
    for v in reg.vaults:
        if v.is_active:
            return v
    return None


def find_by_name(reg: VaultRegistry, name: str) -> VaultEntry | None:
    """Return the entry named ``name`` or ``None``."""
    for v in reg.vaults:
        if v.name == name:
            return v
    return None


def find_dangling(reg: VaultRegistry) -> list[VaultEntry]:
    """Return registered vault entries whose ``path`` no longer exists on disk.

    "Dangling" here means **the directory itself is missing** (``Path.exists()``
    returns False). A directory that exists but no longer holds a
    ``lit-config.yaml`` is a different failure mode handled by ``find_vault()``
    — we intentionally don't conflate the two so drift surfacing can be loud
    (yes/no prompt to prune) while corrupted-vault has its own targeted error.

    Order matches ``reg.vaults`` so callers can render the list deterministically.
    """
    return [v for v in reg.vaults if not Path(v.path).exists()]


def resolve_vault_param(reg: VaultRegistry, name: str) -> Path:
    """Return the absolute path of vault ``name`` for ``--vault`` plumbing.

    Used by M8.3's transparent layer: each command resolves ``--vault NAME``
    via this helper, then feeds the resulting Path to the existing
    ``find_vault()`` entry point.

    Raises:
        VaultRegistryError: ``name`` is not in the registry.
    """
    entry = find_by_name(reg, name)
    if entry is None:
        names = ", ".join(v.name for v in reg.vaults) or "(none registered)"
        raise VaultRegistryError(
            f"No vault named {name!r} in the registry. Available: {names}. "
            "Run `lit vault add` to register one."
        )
    return Path(entry.path)
