# `TAXONOMY.md` Schema

`<vault>/TAXONOMY.md` is the controlled vocabulary that constrains
which values may appear in classification fields of `metadata.yaml`. It
is one of three vault-level files (alongside `lit-config.yaml` and
`INDEX.json`) and is the only one users edit by hand — but **only via**
`lit taxonomy {add, rename, merge, rm}`. Hand-deleting or hand-renaming
values produces dangling references; see
[philosophy: TAXONOMY changes only via lit taxonomy](philosophy.md#taxonomy-changes-only-via-lit-taxonomy).

## File format

Plain markdown. The parser recognises only:

- `## <name>` — section header. Each `<name>` must match a known dict
  (see below); unknown headers are ignored.
- `- <value>` — list-item lines under a recognised header. Each
  non-empty value is registered in that dict.
- `(empty)` — explicit placeholder for a section with no values yet;
  treated identically to "no list items".

Everything else (preamble paragraph, blank lines, header annotations
like `## type (fixed enum, not extensible)`, free-form text under any
header) is preserved verbatim by `lit taxonomy` writes. The rewriter is
surgical — it only replaces the body of one section at a time.

## Dictionary catalogue

litman recognises seven dicts, split into two classes.

### User-extensible (4)

Modifiable via `lit taxonomy {add, rename, merge, rm}`. Each drives a
like-named list field on `metadata.yaml`.

| Dict | Drives metadata field | Typical values |
|---|---|---|
| `projects` | `projects` | `PepForge`, `PepCodec`, `MyDissertation` |
| `topics` | `topics` | `transformer`, `peptide-design`, `cell-free-expression` |
| `methods` | `methods` | `GAN`, `diffusion`, `cell-free`, `chromatography` |
| `data` | `data` | `UniProt`, `APD3`, `PDB`, `MIBiG` |

`lit taxonomy add <dict> <value>...` adds one or more values to a user
dict. Values are stored sorted on disk; the order is alphabetical so
`git diff` (in the dev repository) and visual scanning stay stable.

### Fixed enums (3)

**Read-only here.** Values are baked into `litman.core.seeds.TAXONOMY_SEED`
and the application logic depends on the exact set; modifying them
requires a code release. `lit taxonomy {rename, merge, rm}` refuse to
touch these dicts.

| Dict | Drives metadata field | Values |
|---|---|---|
| `type` | `type` | `research`, `review`, `position`, `benchmark`, `dataset`, `tutorial`, `thesis`, `book-chapter` |
| `status` | `status` | `deep-read`, `skim`, `inbox`, `dropped` |
| `priority` | `priority` | `A`, `B`, `C` |

## Naming rules for user-dict values

The TAXONOMY parser does not reject any non-empty string, but the
following conventions keep TAXONOMY scannable and metadata grep-friendly.
Future `lit health-check` rules may flag deviations.

- **kebab-case, lowercase.** `peptide-design`, not `Peptide_Design` or
  `peptideDesign`. Project names are the one exception — they appear in
  paths (`<project>/litman_reflib/`) and may use mixed case (`PepCodec`)
  to match a real directory.
- **No leading punctuation, no whitespace, no slashes.** Values are
  embedded in YAML lists; weird characters force quoting and break
  hand-grep.
- **ASCII only.** litman papers cross machines and shells; non-ASCII
  values risk transliteration drift.
- **Singular by convention.** `transformer`, not `transformers`;
  `attention`, not `attentions`. The metadata field's name is plural
  (e.g. `topics`); each item inside it is a single concept.
- **No abbreviations whose meaning isn't obvious.** Prefer `transformer`
  over `tx`; `cell-free-expression` over `cfe`. The vocabulary lives
  long; spelling things out costs little.

To rename a value that already violates these rules, use:

```bash
lit taxonomy rename topics Peptide_Design peptide-design
```

The cascade rewrites every paper's `topics` list in one atomic op.

## Atomic operations and refusal rules

| Command | Refuses when |
|---|---|
| `lit taxonomy add <dict> <value>` | `dict` is a fixed enum; `value` already registered. |
| `lit taxonomy rename <dict> <old> <new>` | `dict` is a fixed enum; `old` not registered; `new` already registered. |
| `lit taxonomy merge <dict> <src>... --into <dest>` | `dict` is a fixed enum; any `src` not registered; `dest` not registered. |
| `lit taxonomy rm <dict> <value>` | `dict` is a fixed enum. (When any paper still references `value`, `rm` does **not** refuse — it shows the affected-paper count and prompts for confirmation; default `N`. Pass `-y` for non-interactive runs.) |

`lit taxonomy rm` and `lit taxonomy merge` are confirm-and-cascade: on
confirmation, the value is removed from `TAXONOMY.md` and from every
referencing `metadata.yaml` (and from `INDEX.json`) in one atomic
transaction. The prompt + default-`N` is the guard — it ensures the vault
never silently reaches a state where `metadata.yaml` references a value
that TAXONOMY no longer lists.

## Initial seed

`lit init` writes `TAXONOMY.md` with all four user dicts marked
`(empty)` and the three fixed enums populated:

```markdown
# Literature Taxonomy

Controlled vocabulary for fields in `papers/<id>/metadata.yaml`. Edit user
dictionaries (projects, topics, methods, data) **only** via
`lit taxonomy {add,rename,merge,rm}` — hand-editing leaves dangling references
in existing metadata files.

## projects

(empty)

## topics

(empty)

## methods

(empty)

## data

(empty)

## type (fixed enum, not extensible)

- research
- review
- position
- benchmark
- dataset
- tutorial
- thesis
- book-chapter

## status (fixed enum, not extensible)

- deep-read
- skim
- inbox
- dropped

## priority (fixed enum, not extensible)

- A
- B
- C
```

Add your first values with `lit taxonomy add` once you have papers to
classify. `lit taxonomy list` prints the current state at any time.
