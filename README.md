# litman

**Local-first, AI-augmented literature management CLI.**
One paper, one folder, one structured metadata file — governed by a
controlled vocabulary, queried by a fast index, and designed from day one
to be navigated by a Claude Code agent.

> **Status: Alpha.** Core CLI is feature-complete (M0–M5, M8, M9 tagged
> through `v0.9.0-m9`). Two Claude Code skills are bundled. PyPI release
> pending. See [Project status](#project-status) for the full roadmap.

---

## What is litman

litman manages a personal literature vault — a folder on disk holding
hundreds of papers, each in its own subfolder with a YAML metadata file,
a markdown notes file, and the original PDF. The same vault can be
shared across multiple projects: one paper, many project bindings.

Everything is plain text on your filesystem. No cloud database, no
proprietary container format. You can edit any file by hand, `grep` the
whole vault, or hand the directory tree to an AI agent. The CLI exists
to make routine edits atomic and to keep cross-references consistent,
not to gate access to your data.

## What makes litman tick

Five design choices, in priority order:

1. **Local-first storage.** Everything is plain text on your
   filesystem — YAML metadata, markdown notes, original PDFs. You can
   edit any file by hand, `grep` the whole library, back it up
   anywhere. No cloud database, no proprietary container format.

2. **Controlled vocabulary with atomic operations.** Topics, methods,
   projects, and data sources are governed by a shared `TAXONOMY.md`
   file. Renames, merges, and removals cascade across every referencing
   paper in a single atomic operation — the vocabulary stays clean as
   the library grows from tens to hundreds of papers.

3. **AI-native CLI.** Two bundled Claude Code skills (`lit-library` for
   ingestion and retrieval, `lit-reading` for reading assistance) teach
   an agent how to navigate the vault and operate the CLI on your
   behalf. The agent emits structured JSON; the CLI validates and
   writes the data — your library stays correct even when the model
   isn't perfect.

4. **Paper ↔ project ↔ code triangle.** One paper can be bound to
   multiple projects without duplication; each project gets its own
   symlinked working folder and an auto-generated `REFERENCES.md`. Each
   paper can also be bound to its official code repository, cloned
   inside the vault next to the metadata.

5. **An agent-readable knowledge graph.** Metadata fields, wikilinks,
   and symlinks together form a knowledge graph that emerges
   naturally — no manual maintenance of "double-linked" notes. Because
   the vocabulary is controlled, an AI agent can do precise
   cross-paper retrieval, not just keyword search.

Together these mean: your data lives on your disk, stays consistent
over time, and an AI assistant can actually understand it.

---

## Install

```bash
# Recommended: use a dedicated conda env so litman's deps stay isolated.
conda create -n litman python=3.12
conda activate litman

# From a clone of this repository:
pip install -e ".[dev]"

# Sanity check.
lit --help
lit hello
```

Dependencies (auto-installed): `click`, `ruamel.yaml`, `httpx`, `pypdf`,
`pydantic`, `rich`, `platformdirs`.

PyPI release (`pipx install litman`) is on the roadmap, not yet shipped.

## Quick start

```bash
# 1. Create a vault. You pass the PARENT dir; the CLI creates the vault subdir.
lit init /work/me/                  # → /work/me/literature_vault/

# 2. Point your shell at it (drop into ~/.bashrc / ~/.zshrc).
export LIT_LIBRARY=/work/me/literature_vault

# 3. Add your first paper (CrossRef fetch).
lit add ~/Downloads/attention_is_all_you_need.pdf --doi 10.48550/arXiv.1706.03762

# 4. Browse.
lit list
lit show 2017_Vaswani_Attention
lit open 2017_Vaswani_Attention      # launches your configured PDF viewer

# 5. Classify (registers a topic, then tags the paper with it).
lit taxonomy add topics transformer attention
lit modify 2017_Vaswani_Attention --add-tag topics=transformer

# 6. Bind to a project (project must be declared in lit-config.yaml first).
lit link 2017_Vaswani_Attention --project MyResearchProject

# 7. Install the Claude Code skills (optional, lets the agent navigate your vault).
lit install-skill
```

---

## Core concepts

This section defines the vocabulary used throughout the CLI. Read it
once — every command builds on these terms.

### Vault

A **vault** is the root folder of a literature library. It is a plain
directory on disk with a fixed skeleton (see [Vault layout](#vault-layout)).
You can have multiple vaults (e.g. your own + a colleague's snapshot) and
switch between them with `lit vault use`.

A vault is identified to the CLI in one of four ways, in priority order:

1. `--vault <name>` — a name registered with `lit vault add`
2. `--library <path>` — explicit filesystem path
3. `$LIT_LIBRARY` — environment variable
4. cwd-walk — `lit` walks up from your current directory looking for
   the vault marker

### Paper folder

Each paper lives in its own folder: `<vault>/papers/<id>/` containing

```
papers/<id>/
├── paper.pdf       # the original PDF
├── metadata.yaml   # structured fields (see below)
└── notes.md        # your personal notes (markdown)
```

### Paper id

A paper id is the on-disk handle for a paper — both the folder name and
the value other places use to refer to it. The default format is
`<year>_<Family>_<Keyword>`, derived from the first author's family
name and a title keyword:

```
2017_Vaswani_Attention
2024_Wang_AMP-design
```

Ids are case-sensitive on Linux but litman refuses near-duplicates that
differ only in case, so vaults move safely between Linux / macOS /
Windows. To change an id, use `lit rename` — never `mv`; `lit rename`
rewrites every back-reference (other papers' `related`, notes wikilinks,
project symlinks).

### metadata.yaml

Every paper's structured data lives in `metadata.yaml`. The schema is
**intentionally flexible**: missing a field means "this dimension does
not apply to this paper". Only the identity layer (id / title / year) is
expected.

```yaml
# Identity (auto-filled by `lit add` from CrossRef or LLM JSON)
id: 2017_Vaswani_Attention
title: "Attention is all you need"
authors: ["Vaswani, Ashish", "Shazeer, Noam", ...]
year: 2017
journal: "NeurIPS"
doi: "10.48550/arXiv.1706.03762"
arxiv-id: "1706.03762"
github: null

# Audit (machine-maintained — never edit by hand)
created-at: "2026-05-13T10:00:00+00:00"
updated-at: "2026-05-13T10:00:00+00:00"

# Classification (controlled by TAXONOMY — see next)
projects: ["PepCodec"]
topics: ["transformer", "attention"]
methods: ["sequence-to-sequence"]
data: []
type: research              # research / review / position

# Personal evaluation
status: deep-read           # inbox / skim / deep-read / dropped
priority: A                 # A / B / C
read-date: "2024-11-02"
last-revisited: null

# Relations
related: ["2018_Devlin_BERT"]
contradicts: []
extends: []
code-clones: ["transformer-pytorch"]
```

Edit fields with `lit modify <id> --set FIELD=VALUE` (scalars) or
`--add-tag FIELD=VALUE` / `--rm-tag FIELD=VALUE` (list fields).

### TAXONOMY (controlled vocabulary)

`<vault>/TAXONOMY.md` is the **controlled vocabulary** governing which
values may appear in the classification fields. It has seven sections:

| Dict | Kind | Drives field | Examples |
|---|---|---|---|
| `projects` | user-extensible | `projects` | `PepForge`, `PepCodec`, ... |
| `topics` | user-extensible | `topics` | `transformer`, `peptide-design`, ... |
| `methods` | user-extensible | `methods` | `GAN`, `cell-free`, `diffusion`, ... |
| `data` | user-extensible | `data` | `UniProt`, `APD3`, ... |
| `type` | fixed enum | `type` | `research`, `review`, `position` |
| `status` | fixed enum | `status` | `inbox`, `skim`, `deep-read`, `dropped` |
| `priority` | fixed enum | `priority` | `A`, `B`, `C` |

**User-extensible** dicts are managed via `lit taxonomy`:

```bash
lit taxonomy list                              # show every dict
lit taxonomy list topics                       # show one dict
lit taxonomy add topics transformer attention  # register new values
lit taxonomy rename methods GAN GANs           # rename + cascade to all papers
lit taxonomy merge topics deep-learning DL --into deep-learning
lit taxonomy rm topics obsolete-topic          # refused if any paper still uses it
```

**Fixed enums** (`type` / `status` / `priority`) are not editable through
`lit taxonomy` — they require a code release, because the application's
enum logic must change in lockstep.

> **Never hand-edit TAXONOMY.md to remove or rename values.** Without
> the cascade, paper metadata still references the old value and you
> end up with dangling references that the system cannot fix
> automatically. `lit taxonomy {rename, merge, rm}` performs an atomic
> operation across TAXONOMY.md, all referencing `metadata.yaml`s, and
> `INDEX.json` — see [Atomic operations](#atomic-operations).

### Tag

"Tag" is informal shorthand for **a value in a list-typed field** in
`metadata.yaml`. The list-typed fields are:

```
authors / projects / topics / methods / data / related / contradicts / extends / code-clones
```

`lit modify --add-tag FIELD=VALUE` and `--rm-tag FIELD=VALUE` are the
operations on these. For `projects` / `topics` / `methods` / `data` the
value must already be registered in TAXONOMY.

### Project link

A **project** in litman is a name (e.g. `PepCodec`) plus a filesystem
path to the project's working directory. Projects are declared in the
vault's `lit-config.yaml`, then a paper is bound to a project with
`lit link`:

```bash
lit link 2017_Vaswani_Attention --project PepCodec \
    --relevance "Foundational architecture reference"
```

This does four things atomically:

1. Adds `PepCodec` to the paper's `projects` field.
2. Sets `relevance-PepCodec` on the paper's metadata (project-specific note).
3. Creates a symlink `<project>/literature/<paper-id>/` pointing back to
   the paper folder in the vault.
4. Regenerates `<project>/REFERENCES.md` with the project's full reading
   list.

The reverse operation is `lit unlink`. For cross-machine recovery
(symlinks broke after `git clone`), `lit link --rebuild-all` rebuilds
every project's symlinks + REFERENCES.md from each paper's metadata.

### Code clone

A **code clone** is a git repository — typically a paper's official
implementation — that lives inside the vault and is bound to one or
more papers:

```
<vault>/codes/<repo-name>/
├── repo/             # the git checkout
├── repo-meta.yaml    # framework, runs-on, status, paper bindings
└── notes.md          # your usage notes
```

```bash
lit code add https://github.com/karpathy/nanoGPT --paper 2017_Vaswani_Attention
lit code list
lit code update nanoGPT                         # git pull
lit code update nanoGPT --unshallow             # promote depth=1 → full history
lit code restore-all                            # re-clone any missing repos after sync
```

The binding is bidirectional: the paper's `code-clones` field references
the repo, and `repo-meta.yaml`'s `papers` field references the paper(s).

### Wikilink

In `notes.md` and the cross-paper notes under `<vault>/notes/`, you can
reference another paper with the wikilink syntax:

```markdown
This idea extends [[2017_Vaswani_Attention]].
For a counterpoint see [[2024_Wang_AMP-design]].
```

Cross-vault references (M8.4) use a `vault-name:` prefix:

```markdown
A similar argument appears in [[zhang-shared:2024_Tobacco_Survey]].
```

`lit health-check` validates that every `[[...]]` resolves to an
existing paper in the named (or active) vault; `lit rename` and
`lit rm` rewrite or strip wikilinks atomically.

### Atomic operations

Multi-file changes go through a staging dir and `os.replace()` so they
either fully succeed or leave the vault untouched:

```
<vault>/.litman-staging/<op-id>/
```

This is why `lit taxonomy rename` (TAXONOMY + every referencing
metadata.yaml + INDEX.json) is safe even though the vault is **not**
under git. Mid-operation crashes leave only an abandoned staging dir,
not corrupt data. Compare this to hand-editing TAXONOMY.md, which gives
no such guarantee — see the warning in [TAXONOMY](#taxonomy-controlled-vocabulary).

### INDEX.json

`<vault>/INDEX.json` is an auto-generated digest of every paper's
metadata, kept up-to-date by all writing commands. The file is the
primary query surface for AI agents that need to scan the vault — it is
faster to read one JSON file than to walk hundreds of YAML files. You
never edit it by hand; if it ever gets out of sync, run
`lit refresh-views` to rebuild it from disk.

### Vaults (multiple)

You can register additional vaults at any time:

```bash
lit vault add my-main /work/me/literature_vault
lit vault add zhang-shared /work/me/imports/zhang-vault \
    --import-from "Zhang via USB drop"
lit vault list                          # ✓ marks the active one
lit vault use zhang-shared              # switch active
lit show 2024_Wang_AMP --vault my-main  # one-shot override
```

Vaults are **forks**, not overlays: once registered, the linked vault
is yours to read and write; the two vaults then evolve independently
and are never auto-merged. (See `dev_docs/decisions/records/ADR-001-fork-vs-overlay.md`
in the wider repository for the design rationale.)

---

## Command reference

Every command supports `--library <path>` and `--vault <name>` for vault
override; both default to the discovery chain described under
[Vault](#vault).

### Vault lifecycle

| Command | Purpose |
|---|---|
| `lit init [PARENT_DIR] [--name <subdir>]` | Create a new vault. Default subdir name `literature_vault`. |
| `lit vault add <name> <path>` | Register an existing vault directory. |
| `lit vault use <name>` | Switch active vault. |
| `lit vault list` | Show registered vaults. |
| `lit vault info <name>` | Show one vault's path, paper count, size, provenance. |
| `lit vault remove <name>` | Unregister (does not delete the directory). |

### Add and query papers

| Command | Purpose |
|---|---|
| `lit add <pdf> --doi <doi>` | Import with metadata fetched from CrossRef. |
| `lit add <pdf> --from-llm-json <path>` | Import with metadata prepared by an LLM (used by the `lit-library` skill). |
| `lit list [--year/--type/--status/--priority/--topic/--method/--project/--data/--author]` | Filtered listing (AND-combined). |
| `lit show <id>` | One paper's metadata + file paths. |
| `lit open <id>` | Launch the PDF in your configured viewer. |

### Edit metadata

| Command | Purpose |
|---|---|
| `lit modify <id> --set FIELD=VALUE` | Set a scalar field. |
| `lit modify <id> --add-tag FIELD=VALUE` | Append to a list field. |
| `lit modify <id> --rm-tag FIELD=VALUE` | Remove from a list field. |
| `lit rename <old-id> <new-id>` | Change a paper id; ripple through related fields and wikilinks. |
| `lit rm <id>` | Move paper to `<vault>/.trash/` (soft delete). |
| `lit rm <id> --purge` | Permanently delete. |
| `lit trash list / restore / empty` | Manage the recoverable-delete bin. |

### Controlled vocabulary

| Command | Purpose |
|---|---|
| `lit taxonomy list [<dict>]` | Show one or every dict. |
| `lit taxonomy add <dict> <value>...` | Register new value(s) in a user dict. |
| `lit taxonomy rename <dict> <old> <new>` | Rename a value; cascade into every referencing paper. |
| `lit taxonomy merge <dict> <src>... --into <dest>` | Fold values into one; cascade. |
| `lit taxonomy rm <dict> <value>` | Remove a value (refused if any paper still uses it). |

### Project and code binding

| Command | Purpose |
|---|---|
| `lit link <paper> --project <name> [--relevance "..."]` | Bind a paper to a project. |
| `lit link --rebuild-all` | Cross-machine recovery: rebuild every project's symlinks + REFERENCES.md. |
| `lit unlink <paper> --project <name>` | Reverse a `lit link`. |
| `lit code add <url> [--paper <id>] [--depth N]` | Clone a repo into `codes/<name>/` and optionally bind to a paper. |
| `lit code list` | Show registered repos and their paper bindings. |
| `lit code link <repo> <paper>` | Bind an already-cloned repo to a paper. |
| `lit code update <repo> [--unshallow]` | `git pull`; optionally promote shallow → full history. |
| `lit code rm <repo>` | Delete the repo and clean up references. |
| `lit code restore-all` | Cross-machine recovery: re-clone any missing repo from its `repo-meta.yaml.upstream`. |

### Cloud sync (rclone-backed)

| Command | Purpose |
|---|---|
| `lit sync setup` | Hand the TTY to `rclone config` and record the remote in `lit-config.yaml`. |
| `lit sync push [--exclude-repos] [--dry-run]` | Upload the vault to the configured remote. |
| `lit sync pull [--exclude-repos] [--dry-run]` | Download the configured remote into the vault. |
| `lit sync status` | Show last-push / last-pull timestamps and counts. |

### Maintenance

| Command | Purpose |
|---|---|
| `lit health-check [--fix]` | Vault-wide consistency probe (dangling refs, schema gaps, stale staging dirs, missing PDFs, ...). |
| `lit refresh-views` | Rebuild `INDEX.json` and `views/by-*/` symlink hubs from `metadata.yaml`. |
| `lit config show` | Print the parsed, validated `lit-config.yaml` as the CLI actually sees it. |
| `lit install-skill [<name>...]` | Copy bundled Claude Code skills into `~/.claude/skills/`. Default: all bundled skills. |

---

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

The vault registry — independent of any one vault — lives at
`~/.config/litman/vaults.yaml` (overridable via `$LITMAN_REGISTRY_DIR`).

---

## Architecture

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

## Design philosophy

A few principles are non-negotiable and shape every command's behaviour:

- **LLMs never write data files directly.** AI agents emit JSON
  candidates; the CLI validates them and performs the actual write.
  Protects vault integrity from any LLM failure mode (hallucinated
  values, schema drift, YAML syntax errors).
- **Atomic multi-file ops via staging + `os.replace()`**, never via
  git rollback. The vault is not git-tracked (cloud sync owns
  versioning).
- **CLI must work standalone.** No LLM API key required for any
  command. Skills are optional sugar.
- **Schema-less metadata.** A missing field means "not applicable",
  not "required-and-empty". Adding a new field costs nothing — no
  migration. Don't add a new field until ≥5 papers genuinely need it.
- **TAXONOMY changes only via `lit taxonomy {rename, merge, rm}`** —
  never by hand-editing `TAXONOMY.md`.
- **Reference-code triangle.** Papers, projects, and code clones bind
  to each other via metadata fields and symlinks; the graph is
  emergent, not stored explicitly.

The full 12-rule list with rationale lives in the parent repository at
`dev_docs/invariants.md`. The authoritative design spec is
`../LITERATURE-SYSTEM-DESIGN.md`.

---

## Project status

Tagged milestones (see git tags):

| Tag | Milestone | Date |
|---|---|---|
| `v0.1.0-m1` | M1 — Storage skeleton (`init`, `add`, `list`, `show`) | early 2026 |
| `v0.2.0-m2` | M2 — Governance (`modify`, `taxonomy`, `rename`, `rm`, `trash`, `health-check`, DOI/id dedup) | 2026-04-28 → 2026-05-11 |
| `v0.3.0-m3` | M3 — `lit code` (clone, list, link, update, rm, restore-all) | 2026-05-11 |
| `v0.4.0-m4` | M4 — LLM JSON importer + `lit-library` skill + `lit install-skill` | 2026-05-11 |
| `v0.5.0-m5` | M5 — Project integration (`lit link`, REFERENCES.md generator) | 2026-05-11 |
| (no tag) | M6 — Cloud sync (M6.1+M6.2 done; M6.3 dogfood deferred) | 2026-05-12 |
| `v0.8.0-m8` | M8 — Multi-vault (registry, `lit vault`, `--vault` everywhere, cross-vault wikilinks) | 2026-05-12 |
| `v0.9.0-m9` | M9 — `lit open` + `lit-reading` skill (agent-assisted reading) | 2026-05-12 |

In flight / planned:

- **M7** — Legacy migration of an existing reading list (waiting on
  user-provided PDFs).
- **M10** — Layer-4 skill matrix: `lit-ingest`, `lit-audit`,
  `lit-writing`, `lit-synthesis`. Targets the next leverage layer —
  agent-driven knowledge-graph coherence on top of an already-complete
  CLI.

The full milestone index lives at `dev_docs/milestones/README.md` in the
parent repository.

---

## License

MIT. See [`LICENSE`](LICENSE).
