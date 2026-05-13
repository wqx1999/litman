# Architecture

litman is built in four layers; each can run without the layer above it.

| Layer | Component | What it does |
|---|---|---|
| 4 | Claude Code + `lit-library` / `lit-reading` skills | Optional AI sugar. Teaches the agent how to navigate the vault and call the CLI. |
| 3 | `lit` CLI (Click + Rich) | User interface; argument parsing, friendly output, command dispatch. |
| 2 | `litman` Python package | Business logic — atomic writes, dedup, TAXONOMY parsing, sync, etc. Testable as a library. |
| 1 | Data on disk — YAML + Markdown + JSON | The source of truth. Plain text, no DB. Any tool can read it. |

This means the CLI works completely without Claude Code. The two
bundled skills are convenience for users who already use Claude Code;
they are not required for any data operation.

## Vault layout

```
<vault>/
├── lit-config.yaml         # vault config: projects, default clone depth, sync target, viewer
├── TAXONOMY.md             # controlled vocabulary (4 user dicts + 3 fixed enums)
├── INDEX.json              # auto-generated; primary query surface for AI agents
│
├── papers/
│   └── <id>/
│       ├── paper.pdf
│       ├── metadata.yaml
│       └── notes.md
│
├── codes/
│   └── <repo-name>/
│       ├── repo/           # git checkout
│       ├── repo-meta.yaml
│       └── notes.md
│
├── notes/                  # cross-paper notes; wikilinks resolve here too
│   ├── methods/
│   ├── ideas/
│   └── debates/
│
├── views/                  # symlink hubs faceted by metadata fields
│   ├── by-project/
│   ├── by-topic/
│   ├── by-method/
│   └── by-status/
│
├── inbox/                  # for triage workflows
├── .trash/                 # recoverable-delete bin (created lazily)
└── .litman-staging/        # atomic-op staging area; transient
```

The **vault registry** — independent of any one vault — lives at
`~/.config/litman/vaults.yaml` (overridable via `$LITMAN_REGISTRY_DIR`).

## Why this layering matters

The strict bottom-up dependency (L1 → L2 → L3 → L4) gives litman a
"weak-LLM-tolerant" property: if Claude Code is unavailable, or the
model deprecated, or the user prefers no agent at all, every data
operation still works through `lit` directly. AI is leverage, not
foundation.

The same property makes the data layer portable: a vault is a directory.
You can ship it as a tarball, sync it via rclone (see `lit sync`), share
a snapshot with a colleague (see [Vaults (multiple)](concepts.md#vaults-multiple)),
or open any individual `metadata.yaml` in a plain editor. The CLI exists
to keep cross-references consistent during edits, not to gate access.
