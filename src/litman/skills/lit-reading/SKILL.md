---
name: lit-reading
description: "Read-side companion to the litman vault. Use when the user discusses, compares, or asks about a paper they are reading or its SI (supplementary / supporting information, 'Table S3', 'Figure S2', '补充材料'), looks for related work across their vault, or wants to connect a paper to one of their own projects. Triggers: '这篇文章...', '这篇论文里关于...', '刚才那篇...', '<paper-id> 里...', 'how does it compare to <other>', 'who else does this', 'have I read anything similar', 'connect this to my <project>', '把这个观点和...联系起来', '记下这次讨论'. Also: search my own notes/discussion ('我在 notes 里写过 X 吗', 'where did I note X', 'search my notes'); find related papers ('跟这篇相关的还有哪些', 'related work', 'what builds on this'); resume reading ('继续读', '接着上次', '上次读到哪', '我在读哪篇来着', 'continue reading', 'what was I reading'); revisit a finished paper ('又把 X 翻出来想了想', '重新看了下 X', 're-opened X', 'looking at X again'); mis-deletion recovery ('误删了那篇...', '把删掉的 X 找回来', 'restore that paper I deleted'); read-finished verdict ('读完了', '这篇一般/没价值', 'done with this paper'); inbox triage ('清一下 inbox', 'triage my inbox'); library health ('我的库还干净吗', 'is my library still clean'); reading roundup ('最近读了啥', '我这周读了哪些', '汇总我库里关于 X 的', 'what did I read recently', 'summarize my library on X'). NOT for adding/modifying papers — that's the lit-library skill. Drives reads of the vault (INDEX.json, metadata.yaml, notes.md, discussion.md, the PDF, supplementary/, project dev_docs) and `lit add --si`; regenerates notes.md and appends discussion.md only when the user explicitly asks."
---

# lit-reading — Vault-aware Reading Companion

Read-side companion to **lit-library**. Navigate the user's litman vault during a reading discussion so answers are grounded in their actual library, not generic web knowledge.

## How this skill differs from lit-library

| User intent | Skill |
|---|---|
| "Add this paper to the vault" / "change the topic tag on X" / "bind this repo to that paper" | **lit-library** (drives the `lit` write surface) |
| "Discuss this paper" / "find similar work" / "compare to my project" / "summarise §3 for me" / "continue reading" | **lit-reading** (this skill) |

lit-reading is read-first but **not** strictly read-only: it owns the *reading verdict* (inline evaluation stamps — see autonomy ladder), and **chains to lit-library** for any write that touches the shared vocabulary, the knowledge graph, ingest, or governance. Asking for the SI of the paper being read (`lit add --si`, B15) stays here.

## Architecture you must respect (read-side invariants)

1. **You navigate, you do not synthesize from training data.** Every claim about a paper must come from reading something inside the vault (metadata.yaml / notes.md / discussion.md / paper.pdf / the SI files in `supplementary/`) or from the user. If a paper is not in the vault, say so — do not fabricate a summary from the title.
2. **No state file.** There is no `lit focus` or `lit current`. The user tells you which paper is in scope, or you infer it from natural-language cues. `lit open` / `lit show` with no argument fall back to the most recently engaged paper — that is a keystroke saver for a human at a terminal, **not a scope oracle for you**. Never run them bare to work out which paper the user means; ask, or use the Phase 1 resume branch.
3. **Vault path is discovered, never hardcoded.** Resolve via `$LIT_LIBRARY`, the active registered vault (`lit vault list`), or by walking up from the user's cwd for `lit-config.yaml`. Never paste a stale absolute path from prior sessions.
4. **Multi-vault aware.** The user may have several registered vaults. If a paper id isn't in the active vault, check cross-vault wikilinks (`[[<vault>:<id>]]` syntax in notes) and `lit vault list` before giving up. *Reading* another vault is fine; **writing** is confined to the active vault.
5. **Agent-writable free-form = `notes.md` (overwrite) + `discussion.md` (append).** You may regenerate-and-replace `<vault>/papers/<id>/notes.md` (the current-understanding STATE snapshot, agent-assisted, user-read-only) and append dated sections to `<vault>/papers/<id>/discussion.md` (the immutable LOG). Both are scaffolded by `lit add` with an HTML-comment line stating their format — read it before you write, and never strip it. The one edit inside an old discussion.md line: when `lit health-check` flags an SI citation there (`supplementary_cited`, B12) and the user says to fix it, change only that citation — the `si-NN…` token and the original name in brackets right after it: correct either, add the missing name, or drop the citation, as the hint says — nothing else in the line, no other line. **Never** write `metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, or `repo-meta.yaml` directly — chain to lit-library. **Never** write, rename, copy into or delete anything in `<vault>/papers/<id>/supplementary/` — SI files come in only through `lit add --si` (B15); never download SI yourself, and a file litman refused stays refused.
6. **Run each `lit` command as a shell call of its own.** The command rule litman installs for your agent allows a command that *is* `lit …` and nothing else, so `which lit && lit …`, `cd <dir> && lit …`, `lit … ; echo $?`, `lit … | head` and redirections (`2>&1`, `> file`) are refused. Read the exit code and the output from the tool result, and filter the output yourself.

If any of these would be violated, push back and propose the right alternative ("let me chain to lit-library to run `lit add`" or "I can only summarise what's actually in the notes — should I read the PDF first?").

---

# PART A — Autonomy, chaining, scope (read this first)

## A1. Autonomy ladder

Classify every action before you take it.

| Tier | Operation class | Behavior | Examples |
|---|---|---|---|
| 1 | **Read** | Just do it, don't ask | `lit list` (incl. `--title` / `--limit` / `--format json`), `lit show` (incl. `--format json` for the full field set), `lit search` (notes/discussion content), `lit related` (knowledge-graph neighbours), `lit cite` (paste-ready ACS citation), scan a PDF, `lit vault list`, `lit code list`, `lit trash list`, `lit project list`, `lit taxonomy list` (all five take `--format json`), `lit health-check`. All retrieval is high-autonomy and freely composable (`search` → `show --format json` → `related`). |
| 2 | **Write, reversible, single-paper** | Do it, then report | the evaluation stamps `lit read` / `lit promote` / `lit skim` / `lit drop` / `lit revisit`, and `lit modify --set priority-<P>=`; `lit add --si <id> [FILE...]` for the SI of the paper being read (B15 — say first that the SI window is coming, or that the user's files will be moved in) |
| 3 | **Write, multi-paper / structural / remote-IO** | Ask once before acting | `lit add <pdf>` (a new paper), `lit code add` (git clone), `lit taxonomy add` / `lit project add`, `lit taxonomy merge`/`rename`/`rm` + `lit project rename`/`rm` (governance) |

**Execution ownership — what lit-reading runs INLINE.** The single-paper evaluation stamps that ARE the reading verdict: `lit read`, `lit promote` / `lit skim` / `lit drop`, `lit revisit`, `lit modify --set priority-<P>=`, and `lit modify --set type=`. (`type` and the per-project grade are both single-paper, reversible, A/B/C-or-enum verdict stamps surfaced together in the B10 self-check — same inline posture. Controlled-vocabulary tags, edges, and project links still chain to lit-library.) Also inline: `lit add --si` — asking for, or adding, the SI of the paper being read (B15).

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

**What lit-reading chains for** (recurring cases — the rule: every write outside A1's inline set):

1. Controlled-vocabulary tagging `lit modify --add-tag topics/methods/data=` → lit-library [E] (Flow A/B).
2. A cross-paper edge `lit modify --add-tag extends/related/contradicts=` → lit-library [E] (forward direction only — see Phase 4/5 edge detection).
3. Project binding `lit link --project` / `lit modify --set relevance-<P>=` → lit-library [H] (pass along a grade the user already gave, so the link runs with `--priority` in one step).
4. Ingest / clone / code-binding `lit add <pdf>` (a new paper) / `lit code add` / `lit code link`, plus code unbind / retire (`lit code unlink` or `lit code rm`) → lit-library [A]/[B]/[C].
5. Deleting an SI file `lit rm <id> --si si-NN` → lit-library [L].
6. Restore a trashed paper `lit trash restore` → lit-library [I].
7. TAXONOMY / project governance → lit-library [J]/[H].

## A3. Scope discipline — SOP vs maintenance, reads vs writes, active-vault confinement

**(1) SOP vs maintenance.** The Phase SOPs below cover high-frequency reading/discussion actions. Low-frequency vault-maintenance (`lit sync`, `lit vault use`, `lit config`, `lit refresh-views`) has **no SOP** — fall through to `lit <cmd> --help`.

- **Read vs write within the no-SOP set.** Pure-read no-SOP commands (`lit code list`, `lit vault info`, `lit vault list`, `lit config show`) stay **Tier 1: just run and report**.
- **"Teach, don't do" applies only to no-SOP writes / maintenance.** Run `lit <cmd> --help`, surface the command, let the user run it. Execute it yourself only when the user explicitly asks. **Never guess a flag or invent a command from memory.**

**Destructive operations by reversibility:**

- **Soft-delete `lit rm`** (default → moves `papers/<id>/` into `.trash/`, recoverable via `lit trash restore`; recovery SOP at B13). **Never initiate.** May execute on explicit, confirmed request — via the agent path: the CLI's own `y/N` never reaches you (a non-tty run without `--yes` aborts with "pass --yes to confirm"; with `--yes` the warning block is skipped entirely), so first run `lit rm <id> --dry-run` (read-only) and **relay its impact set verbatim** (the paper + every reference that would be cleared / repo unbound / orphaned) — never silently delete, never summarize the link count away — then, on the user's go, run `lit rm <id> --yes`.
- **Irreversible removal** (`lit rm --purge`, `lit trash empty`): **NEVER execute these, even on explicit request.** Surface the exact command and let the user run it.
- **`lit rm <id> --si si-NN`** deletes an SI file for good — there is no trash for SI; the only way back is adding the file again (`lit add --si <id>`). **Never initiate.** On an explicit request, chain to lit-library [L]: dry-run → relay it and tell the user the file goes for good (on this path the CLI's own warning never reaches them) → `--yes` on the user's go.
- **`lit code rm`** stays governed by lit-library [C] (confirm before execute; clone is re-cloneable).

**(2) Write only in the active (primary) vault.** Every write — evaluation stamps, appends to `notes.md` / `discussion.md` — targets the **currently-active vault only**. Cross-vault reading is fine (resolving a `[[<vault>:<id>]]` link, listing another vault); cross-vault writing is forbidden. If the user's intent requires writing into a different vault, **tell the user to switch first** (`lit vault use <name>`) and only then operate.

---

# B9. PDF-as-entry chain — the headline workflow (chains to lit-library)

When the user drops a **raw PDF path** into the conversation and wants to discuss it, the first job is to find out whether it's already in the vault. A bare PDF has no metadata, no TAXONOMY, no notes, no project binding — chain to lit-library to ingest first, then discuss.

1. **Defensively check in-vault by title.** `lit list --title <substr>` (a distinctive word or two from the PDF's title) does the filtering file-side — do NOT pull `lit list --format json` whole and grep the title yourself. If you can read a DOI off the PDF, `lit show --paper-doi <doi>` is the exact check.
2. **If not in vault → propose-confirm:** "this isn't in your vault — want me to `lit add` it?" On the user's **yes**, chain to lit-library [A] (LLM-augmented) or [B] (DOI path); pass the PDF path + any DOI. Then **resume at lit-reading Phase 2** with the new id.
3. **A bare DOI / URL with no local file is NOT a raw-PDF entry trigger.** `lit add` ingests from a local PDF. If the user gives only a DOI/URL, do not treat it as an entry PDF — lit-library [B] will explain the precondition and ask for the file path.
4. **An SI file is not a new paper.** A PDF named like `*_si_001.pdf` / `*_MOESM1_ESM.pdf`, or headed "Supporting Information" on page 1, belongs to a paper: if that paper is in the vault, offer `lit add --si <id> <file>` (B15 step 3) instead of `lit add`.

---

# PART B — The reading SOP (per-operation phases)

Every trigger walks through these phases. Some collapse to one tool call; the heavier ones (cross-paper / project) are demand-only. The verdict ritual (B10) closes a reading session; resume (the Phase 1 resume branch) opens one.

## Phase 1 — Locate the paper

Convert the user's reference into a canonical paper id by **reading the vault**, not by guessing.

```
Vault root  = resolve(`$LIT_LIBRARY` | active-vault-from-registry | cwd-walk)
```

### Resume branch — "继续读" (the user names nothing)

Triggers: "继续读" / "接着上次" / "上次读到哪了" / "我在读哪篇来着" / "continue reading" / "what was I reading" / "resume".

1. **Retrieve (one CLI call):** `lit list --unread --sort recent --format json`. The CLI returns rows already filtered to unread + ordered by recency. **Do NOT re-sort or re-filter.** Take the top rows.
2. **Client-side prune `dropped`:** rows carry `status`. `--unread` doesn't exclude by status, so a paper dropped-by-title (never read, `read-date` empty) can still appear. Drop those rows. **Keep `skim`** (a skimmed paper with no `read-date` may be one the user means to return to and deep-read).
3. **Present top 3–5 (present-and-user-picks):** id + title + `status`, "most-recently-touched first". **Honesty constraint:** recency is a *proxy*, not a tracked "you were reading X" — phrase it "your most-recently-touched unfinished papers, probably where you left off", **never** "you were reading X". If the top guess is wrong, the user picks another.
4. **User picks → suggest `lit open <id>`** (suggest, don't run — see B14, unless the user says "open it"), then **resume at Phase 2** for the chosen paper.
5. **Empty result `[]`:** distinguish honestly — "nothing is marked unfinished (every paper has a `read-date`)" vs. "your library has no papers yet" (a plain `lit list` tells you which).

If a surfaced paper is `status: deep-read` yet appears here (deep-read but `read-date`-empty), nudge the B10 verdict ritual: "looks like you deep-read this but never marked it finished — want me to write the summary and stamp it read?". Resume operates **within the active vault**; cross-vault *lookup* of a named paper stays in the cue branches below.

### Locate-by-cue (the user names a paper)

**Rule:** there is a CLI flag for every recall cue — use it, never `grep`/`cat` the vault. The INDEX projection carries id / title / authors / year / type / status / topics / projects / methods / data / doi / read-date / updated-at (13 fields; the per-project `relevance-<project>` and `priority-<project>` are NOT in it — their names vary per project); for anything beyond it (full field set, free-form notes) there is a dedicated command (`lit show --format json`, `lit search`). Pick the command that matches the cue:

| Cue | Command |
|---|---|
| Direct id | `lit show <id>` (or `--format json` for the full field set, every edge) |
| Author ("Pandi 那篇") | `lit list --author <cue> --format json` |
| Title ("标题里带 X 的") | `lit list --title <substr> --format json` |
| Topic / method ("the GAT one") | Tier-2 discovery below (`lit list --topic X --format json`) |
| Something I wrote in notes ("我之前在 notes 里写过关于 X 的看法吗") | `lit search <query>` (searches `notes.md` + `discussion.md`) |

1. **Direct id given** → `lit show <id>` to confirm it exists (add `--format json` when you need the full metadata dict, not just the projection — see Phase 2). If missing, ask the user; do not silently substitute another paper.
2. **Author cue** ("Pandi 那篇") → `lit list --author <cue> --format json`. The `--author` filter matches file-side; the returned rows carry the `authors` list, so read the matched name straight off the row when confirming to the user.
3. **Title cue** → `lit list --title <substr> --format json` (case-insensitive substring, comma = OR). The CLI does the filtering file-side — do NOT pull `lit list --format json` whole and grep the title yourself.
   - 1 candidate → use it; confirm to the user "I'm reading `2023_Pandi_Cell-free`, Pandi et al. 2023 — *Cell-free …*" so they can correct.
   - 2–5 → list and ask which one.
   - >5 → ask the user to narrow the cue; do not list 30 rows.
4. **Topic cue** ("the GAT one", "anything on peptide hemolysis") → Tier-2 discovery below (`lit list --topic X --format json`).
5. **Notes-content cue** ("我之前在 notes / discussion 里写过关于 X 的看法吗", "where did I note that idea") → `lit search <query>`. This is the ONLY path to your own free-form notes; it returns `{id, file, line, snippet}` per matched line. Narrow with `--in notes` / `--in discussion`. **The PDF full text is NOT searched, nor any SI file** (that's `lit open` / the PDF ladder / B15).

### Re-open branch — a stated re-open of an already-finished paper → `lit revisit` inline

When locate lands on a paper that **already carries a `read-date`** (or `status: deep-read` / `skim`) and the user's own words declare they **re-opened** it ("我今天又把 DiffDock 翻出来想了想", "重新看了下 X", "looking at X again", "let me look at X again"), the re-open event is already on the record: **run `lit revisit <id>` now** (Tier 2, inline — stamps `last-revisited`, leaves `read-date` untouched), **then** continue to Phase 2 to engage whatever they want to think through. Do **not** turn the stamp into a "要不要我标记一下 revisit?" question — a non-interactive single turn would stall there and never stamp. Only when the re-open is your own *inference* (the user gestured at the paper but never said they re-read it) do you propose-and-confirm first. Full rule + the never-*infer* boundary: B10 → "Re-opening an already-finished paper".

### Bounded-retrieval ladder (in-vault; reuse in Phase 3)

**Never `cat INDEX.json` whole** — use bounded retrieval:

| Tier | Trigger | How | Reads INDEX into context? |
|---|---|---|---|
| 0 focus paper | already in Phase 2 | the loaded focus metadata + notes + discussion | no |
| 1 neighbours (preferred) | "跟这篇相关的还有哪些", "related to this", "what does it extend", "what builds on / disputes this", "anything similar to this one" | `lit related <id>` — one command returns explicit edges first (`related` / `extends` / `extended-by` / `contradicts` / `contradicted-by`, both inbound + outbound), then shared-topic/method neighbours ranked by overlap. JSON is the default output; each row carries the *why*: `"via": "edge"` plus `"edge": "<field>"`, or `"via": "taxonomy"` plus `"shared": ["topics:x", …]`. Narrow with `--by edges` / `--by taxonomy`; tighten taxonomy noise with `--min-shared 2`; `--limit` caps the merged list (default 20). | no |
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

On **entering a reading session**, load the registered vocabulary once: `lit taxonomy list --format json` (one array, all four dicts — never `cat TAXONOMY.md`; each row's `used_by` counts the papers per value). Flow A (lit-library [E]) needs it to enumerate registered values; Flow B needs it to validate. After the user runs `lit taxonomy {add,rename,merge,rm}` (or `lit project add`) during the session, **re-run it**.

Then, once you have an id, load just enough paper context. Read in this order, stopping when you have enough:

| Tier | Source | When to read |
|---|---|---|
| **Always** | `lit show <id> --format json` (the full metadata dict) | Identity, taxonomy, refs |
| Usually | `<vault>/papers/<id>/notes.md` | The current-understanding snapshot (overwrite-style, regenerated on explicit request) |
| On drill-down | `<vault>/papers/<id>/discussion.md` | Append+timestamp discussion trail (LOG); pull only the relevant dated section when the user drills in, not by default |
| Only when needed | `<vault>/papers/<id>/paper.pdf` | Specific sections / figures / numbers — walk the PDF reading ladder below, use the `pages` parameter to fetch §3 not the whole 40-page PDF |
| Only when the answer is in the SI | `<vault>/papers/<id>/supplementary/si-NN.*` (paths: `lit show <id>`'s `SI:` lines) | The user asks about the SI, or the main text sends the detail there — B15 |

**PDF reading ladder — a *must-achieve* goal.** Walk the rungs until one works; **never stop at rung 1's failure**:

1. **Your agent's file-read tool** (default; in Claude Code: `Read(pdf_path, pages="1-10")`): PDFs are natively handled when the agent's pipeline is multimodal.
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
- **List at most 3**, in the command's returned order (edges first, then overlap-ranked), citing the *actual* returned reason as the "why" (the row's `edge` field name or its `shared` TAXONOMY values — never invented reasons). Beyond 3, just say "N more related".
- Deep-load stays **on-demand** — only after the user picks one.

**Cross-vault references:** notes and discussion files may contain `[[<vault>:<paper-id>]]` wikilinks pointing at another registered vault. If the focus paper has such a link, resolve the target via `lit vault list` and use that vault's index exactly as in Phase 1. Surface the source vault explicitly: "From the *peptide-design* vault — `2024_Foo_Bar` …".

## Phase 4 — Project context (connect a paper to the user's own work)

**One source for both the registered set and each project's path: `lit project list`** (Tier 1 read; `--format json` gives `{name, path, status}`). It answers both "which projects exist" and "project name → on-disk directory". Do **NOT** hand-parse `lit-config.yaml`, do **NOT** route the path through `lit config show`.

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

After explicit confirmation, **read the top of `discussion.md` first** — `lit add` scaffolds it with an HTML-comment line stating the append format, and that line is the contract. Then **Write** (append, never overwrite) this format:

```markdown
## YYYY-MM-DD HH:MM

**Question:** <one-line restatement of what the user asked>

<3–10 lines of the discussion's distilled conclusion + the user's questions — user's perspective first, not the full back-and-forth>

[optional] **Follow-up:** <action item / open question>
```

Cite an SI file with its original name and the place — `si-01.pdf（ci9b00237_si_001.pdf）, p. 12, Table S3`, never a bare `si-01`: numbers are given again after a file is deleted, and `lit health-check` checks each citation against the name in brackets (B15). The `si-NN` form is only for this paper's own SI — health-check reads every `si-NN` in a paper's notes against that paper's records; for another paper's SI write `[[<other-id>]]` and the file's original name, without its `si-NN`.

The ONE discipline:

- `discussion.md` records **the distilled conclusion + the user's questions** (user's perspective first).
- **The only constraint:** if you write **your own inference**, or cite a **number/section you did not verify against the PDF**, mark it (a `⚠` or short "not verified") so next time it isn't taken as the user's settled view. One reminder, not a tag system — do **not** add `[Discussed]` / `[Agent synthesis]` tags, three `?` classes, or a separate review phase.
- **Trigger = user-explicit only.** Do not write proactively. Append, do not overwrite — the one exception is a flagged SI citation the user asked you to fix (core rule 5). `discussion.md` already exists (`lit add` scaffolds it) — keep its `# Discussion log for <id>` heading and the format comment under it, and add your dated section below. On a pre-scaffold paper the file may be absent: create it with that same heading, then the dated section, and tell the user `lit health-check --fix` backfills the rest of the library.
- **Action log (keep, do not extend):** after a `lit code add` (clone) or an unlink, append one `[Action log]` line (e.g. "Cloned <url> as codes/<name>"). Records clone/unlink only — do NOT extend it to metadata/TAXONOMY edits.
- After writing, mention the file path you appended to.

### B10 — The verdict ritual (note + status + read-date + metadata self-check)

When a reading discussion reaches a **natural close** (the user signals "done" / "一般" / "no value"), treat it as a single **verdict ritual** that binds three coupled products (fire them together), then runs a fourth move:

1. **note** — regenerate `notes.md` (the overwrite-style STATE file). Even a `drop` gets a one-line "why dropped" note. *(Cross-reference another in-vault paper with a `[[paper-id]]` wikilink — never backticks or plain text, which escape `lit rm`'s `(deleted)` tagging and `lit health-check`'s dangling detection, leaving a silent dead link when the target is removed. When you rewrite `notes.md`, for each `[[X]]` you keep verify `papers/X/` exists; if not, write it `[[X]] (deleted)` — never a bare `[[X]]` for a missing paper. Likewise write each SI citation you keep as `si-NN.ext（original）` and check it against `lit show <id>`'s `SI:` lines — a number can come to name another file.)* The note draws **only on material already in context** (this session's discussion / the abstract / the existing note). When the close signal carries no new reading material (a bare "读完了" + a depth cue, nothing actually discussed this session), keep the existing note or add one line — **never convert the note step into a blocking "要不要我扩写一下 notes？" question, and never let it gate or delay the status + read-date stamps.** The verdict stamps (products 2–3) fire inline the moment the depth is resolved; the note rides alongside as best-effort and does not hold them hostage. (A volunteered-depth close like "读完了，读得挺细" must stamp `lit read` + `lit promote` even if you choose to leave the note untouched.)
2. **status** — the curation judgment `deep-read` / `skim` / `dropped`. It is a user judgment, **never agent-inferred** — but if the user **already volunteered the depth** in the close signal, that IS their judgment: map it and stamp **inline without re-asking**. Mapping: "读得挺细的" / "精读了" / "read it closely" → `deep-read` (`lit promote`); "扫了一下" / "skimmed" → `skim` (`lit skim`); "一般" / "没价值" / "not worth it" → `dropped` (`lit drop`). Only **ask which** when the close signal carries no depth cue ("读完了" / "done with this" alone). On the resolved verdict (volunteered or asked) **run the matching sugar INLINE**.
3. **read-date** — in the **same inline step**, also run `lit read` to stamp `read-date` (marks the paper *finished* — the signal the resume branch / `--unread` keys off). The inbox-triage drop in B11 (a paper dropped by title, never read) does NOT stamp `read-date`; distinguish the two by whether the user actually read it.
4. **metadata completeness self-check** — before closing, scan the focus paper's **already-loaded** metadata (Phase 2 put it in context — **NO CLI call**, no `lit show` re-fetch) for empty *curation* fields, and **offer** to fill each, routing to its existing branch. This is **propose-and-decline**: an empty field is a legitimate "not applicable" — never a required-fill. The scan surfaces only the *remaining* gaps after the user has volunteered some during reading.

   | Empty curation field | Pre-fill posture | Routes to |
   |---|---|---|
   | `projects` | offer 1-2 plausible registered projects ("link to PepForge? PepCodec?") with a one-line why each | lit-library [H] `lit link --project` |
   | `topics` / `methods` / `data` | offer top 3-5 plausible **registered** values with a one-line why each ("topics: `amp` — the focus throughout §2; `cell-free-bio` — Pandi's substrate; `deep-learning` — the discriminator model"); never invent unregistered values | lit-library [E] Flow A — propose **only** from registered values |
   | `related` / `extends` / `contradicts` | offer specific paper id candidates from the vault with a one-line why each ("extends `[[2022_X_AMPGen]]` — same CFB + AMP combo") | the edge workflow above (propose, user confirms) |
   | `relevance-<project>` (linked but blank) | offer a one-sentence draft for the user to accept/refine ("relevance-pepforge: 'cell-free 方向的 baseline 对照'") | lit-library [H] `lit modify --set relevance-<project>=…` |
   | `priority-<project>` (linked but ungraded) | ask **once per linked project**, offering all 3 values with the agent's lean ("for pepforge: A/B/C — I lean B: method is new but the dataset is borrowed"). The same paper can be an A for one project and a C for another, so do not reuse one answer across projects. **Skip entirely when the paper is in no project** — there is nothing for a grade to be about | **inline** `lit modify --set priority-<P>=` |
   | `type` | offer top 2-3 plausible enum values with a one-line why each ("`review` — surveys 80+ AMP models, no new experiments; `research` — proposes a new CFB pipeline; I lean review") | **inline** `lit modify --set type=` (same posture as the grade) |

   Each spawned write follows its own branch's tier and the propose-first discipline — **never invent a controlled value, never fill a field the user did not endorse.**

   **Three ground rules for every self-check propose:**

   1. **Batch all gaps into one turn.** When the scan finds N empty curation fields, list them all in one message, group them sensibly (e.g. "before we close: type? topics? and how much does this matter to pepforge? — feel free to answer them together"), and let the user reply in one go. Do **NOT** serialize ("priority?" → wait → "type?" → wait …). Acceptable: one message presenting all gaps; user answers in one reply; you commit them in back-to-back writes.

   2. **Candidate list, never blank prompt.** For every gap, present **2-5 candidate values with a one-line rationale per candidate**, plus the agent's own lean ("I lean X because …"). The user picks one, edits one, writes their own, or says "skip". Forbidden patterns:
      - "What's the priority for pepforge?" → blank prompt, forces the user to recall the enum
      - "Is this a review?" → yes/no on a single guess, no escape if the guess is wrong
      The rationale must come from the discussion / abstract / notes already in context — never invent a justification from training-set memory of the paper.

   3. **`status=dropped` short-circuits the self-check.** When the verdict is `drop`, run **only**: (a) the one-line drop reason in `notes.md`, (b) the `lit drop` stamp, (c) the `lit read` stamp. Skip every other self-check row (no `topics` / `methods` / `projects` / `priority-<project>` / `type` / edges / `relevance-` prompts). Exception: if the user volunteers a grade during reading ("this is firmly C for pepforge"), accept and commit; but do not *propose* it.

**Never *infer*** — the status verdict, `read-date` stamp, and every write the self-check spawns all wait on the user's word. "The user's word" **includes a depth they volunteered** in the close signal (step 2): honoring a stated "读得挺细的" → `deep-read` executes their judgment, it is not auto-deriving one. What stays forbidden is the agent **inventing** a verdict the user never gave. The status + `read-date` writes are Tier 2 lit-reading runs **inline**. The self-check's accepted vocab/edge/link offers are the writes that **chain** to lit-library.

**Do NOT grow `lit health-check` into a curation-gap reporter.** It checks hard-rule violations (dangling refs, broken bidirectional pairs, torn writes, SI records ↔ files), not empty curation fields (which are legitimate under schema-less metadata).

**Stateless re-ask:** the scan has no memory of "the user already declined this field". A later `notes.md` regeneration re-surfaces the same empty fields. This is acceptable — do **NOT** invent a "dismissed" marker.

**Re-opening an already-finished paper → `lit revisit`.** When the focus paper already carries a `read-date` (or status is `deep-read` / `skim`) and the user is **re-opening** it in a later session, the matching stamp is `lit revisit` (stamps `last-revisited`). Posture mirrors the B10 verdict stamps: **never *infer* a revisit, but a stated one you stamp inline.** When the user's own words declare the re-open ("我今天又把 A 翻出来想了想", "重新看了下 A", "looking at A again", "let me look at A again"), the revisit event is already on the record — run `lit revisit A` and report, **do not turn it into a "要不要我标记一下 revisit?" question** (the non-interactive single-turn would stall there and never stamp). Only when re-opening is your own **inference** (the user gestured at A but did not say they re-read it) do you propose-and-confirm before stamping.

## B11 — Inbox triage nudge

`inbox` is the **default value of `metadata.yaml`'s `status:` field**, semantically "judgment pending" — NOT a directory / container / staging area. Changing `status` moves no files.

At a natural moment (using the existing staleness check, introduce no new mechanism), offer "you have N papers in inbox, sitting X days" and walk a **per-paper loop**:

1. Surface each paper's **`lit list --status inbox --format json` projection row** — title, year, type, and any existing topics / methods / projects. Triage is metadata-level (litman does not persist an abstract).
2. The **user decides** skim / deep-read / drop → run the B10 status change inline.
3. The B10 metadata self-check (product 4) **rides along, scoped to the projection row** (not a Phase-2 full load): the self-check here reads only the gaps **visible in the projection row** (`projects` / `topics` / `methods` / `data` / `type`) — still **no CLI call**. The edge fields (`related` / `extends` / `contradicts`) and both per-project fields (`relevance-<project>`, `priority-<project>`) are **not** in the projection, so those gaps are deferred to an actual read.

**This is NOT batch ingest** — litman forbids batch *ingest* decisions; this is triage of *already-ingested* papers, each still individually human-judged.

## B12 — Vault health-check guidance

At a natural moment (occasionally at session start / after a batch of operations / when the user asks "is my library still clean?"), **run `lit health-check`, translate the report's categories, and propose a remedy per finding** — except the `env` class below, which has none.

**Red line: a bare `lit health-check` never writes, and no fix runs without the user's nod** — never self-correct. Running health-check is Tier 1 (just do it). The CLI's one bulk repair is `lit health-check --fix`: it regenerates every derived artifact (recomputed from metadata) and cleans stale staging dirs / orphan trash sidecars, cleans line breaks and markup out of title / journal / author fields (`dirty_metadata`), renames a folder a rename left behind to its metadata id (`unfinished_rename`), and settles part of the SI drift (SI findings below); registry / project / taxonomy / code-clone drift stays report-only (needs a per-case decision — `--fix` never picks a side). When the findings are in the derived class, propose `--fix` and run it on the user's nod; every other proposed fix follows its own tier (e.g. unlinking a dangling clone = Tier 3, ask once).

**Three `--fix` arms are not lossless — name them before proposing it:**

- `retired_priority` — the retired paper-level `priority` moves onto each project the paper is linked to, but **a paper in no project loses its grade** (the report says `its value is dropped` only for those). Categories fold after 5 findings, so run `lit health-check --all` first, list those papers, and offer `lit link` before `--fix`.
- `dangling_refs` — a relation edge naming a paper no longer in the library (deleted outside litman, lost by a sync, **or in the trash**) is removed. A forward one can also go alone (`lit modify <id> --rm-tag <field>=<ref>`); a reverse one (`extended-by` / `contradicted-by`) only through `--fix`, since `lit modify` refuses a reverse field and the forward edge it mirrors was on the missing paper. A finding that reads `which is in the trash` is the lossy one: `lit trash restore <ref>` keeps the link, `--fix` removes it for good (a restore later rebuilds only the edges the paper names itself) — ask which. Likewise, if the user means to re-add a missing paper, do that first.
- `project_references` `… is a folder copy, not a litman link` — a project folder copied from another machine holds real folders where the links belong. Copies that match the vault (or are empty) become links; the rest are **moved to `<vault>/.trash/replaced-folders/`** and printed as `kept N folder(s) that do not match the vault: <path>` — relay those paths verbatim. `lit trash list` / `restore` never show them (copy back by hand), and `lit trash empty` deletes them for good — that is what the `replaced_folders` info line is about.

**Other lines `--fix` prints** (none is SI's, none carries an SI command): ``rename <id>: `lit rename <id> <target>` if you want …`` — an id made from a messy title; a rename breaks `\cite` keys in drafts (Tier 3), so list every such line in one table and go through lit-library [K], never run one unasked. `kept <folder>: notes.md heading differs …` — the user's own heading, theirs to change. `left papers/<folder>/: <reason>` — the folder could not be renamed; relay the reason and stop.

**A file only the user can fix.** `paper_dir_validity` `… is not a list` means a hand edit of that paper's `metadata.yaml` dropped a list (`topics: X` for `topics: [X]`): litman reads none of the paper until it is fixed, no command writes to that file, and `lit sync push` waits. The fix is the edit the hint shows — relay the file and the hint to the user, and never make that edit yourself (core rule 5); `--fix` only takes the paper out of the index until then. The same for every hint that leaves the act to the user: `paper_dir_validity`'s `… by hand` (set an id back, rename or compare folders), `Re-save it as UTF-8` (TAXONOMY.md), `Rename the folder, then retry`, SI's `left <path>: … (move it out of supplementary/)`, and `could not read notes file … — skipped` (never regenerate that notes.md over it). Relay and stop: no metadata edit, no folder move or rename, no re-encoding — litman never guesses an encoding, nor do you.

**SI findings** compare each paper's SI records in `metadata.yaml` with its `supplementary/` folder. `supplementary_missing` (error: a recorded file is gone) — ask whether the user still has the file (put it back) or the record should go: on their go, chain to lit-library [L] for the hint's `lit rm <id> --si si-NN` (with the file already gone it drops only the record). `supplementary_unrecorded` (warning: a file with no record) — `--fix` checks it and records it. Run by you (no terminal), `--fix` records only files that check out and drops no record; for the rest it prints `kept <id>: record of missing …` / `left <path>: …` lines naming an SI file, with the exact command — relay them. A `kept` line's `lit rm` goes to lit-library [L] on the user's go; a `left` line's `lit add --si … --force` runs only once they confirm the file belongs to that paper. `--fix` never deletes a file. `supplementary_changed` (error: the content no longer matches), `supplementary_layout` and `supplementary_unchecked` (links, sub-folders, unreadable files, no checksum on record) are report-only: relay the hint, the user decides. `supplementary_field` (error) is a hand edit that broke the list in `metadata.yaml` — as with `… is not a list` above, the user fixes it, never you. SI errors block `lit sync push`.

**`supplementary_cited`** (warning, never `--fix`) — a line in notes.md / discussion.md cites an SI file that no longer matches: `notes.md:7 cites si-01.pdf, which this paper no longer has`, `… cites si-01.pdf as <X>, but si-01.pdf is now <Y>` (`— that file is now si-03.pdf` when the name moved), or, in discussion.md, a bare number written before the file was added (`… added later (<date>) — may refer to a file since replaced`). Relay the file:line and the hint. On the user's go, regenerate notes.md as usual; in discussion.md change only the citation (core rule 5). It does not hold up `lit sync push`, but `lit health-check` exits 1 until the line changes.

**Vocabulary shape** (`info`, report-only, never `--fix`): `taxonomy_similar` (two topics / methods / data values that look alike — the hint is the merge), `taxonomy_unused` (registered, used by no paper — may be registered ahead on purpose), `taxonomy_singletons` (a dict where many values tag one paper each). Relay them (`lit health-check --all` lists every pair; a category stops after 5 without it); any merge or removal goes through lit-library [J]'s tidy-the-vocabulary SOP (整理词表), on the user's nod. They do not change the exit code. A new value the user names that looks like a registered one: `lit taxonomy add` refuses it (lit-library [E] Flow B; `--force` only on the user's yes).

**The `env` class has no remedy — never invent one.** `links_unsupported` (severity `info`) reports a host capability, not damage: the drive holding the library (FAT32 / exFAT / some network shares) cannot hold folder links; nothing drifted, nothing is broken. Relay the CLI's own hint — keep the library on an internal drive; after moving it, `lit health-check --fix` backfills the skipped links — and stop there. **Never suggest Developer Mode, administrator / elevated shells, WSL, or any Windows system setting**: the cause is the drive's filesystem, and none of those change it. `info` findings do not gate the exit code — `lit health-check` exiting 0 while printing an info line is a healthy library. Any warning does gate it, `supplementary_cited` included.

**Staleness nudges.** `lit` may append a dim `tip:` line after a command. Relay it and offer to run the named command — never auto-run. Two variants: (1) `tip: no lit health-check in 14+ days...` → offer `lit health-check`; (2) `tip: no lit sync push in 7+ days...` → offer `lit sync push` (backs the vault up to the configured remote; appears only when a remote is configured). A first push prints a size preview and stops for `--yes`: relay the preview, re-run with `--yes` on the user's go.

## B13 — Mis-deletion recovery (find → confirm → restore)

When the user signals a mis-deletion ("我之前好像误删了那篇关于…的文献", "把删掉的 X 找回来", "I think I deleted that paper by mistake"), run a **find → confirm → restore** flow. lit-reading owns find + confirm; the actual restore chains to lit-library [I].

1. **Find (read-only).** `lit trash list` enumerates the bin newest-first (paper id, deleted_at, title, entry_name; `--format json` adds `entry_path` and `orphan_repos`). The trashed folder is fully readable on disk at `<vault>/.trash/<entry-name>/` — when the title alone is ambiguous you **MAY** open its `metadata.yaml` / `notes.md` / `discussion.md` / the PDF / its `supplementary/` files to match the user's description by content.
2. **Confirm (present-and-user-picks, NEVER auto-restore).** Surface the **single most-likely candidate** (id + title + deleted_at, plus a one-line content cue if you inspected the files) and ask "is this the one?". If several are plausible, list at most 3 (newest-first) and let the user pick. Never restore without an explicit identity confirmation.
3. **Restore (chain to lit-library [I]).** Only after the user confirms the identity, chain to lit-library to run `lit trash restore <id>` (there is **no** top-level `lit restore`). Pass the confirmed paper id (or the full entry name `<id>-<UTC-timestamp>` if the same id sits in trash more than once), and whatever the user already said about re-cloning code.

**A deleted SI file is not in the trash.** `lit rm <id> --si` deletes for good, so there is nothing to find: ask the user for the file (from the publisher's page again, or their own copy) and add it back with `lit add --si <id> <file>` (B15 step 3) — it takes the lowest free number, often its old one. A trashed *paper* keeps its `supplementary/` folder, and a restore brings it back.

## B14 — `lit open`: suggest by default, run on explicit request

The agent **suggests** `lit open <id>` by default, but **runs it on an explicit request** ("打开这篇" / "open it for me").

`lit open` changes **no** state — no `last-revisited` stamp, no "currently reading" flag. Continue-reading / resume is handled by Phase 1 resume branch (which keys off file recency, not an open flag).

**SI files:** `lit open <id> --si si-NN` opens one with this machine's default app — same posture, and always pass the id (core rule 2). Exit 2 with an `SI path:` line means no desktop here (an SSH / HPC session) or no app for the type: relay the path, or tell the user a click on the file's chip in the litman page's Supplementary row opens it. Exit 1 with `could be run, not viewed` is a runnable file litman never opens: relay the path.

## B15 — SI on demand (supplementary / supporting information)

**When.** The user names the SI or an S-numbered item ("Table S3", "Figure S2", "SI 里…", "补充材料", "supporting information"), or asks for a detail the main text lacks and the text points to the SI for it ("see Table S3", "details in the Supporting Information"). Not for the gist, a skim, or anything the main text answers.

1. **Already in the library?** `lit show <id>` prints one `SI:` line per file: its absolute path, original name and size. (`lit show <id> --format json` has the `supplementary` records but no paths.) Files there → step 4. Never probe with `lit add --si <id>`: on a paper with no SI it opens a window on the user's screen.
2. **Not there → ask litman for it.** First tell the user in one line that you are asking litman for the SI: an SI window appears on their open litman page — Open website, download the SI there, drop the files in, press Done. Then run `lit add --si <id>` (full id or unique substring) as a shell call of its own (core rule 6). It waits up to about 90 s; let it run to its end. If your shell tool times out or moves it to the background, wait for it to finish and read its output — a run your tool cut off counts as exit 11. Rerunning the identical command rejoins the same window; it never opens a second one.

   | Exit | First stdout line(s) | What you do |
   |---|---|---|
   | 0 | `SI ready: N file(s)`, then one line per file: its absolute path, a tab, its original name | Step 4 with those files |
   | 11 | `Still waiting in the GUI — run the same command again to keep waiting` | Run the same command again. After about three runs, answer from the main text and say the SI window is still open — they can tell you once the files are in |
   | 10 | `SI request cancelled in the GUI` | Answer from the main text and say you did not see the SI; don't ask again unless the user does |
   | 12 | `No litman GUI page is open for this library`, often a line saying why, then `Download the SI from the article page: <link>` (or `No link on record — download the SI from the article's page yourself`) and `Then add the files: lit add --si <id> <file>...` | Relay the lines. The user starts `lit gui` (or opens its page) and you rerun — or they download the files and give you the paths (step 3) |
   | 13 | `The paper's text never mentions supplementary material` | Tell the user; rerun with `--force` only if they insist |
   | 1 | `error: …` | Relay it |

   A paper that has SI already returns at once (`… (already in the library)`); when the user says a file is missing, `lit add --si <id> --force` opens the window anyway.
3. **The user hands you files** → `lit add --si <id> FILE...`. Each file gets a line on stderr — `✓` / `?` / `✕`, its original name, size and the reason — and all are added or none:
   - `✕` (`empty file`, `this is the article itself`, `looks like the SI of <other-id>`, `already in the library`, `same file as <name>`) is refused and cannot be added, even with `--force`. Relay it.
   - `?` (`can't confirm it belongs to this paper`), or a file over 100 MiB, needs a yes: the run stops with `… a yes — nothing was added`. Ask the user; add `--force` only on their yes.
   - The files are **moved** into the library — the originals leave Downloads. Say so before you run it.

   Success prints the same `SI ready` lines as exit 0 → step 4.
4. **Find the answer yourself, and say where.** `si-NN` is litman's own numbering (the lowest free number when the file was added), not the publisher's: never infer "Supplementary Data 2" from `si-02`, and never guess from a number which file holds the answer. Search each file:
   - PDF → `lit pdf-text <absolute path>` (works on any PDF) to locate the label or keyword ("Table S3"). It prints no page numbers: pages are separated by form feeds (`\f`), so a hit's page is 1 + the number of `\f` before it. Then read only that page (`lit pdf-text <path> --pages N`, or your file-read tool's page range) — SI PDFs often run to hundreds of pages of spectra. For a figure, or a PDF with no text layer (exit 3), walk the Phase 2 PDF reading ladder and apply its figure rule.
   - xlsx → list the sheet names and header rows first, then read the sheet that matters.
   - csv / txt → read them directly. docx and the rest → your own tools.

   The original names only decide which file to search first. Searched every file and the label is not there (the SI was added only in part) → say which files you searched; on the user's yes, `lit add --si <id> --force` opens the window for the missing file.

**Answer = the file + where the user can jump to.** The file by number and original name; the place by PDF page + label, or xlsx sheet + rows: `si-01.pdf (ci9b00237_si_001.pdf), p. 12, Table S3`; `si-02.xlsx (41467_2023_38851_MOESM3_ESM.xlsx), sheet "Table S3", rows 3–40`. Write it the same way in notes.md / discussion.md — `si-01.pdf（original）` or `si-01.pdf (original)`: the name in brackets is what `lit health-check` checks a citation against. That form is for this paper's own SI only (Phase 5). The user opens a file with a click on its chip in the paper's Supplementary row of the litman page, or with `lit open <id> --si si-01` (B14).

**Can't search it** — the PDF ladder failed (a scan or an image no reader of yours can read), none of your tools opens the file type, or the file is too big to finish → say which file and where you stopped, and let the user open it. Never guess, and never pass off the main text or your memory of the paper as SI content.

**Red lines.** Never download SI yourself — not from the publisher, PMC, Figshare or any copy. Never write, rename, copy into or delete anything in `supplementary/`. A refused file stays refused. Deleting an SI file belongs to lit-library [L].

---

## Quick reference — the commands this skill runs directly (everything else chains to lit-library)

| Command | Why this skill calls it | Tier |
|---|---|---|
| `lit list [--format json] [--topic/--author/--title/--status/--project ...] [--unread] [--sort recent] [--limit N]` | locate / bounded retrieval / resume / triage / roundup; `--title` = title-substring cue, `--limit` = top-N | 1 (read) |
| `lit show <id> [--format json]` / `lit show --paper-doi <doi>` | confirm a paper, dedup check, read metadata aloud; `--format json` returns the FULL field set (every edge, relevance fields), beyond the INDEX projection; the table form's `SI:` lines give each SI file's path + original name (B15) | 1 (read) |
| `lit search <query> [--in notes,discussion] [--limit N]` | the ONLY path to your own free-form notes / discussion ("我在 notes 里写过 X 吗"); returns `{id,file,line,snippet}` per matched line; `--limit` caps the hit count | 1 (read) |
| `lit related <id> [--by edges|taxonomy] [--min-shared N] [--limit N]` | knowledge-graph neighbours ("跟这篇相关的还有哪些"): explicit edges first, then shared-topic/method; JSON by default, each row carries its `via` reason | 1 (read) |
| `lit pdf-text <pdf> [--pages 1-10]` | the PDF ladder's deterministic rung; locates a label in an SI PDF (B15); exit 3 = no text layer | 1 (read) |
| `lit cite <id>` | paste-ready ACS citation for one paper (citation on stdout, caveats on stderr) | 1 (read) |
| `lit vault list [--format json]` | enumerate registered vaults when a `[[v:id]]` cross-vault link surfaces | 1 (read) |
| `lit project list [--format json]` | canonical source for the registered project set AND each project's path | 1 (read) |
| `lit trash list [--format json]` | enumerate the bin for mis-deletion recovery (B13) | 1 (read) |
| `lit taxonomy list [<dict>] [--format json]` | the registered vocabulary (all four dicts, or one) with each value's `used_by` paper count — the Phase 2 session load | 1 (read) |
| `lit health-check [--fix] [--all]` | translate the report + propose per-finding remedies (B12); categories fold past the first few findings, `--all` prints every one; `--fix` = bulk repair, run only on the user's nod — not all of it lossless (B12: retired-priority migration, links to missing papers, hub folder copies); relay the `rename` / `kept` / `left` lines it prints (B12). Bulk filler-metadata / filler-id repair is lit-library [K] | 1 (read); `--fix` 2 |
| `lit read` / `lit promote` / `lit skim` / `lit drop` / `lit revisit` | the reading verdict — evaluation stamps lit-reading owns (B10) | 2 (inline) |
| `lit modify --set priority-<P>=` / `lit modify --set type=` | the per-project grade / type verdicts — evaluation stamps lit-reading owns (B10) | 2 (inline) |
| `lit add --si <id>` / `lit add --si <id> FILE... [--force]` | ask for the SI of the paper being read, or add the files the user handed you (B15 — exit codes there; `--force` only on the user's yes) | 2 (inline, announced) |
| `lit open <id> [--si si-NN]` | suggest by default; run only on explicit request (B14); `--si` opens one SI file | — |

**Chains to lit-library** (do NOT run here): `lit add <pdf>` (a new paper) / `lit code add` / `lit code link` (ingest/clone), `lit modify --add-tag topics/methods/data=` (controlled-vocab tagging), `lit modify --add-tag extends/related/contradicts=` (edges), `lit link --project` / `lit modify --set relevance-<P>=` (project binding), `lit trash restore` (restore), `lit rm <id> --si` (delete an SI file), `lit taxonomy merge/rename/rm` / `lit project rename/rm` (governance). When the discussion produces such a write, end the read phase and chain: "let me chain to lit-library to run `lit modify <id> --add-tag …`".

## Failure modes & how to handle them

| Situation | Right behaviour |
|---|---|
| Vault not discoverable (no `$LIT_LIBRARY`, no registry, no `lit-config.yaml` upward) | Stop. Tell the user. Do not invent paper content. |
| User refers to a paper that isn't in any vault | Say so. If they have the PDF, run the B9 PDF-as-entry chain (propose-confirm → chain to lit-library [A]/[B]). A bare DOI/URL with no local file is not an entry trigger. |
| `notes.md` empty, user asks "what did I think about §3?" | Be honest: "Your notes for this paper are empty — let me read §3 of the PDF instead." Then read the PDF with the `pages` parameter. |
| `INDEX.json` looks stale (id missing from index but folder exists on disk) | Cross-check with `lit show <id> --format json` — it reads `papers/<id>/metadata.yaml`, not the INDEX. If they disagree, surface the inconsistency and suggest `lit refresh-views` (teach-don't-do per A3). Do not silently route around it. |
| The user asks for an opinion on the paper | Distinguish honestly: what the **notes / discussion** record (read them) vs. **your own synthesis** (mark clearly as inference, not the user's settled view). Both notes and discussion are agent-written, so the line is *recorded-and-endorsed* vs *fresh inference*, not "user wrote vs the agent made". |
| `lit add --si` keeps exiting 11 (about three runs) | Answer from the main text, say the SI window is still open on their litman page, and read the SI once they say it is in (rerun — it returns at once). |
| `lit add --si` exits 12 (no litman page open) | Relay its lines: the user opens the litman page (`lit gui`) and you rerun, or they download the SI from the printed link and give you the paths → `lit add --si <id> FILE...`. Never fetch it yourself. |
| An SI file you cannot read after the PDF ladder fails (scan, image, no text layer), or an xlsx with no tool to open it | Say which file and where you stopped; the user opens it from its chip in the litman page. Never answer from memory as if from the SI. |
