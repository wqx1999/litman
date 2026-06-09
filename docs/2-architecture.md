# Architecture

litman is built in four layers: the data on disk, the Python package, the `lit`
CLI, and an optional Claude Code layer on top. Each layer depends only on the
one below it, so each keeps working when the layers above it are absent. This
page describes them from the bottom up, because the upper layers only make sense
once you know what they act on.

## The vault: where everything lives (Layer 1)

The library is a single directory called the **vault**. It is plain local files
with no database, so any tool can read it. Everything litman knows about a paper
lives inside the vault: the PDF, the structured metadata, your notes and
discussion, the links between papers, and the code repositories cloned for it.

```
<vault>/
├── lit-config.yaml         # vault config: projects, default clone depth, sync target, viewer
├── TAXONOMY.md             # controlled vocabulary (4 user dicts + 3 fixed enums)
├── INDEX.json              # auto-generated thin projection; the primary read surface
│
├── papers/
│   └── <id>/
│       ├── paper.pdf
│       ├── metadata.yaml
│       ├── notes.md
│       └── discussion.md   # created when you first record a discussion
│
├── codes/
│   └── <repo-name>/
│       ├── repo/           # the git checkout
│       ├── repo-meta.yaml
│       └── notes.md
│
├── views/                  # symlink hubs faceted by metadata field
│   ├── by-project/
│   ├── by-topic/
│   ├── by-method/
│   └── by-status/
│
├── .trash/                 # recoverable-delete bin (created on first delete)
└── .litman-staging/        # atomic-op staging area; transient
```

The files split into two kinds:

1. **Authored truth.** The source of truth: each paper's `metadata.yaml`,
   `notes.md`, `discussion.md`, and `paper.pdf`, plus `TAXONOMY.md` and
   `lit-config.yaml`.
2. **Derived.** `INDEX.json` and everything under `views/` are generated from
   the authored files and can be rebuilt at any time. They are caches and
   projections, never edited by hand.

You are not meant to hand-edit the vault. Three of the truth files
(`metadata.yaml`, `TAXONOMY.md`, and `paper.pdf`) are held read-only on disk and
change only through a `lit` command, which unlocks them, writes, and re-locks, so
a stray editor save cannot corrupt them. The derived files regenerate
themselves. That leaves `notes.md`, `discussion.md`, and `lit-config.yaml` as the
files you edit directly.

Two more structural facts. Code repositories cloned for a paper live inside the
vault under `codes/` and are bound to the paper through its metadata, so the
vault stays a single sync target. The vault itself is registered in a small
registry that lives outside any vault, and exactly one registered vault is active
at a time. (The registry is described in [3-concepts.md](3-concepts.md).)

## The litman package and the lit CLI (Layers 2 and 3)

These two layers are both code inside the `litman` package, so the split between
them is one of responsibility, not location. Layer 2 is the logic that does the
work. Layer 3 is the typed-command surface on top of it: it parses what you
type, calls into Layer 2 to do the work, and formats the result for the
terminal.

**Layer 2 is the importable logic**, callable from any Python script with no CLI
involved. It is organised into three parts:

1. `core/` holds the business logic: atomic staged writes, file locking,
   TAXONOMY parsing, deduplication, paper-to-paper and paper-to-code relations,
   the drift correctors, `views/` generation, and sync.
2. `importers/` pulls an external source into a draft metadata record:
   `crossref` resolves a DOI against CrossRef, and `llm` extracts fields from
   text the agent supplies.
3. `exporters/` renders the library outward: `bibtex` emits a BibTeX file from a
   set of papers.

**Layer 3 is the `lit` CLI**, the interface a user actually types. `cli.py`
defines the root Click group, and every command is its own module under
`commands/` (`add`, `modify`, `link`, `list`, `show`, `taxonomy`, `sync`,
`health-check`, and the rest), each registered onto the group with
`cli.add_command`. Each command module is thin: it reads the arguments, calls
the `core/` functions that do the work, and prints the result, with Click
handling the parsing and Rich the formatting. The root group is a `LitGroup`
whose `invoke` runs the cheap drift hook (`commands/_drift.py`)
before dispatching each command. That hook reads only `INDEX.json`, the registry,
and directory listings, never the hundreds of per-paper files, so it stays fast
on every invocation.

The CLI is the lowest layer a user needs. It runs with no agent, no API key, and
no network for any data operation.

## Claude Code orchestration (Layer 4)

The top layer is optional. litman ships two skills that Claude Code loads on
demand:

1. `lit-library` drives the write side (`add`, `modify`, `link`, `taxonomy`, and
   so on).
2. `lit-reading` drives the read side (`search`, `show`, `related`, and so on).

Claude Code picks a skill by matching your request against the skill's
description, then orchestrates the work as a short loop: it translates the
plain-language request into one or more `lit` commands, runs them for you, and
reports what each one did. The style is active, not instructional. The agent
types the commands itself rather than telling you what to type, and after each
run it reports the result rather than leaving you to check.

Two boundaries keep this safe. The agent only ever proposes the command. The CLI
is what validates the input and writes the data, so a hallucinated or malformed
value is rejected before it reaches disk. And the agent works one paper at a
time, with no autonomous bulk operations. Remove Layer 4 entirely and every data
operation still works from the CLI.

## Data flow

A **write** (for example `lit add`, `lit modify`, `lit link`) travels down the
layers and back up:

1. The CLI (Layer 3) parses the command and its arguments.
2. The package (Layer 2) validates the input, then stages every file it will
   change under `<vault>/.litman-staging/<op-id>/`.
3. The staged files are promoted to their final paths in a single `os.replace()`,
   and `INDEX.json` is regenerated in the same operation.
4. The CLI reports what changed.

A **read** (a `lit list` or `lit show`, or an agent gathering context) goes the
other way and is ordered for cost:

1. Start from `INDEX.json`, the thin projection of every paper. One file read
   narrows the candidates.
2. Open `papers/<id>/metadata.yaml` only for the candidates that survive the
   first pass.
3. Fall through to `notes.md`, `discussion.md`, or the PDF only when their full
   text is actually needed.

This ordering is what keeps a large library fast to query. The common case never
walks hundreds of metadata files, and it is the same path an agent follows to
reach the relevant papers without reading the whole vault.

## The four layers at a glance

| Layer | What it is (real names) | Role | Needed for data ops? |
|---|---|---|---|
| 4 | Claude Code + `lit-library` / `lit-reading` skills | Translate requests into `lit` commands, run them, report back | No, pure convenience |
| 3 | `cli.py` + `commands/` (Click + Rich) | The `lit` command surface: parse, dispatch, format, drift hook | Yes |
| 2 | `core/` + `importers/` + `exporters/` | Business logic, importable as a plain library | Yes |
| 1 | `papers/`, `TAXONOMY.md`, `lit-config.yaml` (truth) + `INDEX.json`, `views/` (derived) | The source of truth and its projections | Yes, it is the data |
