# Concepts

This section defines the vocabulary used throughout the CLI. Read it
once — every command builds on these terms.

## Vault

A **vault** is the root folder of a literature library. It is a plain
directory on disk with a fixed skeleton (see [Vault layout](architecture.md#vault-layout)).
You can have multiple vaults (e.g. your own + a colleague's snapshot) and
switch between them with `lit vault use`.

Normally you set nothing: `lit init` registers the vault and makes it active,
and the CLI resolves to that active registered vault. The other ways are
explicit overrides, in priority order:

1. `--vault <name>` — a name registered with `lit vault add`
2. `--library <path>` — explicit filesystem path
3. `$LIT_LIBRARY` — environment variable
4. the active registered vault (set by `lit init` / `lit vault use`)
5. cwd-walk — `lit` walks up from your current directory looking for
   the vault marker

## Paper folder

Each paper lives in its own folder: `<vault>/papers/<id>/` containing

```
papers/<id>/
├── paper.pdf       # the original PDF
├── metadata.yaml   # structured fields (see below)
└── notes.md        # your personal notes (markdown)
```

## Paper id

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

## metadata.yaml

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

For the complete field-by-field reference, see
[metadata.yaml schema](metadata-schema.md).

## TAXONOMY (controlled vocabulary)

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

For the full file format and naming rules, see
[TAXONOMY.md schema](taxonomy-schema.md).

## Tag

"Tag" is informal shorthand for **a value in a list-typed field** in
`metadata.yaml`. The list-typed fields are:

```
authors / projects / topics / methods / data / related / contradicts / extends / code-clones
```

`lit modify --add-tag FIELD=VALUE` and `--rm-tag FIELD=VALUE` are the
operations on these. For `projects` / `topics` / `methods` / `data` the
value must already be registered in TAXONOMY.

## Project link

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
3. Creates a symlink `<project>/litman_reflib/<paper-id>/` pointing back to
   the paper folder in the vault.
4. Regenerates `<project>/REFERENCES.md` with the project's full reading
   list.

The reverse operation is `lit unlink`. For cross-machine recovery
(symlinks broke after `git clone`), `lit link --rebuild-all` rebuilds
every project's symlinks + REFERENCES.md from each paper's metadata.

## Code clone

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

## Wikilink

In a paper's `notes.md` you can reference another paper with the
wikilink syntax:

```markdown
This idea extends [[2017_Vaswani_Attention]].
For a counterpoint see [[2024_Wang_AMP-design]].
```

Cross-vault references (M8.4) use a `vault-name:` prefix:

```markdown
A similar argument appears in [[zhang-shared:2024_Tobacco_Survey]].
```

`lit health-check` validates that every `[[...]]` resolves to an
existing paper in the named (or active) vault. `lit rename` rewrites
wikilinks atomically. `lit rm` annotates referring wikilinks with a
trailing ` (deleted)` marker (the link text is preserved); `lit trash
restore` removes the marker atomically.

## Atomic operations

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

## INDEX.json

`<vault>/INDEX.json` is an auto-generated digest of every paper's
metadata, kept up-to-date by all writing commands. The file is the
primary query surface for AI agents that need to scan the vault — it is
faster to read one JSON file than to walk hundreds of YAML files. You
never edit it by hand; if it ever gets out of sync, run
`lit refresh-views` to rebuild it from disk.

## Vaults (multiple)

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
and are never auto-merged.
