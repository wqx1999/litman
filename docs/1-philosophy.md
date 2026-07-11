# Design Philosophy

## The problem: a hand-kept library rots

Managing a research library by hand fails in three predictable ways.

1. **Libraries drift, and only a human can repair them.** Tags accumulate
   inconsistencies, near-duplicates creep in, and classifications slowly stop
   meaning the same thing. Correcting this is tedious manual work, so in
   practice a library stays tidy for a few months and is then abandoned for a
   fresh one. The organising structure lives in one person's head, and no one
   can hold a large library coherent that way for years.

2. **Papers are islands.** A paper's relationships to other papers exist only in
   the reader's memory. There is no durable, explicit link between papers, and
   code repositories live in a separate world with no first-class connection
   back to the paper that produced them.

3. **One paper serves many projects, but should live in one place.** A paper
   read for one project is often relevant to another later. If every project
   keeps its own copy, the copies diverge. The paper should exist once, as a
   single source of truth, with each project referencing a projection of it
   rather than a private duplicate.

## A curated library, not a general one

litman manages a library you curate by hand, not a general collection it fills
for you. Every paper in the vault is one you have read, or at least judged, and
chosen to keep, and the notes and verdict on each are written by you. You add
papers one at a time, at the moment you decide a paper is worth keeping.

This is a deliberate limit, not a missing feature. litman has no (1) topic search
that bulk-imports results, (2) batch ingest, or (3) agent that fills the library
on its own. Each of those would put papers in the vault that you never actually
evaluated.

A library is only useful while you stay in control of what is in it. Once most
of its contents are papers you never read or judged, you have lost that control,
and it decays into the same noise as a neglected hand-kept one. Keeping every
entry a deliberate human decision is what makes the rest of litman worth having.
A controlled vocabulary, a knowledge graph, and agent retrieval are only as
trustworthy as the judgement behind each paper they act on.

## How litman responds

litman is built as three layers, described here from the top (what you touch)
down to the bottom (what keeps the library honest).

### Top layer: how you touch the library

You reach the library three ways, and all three go through the same validated
core.

**The Web UI is the everyday front door.** Running `lit gui` opens a three-pane
browser interface — a classification tree on the left, a tabbed PDF reader in the
middle, a context panel on the right — for the things you do most: browsing,
reading, annotating, taking notes, and the everyday curation around them — status
and tags, delete and restore, projects, the taxonomy. What the UI exposes is a
subset of the CLI, and a growing one. Day to day, this is where you work, and you
rarely need to think about commands at all.

**The CLI is the foundation everything else rests on.** Every operation on the
library is a dedicated `lit` subcommand (see [4-commands.md](4-commands.md)), and
the CLI is complete on its own. It runs with no GUI, no agent, and no API key, so
the library stays fully operable even with a weak language model or none at all.
For every structured change, the Web UI and the agent are wrappers over these
same commands, so nothing they do can bypass the checks the CLI enforces. The one
exception is a short whitelist of free-form files the Web UI writes itself — your
PDF annotations, `notes.md`, and `discussion.md` — and even those go through the
same atomic write.

**The agent is the optional layer on top.** For anything past everyday browsing,
we recommend driving litman through an AI agent, which makes managing the
library faster. litman ships two orchestration skills (`lit-library` for the
write side, `lit-reading` for the read side) that let you work in plain language
instead of memorising flags, and an agent traverses the links between papers,
code, and projects far faster than reading them by hand, so it reaches the right
context for a task without scanning the whole library.

The agent works by typing the `lit` commands for you and reporting what each one
did. It only ever proposes the command; the CLI is what validates the input and
writes the data, so a hallucinated or malformed value is rejected before it
reaches disk rather than quietly corrupting the library.

### Middle layer: everything lives in the vault, projects only borrow

The library (the **vault**) is a plain directory of local source files. Every
piece of knowledge about a paper lives inside it: the original PDF, the
structured `metadata.yaml`, your `notes.md` and longer `discussion.md`, the
links to other papers, and the code repositories cloned for it. Nothing about a
paper is kept outside the vault.

Projects do not own papers. Linking a project to a paper creates a projection on
the project side (a symlink back into the vault plus a generated reference list).
The paper stays in the vault as the single source of truth, and one paper can
serve any number of projects at once with no duplication. Re-tag or re-read a
paper, and every project that borrows it sees the change, because they all point
at the same original.

The knowledge graph (paper-to-paper, paper-to-code, paper-to-project) is never
stored as a separate file. It emerges from these links and can be reconstructed
by any tool that reads the metadata.

### Bottom layer: the machinery that keeps the library from rotting

The bottom layer answers problem 1 directly. Rather than leaving you to mop up
drift by hand, it prevents what it can and surfaces the rest.

**Prevention.**

1. **Every change is atomic.** Multi-file edits stage all their writes under
   `<vault>/.litman-staging/<op-id>/` and promote them in a single
   `os.replace()`. A crash mid-edit leaves an abandoned staging directory, never
   a half-applied change.
2. **Classification is governed, not free-form.** The values allowed in a
   paper's `topics`, `methods`, `projects`, and `data` fields come from a
   controlled vocabulary (the TAXONOMY). You cannot create a tag just by typing
   it, and renaming or merging a value goes through `lit taxonomy`, which
   cascades the change across every referencing paper in one atomic operation.
   This is the first defence against the tag drift of problem 1.
3. **The source-of-truth files are read-only.** `metadata.yaml`, `TAXONOMY.md`,
   and the PDF are held read-only on disk, so a stray editor save cannot corrupt
   them. The `lit` commands unlock, write, and re-lock. It is a speed bump
   rather than a wall (you can still override it on purpose), enough to keep
   accidental hand-edits from being what rots the library.

**Detection.** Whatever slips past prevention is surfaced, not left to fester.

1. A cheap check runs on every `lit` command. It reads only the index and the
   registry (never the hundreds of metadata files), so it stays fast, and when
   it finds a serious inconsistency it offers to fix it on the spot.
2. `lit health-check` is the full sweep over the whole library, covering the
   deeper checks the cheap path deliberately skips.

**The vault and its backup.** The vault is the central unit. You can register
several (your own, a colleague's snapshot), but exactly one is active at a time.
Because the vault is deliberately not under version control, backup is the job of
remote sync: `lit sync` mirrors the active vault to a cloud target, so a lost
machine never means a lost library.
