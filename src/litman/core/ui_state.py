"""Machine-level GUI state (task-gui-pin): the per-vault pinned-paper lists.

A pin is working state — "these are the papers I'm going back and forth
between this week" — not a property of the paper, so it deliberately lives
OUTSIDE the vault: not in ``metadata.yaml`` (any metadata write bumps
``updated-at``, which is the recency sort key — pinning would itself reorder
the list it is trying to stabilize), and not as a new vault-root file (the
webUI's direct-write surface is a closed whitelist, invariant #16). Instead
it sits beside the vault registry in a single global ``ui-state.json``,
exactly like :mod:`litman.core.agent_prefs`:

This writes a machine-global config file, NOT a vault TRUTH/DERIVED surface:
invariant #16 (the WebUI structured-write whitelist) does not apply and there
is no drift-ledger pair to register. The write is atomic (tmp-file +
``Path.replace``) for the same Windows read-only-lock reason the registry
uses (invariants dimension F).

Path resolution mirrors :func:`litman.core.vault_registry.registry_path`
exactly — ``$LITMAN_REGISTRY_DIR`` overrides the platformdirs default,
recomputed on every call — so the test suite's ``_isolate_registry`` fixture
isolates ``ui-state.json`` for free.

Pins are keyed per vault (registry name when the vault is registered, its
resolved path otherwise) so switching vaults in the GUI never leaks one
library's pins into another. List order IS pin order: append-on-pin, oldest
first — the pinned block must never reorder under the user.

File shape (self-describing, so a future CLI reader needs no migration)::

    {"pins": {"<vault-key>": ["<paper-id>", ...]}}
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from platformdirs import user_config_dir

from litman.core.vault_registry import (
    REGISTRY_APP_NAME,
    REGISTRY_ENV_VAR,
    load_registry,
)

UI_STATE_FILENAME = "ui-state.json"

# Top-level key holding the per-vault pin lists.
_PINS_KEY = "pins"


def ui_state_path() -> Path:
    """Path to the machine-level ``ui-state.json``.

    Recomputed on each call (not cached at import) so tests that redirect
    ``$LITMAN_REGISTRY_DIR`` see the new location. Resolution mirrors
    :func:`litman.core.vault_registry.registry_path`:

    1. ``$LITMAN_REGISTRY_DIR / ui-state.json`` when the env var is set
       (and non-empty after strip).
    2. Otherwise ``platformdirs.user_config_dir("litman") / ui-state.json``.
    """
    override = os.environ.get(REGISTRY_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser() / UI_STATE_FILENAME
    return Path(user_config_dir(REGISTRY_APP_NAME)) / UI_STATE_FILENAME


def vault_key(vault: Path) -> str:
    """Stable per-vault key into the state file.

    The registered vault *name* when ``vault`` matches a registry entry —
    names survive ``lit vault set-path`` (moving a library keeps its pins) —
    else the resolved path string (a vault found by walking up from cwd,
    never registered). A corrupt registry falls through to the path key:
    UI state must degrade to "no pins", never propagate a registry error.
    """
    resolved = vault.resolve()
    try:
        reg = load_registry()
    except Exception:
        return str(resolved)
    for entry in reg.vaults:
        try:
            if Path(entry.path).expanduser().resolve() == resolved:
                return entry.name
        except OSError:
            continue
    return str(resolved)


def _load_state() -> dict[str, object]:
    """The whole state file as a dict; ``{}`` on missing/garbage.

    Tolerant on purpose (same contract as ``agent_prefs.load_default_agent``):
    a corrupt UI-state file must degrade to "no pins", never break the GUI.
    """
    path = ui_state_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def load_pins(vault: Path) -> list[str]:
    """The pinned paper ids for ``vault``, oldest pin first.

    Missing file / unparseable JSON / wrong shape at any level all yield
    ``[]`` — this never raises. Non-string entries are dropped rather than
    poisoning the list.
    """
    pins = _load_state().get(_PINS_KEY)
    if not isinstance(pins, dict):
        return []
    ids = pins.get(vault_key(vault))
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str)]


def save_pins(vault: Path, ids: list[str]) -> None:
    """Persist ``ids`` as ``vault``'s pin list (atomic, other vaults kept).

    Read-modify-write of the whole file so other vaults' pin lists survive;
    an empty ``ids`` removes the vault's key entirely (clearing your pins
    should not leave husks behind). tmp + ``Path.replace``, never a naive
    ``open(path, "w")`` — a crash mid-write or a Windows read-only lock must
    not eat every vault's pins.
    """
    state = _load_state()
    pins = state.get(_PINS_KEY)
    if not isinstance(pins, dict):
        pins = {}
    key = vault_key(vault)
    if ids:
        pins[key] = list(ids)
    else:
        pins.pop(key, None)
    state[_PINS_KEY] = pins

    path = ui_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def remove_ui_state() -> dict[str, object]:
    """Delete ``ui-state.json``; counterpart of ``agent_prefs.remove_prefs``.

    Used by ``lit uninstall`` so pin state does not outlive the install.
    Removes the containing config dir too if it becomes empty (this runs
    last in the uninstall sequence, so it gets the rmdir chance the earlier
    removers pass up while siblings still exist).

    Returns ``{"path", "removed", "dir_removed"}``.
    """
    path = ui_state_path()
    if not path.is_file():
        return {"path": path, "removed": False, "dir_removed": False}
    path.unlink()
    dir_removed = False
    parent = path.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            dir_removed = True
    except OSError:
        pass
    return {"path": path, "removed": True, "dir_removed": dir_removed}
