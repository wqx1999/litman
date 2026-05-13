# `lit-config.yaml` Schema

Each vault has one `lit-config.yaml` at its root. It carries
library-level preferences that the CLI reads once per command via
`load_config(vault)`. The on-disk form is validated against
`litman.core.config.LitConfig` (pydantic, `extra="forbid"`); typos and
wrong types fail fast as `ConfigError` instead of silent fallback.

This page documents every field, its default, and which command(s)
consume it. Print the live, parsed view for a given vault with:

```bash
lit config show
```

## Required fields

| Field | Type | Notes |
|---|---|---|
| `library_name` | string | Human-readable label. Conventionally matches the vault subdirectory name (`literature_vault` for the default `lit init` layout). |

## Optional fields (all have safe defaults)

### `default_pdf_viewer`

```yaml
default_pdf_viewer: null    # default
# default_pdf_viewer: okular
# default_pdf_viewer: /usr/bin/zathura
```

| Property | Value |
|---|---|
| Type | string or null |
| Default | `null` |
| Consumed by | `lit open` (M9.1) |

`null` falls back to the platform default (`open` on macOS,
`xdg-open` on Linux, `os.startfile` on Windows, `wslview` on WSL).
Set to a string to override; the CLI passes the PDF path as the
single argument.

### `view_definitions`

```yaml
view_definitions:
  - by-project
  - by-topic
  - by-method
  - by-status
```

| Property | Value |
|---|---|
| Type | list[string] |
| Default | `["by-project", "by-topic", "by-method", "by-status"]` |
| Consumed by | `lit refresh-views` |

Which `views/by-*/` symlink hubs `refresh-views` rebuilds.
Currently informational — the renderer hardcodes the same set;
documented for forward compatibility.

### `unique_keys`

```yaml
unique_keys:
  - doi
  - arxiv-id
```

| Property | Value |
|---|---|
| Type | list[string] |
| Default | `["doi", "arxiv-id"]` |
| Consumed by | `lit add` (M2.9) |

Fields used to detect duplicate papers during `lit add`. The DOI
precheck is fully wired (`lit add` refuses on duplicate DOI);
`arxiv-id` is informational pending a future milestone.

### `default_clone_depth`

```yaml
default_clone_depth: 1
```

| Property | Value |
|---|---|
| Type | integer ≥ 0 |
| Default | `1` |
| Consumed by | `lit code add`, `lit code restore-all` |

Shallow-clone depth for `lit code add` when `--depth` is not
specified. `0` means full history (non-shallow); `1` (default)
keeps repos small. Promote a shallow clone to full history later
with `lit code update --unshallow`.

### `codes_ignore_patterns`

```yaml
codes_ignore_patterns:
  - repo/
```

| Property | Value |
|---|---|
| Type | list[string] |
| Default | `["repo/"]` |
| Consumed by | `lit sync push` (M6.2) |

Glob patterns under `codes/` that backup tools (rclone, etc.)
exclude from cloud sync. The default keeps the bulky `repo/`
checkout out of L2 backup; rebuild on a fresh machine with
`lit code restore-all`.

### `projects`

```yaml
projects:
  pepforge: /work/wangq/Project/PepForge
  pepcodec: /work/wangq/Project/PepCodec
```

| Property | Value |
|---|---|
| Type | dict[string, string] |
| Default | `{}` |
| Consumed by | `lit link`, `lit unlink`, `lit refresh-views` |

Project name → project directory path. **Must be populated before
`lit link`** — the CLI refuses to link a paper to a project that is
not in this map. Paths are resolved per command; the project's
`literature/` subdirectory and `REFERENCES.md` are written under
each registered path during `refresh-views`.

### `sync`

```yaml
sync:
  remote: my-gdrive          # rclone remote name
  path: litman-vault/        # path inside the remote
  exclude_repos: false       # M6.2: skip codes/*/repo/ on sync
```

| Property | Value |
|---|---|
| Type | object or null |
| Default | `null` |
| Consumed by | `lit sync push`, `lit sync pull`, `lit sync status` |
| Populated by | `lit sync setup` |

`null` means cloud sync is not configured; sync subcommands raise
`SyncError` until `lit sync setup` completes.

The nested `sync` object's fields:

| Field | Type | Default | Notes |
|---|---|---|---|
| `remote` | string (required) | — | rclone remote name as registered in `rclone config`. Must match one of `rclone listremotes`. |
| `path` | string | `""` | Path inside the remote where the vault is mirrored. Empty syncs to the remote root. |
| `exclude_repos` | boolean | `false` | When `true`, `codes/*/repo/` checkouts are excluded by default. Re-derive on pull with `lit code restore-all`. |

The full rclone target URL is `<remote>:<path>`.

## Strict on unknown keys

The schema uses pydantic `extra="forbid"`. A misspelled key (e.g.
`default_pdf_viewr`) fails at load time:

```
ConfigError: Invalid lit-config.yaml at /work/me/literature_vault/lit-config.yaml:
  field 'default_pdf_viewr': Extra inputs are not permitted
```

This is intentional — silent fallback to defaults on typos has bitten
users in the past. If you want to record per-vault notes that aren't
part of the schema, put them in YAML comments (`#`) instead.

## What's deliberately NOT in the schema

- **LLM API keys / endpoints / models.** Claude Code (or any other
  agent) manages its own auth. litman is LLM-credential-agnostic; the
  CLI itself never makes an API call. See
  [philosophy: CLI must work standalone](philosophy.md#cli-must-work-standalone).
- **Per-paper overrides.** Anything paper-specific belongs in the
  paper's `metadata.yaml`, not in the config.
- **A "default project".** Linking a paper to a project is always an
  explicit `lit link --project <name>` — there is no implicit project
  context.

## Forward compatibility

Adding a new optional field to `LitConfig` does not break existing
vaults: pydantic supplies the default for any field absent from the
on-disk yaml. The seed in `core.seeds.render_lit_config_seed()` is
updated in lockstep so newly-initialized vaults' yaml is
self-documenting, but old vaults continue to load.
