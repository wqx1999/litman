# Tutorial

This walkthrough takes one real paper through a complete everyday workflow:
install litman, set up a library, register a project, add the paper, read it,
keep its fields up to date, write notes, link it to the project, clone its code,
export a bibliography, and come back for a second read. By the end you have done
every action a normal reading day needs.

It is deliberately the **basic daily path**, not a complete reference. Anything
not shown here lives in [3-concepts.md](3-concepts.md) (what every field means)
and [4-commands.md](4-commands.md) (every command and flag). The fastest way to
look something up, though, is to **ask the agent** ("how do I rename a paper in
lit?") — it reads those pages for you, faster and more accurately than scanning
them by hand.

The paper used throughout is **PepINVENT** (Geylan et al., *Chemical Science*
2025, [10.1039/D4SC07642G](https://doi.org/10.1039/D4SC07642G)), which ships a
public repository so the code-binding step works for real. A second paper,
**PocketXMol** (Peng et al., *Cell* 2026), is added briefly so the list views
have more than one row. Both DOIs resolve through CrossRef, so you can reproduce
every step.

> **Tested with** litman 1.0.0 on Unix (Linux and macOS), June 2026, with Claude
> Sonnet 4.6 driving the agent path in Claude Code. The command output shown
> below comes from this setup; exact wording can shift slightly with a different
> litman version.

## Three surfaces, used where each fits

litman is a human–machine tool, so most steps below can be done more than one
way. Reach for whichever fits the moment:

- 🖥️ **Web UI** — the browser app you open with `lit gui`: a classification tree,
  a tabbed PDF reader, and a context panel. This is the human's home for reading,
  annotating, and everyday curation — where you spend most of a reading day.
- 🤖 **Agent** — what you say in Claude Code. The bundled skills (`lit-library`
  for the write side, `lit-reading` for the read side) turn your sentence into the
  exact `lit` command, and the agent does the heavier lifting too: extracting
  metadata, drafting notes, traversing the links between papers.
- ⌨️ **CLI** — the `lit` command you type yourself. It is the complete surface
  underneath the other two; drop to it whenever you want precision or a script.

Read and tag in the UI, hand bigger jobs to the agent, drop to the CLI when you
want control — and mix them freely; all three drive the same validated core. The
Web UI covers the everyday reading and curation and keeps growing; whatever it
does not cover yet, the agent or the CLI always can. A couple of steps (installing
the tool, importing a PDF) start from the agent or command line by nature, and
are marked so.

## The fields you maintain while reading

`lit add` fills a paper's identity (title, authors, year, DOI) automatically and
leaves the rest empty. The fields below are the ones **you** fill as you read. In
the Web UI these are the controls in the context panel; the commands in the table
are what they run underneath (and what you type, or ask the agent for). Keeping
them current while the paper is fresh in your head is the single habit that makes
the library searchable later, so build the muscle memory now: tag as you read,
not in a cleanup pass three months on.

| Field | What it captures | Underlying `lit` command | Controlled by |
|---|---|---|---|
| `type` | what kind of paper it is | `lit modify --set type=research` | fixed enum |
| `status` | where it is in your reading | `lit skim` / `lit promote` / `lit drop` | fixed enum |
| `priority` | how much it matters to you | `lit modify --set priority=A` | fixed enum (`A`/`B`/`C`) |
| `topics` | subject matter | `lit modify --add-tag topics=peptide-design` | TAXONOMY (register first) |
| `methods` | techniques used | `lit modify --add-tag methods=reinforcement-learning` | TAXONOMY (register first) |
| `data` | datasets used | `lit modify --add-tag data=...` | TAXONOMY (register first) |
| `projects` | which of your projects it belongs to | `lit link --project peptide-design` | project registry |

`type`, `status`, and `priority` take a value from a fixed list (see
[3-concepts.md](3-concepts.md) §1.1). `topics`, `methods`, and `data` take any
value you register first in the TAXONOMY ([3-concepts.md](3-concepts.md) §1.3).
`projects` is set by linking, covered in step 8.

---

# Part 1 — One-time setup

You do these three steps once per machine (steps 1–2) and once per project
(step 3). After that you live in Part 2.

## 1. Install litman

Command-line only — you install the tool before any agent or Web UI can use it.
litman installs with [pipx](https://pipx.pypa.io/), which keeps it in its own
isolated environment and puts `lit` on your PATH:

```console
$ pipx install litman
$ lit --version          # confirms lit is installed and on your PATH
```

(Plain `pip install litman` works too if you manage your own environment.)

**To remove it:** run `lit uninstall` first — it strips the agent skills, the
shell completion, and the vault registry while leaving your papers untouched —
then `pipx uninstall litman` to remove the CLI itself.

## 2. Set up your library

A *library* (or *vault*) is the single directory that holds every paper. You
never create it by hand — `lit` builds it with the right skeleton and registers
it so future commands find it automatically.

🤖 **Agent:** *"set up a new litman library under ~/research"* → runs the command
below.

⌨️ **CLI:**

```console
$ lit init ~/research
```

This creates `~/research/literature_vault/` with the standard layout, registers
it, and makes it active. Because it is the active library, every later command
finds it with no flags — you do not set any environment variable.

Prefer a guided wizard? `lit setup` walks the same setup plus shell completion
and the agent skills. It also offers cloud sync — **decline that step**; this
tutorial stays sync-off (see [4-commands.md](4-commands.md) under `lit sync`).

**To unregister this library:** `lit vault remove literature_vault`. That removes
it from the registry only — the directory and your papers stay on disk; delete
them yourself if you want them gone.

## 3. Register your first project

A *project* is a name bound to a directory on disk. Linking a paper to it (step
8) drops a reference into that directory and lets you export a per-project
bibliography. The directory must already exist.

🤖 **Agent:** *"register a project called peptide-design at ~/projects/peptide-design"*
→ runs the command below.

⌨️ **CLI:**

```console
$ lit project add peptide-design --path ~/projects/peptide-design
```

This registers the project in both truth sources (the TAXONOMY and the config)
in one step. You can now use `peptide-design` as a `projects` value. (Projects
can also be created and renamed from the Web UI once it is running; the command
above is the quickest way during first-run setup.)

**To remove it:** `lit project rm peptide-design`. This untags every paper that
referenced it and drops it from the registry, after a `[y/N]` confirmation.

---

# Part 2 — A paper from add to second read

This is the loop you repeat for every paper. Steps 4–11 follow one paper,
PepINVENT, from import to its second read.

## 4. Add the paper

Adding a paper is where the agent earns its keep: it reads the PDF and pulls out
the metadata, so this step lives on the agent or the command line. `lit add` needs
the PDF file (litman manages papers you have already obtained; it does not
download them) and a metadata source. There are two sources:

- `--doi` — fetch the metadata from CrossRef. No model involved.
- `--from-llm-json` — the agent reads the PDF, extracts the metadata to JSON,
  and hands it to `lit add`. This is the path the `lit-library` skill uses, and
  where the model (Sonnet, here) does its work.

🤖 **Agent:** drop the PDF into the chat and say *"add this paper to my library"*. The skill reads it, extracts the metadata,
and runs `lit add --from-llm-json` (or `--doi` for a clean DOI), passing the id
you named.

⌨️ **CLI:**

```console
$ lit add ~/Downloads/pepinvent.pdf --doi 10.1039/D4SC07642G --id 2025_Geylan_PepINVENT
Paper added: 2025_Geylan_PepINVENT
Folder: ~/research/literature_vault/papers/2025_Geylan_PepINVENT

Title: PepINVENT: generative peptide design beyond natural amino acids
Year: 2025    Journal: Chemical Science
Authors: Geylan, Gökçe et al. (10 authors)
```

CrossRef fills the identity fields. The `--id` gives the paper a short handle;
drop it and litman auto-derives one from the year, author, and title (here that
would be `2025_Geylan_PepINVENT-Generative`). Note that `lit add` *moves* the PDF
into the vault, so the original in `~/Downloads` is removed once the import
succeeds — the vault now holds the only copy.

Add the second paper the same way, so the list has more than one row:

```console
$ lit add ~/Downloads/pocketxmol.pdf --doi 10.1016/j.cell.2026.01.003 --id 2026_Peng_PocketXMol
```

`lit list` shows where everything stands (in the Web UI this is the paper list in
the left pane):

```console
$ lit list
                                          Papers (2 of 2)
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━┳━━━━━━━━┳━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ id                    ┃ year ┃ type ┃ status ┃ pri ┃ title                                       ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━╇━━━━━━━━╇━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 2025_Geylan_PepINVENT │ 2025 │ -    │ inbox  │  -  │ PepINVENT: generative peptide design beyond │
│                       │      │      │        │     │ natural amino a…                            │
│ 2026_Peng_PocketXMol  │ 2026 │ -    │ inbox  │  -  │ Unified modeling of 3D molecular generation │
│                       │      │      │        │     │ via atomic inte…                            │
└───────────────────────┴──────┴──────┴────────┴─────┴─────────────────────────────────────────────┘
```

**To remove a paper:** preview the impact with `lit rm 2025_Geylan_PepINVENT
--dry-run` first, then `lit rm 2025_Geylan_PepINVENT` moves it to the trash after
a `Delete? [y/N]` prompt. It stays recoverable with `lit trash restore
2025_Geylan_PepINVENT`; `--purge` deletes permanently instead. (The Web UI can
soft-delete and restore too.)

## 5. Start reading

litman tracks where a paper is in your reading with its `status`. A fresh paper
is `inbox`. Mark it `skim` when you start, `deep-read` when you commit to it.

🖥️ **Web UI:** open the paper and set its status from the context panel — `skim`
when you start, `deep-read` when you commit. You are reading the PDF in the same
window, so it is one click away.

🤖 **Agent:** *"I'm starting to skim PepINVENT"* → `lit skim`; later *"I'm doing a
deep read of it"* → `lit promote`.

⌨️ **CLI:**

```console
$ lit skim 2025_Geylan_PepINVENT
$ lit promote 2025_Geylan_PepINVENT
```

`status` is reversible at any time with `lit modify 2025_Geylan_PepINVENT --set
status=inbox`, so there is nothing to undo here.

## 6. Capture the discussion as you read

Each paper has a `discussion.md` — your running log of questions, objections, and
working-through while you read. Keep it open and write to it as things occur to
you.

🖥️ **Web UI:** the reader has a discussion tab beside the PDF. Jot questions and
objections there as they occur, without leaving the page.

🤖 **Agent:** talk through the paper in Claude Code. When you say *"note that down"*
or work through a question, the `lit-reading` skill appends the exchange to
`discussion.md`.

✏️ **By hand:** open
`~/research/literature_vault/papers/2025_Geylan_PepINVENT/discussion.md` and
write.

`discussion.md` is your *thinking while reading*; `notes.md` (step 7) is your
*settled summary after*. Keeping them separate means a later reader (you, or the
agent) can read the conclusion without wading through the back-and-forth.

## 7. Maintain the fields and write notes

As you understand the paper, record what it is, tag what it covers, and write your
summary. This is the everyday curation the Web UI is built for.

🖥️ **Web UI:** in the paper's context panel, set `type` and `priority` from their
dropdowns and add `topics` / `methods` tags — type a new value and it is
registered in the TAXONOMY on the spot. Write the summary straight into the notes
tab. You are already reading the PDF right there, so nothing pulls you out of the
paper.

🤖 **Agent:** *"PepINVENT is a research paper, priority A; tag topics
peptide-design and de-novo-design, method reinforcement-learning; then summarize
it into its notes"* → the skill registers any missing taxonomy values, sets the
fields, and drafts the summary into `notes.md`. Review and edit the draft; it is a
starting point, not the final word.

⌨️ **CLI:** the same, one operation at a time. The scalar fields set directly:

```console
$ lit modify 2025_Geylan_PepINVENT --set type=research
$ lit modify 2025_Geylan_PepINVENT --set priority=A
```

The controlled-vocabulary fields are **register-first**: a value must exist in the
TAXONOMY before it can be tagged onto a paper, so register then tag (this is what
the UI's type-a-new-value and the agent both do for you underneath):

```console
$ lit taxonomy add topics peptide-design de-novo-design
$ lit taxonomy add methods reinforcement-learning
$ lit modify 2025_Geylan_PepINVENT --add-tag topics=peptide-design
$ lit modify 2025_Geylan_PepINVENT --add-tag topics=de-novo-design
$ lit modify 2025_Geylan_PepINVENT --add-tag methods=reinforcement-learning
```

The metadata is schema-less, so you can also record anything else with a plain
`--set`, for example a note on why it matters:

```console
$ lit modify 2025_Geylan_PepINVENT --set relevance-peptide-design="Baseline generator for the macrocycle work."
```

However you write `notes.md`, a `[[2026_Peng_PocketXMol]]` wikilink in the prose
creates a tracked cross-paper link (and `lit rename` keeps it valid if either id
changes later).

**To undo a tag or field:** `lit modify 2025_Geylan_PepINVENT --rm-tag
topics=de-novo-design` removes one tag; `lit modify ... --set priority=` (empty
value) clears a scalar.

## 8. Finish: link to the project and clone the code

When you have finished the read, stamp it, connect it to your project, and pull in
its code.

🖥️ **Web UI:** mark the read complete and link the paper to `peptide-design` from
the context panel — the read stamp and the project link are both there.

🤖 **Agent:** *"I've finished PepINVENT — link it to peptide-design with relevance
'Baseline macrocycle generator.' and clone its repo"* → the three commands below.

⌨️ **CLI:**

```console
$ lit read 2025_Geylan_PepINVENT
$ lit link 2025_Geylan_PepINVENT --project peptide-design --relevance "Baseline macrocycle generator."
$ lit code add https://github.com/MolecularAI/PepINVENT --paper 2025_Geylan_PepINVENT
```

`lit read` stamps `read-date` (the first-read marker). `lit link` tags the
project, drops a reference under `~/projects/peptide-design/litman_reflib/`, and
regenerates that project's `REFERENCES.md`. Cloning the code (`lit code add`) is an
agent/CLI step — it fetches a git repository into the vault and binds it to the
paper in both directions at once.

**To undo:** `lit unlink 2025_Geylan_PepINVENT --project peptide-design` reverses
the link; `lit code rm PepINVENT --cascade` removes the clone and unbinds it.

## 9. Export a bibliography

Exporting a `.bib` is an agent/CLI step. When you cite the paper, export the
project's references as BibTeX; the cite key is the paper id, so
`\cite{2025_Geylan_PepINVENT}` works across machines.

🤖 **Agent:** *"export a bib file for peptide-design"* → runs the command below.

⌨️ **CLI:**

```console
$ lit export --project peptide-design -o ~/projects/peptide-design/refs.bib
```

Re-run the same command to update the file as you link more papers. `lit export
--all` exports the whole library. (There is nothing to undo — the `.bib` is a
projection, not vault state; delete the file if you no longer want it.)

For a single paste-ready citation instead of a whole file — the kind you drop on a
slide — `lit cite 2025_Geylan_PepINVENT` prints one clean line to stdout, and the
Web UI shows the same per-paper Cite button.

## 10. Come back: the second read

Months later you open the paper again. litman records this with `last-revisited`,
the companion to the `read-date` you stamped in step 8:

- `read-date` is set **once** — the day you first read the paper, and it never
  moves after that.
- `last-revisited` holds the **most recent** day you came back, overwritten each
  time you return.

The pair lets you tell a paper you read once and were done with from one you keep
returning to. Marking a revisit writes today into `last-revisited` — it does not
touch `read-date`, and it does not open the PDF; it only records the date.

🖥️ **Web UI:** open the paper and hit revisit in the context panel; it stamps
today into `last-revisited`.

🤖 **Agent:** *"I'm re-reading PepINVENT"* → `lit revisit`.

⌨️ **CLI:**

```console
$ lit revisit 2025_Geylan_PepINVENT
```

Do it whenever you come back to the paper (not at any particular point in the
re-read — it just marks the day). Same-day repeats do nothing, and `--date
2026-05-01` backdates an older revisit. Add to `notes.md` and `discussion.md` as
you re-read, exactly as before.

---

## Where to go next

That is the whole daily loop. The second paper, PocketXMol, runs through the same
steps whenever you are ready to read it. Open `lit gui` to browse and read it all
in one place. For anything beyond this path — multiple libraries, cloud sync,
TAXONOMY housekeeping, health checks — see [4-commands.md](4-commands.md), or just
ask the agent.
