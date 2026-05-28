---
name: lit-reading
description: "Read-side companion to the litman vault. Use when the user discusses, compares, or asks about a paper they are reading, looks for related work across their vault, or wants to connect a paper to one of their own projects. Triggers: '这篇文章...', '这篇论文里关于...', '刚才那篇...', '<paper-id> 里...', 'how does it compare to <other>', 'who else does this', 'have I read anything similar', 'connect this to my <project>', '把这个观点和...联系起来', '记下这次讨论'. Also: resume reading ('继续读', '接着上次', '上次读到哪', '我在读哪篇来着', 'continue reading', 'what was I reading'); mis-deletion recovery ('误删了那篇...', '把删掉的 X 找回来', 'restore that paper I deleted'); read-finished verdict ('读完了', '这篇一般/没价值', 'done with this paper'); inbox triage ('清一下 inbox', 'triage my inbox'); library health ('我的库还干净吗', 'is my library still clean'); reading roundup ('最近读了啥', '我这周读了哪些', '汇总我库里关于 X 的', 'what did I read recently', 'summarize my library on X'). NOT for adding/modifying papers — that's the lit-library skill. Drives only reads of the vault (INDEX.json, metadata.yaml, notes.md, discussion.md, the PDF, project dev_docs); regenerates notes.md and appends discussion.md only when the user explicitly asks."
---

# lit-reading — Vault-aware Reading Companion

This skill teaches you (Claude) to **navigate the user's litman vault** during a reading discussion so your answers are grounded in their actual library, not generic web knowledge. Its differentiator is the same as litman's: TAXONOMY + INDEX.json give you precise retrieval *across* the user's papers, and metadata + symlinks + project bindings let you connect any paper to the user's own work.

## How this skill differs from lit-library

| User intent | Skill |
|---|---|
| "Add this paper to the vault" / "change the topic tag on X" / "bind this repo to that paper" | **lit-library** (drives the `lit` write surface) |
| "Discuss this paper" / "find similar work" / "compare to my project" / "summarise §3 for me" / "continue reading" | **lit-reading** (this skill) |

lit-reading is read-first, but it is **not** strictly read-only: it owns the *reading verdict* (the evaluation stamps it runs inline — see the autonomy ladder), and it **chains to lit-library** for any write that touches the shared vocabulary, the knowledge graph, ingest, or governance. If you want to tag a controlled value, add an edge, link a project, ingest a PDF, or run governance, that is a lit-library write — chain to it (A2), don't do it here.

## Architecture you must respect (read-side invariants)

1. **You navigate, you do not synthesize from training data.** Every claim about a paper must come from reading something inside the vault (metadata.yaml / notes.md / discussion.md / paper.pdf) or from the user. If a paper is not in the vault, say so — do not fabricate a summary from the paper's title.
2. **No state file.** There is no `lit focus` or `lit current` — do not assume one exists. The user tells you which paper is in scope, or you infer it from natural-language cues.
3. **Vault path is discovered, never hardcoded.** Resolve via `$LIT_LIBRARY`, the active vault in `~/.config/litman/vaults.yaml`, or by walking up from the user's cwd for `lit-config.yaml`. The exact path differs per machine — never paste a stale absolute path from prior sessions.
4. **Multi-vault aware.** The user may have several registered vaults. If a paper id isn't in the active vault, check cross-vault wikilinks (`[[<vault>:<id>]]` syntax in notes) and `lit vault list` before giving up. *Reading* another vault is fine; **writing** is confined to the active vault (see scope discipline).
5. **Agent-writable free-form = `notes.md` (overwrite) + `discussion.md` (append).** You may regenerate-and-replace `<vault>/papers/<id>/notes.md` (the current-understanding STATE snapshot, agent-assisted, user-read-only) and append dated sections to `<vault>/papers/<id>/discussion.md` (the immutable LOG). You must **never** write `metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, or `repo-meta.yaml` directly — those are structured fields, mediated by the `lit` CLI (chain to lit-library).

If any of these would be violated, push back to the user and propose the right alternative (e.g. "let me chain to lit-library to run `lit add`" or "I can only summarise what's actually in the notes — should I read the PDF first?").

---

# PART A — Autonomy, chaining, scope (read this first)

## A1. Autonomy ladder

Classify every action before you take it. The tier is about *behavior*, not which skill executes.

| Tier | Operation class | Behavior | Examples |
|---|---|---|---|
| 1 | **Read** | Just do it, don't ask | `lit list`, `lit show`, scan a PDF, query INDEX via `lit list --format json`, `lit vault list`, `lit code list`, `lit trash list`, `lit project list`, `lit health-check` |
| 2 | **Write, reversible, single-paper** | Do it, then report | the evaluation stamps `lit read` / `lit promote` / `lit skim` / `lit drop` / `lit revisit`, and `lit modify --set priority=` |
| 3 | **Write, multi-paper / structural / remote-IO** | Ask once before acting | `lit add` (ingest), `lit code add` (git clone), `lit taxonomy add` / `lit project add` (register a controlled value), `lit taxonomy merge`/`rename`/`rm` + `lit project rename`/`rm` (governance — cascades to every referencing paper) |

**Execution ownership — which Tier-2 writes lit-reading runs INLINE.** lit-reading runs inline **only the single-paper evaluation stamps that ARE the reading verdict**: `lit read`, `lit promote` / `lit skim` / `lit drop`, `lit revisit`, and `lit modify --set priority=`. Each carries no controlled vocabulary, no cross-paper cascade, no project-dir or remote write — that enumerated set is *why* it qualifies (the gate is the enumerated stamps, not fixed-enum-ness in general; a classification field like `type` is also fixed-enum but is **not** inline — it chains to [E]).

**Every other Tier-2 write lit-reading does NOT run — it CHAINS to lit-library** (A2): `lit modify --add-tag topics/methods/data=` (controlled vocabulary), `lit modify --add-tag extends/related/contradicts=` (cross-paper edge), `lit link --project` / `lit modify --set relevance-<P>=` (project binding). Reason: those SOPs (register-first, edge, link) live in lit-library, and a skill must stand alone without duplicating them in. The dividing line: *the reading verdict (read/skim/drop/revisit + priority + the note) is reading completing itself → inline; anything touching the shared vocabulary or the knowledge graph is curation → lit-library.*

The CLI already physically splits tier 2 from tier 3: registering a controlled value (`lit taxonomy add` / `lit project add`, tier 3, *user types it*) is a separate hard step from applying it (`lit modify --add-tag`, tier 2). The CLI hard-rejects unregistered values, so your only job is to **not invent values** (curation boundary) — the CLI polices registration.

## A2. Chain hand-off contract

Switching to lit-library is a real, explicit `Skill` tool call — there is no call stack and no auto-return. The chain works **only because this body literally tells you to chain and you call the Skill tool**. A target skill's `description` can never trigger a mid-task chain (descriptions match only on a *new user message*).

| Slot | What to fill in |
|---|---|
| **Trigger** | the user-confirmed write event below |
| **Target skill** | `lit-library` (named explicitly) |
| **Context to pass** | paper id, PDF path, the exact intent (e.g. "add `extends=<other-id>`"), and any user instruction already given |
| **Resume point** | the named Phase / branch to return to (e.g. "resume Phase 2 with the new id"), never "go back to before" |

**Direction is asymmetric: the chain is `lit-reading → lit-library` only.** The reverse rarely needs a real switch — once lit-library finishes, the result (e.g. a new id) is already in context and you continue down the still-resident lit-reading SOP. Do **NOT** add any "can be summoned by lit-reading" cue to any description; that pollutes the activation surface for zero benefit.

**What lit-reading chains for (recurring cases, not a closed list — the rule is A1's "every write outside the evaluation-stamp set"):**

1. Controlled-vocabulary tagging `lit modify --add-tag topics/methods/data=` → lit-library [E] (Flow A/B).
2. A cross-paper edge `lit modify --add-tag extends/related/contradicts=` → lit-library [E] (forward direction only — see B7).
3. Project binding `lit link --project` / `lit modify --set relevance-<P>=` → lit-library [H].
4. Ingest / clone / code-binding `lit add` / `lit code add` / `lit code link`, plus code unbind / retire (`lit modify --rm-tag code-clones=` or `lit code rm`) → lit-library [A]/[B]/[C].
5. Restore a trashed paper `lit trash restore` → lit-library [I].
6. TAXONOMY / project governance → lit-library [J]/[H].

The verdict ritual's core (B10) is **absent** from this list — it is inline — so the highest-frequency reading event triggers no switch. Only the *user-accepted* vocab/structure offers chain, which is exactly the legitimate forward chain (no reverse bounce needed).

## A3. Scope discipline — SOP vs maintenance, reads vs writes, active-vault confinement

**(1) SOP = high-frequency reading actions; maintenance falls through to `lit <cmd> --help`.** The Phase SOPs make you fluent at the high-frequency reading/discussion actions this skill is for. Low-frequency vault-maintenance — cloud sync (`lit sync`), switching the active vault (`lit vault use`), config edits (`lit config`), view rebuilds (`lit refresh-views`) — gets **no SOP**. This is not a refusal: the SOP is the expert shortcut, `lit <cmd> --help` is the universal fallback, the CLI is complete on its own.

- **Read vs write within the no-SOP set.** A no-SOP command that is a pure *read* — `lit code list`, `lit vault info`, `lit vault list`, `lit config show` — stays **Tier 1: just run it and report**. Lacking a narrative SOP lowers your *fluency at composing the call*, not a read's autonomy tier (so `lit vault list` in B1/B4 and `lit code list --paper <id>` run directly).
- **"Teach, don't do" applies only to no-SOP *writes / maintenance*.** When the user asks for a maintenance action with no SOP, understand the intent → run `lit <cmd> --help` to find the real command/flags → **surface the command for the user to run**, don't run it. A no-SOP path is by definition rare/unfamiliar — the user more likely wants control and a wrong move costs more. Execute it yourself **only when the user explicitly asks** ("you run it" / "just do it"). Either way, **never guess a flag or invent a command from memory** — a wrong maintenance command corrupts the library.

**Destructive operations split by reversibility:**

- **Soft-delete `lit rm`** (default → moves `papers/<id>/` into `.trash/`, recoverable via `lit trash restore`; recovery SOP at B13). You **never initiate** it (trigger is the user's), but **may execute it on an explicit, confirmed request**, and must **relay `lit rm`'s cascade report verbatim** ("This paper is linked with N entries…") — never silently delete, never summarize the link count away.
- **Irreversible removal** (`lit rm --purge`, `lit trash empty`): you **NEVER execute these, even on explicit request.** Your deletions are always the recoverable soft-delete; permanent destruction is user-sovereign. Surface the exact command and let the user run it (teach, don't do). *The user holds absolute sovereignty over their own knowledge base; the agent and litman help manage the vault, they never irreversibly destroy its contents.*
- **`lit code rm`** stays governed by lit-library [C] (confirm before execute; the clone is re-cloneable from upstream, so it is not in the never-execute tier).

**(2) Write only in the active (primary) vault.** Every write — evaluation stamps, and even appending to `notes.md` / `discussion.md` — targets the **currently-active vault only**. Cross-vault *reading* is fine (resolving a `[[<vault>:<id>]]` link, listing another vault — B1/B4); cross-vault *writing* is forbidden. If the user's intent requires writing into a different vault, do **not** switch silently — **tell the user to switch first** (`lit vault use <name>`, a user-owned maintenance action) and only then operate in what is by then the active vault.

---

# B9. PDF-as-entry chain — the headline workflow (chains to lit-library)

When the user drops a **raw PDF path** into the conversation and wants to discuss it, the first job is to find out whether it's already in the vault, because **a bare PDF has no metadata, no TAXONOMY, no notes, no project binding — so this skill's whole differentiator (controlled-vocab neighbor search, project links, prior notes) is dead until ingest.** "Chain to lit-library first" is the unlock switch, not a courtesy.

1. **Defensively check in-vault by title.** Grep the `title` field of `lit list --format json` for the PDF's title (the INDEX projection carries no content-hash field; `title` + `doi` are the only dedup keys). If you can read a DOI off the PDF, `lit show --paper-doi <doi>` is the exact check. (~99% of raw-PDF triggers are not-in-vault; ~99% of "that paper"/`<id>`/author triggers are in-vault.)
2. **If not in vault → propose-confirm:** "this isn't in your vault — want me to `lit add` it?" On the user's **yes**, chain to lit-library [A] (LLM-augmented) or [B] (DOI path); pass the PDF path + any DOI. Then **resume at lit-reading Phase 2** with the new id.
3. **A bare DOI / URL with no local file is NOT a raw-PDF entry trigger.** `lit add` ingests from a *local PDF you've read* (curation means you read it; litman is not a fetch-by-DOI discovery tool). If the user gives only a DOI/URL, do not treat it as an entry PDF — lit-library [B] will explain the precondition and ask for the file path.

---

# PART B — The reading SOP (per-operation phases)

Every trigger walks through these phases. Some collapse to one tool call; the heavier ones (cross-paper / project) are demand-only. The verdict ritual (B10) closes a reading session; resume (B15) opens one.

## Phase 1 — Locate the paper

Convert the user's reference into a canonical paper id by **reading the vault**, not by guessing.

```
Vault root  = resolve(`$LIT_LIBRARY` | active-vault-from-registry | cwd-walk)
```

### Resume branch — "继续读" (the user names nothing)

Triggers: "继续读" / "接着上次" / "上次读到哪了" / "我在读哪篇来着" / "continue reading" / "what was I reading" / "resume". Unlike the cue branches below (the user *names* a paper), here the user names nothing and wants the system to surface where they left off.

1. **Retrieve (one CLI call; the CLI does the work):** `lit list --unread --sort recent --format json`. `--unread` filters to `read-date`-empty (not-yet-finished) papers; `--sort recent` orders by `max(paper.pdf mtime, updated-at)` descending. **The CLI has already filtered and sorted — do NOT re-sort or re-filter.** The json projection carries **no timestamp / no `read-date`** (it is the INDEX projection): by design, the row order *is* the recency ranking and being *in* the list *is* the unread signal. Take the top rows; you never need to see timestamps (bounded retrieval).
2. **Client-side prune `dropped`:** rows carry `status`. `--unread` doesn't exclude by status, so a `dropped` paper dropped-by-title (never read, `read-date` empty) can still appear. Drop those rows — a discarded paper is not a "continue reading" candidate. **Keep `skim`** (a skimmed paper with no `read-date` may be one the user means to return to and deep-read).
3. **Present top 3–5 (present-and-user-picks):** id + title + `status`/`priority`, "most-recently-touched first". **Honesty constraint:** recency is a *proxy* (file mtime / `updated-at`), not a tracked "you were reading X" — phrase it "your most-recently-touched unfinished papers, probably where you left off", **never** "you were reading X". If the top guess is wrong, the user picks another.
4. **User picks → suggest `lit open <id>`** (suggest, don't run — see B14, unless the user says "open it"), then **resume at Phase 2** for the chosen paper.
5. **Empty result `[]`:** distinguish honestly — "nothing is marked unfinished (every paper has a `read-date`)" vs. "your library has no papers yet" (a plain `lit list` tells you which).

Closes the loop with B10: if a surfaced paper is `status: deep-read` yet appears here (deep-read but `read-date`-empty — the orthogonality B10 describes), nudge the B10 verdict ritual: "looks like you deep-read this but never marked it finished — want me to write the summary and stamp it read?". **Cross-vault is out of scope here** — resume operates within the active vault (the recency signal is per-vault file state); cross-vault *lookup* of a named paper stays in the cue branches below.

### Locate-by-cue (the user names a paper)

The bounded-retrieval ladder below finds papers *related to* a focus paper (Phase-3-style discovery). Locating the paper the user **named** is the inverse — and the rule is: **never grep INDEX for a field the projection doesn't carry** (the projection is id / title / year / type / priority / status / topics / projects / methods / data / doi — `authors` is NOT in it).

1. **Direct id given** → `lit show <id>` (or read `<vault>/papers/<id>/metadata.yaml`) to confirm it exists. If missing, ask the user; do not silently substitute another paper.
2. **Author cue** ("Pandi 那篇") → `lit list --author <cue> --format json`. The `--author` filter matches a case-insensitive substring against the full metadata file-side; the returned projection rows don't echo the author, but **being *in* the result is the hit**.
3. **Title cue** → grep the `title` field of `lit list --format json` (title IS in the projection).
   - 1 candidate → use it; confirm to the user "I'm reading `2023_Pandi_Cell-free`, Pandi et al. 2023 — *Cell-free …*" so they can correct.
   - 2–5 → list and ask which one.
   - >5 → ask the user to narrow the cue; do not list 30 rows.
4. **Topic cue** ("the GAT one", "anything on peptide hemolysis") → Tier-2 discovery below (`lit list --topic X --format json`) — the controlled vocabulary makes this filter sharp, not a noisy free-text grep.

### Bounded-retrieval ladder (in-vault; reuse in Phase 3)

litman scales to 500 papers (~53k tokens of INDEX). **Never `cat INDEX.json` whole** — use bounded retrieval:

| Tier | Trigger | How | Reads INDEX into context? |
|---|---|---|---|
| 0 focus paper | already in Phase 2 | the loaded focus metadata + notes + discussion | no |
| 1 explicit edges (preferred) | "related to this", "what does it extend", "what builds on / disputes this" | the focus metadata's `related` / `extends` / `extended-by` / `contradicts` / `contradicted-by` ids (the CLI stores **inbound** edges too, so "what extends/contradicts this paper" is answerable from the focus metadata alone) + ids named in `discussion.md` → `lit show <id>` (precise by id) | no |
| 2 bounded discovery (fallback, when Tier 1 can't answer) | "anything similar", "what's in my library about X" | `lit list --topic X --format json` (CLI filters file-side, returns only matching rows ~1–3k tok) | file read by CLI, NOT into context |
| never | — | `cat INDEX.json` whole | ❌ |

`lit list --format json` is the **Tier-2 primary path** (not a fallback footnote). `data` is in the projection now, so a Tier-2 hit row already carries the `data` key — no `lit list --data X` workaround needed. Reading the whole INDEX is a fallback only under ~100 papers.

**Cross-vault (regression guard):** this ladder operates *within the active vault*. If a paper id isn't in the active vault, cross-vault lookup still goes through `[[<vault>:<id>]]` wikilinks + `lit vault list` exactly as before — only the in-vault "read the whole INDEX" clause is gone.

### Retrospective / summary queries (route here, never to a vault grep)

- **"What did I read recently"** → `lit list --sort recent --format json`. The CLI orders by `max(paper.pdf mtime, updated-at)` descending (the **same proxy** the resume branch uses, not a tracked "you read X"); **summarize the returned rows, do NOT re-sort.** **Honesty constraint:** the projection carries **no timestamp / no `read-date`** (the row order *is* the recency ranking), and recency is a *proxy* for engagement (any write bumps `updated-at`, any annotation bumps the PDF mtime) — phrase it "your most-recently-touched papers, probably what you read lately", never a precise "you read these this week". There is **no `--since` flag**, so a precise calendar-week filter is **not answerable** under the current projection — do not fabricate one from a `read-date` (it is not in the projection; pulling it would need a per-paper `metadata.yaml` read, violating bounded retrieval). Offer the recency proxy instead.
- **"Summarize my library on X"** → Tier 2 (`lit list --topic` / `--method` / `--project X --format json`).

Both stay read-only — never `grep` the vault or `cat` INDEX.json to answer them; no per-paper load unless the user drills into one.

## Phase 2 — Load paper context (lazy)

On **entering a reading session**, read `TAXONOMY.md` once into context (it is small, ~few KB). Flow A (lit-library [E]) needs it to enumerate registered values; Flow B needs it to validate. **Cache invalidation:** after the user runs `lit taxonomy {add,rename,merge,rm}` (or `lit project add`) during the session, **re-read `TAXONOMY.md`**, or later validation uses a stale dict.

Then, once you have an id, load just enough paper context. Read in this order, stopping when you have enough:

| Tier | File | When to read |
|---|---|---|
| **Always** | `<vault>/papers/<id>/metadata.yaml` | Identity, taxonomy, refs |
| Usually | `<vault>/papers/<id>/notes.md` | The current-understanding snapshot — agent-assisted, user-read-only (overwrite-style summary, regenerated on explicit request) |
| On drill-down | `<vault>/papers/<id>/discussion.md` | The append+timestamp discussion trail (LOG); pull only the relevant dated section when the user drills into details, not by default |
| Only when needed | `<vault>/papers/<id>/paper.pdf` | For specific sections / figures / numbers — walk the **PDF reading ladder** below (use the `pages` parameter so you fetch §3, not the whole 40-page PDF) |

**PDF reading ladder — reading the PDF is a *must-achieve* goal, not best-effort.** Walk the rungs until one works; **never stop at rung 1's first failure** and report "I can't read PDFs":

1. **Claude Code `Read` tool** (default — covers ~99% of CC users with any vision-capable Claude model): `Read(pdf_path, pages="1-10")`. PDFs are natively handled via CC's multimodal pipeline; do NOT try external CLIs first.
2. **PDF-related MCP tool** (for non-multimodal backends — DeepSeek / GLM / Qwen / etc.): scan your current `available tools` list for any tool whose description mentions PDF / document / extract, and use that.
3. **System CLI fallback** (probe with `command -v` first, then run the first available): `pdftotext -layout <pdf> -`, `mutool draw -F txt <pdf> -`, or `python -c "from pypdf import PdfReader; print(PdfReader('<pdf>').pages[N].extract_text())"`.
4. **Tell the user what's missing — with concrete install commands for their OS** (`brew install poppler` / `apt install poppler-utils` / `dnf install poppler-utils` / `scoop install poppler`), or suggest switching to a vision-capable model. Never report a vague "can't read PDF" — name the rung that failed and the exact gap.

**Forbidden**: stopping at rung 1's failure without trying 2/3/4; summarizing from the paper's title / training data without reading (the navigate-don't-synthesize rule); batching all 20 pages into a single Read call when the user only needs §3.

`notes.md` vs `discussion.md` (state/log split): **`notes.md` is the STATE** — the current-understanding snapshot, kept as a single latest version to avoid ambiguity, agent-assisted and overwrite-regenerated (B10 step 1). **`discussion.md` is the immutable LOG** — each session appends a distilled conclusion (Phase 5). If the notes drift, they can be rebuilt from the discussion trail.

**Review folds in here (not a separate phase):** when loading a paper, if you see a `⚠` / "not verified" item in `discussion.md` (a past agent inference that was never confirmed), ask the user about it in passing.

If `notes.md` is empty or absent (a paper still in `inbox`), say so, then offer to read the PDF directly. Do not pretend the user has notes they don't.

## Phase 3 — Cross-paper / cross-vault retrieval (on demand)

Only run when the user asks for comparison, related work, or "anything similar". Use the bounded-retrieval ladder (Phase 1): prefer the focus paper's explicit edges (Tier 1), fall back to a controlled-vocab discovery slice (Tier 2). Do **not** deep-load all candidates — only after the user picks one or two does it pay to read their metadata + notes.

**Neighbor output — signpost discipline (do NOT auto-expand):**

- Emit **one signpost line**, not a fan-out.
- **Threshold = the neighbor shares ≥2 TAXONOMY keys** with the focus paper (sharing 1 broad topic is too noisy).
- **List at most 3**, ranked by overlap size, citing the *actual* shared TAXONOMY values as the "why" (never invented reasons). Beyond 3, just say "N more related".
- Deep-load stays **on-demand** — only after the user picks one.

**Cross-vault references (regression guard):** notes and discussion files may contain `[[<vault>:<paper-id>]]` wikilinks pointing at another registered vault. If the focus paper has such a link, recognise it as an explicit cross-vault binding; resolve the target via `lit vault list` (or reading `~/.config/litman/vaults.yaml` if you must) and use that vault's index exactly as in Phase 1. Surface the source vault explicitly: "From the *peptide-design* vault — `2024_Foo_Bar` …". The neighbor ladder ranks within the active vault, but a cross-vault wikilink still resolves and surfaces with its source vault named.

## Phase 4 — Project context (connect a paper to the user's own work)

The user maintains a registry mapping project name → project dev_docs directory. **One source for both the registered set and each project's path: `lit project list`** (Tier 1 read). It is a JOIN of `TAXONOMY.md`'s `projects` section and the config map, rendered as three columns — `name` / `path` / `status` — so it answers **both** needs in one read: (a) *enumeration / canonicalization* (which projects exist) and (b) *path resolution* (project name → its on-disk dev_docs directory). Do **NOT** hand-parse `lit-config.yaml`, and do **NOT** route the path through `lit config show` (it collapses the whole `projects` map into a single `{}` cell). (`lit project list` prints a Rich table; a long path in a narrow render can wrap — `lit project list --format json` is a deferred enhancement, not part of this milestone.)

**Project-name normalization (query side): deterministic canonicalization only.** Normalize the user's token by case + whitespace + separator, then **exact-match** against a registered `name` from `lit project list`. One step beyond that (abbreviation / alias / 0-match / multi-match) → fall back to **presenting the registered set** and let the user pick. **Never fuzzy-guess** a controlled value. cwd is at most a pre-checked hint (the user confirms; it never overrides an explicit reference).

Procedure when the user asks "connect this paper to my `<project>`":

1. `lit project list` → resolve the project name to its path (canonicalize per above; if unresolved, present the set).
2. From the resolved project directory, read in this order (stop when you have enough): `<project>/dev_docs/identity.md` (what is this project, current goals), `<project>/dev_docs/todo/active/*.md` (current work), `<project>/dev_docs/proposals/*.md` (what's being considered).
3. Synthesise: paper's claim/method/data ↔ project's current question/blocker/opportunity, with concrete pointers ("paper §3 shows X; the project's active todo <name> needs Y; the link is …").

**Link trigger (informational only, user-initiated).** During discussion you may **at most informationally note** "this looks related to your `<project>`" (assist, don't drive). You must **NOT proactively initiate** a binding. Only after the user **explicitly** says "bind it / add to `<project>`" do you chain to lit-library [H] to run `lit link <paper-id> --project <name>`. Trigger ownership is the user's — this is stricter than present-and-user-picks.

## Phase 4/5 — Knowledge-graph edge detection (propose only; chains to lit-library)

The three edge fields — `related` / `extends` / `contradicts` — exist to make the knowledge graph traversable, but nothing fills them automatically. When discussion implies one ("this is Pandi's follow-up" → `extends`; "its conclusion contradicts X" → `contradicts`; "these two read well together" → `related`):

- **Judge by judgment** (using the discussion + your own inference) — NOT against an enumerated "what phrasing counts" checklist.
- **PROPOSE** the edge. **Only after the user confirms** do you chain to lit-library [E] to run `lit modify <id> --add-tag extends=<other-id>` (or `related=` / `contradicts=`). **You only propose; you NEVER write the edge yourself** — edges are structured fields with cascade + cross-paper retrieval, stricter than free-form sediment, and all structured-field writes go through the CLI.
- **Name the FORWARD direction only** (`extends` / `contradicts` / `related`). The CLI auto-writes the paired reverse field on the opposite paper inside the same transaction (`extends` → `extended-by`, `contradicts` → `contradicted-by`, `related` self-paired) and does not even expose `extended-by` / `contradicted-by` as `--add-tag` targets — so **never set a reverse field by hand, never run a second command for the reverse.**
- **Do NOT** distinguish source / add a field / regulate proposal wording. Once the user nods, the user has endorsed it; provenance (agent-inferred vs user-stated) has no downstream consumer.

This is the same present-and-user-picks shape as code-clone candidates: you may over-propose, the user provides precision by rejecting. That is the design fulfilling itself, not a failure mode.

## Phase 5 — Sediment a discussion (only when asked) + the verdict ritual

### Sediment writing discipline

If a conversation produces a genuine conclusion, idea, or follow-up the user wants to keep, offer:

> "Should I append this to `papers/<id>/discussion.md`?"

After explicit confirmation, **Write** (append, never overwrite) this format:

```markdown
## YYYY-MM-DD HH:MM

**Question:** <one-line restatement of what the user asked>

<3–10 lines of the discussion's distilled conclusion + the user's questions — user's perspective first, not the full back-and-forth>

[optional] **Follow-up:** <action item / open question>
```

The ONE discipline:

- `discussion.md` records **the distilled conclusion + the user's questions** (user's perspective first).
- **The only constraint:** if you write **your own inference**, or cite a **number/section you did not verify against the PDF**, mark it (a `⚠` or a short "not verified") so next time it isn't taken as the user's settled view. This stops agent bluster from fossilizing as the user's judgment. It is one reminder, not a tag system — do **not** add `[Discussed]` / `[Agent synthesis]` tags, three `?` classes, or a separate review phase.
- **Trigger = user-explicit only.** Do not write proactively. Append, do not overwrite. If `discussion.md` doesn't exist, create it with a top-level `# Discussion log for <id>` header, then the dated section.
- **Action log (keep, do not extend):** after a `lit code add` (clone) or an unlink, append one `[Action log]` line (e.g. "Cloned <url> as codes/<name>"). This is fact, not sediment; needs no review; **records clone/unlink only** — do NOT extend it to metadata/TAXONOMY edits (those have their own `updated-at`).
- After writing, mention the file path you appended to.

### B10 — The verdict ritual (note + status + read-date + metadata self-check)

When a reading discussion reaches a **natural close** (the user signals "done" / "一般" / "no value"), treat it as a single **verdict ritual** that binds three coupled products (fire them together), then runs a fourth move:

1. **note** — regenerate `notes.md` (the overwrite-style STATE file). Even a `drop` gets a one-line "why dropped" note; reaching *any* verdict means there is something to record. *(When you rewrite `notes.md`, for each `[[X]]` you keep verify `papers/X/` exists; if not, write it `[[X]] (deleted)` — never a bare `[[X]]` for a missing paper.)*
2. **status** — the curation judgment `deep-read` / `skim` / `dropped`. **Ask which** (it is a user judgment, never auto-derived); on the user's pick **run the matching sugar INLINE** (`lit promote` / `lit skim` / `lit drop`) — an evaluation stamp lit-reading owns, **not** a chain.
3. **read-date** — in the **same inline step**, also run `lit read` to stamp `read-date` (marks the paper *finished* — the signal the resume branch / `--unread` keys off). `lit read` is likewise an evaluation stamp lit-reading runs directly (no chain). All three verdicts stamp it, because the premise here is *read-then-judge*. (The inbox-triage drop in B11 — a paper dropped by title, never read — does NOT stamp `read-date`; distinguish the two by whether the user actually read it.)
4. **metadata completeness self-check** — before closing, scan the focus paper's **already-loaded** metadata (Phase 2 put it in context — this is a context read, **NO CLI call**, no `lit show` re-fetch) for empty *curation* fields, and **offer** to fill each, routing to its existing branch. This is **propose-and-decline**: an empty field is a *legitimate* "not applicable" (metadata is schema-less by design), so the user may wave it off — it is a reminder, **never a required-fill**. The user has often already volunteered some during reading ("this extends Pandi", "tag it transformer", "it's for PepForge"), so the scan surfaces only the *remaining* gaps.

   | Empty curation field | Offer | Routes to |
   |---|---|---|
   | `projects` | "link this to a project?" | lit-library [H] `lit link --project` |
   | `topics` / `methods` / `data` | "classify it?" | lit-library [E] Flow A — propose **only** from registered values |
   | `related` / `extends` / `contradicts` | "related to anything in your library?" | the edge workflow above (propose, user confirms) |
   | `relevance-<project>` (linked but blank) | "what is it to `<project>`?" | lit-library [H] `lit modify --set relevance-<project>=…` |
   | `priority` (still default `B`) | light touch only | **inline** `lit modify --set priority=` (evaluation stamp lit-reading runs directly — not a chain) |

   `type` (defaults to `research`) is normally skipped unless the paper is obviously a review / benchmark / dataset. Each spawned write follows its own branch's tier and the propose-first discipline — **never invent a controlled value, never fill a field the user did not endorse.**

**Never auto** — the status verdict, the `read-date` stamp, and every write the self-check spawns all wait on the user's nod. The status + `read-date` writes are Tier 2 that lit-reading runs **inline**; two back-to-back `staged_write`s are fine (single-user, single-machine — non-atomic just means two history entries, not corruption). The self-check's accepted vocab/edge/link offers are the writes that **chain** to lit-library.

**Why the coupling is skill-side, not a CLI command:** one of the three products — the note — is the agent's free-form sandbox the CLI deliberately does not manage (no `lit` command generates a summary into `notes.md`), so a single command can never bind all three. **Do NOT propose a `lit read --status X` / `lit finish` fusion** — it loses composability ("read without a verdict yet", "drop-by-title without reading", "re-stamp read-date without re-judging") and still can't carry the note. The CLI keeps `read-date` ⊥ `status` as clean orthogonal primitives on purpose.

**No vault-wide floor; the self-check is the SOLE anti-drift checkpoint.** Detecting *this* paper's gaps is free (its metadata is already in context). A *vault-wide* "which papers are read-but-unclassified" sweep (`lit list --untagged`-style) was **considered and rejected**: the missing value is a curation *judgment* cheapest at read-time and unrecoverable once cold, so a post-hoc floor only converts drift into re-reading homework that never gets done — and invites the batch posture litman rejects. Contrast `lit health-check`, which is **NOT** an anti-drift tool: it repairs states that *violate* a hard rule (dangling refs, broken bidirectional pairs, torn writes), whereas an empty curation field is *legitimate* under litman's schema-less metadata. Do NOT grow health-check into a curation-gap reporter.

**Stateless re-ask (accept, do not "fix"):** the scan has no memory of "the user already declined this field" — litman's schema-less metadata leaves no marker distinguishing "judged N/A" from "not yet judged". Because `notes.md` is overwrite-regenerable, a later regeneration re-surfaces the same empty fields. This is acceptable (low frequency; a second glance costs one "no") — do **NOT** invent a "dismissed" marker or a `lit`-side flag.

**Re-opening an already-finished paper → propose `lit revisit`.** The ritual above covers a *first* verdict. When the focus paper already carries a `read-date` (or status is `deep-read` / `skim`) and the user is **re-opening** it in a later session ("let me look at A again"), the matching nudge is `lit revisit` (stamps `last-revisited`, the semantic field for "I came back to this", distinct from the technical `updated-at`). Same posture: propose, user confirms, never auto-stamp.

## B11 — Inbox triage nudge

`inbox` is the **default value of `metadata.yaml`'s `status:` field**, semantically "judgment pending" — NOT a directory / container / staging area. The lifecycle moves papers *out* of inbox (skim / deep-read / dropped); changing `status` moves no files.

Using the **existing** `INBOX_STALE_DAYS` staleness check (introduce NO new mechanism), at a natural moment offer "you have N papers in inbox, sitting X days" and walk the user through a **per-paper loop**:

1. Surface each paper's **`lit list --status inbox --format json` projection row** — title, year, type, and any existing topics / methods / projects. litman does **not** persist an abstract (the add-time abstract is dropped, never written to disk), so triage is **metadata-level, not abstract-level**.
2. The **user decides** skim / deep-read / drop → run the B10 status change inline.
3. The B10 metadata self-check (product 4) **rides along, but scoped to the projection row**, not a Phase-2 full load: triage has not loaded the focus paper's full `metadata.yaml`, so the self-check here reads only the gaps **visible in the projection row** (`projects` / `topics` / `methods` / `data` / `priority`) — still **no CLI call**, just a lighter source. The edge fields (`related` / `extends` / `contradicts`) and `relevance-<project>` are **not** in the projection, so those gaps are deferred to an actual read.

**This is NOT batch ingest** (state this explicitly): litman forbids batch *ingest* decisions; this is triage of *already-ingested* papers, each still individually human-judged.

## B12 — Vault health-check guidance

`lit health-check` (with the clone-link bidirectional checks) is not just a post-write safety net — a 300–500-paper vault needs a user-facing "check my library" scenario. At a natural moment (occasionally at session start / after a batch of operations / when the user asks "is my library still clean?"), **run `lit health-check`, translate the report's categories, and propose a fix per finding.**

**Red line (unbreakable): health-check only reports, never auto-fixes** — each fix runs only on the user's nod; never self-correct. Autonomy: running health-check is a Read (Tier 1, just do it); each proposed fix follows its own tier (e.g. unlinking a dangling clone = Tier 3, ask once).

## B13 — Mis-deletion recovery (find → confirm → restore)

When the user signals a mis-deletion ("我之前好像误删了那篇关于…的文献", "把删掉的 X 找回来", "I think I deleted that paper by mistake"), run a **find → confirm → restore** flow. lit-reading owns find + confirm (read-only navigation); the actual restore is a write, so it chains to lit-library [I].

1. **Find (read-only).** `lit trash list` enumerates the bin newest-first (paper id, deleted_at, title, entry_name). The trashed folder is fully readable on disk at `<vault>/.trash/<entry-name>/` — when the title alone is ambiguous you **MAY** open its `metadata.yaml` / `notes.md` / `discussion.md` / the PDF to match the user's description by content. **Reading trash files is allowed** (the hard rule forbids only *writing* structured files, and the deleted folder is not under `papers/`).
2. **Confirm (present-and-user-picks, NEVER auto-restore).** Surface the **single most-likely candidate** (id + title + deleted_at, plus a one-line content cue if you inspected the files) and ask "is this the one?". If several are plausible, list at most 3 (newest-first) and let the user pick. Never restore without an explicit identity confirmation — restore writes back into the live vault, and which paper to revive is a user curation decision.
3. **Restore (chain to lit-library [I]).** Only after the user confirms the identity, chain to lit-library to run the restore. The command is **`lit trash restore <id>`** — there is **no** top-level `lit restore`. Pass the confirmed paper id (or the full entry name `<id>-<UTC-timestamp>` if the same id sits in trash more than once), and whatever the user already said about re-cloning code.

## B14 — `lit open`: suggest by default, run on explicit request

The agent **suggests** `lit open <id>` by default (launching a GUI on the user's machine unprompted is intrusive — the A3 posture), but **runs it on an explicit request** ("打开这篇" / "open it for me"). Encode the autonomy split, not a blanket "never run".

**State note:** `lit open` deliberately changes **no** state — no `last-revisited` stamp, no "currently reading" flag. The continue-reading / resume scenario is handled by the Phase 1 resume branch, which keys off the recency *proxy* `max(paper.pdf mtime, updated-at)` and needs no open-triggered state. Do **NOT** add any open-triggered state write — resume does not need one.

---

## Quick reference — the commands this skill runs directly (everything else chains to lit-library)

This skill is read-heavy. What it runs **itself**:

| Command | Why this skill calls it | Tier |
|---|---|---|
| `lit list [--format json] [--topic/--author/--status/--project ...] [--unread] [--sort recent]` | locate / bounded retrieval / resume / triage / roundup | 1 (read) |
| `lit show <id>` / `lit show --paper-doi <doi>` | confirm a paper, dedup check, read metadata aloud | 1 (read) |
| `lit vault list` | enumerate registered vaults when a `[[v:id]]` cross-vault link surfaces | 1 (read) |
| `lit project list` | the canonical source for the registered project set AND each project's path | 1 (read) |
| `lit trash list` | enumerate the bin for mis-deletion recovery (B13) | 1 (read) |
| `lit health-check` | translate the report + propose per-finding fixes (B12) | 1 (read) |
| `lit read` / `lit promote` / `lit skim` / `lit drop` / `lit revisit` | the reading verdict — evaluation stamps lit-reading owns (B10) | 2 (inline) |
| `lit modify --set priority=` | the priority verdict — evaluation stamp lit-reading owns | 2 (inline) |
| `lit open <id>` | suggest by default; run only on explicit request (B14) | — |

**Chains to lit-library** (the agent does NOT run these here): `lit add` / `lit code add` / `lit code link` (ingest/clone), `lit modify --add-tag topics/methods/data=` (controlled-vocab tagging), `lit modify --add-tag extends/related/contradicts=` (edges), `lit link --project` / `lit modify --set relevance-<P>=` (project binding), `lit trash restore` (restore), `lit taxonomy merge/rename/rm` / `lit project rename/rm` (governance). When the discussion produces such a write, end the read phase and chain: "let me chain to lit-library to run `lit modify <id> --add-tag …`".

## Failure modes & how to handle them

| Situation | Right behaviour |
|---|---|
| Vault not discoverable (no `$LIT_LIBRARY`, no registry, no `lit-config.yaml` upward) | Stop. Tell the user. Do not invent paper content. |
| User refers to a paper that isn't in any vault | Say so. If they have the PDF, run the B9 PDF-as-entry chain (propose-confirm → chain to lit-library [A]/[B]). A bare DOI/URL with no local file is not an entry trigger (lit-library [B] explains the precondition). |
| `notes.md` empty, user asks "what did I think about §3?" | Be honest: "Your notes for this paper are empty — let me read §3 of the PDF instead." Then read the PDF with the `pages` parameter. |
| `INDEX.json` looks stale (id missing from index but folder exists on disk) | Cross-check by reading the metadata.yaml directly. If they disagree, surface the inconsistency and suggest `lit refresh-views` (teach-don't-do per A3). Do not silently route around it. |
| The user asks for an opinion on the paper | Distinguish honestly: what the **notes / discussion** record (read them) vs. **your own synthesis** (mark clearly as inference, not the user's settled view). Both notes and discussion are agent-written, so the line is *recorded-and-endorsed* vs *fresh inference*, not "user wrote vs Claude made". |
