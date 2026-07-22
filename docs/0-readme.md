# litman

**lit**(erature) **man**(ager) keeps every paper you read on your own computer, and
lets an AI assistant do the filing. You curate the library by hand; each paper lives in one folder of
plain files — the PDF, structured metadata, and your notes. Day to day you work in the **Web UI** (`lit gui`), a
browser app for reading, annotating, and everyday curation; for anything beyond
that, ask your **AI agent** in plain language and it runs the right command
for you. Underneath both, the `lit` **command line** performs every operation itself
— so everything still works typed by hand, and the tool never depends on an LLM.

These pages are the full documentation set. This one tells you where to start and
what each of the others is for.

## Before you start

1. **Don't move a vault or project folder by hand.** The links, project
   bridges, and registry that hold it together are path-based; if you must move
   one, run `lit health-check --fix` afterward to repair what broke.
2. **Don't edit metadata files by hand.** Change papers and taxonomy through the
   Web UI or your AI agent — both go through validated `lit` commands.
   `lit-config.yaml` is the one settings file you may edit directly.
3. **Figure and table reading needs a multimodal model.** A text-only model falls
   back to plain-text extraction and cannot see figures or image-based tables.
4. **Keep the library on an internal drive.** The `views/` browsing hubs and
   the project bridges are folder links, which FAT32 / exFAT drives (most USB
   sticks) and network shares cannot hold. On such drives litman says so once
   and skips them; metadata, PDFs, notes, and everything else work normally.
5. **After a crash or power loss, run `lit health-check`.** It finishes or
   rolls back any write the interruption cut short.

## Start here

**If you just want to use litman, read the [tutorial](5-tutorial.md) and nothing
else.** It walks one real paper through a complete reading day — install, set up
a library, add the paper, read it, keep its fields current, write notes, link its
code, export a bibliography — and that single path covers about 80% of everyday
use.

For the occasional thing the tutorial leaves out (a second library, cloud sync,
TAXONOMY housekeeping, a flag you half-remember), you have two routes, fastest
first:

1. **Ask the agent — recommended.** `lit agent` opens it in your library; then
   just say what you want: *"how do I rename a paper in lit?"*, *"merge topics
   gnn and graph-neural-net"*. The skills read the reference pages and run the
   command for you, faster and more accurately than scanning docs by hand.
2. **Look it up** in [4-commands.md](4-commands.md) (every command and flag) or
   [3-concepts.md](3-concepts.md) (what a given field means).

You rarely need the design pages (1 and 2) to use litman at all — they are there
for when you want to know *why* it works the way it does.

## The documentation set

The files are numbered as one continuous read-through, most abstract first and
hands-on last. **You do not have to read them in order** — most people jump
straight to the tutorial and open the rest only when they want the reasoning or a
specific reference.

| File | What it covers | Open it when |
|---|---|---|
| [1-philosophy.md](1-philosophy.md) | Why litman manages a hand-curated library rather than a collection it fills for you, and what follows from that choice | You want the reasoning behind the design, or are deciding whether litman fits how you work |
| [2-architecture.md](2-architecture.md) | The four layers — vault files, Python package, the `lit` CLI and Web UI, and the optional agent layer — and how a read or a write flows through them | You want to know where things live on disk and why the tool stays usable without an LLM |
| [3-concepts.md](3-concepts.md) | The field-by-field reference for `metadata.yaml`, `repo-meta.yaml`, `TAXONOMY.md`, `lit-config.yaml`, `INDEX.json`, and the vault registry, plus a glossary of the terms the other pages use | You need to know exactly what a field means or which values it accepts |
| [4-commands.md](4-commands.md) | Every `lit` subcommand, the shapes you call it in, and all of its flags, grouped as `lit --help` groups them | You need the full options for a command, or a command the tutorial skips |
| [5-tutorial.md](5-tutorial.md) | One real paper taken through a complete everyday workflow, shown three ways — in the Web UI, as agent requests, and as `lit` commands | **You are getting started — begin here** |
| [6-agent-benchmark.md](6-agent-benchmark.md) | How well different models and agents drive litman, and how that was measured | You are choosing which agent and model to run |

Installation is the first step of the [tutorial](5-tutorial.md). For any single
command, `lit <cmd> --help` is always the most current authority.
