# `metadata.yaml` Schema

Each paper's structured data lives at `<vault>/papers/<id>/metadata.yaml`.
The schema is **deliberately schema-less**: a missing field means "this
dimension does not apply to this paper", not "required-and-empty". Only
the identity layer (`id`, `title`) is structurally required; everything
else is opt-in per paper.

This page is the field-by-field reference. For the broader rationale,
see [philosophy: schema-less metadata](philosophy.md#schema-less-metadata).

## Layer overview

| Layer | Maintained by | Editable? |
|---|---|---|
| Identity | `lit add` (CrossRef / LLM) | Yes, but most fields auto-fill |
| Audit | The CLI itself (timestamps) | **No** — touched by every write |
| Classification | You, via `lit modify` and `lit link` | Yes (TAXONOMY-controlled) |
| Personal evaluation | You, via `lit modify` | Yes |
| Relations | You, via `lit modify` (or `lit code link`) | Yes |
| Project relevance | `lit link --relevance` (per project) | Yes |

## Identity layer

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | string | — | Folder name. Format `<year>_<Family>_<Keyword>`. Change only via `lit rename`. |
| `title` | string | `""` | The paper's title. |
| `authors` | list[string] | `[]` | Each entry `"Family, Given"`. First entry's family name drives id derivation. |
| `year` | integer or null | null | Publication year. |
| `journal` | string | `""` | Venue / journal / preprint server. |
| `doi` | string | `""` | Canonical DOI (no URL prefix). Used for `lit add` dedup; see `unique_keys` in [config-schema.md](config-schema.md). |
| `arxiv-id` | string or null | null | arXiv identifier; informational. |
| `github` | string or null | null | URL of the paper's official code repo; informational. Use `lit code add` + `code-clones` for actual binding. |
| `volume` | string | `""` | Journal volume. Filled from CrossRef `message.volume` for `@article`. Empty for preprints / books. |
| `issue` | string | `""` | Journal issue / number. Filled from CrossRef `message.issue`. |
| `pages` | string | `""` | Page range, raw CrossRef form (e.g. `"45-67"`). `lit export` renders to `"45--67"` for bibtex. |
| `publisher` | string | `""` | Publisher / conference organizer. Used by `@book`, `@inproceedings`, `@phdthesis`. |
| `venue-type` | string | `""` | CrossRef-style publication form: `journal-article`, `proceedings-article`, `posted-content`, `book`, `book-chapter`, `dissertation`, `report`. **Distinct from `type` below** (editorial classification). Drives the bibtex entry chosen by `lit export`; missing / unknown maps to `@misc`. |
| `booktitle` | string | `""` | Conference / book title for `@inproceedings` / `@incollection`. Filled instead of `journal` when CrossRef `type` is a proceedings or chapter form. Empty for journal articles. |

## Audit layer (machine-maintained — do not edit)

| Field | Type | Notes |
|---|---|---|
| `created-at` | ISO 8601 string with TZ offset | Set once by `lit add`; never updated. |
| `updated-at` | ISO 8601 string with TZ offset | Refreshed by every `lit modify` / `lit link` / `lit taxonomy` write. |

These are **technical** timestamps. Do not conflate with `read-date` /
`last-revisited`, which are user-set semantic markers.

## Classification layer (TAXONOMY-controlled)

Each list field is constrained to values registered in the corresponding
TAXONOMY dict (`<vault>/TAXONOMY.md`). Add values with `lit taxonomy add`
before tagging.

| Field | Type | TAXONOMY dict | Edit via |
|---|---|---|---|
| `projects` | list[string] | `projects` (user-extensible) | `lit link --project` (preferred) or `lit modify --add-tag projects=…` |
| `topics` | list[string] | `topics` (user-extensible) | `lit modify --add-tag topics=…` |
| `methods` | list[string] | `methods` (user-extensible) | `lit modify --add-tag methods=…` |
| `data` | list[string] | `data` (user-extensible) | `lit modify --add-tag data=…` |
| `type` | string | `type` (fixed enum) | `lit modify --set type=…` |

`type` enum values (defined in [taxonomy-schema.md](taxonomy-schema.md)):
`research`, `review`, `position`, `benchmark`, `dataset`, `tutorial`,
`thesis`, `book-chapter`. Default: `null` (unset until you classify the
paper — `lit health-check` tolerates null, but a non-null value must be
in the enum). `lit add` writes `null`; the `lit-reading` skill's verdict
ritual offers candidates with rationale on first read.

**`type` vs `venue-type` — keep them straight**: `type` is your
editorial label for what kind of paper it is to you (a research paper
you'll cite for results, a review that surveys the field, a position
piece on direction). `venue-type` (identity layer above) is CrossRef's
label for what kind of publication object it is (journal article,
proceedings article, preprint, book chapter). The two are intentionally
independent — a peer-reviewed `journal-article` can be a `review`, and
a `proceedings-article` can be a `position` piece. `lit export` uses
`venue-type` (not `type`) to pick a bibtex entry type.

## Personal evaluation layer

| Field | Type | Default | Notes |
|---|---|---|---|
| `status` | string enum | `inbox` | Reading workflow state. Values: `inbox`, `skim`, `deep-read`, `dropped`. |
| `priority` | string enum or null | `null` | Reading priority. Values: `A`, `B`, `C`. `null` = "not yet evaluated"; `lit add` writes `null` and the `lit-reading` verdict ritual offers a value on first read. `lit health-check` tolerates `null` but rejects non-enum values. |
| `read-date` | date string or null | null | When you finished a deep read. **Semantic** (user-set). Format `YYYY-MM-DD`. |
| `last-revisited` | date string or null | null | When you most recently re-read or referenced this paper. **Semantic** (user-set). Format `YYYY-MM-DD`. |

`read-date` and `last-revisited` are deliberately separate from
`created-at` / `updated-at`. The audit timestamps record what the tool
did to the file; the semantic dates record what *you* did with the
paper. Never merge them — `lit health-check` warns on suspicious
combinations (e.g. `read-date` set but `status` still `inbox`).

## Relations layer

All fields are list[string] of other paper ids. Empty by default. The
three paper-to-paper relations are **symmetric and stored on both ends**
(ADR-012): you set the *forward* field on one paper, and the CLI's atomic
double-write maintains the paired *reverse* field on the opposite paper
in the same transaction. Back-references are therefore real stored fields,
not computed on demand. `lit health-check` validates that every pair is
intact (`extends`↔`extended-by`, `contradicts`↔`contradicted-by`,
`related` self-paired).

| Field | Direction | Semantic |
|---|---|---|
| `related` | symmetric | Generic "see also". Use when the link is informational, not directional. Self-paired: `A.related:[B]` implies `B.related:[A]`. |
| `extends` | forward | This paper builds on or improves the linked paper(s). |
| `extended-by` | reverse | Set automatically when another paper lists this one under `extends`. **CLI-maintained — never set it directly.** |
| `contradicts` | forward | This paper's results disagree with the linked paper(s). |
| `contradicted-by` | reverse | Set automatically when another paper lists this one under `contradicts`. **CLI-maintained — never set it directly.** |
| `code-clones` | — | Names of repos under `<vault>/codes/<name>/` that implement this paper. Maintained by `lit code link` / `lit code add --paper`. |

**Forward vs reverse — what you may edit.** You drive only the forward
fields (`related` / `extends` / `contradicts`) via `lit modify --add-tag` /
`--rm-tag`; each write triggers the paired reverse write on the opposite
paper. The reverse fields (`extended-by` / `contradicted-by`) are
maintained by that auto double-write **only** — `lit modify` rejects them
as `--add-tag` / `--rm-tag` targets, because naming a reverse field by hand
would break the pairing. To re-sync a broken pair, always act on the
forward field. (`related` has no separate reverse field; it pairs with
itself.)

`lit rename <old> <new>` rewrites every occurrence of `<old>` across all
papers' relations fields (forward and reverse) atomically. `lit rm <id>`
shows the count of external references that will be torn down and prompts
for confirmation (default `N`); answer `y` (or pass `-y` for
non-interactive runs) to commit the cascade atomically.

## Project-relevance layer (dynamic per project)

When you bind a paper to a project with `lit link <id> --project <P> --relevance "…"`,
litman writes a per-project field:

```yaml
relevance-PepCodec: "Direct baseline — section 3.2 reuses the encoder block."
relevance-PepForge: "Cited only as motivation in introduction."
```

| Field | Type | Notes |
|---|---|---|
| `relevance-<project>` | string or null | One field per project the paper is linked to. The project name (lower / mixed case) appears verbatim in the field name. Set via `lit link --relevance` or later via `lit modify --set relevance-<project>=…`. Removed by `lit unlink` (default) unless `--keep-relevance`. |

The dynamic field name is intentional: it lets each project carry its
own perspective on the same paper (e.g. baseline for one project,
peripheral for another) without forcing a uniform schema.

## Putting it together — full example

The example below is a **fully evaluated** paper after a deep read, not
the freshly-added state from `lit add`. On first add, `type`, `priority`,
`read-date`, and `last-revisited` are all `null`; you fill them via the
`lit-reading` skill's verdict ritual or `lit modify --set …`.

```yaml
# Identity
id: 2017_Vaswani_Attention
title: "Attention is all you need"
authors:
  - "Vaswani, Ashish"
  - "Shazeer, Noam"
  - "Parmar, Niki"
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

# Audit (machine)
created-at: "2026-05-13T10:00:00+00:00"
updated-at: "2026-05-13T11:24:11+00:00"

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
read-date: "2024-11-02"
last-revisited: "2026-04-18"

# Relations
related:
  - 2018_Devlin_BERT
contradicts: []
contradicted-by: []      # CLI-maintained reverse field
extends: []
extended-by: []          # CLI-maintained reverse field
code-clones:
  - transformer-pytorch

# Project relevance (dynamic)
relevance-PepCodec: "Foundational architecture; encoder block reused in section 3.2."
```

## Adding a new field

The schema is open by design. To capture a new dimension:

1. Decide the field name (kebab-case, lowercase). Pick a name that reads
   well in a YAML form and that doesn't collide with the layers above.
2. Add it via `lit modify <id> --set <field>=<value>` on one paper.
3. Wait until ≥5 papers genuinely need the field before considering
   first-class CLI support. Until then, treat it as a personal extension.

There is **no migration step** — the schema is the union of fields ever
written. `lit health-check` does not warn on unknown fields.
