---
name: lit-reading
description: "Read side of the litman vault: grounds a discussion of a paper, or its SI, in the user's own library. NOT for adding or changing papers — that's lit-library. Use when the user talks about a paper they are reading, asks about its supplementary information ('Table S3', '补充材料'), looks for related work in the vault, connects a paper to their project, or wants the discussion logged. Triggers: '这篇文章…' / 'this paper…', '继续读' / 'continue reading', '读完了' / '这篇一般' / 'done with this paper', '跟这篇相关的还有哪些' / 'related work', '我在 notes 里写过 X 吗' / 'search my notes', '最近读了啥' / 'what did I read recently', '记下这次讨论' / 'log this discussion', '误删了那篇' / 'restore a paper I deleted', '清一下 inbox' / 'triage my inbox', '我的库还干净吗' / 'is my library clean'. Reads the vault and the PDF; writes notes.md and discussion.md only when asked."
---

# lit-reading — vault-aware reading companion

Grounds a reading discussion in the user's own litman vault, not in general knowledge. Changing the library is **lit-library**.

## Rules — they hold for every workflow below

1. **Read with `--format json`, one `lit` command per shell call.** Every read of the vault (`lit list`, `lit show`, `lit project list`, `lit taxonomy list`, …) takes `--format json` (`lit search` and `lit related` print JSON already): the table form is cut to the terminal's width. The command rule litman installs for your agent allows a command that *is* `lit …` and nothing else, so `which lit && lit …`, `cd <dir> && lit …`, `lit … ; echo $?`, `lit … | head` and redirections (`2>&1`, `> file`) are refused. Read the exit code and the output from the tool result, and filter the output yourself.
2. **Not sure what the user means, or which command or flag does it → `lit help` / `lit <cmd> --help` first**; if it is still unclear, ask the user. Never guess a command or a flag from memory.
3. **A request no workflow here covers → look it up in `lit help`.** If a command does it, run it — Rules 4 and 11 still apply. Don't answer "I can't" before you have looked.
4. **Sensitive operations come in two levels.**
   - **Never run these, even when asked — show the exact command and let the user type it:** `lit trash empty` and `lit rm --purge`. Nothing brings their files back. You may run `lit trash empty --dry-run` or `lit rm <id> --dry-run` first to show what would go; `trash empty` also deletes the folders `lit health-check --fix` moved to `.trash/replaced-folders/`, so say that too.
   - **Run these only after the user has seen the consequences and said go:** `lit rm <id>` (moves the paper to the trash), `lit rm <id> --si si-NN` (deletes an SI file for good — there is no trash for SI), `lit code rm`, `lit taxonomy merge`, `lit taxonomy rm`, `lit project rm`, `lit export --force` (overwrites a hand-edited .bib), and rewriting a `notes.md` that holds more than the `lit add` scaffold. Never start one yourself — relaying a `lit` hint that names one is fine, and lit-reading B10 may offer a `notes.md` rewrite in its closing proposal. When the user asks for one, show the command and what it will do — run its `--dry-run` where it has one and relay the lines verbatim, never summarising a link count away — and wait. Their request and their go after seeing the consequences are the two confirmations: then run it (with `--yes`: the CLI's own `y/N` prompt never reaches you, and a non-interactive run without `--yes` aborts) and relay the result. Don't ask a third time.
   - **Renames lose nothing** (`lit rename`, `lit taxonomy rename`, `lit project rename`): say what will change, then run them, with no separate question.
5. **Write only in the active vault.** Reading another registered vault is fine. To write into another one, the user switches first (`lit vault use <name>`).
6. **Windows PowerShell 5.1** shows whatever `lit` writes to stderr, warnings included, as a red error and may report exit code 1. Judge by what the command printed, not by the colour.
7. **What you write yourself.** **Never** write `metadata.yaml`, `TAXONOMY.md`, `INDEX.json`, `lit-config.yaml` or `codes/<name>/repo-meta.yaml`, and never write, rename, copy into or delete anything in `papers/<id>/supplementary/` — every such change goes through `lit` (metadata as JSON through `lit add --from-llm-json` or `lit modify`, vocabulary through `lit taxonomy` / `lit project`, SI through `lit add --si` / `lit rm <id> --si`). The only files you write are `notes.md` (overwrite) and `discussion.md` (append); `lit add` scaffolds both with an HTML-comment line stating the format — read it before you write and never strip it. The one edit inside existing `discussion.md` text is an SI citation `lit health-check` flagged, fixed on the user's go: that citation only (B12). When `lit` stops with a message that leaves the act to the user — `Cannot use <file>: '<field>' is not a list`, `… by hand`, `Re-save it as UTF-8`, `Rename the folder, then retry`, `Fix that file, then retry`, `move it out of supplementary/`, `could not read notes file … — skipped` — relay it verbatim and stop: no metadata edit, no folder move or rename, no re-encoding, no rewriting that notes.md. If the user asks you to edit one of these files by hand, say why not and offer the `lit` command that does it.
8. **Don't install, upgrade or uninstall litman.** If `lit` is missing, give the user the install command — `curl -LsSf https://get.litman.dev/install.sh | sh` (or `uv tool install litman` / `pipx install litman`) — and stop. If `lit hello` says a newer litman exists, pass that on and carry on with the task.
9. **Cite in-vault papers as `[[paper-id]]`**, never in backticks or plain text — those escape `lit rm`'s `(deleted)` tagging and `lit health-check`'s dangling-link check. Check that each `[[X]]` you write or keep exists (`lit show X --format json`); a missing one becomes `[[X]] (deleted)`. Cite this paper's own SI file as `si-01.pdf（<original name>）`, never a bare number; another paper's SI as `[[<other-id>]]` plus the file's original name, without its `si-NN`.
10. **Don't organise on your own.** Never tag, add an edge, link a paper to a project or bind code on your own initiative. Saying what you notice ("this touches tokenization") is fine — noticing is not tagging. Propose; the user decides; then you run the command.
11. **Write only on an explicit instruction.** Reads need no permission. A write — a status or date stamp, a tag, a grade, `type`, an edge, a project link, rewriting `notes.md`, appending to `discussion.md` — runs only when (a) the user's message asks for that write ("标成已读", "记下来", "打上 X", "加到文献库"), or (b) the user said yes to a proposal that listed it. A remark is not an instruction: "这篇差不多了", "感觉没什么要读的了", "读完了", "looks done" get one proposal that lists every write you intend, and then you wait. A yes covers the listed items only — nothing extra on the side. One exception: `lit add --si <id>` with no files — say so in one line, then run it; it only opens a window, and nothing is written until the user adds files and presses Done.
12. **Speak only from the vault or the user.** Every claim about a paper comes from something in the vault — `metadata.yaml`, `notes.md`, `discussion.md`, `paper.pdf`, the SI files in `supplementary/` — or from the user. A paper not in the vault: say so; never summarise it from its title or from memory. Keep what the notes and discussion record (the user's endorsed view) apart from your own fresh synthesis, and mark the latter as yours.
13. **There is no "current paper" on record.** The user names it, or you infer it from their words. `lit open` / `lit show` with no id fall back to the most recently touched paper — a keystroke saver for a human at a terminal, not a way for you to find out which paper is meant. Never run them bare: ask, or use the resume branch (Phase 1). Always pass the id.
14. **The vault is discovered, never hard-coded:** `$LIT_LIBRARY`, the active vault (`lit vault list --format json`), or `lit-config.yaml` walking up from the cwd — never a path pasted from an earlier session. Several vaults may be registered; a `[[<vault>:<id>]]` link points into another one, which you may read (Rule 5). No vault found → say so and stop; never make up paper content.

## What lit-reading runs itself, and what goes to lit-library

On the user's instruction (Rule 11) lit-reading runs the evaluation stamps (`lit read`, `lit promote`, `lit skim`, `lit drop`, `lit revisit`, `lit modify --set priority-<P>=`, `lit modify --set type=`), `lit add --si` for the paper being read (B15), and the writes to `notes.md` / `discussion.md`. Reads it runs freely. **Every other write goes to lit-library**, named explicitly, with the paper id, the PDF path, the exact intent ("add `extends=<other-id>`"), anything the user already said, and the place to resume here ("resume Phase 2 with the new id"):

- a `topics` / `methods` / `data` tag → [E] Flow A / B;
- an `extends` / `related` / `contradicts` edge → [E] (forward only);
- a project link or a `relevance-<P>` → [H] (pass along a grade the user gave, so the link runs with `--priority` in one step);
- a new paper, a code clone, binding / unbinding / retiring code → [A] / [B] / [C];
- deleting an SI file → [L]; restoring a trashed paper → [I]; vocabulary or project governance → [J] / [H].

Once lit-library is done its result (a new id, say) is in context: carry on here.

---

## B9 — A PDF dropped into the chat

1. **Is it in the vault?** `lit list --title <a distinctive word or two> --format json` — the CLI filters; don't pull the whole list and search it yourself. If the PDF prints a DOI, `lit show --paper-doi <doi> --format json` is the exact check.
2. **Not in the vault → propose** "this isn't in your vault — want me to add it?". On yes, hand off to lit-library [A] / [B] with the PDF path and any DOI, then resume at Phase 2 with the new id.
3. A bare DOI or URL with no local file is not this entry: lit-library [B] asks for the file.
4. **An SI file is not a new paper** (`*_si_001.pdf`, `*_MOESM1_ESM.pdf`, "Supporting Information" on page 1): if its paper is in the vault, offer `lit add --si <id> <file>` (B15 step 3) instead of `lit add`.

## Phase 1 — Locate the paper

### Resume — "继续读" / "上次读到哪了" / "continue reading" (no paper named)

1. `lit list --unread --sort recent --format json`. Take the rows in the order returned — **don't re-sort or re-filter**, except: drop rows with `status: dropped` (`--unread` keeps a paper dropped by title), keep `skim`.
2. Show the top 3–5 (id, title, `status`) and let the user pick. Recency is a proxy: say "your most-recently-touched unfinished papers, probably where you left off" — **never** "you were reading X".
3. They pick → suggest `lit open <id>` (B14) and go to Phase 2.
4. `[]` → say which: nothing is unfinished (every paper has a `read-date`), or there are no papers yet (a plain `lit list --format json` tells).

A row that is `deep-read` with no `read-date` → offer the B10 close ("looks like you deep-read this but never marked it finished — want me to write the summary and stamp it read?"). Resume stays in the active vault.

### The user names a paper

There is a CLI filter for every cue — never `grep` or `cat` the vault:

| Cue | Command |
|---|---|
| an id | `lit show <id> --format json` — missing → ask; never substitute another paper |
| an author ("Pandi 那篇") | `lit list --author <cue> --format json` |
| a title word ("标题里带 X 的") | `lit list --title <substr> --format json` |
| a topic / method ("the GAT one") | `lit list --topic <X> --format json` (or `--method`) |
| something they wrote ("我在 notes 里写过 X 吗") | `lit search <query>` — notes.md + discussion.md only (`--in notes` / `--in discussion`); never the PDF or the SI |

One match → use it and say which ("I'm reading `2023_Pandi_Cell-free`, Pandi et al. 2023 — *Cell-free …*") so they can correct you; 2–5 → list them and ask; more than 5 → ask them to narrow the cue.

The user says they went back to a paper they had finished ("又把 X 翻出来想了想", "重新看了下 X", "looking at X again") → B10, "Re-opening a finished paper".

### Related papers — bounded retrieval

**Never `cat INDEX.json`**, and never pull every row to compare topics by hand.

- **With a focus paper** ("跟这篇相关的还有哪些", "what builds on this", "anything similar") → `lit related <id>`: explicit edges first, both directions, then papers sharing topics / methods, ranked by overlap. Each row carries its reason in `via` (`edge` plus the field, or `taxonomy` plus `shared`). Narrowing flags: `lit related --help`.
- **No focus paper** ("what's in my library about X") → `lit list --topic X --format json` (or `--method`, `--data`, `--project`).
- Chain reads freely: `lit search <term>` → `lit show <id> --format json` → `lit related <id>`.

This stays in the active vault; a `[[<vault>:<id>]]` link is resolved with `lit vault list --format json`.

### Retrospective questions

- "最近读了啥" / "what did I read recently" → `lit list --sort recent --format json`; summarise the rows as returned, without re-sorting. Recency is a proxy: "your most-recently-touched papers, probably what you read lately", never "you read these this week". A date bound: `--read-since` / `--added-since` (`lit list --help`).
- "汇总我库里关于 X 的" / "summarize my library on X" → `lit list --topic` / `--method` / `--project X --format json`.
- "我在哪写过 X" → `lit search X`.

Load a paper's own files only when the user drills into one.

**The index looks stale** — an id is missing from `lit list` but its folder exists, or `lit show <id> --format json` (which reads `metadata.yaml`) disagrees with the list → say so and propose `lit refresh-views`; run it on the user's yes. Don't route around it silently.

## Phase 2 — Load the paper (lazily)

At the start of a reading session load the vocabulary once: `lit taxonomy list --format json` (all four dicts, `used_by` per value; never `cat TAXONOMY.md`). Re-run it after any `lit taxonomy` or `lit project add` change during the session.

Then read only as much as you need, in this order:

| When | Source |
|---|---|
| Always | `lit show <id> --format json` — the full metadata |
| Usually | `papers/<id>/notes.md` — the current-understanding snapshot |
| On drill-down | `papers/<id>/discussion.md` — the dated log; only the relevant section |
| For sections, figures, numbers | `papers/<id>/paper.pdf` — the ladder below, only the pages you need |
| When the answer is in the SI | `papers/<id>/supplementary/si-NN.*` (paths: `lit show <id>`'s `SI:` lines) — B15 |

**PDF reading ladder — keep going until one rung works; never stop at the first failure:**

1. your agent's file-read tool (in Claude Code: `Read(pdf_path, pages="1-10")`);
2. a PDF / document MCP tool, if you have one;
3. `lit pdf-text <pdf> --pages 1-10` — needs nothing beyond `lit` (no poppler); exit 3 means no text layer (a scan): go back up to a multimodal reader or OCR;
4. only when all three failed: say which rung failed and why, then show install commands for the user to run (`brew install poppler` / `apt install poppler-utils` / `dnf install poppler-utils` / `scoop install poppler`) or suggest a vision-capable model. **Never run an install yourself.**

Never summarise from the title or from memory instead of reading; never read all 40 pages when the user asked about §3.

**Text-only backend asked about a figure / table image ("what does Fig./Table N show?").** When the active model has no native vision (rung 1 unavailable) AND no vision-capable PDF MCP (rung 2), `lit pdf-text` is the only reader and it returns the *text layer only* — it cannot see images. Do this, in order: (1) pull the figure/table **caption** and the **body sentences that reference it** via `lit pdf-text`, and answer strictly from that text; (2) state plainly that you **cannot see the figure/table image itself**; (3) tell the user that reading the actual visual (curve shapes, bar heights, numbers inside an image-table) needs a multimodal model or an explicitly attached vision MCP. **Never infer or fabricate the visual content from the caption or surrounding prose** — caption text is not the figure. This is the highest-risk hallucination point in the whole skill.

`notes.md` is the STATE (one snapshot, rewritten at B10); `discussion.md` is the LOG (appended at Phase 5). A `⚠` / "not verified" item in discussion.md is a past inference nobody confirmed — ask about it in passing. An empty or missing `notes.md` (an `inbox` paper): say so and offer to read the PDF; never pretend the user has notes they don't.

## Phase 3 — Cross-paper (on demand)

Only when the user asks for a comparison or related work. Run `lit related <id>` and give **one** signpost line: **at most 3** papers in the order returned, each with the reason its row gives (the `edge` field or the `shared` values — never an invented one); beyond that, "N more related". Load a candidate's metadata and notes only after the user picks it.

A `[[<vault>:<id>]]` link → resolve that vault with `lit vault list --format json` and name the source: "From the *peptide-design* vault — `2024_Foo_Bar` …".

## Phase 4 — Connect to the user's project

`lit project list --format json` gives the registered projects and their paths — not `lit-config.yaml`, not `lit config show`. Match the user's project name by case, whitespace and separators only; anything looser (an abbreviation, no match, several matches) → show the registered set and let them pick. Never guess; the cwd is at most a hint.

Then read, stopping when you have enough: `<project>/dev_docs/identity.md`, `<project>/dev_docs/todo/active/*.md`, `<project>/dev_docs/proposals/*.md`. Connect the paper's claim / method / data to the project's current question with concrete pointers ("paper §3 shows X; the active todo <name> needs Y; the link is …").

You may say "this looks related to your `<project>`". A link happens only when the user asks ("bind it" / "add it to `<project>`") → lit-library [H].

## Edges — propose only

When the discussion implies one ("this is Pandi's follow-up" → `extends`; "its conclusion contradicts X" → `contradicts`; "these two read well together" → `related`), judge it and **propose** it. Only once the user confirms, hand it to lit-library [E]. Name the **forward** direction only; the CLI writes the reverse field itself.

## Phase 5 — Write down a discussion (only when asked)

When the conversation produces a conclusion, an idea or a follow-up the user may want to keep, offer: "Should I append this to `papers/<id>/discussion.md`?" When the user asks for it ("记下这次讨论"), just do it. Read the top of `discussion.md` first — its HTML-comment line is the format contract — then append, never overwrite:

```markdown
## YYYY-MM-DD HH:MM

**Question:** <one-line restatement of what the user asked>

<3–10 lines of the discussion's distilled conclusion + the user's questions — user's perspective first, not the full back-and-forth>

[optional] **Follow-up:** <action item / open question>
```

Cite an SI file with its original name and the place — `si-01.pdf（ci9b00237_si_001.pdf）, p. 12, Table S3`, never a bare `si-01`: numbers are given again after a file is deleted, and `lit health-check` checks each citation against the name in brackets (B15). The `si-NN` form is only for this paper's own SI — health-check reads every `si-NN` in a paper's notes against that paper's records; for another paper's SI write `[[<other-id>]]` and the file's original name, without its `si-NN`.

- Record the distilled conclusion and the user's questions — their perspective first, not the whole back-and-forth.
- Mark **your own inference**, or a number or section you did not check against the PDF, with `⚠` or "not verified", so it is not later taken for the user's settled view. One marker — no tag system, no review phase.
- Append only; the one exception is a flagged SI citation the user asked you to fix (Rule 7). Keep the `# Discussion log for <id>` heading and the format comment under it, and add your dated section below. On a pre-scaffold paper with no file, create it with that heading, then your section, and tell the user `lit health-check --fix` backfills the rest of the library.
- Afterwards, say which file you appended to.

## B10 — Closing out a paper

When the reading reaches a natural close — "读完了", "这篇一般", "没价值", "这篇差不多了", "done with this paper" — **send one proposal and wait** (Rule 11). It lists every write you intend:

1. **Status and read-date.** The depth is the user's judgment, never yours. If they gave it, pre-fill it: "读得挺细的" / "精读了" / "read it closely" → `deep-read` (`lit promote`); "扫了一下" / "skimmed" → `skim` (`lit skim`); "一般" / "没价值" / "not worth it" → `dropped` (`lit drop`). If not, ask which, with your lean. Either way `lit read` stamps the read-date — it marks the paper finished, which the resume branch and `--unread` key off; `lit promote` does not stamp it by itself. A paper dropped by its title at inbox triage, never read, gets no `lit read`.
2. **notes.md.** If it holds only the `lit add` scaffold and the user asked for notes, you may write it; if it has content, ask in the proposal whether to rewrite it from this session (Rule 4). Draw only on what is already in context — this session's discussion, the abstract, the existing note; with nothing new discussed, propose keeping the note or adding one line. Even a drop gets a one-line "why dropped". Check every `[[X]]` you keep (Rule 9), and every SI citation against `lit show <id>`'s `SI:` lines — a number can come to name another file.
3. **Empty curation fields.** Scan the metadata Phase 2 already loaded (no new CLI call) and add each empty field to the same message, with 2–5 candidates, a one-line why for each, and your lean:

   | Empty field | What to offer | Runs |
   |---|---|---|
   | `projects` | 1–2 registered projects ("link to PepForge? PepCodec?") | lit-library [H] `lit link --project` |
   | `topics` / `methods` / `data` | the top 3–5 **registered** values ("topics: `amp` — the focus throughout §2; `cell-free-bio` — Pandi's substrate"); never an unregistered one | lit-library [E] Flow A |
   | `related` / `extends` / `contradicts` | paper ids from the vault ("extends `[[2022_X_AMPGen]]` — same CFB + AMP combo") | lit-library [E], forward only |
   | `relevance-<project>` (linked, blank) | a one-sentence draft ("relevance-pepforge: 'cell-free 方向的 baseline 对照'") | lit-library [H] |
   | `priority-<project>` (linked, ungraded) | A / B / C with your lean, **once per linked project** — a paper can be A for one and C for another; skip it when the paper is in no project | here: `lit modify --set priority-<P>=` |
   | `type` | 2–3 enum values ("`review` — surveys 80+ AMP models, no new experiments; I lean review") | here: `lit modify --set type=` |

   An empty field is a legitimate "not applicable" — never press for it. A grade or a tag the user mentioned while reading ("这篇对 pepforge 是 C") goes into this proposal pre-filled; don't write it on the spot. Rationale comes only from the discussion, the abstract and the notes in context, never from memory of the paper. Never a blank question ("What's the priority for pepforge?") and never a yes/no on a single guess ("Is this a review?").

Put it all in **one** message, grouped ("收尾前：状态？type？对 pepforge 有多重要？可以一起回"), so the user answers once. Then run exactly what they approved — the stamps, grade and `type` here, the rest handed to lit-library — and nothing else.

- **An explicit instruction is not a remark.** "标成已读，精读" / "mark it read, deep-read" → run `lit read` and `lit promote` at once, without asking again; offer the notes and the empty fields afterwards, in one message.
- **A drop is short.** For `dropped` the proposal is only a one-line drop reason in `notes.md`, `lit drop` and `lit read` — no field offers (a grade the user volunteered still goes in).
- **No memory of declined fields.** A later close may offer the same empty fields again; that is fine — don't invent a "dismissed" marker.
- **Re-opening a finished paper.** The user says they went back to a paper that already has a `read-date` (or is `deep-read` / `skim`): "又把 X 翻出来想了想", "重新看了下 X", "looking at X again". Propose `lit revisit <id>` (stamps `last-revisited`, leaves `read-date` alone) in your reply, carry on with what they want to discuss, and run it when they say yes. Never infer a revisit they did not state.

## B11 — Inbox triage

`inbox` is the default `status` — "not judged yet", not a folder; changing it moves no files. At a natural moment (no new mechanism), offer "you have N papers in inbox, sitting X days" and go one paper at a time:

1. Show its row from `lit list --status inbox --format json`: title, year, type, topics / methods / projects. Triage is metadata-level.
2. The **user** decides skim / deep-read / drop → run that stamp.
3. B10's field offers ride along, limited to what the row shows (`projects` / `topics` / `methods` / `data` / `type`) — no extra CLI call. Edges and the per-project fields are not in the row; leave them for an actual read.

This is triage of papers already in the vault, each judged by the user — not batch ingest.

## B12 — Vault health-check guidance

At a natural moment (occasionally at session start / after a batch of operations / when the user asks "is my library still clean?"), **run `lit health-check`, translate the report's categories, and propose a remedy per finding** — except the `env` class below, which has none.

**Red line: a bare `lit health-check` never writes, and no fix runs without the user's nod** — never self-correct. Running health-check is a read: just do it. The CLI's one bulk repair is `lit health-check --fix`: it regenerates every derived artifact (recomputed from metadata) and cleans stale staging dirs / orphan trash sidecars, cleans line breaks and markup out of title / journal / author fields (`dirty_metadata`), renames a folder a rename left behind to its metadata id (`unfinished_rename`), and settles part of the SI drift (SI findings below); registry / project / taxonomy / code-clone drift stays report-only (needs a per-case decision — `--fix` never picks a side). When the findings are in the derived class, propose `--fix` and run it on the user's nod; every other fix follows its own workflow (unlinking a dangling clone, say, is lit-library [C.3]) and waits for the user's go.

**Three `--fix` arms are not lossless — name them before proposing it:**

- `retired_priority` — the retired paper-level `priority` moves onto each project the paper is linked to, but **a paper in no project loses its grade** (the report says `its value is dropped` only for those). Categories fold after 5 findings, so run `lit health-check --all` first, list those papers, and offer `lit link` before `--fix`.
- `dangling_refs` — a relation edge naming a paper no longer in the library (deleted outside litman, lost by a sync, **or in the trash**) is removed. A forward one can also go alone (`lit modify <id> --rm-tag <field>=<ref>`); a reverse one (`extended-by` / `contradicted-by`) only through `--fix`, since `lit modify` refuses a reverse field and the forward edge it mirrors was on the missing paper. A finding that reads `which is in the trash` is the lossy one: `lit trash restore <ref>` keeps the link, `--fix` removes it for good (a restore later rebuilds only the edges the paper names itself) — ask which. Likewise, if the user means to re-add a missing paper, do that first.
- `project_references` `… is a folder copy, not a litman link` — a project folder copied from another machine holds real folders where the links belong. Copies that match the vault (or are empty) become links; the rest are **moved to `<vault>/.trash/replaced-folders/`** and printed as `kept N folder(s) that do not match the vault: <path>` — relay those paths verbatim. `lit trash list` / `restore` never show them (copy back by hand), and `lit trash empty` deletes them for good — that is what the `replaced_folders` info line is about.

**Other lines `--fix` prints** (none is SI's, none carries an SI command): ``rename <id>: `lit rename <id> <target>` if you want …`` — an id made from a messy title; a rename breaks `\cite` keys in drafts, so list every such line in one table and go through lit-library [K], never run one unasked. `left papers/<folder>/: <reason>` — the folder could not be renamed; relay the reason and stop.

**A file only the user can fix.** `paper_dir_validity` `… is not a list` means a hand edit of that paper's `metadata.yaml` dropped a list (`topics: X` for `topics: [X]`): litman reads none of the paper until it is fixed, no command writes to that file, and `lit sync push` waits. The fix is the edit the hint shows — relay the file and the hint to the user, and never make that edit yourself (Rule 7); `--fix` only takes the paper out of the index until then. The same for every hint that leaves the act to the user: `paper_dir_validity`'s `… by hand` (set an id back, rename or compare folders), `Re-save it as UTF-8` (TAXONOMY.md), `Rename the folder, then retry`, SI's `left <path>: … (move it out of supplementary/)`, and `could not read notes file … — skipped` / `could not read papers/<folder>/notes.md …` (never regenerate that notes.md over it). Relay and stop: no metadata edit, no folder move or rename, no re-encoding — litman never guesses an encoding, nor do you.

**SI findings** compare each paper's SI records in `metadata.yaml` with its `supplementary/` folder. `supplementary_missing` (error: a recorded file is gone) — ask whether the user still has the file (put it back) or the record should go: on their go, chain to lit-library [L] for the hint's `lit rm <id> --si si-NN` (with the file already gone it drops only the record). `supplementary_unrecorded` (warning: a file with no record) — `--fix` checks it and records it. Run by you (no terminal), `--fix` records only files that check out and drops no record; for the rest it prints `kept <id>: record of missing …` / `left <path>: …` lines naming an SI file, with the exact command — relay them. A `kept` line's `lit rm` goes to lit-library [L] on the user's go; a `left` line's `lit add --si … --force` runs only once they confirm the file belongs to that paper. `--fix` never deletes a file. `supplementary_changed` (error: the content no longer matches), `supplementary_layout` and `supplementary_unchecked` (links, sub-folders, unreadable files, no checksum on record) are report-only: relay the hint, the user decides. `supplementary_field` (error) is a hand edit that broke the list in `metadata.yaml` — as with `… is not a list` above, the user fixes it, never you. SI errors block `lit sync push`.

**`supplementary_cited`** (warning, never `--fix`) — a line in notes.md / discussion.md cites an SI file that no longer matches: `notes.md:7 cites si-01.pdf, which this paper no longer has`, `… cites si-01.pdf as <X>, but si-01.pdf is now <Y>` (`— that file is now si-03.pdf` when the name moved), or, in discussion.md, a bare number written before the file was added (`… added later (<date>) — may refer to a file since replaced`). Relay the file:line and the hint. On the user's go, regenerate notes.md as usual; in discussion.md change only the citation (Rule 7). It does not hold up `lit sync push`, but `lit health-check` exits 1 until the line changes.

**Vocabulary shape** (`info`, report-only, never `--fix`): `taxonomy_similar` (two topics / methods / data values that look alike — the hint is the merge), `taxonomy_unused` (registered, used by no paper — may be registered ahead on purpose), `taxonomy_singletons` (a dict where many values tag one paper each). Relay them (`lit health-check --all` lists every pair; a category stops after 5 without it); any merge or removal goes through lit-library [J]'s tidy-the-vocabulary SOP (整理词表), on the user's nod. They do not change the exit code. A new value the user names that looks like a registered one: `lit taxonomy add` refuses it (lit-library [E] Flow B; `--force` only on the user's yes).

**The `env` class has no remedy — never invent one.** `links_unsupported` (severity `info`) reports a host capability, not damage: the drive holding the library (FAT32 / exFAT / some network shares) cannot hold folder links; nothing drifted, nothing is broken. Relay the CLI's own hint — keep the library on an internal drive; after moving it, `lit health-check --fix` backfills the skipped links — and stop there. **Never suggest Developer Mode, administrator / elevated shells, WSL, or any Windows system setting**: the cause is the drive's filesystem, and none of those change it. `info` findings do not gate the exit code — `lit health-check` exiting 0 while printing an info line is a healthy library. Any warning does gate it, `supplementary_cited` included.

## B13 — Mis-deletion recovery: find → confirm → restore

"我之前好像误删了那篇关于…的文献" / "把删掉的 X 找回来" / "I deleted that paper by mistake":

1. **Find.** `lit trash list --format json` (newest first: id, deleted_at, title, entry_name, entry_path, orphan_repos). A trashed folder is readable at `<vault>/.trash/<entry-name>/` — when the title is ambiguous you may open its `metadata.yaml`, notes, discussion, PDF or SI files to match the user's description.
2. **Confirm — never restore on a guess.** Show the most likely candidate (id, title, deleted_at, plus a content cue if you looked inside) and ask "is this the one?"; several plausible → at most 3, newest first, and the user picks.
3. **Restore** — once the user confirmed which, hand off to lit-library [I] with the id (or the full entry name `<id>-<UTC-timestamp>` when the id is in the trash more than once) and anything they said about re-cloning code. There is **no** top-level `lit restore`.

**A deleted SI file is not in the trash** (`lit rm <id> --si` deletes for good): ask the user for the file — from the publisher's page again, or their own copy — and add it back with `lit add --si <id> <file>` (B15 step 3); it takes the lowest free number, often its old one. A trashed *paper* keeps its `supplementary/`, and a restore brings it back.

## B14 — `lit open`

Suggest `lit open <id>` by default; run it when the user asks ("打开这篇" / "open it for me"). It changes no state — no `last-revisited`, no "currently reading" flag.

`lit open <id> --si si-NN` opens one SI file with this machine's default app — same posture, always with the id (Rule 13). Exit 2 with an `SI path:` line means no desktop here (an SSH / HPC session) or no app for the type: relay the path, or tell the user that a click on the file's chip in the litman page's Supplementary row opens it.

## B15 — SI on demand (supplementary / supporting information)

**When.** The user names the SI or an S-numbered item ("Table S3", "Figure S2", "SI 里…", "补充材料", "supporting information"), or asks for a detail the main text lacks and the text points to the SI for it ("see Table S3", "details in the Supporting Information"). Not for the gist, a skim, or anything the main text answers.

1. **Already in the library?** `lit show <id>` prints one `SI:` line per file: its absolute path, original name and size. (`lit show <id> --format json` has the `supplementary` records but no paths.) Files there → step 4. Never probe with `lit add --si <id>`: on a paper with no SI it opens a window on the user's screen.
2. **Not there → ask litman for it.** First tell the user in one line that you are asking litman for the SI: an SI window appears on their open litman page — Open website, download the SI there, drop the files in, press Done. Then run `lit add --si <id>` (full id or unique substring) as a shell call of its own (Rule 1). It waits up to about 90 s; let it run to its end. If your shell tool times out or moves it to the background, wait for it to finish and read its output — a run your tool cut off counts as exit 11. Rerunning the identical command rejoins the same window; it never opens a second one.

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

## `tip:` lines

`lit` may print a dim `tip:` line after a command — `no lit health-check in 14+ days…`, or `no lit sync push in 7+ days…` (only when a sync remote is configured). Relay it and offer to run the command it names; never run it unasked. A first `lit sync push` prints a size preview and stops for `--yes`: relay the preview, re-run with `--yes` on the user's go.
