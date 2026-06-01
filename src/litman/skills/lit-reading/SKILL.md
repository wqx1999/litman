---
name: lit-reading
description: "Read-side companion to the litman vault. Use when the user discusses, compares, or asks about a paper they are reading, looks for related work across their vault, or wants to connect a paper to one of their own projects. Triggers: '这篇文章...', '这篇论文里关于...', '刚才那篇...', '<paper-id> 里...', 'how does it compare to <other>', 'who else does this', 'have I read anything similar', 'connect this to my <project>', '把这个观点和...联系起来', '记下这次讨论'. Also: search my own notes/discussion ('我在 notes 里写过 X 吗', 'where did I note X', 'search my notes'); find related papers ('跟这篇相关的还有哪些', 'related work', 'what builds on this'); resume reading ('继续读', '接着上次', '上次读到哪', '我在读哪篇来着', 'continue reading', 'what was I reading'); mis-deletion recovery ('误删了那篇...', '把删掉的 X 找回来', 'restore that paper I deleted'); read-finished verdict ('读完了', '这篇一般/没价值', 'done with this paper'); inbox triage ('清一下 inbox', 'triage my inbox'); library health ('我的库还干净吗', 'is my library still clean'); reading roundup ('最近读了啥', '我这周读了哪些', '汇总我库里关于 X 的', 'what did I read recently', 'summarize my library on X'). NOT for adding/modifying papers — that's the lit-library skill. Drives only reads of the vault (INDEX.json, metadata.yaml, notes.md, discussion.md, the PDF, project dev_docs); regenerates notes.md and appends discussion.md only when the user explicitly asks."
---

# lit-reading — Vault-aware Reading Companion

Read-side companion to **lit-library**. Navigate the user's litman vault during a reading discussion so answers are grounded in their actual library, not generic web knowledge.

## How this skill differs from lit-library

| User intent | Skill |
|---|---|
| "Add this paper to the vault" / "change the topic tag on X" / "bind this repo to that paper" | **lit-library** (drives the `lit` write surface) |
| "Discuss this paper" / "find similar work" / "compare to my project" / "summarise §3 for me" / "continue reading" | **lit-reading** (this skill) |

lit-reading is read-first but **not** strictly read-only: it owns the *reading verdict* (inline evaluation stamps — see autonomy ladder), and **chains to lit-library** for any write that touches the shared vocabulary, the knowledge graph, ingest, or governance.

## Architecture you must respect (read-side invariants)

1. **You navigate, you do not synthesize from training data.** Every claim about a paper must come from reading something inside the vault (metadata.yaml / notes.md / discussion.md / paper.pdf) or from the user. If a paper is not in the vault, say so — do not fabricate a summary from the title.
2. **No state file.** There is no `lit focus` or `lit current`. The user tells you which paper is in scope, or you infer it from natural-language cues.
3. **Vault path is discovered, never hardcoded.** Resolve via `$LIT_LIBRARY`, the active vault in `~/.config/litman/vaults.yaml`, or by walking up from the user's cwd for `lit-config.yaml`. Never paste a stale absolute path from prior sessions.
4. **Multi-vault aware.** The user may have several registered vaults. If a paper id isn't in the active vault, check cross-vault wikilinks (`[[<vault>:<id>]]` syntax in notes) and `lit vault list` before giving up. *Reading* another vault is fine; **writing** is confined to the active vault.
5. **Agent-writable free-form = `notes.md` (overwrite) + `discussion.md` (append).** You may regenerate-and-replace `<vault>/papers/<id>/notes.md` (the current-understanding STATE snapshot, agent-assisted, user-read-only) and append dated sections to `<vault>/papers/<id>/discussion.md` (the immutable LOG). **Never** write `metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, or `repo-meta.yaml` directly — chain to lit-library.

If any of these would be violated, push back and propose the right alternative ("let me chain to lit-library to run `lit add`" or "I can only summarise what's actually in the notes — should I read the PDF first?").

---

# PART A — Autonomy, chaining, scope (read this first)

## A1. Autonomy ladder

Classify every action before you take it.

| Tier | Operation class | Behavior | Examples |
|---|---|---|---|
| 1 | **Read** | Just do it, don't ask | `lit list` (incl. `--title` / `--limit` / `--format json`), `lit show` (incl. `--format json` for the full field set), `lit search` (notes/discussion content), `lit related` (knowledge-graph neighbours), scan a PDF, `lit vault list`, `lit code list`, `lit trash list`, `lit project list`, `lit health-check`. All retrieval is high-autonomy and freely composable (`search` → `show --format json` → `related`). |
| 2 | **Write, reversible, single-paper** | Do it, then report | the evaluation stamps `lit read` / `lit promote` / `lit skim` / `lit drop` / `lit revisit`, and `lit modify --set priority=` |
| 3 | **Write, multi-paper / structural / remote-IO** | Ask once before acting | `lit add` (ingest), `lit code add` (git clone), `lit taxonomy add` / `lit project add`, `lit taxonomy merge`/`rename`/`rm` + `lit project rename`/`rm` (governance) |

**Execution ownership — what lit-reading runs INLINE.** The single-paper evaluation stamps that ARE the reading verdict: `lit read`, `lit promote` / `lit skim` / `lit drop`, `lit revisit`, `lit modify --set priority=`, and `lit modify --set type=`. (`type` and `priority` are both single-paper, reversible, fixed-enum verdict stamps surfaced together in the B10 self-check — same inline posture. Controlled-vocabulary tags, edges, and project links still chain to lit-library.)

**Every other Tier-2 write CHAINS to lit-library** (A2):
- `lit modify --add-tag topics/methods/data=` (controlled vocabulary)
- `lit modify --add-tag extends/related/contradicts=` (cross-paper edge)
- `lit link --project` / `lit modify --set relevance-<P>=` (project binding)

The CLI hard-rejects unregistered controlled-vocabulary values, so your job is to **not invent values**.

## A2. Chain hand-off contract

**Direction: `lit-reading → lit-library` only.** Once lit-library finishes, the result (e.g. a new id) is in context and you continue down the still-resident lit-reading SOP — no reverse switch needed.

| Slot | What to fill in |
|---|---|
| **Trigger** | the user-confirmed write event below |
| **Target skill** | `lit-library` (named explicitly) |
| **Context to pass** | paper id, PDF path, the exact intent (e.g. "add `extends=<other-id>`"), and any user instruction already given |
| **Resume point** | the named Phase / branch to return to (e.g. "resume Phase 2 with the new id"), never "go back to before" |

**What lit-reading chains for** (recurring cases — the rule is A1's "every write outside the evaluation-stamp set"):

1. Controlled-vocabulary tagging `lit modify --add-tag topics/methods/data=` → lit-library [E] (Flow A/B).
2. A cross-paper edge `lit modify --add-tag extends/related/contradicts=` → lit-library [E] (forward direction only — see B7).
3. Project binding `lit link --project` / `lit modify --set relevance-<P>=` → lit-library [H].
4. Ingest / clone / code-binding `lit add` / `lit code add` / `lit code link`, plus code unbind / retire (`lit modify --rm-tag code-clones=` or `lit code rm`) → lit-library [A]/[B]/[C].
5. Restore a trashed paper `lit trash restore` → lit-library [I].
6. TAXONOMY / project governance → lit-library [J]/[H].

## A3. Scope discipline — SOP vs maintenance, reads vs writes, active-vault confinement

**(1) SOP vs maintenance.** The Phase SOPs below cover high-frequency reading/discussion actions. Low-frequency vault-maintenance (`lit sync`, `lit vault use`, `lit config`, `lit refresh-views`) has **no SOP** — fall through to `lit <cmd> --help`.

- **Read vs write within the no-SOP set.** Pure-read no-SOP commands (`lit code list`, `lit vault info`, `lit vault list`, `lit config show`) stay **Tier 1: just run and report**.
- **"Teach, don't do" applies only to no-SOP writes / maintenance.** Run `lit <cmd> --help`, surface the command, let the user run it. Execute it yourself only when the user explicitly asks. **Never guess a flag or invent a command from memory.**

**Destructive operations by reversibility:**

- **Soft-delete `lit rm`** (default → moves `papers/<id>/` into `.trash/`, recoverable via `lit trash restore`; recovery SOP at B13). **Never initiate.** May execute on explicit, confirmed request, and must **relay `lit rm`'s cascade report verbatim** ("This paper is linked with N entries…") — never silently delete, never summarize the link count away.
- **Irreversible removal** (`lit rm --purge`, `lit trash empty`): **NEVER execute these, even on explicit request.** Surface the exact command and let the user run it.
- **`lit code rm`** stays governed by lit-library [C] (confirm before execute; clone is re-cloneable).

**(2) Write only in the active (primary) vault.** Every write — evaluation stamps, appends to `notes.md` / `discussion.md` — targets the **currently-active vault only**. Cross-vault reading is fine (resolving a `[[<vault>:<id>]]` link, listing another vault); cross-vault writing is forbidden. If the user's intent requires writing into a different vault, **tell the user to switch first** (`lit vault use <name>`) and only then operate.

---

# B9. PDF-as-entry chain — the headline workflow (chains to lit-library)

When the user drops a **raw PDF path** into the conversation and wants to discuss it, the first job is to find out whether it's already in the vault. A bare PDF has no metadata, no TAXONOMY, no notes, no project binding — chain to lit-library to ingest first, then discuss.

1. **Defensively check in-vault by title.** `lit list --title <substr>` (a distinctive word or two from the PDF's title) does the filtering file-side — do NOT pull `lit list --format json` whole and grep the title yourself. If you can read a DOI off the PDF, `lit show --paper-doi <doi>` is the exact check.
2. **If not in vault → propose-confirm:** "this isn't in your vault — want me to `lit add` it?" On the user's **yes**, chain to lit-library [A] (LLM-augmented) or [B] (DOI path); pass the PDF path + any DOI. Then **resume at lit-reading Phase 2** with the new id.
3. **A bare DOI / URL with no local file is NOT a raw-PDF entry trigger.** `lit add` ingests from a local PDF. If the user gives only a DOI/URL, do not treat it as an entry PDF — lit-library [B] will explain the precondition and ask for the file path.

---

# PART B — The reading SOP (per-operation phases)

Every trigger walks through these phases. Some collapse to one tool call; the heavier ones (cross-paper / project) are demand-only. The verdict ritual (B10) closes a reading session; resume (B15) opens one.

## Phase 1 — Locate the paper

Convert the user's reference into a canonical paper id by **reading the vault**, not by guessing.

```
Vault root  = resolve(`$LIT_LIBRARY` | active-vault-from-registry | cwd-walk)
```

### Resume branch — "继续读" (the user names nothing)

Triggers: "继续读" / "接着上次" / "上次读到哪了" / "我在读哪篇来着" / "continue reading" / "what was I reading" / "resume".

1. **Retrieve (one CLI call):** `lit list --unread --sort recent --format json`. The CLI returns rows already filtered to unread + ordered by recency. **Do NOT re-sort or re-filter.** Take the top rows.
2. **Client-side prune `dropped`:** rows carry `status`. `--unread` doesn't exclude by status, so a paper dropped-by-title (never read, `read-date` empty) can still appear. Drop those rows. **Keep `skim`** (a skimmed paper with no `read-date` may be one the user means to return to and deep-read).
3. **Present top 3–5 (present-and-user-picks):** id + title + `status`/`priority`, "most-recently-touched first". **Honesty constraint:** recency is a *proxy*, not a tracked "you were reading X" — phrase it "your most-recently-touched unfinished papers, probably where you left off", **never** "you were reading X". If the top guess is wrong, the user picks another.
4. **User picks → suggest `lit open <id>`** (suggest, don't run — see B14, unless the user says "open it"), then **resume at Phase 2** for the chosen paper.
5. **Empty result `[]`:** distinguish honestly — "nothing is marked unfinished (every paper has a `read-date`)" vs. "your library has no papers yet" (a plain `lit list` tells you which).

If a surfaced paper is `status: deep-read` yet appears here (deep-read but `read-date`-empty), nudge the B10 verdict ritual: "looks like you deep-read this but never marked it finished — want me to write the summary and stamp it read?". Resume operates **within the active vault**; cross-vault *lookup* of a named paper stays in the cue branches below.

### Locate-by-cue (the user names a paper)

**Rule:** there is a CLI flag for every recall cue — use it, never `grep`/`cat` the vault. The INDEX projection carries id / title / year / type / priority / status / topics / projects / methods / data / doi / read-date; for anything beyond it (full field set, free-form notes) there is a dedicated command (`lit show --format json`, `lit search`). Pick the command that matches the cue:

| Cue | Command |
|---|---|
| Direct id | `lit show <id>` (or `--format json` for the full field set, all authors, every edge) |
| Author ("Pandi 那篇") | `lit list --author <cue> --format json` |
| Title ("标题里带 X 的") | `lit list --title <substr> --format json` |
| Topic / method ("the GAT one") | Tier-2 discovery below (`lit list --topic X --format json`) |
| Something I wrote in notes ("我之前在 notes 里写过关于 X 的看法吗") | `lit search <query>` (searches `notes.md` + `discussion.md`) |

1. **Direct id given** → `lit show <id>` to confirm it exists (add `--format json` when you need the full metadata dict, not just the projection — see Phase 2). If missing, ask the user; do not silently substitute another paper.
2. **Author cue** ("Pandi 那篇") → `lit list --author <cue> --format json`. The `--author` filter matches file-side; the returned rows don't echo the author, but **being *in* the result is the hit**.
3. **Title cue** → `lit list --title <substr> --format json` (case-insensitive substring, comma = OR). The CLI does the filtering file-side — do NOT pull `lit list --format json` whole and grep the title yourself.
   - 1 candidate → use it; confirm to the user "I'm reading `2023_Pandi_Cell-free`, Pandi et al. 2023 — *Cell-free …*" so they can correct.
   - 2–5 → list and ask which one.
   - >5 → ask the user to narrow the cue; do not list 30 rows.
4. **Topic cue** ("the GAT one", "anything on peptide hemolysis") → Tier-2 discovery below (`lit list --topic X --format json`).
5. **Notes-content cue** ("我之前在 notes / discussion 里写过关于 X 的看法吗", "where did I note that idea") → `lit search <query>`. This is the ONLY path to your own free-form notes; it returns `{id, file, line, snippet}` per matched line. Narrow with `--in notes` / `--in discussion`. **The PDF full text is NOT searched** (that's `lit open` / the PDF ladder).

### Bounded-retrieval ladder (in-vault; reuse in Phase 3)

**Never `cat INDEX.json` whole** — use bounded retrieval:

| Tier | Trigger | How | Reads INDEX into context? |
|---|---|---|---|
| 0 focus paper | already in Phase 2 | the loaded focus metadata + notes + discussion | no |
| 1 neighbours (preferred) | "跟这篇相关的还有哪些", "related to this", "what does it extend", "what builds on / disputes this", "anything similar to this one" | `lit related <id>` — one command returns explicit edges first (`related` / `extends` / `extended-by` / `contradicts` / `contradicted-by`, both inbound + outbound), then shared-topic/method neighbours ranked by overlap. Each row carries a `via` annotation (`edge:<field>` or `taxonomy:` + the shared keys) so you can read *why* / how strong. Narrow with `--by edges` / `--by taxonomy`; tighten taxonomy noise with `--min-shared 2`. | no |
| 2 bounded discovery (fallback) | "what's in my library about X" (no focus paper) | `lit list --topic X --format json` (CLI filters file-side, returns only matching rows) | file read by CLI, NOT into context |
| never | — | `cat INDEX.json` whole; pulling all rows to compute shared keys by hand | ❌ |

`lit related <id>` replaces the old hand-walk of the focus paper's edge fields + manual shared-key counting — do NOT reconstruct the graph yourself by pulling `lit list --format json` and intersecting topics. `lit list --format json` is the **Tier-2 primary path** (no focus paper). `data` is in the projection — no `lit list --data X` workaround needed.

**Composable read-only chain (high autonomy — self-invoke, don't ask):** all of `lit search`, `lit show --format json`, `lit related`, and `lit list` filters are read-only retrieval. Chain them freely without confirmation: `lit search <term>` to find the paper id that mentions a thing → `lit show <id> --format json` to load its full record → `lit related <id>` to fan out to neighbours. Only a *write* (modify / link / append) needs the user gate.

**Cross-vault:** this ladder operates within the active vault. If a paper id isn't in the active vault, cross-vault lookup goes through `[[<vault>:<id>]]` wikilinks + `lit vault list`.

### Retrospective / summary queries (route here, never to a vault grep)

- **"What did I read recently"** → `lit list --sort recent --format json`. **Summarize the returned rows, do NOT re-sort.** **Honesty constraint:** recency is a *proxy* for engagement — phrase it "your most-recently-touched papers, probably what you read lately", never a precise "you read these this week". Use `--sort recent` for fuzzy "what did I touch lately" queries.
- **"What did I read / add since a date"** → for a precise date lower-bound, `lit list --read-since YYYY-MM-DD --format json` (papers I read on/after that date) or `lit list --added-since YYYY-MM-DD --format json` (papers I added to the vault on/after it). `--read-since` reads `read-date`, `--added-since` reads `created-at` — they never cross.
- **"Summarize my library on X"** → Tier 2 (`lit list --topic` / `--method` / `--project X --format json`).
- **"What did I write / note about X"** → `lit search X` (notes + discussion). This is the dedicated retrieval path for your own free-form prose; never `grep papers/*/notes.md` by hand.

All stay read-only — never `grep` the vault or `cat` INDEX.json; route notes/discussion content queries through `lit search` and metadata queries through `lit list` / `lit show`; no per-paper load unless the user drills into one.

## Phase 2 — Load paper context (lazy)

On **entering a reading session**, read `TAXONOMY.md` once into context. Flow A (lit-library [E]) needs it to enumerate registered values; Flow B needs it to validate. After the user runs `lit taxonomy {add,rename,merge,rm}` (or `lit project add`) during the session, **re-read `TAXONOMY.md`**.

Then, once you have an id, load just enough paper context. Read in this order, stopping when you have enough:

| Tier | File | When to read |
|---|---|---|
| **Always** | `<vault>/papers/<id>/metadata.yaml` | Identity, taxonomy, refs |
| Usually | `<vault>/papers/<id>/notes.md` | The current-understanding snapshot (overwrite-style, regenerated on explicit request) |
| On drill-down | `<vault>/papers/<id>/discussion.md` | Append+timestamp discussion trail (LOG); pull only the relevant dated section when the user drills in, not by default |
| Only when needed | `<vault>/papers/<id>/paper.pdf` | Specific sections / figures / numbers — walk the PDF reading ladder below, use the `pages` parameter to fetch §3 not the whole 40-page PDF |

**PDF reading ladder — a *must-achieve* goal.** Walk the rungs until one works; **never stop at rung 1's failure**:

1. **Claude Code `Read` tool** (default): `Read(pdf_path, pages="1-10")`. PDFs are natively handled via CC's multimodal pipeline.
2. **PDF-related MCP tool** (for non-multimodal backends): scan your `available tools` for any whose description mentions PDF / document / extract.
3. **`lit pdf-text` — deterministic fallback, no model / network / system tool**: `lit pdf-text <pdf> --pages 1-10` (omit `--pages` for the whole doc). litman ships pypdf as a hard dependency, so this works wherever `lit` runs — it does NOT need poppler / pdftoppm. Exit code 3 means "no extractable text layer" (scanned / image-only PDF): go back up to a multimodal reader or OCR, don't retry here.
4. **Only if every rung above failed** (no multimodal read, no PDF MCP, and `lit pdf-text` returned no text): name the rung that failed and the exact gap, then **surface** OS-appropriate install commands for the user to run (`brew install poppler` / `apt install poppler-utils` / `dnf install poppler-utils` / `scoop install poppler`) or suggest a vision-capable model. **Show these commands — never execute an install yourself.**

**Forbidden**: stopping at rung 1's failure without trying 2/3/4; summarizing from the paper's title / training data without reading; batching all 20 pages into a single Read call when the user only needs §3.

**Text-only backend asked about a figure / table image ("what does Fig./Table N show?").** When the active model has no native vision (rung 1 unavailable) AND no vision-capable PDF MCP (rung 2), `lit pdf-text` is the only reader and it returns the *text layer only* — it cannot see images. Do this, in order: (1) pull the figure/table **caption** and the **body sentences that reference it** via `lit pdf-text`, and answer strictly from that text; (2) state plainly that you **cannot see the figure/table image itself**; (3) tell the user that reading the actual visual (curve shapes, bar heights, numbers inside an image-table) needs a multimodal model or an explicitly attached vision MCP. **Never infer or fabricate the visual content from the caption or surrounding prose** — caption text is not the figure. This is the highest-risk hallucination point in the whole skill.

`notes.md` = STATE (single latest snapshot, overwrite-regenerated at B10 step 1). `discussion.md` = LOG (each session appends a distilled conclusion at Phase 5). If notes drift, they can be rebuilt from the discussion trail.

When loading a paper, if you see a `⚠` / "not verified" item in `discussion.md` (a past agent inference never confirmed), ask the user about it in passing.

If `notes.md` is empty or absent (a paper still in `inbox`), say so, then offer to read the PDF directly. Do not pretend the user has notes they don't.

## Phase 3 — Cross-paper / cross-vault retrieval (on demand)

Only run when the user asks for comparison, related work, or "anything similar". With a focus paper in hand, **`lit related <id>` is the one command for this** (Tier 1 of the Phase-1 ladder): it returns explicit edges first, then shared-topic/method neighbours ranked by overlap, each tagged with a `via` annotation. Fall back to a controlled-vocab discovery slice (`lit list --topic`, Tier 2) only when there is no focus paper. Do **not** deep-load all candidates — only after the user picks one or two does it pay to read their metadata + notes.

**Neighbor output — signpost discipline (do NOT auto-expand):**

- Run `lit related <id>` and read its `via` field; **do not** recompute shared keys by hand.
- Emit **one signpost line**, not a fan-out.
- **Filter taxonomy noise with `--min-shared 2`** when the user wants only strong neighbours (a single shared generic topic is weak). Explicit-edge neighbours are always strong regardless of shared count.
- **List at most 3**, in the command's returned order (edges first, then overlap-ranked), citing the *actual* `via` reason as the "why" (the `edge:<field>` label or the shared TAXONOMY values — never invented reasons). Beyond 3, just say "N more related".
- Deep-load stays **on-demand** — only after the user picks one.

**Cross-vault references:** notes and discussion files may contain `[[<vault>:<paper-id>]]` wikilinks pointing at another registered vault. If the focus paper has such a link, resolve the target via `lit vault list` and use that vault's index exactly as in Phase 1. Surface the source vault explicitly: "From the *peptide-design* vault — `2024_Foo_Bar` …".

## Phase 4 — Project context (connect a paper to the user's own work)

**One source for both the registered set and each project's path: `lit project list`** (Tier 1 read). Three columns — `name` / `path` / `status` — answer both "which projects exist" and "project name → on-disk dev_docs directory". Do **NOT** hand-parse `lit-config.yaml`, do **NOT** route the path through `lit config show`.

**Project-name normalization (query side): deterministic canonicalization only.** Normalize the user's token by case + whitespace + separator, then **exact-match** against a registered `name`. One step beyond (abbreviation / alias / 0-match / multi-match) → fall back to **presenting the registered set** and let the user pick. **Never fuzzy-guess.** cwd is at most a pre-checked hint; the user confirms; never overrides an explicit reference.

Procedure when the user asks "connect this paper to my `<project>`":

1. `lit project list` → resolve the project name to its path (canonicalize per above; if unresolved, present the set).
2. From the resolved project directory, read in this order (stop when you have enough): `<project>/dev_docs/identity.md` (what is this project, current goals), `<project>/dev_docs/todo/active/*.md` (current work), `<project>/dev_docs/proposals/*.md` (what's being considered).
3. Synthesise: paper's claim/method/data ↔ project's current question/blocker/opportunity, with concrete pointers ("paper §3 shows X; the project's active todo <name> needs Y; the link is …").

**Link trigger (informational only, user-initiated).** During discussion you may **at most informationally note** "this looks related to your `<project>`". You must **NOT proactively initiate** a binding. Only after the user **explicitly** says "bind it / add to `<project>`" do you chain to lit-library [H] to run `lit link <paper-id> --project <name>`.

## Phase 4/5 — Knowledge-graph edge detection (propose only; chains to lit-library)

The three edge fields — `related` / `extends` / `contradicts` — exist to make the knowledge graph traversable. When discussion implies one ("this is Pandi's follow-up" → `extends`; "its conclusion contradicts X" → `contradicts`; "these two read well together" → `related`):

- **Judge by judgment** — NOT against an enumerated "what phrasing counts" checklist.
- **PROPOSE** the edge. **Only after the user confirms** do you chain to lit-library [E] to run `lit modify <id> --add-tag extends=<other-id>` (or `related=` / `contradicts=`). **You only propose; never write the edge yourself.**
- **Name the FORWARD direction only** (`extends` / `contradicts` / `related`). The CLI auto-writes the paired reverse field on the opposite paper. **Never set a reverse field by hand, never run a second command for the reverse.**
- **Do NOT** distinguish source / add a field / regulate proposal wording. Once the user nods, the user has endorsed it.

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
- **The only constraint:** if you write **your own inference**, or cite a **number/section you did not verify against the PDF**, mark it (a `⚠` or short "not verified") so next time it isn't taken as the user's settled view. One reminder, not a tag system — do **not** add `[Discussed]` / `[Agent synthesis]` tags, three `?` classes, or a separate review phase.
- **Trigger = user-explicit only.** Do not write proactively. Append, do not overwrite. If `discussion.md` doesn't exist, create it with a top-level `# Discussion log for <id>` header, then the dated section.
- **Action log (keep, do not extend):** after a `lit code add` (clone) or an unlink, append one `[Action log]` line (e.g. "Cloned <url> as codes/<name>"). Records clone/unlink only — do NOT extend it to metadata/TAXONOMY edits.
- After writing, mention the file path you appended to.

### B10 — The verdict ritual (note + status + read-date + metadata self-check)

When a reading discussion reaches a **natural close** (the user signals "done" / "一般" / "no value"), treat it as a single **verdict ritual** that binds three coupled products (fire them together), then runs a fourth move:

1. **note** — regenerate `notes.md` (the overwrite-style STATE file). Even a `drop` gets a one-line "why dropped" note. *(When you rewrite `notes.md`, for each `[[X]]` you keep verify `papers/X/` exists; if not, write it `[[X]] (deleted)` — never a bare `[[X]]` for a missing paper.)*
2. **status** — the curation judgment `deep-read` / `skim` / `dropped`. **Ask which** (it is a user judgment, never auto-derived); on the user's pick **run the matching sugar INLINE** (`lit promote` / `lit skim` / `lit drop`).
3. **read-date** — in the **same inline step**, also run `lit read` to stamp `read-date` (marks the paper *finished* — the signal the resume branch / `--unread` keys off). The inbox-triage drop in B11 (a paper dropped by title, never read) does NOT stamp `read-date`; distinguish the two by whether the user actually read it.
4. **metadata completeness self-check** — before closing, scan the focus paper's **already-loaded** metadata (Phase 2 put it in context — **NO CLI call**, no `lit show` re-fetch) for empty *curation* fields, and **offer** to fill each, routing to its existing branch. This is **propose-and-decline**: an empty field is a legitimate "not applicable" — never a required-fill. The scan surfaces only the *remaining* gaps after the user has volunteered some during reading.

   | Empty curation field | Pre-fill posture | Routes to |
   |---|---|---|
   | `projects` | offer 1-2 plausible registered projects ("link to PepForge? PepCodec?") with a one-line why each | lit-library [H] `lit link --project` |
   | `topics` / `methods` / `data` | offer top 3-5 plausible **registered** values with a one-line why each ("topics: `amp` — the focus throughout §2; `cell-free-bio` — Pandi's substrate; `deep-learning` — the discriminator model"); never invent unregistered values | lit-library [E] Flow A — propose **only** from registered values |
   | `related` / `extends` / `contradicts` | offer specific paper id candidates from the vault with a one-line why each ("extends `[[2022_X_AMPGen]]` — same CFB + AMP combo") | the edge workflow above (propose, user confirms) |
   | `relevance-<project>` (linked but blank) | offer a one-sentence draft for the user to accept/refine ("relevance-pepforge: 'cell-free 方向的 baseline 对照'") | lit-library [H] `lit modify --set relevance-<project>=…` |
   | `priority` | offer all 3 values with the agent's lean ("A/B/C — I lean B: method is new but the dataset is borrowed") | **inline** `lit modify --set priority=` |
   | `type` | offer top 2-3 plausible enum values with a one-line why each ("`review` — surveys 80+ AMP models, no new experiments; `research` — proposes a new CFB pipeline; I lean review") | **inline** `lit modify --set type=` (same posture as priority) |

   Each spawned write follows its own branch's tier and the propose-first discipline — **never invent a controlled value, never fill a field the user did not endorse.**

   **Three ground rules for every self-check propose:**

   1. **Batch all gaps into one turn.** When the scan finds N empty curation fields, list them all in one message, group them sensibly (e.g. "before we close: priority? type? topics? — feel free to answer them together"), and let the user reply in one go. Do **NOT** serialize ("priority?" → wait → "type?" → wait …). Acceptable: one message presenting all gaps; user answers in one reply; you commit them in back-to-back writes.

   2. **Candidate list, never blank prompt.** For every gap, present **2-5 candidate values with a one-line rationale per candidate**, plus the agent's own lean ("I lean X because …"). The user picks one, edits one, writes their own, or says "skip". Forbidden patterns:
      - "What's the priority?" → blank prompt, forces the user to recall the enum
      - "Is this a review?" → yes/no on a single guess, no escape if the guess is wrong
      The rationale must come from the discussion / abstract / notes already in context — never invent a justification from training-set memory of the paper.

   3. **`status=dropped` short-circuits the self-check.** When the verdict is `drop`, run **only**: (a) the one-line drop reason in `notes.md`, (b) the `lit drop` stamp, (c) the `lit read` stamp. Skip every other self-check row (no `topics` / `methods` / `projects` / `priority` / `type` / edges / `relevance-` prompts). Exception: if the user volunteers a priority during reading ("this is firmly C"), accept and commit; but do not *propose* it.

**Never auto** — the status verdict, `read-date` stamp, and every write the self-check spawns all wait on the user's nod. The status + `read-date` writes are Tier 2 lit-reading runs **inline**. The self-check's accepted vocab/edge/link offers are the writes that **chain** to lit-library.

**Do NOT grow `lit health-check` into a curation-gap reporter.** It checks hard-rule violations (dangling refs, broken bidirectional pairs, torn writes), not empty curation fields (which are legitimate under schema-less metadata).

**Stateless re-ask:** the scan has no memory of "the user already declined this field". A later `notes.md` regeneration re-surfaces the same empty fields. This is acceptable — do **NOT** invent a "dismissed" marker.

**Re-opening an already-finished paper → propose `lit revisit`.** When the focus paper already carries a `read-date` (or status is `deep-read` / `skim`) and the user is **re-opening** it in a later session ("let me look at A again"), the matching nudge is `lit revisit` (stamps `last-revisited`). Same posture: propose, user confirms, never auto-stamp.

## B11 — Inbox triage nudge

`inbox` is the **default value of `metadata.yaml`'s `status:` field**, semantically "judgment pending" — NOT a directory / container / staging area. Changing `status` moves no files.

At a natural moment (using the existing staleness check, introduce no new mechanism), offer "you have N papers in inbox, sitting X days" and walk a **per-paper loop**:

1. Surface each paper's **`lit list --status inbox --format json` projection row** — title, year, type, and any existing topics / methods / projects. Triage is metadata-level (litman does not persist an abstract).
2. The **user decides** skim / deep-read / drop → run the B10 status change inline.
3. The B10 metadata self-check (product 4) **rides along, scoped to the projection row** (not a Phase-2 full load): the self-check here reads only the gaps **visible in the projection row** (`projects` / `topics` / `methods` / `data` / `priority` / `type`) — still **no CLI call**. The edge fields (`related` / `extends` / `contradicts`) and `relevance-<project>` are **not** in the projection, so those gaps are deferred to an actual read.

**This is NOT batch ingest** — litman forbids batch *ingest* decisions; this is triage of *already-ingested* papers, each still individually human-judged.

## B12 — Vault health-check guidance

At a natural moment (occasionally at session start / after a batch of operations / when the user asks "is my library still clean?"), **run `lit health-check`, translate the report's categories, and propose a fix per finding.**

**Red line: health-check only reports, never auto-fixes** — each fix runs only on the user's nod; never self-correct. Running health-check is Tier 1 (just do it); each proposed fix follows its own tier (e.g. unlinking a dangling clone = Tier 3, ask once).

**Staleness nudges.** `lit` may append a dim `tip:` line after a command. Relay it and offer to run the named command — never auto-run. Two variants: (1) `tip: no lit health-check in 14+ days...` → offer `lit health-check`; (2) `tip: no lit sync push in 7+ days...` → offer `lit sync push` (backs the vault up to the configured remote; appears only when a remote is configured).

## B13 — Mis-deletion recovery (find → confirm → restore)

When the user signals a mis-deletion ("我之前好像误删了那篇关于…的文献", "把删掉的 X 找回来", "I think I deleted that paper by mistake"), run a **find → confirm → restore** flow. lit-reading owns find + confirm; the actual restore chains to lit-library [I].

1. **Find (read-only).** `lit trash list` enumerates the bin newest-first (paper id, deleted_at, title, entry_name). The trashed folder is fully readable on disk at `<vault>/.trash/<entry-name>/` — when the title alone is ambiguous you **MAY** open its `metadata.yaml` / `notes.md` / `discussion.md` / the PDF to match the user's description by content.
2. **Confirm (present-and-user-picks, NEVER auto-restore).** Surface the **single most-likely candidate** (id + title + deleted_at, plus a one-line content cue if you inspected the files) and ask "is this the one?". If several are plausible, list at most 3 (newest-first) and let the user pick. Never restore without an explicit identity confirmation.
3. **Restore (chain to lit-library [I]).** Only after the user confirms the identity, chain to lit-library to run `lit trash restore <id>` (there is **no** top-level `lit restore`). Pass the confirmed paper id (or the full entry name `<id>-<UTC-timestamp>` if the same id sits in trash more than once), and whatever the user already said about re-cloning code.

## B14 — `lit open`: suggest by default, run on explicit request

The agent **suggests** `lit open <id>` by default, but **runs it on an explicit request** ("打开这篇" / "open it for me").

`lit open` changes **no** state — no `last-revisited` stamp, no "currently reading" flag. Continue-reading / resume is handled by Phase 1 resume branch (which keys off file recency, not an open flag).

---

## Quick reference — the commands this skill runs directly (everything else chains to lit-library)

| Command | Why this skill calls it | Tier |
|---|---|---|
| `lit list [--format json] [--topic/--author/--title/--status/--project ...] [--unread] [--sort recent] [--limit N]` | locate / bounded retrieval / resume / triage / roundup; `--title` = title-substring cue, `--limit` = top-N | 1 (read) |
| `lit show <id> [--format json]` / `lit show --paper-doi <doi>` | confirm a paper, dedup check, read metadata aloud; `--format json` returns the FULL field set (all authors, every edge), beyond the INDEX projection | 1 (read) |
| `lit search <query> [--in notes,discussion]` | the ONLY path to your own free-form notes / discussion ("我在 notes 里写过 X 吗"); returns `{id,file,line,snippet}` per matched line | 1 (read) |
| `lit related <id> [--by edges|taxonomy] [--min-shared N]` | knowledge-graph neighbours ("跟这篇相关的还有哪些"): explicit edges first, then shared-topic/method, each with a `via` reason | 1 (read) |
| `lit vault list` | enumerate registered vaults when a `[[v:id]]` cross-vault link surfaces | 1 (read) |
| `lit project list` | canonical source for the registered project set AND each project's path | 1 (read) |
| `lit trash list` | enumerate the bin for mis-deletion recovery (B13) | 1 (read) |
| `lit health-check` | translate the report + propose per-finding fixes (B12) | 1 (read) |
| `lit read` / `lit promote` / `lit skim` / `lit drop` / `lit revisit` | the reading verdict — evaluation stamps lit-reading owns (B10) | 2 (inline) |
| `lit modify --set priority=` / `lit modify --set type=` | the priority / type verdicts — fixed-enum evaluation stamps lit-reading owns (B10) | 2 (inline) |
| `lit open <id>` | suggest by default; run only on explicit request (B14) | — |

**Chains to lit-library** (do NOT run here): `lit add` / `lit code add` / `lit code link` (ingest/clone), `lit modify --add-tag topics/methods/data=` (controlled-vocab tagging), `lit modify --add-tag extends/related/contradicts=` (edges), `lit link --project` / `lit modify --set relevance-<P>=` (project binding), `lit trash restore` (restore), `lit taxonomy merge/rename/rm` / `lit project rename/rm` (governance). When the discussion produces such a write, end the read phase and chain: "let me chain to lit-library to run `lit modify <id> --add-tag …`".

## Failure modes & how to handle them

| Situation | Right behaviour |
|---|---|
| Vault not discoverable (no `$LIT_LIBRARY`, no registry, no `lit-config.yaml` upward) | Stop. Tell the user. Do not invent paper content. |
| User refers to a paper that isn't in any vault | Say so. If they have the PDF, run the B9 PDF-as-entry chain (propose-confirm → chain to lit-library [A]/[B]). A bare DOI/URL with no local file is not an entry trigger. |
| `notes.md` empty, user asks "what did I think about §3?" | Be honest: "Your notes for this paper are empty — let me read §3 of the PDF instead." Then read the PDF with the `pages` parameter. |
| `INDEX.json` looks stale (id missing from index but folder exists on disk) | Cross-check by reading the metadata.yaml directly. If they disagree, surface the inconsistency and suggest `lit refresh-views` (teach-don't-do per A3). Do not silently route around it. |
| The user asks for an opinion on the paper | Distinguish honestly: what the **notes / discussion** record (read them) vs. **your own synthesis** (mark clearly as inference, not the user's settled view). Both notes and discussion are agent-written, so the line is *recorded-and-endorsed* vs *fresh inference*, not "user wrote vs Claude made". |
