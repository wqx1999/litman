"""``lit-config.yaml`` schema + loader.

The vault's ``lit-config.yaml`` is the single source for library-level
preferences (default viewer, view set, dedup keys, default clone depth,
etc.). M2.0–M2.1 treated the file as a marker (its mere existence
identified the vault root); M2.2 adds typed parsing with pydantic so
typos / wrong types surface as explicit ``ConfigError`` instead of
mysterious runtime behavior downstream.

Design constraints honored here (see design doc §4.2):

- **No LLM credentials.** Anthropic API keys live with Claude Code, not
  this file. The schema deliberately has no field for them.
- **All fields have safe defaults.** Existing vaults whose
  ``lit-config.yaml`` was written before a field was added still load —
  the missing field materializes at its default. Only ``library_name``
  is required.
- **Strict on unknown keys.** ``extra="forbid"`` catches typos (e.g.
  ``default_pdf_viewr``) instead of silently ignoring them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from litman.core.yaml_pool import ThreadLocalYAML
from litman.exceptions import ConfigError

# Canonical filename — matches the seed written by ``lit init`` and the
# discovery probe used by ``find_vault``.
CONFIG_FILENAME = "lit-config.yaml"

# Default value sets. These are the *schema* defaults — i.e. what a vault
# whose ``lit-config.yaml`` omits a field will compute. They must stay in
# sync with whatever ``seeds.py`` writes for newly-initialized vaults, but
# the two paths are deliberately separate: the seed exists so the file is
# self-documenting on disk, the schema exists so omitted fields still work.
DEFAULT_PDF_VIEWER: str | None = None
DEFAULT_VIEW_DEFINITIONS: tuple[str, ...] = (
    "by-project",
    "by-topic",
    "by-method",
    "by-status",
)
DEFAULT_UNIQUE_KEYS: tuple[str, ...] = ("doi", "arxiv-id")
DEFAULT_CLONE_DEPTH = 1
DEFAULT_CODES_IGNORE_PATTERNS: tuple[str, ...] = ("repo/",)

_yaml = ThreadLocalYAML(typ="safe")


class SyncConfig(BaseModel):
    """rclone-backed cloud sync configuration (M6).

    Populated by ``lit sync setup``. Absent on freshly-initialized vaults;
    sync subcommands raise ``SyncError`` until ``sync`` is configured.

    Fields:
        remote: rclone remote name as registered in ``rclone config``
            (e.g. ``my-gdrive``). Must match an entry in ``rclone listremotes``.
        path: Path inside the remote where the vault is mirrored
            (e.g. ``litman-vault/``). May be empty to sync to the remote root.
        exclude_repos: When ``True``, M6.2's ``--exclude-repos`` style filter
            is applied by default: ``codes/*/repo/`` checkouts are NOT
            uploaded. Re-derive on pull with ``lit code restore-all``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    remote: str = Field(
        ...,
        min_length=1,
        description="rclone remote name (must exist in `rclone listremotes`).",
    )
    path: str = Field(
        default="",
        description="Path inside the remote. Empty syncs to remote root.",
    )
    exclude_repos: bool = Field(
        default=False,
        description=(
            "If true, codes/*/repo/ checkouts are excluded from sync by "
            "default (M6.2). Re-derive on pull via `lit code restore-all`."
        ),
    )

    def target_url(self) -> str:
        """Return the full rclone target ``"<remote>:<path>"``.

        rclone expects ``remote:`` (with trailing colon) for the remote root
        and ``remote:path/`` for a subpath. We always emit the colon so the
        result is unambiguously a remote target, never a local path.
        """
        return f"{self.remote}:{self.path}"


class LitConfig(BaseModel):
    """Typed view of ``lit-config.yaml``.

    Adding a new field: pick a safe default so older configs still load,
    document the field's purpose in the ``Field(..., description=...)``
    argument, and update ``core.seeds.render_lit_config_seed`` so the
    on-disk form for new vaults reflects the addition.

    Wiring a field into a command: read it via ``load_config(vault)`` in
    the command, NOT by importing the default constant — the user may have
    overridden the value in their vault's yaml.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    library_name: str = Field(
        ...,
        description=(
            "Human-readable label for the library. Conventionally matches "
            "the vault subdirectory name."
        ),
    )
    default_pdf_viewer: str | None = Field(
        default=DEFAULT_PDF_VIEWER,
        description=(
            "Command used by `lit open` to launch a paper's PDF (M9.1). "
            "``null`` (the default) falls back to the platform default: "
            "``open`` (macOS), ``xdg-open`` (Linux), ``os.startfile`` "
            "(Windows), or ``wslview`` (WSL). Set to a string "
            "(e.g. 'okular', 'skim', '/usr/bin/zathura', 'code') to override."
        ),
    )
    view_definitions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_VIEW_DEFINITIONS),
        description=(
            "Legacy key — parsed but never read. The hub set "
            "`lit refresh-views` rebuilds is fixed in code. No longer "
            "seeded into new libraries; declared so older libraries that "
            "carry it still load (the schema forbids unknown keys)."
        ),
    )
    unique_keys: list[str] = Field(
        default_factory=lambda: list(DEFAULT_UNIQUE_KEYS),
        description=(
            "Legacy key — parsed but never read. `lit add`'s DOI duplicate "
            "precheck is fixed in code. No longer seeded into new "
            "libraries; declared so older libraries that carry it still "
            "load (the schema forbids unknown keys)."
        ),
    )
    default_clone_depth: int = Field(
        default=DEFAULT_CLONE_DEPTH,
        ge=0,
        description=(
            "Default --depth for `lit code add` / `lit code restore-all`. "
            "0 means full history (non-shallow clone). 1 (the default) is a "
            "shallow clone — promote later with `lit code update --unshallow`."
        ),
    )
    codes_ignore_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CODES_IGNORE_PATTERNS),
        description=(
            "Glob patterns under codes/ that backup tools (rclone, etc.) "
            "should exclude from cloud sync. Default ['repo/'] — keep the "
            "bulky checkout out of L2 backup, rebuild via "
            "`lit code restore-all`. Informational; consumed by M6 backup."
        ),
    )
    projects: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Registry of project_name → project_directory_path. Consumed "
            "by M5 `lit link/unlink/refresh-views` to know where to drop "
            "the project's `litman_reflib/` symlinks and regenerate "
            "REFERENCES.md. Stored as strings (not Path) so the yaml "
            "round-trip is plain; paths are resolved per command. Empty "
            "default — populate as `pepforge: /work/wangq/Project/PepForge` "
            "before linking papers to that project."
        ),
    )
    sync: SyncConfig | None = Field(
        default=None,
        description=(
            "rclone cloud sync target (M6). None means sync is not yet "
            "configured; `lit sync setup` populates this field. Once set, "
            "`lit sync push/pull/status` operate against it."
        ),
    )


def load_config(vault: Path) -> LitConfig:
    """Load + validate ``<vault>/lit-config.yaml``.

    Args:
        vault: Vault root (the directory that contains ``lit-config.yaml``).

    Returns:
        A populated ``LitConfig``. Missing optional fields take their
        schema defaults; required fields raise.

    Raises:
        ConfigError: file missing, unreadable, malformed YAML, top-level is
            not a mapping, contains an unknown key, or a field fails its
            type / constraint.
    """
    path = vault / CONFIG_FILENAME
    if not path.is_file():
        raise ConfigError(
            f"No {CONFIG_FILENAME} at {path}. Is {vault} really a vault? "
            "Run `lit init` first."
        )
    try:
        raw = _yaml.load(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ConfigError(
            f"Failed to parse {path} as YAML: {e}"
        ) from e
    if raw is None:
        # Empty file — fall back to defaults but library_name is required so
        # this still surfaces as a validation error below.
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} must contain a YAML mapping at the top level, got "
            f"{type(raw).__name__}."
        )
    # Legacy per-vault agent keys from the unreleased agent-launch cycle
    # (task-agent-launch, never shipped to PyPI). Agent config moved to the
    # machine-level preferences.yaml (task-agent-onboarding); drop the old
    # keys silently so a dogfood vault seeded with them still loads under
    # ``extra="forbid"``.
    raw.pop("agents", None)
    raw.pop("default_agent", None)
    try:
        return LitConfig.model_validate(raw)
    except ValidationError as e:
        # Pydantic's default message is multi-line; flatten the first error
        # for a friendlier CLI display while preserving the full report.
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise ConfigError(
            f"Invalid {CONFIG_FILENAME} at {path}:\n  field '{loc}': "
            f"{first['msg']}\n(full report: {e})"
        ) from e


def config_to_yaml_dict(config: LitConfig) -> dict[str, Any]:
    """Render a ``LitConfig`` as a plain dict suitable for YAML dumping.

    Used by ``lit config show`` and by tests that round-trip the schema.
    Field order follows the model definition so the printed form is stable.
    """
    return config.model_dump(mode="python")
