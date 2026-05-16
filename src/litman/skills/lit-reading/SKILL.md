---
name: lit-reading
description: "Read-side companion to the litman vault. Use this skill when the user discusses a paper they are reading or want to compare, asks about contents of a paper, looks for related work across their vault, or wants to connect a paper to one of their own projects. Triggers: '这篇文章...', '这篇论文里关于...', '刚才那篇...', '<paper-id> 里...', 'and how does it compare to <other>', 'who else does this', 'have I read anything similar', 'connect this to my <project> project', '把这个观点和...联系起来', '记下这次讨论'. NOT for adding / modifying papers in the vault — that's the lit-library skill. Drives only **reads** of the vault: INDEX.json, metadata.yaml, notes.md, discussion.md, the PDF itself, and project dev_docs. Optionally appends free-form prose to <paper>/discussion.md when the user explicitly asks to record the conversation."
---

# lit-reading — Vault-aware Reading Companion

This skill teaches you (Claude) to **navigate the user's litman vault** during a reading discussion so your answers are grounded in their actual library, not generic web knowledge. Its differentiator is the same as litman's: TAXONOMY + INDEX.json give you precise retrieval *across* the user's papers, and metadata + symlinks + project bindings let you connect any paper to the user's own work.

## How this skill differs from lit-library

| User intent | Skill |
|---|---|
| "Add this paper to the vault" / "change the tag on X" / "bind this repo to that paper" | **lit-library** (writes via `lit` CLI) |
| "Discuss this paper" / "find similar work" / "compare to my project" / "summarise §3 for me" | **lit-reading** (this skill — reads only) |

If you find yourself wanting to run `lit add`, `lit modify`, or `lit taxonomy`, you have crossed into lit-library territory — finish the discussion first, then suggest the write action.

## Architecture you must respect (read-side invariants)

1. **You navigate, you do not synthesize from training data.** Every claim about a paper must come from reading something inside the vault (metadata.yaml / notes.md / discussion.md / paper.pdf) or from the user. If a paper is not in the vault, say so — do not fabricate a summary from the paper's title.
2. **No state file.** There is no `lit focus` or `lit current` — do not assume one exists. The user tells you which paper is in scope, or you infer it from natural-language cues.
3. **Vault path is discovered, never hardcoded.** Resolve via `$LIT_LIBRARY`, the active vault in `~/.config/litman/vaults.yaml`, or by walking up from the user's cwd for `lit-config.yaml`. The exact path differs per machine — never paste a stale absolute path from prior sessions.
4. **Multi-vault aware.** After M8 the user may have several registered vaults. If a paper id isn't in the active vault, check cross-vault wikilinks (`[[<vault>:<id>]]` syntax in notes) and `lit vault list` before giving up.
5. **Free-form prose only — never write structured metadata.** When the user asks to record a discussion you may append to `<vault>/papers/<id>/discussion.md` (markdown, no schema). You must **not** write metadata.yaml, TAXONOMY.md, INDEX.json, or any structured field directly. That's lit-library's job, mediated by the CLI.

If any of these would be violated, push back to the user and propose the right alternative (e.g. "let me run `lit add` for that" or "I can only summarise what's actually in the notes — should I read the PDF first?").

## Trigger signals

You should activate this skill (rather than answering from generic knowledge) when the user's message contains any of:

- Demonstratives over a paper: "这篇文章", "这篇论文", "this paper", "刚才那篇", "the one we just looked at"
- Direct id reference: "`<paper-id>` 里...", "`2023_Pandi_Cell-free` 的 §3..."
- Author / title cue: "Pandi 那篇", "the GAT one", "the cell-free synthesis paper"
- Cross-paper requests: "和 `<other-id>` 怎么对比？", "有没有人做过类似的？", "what does my library say about X"
- Project-coupling requests: "把这个和 PepCodec 联系起来", "is this relevant to my PepForge work?"
- Recording requests: "把这次讨论记下来", "save this to the paper's notes"

If the user is asking about external papers / arxiv / web — that is NOT this skill. Use web search / generic knowledge. This skill is strictly for papers already in the vault.

## The SOP

Every trigger walks through these four phases. Some phases collapse to one tool call; the heavier ones (cross-paper / project) are demand-only.

### Phase 1 — Locate the paper

Convert the user's reference into a canonical paper id by **reading the vault**, not by guessing.

```
Vault root  = resolve(`$LIT_LIBRARY` | active-vault-from-registry | cwd-walk)
INDEX_path  = <vault>/INDEX.json
```

Reading order:

1. **Direct id given** → Read `<vault>/papers/<id>/metadata.yaml` to confirm it exists. If missing, ask the user; do not silently substitute another paper.
2. **Author / title cue** → Read `INDEX.json` once and grep semantically over the `papers[]` array (id, title, authors). The index is a flat JSON document, cheap to read fully.
   - 1 candidate → use it; show the user "I'm reading 2023_Pandi_Cell-free, Pandi et al. 2023 — *Cell-free …*" so they can correct.
   - 2–5 candidates → list them and ask which one.
   - >5 candidates → ask the user to narrow the cue, do not list 30 rows.
3. **No useful cue, but a topic** ("the GAT one", "anything on peptide hemolysis") → use the 4-key TAXONOMY in `INDEX.json` to filter `papers[]` where `topics` / `methods` / `data` intersect the cue. This is the killer feature: the vault has a *controlled vocabulary*, so this filter is sharp, not a noisy free-text grep.

You may also call `lit list --topic X --format json` as a fallback if INDEX.json is stale or hard to interpret — the CLI always reflects current state.

### Phase 2 — Load paper context (lazy)

Once you have an id, load just enough context. Read in this order, stopping when you have enough:

| Tier | File | When to read |
|---|---|---|
| **Always** | `<vault>/papers/<id>/metadata.yaml` | Identity, taxonomy, refs |
| Usually | `<vault>/papers/<id>/notes.md` | The user's own summary / annotations |
| When prior discussion exists | `<vault>/papers/<id>/discussion.md` | Past conversation thread; avoids re-litigating |
| Only when needed | `<vault>/papers/<id>/paper.pdf` | For specific sections / figures / numbers the user asks about — read with the `Read` tool and **the `pages` parameter** so you fetch §3 not the whole 40-page PDF |

If `notes.md` is empty or absent (a paper still in `inbox` status) — say so, then offer to read the PDF directly. Do not pretend the user has notes they don't.

### Phase 3 — Cross-paper / cross-vault retrieval (on demand)

Only run when the user asks for comparison, related work, or "anything similar".

Use the focus paper's taxonomy as the search key. Pseudo-procedure:

```
focus_tax = metadata[focus_id]["topics"] ∪ ["methods"] ∪ ["data"]
candidates = [p for p in INDEX.papers
              if (p.topics ∪ p.methods ∪ p.data) ∩ focus_tax
              and p.id ≠ focus_id]
```

Rank by overlap size, present the top 3–5 with one-line "why this is related" (the actual shared TAXONOMY values, not invented reasons). Do **not** deep-load all candidates — only after the user picks one or two does it pay to read their metadata + notes.

**Cross-vault references**: notes and discussion files may contain `[[<vault>:<paper-id>]]` wikilinks pointing at another registered vault. If the focus paper has such a link, recognise it as an explicit cross-vault binding; resolve the target by `lit vault list` (or reading `~/.config/litman/vaults.yaml` if you must) and use that vault's `INDEX.json` exactly as in Phase 1. Surface the source vault explicitly: "From the *peptide-design* vault — `2024_Foo_Bar` …".

### Phase 4 — Project context (when the user asks to connect a paper to their own work)

The user maintains a registry mapping project name → project dev_docs directory in `lit-config.yaml`'s `projects:` field. If a paper has `projects: [pepforge]` in its metadata, the connection target is whatever path `pepforge` resolves to in that registry.

Procedure when the user asks "connect this paper to my <project>":

1. Read `<vault>/lit-config.yaml` → `projects:` map. If the requested project isn't there, tell the user — they can register it via the lit-library skill / `lit config`.
2. From the resolved project directory, read in this order (stop when you have enough to answer):
   - `<project>/dev_docs/identity.md` — what is this project, current goals
   - `<project>/dev_docs/todo/active/*.md` — what the team is currently doing
   - `<project>/dev_docs/proposals/*.md` — what is being considered
3. Synthesise: paper's claim/method/data ↔ project's current question/blocker/opportunity. Use concrete pointers ("paper §3 shows X; PepCodec's active todo M3.2 needs Y; the link is …").

Do **not** auto-write anything into either the paper's notes or the project's docs from this step — Phase 5 (below) handles deliberate sediment.

### Phase 5 — Sediment a discussion (only when asked)

If a conversation produces a genuine conclusion, idea, or follow-up the user wants to keep, offer:

> "Should I append this to `papers/<id>/discussion.md`?"

After explicit confirmation, **Write** (append, not overwrite) the following format:

```markdown
## YYYY-MM-DD HH:MM

**Question:** <one-line restatement of what the user asked>

<3–10 lines of the discussion's key points — not the full back-and-forth>

[optional] **Follow-up:** <action item / open question>
```

Rules:
- Do **not** write proactively. The user has to ask.
- Free-form markdown only. Never structured key-value pairs that look like metadata.yaml fields.
- Append, do not overwrite. If `discussion.md` doesn't exist, create it with a top-level `# Discussion log for <id>` header and then the dated section.
- After writing, mention the file path you appended to so the user can `lit open <id>`'s notes / open the file directly.

## Quick reference — the only commands this skill needs

This skill is read-heavy. The CLI commands you would ever invoke (rare, mostly as fallbacks):

| Command | Why this skill might call it |
|---|---|
| `lit list --format json` | Fallback if INDEX.json is hard to grep or you want pre-filtered output |
| `lit list --topic X --status deep-read` | Quick TAXONOMY filter without parsing INDEX.json yourself |
| `lit show <id>` | Pretty-print a paper's metadata when you want to read aloud to the user |
| `lit vault list` | Enumerate registered vaults when a `[[v:id]]` cross-vault link surfaces |
| `lit open <id>` | Suggest to the user (don't run yourself unless they ask) — opens the PDF in their viewer |

Notably **not** in this skill's toolkit: `lit add`, `lit modify`, `lit taxonomy`, `lit rename`, `lit rm`, `lit code add`. Those are writes, owned by lit-library. If the discussion produces a clear write action (new tag, new related-paper edge, code repo to clone), end the read-phase and hand off: "Once you've decided, I can run `lit modify <id> --add-tag …` via lit-library."

## Failure modes & how to handle them

| Situation | Right behaviour |
|---|---|
| Vault not discoverable (no `$LIT_LIBRARY`, no registry, no `lit-config.yaml` upward) | Stop. Tell the user. Do not invent paper content. |
| User refers to a paper that isn't in any vault | Say so. Offer to add it via lit-library (don't switch skills mid-message — propose, let them decide). |
| `notes.md` empty, user asks "what did I think about §3?" | Be honest: "Your notes for this paper are empty — let me read §3 of the PDF instead." Then read the PDF with the `pages` parameter. |
| `INDEX.json` looks stale (e.g. id missing from index but folder exists on disk) | Cross-check by reading the metadata.yaml directly. If they disagree, surface the inconsistency and suggest `lit refresh-views`. Do not silently route around it. |
| The user asks for an opinion on the paper | Distinguish: "what *you* (the user) wrote" (read notes/discussion) vs. "what *I* (Claude) make of it" (mark clearly as your synthesis, not the user's). |
