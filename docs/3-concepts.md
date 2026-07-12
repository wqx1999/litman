# Concepts and Field Reference

This is the lookup page. The field tables tell you exactly what every file
in a litman library stores, and the glossary at the end defines the terms
the rest of the documentation uses. Read [1-philosophy.md](1-philosophy.md)
for the reasoning behind these choices and [2-architecture.md](2-architecture.md)
for how the files fit together.

## How to read this page

The field tables below tag each field on four attributes:

- **Controlled** — the value must be a registered TAXONOMY value (only the
  classification fields are), otherwise it is free.
- **Written by** — you or the machine. A *you*-written field is one you set
  through a `lit` command — whether you type it, click it in the Web UI, or have
  an agent issue it, it is the same command underneath. A *machine*-written field
  is maintained for you and never set by hand.
- **On disk** — read-only locked, plain-writable, or derived (regenerated, so a
  hand edit is overwritten). How litman holds each file is covered in
  [2-architecture.md](2-architecture.md).
- **Atomic** — whether the change is crash-safe, staged and then promoted in a
  single rename.

Where an attribute is uniform across a file or layer it is stated once at the
top of that section. Where it varies, the Notes column says so.

---

## 1. Fields inside the vault

Everything litman knows about a paper or a library lives in the vault as
plain files (the full layout is in [2-architecture.md](2-architecture.md)).
This section is the field-by-field reference for each one, heaviest first.

### 1.1 `metadata.yaml` — per paper

`<vault>/papers/<id>/metadata.yaml` holds one paper's structured data. `lit add`
writes the **complete field skeleton**, filling every identity field CrossRef
or the LLM resolved and leaving empty lists and `null`s for the rest, so a
freshly added paper already contains every standard field. The schema is still
**schema-less** at validation time: a field left empty, or removed entirely, is
valid and read as "this dimension does not apply to this paper". What
`lit health-check` insists on is narrow: `id`, `created-at`, and `updated-at`
must be present and non-empty, and `status` must carry one of its enum values
(`inbox` is the value for "not evaluated yet", so an empty `status` is an
error). Everything else may be empty or absent.

The whole file is **read-only locked** and every write is **atomic**. Edit it
through `lit add`, `lit modify`, `lit link`, and the reading-lifecycle
commands, never by hand.

#### Identity layer

Auto-filled by `lit add` from CrossRef (`lit add --doi`) or from LLM-extracted
JSON (`lit add --from-llm-json`). You rarely touch these after add.

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | required | Folder name. Format `<year>_<Family>_<Keyword>`. Change only via `lit rename`, which rewrites every back-reference. |
| `title` | string | required at add time | The paper's title, not normalized. |
| `authors` | list[string] | `[]` | Each entry `"Family, Given"`. The first entry's family name drives id derivation. |
| `year` | integer or null | null | Publication year. |
| `journal` | string | `""` | Journal, conference, or preprint server. |
| `doi` | string | `""` | Canonical DOI, no URL prefix. Used for `lit add` deduplication, matched case-insensitively. |
| `arxiv-id` | string or null | null | arXiv identifier, informational. |
| `github` | string or null | null | URL of the paper's official repo, informational. Actual binding goes through `code-clones`. |
| `volume` | string | `""` | Journal volume, for BibTeX export. |
| `issue` | string | `""` | Journal issue or number. |
| `pages` | string | `""` | Page range in raw CrossRef form (e.g. `"45-67"`). |
| `publisher` | string | `""` | Publisher or conference organizer. |
| `venue-type` | string | `""` | CrossRef-style publication form (`journal-article`, `proceedings-article`, `posted-content`, `book-chapter`, ...). `lit export` picks the BibTeX entry type from it. |
| `booktitle` | string | `""` | Conference or book title, for proceedings and chapter entries. |

**`type` vs `venue-type`.** `type` (classification layer below) is your
editorial label for what the paper is to you. `venue-type` is CrossRef's label
for what kind of publication object it is. A peer-reviewed `journal-article`
can be a `review`; a `proceedings-article` can be a `position` piece. They are
independent, and only `venue-type` drives BibTeX export.

#### Timestamp layer

Four timestamps with a strict boundary. The first two are technical (the tool
maintains them), the last two are semantic (you mark them through a command).
**Never merge the two pairs.**

| Field | Written by | When | Meaning |
|---|---|---|---|
| `created-at` | machine | once, at `lit add` | When the paper entered the vault. Never changes. |
| `updated-at` | machine | every metadata write | Last structured-field change. |
| `read-date` | you (`lit read`) | the first time you finish a deep read | The "finished reading" marker. |
| `last-revisited` | you (`lit revisit`) | when you re-open a paper that already has a `read-date` | The "came back to it" marker. |

You rarely set these by hand. The Web UI stamps them from its read /
revisit buttons, and the lit-reading skill stamps them when you
say you finished or re-opened a paper — both routed through `lit read` /
`lit revisit`. The hinge is `read-date`. A paper without one is still on its
first read, so finishing it stamps `read-date`, not `last-revisited`, and
continuing an unfinished read is never a revisit.

All four are ISO 8601 with a timezone offset (`2026-04-27T17:05:00+02:00`).
File `mtime` and git history cannot stand in for them (a `cp -r` resets
`mtime`, and the vault is not under git), so they are stored in the file.

#### Classification layer

**Controlled: yes (TAXONOMY).** A value must be registered in the matching
TAXONOMY dictionary before it can be tagged onto a paper. Set them through
`lit modify --add-tag`, `lit link` (for `projects`), and `lit project` /
`lit taxonomy` to manage the vocabulary.

| Field | Type | Controlled by | Edit via |
|---|---|---|---|
| `projects` | list[string] | `projects` dict (user-extensible) | `lit link --project` (preferred), or `lit modify --add-tag projects=…` |
| `topics` | list[string] | `topics` dict (user-extensible) | `lit modify --add-tag topics=…` |
| `methods` | list[string] | `methods` dict (user-extensible) | `lit modify --add-tag methods=…` |
| `data` | list[string] | `data` dict (user-extensible) | `lit modify --add-tag data=…` |
| `type` | string or null | `type` dict (fixed enum) | `lit modify --set type=…` |

`type` values: `research`, `review`, `position`, `benchmark`, `dataset`,
`tutorial`, `thesis`, `book-chapter`. Default `null` ("not yet classified").
`lit health-check` tolerates `null` but rejects any non-null value outside the
enum.

#### Personal-evaluation layer

**Controlled: fixed enum.** Set through `lit modify --set` or the reading
lifecycle commands.

| Field | Type | Default | Values / Notes |
|---|---|---|---|
| `status` | string enum | `inbox` | `inbox`, `skim`, `deep-read`, `dropped`. Changed by `lit skim` / `lit promote` / `lit drop`. A status, not a folder, so no file moves. |
| `priority` | string enum or null | null | `A`, `B`, `C`. `null` means "not yet evaluated". |

#### Relations layer

Cross-paper and cross-code links, all list[string] of ids, empty by default.
The three paper-to-paper relations are **symmetric and stored on both ends**:
you set the forward field, and the CLI maintains the paired reverse field on
the opposite paper in the same atomic write.

| Field | Direction | Written by | Meaning |
|---|---|---|---|
| `related` | symmetric | you | Generic "see also". Self-paired: `A.related:[B]` implies `B.related:[A]`. |
| `extends` | forward | you | This paper builds on the linked paper(s). |
| `extended-by` | reverse | machine | Set when another paper lists this one under `extends`. Never set directly. |
| `contradicts` | forward | you | This paper's results disagree with the linked paper(s). |
| `contradicted-by` | reverse | machine | Set when another paper lists this one under `contradicts`. Never set directly. |
| `code-clones` | — | machine (via `lit code`) | Names of repos under `<vault>/codes/<name>/` that implement this paper. A 1:N binding mirrored by the repo's `papers` field. |

You drive only the forward fields (`related`, `extends`, `contradicts`) with
`lit modify --add-tag` / `--rm-tag`. The reverse fields are maintained by the
double-write only, and `lit modify` rejects them as targets. To repair a
broken pair, act on the forward field.

#### Project-relevance layer

A schema-less, per-project annotation. One `relevance-<project>` field per
project the paper is linked to, written by `lit link --project <P> --relevance "…"`
(or later `lit modify --set relevance-<P>=…`).

```yaml
relevance-pepcodec: "Direct baseline — section 3.2 reuses the encoder block."
relevance-pepforge: "Cited only as motivation in the introduction."
```

The project name appears verbatim in the field name, so each project carries
its own perspective on the same paper.

#### Full example

A fully evaluated paper after a deep read. On first add, `type`, `priority`,
`read-date`, and `last-revisited` are all `null`.

```yaml
# Identity
id: 2017_Vaswani_Attention
title: "Attention is all you need"
authors:
  - "Vaswani, Ashish"
  - "Shazeer, Noam"
year: 2017
journal: "NeurIPS"
doi: "10.48550/arXiv.1706.03762"
arxiv-id: "1706.03762"
github: null
volume: "30"
issue: ""
pages: "5998-6008"
publisher: ""
venue-type: journal-article
booktitle: ""

# Timestamps
created-at: "2026-05-13T10:00:00+00:00"
updated-at: "2026-05-13T11:24:11+00:00"
read-date: "2024-11-02"
last-revisited: "2026-04-18"

# Classification
projects:
  - PepCodec
topics:
  - transformer
  - attention
methods:
  - sequence-to-sequence
data: []
type: research

# Personal evaluation
status: deep-read
priority: A

# Relations
related:
  - 2018_Devlin_BERT
contradicts: []
contradicted-by: []      # machine-maintained
extends: []
extended-by: []          # machine-maintained
code-clones:
  - transformer-pytorch

# Project relevance (dynamic)
relevance-PepCodec: "Foundational architecture; encoder block reused in section 3.2."
```

#### Adding a new field

The schema is open. To capture a new dimension:

1. Pick a name (kebab-case, lowercase) that does not collide with the layers
   above.
2. Set it on one paper with `lit modify <id> --set <field>=<value>`.
3. Treat it as a personal extension until at least 5 papers genuinely need it
   before considering first-class CLI support.

There is no migration step. The schema is the union of fields ever written,
and `lit health-check` does not warn on unknown fields.

### 1.2 `repo-meta.yaml` — per code clone

`<vault>/codes/<repo-name>/repo-meta.yaml` annotates one cloned repository and
records which papers it is bound to. The file is **not locked**. Its
machine-maintained fields are written atomically by `lit code` commands. Its
annotation fields you fill in by editing the file directly; nothing is derived
from them, so there is no rebuild to run afterwards.

| Field | Type | Written by | Notes |
|---|---|---|---|
| `name` | string | machine | The `codes/<name>/` directory name. |
| `upstream` | string or null | machine | Clone URL, `local:<path>` for an imported directory, or `null` when nothing is fetchable. `lit code restore-all` clones from it. |
| `created-at` | string | machine | ISO 8601, set once at `lit code add`. |
| `updated-at` | string | machine | Refreshed on bind, unbind, and `lit code update`. |
| `papers` | list[string] | machine | Back-reference: the paper ids that list this repo under `code-clones`. The N side of the 1:N binding. |
| `framework` | string or null | you (hand-edit) | Free annotation (e.g. `pytorch`). |
| `runs-on` | string or null | you (hand-edit) | Free annotation (e.g. `cuda 12`). |
| `status` | string or null | you (hand-edit) | Free annotation (e.g. `reproduced`). |

The paper-to-repo binding is bidirectional and bound atomically: `lit code add`,
`lit code link`, and `lit code unlink` rewrite the paper's `code-clones` and
the repo's `papers` in one staged write, so the two sides cannot drift apart.

### 1.3 `TAXONOMY.md` — the controlled vocabulary

`<vault>/TAXONOMY.md` constrains which values may appear in the classification
fields. It is **read-only locked** and changed only through `lit project`
(for `projects`) and `lit taxonomy` (for the rest), each an **atomic** cascade
across the vocabulary and every referencing paper. Hand-editing it to remove
or rename a value leaves dangling references in metadata that the system
cannot repair.

#### Two steps: register a value, then tag a paper

TAXONOMY is the menu of allowed values. It never touches a paper on its own.
Putting a value on a paper is a separate, second step, and the CLI gates that
step on the value already being registered here.

| Step | Action | Command | Writes |
|---|---|---|---|
| 1 | Register a value into a dictionary | `lit taxonomy add topics information` (for `projects`, `lit project add`) | `TAXONOMY.md` only |
| 2 | Tag the value onto a paper | `lit modify <id> --add-tag topics=information` (for `projects`, `lit link`) | the paper's `metadata.yaml` + `INDEX.json` |

Step 2 is register-first checked for the four user dictionaries. If the value
is not yet in `TAXONOMY.md`, `lit modify` refuses and names the step-1 command
to run:

```
'topics' value 'information' is not registered in TAXONOMY.md.
Run `lit taxonomy add topics information` first.
```

So "managing the vocabulary" (this section) and "classifying a paper"
(`metadata.yaml`, section 1.1) are deliberately distinct. Registration adds a
value to the menu. Tagging applies a registered value to one paper.

#### File format

Plain markdown. The parser recognizes only:

1. `## <name>` — a section header that must match a known dictionary.
2. `- <value>` — a list item under a recognized header, registered in that
   dictionary.
3. `(empty)` — an explicit placeholder for a dictionary with no values yet.

Everything else (preamble, blank lines, header annotations) is preserved
verbatim. The rewriter replaces one section's body at a time.

#### Dictionary catalogue

Seven dictionaries in two classes.

**User-extensible (4).** Each drives a like-named metadata list field.

| Dictionary | Drives field | Managed by | Typical values |
|---|---|---|---|
| `projects` | `projects` | `lit project {add,rename,rm,list}` | `PepForge`, `PepCodec` |
| `topics` | `topics` | `lit taxonomy {add,rename,merge,rm}` | `transformer`, `peptide-design` |
| `methods` | `methods` | `lit taxonomy {add,rename,merge,rm}` | `GAN`, `diffusion`, `cell-free` |
| `data` | `data` | `lit taxonomy {add,rename,merge,rm}` | `UniProt`, `APD3`, `PDB` |

`projects` is managed by its own `lit project` group, not `lit taxonomy`,
because a project is a TAXONOMY entry plus a path in `lit-config.yaml`, and
`lit project add` dual-writes both in one atomic operation. `lit taxonomy …
projects` is deprecated to avoid a half-update. The other three dictionaries
have no dedicated command. Manage them with `lit taxonomy <verb> <dict>`
(e.g. `lit taxonomy add topics transformer`), not a per-field command like
`lit topics add`, which does not exist. The dictionary and field names are
always plural (`projects`, `topics`, `methods`, `data`); the command group is
singular (`lit project`), matching litman's other groups (`lit code`,
`lit vault`).

**Fixed enums (3).** Read-only here. The values are baked into the application
logic, so changing them requires a code release, and `lit taxonomy` refuses to
touch these dictionaries.

| Dictionary | Drives field | Values |
|---|---|---|
| `type` | `type` | `research`, `review`, `position`, `benchmark`, `dataset`, `tutorial`, `thesis`, `book-chapter` |
| `status` | `status` | `deep-read`, `skim`, `inbox`, `dropped` |
| `priority` | `priority` | `A`, `B`, `C` |

#### Naming rules for user-dictionary values

The parser accepts any non-empty string, but these conventions keep the
vocabulary scannable and metadata grep-friendly:

1. **kebab-case, lowercase** (`peptide-design`). Project names are the one
   exception and may use mixed case (`PepCodec`) to match a real directory.
2. **No leading punctuation, whitespace, or slashes.**
3. **ASCII only.**
4. **Singular** (`transformer`, not `transformers`).
5. **No opaque abbreviations** (`cell-free-expression`, not `cfe`).

#### Refusal and cascade rules

| Command | Refuses when |
|---|---|
| `lit taxonomy add <dict> <value>` | `dict` is a fixed enum, or `value` already registered. |
| `lit taxonomy rename <dict> <old> <new>` | `dict` is a fixed enum, `old` not registered, or `new` already registered. |
| `lit taxonomy merge <dict> <src>… --into <dest>` | `dict` is a fixed enum, any `src` not registered, or `dest` not registered. |
| `lit taxonomy rm <dict> <value>` | `dict` is a fixed enum. When a paper still uses `value`, it does not refuse — it shows the affected-paper count and prompts (default `N`). |

`rm` and `merge` are confirm-and-cascade: on confirmation the value is removed
from `TAXONOMY.md`, from every referencing `metadata.yaml`, and from
`INDEX.json` in one atomic transaction.

#### Initial seed

`lit init` writes `TAXONOMY.md` with the four user dictionaries marked
`(empty)` and the three fixed enums populated:

```markdown
# Literature Taxonomy

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

### 1.4 `lit-config.yaml` — library settings

Each vault has one `lit-config.yaml` at its root, carrying library-level
preferences. It is **writable** (you may hand-edit it, or let `lit sync setup`,
`lit project`, and the Web UI's project controls write the parts they own —
`lit config show` only prints it, there is no `lit config set`). It is validated
against a strict schema that **forbids unknown keys**, so a typo fails at load
time rather than silently falling back to a default. Print the live, parsed
view with `lit config show`.

| Field | Type | Default | Consumed by |
|---|---|---|---|
| `library_name` | string (required) | — | Human-readable label, conventionally the vault subdirectory name. |
| `default_pdf_viewer` | string or null | `null` | `lit open`. `null` uses the platform default (`xdg-open`, `open`, `os.startfile`, `wslview`). |
| `view_definitions` | list[string] | `["by-project", "by-topic", "by-method", "by-status"]` | `lit refresh-views`. Which `views/by-*/` hubs to rebuild. |
| `unique_keys` | list[string] | `["doi", "arxiv-id"]` | `lit add`. Fields checked for duplicates. The DOI precheck is enforced; `arxiv-id` is informational. |
| `default_clone_depth` | integer ≥ 0 | `1` | `lit code add`, `lit code restore-all`. `0` means full history. |
| `codes_ignore_patterns` | list[string] | `["repo/"]` | `lit sync push`. Glob patterns under `codes/` that backup tools exclude (keeps bulky checkouts out of the cloud; rebuild with `lit code restore-all`). |
| `projects` | dict[string, string] | `{}` | `lit link`, `lit unlink`, `lit refresh-views`. Project name to directory path. Must be populated (via `lit project add`) before `lit link`. |
| `sync` | object or null | `null` | `lit sync`. `null` means sync is not configured. Populated by `lit sync setup`. |

The nested `sync` object:

| Field | Type | Default | Notes |
|---|---|---|---|
| `remote` | string (required) | — | rclone remote name, must match one in `rclone listremotes`. |
| `path` | string | `""` | Path inside the remote. Empty syncs to the remote root. |
| `exclude_repos` | boolean | `false` | When `true`, `codes/*/repo/` checkouts are excluded. |

Three things are deliberately absent from the schema: (1) LLM API keys,
endpoints, or models (the agent manages its own auth and litman never makes an
LLM API call), (2) per-paper overrides (those belong in `metadata.yaml`), and
(3) a default project (linking is always an explicit `lit link --project`).

### 1.5 `INDEX.json` — the derived read surface

`<vault>/INDEX.json` is an auto-generated digest of every paper, kept current
by every writing command and rebuilt from disk with `lit refresh-views`. It is
**derived, do not edit** (a hand edit is overwritten), and it is the primary
query surface for agents, which read one JSON file instead of walking hundreds
of `metadata.yaml` files.

Top-level structure:

| Key | Type | Notes |
|---|---|---|
| `_comment` | string | Auto-generated banner (JSON has no comment syntax). |
| `generated_at` | string | ISO 8601 timestamp of the last rebuild. |
| `n_papers` | integer | Paper count. |
| `papers` | list[object] | One thin projection per paper, sorted by id. |
| `by_doi` | dict[string, string] | Reverse map from normalized (lowercase) DOI to paper id. |

Each entry in `papers` is a **14-field projection** of `metadata.yaml`, not the
whole record: `id`, `title`, `authors`, `year`, `type`, `priority`, `status`,
`topics`, `projects`, `methods`, `data`, `doi`, `read-date`, `updated-at`. A
consumer narrows the candidate set from this file, then opens a candidate's
`metadata.yaml` for any field not in the projection. `lit list --format json`
emits the same per-paper projection, so its schema never drifts from the index.

---

## 2. Fields outside the vault: the registry

The registry is litman's state that lives outside any vault: a user-level file
recording which vaults exist on this machine and which one is active. It is
**not** part of a vault, so it is never synced with one. A second user-level
file, `preferences.yaml`, sits next to it and holds your default agent — also
machine-level, for the same reason (which agent you run is a property of the
machine, not of a library).

Location, highest precedence first:

1. `$LITMAN_REGISTRY_DIR/vaults.yaml` when the environment variable is set
   (point it at a cloud-synced directory for cross-machine backup).
2. Otherwise the platform config directory: `~/.config/litman/vaults.yaml` on
   Linux, `~/Library/Application Support/litman/vaults.yaml` on macOS,
   `%APPDATA%\litman\vaults.yaml` on Windows.

The file holds a `vaults:` list. Each entry has these fields:

| Field | Type | Written by | Notes |
|---|---|---|---|
| `name` | string | `lit vault add` / `lit init` | Unique handle. No `:` (reserved for cross-vault wikilinks), no leading hyphen, checked case-insensitively unique. |
| `path` | string | `lit vault add` / `lit init` | Absolute path to the vault root. |
| `imported_from` | string or null | `lit vault add --import-from` | Free-form provenance for a forked vault. `null` for a locally created one. |
| `imported_at` | string or null | `lit vault add` | ISO 8601 date the vault was registered. |
| `is_active` | boolean | `lit vault use` / `lit init` | The default vault when no `--vault` / `--library` / `$LIT_LIBRARY` is given. |
| `last_health_check_at` | string or null | `lit health-check` | ISO 8601 timestamp of the last successful run, `null` if never. Drives the staleness nudge. |

Two invariants are enforced when the file loads: all names are unique, and at
most one entry is active. The file is managed by `lit vault {add,use,remove}`
and `lit init` (which registers the new vault and makes it active), and by the
Web UI's vault controls, which call the same registry code. It rejects unknown
keys, so it is not meant to be hand-edited.

**Project-side references.** Linking a paper to a project writes two artifacts
under the project directory, outside the vault: a `litman_reflib/<id>/` symlink
back to the paper folder, and a generated `litman_reflib/REFERENCES.md` reading
list. Both
are derived and rebuilt by `lit link --rebuild-all`. They are not fields, but
they are the project end of the paper-to-project binding.

---

## 3. Glossary

The structural terms used throughout the documentation. The cross-cutting
attributes (controlled, written-by, on-disk state, atomic) are defined in
*How to read this page* above and not repeated here.

**Vault.** The root folder of a literature library, a plain directory with a
fixed skeleton. You can register several and switch between them. The CLI
resolves which vault to use in priority order: `--vault <name>`, then
`--library <path>`, then `$LIT_LIBRARY`, then the active registered vault, then
walking up from the current directory for a vault marker.

**Paper folder.** Each paper lives in `<vault>/papers/<id>/`, containing
`paper.pdf`, `metadata.yaml`, `notes.md`, and `discussion.md`. `lit add` creates
all four; the two markdown files start empty apart from a comment stating how
each is written.

**Paper id.** The on-disk handle for a paper: both the folder name and the
value other places use to refer to it. Default format `<year>_<Family>_<Keyword>`
(e.g. `2017_Vaswani_Attention`). Change it only with `lit rename`, never `mv`,
so every back-reference (other papers' relations, notes wikilinks, project
symlinks) is rewritten.

**Tag.** Informal shorthand for a value in a list-typed metadata field
(`topics`, `methods`, `projects`, `data`, the relation fields, `code-clones`).
For the four controlled fields, the value must already be registered in
TAXONOMY.

**Project link.** A project is a name plus a filesystem path, declared in
`lit-config.yaml`. `lit link` binds a paper to a project, which (1) adds the
project to the paper's `projects` field, (2) records a per-project
`relevance-<project>` note, (3) creates a symlink under the project's
`litman_reflib/`, and (4) regenerates the project's `REFERENCES.md`, all
atomically.

**Code clone.** A git repository (typically a paper's official implementation)
cloned into `<vault>/codes/<name>/` and bound to one or more papers. The repo
stays a single sync target inside the vault, and the binding is mirrored on
both ends (the paper's `code-clones` and the repo's `papers`).

**Wikilink.** A `[[<paper-id>]]` reference in a `notes.md`, with a
`[[<vault-name>:<paper-id>]]` form for cross-vault references. `lit health-check`
validates that each one resolves, and `lit rename` rewrites them atomically.

**View.** A `views/by-*/` hub of symlinks faceted by a metadata field
(`by-project`, `by-topic`, `by-method`, `by-status`), regenerated by
`lit refresh-views`. A derived browsing aid, rebuilt from the truth.

**Fork.** A registered vault other than your own (a colleague's snapshot, an
archived project). Vaults are forks, not overlays: once registered, the two
evolve independently and are never auto-merged.
