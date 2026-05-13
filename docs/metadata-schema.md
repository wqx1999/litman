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
`thesis`, `book-chapter`. Default: `research`.

## Personal evaluation layer

| Field | Type | Default | Notes |
|---|---|---|---|
| `status` | string enum | `inbox` | Reading workflow state. Values: `inbox`, `skim`, `deep-read`, `dropped`. |
| `priority` | string enum | `B` | Reading priority. Values: `A`, `B`, `C`. |
| `read-date` | date string or null | null | When you finished a deep read. **Semantic** (user-set). Format `YYYY-MM-DD`. |
| `last-revisited` | date string or null | null | When you most recently re-read or referenced this paper. **Semantic** (user-set). Format `YYYY-MM-DD`. |

`read-date` and `last-revisited` are deliberately separate from
`created-at` / `updated-at`. The audit timestamps record what the tool
did to the file; the semantic dates record what *you* did with the
paper. Never merge them — `lit health-check` warns on suspicious
combinations (e.g. `read-date` set but `status` still `inbox`).

## Relations layer

All fields are list[string] of other paper ids. Empty by default. The
forward direction is stored on this paper; back-references are derived
on demand by `lit show` and `lit health-check`.

| Field | Semantic |
|---|---|
| `related` | Generic "see also". Use when the link is informational, not directional. |
| `contradicts` | This paper's results disagree with the linked paper(s). |
| `extends` | This paper builds on or improves the linked paper(s). |
| `code-clones` | Names of repos under `<vault>/codes/<name>/` that implement this paper. Maintained by `lit code link` / `lit code add --paper`. |

`lit rename <old> <new>` rewrites every occurrence of `<old>` across all
papers' relations fields atomically. `lit rm <id>` refuses if any other
paper still references `<id>`, unless `--cascade` is given.

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
extends: []
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
