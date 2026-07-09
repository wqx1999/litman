"""Machine-level agent preferences (task-agent-onboarding, ADR-020 / ADR-021).

The user's chosen default agent is a per-machine choice, not a per-vault one
("nobody picks their agent per library"), so it lives beside the vault
registry in a single global ``preferences.yaml`` — NOT inside any vault's
``lit-config.yaml``. Both ``lit agent`` (no NAME) and the GUI red-dot resolve
the default the same way: this file's ``default_agent`` if set, else the
catalog fallback (:func:`litman.core.agents.default_agent_name`).

Path resolution mirrors :func:`litman.core.vault_registry.registry_path`
exactly — ``$LITMAN_REGISTRY_DIR`` overrides the platformdirs default,
recomputed on every call — so the test suite's ``_isolate_registry`` fixture
(which points that env var at a tmp dir) isolates ``preferences.yaml`` for
free, and the file rides along with the registry on a cloud-synced config dir.

This writes a machine-global config file, NOT a vault TRUTH/DERIVED surface:
invariant #16 (the WebUI structured-write whitelist) does not apply and there
is no drift-ledger pair to register. The write is atomic (tmp-file +
``Path.replace``) for the same Windows read-only-lock reason the registry uses
(invariants dimension F).
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from platformdirs import user_config_dir

from litman.core.agents import get_agent
from litman.core.vault_registry import REGISTRY_APP_NAME, REGISTRY_ENV_VAR
from litman.core.yaml_pool import ThreadLocalYAML

PREFS_FILENAME = "preferences.yaml"

# Sole key stored in the file today: the chosen default agent name.
_DEFAULT_AGENT_KEY = "default_agent"

_yaml = ThreadLocalYAML(
    indent={"mapping": 2, "sequence": 4, "offset": 2},
    default_flow_style=False,
)


def prefs_path() -> Path:
    """Path to the machine-level ``preferences.yaml``.

    Recomputed on each call (not cached at import) so tests that redirect
    ``$LITMAN_REGISTRY_DIR`` see the new location. Resolution mirrors
    :func:`litman.core.vault_registry.registry_path`:

    1. ``$LITMAN_REGISTRY_DIR / preferences.yaml`` when the env var is set
       (and non-empty after strip).
    2. Otherwise ``platformdirs.user_config_dir("litman") / preferences.yaml``.
    """
    override = os.environ.get(REGISTRY_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser() / PREFS_FILENAME
    return Path(user_config_dir(REGISTRY_APP_NAME)) / PREFS_FILENAME


def load_default_agent() -> str | None:
    """Return the user's chosen default agent name, or ``None``.

    ``None`` (missing file / unreadable YAML / non-mapping top level / absent
    or non-string key) means "the user has not chosen yet" — the caller falls
    back to the catalog default and the GUI red dot stays lit.
    """
    path = prefs_path()
    if not path.is_file():
        return None
    try:
        raw = _yaml.load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get(_DEFAULT_AGENT_KEY)
    return value if isinstance(value, str) else None


def save_default_agent(name: str) -> None:
    """Persist ``name`` as the machine-level default agent (atomic write).

    Raises:
        ValueError: ``name`` is not a *supported* catalog agent. The endpoint
            layer turns that into an HTTP 400; the CLI into a friendly error.
    """
    spec = get_agent(name)
    if spec is None or not spec.supported:
        raise ValueError(
            f"Cannot set default agent to {name!r}: not a supported agent."
        )
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    buf.write(
        "# litman machine-level preferences (not per-vault). Managed by\n"
        "# `lit setup`, the GUI agent panel, and `lit agent --set-default`.\n\n"
    )
    _yaml.dump({_DEFAULT_AGENT_KEY: name}, buf)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    tmp.replace(path)


def remove_prefs() -> dict[str, object]:
    """Delete ``preferences.yaml``, honouring the same path resolution as
    :func:`prefs_path`. Counterpart of
    :func:`litman.core.vault_registry.remove_registry`, used by
    ``lit uninstall`` so the machine-level default-agent choice does not
    outlive the install. Removes the containing config dir too if it becomes
    empty (so a lone ``preferences.yaml`` no longer keeps the dir alive after
    the registry is gone).

    Returns ``{"path", "removed", "dir_removed"}``.
    """
    path = prefs_path()
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
