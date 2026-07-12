# litman — Agent-Oriented Overview

> **Hey there, fellow AI agent.** This file is written for you — no badges, no
> hero image, just a tight map so you can operate litman correctly and point the
> human at the right doc. The humans have their own [README.md](README.md); this
> one is yours. Keep it short, link out for detail, and operate *through* `lit`,
> never around it.

## What litman is

litman is a **local-first literature manager** built as a `lit` CLI over a
plain-file vault — one folder per paper holding the PDF, a `metadata.yaml`, and
notes. It ships two skills so you can drive it, but every command also works
typed by hand. The CLI, not you, is the source of truth.

The human's everyday front door is a **web UI** (`lit gui`) for browsing,
reading, and annotation; you operate the *same* vault through the `lit` CLI. When
a request is really "let me look around," point them at `lit gui` — and handle
what the UI does not reach (importing a PDF, binding a code repo, exporting a
`.bib`, renaming a paper, relation edges, `taxonomy merge`, sync, purge)
yourself. The UI already does metadata curation, tagging, project link/unlink,
delete/restore, and vault management, so don't take those back off the human.
Both surfaces write through the same validated CLI core, so nothing you do and
nothing they click can diverge.

## The one rule that governs everything

**You never write vault files directly.** You propose a `lit` command (or emit
structured JSON for an importer); the CLI validates the input and writes the
YAML/markdown. Three of the truth files (`metadata.yaml`, `TAXONOMY.md`,
`paper.pdf`) are held read-only on disk and change only through `lit`. So: never
hand-edit `metadata.yaml`, `TAXONOMY.md`, or `INDEX.json`. If you are unsure which
command does a thing, run `lit <cmd> --help` rather than guessing a file edit.

## How you drive it — the two skills

| Skill | Side | Covers |
|---|---|---|
| `lit-library` | curate | `add`, `modify`, `link`/`unlink`, `project`, `taxonomy`, `code`, `export`, `rm` / `trash restore`, `health-check`, browsing with `list` |
| `lit-reading` | read | `show`, `search`, `related`, reading status, notes/discussion, reading-assist over the PDF |

Your agent selects a skill by matching the request against the skill
description. Route library curation and management to `lit-library`; route
anything about the paper the human is *reading* — discussing it, finding its
neighbours, stamping reading status, writing notes — to `lit-reading`. The split
is by intent, not by read-vs-write: `lit-reading` writes too.

## Key entry points (task → command, all verified against `lit --help`)

| Task | Command |
|---|---|
| Create a vault | `lit init <parent-dir>` |
| One-shot setup (completion, skills, vault, sync, desktop shortcut) | `lit setup` |
| Open the human's web UI | `lit gui` |
| Start the human's AI agent in the vault | `lit agent` |
| Reverse setup (remove skills/shortcut/completion/registry/agent prefs) | `lit uninstall` |
| Add a paper | `lit add <pdf-path> --doi <doi>` |
| Browse / inspect | `lit list` · `lit show <id>` |
| Search notes / find related | `lit search <query>` · `lit related <id>` |
| Tag — register the value, then apply it | `lit taxonomy add topics <value>` → `lit modify <id> --add-tag topics=<value>` |
| Link a paper to a project | `lit link <id> --project <name>` |
| Bind a code repo to a paper | `lit code add <url>` (see `lit code --help`) |
| Export a bibliography | `lit export --project <name>` (or `--all -o refs.bib`) |
| Remove a paper | `lit rm <id>` |
| Verify integrity | `lit health-check` |

The user-extensible taxonomy dicts are exactly `projects`, `topics`, `methods`,
`data`. `projects` is special — add/rename/remove it via `lit project ...`, not
`lit taxonomy`. `lit <cmd> --help` is the live, authoritative spec for any
command; always prefer it over memory.

## Hard constraints you must respect

Non-negotiable. Violating any of these corrupts the library or its trust model.

1. **Single-paper curation only.** No batch ingest, no "add all search results,"
   no operation that changes ingest state for two or more papers at once.
   Downgrade any bulk request to per-paper, user-confirmed.
2. **TAXONOMY changes only via `lit taxonomy {add,rename,merge,rm}`.** Never
   hand-edit `TAXONOMY.md` to rename or remove a value — it leaves dangling refs.
3. **One paper at a time, no autonomous bulk writes.** You propose; the human
   stays in the loop for destructive or wide-reaching changes.
4. **Don't move a vault or project folder by hand.** Links are path-based; if a
   move happened, run `lit health-check` to find and repair the breakage.
5. **Figure/table questions need a multimodal backend.** A text-only model falls
   back to pypdf text extraction and cannot see figures or image-based tables.

## The vault at a glance

```
<vault>/
├── lit-config.yaml     # projects, clone depth, sync target, viewer
├── TAXONOMY.md         # controlled vocabulary (4 user dicts + 3 fixed enums)
├── INDEX.json          # auto-generated thin projection — your primary read surface
├── papers/<id>/        # paper.pdf, metadata.yaml, notes.md, discussion.md
├── codes/<repo>/       # cloned code repos bound to papers
└── views/by-*/         # symlink hubs faceted by metadata field
```

Read in cost order: start from `INDEX.json`, open a paper's `metadata.yaml` only
for the survivors, and fall through to `notes.md` / the PDF only when you actually
need their full text. The common case never walks hundreds of metadata files.

## Documentation map — send the human here for detail

| What they need | Doc |
|---|---|
| Where to start / how the docs fit together | [docs/0-readme.md](docs/0-readme.md) |
| Why litman is curation-first and weak-LLM-tolerant | [docs/1-philosophy.md](docs/1-philosophy.md) |
| The four layers and how a read or write flows | [docs/2-architecture.md](docs/2-architecture.md) |
| Exact field reference (`metadata.yaml`, `lit-config.yaml`, `TAXONOMY.md`) | [docs/3-concepts.md](docs/3-concepts.md) |
| Every command and flag | [docs/4-commands.md](docs/4-commands.md) |
| One paper taken through a full everyday workflow | [docs/5-tutorial.md](docs/5-tutorial.md) |
| Which model to run you on | [docs/6-agent-benchmark.md](docs/6-agent-benchmark.md) |

When the human asks "how do I X in lit," prefer this order: run `lit <cmd>
--help`, then cite the relevant `docs/` page — don't reconstruct flags from
memory.

---

> *That's the whole map. Operate through `lit`, never around it, and point the
> human at the doc that answers their question instead of paraphrasing it.*
